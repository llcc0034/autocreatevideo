"""M5 common source/config contract; no active entry/body-writer acceptance.

M2 import/adoption and M3 approval are original isolated writers. All proposed
contract reads must leave business rows unchanged. No real provider or media.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, Asset, BibleVariant, Episode, ExecutionPreparation,
    GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ProductionRun, Shot,
    ShotCharacterRelation, ShotReferenceUse, StageRun)
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import advance_production_run
from studio.services.production_storyboard_materialization import (
    _creation_rows, declared_storyboard_parameters, declared_storyboard_timings, read_storyboard_materialization_plan,
)
from studio.services.script_storyboard_plan import StoryboardSourcePlan
from studio.services.security_boundaries import boundary_int
from studio.tests import test_bible_policy_continuation as recipe_fixtures
from studio.tests import test_production_manual_bible as manual_fixtures


class DeclaredStoryboardTimingTests(SimpleTestCase):
    parameters = {"default_shot_duration_ms": 4000, "video_shot_min_duration_ms": 1000,
        "video_shot_max_duration_ms": 10000}

    def test_created_episode_preserves_explicit_frozen_audio_policy(self):
        for enabled in (False, True):
            source = StoryboardSourcePlan(basis={}, provenance=[], episodes=[{
                "number": 3, "title": "", "shots": [{"number": 2, "body": {}, "relations": {}}]}])
            parameters = {"multi_shot": False, "include_audio": enabled,
                "default_shot_media_type": "video", "default_first_frame_continuity": False,
                "default_last_frame_continuity": False}
            episodes, shots = _creation_rows(source, parameters, {(3, 2): {"duration_ms": 4000}})
            self.assertEqual(episodes[3]["prompt_settings"], {"multi_shot": False, "include_audio": enabled})
            self.assertIs(shots[3, 2]["include_audio"], enabled)

    def source(self, bodies):
        return StoryboardSourcePlan(basis={}, provenance=[], episodes=[{"number": 4, "title": "",
            "shots": [{"number": number, "body": body, "relations": {}}
                for number, body in zip((8, 2, 9), bodies)]}])

    def timings(self, source):
        return declared_storyboard_timings(source=source, parameters=self.parameters)

    def test_authored_duration_interval_and_frozen_default_have_distinct_precedence(self):
        source = self.source([{"duration_ms": 2000}, {"episode_start_ms": 0, "episode_end_ms": 3000}, {}])
        original = deepcopy(source)
        self.assertEqual(self.timings(source), {
            (4, 8): {"duration_ms": 2000, "episode_start_ms": None, "episode_end_ms": None},
            (4, 2): {"duration_ms": 3000, "episode_start_ms": 0, "episode_end_ms": 3000},
            (4, 9): {"duration_ms": 4000, "episode_start_ms": None, "episode_end_ms": None}})
        self.assertEqual(source, original)

    def test_inclusive_limits_do_not_round_or_synthesize_episode_positions(self):
        source = self.source([{"duration_ms": 1000}, {"duration_ms": 10000}, {"duration_ms": 2345}])
        result = self.timings(source)
        self.assertEqual([row["duration_ms"] for row in result.values()], [1000, 10000, 2345])
        self.assertTrue(all(row["episode_start_ms"] is None for row in result.values()))

    def test_outside_policy_reports_original_array_paths_without_splitting_or_clamping(self):
        source = self.source([{"duration_ms": 999}, {"episode_start_ms": 0, "episode_end_ms": 10001}])
        original = deepcopy(source)
        with self.assertRaises(ValidationError) as caught: self.timings(source)
        self.assertEqual(set(caught.exception.message_dict), {
            "value.episodes[0].shots[0].duration_ms", "value.episodes[0].shots[1].duration_ms"})
        self.assertEqual(source, original)

    def test_partial_or_disagreeing_episode_interval_is_not_repaired(self):
        for body in ({"episode_start_ms": 0}, {"episode_end_ms": 3000},
                {"duration_ms": 2000, "episode_start_ms": 0, "episode_end_ms": 3000}):
            with self.subTest(body=body), self.assertRaises(ValidationError): self.timings(self.source([body]))

    def test_dialogue_uses_effective_default_or_interval_duration_and_preserves_zero(self):
        for body in ({"dialogue": {"text": "原文", "end_ms": 4001}},
                {"episode_start_ms": 0, "episode_end_ms": 2000, "dialogue": {"text": "原文", "start_ms": 2001}}):
            with self.subTest(body=body), self.assertRaises(ValidationError): self.timings(self.source([body]))
        source = self.source([{"dialogue": {"text": "", "start_ms": 0, "end_ms": 0, "is_voiceover": False}},
            {"dialogue": {"text": "No timing"}}, {"dialogue": {"text": "Exact end", "end_ms": 4000}}])
        original = deepcopy(source)
        self.timings(source)
        self.assertEqual(source, original)

    def test_empty_plan_or_empty_episode_is_not_success(self):
        for episodes in ([], [{"number": 1, "shots": []}]):
            with self.subTest(episodes=episodes), self.assertRaises(ValidationError):
                self.timings(StoryboardSourcePlan(basis={}, provenance=[], episodes=episodes))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualStoryboardMaterializationPlanTests(TestCase):
    create = manual_fixtures.ProductionManualBibleTests.create
    post = manual_fixtures.ProductionManualBibleTests.post
    read = manual_fixtures.ProductionManualBibleTests.read
    adopt_source = manual_fixtures.ProductionManualBibleTests.adopt_source
    approval = manual_fixtures.ProductionManualBibleTests.approval

    def setUp(self):
        original = manual_fixtures.confirmed_bible_source
        def source(testcase):
            return original(testcase, episodes=[{"episode": 4, "title": "独立分集", "shots": [
                {"shot": 8, "duration_ms": 5000, "scene_key": "room", "character_keys": [],
                    "dialogue": {"speaker_entity_key": "traveler", "text": " 原文\nBonjour。 ",
                        "is_voiceover": True, "start_ms": 0, "end_ms": 1500}},
                {"shot": 2, "episode_start_ms": 0, "episode_end_ms": 6000,
                    "references": [{"use_key": "look", "entity_key": "traveler", "variant_key": "raincoat",
                        "media_kind": "image", "purpose": "appearance"}]}, {"shot": 9}]}])
        with patch("studio.tests.test_production_manual_bible.confirmed_bible_source", side_effect=source):
            manual_fixtures.ProductionManualBibleTests.setUp(self)
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        self.stage = self.run.stages.get(stage_key="storyboard")
        self.no_network = patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def proposed(self, **changes):
        return read_storyboard_materialization_plan(**{"run": self.run, "stage": self.stage, **changes})

    def facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            ProductionRun, StageRun, ApprovalDecision, ProductionManualCommand, Episode, Shot,
            ShotCharacterRelation, ShotReferenceUse, ExecutionPreparation, GenerationAttempt, GeneratedArtifact, Asset)}

    def test_exact_m2_m3_contract_reads_only_and_copies_no_script_body_or_timing_list(self):
        before = self.facts()
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("No reparsing")), CaptureQueriesContext(connection) as queries:
            plan = self.proposed()
            repeated = self.proposed()
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual(plan.contract, repeated.contract)
        self.assertEqual(set(plan.contract.value), {"schema_version", "source_basis", "run_config_sha256", "parameters"})
        self.assertEqual(plan.contract.schema_version, 3)
        self.assertEqual(plan.contract.value["source_basis"], plan.source.basis)
        self.assertEqual(plan.source.basis["script"]["id"], str(self.revision.pk))
        self.assertEqual(plan.contract.value["run_config_sha256"], self.run.config_snapshot_sha256)
        self.assertEqual(list(plan.timings), [(4, 8), (4, 2), (4, 9)])
        self.assertEqual(plan.timings[4, 2]["duration_ms"], 6000)
        self.assertEqual(plan.timings[4, 9]["duration_ms"], self.run.config_snapshot["values"]["production.default_shot_duration_ms"])
        self.assertNotIn("Bonjour", str(plan.contract.value))
        self.assertEqual(self.facts(), before)
        plan.source.episodes[0]["shots"][0]["body"]["dialogue"]["text"] = "changed by reader"
        plan.contract.value["parameters"]["include_audio"] = None
        self.assertEqual(self.proposed().contract, repeated.contract)

    def test_newer_manuscript_and_mutated_caller_objects_cannot_replace_frozen_source(self):
        expected = self.proposed().contract
        self.project.master_script = "Newer manuscript is not this Run's source"
        self.project.save(update_fields=("master_script",))
        self.run.config_snapshot = {"schema_version": 1, "values": {}}
        self.run.project_id = uuid4()
        self.assertEqual(self.proposed().contract, expected)

    def test_config_checksum_corruption_has_no_default_fallback_or_write(self):
        ProductionRun.objects.filter(pk=self.run.pk).update(config_snapshot_sha256="0" * 64)
        before = self.facts()
        with self.assertRaises(ValidationError): self.proposed()
        self.assertEqual(self.facts(), before)

    def test_explicit_config_drift_changes_contract_but_not_source_and_obsolete_keys_are_unused(self):
        original = self.proposed()
        self.run.refresh_from_db()
        snapshot = deepcopy(self.run.config_snapshot)
        snapshot["values"]["production.default_include_audio"] = not original.contract.value["parameters"]["include_audio"]
        for key in ("production.script_auto_split_trigger_chars", "production.script_auto_split_target_chars",
                "production.script_prop_alias_policy"):
            snapshot["values"].pop(key, None)
        # Explicit isolated frozen-config fixture mutation, not a runtime reconfiguration API.
        assign_json_contract(self.run, "config_snapshot", snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        before = self.facts()
        current = self.proposed()
        self.assertEqual(current.source, original.source)
        self.assertNotEqual(current.contract.sha256, original.contract.sha256)
        self.assertFalse({"prop_alias_policy", "episode_count_hint", "auto_split_trigger_chars", "auto_split_target_chars"}
            & set(current.contract.value["parameters"]))
        self.assertEqual(self.facts(), before)

    def test_missing_or_non_boolean_policy_fails_without_current_default(self):
        values = deepcopy(self.run.config_snapshot["values"])
        for invalid in (None, 0, "false"):
            values["production.default_include_audio"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                declared_storyboard_parameters(values=values)
        values.pop("production.default_include_audio")
        with self.assertRaises(ValidationError): declared_storyboard_parameters(values=values)

    def test_unreviewed_new_setting_fails_instead_of_reusing_stale_basis(self):
        self.proposed()
        variant = BibleVariant.objects.get(source_variant_key="raincoat")
        revision = variant.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(revision), "appearance_anchor": "改变了设定"})
        before = self.facts()
        with self.assertRaises(ValidationError): self.proposed()
        self.assertEqual(self.facts(), before)

    def test_wrong_stage_or_foreign_identity_cannot_use_this_contract(self):
        for stage in (self.run.stages.get(stage_key="bible_review"), StageRun(pk=uuid4())):
            with self.subTest(stage=stage.pk), self.assertRaises(ValidationError): self.proposed(stage=stage)
        StageRun.objects.filter(pk=self.stage.pk).update(mode="deterministic")
        with self.assertRaises(ValidationError): self.proposed()


class RecipeStoryboardMaterializationPlanTests(TestCase):
    root = recipe_fixtures.BiblePolicyContinuationTests.root
    recipe = recipe_fixtures.BiblePolicyContinuationTests.recipe
    build = recipe_fixtures.BiblePolicyContinuationTests.build

    def setUp(self):
        recipe_fixtures.BiblePolicyContinuationTests.setUp(self)

    def test_original_auto_policy_source_uses_common_plan_without_human_or_storyboard_facts(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls")):
            run = advance_production_run(self.build(), automatic=True)
            stage = run.stages.get(stage_key="storyboard")
            before = list(ApprovalDecision.objects.values())
            with CaptureQueriesContext(connection) as queries:
                plan = read_storyboard_materialization_plan(run=run, stage=stage)
            self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual(list(ApprovalDecision.objects.values()), before)
        self.assertIsNone(plan.source.basis["script_decision_id"])
        self.assertEqual(ApprovalDecision.objects.get().actor, "recipe_policy")
        self.assertEqual(plan.contract.value["source_basis"], plan.source.basis)
        self.assertEqual(plan.timings[1, 1]["duration_ms"], run.config_snapshot["values"]["production.default_shot_duration_ms"])
        self.assertFalse(Episode.objects.exists())
        self.assertFalse(Shot.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        stage.refresh_from_db()
        self.assertEqual((stage.status, stage.reason_code), ("blocked", "isolated_next_stage"))

    def test_uncompleted_original_script_stage_does_not_fall_back_to_run_revision(self):
        run = self.build()
        with self.assertRaises(ValidationError):
            read_storyboard_materialization_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
        self.assertFalse(Episode.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())
