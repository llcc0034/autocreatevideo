"""Explicit recipe storage fixtures and original Bible stage/review writers.

Not recipe publication, scheduler or provider acceptance. No business DB/model.
"""
from copy import deepcopy
from uuid import uuid4

from django.test import TestCase

from studio.models import BibleVariant, GeneratedArtifact, GenerationAttempt, ScriptBibleEntityBinding, StageRun
from studio.services.bible_content_review import approve_bible_content, recipe_bible_review_detail, recipe_bible_review_source
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _execute_bible_materialize, _script_artifact, _save_stage_snapshot, _formal_snapshot, StageBlocked
from studio.tests import test_production_run_plans as plans
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source


class StructuredBibleRecipeTests(TestCase):
    root = plans.ProductionRunPlanTests.root
    recipe = plans.ProductionRunPlanTests.recipe

    def setUp(self):
        plans.ProductionRunPlanTests.setUp(self)
        self.revision = confirmed_structured_bible_source(self, layout="b")
        recipe = self.recipe(definition={"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "required"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"}]})
        self.run = self.root(plan_source="recipe", recipe_version=recipe, script_revision=self.revision)
        freeze_run_execution_plan(run=self.run, **{**self.request, "initial_content": {
            "kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256}})
        self.input = StageRun.objects.create(production_run=self.run, stage_key="script_input", ordinal=1,
            mode="confirmed_revision", status="succeeded", idempotency_key=str(uuid4()))
        self.source = StageRun.objects.create(production_run=self.run, stage_key="bible_materialize", ordinal=2,
            mode="deterministic", status="running", idempotency_key=str(uuid4()))
        self.review = StageRun.objects.create(production_run=self.run, stage_key="bible_review", ordinal=3,
            mode="review", status="awaiting_approval", idempotency_key=str(uuid4()))

    def freeze(self):
        reference = _execute_bible_materialize(self.run, self.source)
        _save_stage_snapshot(self.source, "output_snapshot", _formal_snapshot(self.run, self.source, reference))
        self.source.status = "succeeded"
        self.source.save(update_fields=("status",))

    def test_import_receipt_to_bible_source_review_and_repeat_do_not_duplicate_objects(self):
        artifact = _script_artifact(self.run, self.input, create=True)
        self.freeze()
        first = recipe_bible_review_detail(self.run)
        self.assertTrue(all("identity_anchor" in row and "visual_identity" not in row for row in first["candidates"]))
        frozen, source_sha = recipe_bible_review_source(self.run)
        self.assertEqual(frozen["structure_source"], artifact.manifest_metadata["structure_source"])
        self.assertEqual(first["source_snapshot_sha256"], source_sha)
        for index in range(2):
            detail = recipe_bible_review_detail(self.run)
            result = approve_bible_content(run=self.run, actor=self.actor, frozen=frozen,
                expected_project_bible_sha256=detail["project_bible_sha256"],
                reviewed_candidate_ids=[row["id"] for row in detail["candidates"]],
                reviewed_setting_keys=[row["review_key"] for row in detail["settings"]], reviewed_project_bible=True, comment="隔离核对")
            if index == 0:
                initial = result["materialization"]
            else:
                self.assertEqual(result["materialization"], initial)
        self.assertEqual(ScriptBibleEntityBinding.objects.count(), 2)
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        canonical = _formal_snapshot(self.run, self.review, None)["canonical"]
        for entities in canonical.values():
            for entity in entities:
                self.assertNotIn("visual_identity", entity)
                self.assertIn("appearance_settings", entity)

    def test_missing_or_changed_upstream_receipt_is_not_created_or_repaired(self):
        with self.assertRaises(StageBlocked):
            self.freeze()
        self.assertFalse(GeneratedArtifact.objects.exists())
        artifact = _script_artifact(self.run, self.input, create=True)
        metadata = deepcopy(artifact.manifest_metadata)
        metadata["structure_source"]["document_sha256"] = "0" * 64
        assign_json_contract(artifact, "manifest_metadata", metadata, schema_version=2, schema_version_field_name="manifest_schema_version")
        artifact.save()
        with self.assertRaises(StageBlocked):
            self.freeze()
        artifact.refresh_from_db()
        self.assertEqual(artifact.manifest_metadata, metadata)
        self.source.refresh_from_db()
        self.assertEqual(self.source.output_snapshot, {})
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_unfinished_upstream_does_not_mean_adoption(self):
        _script_artifact(self.run, self.input, create=True)
        self.input.status = "running"
        self.input.save(update_fields=("status",))
        with self.assertRaisesMessage(StageBlocked, "已完成"):
            self.freeze()
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())
