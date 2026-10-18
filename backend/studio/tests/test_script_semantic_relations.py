"""Finite relation checks on configurable inputs; no DB or external calls."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.data_structures import _value_errors
from studio.services.json_contracts import JsonExpansionBudget
from studio.services.script_semantic_relations import VALIDATION_SCOPE, script_relation_errors
from studio.services.script_semantics import (
    freeze_script_semantics, parse_script_semantic_output, preview_script_semantics, script_semantic_schema,
)
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.script_semantics_fixtures import path, script_example
from studio.tests.test_script_semantic_requests import LIMIT, reference


class ScriptSemanticRelationTests(SimpleTestCase):
    def setUp(self):
        raw, source, mapping = script_example()
        self.value = self.parse(raw, source, mapping)["value"]

    def parse(self, raw, source, mapping):
        return parse_script_semantic_output(reference=reference(source, mapping),
            response_text=json.dumps(raw, ensure_ascii=False), max_bytes=LIMIT)

    def errors(self, value=None, *, provenance=()):
        value = self.value if value is None else value
        self.assertEqual(_value_errors(script_semantic_schema(), value, path="value"), {})
        before = deepcopy(value)
        result = script_relation_errors(value, provenance=provenance,
            consume=JsonExpansionBudget(LIMIT, path="relations").consume)
        self.assertEqual(value, before, "relations must never repair/reorder the authored content")
        return result

    def codes(self, value=None):
        return {row["code"] for row in self.errors(value)}

    def shot(self):
        return self.value["episodes"][0]["shots"][0]

    def test_both_layouts_validate_without_mutation_and_report_actual_source_path(self):
        for layout in ("a", "b"):
            raw, source, mapping = script_example(layout)
            self.assertTrue(self.parse(raw, source, mapping)["valid"])
            ref = (raw["episodes"][0]["shots"][0]["references"][0] if layout == "a"
                else raw["chapters"][0]["frames"][0]["uses"][0])
            ref["variant_key"] = "not-this-entity"
            report = self.parse(raw, source, mapping)
            self.assertFalse(report["valid"])
            self.assertEqual(report["source_value"], raw)
            self.assertEqual(report["validation_scope"], VALIDATION_SCOPE)
            self.assertEqual(report["relation_errors"], [{
                "path": "value.episodes[0].shots[0].references[0].variant_key", "code": "variant_unknown",
                "message": "变体未声明或属于另一实体，不回退主图", "origin_kind": "path",
                "source_path": ("source.episodes[0].shots[0].references[0].variant_key" if layout == "a"
                    else "source.chapters[0].frames[0].uses[0].variant_key")}])

    def test_config_preview_remains_explicitly_structure_only(self):
        raw, source, mapping = script_example()
        raw["entities"].append(deepcopy(raw["entities"][0]))
        preview = preview_script_semantics(configuration=freeze_script_semantics(
            source_contract=source, mapping=mapping, max_bytes=LIMIT), response_text=json.dumps(raw), max_bytes=LIMIT)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["validation_scope"], "structure_and_mapping")
        self.assertFalse(self.parse(raw, source, mapping)["valid"])

    def test_duplicate_entity_and_variant_keys_are_ambiguous_not_first_match(self):
        self.value["entities"].append(deepcopy(self.value["entities"][0]))
        self.assertTrue({"entity_duplicate", "entity_ambiguous"} <= self.codes())
        self.value["entities"].pop()
        entity = self.value["entities"][0]
        entity["variants"].append(deepcopy(entity["variants"][0]))
        self.assertTrue({"variant_duplicate", "variant_ambiguous"} <= self.codes())

    def test_variant_key_is_local_to_entity_and_never_falls_back_to_main(self):
        entity = deepcopy(self.value["entities"][0])
        entity["key"] = "another-person"
        entity.pop("primary")
        for variant in entity["variants"]:
            variant.pop("reference_name")
        self.value["entities"].append(entity)
        self.assertEqual(self.errors(), [])
        entity["variants"].append({"key": "only-other", "name": "某变体"})
        self.shot()["references"][0]["variant_key"] = "only-other"
        self.assertEqual(self.codes(), {"variant_unknown"})

    def test_reference_names_unique_without_requiring_english_or_unique_display_names(self):
        self.value["entities"][0]["primary"]["reference_name"] = "ＡＢＣ"
        self.value["entities"][0]["variants"][0]["reference_name"] = "abc"
        self.assertEqual(self.codes(), {"reference_name_duplicate"})
        self.value["entities"][0]["variants"][0]["reference_name"] = "人物·外套"
        self.assertEqual(self.errors(), [])

    def test_duplicate_coordinates_rejected_but_gaps_and_authored_order_preserved(self):
        self.value["episodes"][1]["episode"] = 1
        self.assertIn("episode_duplicate", self.codes())
        self.value["episodes"][1]["episode"] = 7
        self.shot()["shot"] = 2
        self.assertIn("shot_duplicate", self.codes())
        self.shot()["shot"] = 19
        self.value["episodes"].reverse()
        self.assertEqual(self.errors(), [])

    def test_only_supplied_typed_participation_is_checked(self):
        self.shot().update(scene_key="person-1", character_keys=["person-1", "person-1"], prop_keys=["missing"])
        self.assertEqual(self.codes(), {"entity_kind_mismatch", "participant_duplicate", "entity_unknown"})
        self.value["entities"].extend([{"key": "room", "kind": "scene", "name": "测试人物"},
            {"key": "item", "kind": "prop", "name": "测试人物"}])
        self.shot().update(scene_key="room", character_keys=[], prop_keys=["item"])
        self.assertEqual(self.errors(), [])

    def test_uses_are_per_shot_and_multiple_media_need_not_be_visible(self):
        self.shot()["character_keys"] = []
        ref = self.shot()["references"][0]
        for kind in ("video", "audio"):
            self.shot()["references"].append({**ref, "use_key": kind, "media_kind": kind, "purpose": "用户定义用途"})
        self.assertEqual(self.errors(), [])
        self.shot()["references"].append(deepcopy(ref))
        self.assertEqual(self.codes(), {"use_duplicate"})
        ref["entity_key"] = "测试人物"
        self.assertIn("entity_unknown", self.codes())

    def test_dialogue_speaker_is_explicit_and_never_inferred_from_text_or_visible_people(self):
        self.shot()["character_keys"] = ["person-1"]
        dialogue = self.shot()["dialogue"]
        dialogue["speaker_entity_key"] = None
        self.assertEqual(self.codes(), {"speaker_binding_invalid"})
        dialogue["speaker_entity_key"] = "person-1"
        dialogue["text"] = ""
        self.assertEqual(self.codes(), {"speaker_binding_invalid"})
        dialogue.update(speaker_entity_key=None, start_ms=0, end_ms=0)
        self.assertEqual(self.errors(), [])

    def test_voiceover_false_true_and_absence_never_add_visible_people(self):
        dialogue = self.shot()["dialogue"]
        self.shot()["character_keys"] = []
        for flag in (False, True):
            dialogue["is_voiceover"] = flag
            self.assertEqual(self.errors(), [])
            self.assertIs(dialogue["is_voiceover"], flag)
            self.assertEqual(self.shot()["character_keys"], [])
        del dialogue["is_voiceover"]
        self.assertEqual(self.errors(), [])
        self.assertNotIn("is_voiceover", dialogue)
        self.assertEqual(dialogue["text"], "等一下！")

    def test_missing_voiceover_uses_only_configured_default_and_keeps_raw(self):
        raw, source, mapping = script_example()
        del raw["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"]
        for flag in (False, True):
            next(n for n in source["nodes"] if n["property_name"] == "is_voiceover").update(has_default=True, default_value=flag)
            report = self.parse(raw, source, mapping)
            self.assertTrue(report["valid"], report["errors"])
            self.assertIs(report["value"]["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"], flag)
            self.assertNotIn("is_voiceover", report["raw_value"]["episodes"][0]["shots"][0]["dialogue"])
            self.assertTrue(report["defaults"])

    def test_supplied_times_must_agree_without_filling_optional_clock_fields(self):
        shot = self.shot()
        shot.update(episode_start_ms=0, episode_end_ms=5000)
        self.assertEqual(self.errors(), [])
        shot["episode_end_ms"] = 4000
        self.assertEqual(self.codes(), {"shot_duration_mismatch"})
        shot["episode_end_ms"] = 0
        self.assertEqual(self.codes(), {"shot_interval_invalid"})
        del shot["episode_end_ms"]
        shot["dialogue"].update(start_ms=6000, end_ms=6000)
        self.assertEqual(self.codes(), {"dialogue_interval_invalid", "dialogue_outside_shot"})
        del shot["duration_ms"]
        del shot["dialogue"]["end_ms"]
        self.assertEqual(self.errors(), [])

    def test_empty_and_minimal_optional_content_has_no_creative_quality_gate(self):
        for value in ({"entities": [], "episodes": []}, {"entities": [], "episodes": [{"episode": 1, "shots": [{"shot": 3}]}]}):
            self.assertEqual(self.errors(value), [])

    def test_literal_mapping_is_not_reported_as_source_text(self):
        raw, source, mapping = script_example()
        mapping["node"]["fields"]["entities"]["item"]["fields"]["variants"] = {
            "kind": "literal", "value": [{"key": "dup", "name": "一"}, {"key": "dup", "name": "二"}]}
        report = self.parse(raw, source, mapping)
        error = next(e for e in report["relation_errors"] if e["code"] == "variant_duplicate")
        self.assertEqual(error["origin_kind"], "literal")
        self.assertNotIn("source_path", error)

    def test_source_coordinates_support_list_root_and_literal_dotted_names(self):
        raw, _, mapping = script_example()
        raw["entities"][0]["variants"][1]["key"] = "uniform"
        raw = [{"cast.items": raw["entities"], "part.一": raw["episodes"]}]
        source = frozen_response_structure(raw)
        mapping["node"]["fields"]["entities"]["source"] = path(0, "cast.items", scope="root")
        mapping["node"]["fields"]["episodes"]["source"] = path(0, "part.一", scope="root")
        report = self.parse(raw, source, mapping)
        error = next(e for e in report["relation_errors"] if e["code"] == "variant_duplicate")
        self.assertEqual(error["source_path"], 'source[0]["cast.items"][0].variants[1].key')

    def test_lookup_diagnostic_points_to_selected_value_not_key_or_collection(self):
        raw, source, mapping = script_example()
        raw["entities"][0]["variants"][1]["key"] = "uniform"
        mapping["node"]["fields"]["entities"]["item"]["fields"]["variants"] = {
            "kind": "lookup", "collection": path("entities", scope="root"),
            "key_path": ["key"], "key": path("key"), "value_path": ["variants"]}
        report = self.parse(raw, source, mapping)
        error = next(e for e in report["relation_errors"] if e["code"] == "variant_duplicate")
        self.assertEqual(error["origin_kind"], "lookup")
        self.assertEqual(error["source_path"], "source.entities[0].variants[1].key")

    def test_invalid_types_never_enter_relationship_walk(self):
        raw, source, mapping = script_example()
        raw["entities"] = False
        with patch("studio.services.script_semantics.script_relation_errors", side_effect=AssertionError("bad shape")):
            self.assertFalse(self.parse(raw, source, mapping)["valid"])

    def test_relation_diagnostics_share_parse_budget_and_never_leave_valid_true(self):
        raw, source, mapping = script_example()
        with patch("studio.services.script_semantics.script_relation_errors",
                side_effect=ValidationError({"script_semantics.parse_budget": "budget exceeded"})):
            report = self.parse(raw, source, mapping)
        self.assertFalse(report["valid"])
        self.assertIn("script_semantics.parse_budget", report["errors"])
        self.value["entities"].append(deepcopy(self.value["entities"][0]))
        with self.assertRaises(ValidationError):
            script_relation_errors(self.value, provenance=[], consume=JsonExpansionBudget(1, path="relations").consume)
