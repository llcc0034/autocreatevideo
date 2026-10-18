"""Recipe structure -> real reference Profile -> original persisted preparation."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, GenerationAttempt, ProductionRecipeVersion, ProductionRun, Shot, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests import test_execution_reference_preparation as reference_fixture
from studio.tests import test_production_reviewed_references as reviewed_fixture


def literal(value):
    return {"kind": "literal", "value": value}


def field(key):
    return {"kind": "context_field", "selector_key": key, "scope": {"kind": "shot"}}


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class RecipeStructurePreparationTests(TestCase):
    write = reference_fixture.ExecutionReferencePreparationTests.write
    approve = reviewed_fixture.ReviewedReferenceTests.approve

    @staticmethod
    def input(*args, **kwargs):
        row = reference_fixture.ExecutionReferencePreparationTests.input(*args, **kwargs)
        row["semantic_contract"]["allowed_scope_kinds"] = ["request", "project", "shot"]
        return row

    def setUp(self):
        reference_fixture.ExecutionReferencePreparationTests.setUp(self)
        reviewed_fixture.ReviewedReferenceTests.setUp(self)
        self.shot.media_type = "video"
        self.shot.save(update_fields=("media_type",))
        self.second = Shot.objects.create(episode=self.episode, number=2, duration_ms=1000, media_type="video", include_audio=False)
        unit = deepcopy(self.sources["units"]["value"]["ordered_units"][0])
        unit_fields = {key: literal(value) for key, value in unit.items()}
        unit_fields.update(unit_key={"kind": "unit_key"}, duration_ms=field("shot.duration_ms"),
            subject_keys={"kind": "reference_subjects"})
        # This fixture deliberately configures literal renderer text. The real
        # creative-field/revision path is covered by ShotFieldMappingTests.
        self.explicit_description = unit["structured_input"]["direction"]
        root_fields = {key: literal(value) for key, value in self.sources["units"]["value"].items() if key != "ordered_units"}
        root_fields["ordered_units"] = {"kind": "array", "items": [{"kind": "object", "fields": unit_fields}]}
        self.rules = {"units": {"kind": "structured", "template": {"kind": "object", "fields": root_fields}},
            "styles": {"kind": "structured", "template": {"kind": "unit_map", "value": literal({"palette": "blue"})}}}
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": self.rules, "parameter_policies": [{"key": key, "policy": "locked_value", "value": value}
                for key, value in {**self.parameters, "video.frames": 24}.items()]}
        self.create_route(route)

    def create_route(self, route):
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Structured reference fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
                "mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": None}}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="b"*64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(), max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1}); self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "episodes", "episode_ids": [str(self.episode.pk)]},
            delivery={"kind": "episode_masters"}, initial_content=None, stage_catalog_key=catalog.value["key"],
            expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", mode="review", status="succeeded",
            ordinal=1, idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline", status="running",
            ordinal=2, idempotency_key=str(uuid4()))
        self.approve()

    def prepare(self, shot, candidate=1):
        return prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope={"kind": "shot", "id": str(shot.pk)}, candidate_number=candidate, actor=self.actor)

    def test_duration_source_freezes_each_reviewed_shot_and_replay_does_not_recalculate(self):
        route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][-1]["routing"]["routes"][0])
        route["parameter_policies"] = [row for row in route["parameter_policies"] if row["key"] != "video.frames"] + [{
            "key": "video.frames", "policy": "locked_source", "source": {
                "kind": "workflow_frames", "selector_key": "shot.duration_ms", "scope": {"kind": "shot"}}}]
        self.shot.duration_ms = 2000; self.shot.save(update_fields=("duration_ms",))
        self.second.duration_ms = 3000; self.second.save(update_fields=("duration_ms",))
        third = Shot.objects.create(episode=self.episode, number=3, duration_ms=5000, media_type="video", include_audio=False)
        self.create_route(route)
        preparations = []
        for shot, frames in ((self.shot, 48), (self.second, 72), (third, 120)):
            preparation = self.prepare(shot); preparations.append(preparation)
            parameters = preparation.contract.resolved_inputs["parameters"]
            self.assertEqual(parameters["values"]["video.frames"], frames)
            source = parameters["resolved_source_contracts"]["video.frames"]
            self.assertEqual(source["duration_ms"], shot.duration_ms)
            self.assertEqual(source["scope"]["id"], str(shot.pk))
            self.assertEqual(source["definition_sha256"], self.definition.sha256)
            self.assertEqual(source["review"]["decision_id"], str(self.review.approval_decisions.get().pk))
            self.assertEqual(source["fps"]["value"], 24)
            self.assertEqual(source["frame_policy"]["alignment_modulus"], 1)
        self.shot.duration_ms = 5000; self.shot.save(update_fields=("duration_ms",))
        from unittest.mock import patch
        with patch("studio.services.production_recipe_media_preparations.bind_recipe_parameter_sources", side_effect=AssertionError("Replay cannot rebind")):
            self.assertEqual(self.prepare(self.shot).pk, preparations[0].pk)
        with self.assertRaisesMessage(ValidationError, "已变更"):
            self.prepare(self.shot, candidate=2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_reviewed_units_and_explicit_text_enter_reference_requests_and_replay_keeps_sources(self):
        preparations = [self.prepare(shot) for shot in (self.shot, self.second)]
        for shot, preparation in zip((self.shot, self.second), preparations):
            facts = preparation.contract.resolved_inputs
            unit_key = f"shot:{shot.pk}"
            context = facts["profile_sources"]["reference_context"]["value"]["context"]
            self.assertEqual(context["ordered_units"][0]["unit_key"], unit_key)
            original = facts["profile_sources"]["projection"]["reference"]["unit_input"]
            self.assertEqual(original["ordered_units"][0]["structured_input"]["direction"], self.explicit_description)
            self.assertEqual(facts["inputs"]["logical_values"]["styles"], {unit_key: {"palette": "blue"}})
            self.assertIn(self.explicit_description, str(facts["field_requests"]))
            self.assertIn(unit_key, str(facts["field_requests"]))
            self.assertEqual(facts["inputs"]["input_provenance"][0]["kind"], "structured")
        self.shot.duration_ms += 1; self.shot.save(update_fields=("duration_ms",))
        self.assertEqual(self.prepare(self.shot).pk, preparations[0].pk)
        with self.assertRaisesMessage(ValidationError, "已变更"): self.prepare(self.shot, candidate=2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_original_mapping_preview_accepts_the_same_structured_sources_without_creating_preparation(self):
        from studio.models import ExecutionPreparation
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/", {
            "stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk), "candidate_number": 1,
            "expected_run_plan_sha256": self.plan.sha256}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "inputs_validated")
        units = next(row for row in response.data["value_inputs"] if row["key"] == "units")
        self.assertEqual(units["value"]["ordered_units"][0]["unit_key"], f"shot:{self.shot.pk}")
        self.assertEqual(units["provenance"]["kind"], "structured")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        self.assertFalse(GenerationAttempt.objects.exists())
