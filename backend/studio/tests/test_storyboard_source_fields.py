"""Frozen user fields reach M5 without fixed creative columns or another parser."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, Episode, GenerationAttempt, Shot
from studio.services.business_outputs import output_reference, write_output
from studio.services.production_runs import advance_production_run
from studio.services.production_storyboard_materialization import read_storyboard_materialization_plan
from studio.services.script_revision_structures import create_structured_script_source
from studio.services.script_semantics import parse_script_semantic_output
from studio.services.script_sources import confirm_script_revision
from studio.services.script_storyboard_fields import storyboard_source_fields
from studio.tests import test_production_manual_bible as manual
from studio.tests import test_bible_policy_continuation as recipe_fixtures
from studio.tests.output_structure_fixtures import frozen_response_structure, response_structure
from studio.tests.script_semantics_fixtures import each, literal, obj, path, script_example
from studio.tests.test_script_semantic_requests import LIMIT, reference


def document(raw, structure, mapping):
    output = reference(structure, mapping)
    report = parse_script_semantic_output(reference=output, response_text=json.dumps(raw, ensure_ascii=False), max_bytes=LIMIT)
    if not report["valid"]:
        raise AssertionError(report["errors"])
    return {"structure": {"document": {"output": output, "projection": report}}}


class StoryboardSourceFieldTests(SimpleTestCase):
    def read(self, saved, **kwargs):
        return storyboard_source_fields(saved, max_bytes=kwargs.get("max_bytes", LIMIT))

    def test_two_layouts_keep_unmapped_user_field_and_exact_field_structure_identity(self):
        for layout, name in (("a", "mood_note"), ("b", "tone")):
            raw, structure, mapping = script_example(layout)
            saved = document(raw, structure, mapping)
            before = deepcopy(saved)
            with self.subTest(layout=layout):
                fields = self.read(saved)
                self.assertEqual(list(fields), [(1, 1), (1, 2), (2, 1), (2, 2)])
                row = next(row for row in fields[1, 2] if row["value_path"][-1] == name)
                self.assertEqual(row["value"], "紧张")
                self.assertEqual(row["structure_version_id"], structure["id"])
                node = next(node for node in structure["nodes"] if node["node_key"] == row["node_key"])
                self.assertEqual(row["field_definition"], node["field_definition"])
                self.assertTrue(row["source_present"])
                self.assertNotIn(name, saved["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][1])
                self.assertEqual(saved, before)
                row["value"] = "reader mutation"
                row["field_definition"]["label"] = "reader mutation"
                self.assertEqual(saved, before)

    def test_whole_collection_mapping_uses_actual_coordinates_not_offsets_or_names(self):
        raw = {"units": [{"episode": 7, "shots": [{"shot": 19, "setting": " 原场景\n "},
            {"shot": 2, "action": "同名不匹配"}]}]}
        saved = document(raw, frozen_response_structure(raw), {"schema_version": 1,
            "node": obj(entities=literal([]), episodes=path("units", scope="root"))})
        fields = self.read(saved)
        self.assertEqual(list(fields), [(7, 19), (7, 2)])
        self.assertEqual(next(row["value"] for row in fields[7, 19] if row["value_path"][-1] == "setting"), " 原场景\n ")
        missing = next(row for row in fields[7, 2] if row["value_path"][-1] == "setting")
        self.assertFalse(missing["present"])
        self.assertNotIn("value", missing)
        self.assertTrue(all(row["value_path"][:3] == ["units", 0, "shots"] for row in fields[7, 2]))
        self.assertTrue(all(row["value_path"][3] == 1 for row in fields[7, 2]))

    def test_explicit_null_blank_false_zero_empty_and_absence_are_not_normalized(self):
        raw = {"frames": [{"number": 3, "zero": 0, "flag": False, "text": "", "none": "placeholder",
            "items": [], "object": {}, "optional": "sample"}]}
        structure = frozen_response_structure(raw)
        for node in structure["nodes"]:
            if node["property_name"] == "none": node["allow_null"] = True
            if node["property_name"] == "optional": node["required"] = False
        raw["frames"][0].update(none=None)
        del raw["frames"][0]["optional"]
        mapping = {"schema_version": 1, "node": obj(entities=literal([]), episodes={"kind": "array", "items": [
            obj(episode=literal(8), shots=each(path("frames", scope="root"), obj(shot=path("number"))))]})}
        fields = self.read(document(raw, structure, mapping))[8, 3]
        values = {row["value_path"][-1]: row for row in fields if len(row["value_path"]) == 3}
        for name in ("zero", "flag", "text", "none", "items", "object"):
            self.assertTrue(values[name]["present"])
            self.assertTrue(values[name]["source_present"])
            self.assertIs(type(values[name]["value"]), type(raw["frames"][0][name]))
            self.assertEqual(values[name]["value"], raw["frames"][0][name])
        self.assertFalse(values["optional"]["present"])
        self.assertNotIn("value", values["optional"])

    def test_configured_defaults_keep_saved_evidence_and_are_not_authored_values(self):
        raw, structure, mapping = script_example()
        for node in structure["nodes"]:
            if node["property_name"] == "mood_note":
                node.update(required=False, has_default=True, default_value="configured only")
        del raw["episodes"][0]["shots"][0]["mood_note"]
        saved = document(raw, structure, mapping)
        fields = self.read(saved)
        row = next(row for row in fields[1, 1] if row["value_path"][-1] == "mood_note")
        self.assertEqual(row["value"], "configured only")
        self.assertTrue(row["present"])
        self.assertFalse(row["source_present"])
        self.assertEqual(row["default_paths"], [row["source_path"]])
        authored = next(row for row in fields[1, 2] if row["value_path"][-1] == "mood_note")
        self.assertEqual(authored["value"], "紧张")
        self.assertTrue(authored["source_present"])
        self.assertEqual(authored["default_paths"], [])

    def test_nested_list_and_punctuation_paths_keep_node_identity_and_item_order(self):
        raw, _, mapping = script_example()
        for episode in raw["episodes"]:
            for shot in episode["shots"]:
                shot['自定义.["说明"]'] = [{"text": " 第一行 "}, {"text": "第二行\n"}]
        saved = document(raw, frozen_response_structure(raw), mapping)
        fields = self.read(saved)[1, 1]
        lines = [row for row in fields if len(row["value_path"]) == 7 and row["value_path"][-1] == "text"]
        self.assertEqual([row["value"] for row in lines], [" 第一行 ", "第二行\n"])
        self.assertEqual(lines[0]["node_key"], lines[1]["node_key"])
        self.assertNotEqual(lines[0]["value_path"], lines[1]["value_path"])
        self.assertNotEqual(lines[0]["source_path"], lines[1]["source_path"])

    def test_literal_shots_do_not_acquire_unrelated_source_fields(self):
        raw = {"setting": "unrelated", "shots": [{"shot": 1, "action": "not selected"}]}
        saved = document(raw, frozen_response_structure(raw), {"schema_version": 1, "node": obj(
            entities=literal([]), episodes=literal([{"episode": 1, "shots": [{"shot": 1, "action": "configured"}]}]))})
        self.assertEqual(self.read(saved), {(1, 1): []})

    def test_explicit_cross_scope_leaf_is_included_without_unselected_siblings(self):
        raw, _, mapping = script_example()
        raw["creative"] = {"motion": " shared exact action ", "private_note": "not selected"}
        mapping["node"]["fields"]["episodes"]["item"]["fields"]["shots"]["item"]["fields"]["action"] = path("creative", "motion", scope="root")
        fields = self.read(document(raw, frozen_response_structure(raw), mapping))[1, 1]
        self.assertEqual([row["value"] for row in fields if row["value_path"] == ["creative", "motion"]], [" shared exact action "])
        self.assertFalse(any(row["value_path"] == ["creative", "private_note"] for row in fields))

    def test_lookup_uses_selected_value_not_search_keys_or_other_entities(self):
        raw = {"texts": [{"key": "a", "text": "选中的独立字段", "extra": "not selected"},
            {"key": "b", "text": "another", "extra": "another"}], "selected": "a"}
        lookup = {"kind": "lookup", "collection": path("texts", scope="root"), "key_path": ["key"],
            "key": path("selected", scope="root"), "value_path": ["text"]}
        mapping = {"schema_version": 1, "node": obj(entities=literal([]), episodes={"kind": "array", "items": [
            obj(episode=literal(5), shots={"kind": "array", "items": [obj(shot=literal(9), action=lookup)]})]})}
        fields = self.read(document(raw, frozen_response_structure(raw), mapping))[5, 9]
        self.assertEqual([(row["value_path"], row["value"]) for row in fields], [(["texts", 0, "text"], "选中的独立字段")])

    def test_unknown_extras_are_retained_in_original_but_not_invented_as_registered_fields(self):
        raw, structure, mapping = script_example()
        raw["episodes"][0]["shots"][0]["unregistered"] = "kept raw"
        saved = document(raw, structure, mapping)
        self.assertFalse(any(row["value_path"][-1] == "unregistered" for row in self.read(saved)[1, 1]))
        self.assertEqual(saved["structure"]["document"]["projection"]["source_value"]["episodes"][0]["shots"][0]["unregistered"], "kept raw")

    def test_bad_saved_source_path_fails_instead_of_silently_dropping_fields(self):
        raw, structure, mapping = script_example()
        saved = document(raw, structure, mapping)
        for row in saved["structure"]["document"]["projection"]["provenance"]:
            if row["path"] == "value.episodes[0].shots.source": row["source_path"] = "source.not_present"
        with self.assertRaises(ValidationError): self.read(saved)

    def test_bounded_output_does_not_truncate_or_mutate_original(self):
        raw, structure, mapping = script_example()
        saved = document(raw, structure, mapping)
        before = deepcopy(saved)
        with self.assertRaises(ValidationError): self.read(saved, max_bytes=128)
        self.assertEqual(saved, before)


def saved_source(testcase):
    raw, _, mapping = script_example("b")
    structure = response_structure(example=raw)
    output = write_output(actor=testcase.actor, data={"name": "Isolated independent shot fields",
        "parser_key": "script_semantics.v1", "data_structure_version": str(structure.pk), "semantic_mapping": mapping})
    output = write_output(actor=testcase.actor, output_id=output.pk, expected_revision=output.revision, action="publish")
    _, revision, _ = create_structured_script_source(project=testcase.project, user=testcase.actor,
        content=json.dumps(raw, ensure_ascii=False), output=output_reference(output), source_type="paste", max_bytes=LIMIT)
    return confirm_script_revision(revision, user=testcase.actor)


def assert_fields(testcase, run):
    before = list(ApprovalDecision.objects.values())
    with patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls")), \
            patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("No reparsing")), \
            CaptureQueriesContext(connection) as queries:
        plan = read_storyboard_materialization_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
    testcase.assertFalse(any(row["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for row in queries))
    testcase.assertFalse(any(table in row["sql"].lower() for row in queries for table in (
        '"studio_fielddefinition', '"studio_datastructure', '"studio_businessoutput')))
    fields = plan.source.episodes[0]["shots"][0]["source_fields"]
    tone = next(row for row in fields if row["value_path"][-1] == "tone")
    testcase.assertEqual(tone["value"], "紧张")
    testcase.assertEqual(tone["source_path"], "source.chapters[0].frames[0].tone")
    testcase.assertNotIn("tone", str(plan.contract.value))
    testcase.assertEqual(plan.source.basis["script"]["id"], str(testcase.revision.pk))
    testcase.assertEqual(list(ApprovalDecision.objects.values()), before)
    testcase.assertFalse(Episode.objects.exists())
    testcase.assertFalse(Shot.objects.exists())
    testcase.assertFalse(GenerationAttempt.objects.exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualStoryboardSourceFieldTests(TestCase):
    create = manual.ProductionManualBibleTests.create
    post = manual.ProductionManualBibleTests.post
    read = manual.ProductionManualBibleTests.read
    adopt_source = manual.ProductionManualBibleTests.adopt_source
    approval = manual.ProductionManualBibleTests.approval

    def test_original_import_adoption_and_bible_review_preserve_independent_fields(self):
        with patch("studio.tests.test_production_manual_bible.confirmed_bible_source", side_effect=saved_source):
            manual.ProductionManualBibleTests.setUp(self)
        self.adopt_source()
        result = self.post(self.approval())
        self.assertEqual(result.status_code, 201, result.data)
        assert_fields(self, self.run)
        self.assertTrue(all(row.actor == "human" for row in ApprovalDecision.objects.all()))


class RecipeStoryboardSourceFieldTests(TestCase):
    root = recipe_fixtures.BiblePolicyContinuationTests.root
    recipe = recipe_fixtures.BiblePolicyContinuationTests.recipe
    build = recipe_fixtures.BiblePolicyContinuationTests.build

    def test_original_recipe_source_and_policy_do_not_invent_human_adoption(self):
        with patch("studio.tests.test_bible_policy_continuation.confirmed_structured_bible_source",
                side_effect=lambda testcase, **_: saved_source(testcase)):
            recipe_fixtures.BiblePolicyContinuationTests.setUp(self)
        run = advance_production_run(self.build(), automatic=True)
        assert_fields(self, run)
        self.assertEqual(ApprovalDecision.objects.get().actor, "recipe_policy")
