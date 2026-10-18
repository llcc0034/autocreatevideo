"""Exact-relation preflight, saved M2 fixtures and original import/review API.

No Episode/Shot writer or deployed new-chain acceptance is claimed here.
"""
from copy import deepcopy
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, BibleVariant, Episode, GenerationAttempt, ScriptBibleEntityBinding, Shot)
from studio.services.bible_content_review import approve_bible_content, bible_review_inventory
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.script_storyboard_plan import _resolve_relations, read_storyboard_source_plan
from studio.tests import test_bible_content_review as content_fixtures
from studio.tests import test_production_manual_bible as manual_fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source
from studio.services.script_bible_materialization import _confirmed_reference_source


class StoryboardRelationResolutionTests(TestCase):
    source_revision = content_fixtures.BibleContentReviewTests.source_revision
    frozen = content_fixtures.BibleContentReviewTests.frozen
    apply = content_fixtures.BibleContentReviewTests.apply
    run_context = content_fixtures.BibleContentReviewTests.run_context
    request = content_fixtures.BibleContentReviewTests.request
    manual_primary_before_appearance_declarations = content_fixtures.BibleContentReviewTests.manual_primary_before_appearance_declarations

    def setUp(self):
        content_fixtures.BibleContentReviewTests.setUp(self)
        def use(key, entity, variant=None):
            return {"use_key": key, "entity_key": entity, "variant_key": variant, "media_kind": "image", "purpose": "appearance"}
        # Non-consecutive coordinates are explicit semantics, not array offsets.
        episodes = [{"episode": 4, "shots": [
            {"shot": 8, "scene_key": "station", "character_keys": ["person_a"], "prop_keys": [],
                "dialogue": {"speaker_entity_key": "person_a", "text": "原对白"},
                "references": [use("hero", "person_a", "uniform"), use("place", "station")]},
            {"shot": 2, "scene_key": "station", "character_keys": [], "prop_keys": [],
                "references": [use("hero", "person_a", "raincoat")]}]},
            {"episode": 9, "shots": [{"shot": 3, "references": []}]}]
        self.revision = confirmed_structured_bible_source(self, entities=self.declarations["entities"], episodes=episodes)
        self.document = _confirmed_reference_source(revision=self.revision, project_id=self.project.pk)

    def resolved(self, document=None, reviewed=None, bindings=None):
        if reviewed is None:
            baseline, _, _ = bible_review_inventory(run=self.run_context())
            reviewed = {"canonical": baseline.value}
        if bindings is None:
            bindings = {row.entity_key: row for row in ScriptBibleEntityBinding.objects.filter(script_revision=self.revision)}
        return _resolve_relations(document or deepcopy(self.document), bindings, reviewed)

    def test_explicit_relations_preserve_empty_sets_and_do_not_infer_from_body_or_uses(self):
        approve_bible_content(**self.request())
        document = deepcopy(self.document)
        document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][1].update(scene_key=None, character_keys=[], prop_keys=[])
        result = self.resolved(document)
        self.assertEqual([(row["episode"], row["shot"]) for row in result], [(4, 8), (4, 2), (9, 3)])
        self.assertIsNone(result[1]["scene"])
        self.assertEqual(result[1]["visible_characters"], [])
        self.assertEqual(result[1]["participant_character_ids"], [])
        self.assertEqual(result[1]["uses"][0]["variant_key"], "raincoat")
        self.assertEqual(result[2]["uses"], [])
        self.assertEqual(result[0]["speaker"]["entity_key"], "person_a")

    def test_offscreen_speaker_is_a_participant_not_a_visible_character(self):
        approve_bible_content(**self.request())
        document = deepcopy(self.document)
        document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["character_keys"] = []
        document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["dialogue"]["text"] = "A different display name"
        row = self.resolved(document)[0]
        self.assertEqual(row["visible_characters"], [])
        self.assertEqual(row["participant_character_ids"], [row["speaker"]["entity_id"]])
        self.assertRegex(row["speaker"]["speaker_id"], r"^S[1-9][0-9]*$")
        self.assertEqual(row["uses"][0]["reference_name"], "海遥·制服")

    def test_two_configured_layouts_resolve_same_scene_prop_voiceover_and_ordered_uses(self):
        from studio.services.script_semantics import parse_script_semantic_output
        from studio.tests.output_structure_fixtures import frozen_response_structure
        from studio.tests.script_semantics_fixtures import each, obj, path
        from studio.tests.test_script_semantic_requests import LIMIT, reference

        self.declarations["entities"].append({"key": "case", "kind": "prop", "name": "同名也不按名字匹配",
            "primary": {"reference_name": "盒子·主图", "anchor": "木盒"}})
        shot = {"shot": 8, "scene_key": "station", "character_keys": [], "prop_keys": ["case"],
            "dialogue": {"speaker_entity_key": "person_a", "text": "站台上没有可见人物。", "is_voiceover": True},
            "references": [{"use_key": "clothes", "entity_key": "person_a", "variant_key": "uniform",
                "media_kind": "image", "purpose": "appearance"},
                {"use_key": "motion", "entity_key": "person_a", "variant_key": "uniform",
                    "media_kind": "video", "purpose": "configured-motion"}]}
        self.revision = confirmed_structured_bible_source(self, entities=self.declarations["entities"],
            episodes=[{"episode": 4, "shots": [shot]}])
        self.document = _confirmed_reference_source(revision=self.revision, project_id=self.project.pk)
        approve_bible_content(**self.request())
        raw_a = deepcopy(self.document["structure"]["document"]["projection"]["value"])
        names = {"shot": "index", "scene_key": "location", "character_keys": "on_screen",
            "prop_keys": "items", "dialogue": "speech", "references": "media"}
        raw_b = {"cast": raw_a["entities"], "chapters": [{"number": 4,
            "frames": [{names[key]: value for key, value in shot.items()}]}]}
        node_a = obj(entities=path("entities", scope="root"), episodes=path("episodes", scope="root"))
        node_b = obj(entities=path("cast", scope="root"),
            episodes=each(path("chapters", scope="root"), obj(episode=path("number"),
                shots=each(path("frames"), obj(**{key: path(name) for key, name in names.items()})))))
        before_shots = list(Shot.objects.filter(episode__project=self.project).order_by("pk").values())
        results = []
        for raw, node in ((raw_a, node_a), (raw_b, node_b)):
            report = parse_script_semantic_output(reference=reference(frozen_response_structure(raw),
                {"schema_version": 1, "node": node}), response_text=json.dumps(raw, ensure_ascii=False), max_bytes=LIMIT)
            self.assertTrue(report["valid"], report["errors"])
            document = deepcopy(self.document)
            document["structure"]["document"]["projection"]["value"] = report["value"]
            document["structure"]["document"]["projection"]["provenance"] = report["provenance"]
            with CaptureQueriesContext(connection) as queries:
                results.append(self.resolved(document))
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for q in queries))
            for invalid_key, code in (("missing", "entity_unknown"), ("case", "participant_duplicate")):
                broken = deepcopy(document)
                broken["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["prop_keys"].append(invalid_key)
                with self.assertRaises(ValidationError) as caught:
                    self.resolved(broken)
                issue = next(item for item in caught.exception.relation_errors if item["code"] == code)
                source_path = ("source.episodes[0].shots[0].prop_keys[1]" if raw is raw_a
                    else "source.chapters[0].frames[0].items[1]")
                self.assertEqual(issue["path"], "value.episodes[0].shots[0].prop_keys[1]")
                self.assertEqual(issue["source_path"], source_path)
                self.assertIn(source_path, caught.exception.message_dict)
        self.assertEqual(results[0], results[1])
        row = results[0][0]
        self.assertEqual(row["visible_characters"], [])
        self.assertEqual(row["speaker"]["entity_key"], "person_a")
        self.assertEqual(row["scene"]["entity_key"], "station")
        self.assertEqual([item["entity_key"] for item in row["props"]], ["case"])
        self.assertEqual([item["use_key"] for item in row["uses"]], ["clothes", "motion"])
        self.assertEqual(list(Shot.objects.filter(episode__project=self.project).order_by("pk").values()), before_shots)

    def test_same_setting_multiple_uses_keeps_order_and_distinct_purposes(self):
        approve_bible_content(**self.request())
        document = deepcopy(self.document)
        uses = document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["references"]
        uses.append({**uses[0], "use_key": "movement", "media_kind": "video", "purpose": "movement"})
        row = self.resolved(document)[0]
        self.assertEqual([item["use_key"] for item in row["uses"]], ["hero", "place", "movement"])
        self.assertEqual(row["uses"][0]["setting_revision_id"], row["uses"][2]["setting_revision_id"])
        self.assertFalse({"identity_anchor", "appearance_anchor", "asset_id", "slot"} & set(row["uses"][0]))

    def test_current_reviewed_revision_and_fixed_historical_primary_not_original_revision_one(self):
        self.apply()
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        original = variant.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(original), "reference_name": "Uniforme azul", "appearance_anchor": "蓝色制服"})
        approve_bible_content(**self.request())
        result = self.resolved()[0]["uses"][0]
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["reference_name"], "Uniforme azul")
        self.assertEqual(result["primary_setting"]["setting_revision_id"], str(original.primary_setting_revision_id))
        self.assertNotEqual(result["setting_revision_id"], str(original.pk))

    def test_new_source_resolves_reused_looks_and_ignores_other_source_extra_looks(self):
        approve_bible_content(**self.request())
        first = self.resolved()
        original_document, original_revision = deepcopy(self.document), self.revision
        first_bindings = {row.entity_key: row for row in ScriptBibleEntityBinding.objects.filter(script_revision=self.revision)}
        self.declarations["entities"][0]["variants"].append({"key": "coat", "name": "外套",
            "reference_name": "海遥·外套", "anchor": "穿外套"})
        episodes = deepcopy(self.document["structure"]["document"]["projection"]["value"]["episodes"])
        self.revision = confirmed_structured_bible_source(self, entities=self.declarations["entities"], episodes=episodes)
        self.document = _confirmed_reference_source(revision=self.revision, project_id=self.project.pk)
        approve_bible_content(**self.request())
        current = self.resolved()
        self.assertEqual(current[0]["speaker"]["entity_id"], first[0]["speaker"]["entity_id"])
        self.assertEqual(current[0]["uses"][0]["appearance_id"], first[0]["uses"][0]["appearance_id"])
        self.assertNotEqual(current[0]["speaker"]["binding_id"], first[0]["speaker"]["binding_id"])
        self.revision = original_revision
        self.assertEqual(self.resolved(document=original_document, bindings=first_bindings), first)

    def test_name_changes_never_change_id_resolution(self):
        approve_bible_content(**self.request())
        binding = ScriptBibleEntityBinding.objects.get(entity_key="person_a")
        expected = str(binding.character_id)
        binding.character.name = "人工更名 / Renommé"
        binding.character.save(update_fields=("name",))
        self.assertEqual(self.resolved()[0]["speaker"]["entity_id"], expected)

    def test_manual_primary_origin_stays_null_when_used_by_declared_storyboard_relations(self):
        episodes = deepcopy(self.document["structure"]["document"]["projection"]["value"]["episodes"])
        primary = self.manual_primary_before_appearance_declarations()
        episodes[0]["shots"][0]["references"].append({"use_key": "manual-base", "entity_key": "person_a",
            "variant_key": None, "media_kind": "image", "purpose": "appearance"})
        self.revision = confirmed_structured_bible_source(self, entities=self.declarations["entities"], episodes=episodes)
        self.document = _confirmed_reference_source(revision=self.revision, project_id=self.project.pk)
        approve_bible_content(**self.request())
        result = self.resolved()[0]["uses"]
        self.assertEqual(result[-1]["appearance_id"], str(primary.pk))
        self.assertEqual(result[-1]["reference_name"], primary.reference_name)
        self.assertEqual(result[0]["primary_setting"]["setting_revision_id"], str(primary.revisions.get().pk))
        primary.refresh_from_db()
        self.assertIsNone(primary.source_binding_id)
        baseline, _, _ = bible_review_inventory(run=self.run_context())
        row = next(row for row in baseline.value["characters"] if row["id"] == str(primary.character_id))
        entry = next(item for item in row["appearance_settings"] if item["appearance_id"] == str(primary.pk))
        self.assertIsNone(entry["source"])
        entry["source"] = {"binding_id": "invented"}
        with self.assertRaises(ValidationError): self.resolved(reviewed={"canonical": baseline.value})

    def test_missing_unknown_duplicate_and_wrong_kind_relations_fail_closed(self):
        approve_bible_content(**self.request())
        for key, value in (("character_keys", ["station"]), ("character_keys", ["person_a", "person_a"]),
                ("scene_key", "missing"), ("scene_key", "person_a"), ("speaker_entity_key", None)):
            document = deepcopy(self.document)
            target = document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]
            (target["dialogue"] if key == "speaker_entity_key" else target)[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                self.resolved(document)
        for references in ([{"use_key": "x", "entity_key": "station", "variant_key": "uniform", "media_kind": "image", "purpose": "appearance"}],
                self.document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["references"] * 2):
            document = deepcopy(self.document)
            document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"][0]["references"] = references
            with self.assertRaises(ValidationError): self.resolved(document)
        with self.assertRaises(ValidationError): self.resolved(bindings={})
        document = deepcopy(self.document)
        shots = document["structure"]["document"]["projection"]["value"]["episodes"][0]["shots"]
        shots.append(deepcopy(shots[0]))
        with self.assertRaises(ValidationError): self.resolved(document)

    def test_unreviewed_wrong_source_and_missing_appearance_are_rejected(self):
        self.apply()
        with self.assertRaises(ValidationError): self.resolved()
        approve_bible_content(**self.request())
        baseline, _, _ = bible_review_inventory(run=self.run_context())
        canonical = baseline.value
        source = next(row for row in canonical["characters"] if row.get("appearance_settings"))
        for mutation in ("revision", "binding", "missing", "duplicate"):
            broken = deepcopy(canonical)
            row = next(row for row in broken["characters"] if row["id"] == source["id"])
            if mutation == "revision": row["appearance_settings"][0]["source"]["script_revision_id"] = "other"
            if mutation == "binding": row["appearance_settings"][0]["source"]["binding_id"] = "other"
            if mutation == "missing": row["appearance_settings"].pop()
            if mutation == "duplicate": row["appearance_settings"].append(deepcopy(row["appearance_settings"][0]))
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                self.resolved(reviewed={"canonical": broken})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class StoryboardSourcePlanIntegrationTests(TestCase):
    create = manual_fixtures.ProductionManualBibleTests.create
    post = manual_fixtures.ProductionManualBibleTests.post
    read = manual_fixtures.ProductionManualBibleTests.read
    adopt_source = manual_fixtures.ProductionManualBibleTests.adopt_source
    approval = manual_fixtures.ProductionManualBibleTests.approval

    def setUp(self):
        original_source = manual_fixtures.confirmed_bible_source
        def source(testcase):
            return original_source(testcase, episodes=[{"episode": 4, "shots": [{"shot": 8,
                "scene_key": "room", "character_keys": [], "prop_keys": ["box"],
                "dialogue": {"speaker_entity_key": "traveler", "delivery": "voiceover, softly", "is_voiceover": True,
                    "text": "原对白不翻译。", "start_ms": 300, "end_ms": 1500},
                "references": [{"use_key": "hero", "entity_key": "traveler", "variant_key": "raincoat", "media_kind": "image", "purpose": "appearance"}]}]}])
        with patch("studio.tests.test_production_manual_bible.confirmed_bible_source", side_effect=source):
            manual_fixtures.ProductionManualBibleTests.setUp(self)

    def plan(self):
        return read_storyboard_source_plan(run=self.run, stage=self.run.stages.get(stage_key="storyboard"))

    def test_original_import_adoption_and_human_review_resolve_readonly_without_new_tasks(self):
        with self.assertRaises(ValidationError): self.plan()
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        attempts = list(GenerationAttempt.objects.values_list("pk", "request_sha256"))
        reviews = list(ApprovalDecision.objects.values_list("pk", "output_snapshot_sha256"))
        with CaptureQueriesContext(connection) as queries:
            result = self.plan()
        mutations = [item["sql"] for item in queries if item["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}]
        self.assertEqual(mutations, [])
        binding_queries = [item["sql"] for item in queries if 'FROM "studio_scriptbibleentitybinding"' in item["sql"]]
        self.assertEqual(len(binding_queries), 2)  # Source approval validation plus this one bulk resolver read.
        explanation = ScriptBibleEntityBinding.objects.filter(script_revision=self.revision).only(
            "id", "script_revision_id", "entity_key", "declaration_sha256", "character_id", "scene_id", "prop_id").explain()
        if connection.vendor == "sqlite":
            self.assertRegex(explanation, r"SEARCH studio_scriptbibleentitybinding USING INDEX")
        self.assertEqual(result.basis["script"]["id"], str(self.revision.pk))
        self.assertEqual(result, self.plan())
        self.assertTrue(result.provenance)
        self.assertNotIn("provenance", result.basis)  # On-demand detail, not another frozen source copy.
        self.assertEqual(len(result.episodes[0]["shots"][0]["relations"]["uses"]), 1)
        shot = result.episodes[0]["shots"][0]
        self.assertEqual((result.episodes[0]["number"], shot["number"]), (4, 8))
        self.assertEqual(shot["relations"]["visible_characters"], [])
        self.assertEqual(shot["relations"]["scene"]["entity_key"], "room")
        self.assertEqual(shot["relations"]["props"][0]["entity_key"], "box")
        self.assertEqual(shot["relations"]["participant_character_ids"], [shot["relations"]["speaker"]["entity_id"]])
        self.assertEqual(shot["body"]["dialogue"], {"speaker_entity_key": "traveler", "delivery": "voiceover, softly", "is_voiceover": True,
            "text": "原对白不翻译。", "start_ms": 300, "end_ms": 1500})
        self.assertEqual(list(GenerationAttempt.objects.values_list("pk", "request_sha256")), attempts)
        self.assertEqual(list(ApprovalDecision.objects.values_list("pk", "output_snapshot_sha256")), reviews)
        self.assertFalse(Episode.objects.filter(project=self.project).exists())
        self.assertFalse(Shot.objects.filter(episode__project=self.project).exists())

    def test_changed_review_and_foreign_stage_are_not_accepted(self):
        self.adopt_source(); self.assertEqual(self.post(self.approval()).status_code, 201)
        before = self.plan()
        variant = BibleVariant.objects.get(source_binding__script_revision=self.revision, source_variant_key="raincoat")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get(revision=1)), "appearance_anchor": "明确蓝衣"})
        with self.assertRaises(ValidationError): self.plan()
        with self.assertRaises(ValidationError):
            read_storyboard_source_plan(run=self.run, stage=self.run.stages.get(stage_key="script_input"))
        self.assertTrue(before.basis["resolved_relations_sha256"])
        self.assertFalse(Episode.objects.filter(project=self.project).exists())
