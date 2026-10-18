"""Real non-model stage freezing; no recipe publication or model transport."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ConfigRevision, GenerationAttempt, ExecutionPreparation, StageRun
from studio.services.configuration import create_config_revision, publish_config_revision, resolve_configuration
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot
from studio.services.security_boundaries import boundary_int
from studio.tests import test_production_run_plans as fixtures


class RecipeStageSnapshotTests(TestCase):
    root = fixtures.ProductionRunPlanTests.root
    recipe = fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        fixtures.ProductionRunPlanTests.setUp(self)
        recipe = self.recipe()
        self.run = self.root(plan_source="recipe", recipe_version=recipe)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(project=self.project).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        freeze_run_execution_plan(run=self.run, **self.request)
        self.source = StageRun.objects.create(production_run=self.run, stage_key="script_input", mode="manual",
            ordinal=1, status="succeeded", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="storyboard", mode="manual",
            ordinal=2, status="running", idempotency_key=str(uuid4()))
        _save_stage_snapshot(self.source, "output_snapshot", {"schema_version": 1, "kind": "isolated-source"})

    def freeze(self):
        _freeze_stage_config(self.run, self.stage)
        _freeze_stage_input(self.run, self.stage)

    def test_freeze_uses_run_plan_without_old_routing_or_resource_lookup(self):
        self.freeze()
        self.assertEqual(self.stage.config_snapshot["schema_version"], 2)
        self.assertEqual(self.stage.config_snapshot["base"], self.run.config_snapshot)
        self.assertEqual(self.stage.input_snapshot["upstream"], [{"stage_id": str(self.source.pk),
            "stage_key": "script_input", "status": "succeeded", "output_snapshot_sha256": self.source.output_snapshot_sha256}])
        self.assertNotIn("routes", self.stage.config_snapshot)
        self.assertNotIn("workflow_selection", self.stage.input_snapshot)
        previous = (self.stage.config_snapshot_sha256, self.stage.input_snapshot_sha256)
        revision = create_config_revision(scope_type=ConfigRevision.ScopeType.PROJECT, project=self.project,
            values={"production.default_include_audio": not self.run.config_snapshot["values"]["production.default_include_audio"]},
            created_by=self.actor, clone_published=False)
        publish_config_revision(revision, published_by=self.actor)
        self.freeze()
        self.assertEqual(previous, (self.stage.config_snapshot_sha256, self.stage.input_snapshot_sha256))
        self.assertEqual(self.stage.config_snapshot["base"]["values"], self.run.config_snapshot["values"])
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_changed_upstream_cannot_be_refrozen_and_pending_or_missing_predecessor_fails(self):
        self.freeze()
        original = self.stage.input_snapshot_sha256
        _save_stage_snapshot(self.source, "output_snapshot", {"schema_version": 1, "kind": "changed"})
        with self.assertRaises(ValidationError):
            self.freeze()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot_sha256, original)
        self.source.status = "pending"
        self.source.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            _freeze_stage_input(self.run, self.stage)
        self.source.delete()
        with self.assertRaises(ValidationError):
            _freeze_stage_input(self.run, self.stage)

    def test_corrupt_upstream_config_and_stage_declaration_reject(self):
        _freeze_stage_config(self.run, self.stage)
        self.source.output_snapshot = {"schema_version": 1, "kind": "unsigned-change"}
        self.source.save(update_fields=("output_snapshot",))
        with self.assertRaises(ValidationError):
            _freeze_stage_input(self.run, self.stage)
        frozen = deepcopy(self.stage.config_snapshot)
        self.stage.config_snapshot["base"]["values"]["production.default_include_audio"] = "corrupt"
        with self.assertRaises(ValidationError):
            _freeze_stage_config(self.run, self.stage)
        self.stage.config_snapshot = frozen
        self.stage.mode = "deterministic"
        with self.assertRaises(ValidationError):
            _freeze_stage_config(self.run, self.stage)

    def test_recipe_changes_do_not_replace_the_frozen_non_model_declaration(self):
        self.freeze()
        before = deepcopy(self.stage.config_snapshot)
        type(self.run.recipe_version).objects.filter(pk=self.run.recipe_version_id).update(status="retired")
        self.freeze()
        self.assertEqual(self.stage.config_snapshot, before)
