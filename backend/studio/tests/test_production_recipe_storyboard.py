"""Original recipe snapshots, dispatcher, scheduler and HTTP continue -> M5 writer.

Isolated M2 import/M3 policy receipt; the fixture stops before storyboard until
explicitly released here. No real model, complete-recipe publication or delivery
acceptance is claimed.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, Episode, ExecutionPreparation, GenerationAttempt,
    ProductionRun, ScriptRevision, Shot, ShotCharacterRelation, ShotReferenceUse, ShotScriptSource, StageRun)
from studio.services.production_recipe_stage_snapshots import recipe_non_model_input_contract
from studio.services.production_runs import (advance_production_run, _assert_frozen_formal_objects,
    _execute_storyboard, _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot, StageBlocked)
from studio.services.production_storyboard_content import storyboard_content
from studio.services.script_storyboard_fields import read_shot_source_fields
from studio.tests import test_bible_policy_continuation as fixtures
from studio.tests.test_storyboard_source_fields import saved_source


class RecipeStoryboardTests(TestCase):
    root = fixtures.BiblePolicyContinuationTests.root
    recipe = fixtures.BiblePolicyContinuationTests.recipe
    build = fixtures.BiblePolicyContinuationTests.build

    def setUp(self):
        with patch("studio.tests.test_bible_policy_continuation.confirmed_structured_bible_source",
                side_effect=lambda testcase, **_: saved_source(testcase)):
            fixtures.BiblePolicyContinuationTests.setUp(self)
        self.run = advance_production_run(self.build(), automatic=True)
        self.stage = self.run.stages.get(stage_key="storyboard")
        self.assertEqual(self.stage.reason_code, "isolated_next_stage")
        self.prior_decisions = list(ApprovalDecision.objects.values())
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/production-runs/{self.run.pk}/continue/"
        # Remove only the deliberately installed isolated stop. This is not a
        # production retry bypass or a fabricated succeeded upstream.
        self.stage.status, self.stage.reason_code, self.stage.reason = "pending", "", ""
        self.stage.save(update_fields=("status", "reason_code", "reason"))
        ProductionRun.objects.filter(pk=self.run.pk).update(status="queued", blocked_code="", blocked_reason="")
        self.run.refresh_from_db()
        for target in ("requests.sessions.Session.request",
                "studio.services.script_revision_structures.parse_script_semantic_output"):
            guard = patch(target, side_effect=AssertionError("No network, old writer or reparsing"))
            guard.start()
            self.addCleanup(guard.stop)

    def freeze(self):
        _freeze_stage_config(self.run, self.stage)
        _freeze_stage_input(self.run, self.stage)

    def facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            Episode, Shot, ShotScriptSource, ShotCharacterRelation, ShotReferenceUse)}

    def empty_targets(self):
        self.assertTrue(all(not value for value in self.facts().values()))

    def complete_storyboard(self):
        result = self.client.post(self.url, {}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        self.assertEqual(self.stage.output_reference, "episodes:2")
        # There is deliberately no delivery stage in this boundary fixture.
        self.run.refresh_from_db()
        self.assertNotEqual(self.run.status, "succeeded")
        return result

    def test_frozen_input_is_exact_shared_source_not_canonical_text_or_split_parameters(self):
        self.freeze()
        with CaptureQueriesContext(connection) as queries:
            current = recipe_non_model_input_contract(self.stage)
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual(current.value, self.stage.input_snapshot)
        materialization = current.value["materialization"]
        self.assertEqual(set(materialization), {"contract", "sha256"})
        contract = materialization["contract"]
        self.assertEqual(contract["schema_version"], 3)
        self.assertEqual(contract["source_basis"]["script"]["id"], str(self.revision.pk))
        self.assertEqual(contract["source_basis"]["bible_review"]["decision_id"], str(ApprovalDecision.objects.get().pk))
        self.assertTrue({"auto_split_trigger_chars", "auto_split_target_chars", "prop_alias_policy",
            "episode_count_hint", "default_conditioning_mode"}.isdisjoint(contract["parameters"]))
        self.assertNotIn("tone", str(materialization))
        self.empty_targets()

    def test_original_continue_api_writes_exact_source_relations_and_original_stage_receipt(self):
        self.project.master_script = "用户后来编辑但未采用的母稿"
        self.project.save(update_fields=("master_script",))
        response = self.complete_storyboard()
        self.assertEqual((Episode.objects.count(), Shot.objects.count(), ShotScriptSource.objects.count()), (2, 4, 4))
        self.assertEqual(list(Shot.objects.values_list("duration_ms", flat=True)), [5000] * 4)
        self.assertFalse(Shot.objects.exclude(conditioning_mode="").exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "用户后来编辑但未采用的母稿")
        snapshot = self.stage.output_snapshot
        public_stage = next(row for row in response.data["stages"] if str(row["id"]) == str(self.stage.pk))
        self.assertEqual(public_stage["output_snapshot"], snapshot)
        self.assertEqual(snapshot["formal_object_type"], "storyboard")
        rows = [shot for episode in snapshot["canonical"]["episodes"] for shot in episode["shots"]]
        for row in rows:
            source = row["script_source"]
            self.assertEqual(source["structure_id"], str(self.revision.pk))
            self.assertEqual(source["stage_id"], str(self.stage.pk))
            self.assertEqual(source["materialization_sha256"], self.stage.input_snapshot["materialization"]["sha256"])
            self.assertEqual([item["role"] for item in row["character_relations"]], ["speaker"])
            self.assertNotIn("character_ids", row)  # The old mixed pool is not a current fact.
            self.assertEqual(row["reference_uses"][0]["use_key"], "appearance")
            self.assertEqual(row["reference_uses"][0]["source_mode"], "")
            self.assertIsNone(row["reference_uses"][0]["asset_id"])
            fields = read_shot_source_fields(shot_id=row["id"], project_id=self.project.pk, max_bytes=4 * 1024 * 1024)["fields"]
            self.assertEqual(next(item["value"] for item in fields if item["value_path"][-1] == "tone"), "紧张")
        self.assertEqual(list(ApprovalDecision.objects.values()), self.prior_decisions)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertEqual(ApprovalDecision.objects.get().actor, "recipe_policy")
        self.assertIsNone(ApprovalDecision.objects.get().decided_by_id)

    def test_exact_dispatch_replay_is_zero_write_and_completed_http_continue_does_not_repeat(self):
        self.complete_storyboard()
        before = self.facts()
        stage_before = deepcopy(self.stage.output_snapshot)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(_execute_storyboard(self.run, self.stage), "episodes:2")
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 200)
        self.assertEqual(self.facts(), before)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.output_snapshot, stage_before)
        self.assertEqual(list(ApprovalDecision.objects.values()), self.prior_decisions)

    def test_rehashed_input_cannot_replace_frozen_parameters(self):
        self.freeze()
        value = deepcopy(self.stage.input_snapshot)
        value["materialization"]["contract"]["parameters"]["default_shot_duration_ms"] += 1
        _save_stage_snapshot(self.stage, "input_snapshot", value)
        with self.assertRaises(StageBlocked): _execute_storyboard(self.run, self.stage)
        self.empty_targets()

    def test_changed_confirmed_source_or_corrupt_original_cannot_create_shots(self):
        self.freeze()
        # Existing saved source must remain exact even when a caller changes a
        # persisted Run pointer; this is isolated corruption, not a user action.
        ProductionRun.objects.filter(pk=self.run.pk).update(script_revision=None)
        with self.assertRaises(StageBlocked): _execute_storyboard(self.run, self.stage)
        ProductionRun.objects.filter(pk=self.run.pk).update(script_revision=self.revision)
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content="broken digest")
        with self.assertRaises(StageBlocked): _execute_storyboard(self.run, self.stage)
        self.empty_targets()

    def test_missing_bible_approval_or_cross_run_stage_is_not_a_fallback_to_old_writer(self):
        self.freeze()
        with self.assertRaises(StageBlocked): _execute_storyboard(self.root(), self.stage)
        StageRun.objects.filter(production_run=self.run, stage_key="bible_review").update(status="pending")
        with self.assertRaises(StageBlocked): _execute_storyboard(self.run, self.stage)
        self.empty_targets()

    def test_existing_unbound_project_content_is_preserved_and_stage_does_not_succeed(self):
        existing = Episode.objects.create(project=self.project, number=17, title="Existing independent work")
        result = self.client.post(self.url, {}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "blocked")
        self.assertEqual(list(Episode.objects.values_list("pk", flat=True)), [existing.pk])
        self.assertFalse(Shot.objects.exists())
        self.assertFalse(self.stage.output_snapshot)
        self.assertEqual(list(ApprovalDecision.objects.values()), self.prior_decisions)

    def test_binding_failure_rolls_back_all_targets_without_losing_original_upstream(self):
        with patch("studio.services.production_storyboard_materialization.ShotScriptSource.objects.bulk_create",
                side_effect=ValidationError("isolated binding storage failure")):
            result = self.client.post(self.url, {}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "blocked")
        self.empty_targets()
        self.assertFalse(self.stage.output_snapshot)
        self.assertEqual(list(ApprovalDecision.objects.values()), self.prior_decisions)

    def test_output_receipt_failure_rolls_back_targets_and_stage_then_original_continue_can_succeed(self):
        original = StageRun.save
        def fail_output(instance, *args, **kwargs):
            if instance.pk == self.stage.pk and "output_snapshot" in kwargs.get("update_fields", ()):
                raise ValidationError("isolated original output receipt failure")
            return original(instance, *args, **kwargs)
        with patch.object(StageRun, "save", fail_output), self.assertRaises(ValidationError):
            advance_production_run(self.run)
        self.empty_targets()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "pending")
        self.assertFalse(self.stage.output_snapshot)
        self.assertFalse(self.stage.input_snapshot)
        self.assertEqual(list(ApprovalDecision.objects.values()), self.prior_decisions)
        self.complete_storyboard()

    def test_new_source_and_typed_relation_drift_is_detected_by_original_receipt(self):
        self.complete_storyboard()
        _assert_frozen_formal_objects(self.run)
        use = ShotReferenceUse.objects.first()
        ShotReferenceUse.objects.filter(pk=use.pk).update(purpose="later changed purpose")
        with self.assertRaises(StageBlocked) as error: _assert_frozen_formal_objects(self.run)
        self.assertEqual(error.exception.code, "storyboard_snapshot_drift")
        ShotReferenceUse.objects.filter(pk=use.pk).update(purpose=use.purpose)
        source = ShotScriptSource.objects.first()
        ShotScriptSource.objects.filter(pk=source.pk).update(document_sha256="0" * 64)
        with self.assertRaises(StageBlocked): _assert_frozen_formal_objects(self.run)
        ShotScriptSource.objects.filter(pk=source.pk).update(document_sha256=source.document_sha256)
        relation = ShotCharacterRelation.objects.first()
        ShotCharacterRelation.objects.filter(pk=relation.pk).update(role="visible")
        with self.assertRaises(StageBlocked) as error: _assert_frozen_formal_objects(self.run)
        self.assertEqual(error.exception.code, "storyboard_snapshot_drift")
        self.assertIn("发言者关系不完整", str(error.exception))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded")  # Reading never rewrites history.

    def test_receipt_projection_batches_narrow_indexed_rows_and_excludes_downstream_media_from_content(self):
        self.complete_storyboard()
        episodes = list(Episode.objects.filter(project=self.project))
        with CaptureQueriesContext(connection) as queries:
            content = storyboard_content(project_id=self.project.pk, episodes=episodes, include_references=False)
        tables = ("studio_shotscriptsource", "studio_shotcharacterrelation", "studio_shotreferenceuse")
        narrow = [query["sql"] for query in queries if any('FROM "' + table + '"' in query["sql"] for table in tables)]
        self.assertEqual(len(narrow), 3)  # Not per-shot queries.
        for sql in narrow:
            self.assertNotIn("SELECT *", sql.upper())
            self.assertNotIn('"document"', sql)
            self.assertNotIn('"studio_scriptrevision"', sql)
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                self.assertIn("SEARCH", plan)
                self.assertNotIn("SCAN STUDIO_", plan)
        for episode in content["episodes"]:
            for shot in episode["shots"]:
                self.assertNotIn("reference_asset_ids", shot)
                self.assertTrue({"source_mode", "reference_version_id", "asset_id", "asset_sha256"}
                    .isdisjoint(shot["reference_uses"][0]))
                self.assertIn("setting_revision_id", shot["reference_uses"][0])

    def test_other_owner_cannot_continue_and_anonymous_cannot_read_or_write(self):
        before = self.facts()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(self.url, {}, format="json").status_code, (401, 403))
        self.assertEqual(self.facts(), before)
