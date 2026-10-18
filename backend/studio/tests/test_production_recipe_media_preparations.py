from studio.tests.field_definition_fixtures import create_field_definition
"""Real exact-instance Recipe -> original preparation storage, without calls."""
from uuid import uuid4
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (Episode, ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion,
    ProductionRun, PromptProfileVersion, SeriesProject, Shot, StageRun)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.configuration import resolve_configuration
from studio.services.security_boundaries import boundary_int
from studio.tests import test_execution_definitions as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMediaPreparationTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)

    def setUp(self):
        fixtures.ExecutionDefinitionTests.setUp(self)
        self.write_contract(self.model, "specification", {**self.model.specification, "model_family": "fixture-family"})
        profile = PromptProfileVersion.objects.create(profile_key="recipe-media-profile", version=1, name="Fixture strategy",
            model_family="fixture-family", operation="video.generate", mode="T2VA", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="media-fixture", label="Instruction", value_type="long_text")
        create_profile_field_rule(fixed_value="A red cube turns.", profile_version=profile, field_definition=field, source_selector="fixed.value",
            generation_source_selector="fixed.value", generation_mode="server_locked", channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile); profile = publish_prompt_profile(profile, actor=self.actor)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"prompt_mode": "profile", "prompt_profile_version": profile})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Recipe media", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=self.project, number=1, title="Shot scope")
        self.shot = Shot.objects.create(episode=episode, number=1, title="Actual unit", media_type="video", duration_ms=3000, include_audio=False)
        parameters = [{"key": key, "policy": "locked_value", "value": value}
            for key, value in {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3}.items()]
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": {}, "parameter_policies": parameters}
        self.create_root({"mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": None})
        self.client = APIClient(); self.client.force_authenticate(self.actor)

    def create_root(self, routing, prior=None):
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Isolated route", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": (prior or []) + [{
            "key": "media_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
                **routing}}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="a"*64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(), max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1, "scope": "isolated recipe mapping integration"})
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=False)
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline",
            status="running", ordinal=len(prior or [])+1, idempotency_key=str(uuid4()))

    def prepare(self, **changes):
        return prepare_recipe_media_unit(**{"stage_run_id": self.stage.pk, "expected_run_plan_sha256": self.plan.sha256,
            "scope": {"kind": "shot", "id": str(self.shot.pk)}, "candidate_number": 1, "actor": self.actor, **changes})

    def test_exact_recipe_creates_original_preparation_with_fixed_unit_and_replays(self):
        first = self.prepare()
        self.assertEqual(first.unit_key, f"shot:{self.shot.pk}")
        self.assertEqual(first.stage_run_id, self.stage.pk)
        self.assertEqual(first.contract.resolved_inputs["production_unit"]["scope"], {"kind": "shot", "id": str(self.shot.pk)})
        self.assertIsNone(first.contract.resolved_inputs["recipe_input_review"])
        self.assertEqual(first.contract.resolved_inputs["parameters"]["values"]["duration_seconds"], 3)
        self.project.language = ""; self.project.save(update_fields=("language",))
        self.assertEqual(self.prepare().pk, first.pk)
        with self.assertRaisesMessage(ValidationError, "明确语言"):
            self.prepare(candidate_number=2)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_preview_returns_real_contract_without_preparation_seed_or_dispatch(self):
        url = f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/"
        body = {"stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk), "candidate_number": 1,
            "expected_run_plan_sha256": self.plan.sha256}
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("Preview cannot allocate seed")):
            response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "inputs_validated")
        self.assertEqual(response.data["definition_sha256"], self.definition.sha256)
        self.assertEqual(response.data["inputs"], [])
        self.assertIsNone(response.data["review"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_current_mapping_preview_identifies_existing_frozen_preparation_without_updating_it(self):
        prepared = self.prepare()
        before = deepcopy(prepared.contract.resolved_inputs)
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/",
            {"stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk), "candidate_number": 1,
                "expected_run_plan_sha256": self.plan.sha256}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(str(prepared.pk), response.data["notice"])
        self.assertIn("保持冻结", response.data["notice"])
        prepared.contract.refresh_from_db()
        self.assertEqual(prepared.contract.resolved_inputs, before)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_manual_media_route_can_be_selected_before_reference_review_without_approving_it(self):
        route = self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0]
        second = deepcopy(route)
        second["parameter_policies"][0]["value"] = 512
        self.create_root({"mode": "manual_choice", "variant_count": 2, "routes": [route, second], "evaluator": None},
            prior=[{"key": "storyboard_review", "mode": "review", "approval": "required"}])
        upstream = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=1,
            mode="review", status="awaiting_approval", idempotency_key=str(uuid4()))
        self.run.current_stage_key = upstream.stage_key; self.run.status = "awaiting_approval"
        self.run.save(update_fields=("current_stage_key", "status"))
        self.stage.status = "pending"; self.stage.save(update_fields=("status",))
        url = f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/"
        body = {"stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk), "candidate_number": 2,
            "expected_run_plan_sha256": self.plan.sha256}
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ExecutionPreparation.objects.exists())
        with patch("studio.services.production_runs.advance_production_run", side_effect=AssertionError("Future route choice cannot advance current review")):
            response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-route/",
                {"stage_key": self.stage.stage_key, "route_index": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db(); upstream.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_approval")
        self.assertEqual(upstream.status, "awaiting_approval")
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["route_index"], 1)
        self.assertEqual(response.data["candidate_number"], 2)
        self.assertEqual(response.data["execution_instance_version_id"], str(self.version.pk))
        self.assertFalse(ExecutionPreparation.objects.exists())
        for number in (0, 3):
            response = self.client.post(url, {**body, "candidate_number": number}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.stage.status = "running"; self.stage.save(update_fields=("status",))
        self.run.status = "running"; self.run.save(update_fields=("status",))
        prepared = self.prepare(candidate_number=2)
        self.assertEqual(prepared.request_key, "route:1:candidate:2")
        self.assertEqual(prepared.contract.resolved_inputs["parameters"]["values"]["width"], 512)


    def test_invalid_candidate_scope_and_changed_plan_create_nothing(self):
        for changes in ({"candidate_number": True}, {"candidate_number": 3}, {"scope": {"kind": "shot", "id": str(uuid4())}},
                {"expected_run_plan_sha256": "0"*64}, {"scope": []}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.prepare(**changes)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_original_scheduler_waits_for_real_admission_and_replays_same_candidates(self):
        from studio.services.production_runs import advance_production_run
        self.stage.status = "pending"; self.stage.save(update_fields=("status",))
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "awaiting_input", result.blocked_reason)
        self.assertEqual(result.blocked_code, "execution_preparation_review_required", result.blocked_reason)
        self.assertFalse(GenerationAttempt.objects.exists())
        preparations = ExecutionPreparation.objects.filter(stage_run=self.stage)
        original = set(preparations.values_list("pk", flat=True))
        self.assertEqual(len(original), 2)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot["units"], [{"kind": "shot", "id": str(self.shot.pk)}])
        self.assertEqual(self.stage.config_snapshot["run_plan_sha256"], self.plan.sha256)
        self.assertNotIn("routes", self.stage.config_snapshot)
        advance_production_run(result)
        self.assertEqual(set(preparations.values_list("pk", flat=True)), original)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_frozen_target_set_rejects_new_shot_without_dispatch(self):
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, _poll_media_stage
        _freeze_stage_config(self.run, self.stage); _freeze_stage_input(self.run, self.stage)
        Shot.objects.create(episode=self.shot.episode, number=2, title="Changed set", media_type="video", duration_ms=3000, include_audio=False)
        with self.assertRaisesMessage(ValidationError, "制作对象范围已改变"):
            _poll_media_stage(self.run, self.stage)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_cancelled_preparation_is_terminal_not_waiting_and_does_not_advance_siblings(self):
        from studio.services.production_runs import StageBlocked, _freeze_stage_input, _poll_media_stage
        _freeze_stage_input(self.run, self.stage)
        stopped = self.prepare()
        stopped.status = "cancelled"; stopped.save(update_fields=("status",))
        with patch("studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=AssertionError("Stopped stage must not submit a sibling")):
            with self.assertRaises(StageBlocked) as error:
                _poll_media_stage(self.run, self.stage)
        self.assertEqual(error.exception.code, "execution_preparations_cancelled")
        self.assertIn("已停止", str(error.exception))
        self.assertFalse(GenerationAttempt.objects.exists())
        stopped.refresh_from_db(); self.assertEqual(stopped.status, "cancelled")

    def test_all_units_are_resolved_before_any_dispatch(self):
        from studio.services.production_runs import _freeze_stage_input, _poll_media_stage
        Shot.objects.create(episode=self.shot.episode, number=2, title="Incompatible unit", media_type="image", duration_ms=3000, include_audio=False)
        _freeze_stage_input(self.run, self.stage)
        with patch("studio.services.production_recipe_media_stages.advance_execution_preparation", side_effect=AssertionError("No partial dispatch")):
            with self.assertRaisesMessage(ValidationError, "产物类型"):
                _poll_media_stage(self.run, self.stage)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_task_boundary_includes_candidates_and_empty_scope_cannot_succeed(self):
        from studio.services.production_recipe_media_stages import recipe_media_input_contract
        self.stage = StageRun.objects.get(pk=self.stage.pk)
        with patch("studio.services.production_runs._generation_task_absolute_limit", return_value=1):
            with self.assertRaisesMessage(ValidationError, "乘以候选数"):
                recipe_media_input_contract(self.stage)
        self.shot.delete()
        with self.assertRaisesMessage(ValidationError, "没有明确的制作对象"):
            recipe_media_input_contract(self.stage)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_original_queue_keeps_candidates_distinct_and_failures_never_create_replacements(self):
        from studio.services.production_runs import StageBlocked, _freeze_stage_input, _poll_media_stage
        from studio.tests.test_execution_production_auxiliaries import production_observations
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        _freeze_stage_input(self.run, self.stage)
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        attempts = list(GenerationAttempt.objects.filter(stage_run=self.stage).order_by("id"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual({row.route_index for row in attempts}, {0})
        self.assertEqual({row.execution_instance_version_id for row in attempts}, {self.version.pk})
        self.assertEqual(len({row.preparation_id for row in attempts}), 2)
        self.assertEqual(len({row.request_sha256 for row in attempts}), 2)
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.stage.refresh_from_db(); self.assertEqual(self.stage.attempt_count, 2)
        self.run.refresh_from_db(); self.assertEqual(self.run.generation_task_count, 2)
        GenerationAttempt.objects.filter(pk=attempts[0].pk).update(status="failed", error_code="uncertain_remote_response")
        with self.assertRaises(StageBlocked) as failed:
            _poll_media_stage(self.run, self.stage)
        self.assertEqual(failed.exception.code, "uncertain_remote_response")
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        # A terminal flag without actual archived candidates is not success.
        GenerationAttempt.objects.filter(stage_run=self.stage).update(status="succeeded")
        with self.assertRaisesMessage(ValidationError, "候选产物"):
            _poll_media_stage(self.run, self.stage)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_ordered_media_fallback_is_not_counted_as_an_extra_candidate(self):
        from studio.services.production_runs import _freeze_stage_input, _poll_media_stage
        from studio.tests.test_execution_production_auxiliaries import production_observations
        route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0])
        self.create_root({"mode": "ordered_fallback", "variant_count": 2, "routes": [route, deepcopy(route)], "evaluator": None})
        # Fallback follows exhaustion of the frozen same-request retry budget.
        snapshot = deepcopy(self.run.config_snapshot)
        snapshot["values"]["production.task_max_attempts"] = 1
        assign_json_contract(self.run, "config_snapshot", snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        # Route orchestration fixture only. Formal admission is independently
        # exercised by the text and Workflow integration tests, not this patch.
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        _freeze_stage_input(self.run, self.stage)
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        first = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:0:candidate:1")
        other = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:0:candidate:2")
        failed = GenerationAttempt.objects.get(preparation=first)
        # Definitive async rejection, with explicitly settled isolated account.
        failed.status = "failed"; failed.error_code = "provider_request_rejected"
        failed.actual_cost = 0; failed.committed_cost = 0
        failed.finished_at = timezone.now()
        failed.save(update_fields=("status", "error_code", "actual_cost", "committed_cost", "finished_at"))
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        replacement = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:1:candidate:1")
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 3)
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 3)
        self.assertEqual(GenerationAttempt.objects.get(preparation=other).status, "queued")
        for number, index in ((1, 1), (2, 0)):
            response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/",
                {"stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk), "candidate_number": number,
                    "expected_run_plan_sha256": self.plan.sha256}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["route_index"], index)
        self.assertNotEqual(first.pk, replacement.pk)

    def test_reference_review_reads_exact_target_without_legacy_write_or_new_preparation(self):
        from copy import deepcopy
        from studio.services.production_runs import (_freeze_stage_config, _freeze_stage_input, _formal_snapshot, _save_stage_snapshot)
        from studio.services.json_contracts import json_contract
        from studio.services.execution_definitions import stored_execution_contract
        Shot.objects.create(episode=self.shot.episode, number=2, title="Second reviewed unit", media_type="video", duration_ms=3000, include_audio=False)
        route_stage = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0])
        recipe = ProductionRecipeVersion(recipe_key="review-write-fixture", version=1, name="Review write", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "storyboard", "mode": "deterministic", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"}, route_stage]}, schema_version=3)
        recipe.save()
        run = ProductionRun(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="a"*64)
        for name in ("input_snapshot", "config_snapshot"):
            assign_json_contract(run, name, getattr(self.run, name), max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        storyboard = StageRun.objects.create(production_run=run, stage_key="storyboard", mode="deterministic",
            status="succeeded", ordinal=1, idempotency_key=str(uuid4()))
        reference_review = StageRun.objects.create(production_run=run, stage_key="storyboard_review", mode="review",
            status="awaiting_approval", ordinal=2, idempotency_key=str(uuid4()))
        StageRun.objects.create(production_run=run, stage_key="media_generation", mode="existing_pipeline",
            status="pending", ordinal=3, idempotency_key=str(uuid4()))
        _save_stage_snapshot(storyboard, "output_snapshot", _formal_snapshot(run, storyboard, "storyboard:"+str(self.shot.episode_id)))
        # T2VA needs no references: approval still validates the exact route,
        # rather than using the old empty-selection early return.
        _freeze_stage_config(run, reference_review)
        _freeze_stage_input(run, reference_review)
        from studio.services.production_storyboard_policy import read_recipe_storyboard_reference_selection
        with patch("studio.services.execution_definitions.stored_execution_contract", wraps=stored_execution_contract) as definition_reader:
            result = read_recipe_storyboard_reference_selection(run)
        self.assertEqual(result["shot_count"], 2)
        self.assertGreaterEqual(definition_reader.call_count, 1)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())
