"""Selection routing reads the frozen plan, not retired Stage.routing JSON.

These are isolated row/route assertions, not media-byte or model acceptance.
Actual candidate readers and adoption have separate integration coverage.
"""
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ArtifactSelectionDecision, GeneratedArtifact, StageRun
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _assert_stage_output_integrity, _execute_stage, StageBlocked
from studio.tests import test_production_run_plans as fixtures


class RecipeSelectionRouteTests(TestCase):
    root = fixtures.ProductionRunPlanTests.root
    recipe = fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        fixtures.ProductionRunPlanTests.setUp(self)
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
                "mode": "fixed", "variant_count": 2, "evaluator": None, "routes": [{
                    "execution_instance_version_id": str(uuid4()), "expected_definition_sha256": "a" * 64,
                    "input_sources": {}, "parameter_policies": []}]}},
            {"key": "artifact_selection", "mode": "review", "approval": "required"}]}
        self.run = self.root(plan_source="recipe", recipe_version=self.recipe(definition=definition))
        freeze_run_execution_plan(run=self.run, **self.request)
        self.media = StageRun.objects.create(production_run=self.run, stage_key="media_generation",
            mode="existing_pipeline", ordinal=1, status="succeeded", idempotency_key=str(uuid4()))
        self.selection = StageRun.objects.create(production_run=self.run, stage_key="artifact_selection",
            mode="review", ordinal=2, status="awaiting_approval", idempotency_key=str(uuid4()))
        self.artifact = GeneratedArtifact.objects.create(production_run=self.run, stage_run=self.media,
            manifest_key=str(uuid4()), variant_group="isolated-group", kind="video", role="media",
            status="selected", manifest_metadata_sha256="a" * 64)

    def test_frozen_variants_require_selection_even_when_old_routing_claims_one(self):
        self.media.routing = {"schema_version": 1, "variant_count": 1, "evaluator": "first_succeeded"}
        self.media.save(update_fields=("routing",))
        with self.assertRaisesMessage(ValidationError, "显式选片"):
            _assert_stage_output_integrity(self.run, self.selection)
        ArtifactSelectionDecision.objects.create(stage_run=self.media, variant_group=self.artifact.variant_group,
            revision=1, artifact=self.artifact, selected_by=self.actor)
        _assert_stage_output_integrity(self.run, self.selection)
        self.assertEqual(self.media.route_events.count(), 0)

    def test_stage_identity_cannot_bypass_the_frozen_route(self):
        self.media.ordinal = 3
        self.media.save(update_fields=("ordinal",))
        with self.assertRaises(ValidationError):
            _assert_stage_output_integrity(self.run, self.selection)

    def test_unregistered_ai_stage_does_not_read_or_write_legacy_routes(self):
        self.selection.mode = "ai"
        with self.assertRaises(StageBlocked) as caught:
            _execute_stage(self.run, self.selection)
        self.assertEqual(caught.exception.code, "driver_contract_missing")
        self.assertFalse(self.selection.route_events.exists())
