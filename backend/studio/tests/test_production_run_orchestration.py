from decimal import Decimal
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    AssemblyJob,
    ConfigRevision,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationTask,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    PromptArtifact,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ReleaseBundle,
    RuntimeHost,
    RuntimeModelAvailability,
    ScriptSource,
    SeriesProject,
    Shot,
    StageRun,
    StageRouteEvent,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.production_runs import (
    StageBlocked,
    _append_route_event,
    _assert_stage_output_integrity,
    _definitive_media_failure,
    _estimate_and_reserve,
    _execute_assembly,
    _execute_delivery,
    _freeze_stage_input,
    _load_frozen_media_route,
    _prompt_rows_for_run,
    _recipe_stages,
    _route_candidates,
    _selected_media_model_profile,
    select_run_artifact,
    validate_production_recipe_executability,
)
from studio.services.system_workflow_contracts import approved_minimax_reference_image_definition
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.services.workflow_compiler import compile_frozen_workflow
from studio.services.workflow_versions import import_workflow_version
from studio.services.production_pipeline import materialize_generation_task
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.task_queue import claim_next_task, prepare_workflow
from studio.services.workflow import WorkflowBuildError
from studio.tests.factories import create_test_prompt_profile_binding


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example", "127.0.0.1"))
class ProductionRunRoutingAndBudgetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="orchestration-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="编排闭环测试")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        _, self.confirmed_revision = create_pasted_script_source(
            project=self.project, user=self.user, content="# 第一集\n\n测试剧本。",
        )
        self.confirmed_revision = confirm_script_revision(self.confirmed_revision, user=self.user)
        seeded_local_profile = ModelProfileVersion.objects.get(
            profile_key="qwen-local-text",
            version=1,
        )
        self.local_profile = ModelProfileVersion(
            profile_key="orchestration-local-qwen-profile",
            version=1,
            provider=seeded_local_profile.provider,
            model_identifier=seeded_local_profile.model_identifier,
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.local_profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 10000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
            },
        })
        self.local_profile.save()
        self.local_deployment = ModelDeployment(
            key="orchestration-local-qwen",
            name="Orchestration local Qwen",
            profile_version=self.local_profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.local_deployment, "runtime_config", {})
        self.local_deployment.save()

    def _recipe(self, key, routing):
        recipe = ProductionRecipeVersion(
            recipe_key=key,
            version=1,
            name=key,
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{
                "key": "script_generate",
                "mode": "ai",
                "approval": "auto",
                "routing": routing,
            }],
        })
        recipe.save()
        return recipe

    def _create_run(self, recipe, key):
        return self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.confirmed_revision.id),
            "recipe_version_id": str(recipe.id),
            "idempotency_key": key,
        }, format="json")

    def _external_deployment(self, *, key, price=None):
        config = {"base_url": "https://approved.example"}
        if price is not None:
            config["pricing"] = {
                "currency": "USD",
                "fixed_per_request": str(price),
            }
        provider = ProviderProfile(
            key=f"provider-{key}",
            name=f"Provider {key}",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="chat_completion",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", config)
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"profile-{key}",
            version=1,
            provider=provider,
            model_identifier="test-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 10000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"deployment-{key}",
            name=f"Deployment {key}",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        return deployment

    def test_manual_choice_is_frozen_and_cannot_be_implicitly_selected(self):
        recipe = self._recipe("manual-route-recipe", {
            "mode": "manual_choice",
            "routes": [{"model_deployment_id": str(self.local_deployment.id)}],
            "variant_count": 1,
            "evaluator": None,
        })

        with self.assertRaisesMessage(Exception, "受控 chat endpoint"):
            validate_production_recipe_executability(recipe)
        created = self._create_run(recipe, "manual-route-run")
        self.assertEqual(created.status_code, 400, created.data)
        self.assertEqual(ScriptSource.objects.filter(project=self.project).count(), 1)

    def test_ordered_fallback_preserves_declared_order(self):
        second = self._external_deployment(key="ordered-second", price="0.25")
        recipe = self._recipe("ordered-route-recipe", {
            "mode": "ordered_fallback",
            "routes": [
                {"model_deployment_id": str(second.id)},
                {"model_deployment_id": str(self.local_deployment.id)},
            ],
            "variant_count": 2,
            "evaluator": "first_succeeded",
        })

        stages = _recipe_stages(recipe)

        frozen = stages[0]["routing"]
        self.assertEqual(frozen["mode"], "ordered_fallback")
        self.assertEqual(
            [row["model_deployment_id"] for row in frozen["routes"]],
            [str(second.id), str(self.local_deployment.id)],
        )
        self.assertEqual([row["index"] for row in frozen["routes"]], [0, 1])

    def test_unknown_and_over_budget_cost_never_commit_or_submit(self):
        recipe = ProductionRecipeVersion(
            recipe_key="budget-gate-recipe", version=1, name="Budget gate",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{"key": "script_input", "mode": "confirmed_revision", "approval": "required"}],
        })
        recipe.save()
        response = self._create_run(recipe, "budget-gate-run")
        run = ProductionRun.objects.get(pk=response.data["id"])
        stage = run.stages.get(stage_key="script_input")

        unknown = self._external_deployment(key="unknown-cost")
        unknown_attempt = GenerationAttempt.objects.create(
            stage_run=stage,
            unit_key="unknown-cost",
            attempt_number=1,
            model_deployment=unknown,
            route_index=0,
            operation="chat_completion",
            is_external=True,
            request_sha256="a" * 64,
        )
        with self.assertRaises(StageBlocked) as unknown_error:
            _estimate_and_reserve(
                run,
                stage,
                unknown_attempt,
                deployment=unknown,
                operation="chat_completion",
                payload={"schema_version": 1},
                is_external=True,
            )
        self.assertEqual(unknown_error.exception.code, "unknown_cost")
        unknown_attempt.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNone(unknown_attempt.committed_cost)
        self.assertEqual(run.committed_cost_total, Decimal("0"))

        priced = self._external_deployment(key="over-budget", price="2")
        run.allow_external_calls = True
        run.budget_limit = Decimal("1")
        run.save(update_fields=("allow_external_calls", "budget_limit", "updated_at"))
        priced_attempt = GenerationAttempt.objects.create(
            stage_run=stage,
            unit_key="over-budget",
            attempt_number=1,
            model_deployment=priced,
            route_index=0,
            operation="chat_completion",
            is_external=True,
            request_sha256="b" * 64,
        )
        with self.assertRaises(StageBlocked) as budget_error:
            _estimate_and_reserve(
                run,
                stage,
                priced_attempt,
                deployment=priced,
                operation="chat_completion",
                payload={"schema_version": 1},
                is_external=True,
            )
        self.assertEqual(budget_error.exception.code, "budget_limit_exceeded")
        priced_attempt.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNone(priced_attempt.committed_cost)
        self.assertIsNone(priced_attempt.generation_task_id)
        self.assertEqual(run.committed_cost_total, Decimal("0"))

    def test_all_transports_project_actual_plus_committed_plus_new_cost(self):
        recipe = ProductionRecipeVersion(
            recipe_key="shared-budget-projection", version=1, name="Shared budget projection",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{"key": "script_input", "mode": "confirmed_revision", "approval": "required"}],
        })
        recipe.save()
        response = self._create_run(recipe, "shared-budget-projection-run")
        run = ProductionRun.objects.get(pk=response.data["id"])
        stage = run.stages.get(stage_key="script_input")
        run.allow_external_calls = True
        run.budget_limit = Decimal("1.000000")
        run.actual_cost_total = Decimal("0.400000")
        run.committed_cost_total = Decimal("0.300000")
        run.cost_currency = "USD"
        run.save(update_fields=(
            "allow_external_calls", "budget_limit", "actual_cost_total",
            "committed_cost_total", "cost_currency", "updated_at",
        ))
        estimate_driver = SimpleNamespace(estimate=lambda *_args, **_kwargs: {
            "schema_version": 1,
            "amount": "0.400000",
            "currency": "USD",
        })
        for index, operation in enumerate((
            "chat_completion", "image.text_to_image", "media.generate",
        ), start=1):
            attempt = GenerationAttempt.objects.create(
                stage_run=stage,
                unit_key=f"shared-budget-{index}",
                attempt_number=1,
                model_deployment=self.local_deployment,
                route_index=0,
                operation=operation,
                is_external=True,
                request_sha256=str(index) * 64,
            )
            with patch(
                "studio.services.production_runs.driver_for",
                return_value=estimate_driver,
            ), self.assertRaises(StageBlocked) as captured:
                _estimate_and_reserve(
                    run,
                    stage,
                    attempt,
                    deployment=self.local_deployment,
                    operation=operation,
                    payload={"schema_version": 1},
                    is_external=True,
                )
            self.assertEqual(captured.exception.code, "budget_limit_exceeded")
            attempt.refresh_from_db()
            self.assertIsNone(attempt.committed_cost)

        run.refresh_from_db()
        self.assertEqual(run.actual_cost_total, Decimal("0.400000"))
        self.assertEqual(run.committed_cost_total, Decimal("0.300000"))

    def test_budget_authorization_cannot_hide_existing_total_exposure(self):
        recipe = ProductionRecipeVersion(
            recipe_key="authorization-exposure", version=1, name="Authorization exposure",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{"key": "script_input", "mode": "confirmed_revision", "approval": "required"}],
        })
        recipe.save()
        response = self._create_run(recipe, "authorization-exposure-run")
        run = ProductionRun.objects.get(pk=response.data["id"])
        stage = run.stages.get(stage_key="script_input")
        run.actual_cost_total = Decimal("0.400000")
        run.committed_cost_total = Decimal("0.300000")
        run.cost_currency = "USD"
        run.status = ProductionRun.Status.AWAITING_APPROVAL
        run.save(update_fields=(
            "actual_cost_total", "committed_cost_total", "cost_currency", "status", "updated_at",
        ))
        stage.status = StageRun.Status.AWAITING_APPROVAL
        stage.reason_code = "budget_limit_exceeded"
        stage.save(update_fields=("status", "reason_code", "updated_at"))
        before_decisions = stage.approval_decisions.count()

        rejected = self.client.post(
            f"/api/v1/production-runs/{run.id}/authorize-cost/",
            {
                "stage_key": "script_input",
                "allow_external_calls": True,
                "budget_limit": "0.690000",
                "currency": "USD",
                "comment": "must cover actual and committed exposure",
            },
            format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(stage.approval_decisions.count(), before_decisions)
        run.refresh_from_db()
        stage.refresh_from_db()
        self.assertIsNone(run.budget_limit)
        self.assertEqual(stage.status, StageRun.Status.AWAITING_APPROVAL)
        self.assertEqual(stage.reason_code, "budget_limit_exceeded")

    def test_removed_low_level_write_routes_are_not_resolvable(self):
        for url in (
            f"/api/v1/projects/{self.project.id}/build-production/",
            "/api/v1/prompts/generate/",
            "/api/v1/workflows/generate/",
        ):
            response = self.client.post(url, {}, format="json")
            self.assertEqual(response.status_code, 404, (url, getattr(response, "data", None)))

        task_count = GenerationTask.objects.count()
        task_create = self.client.post("/api/v1/tasks/", {}, format="json")
        self.assertEqual(task_create.status_code, 405, task_create.data)
        self.assertEqual(GenerationTask.objects.count(), task_count)

        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertIn("get", schema["paths"]["/api/v1/tasks/"])
        self.assertNotIn("post", schema["paths"]["/api/v1/tasks/"])
        self.assertNotIn("/api/v1/projects/{id}/build-production/", schema["paths"])

    def test_route_audit_preserves_more_than_thirty_two_unit_events(self):
        recipe = ProductionRecipeVersion(
            recipe_key="route-event-recipe", version=1, name="Route event",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{"key": "script_input", "mode": "confirmed_revision", "approval": "required"}],
        })
        recipe.save()
        response = self._create_run(recipe, "route-event-run")
        stage = ProductionRun.objects.get(pk=response.data["id"]).stages.get()

        for index in range(40):
            _append_route_event(
                stage,
                unit_key=f"unit:{index}",
                event_type="candidate_selected",
                route_index=0,
                code="fixed_route",
            )

        events = list(StageRouteEvent.objects.filter(stage_run=stage).order_by("sequence"))
        self.assertEqual(len(events), 40)
        self.assertEqual([item.sequence for item in events], list(range(1, 41)))
        self.assertEqual(events[-1].unit_key, "unit:39")


@override_settings(
    AI_PROVIDER_ALLOWED_HOSTS=("approved.example", "127.0.0.1"),
    H3_PROMPT_OPTIMIZER={"enabled": False},
)
class ProductionRunMediaGateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="media-gate-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="媒体门禁测试")
        episode = Episode.objects.create(project=self.project, number=1, title="第一集")
        self.shot = Shot.objects.create(
            episode=episode,
            number=1,
            title="雨夜门口",
            narrative="主角站在雨夜门口，电影感构图。",
            media_type=Shot.MediaType.IMAGE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.workflow_version, self.deployment = self._media_route()
        prompt_field = PromptFieldDefinition.objects.create(
            key="media_gate_quality",
            label="Quality",
            category="quality",
            description="Reviewed media gate fixture",
            impact="Affects the positive prompt",
            value_type=PromptFieldDefinition.ValueType.TEXT,
            default_value="cinematic detail",
        )
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="media-gate-sdxl-image",
            version=1,
            name="Media gate SDXL image",
            model_family="sdxl",
            operation="image.generate",
            mode="",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=prompt_profile,
            field_definition=prompt_field,
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
        )
        refresh_prompt_profile_contract(prompt_profile)
        prompt_profile = publish_prompt_profile(prompt_profile, actor=self.user)
        self.media_prompt_binding = PromptProfileBinding.objects.create(
            key="media-gate-sdxl-image",
            name="Media gate SDXL image",
            operation="image.generate",
            model_family="sdxl",
            prompt_profile_version=prompt_profile,
            priority=0,
            is_active=True,
        )
        self.recipe = ProductionRecipeVersion(
            recipe_key="media-gate-recipe",
            version=1,
            name="Media gate recipe",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {
                    "key": "prompt_compile",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                },
                {
                    "key": "media_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{
                            "workflow_version_id": str(self.workflow_version.id),
                            "model_deployment_id": str(self.deployment.id),
                        }],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
            ],
        })
        self.recipe.save()

    def _publish_language_gate(self, mode: str) -> None:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={"production.h3_prompt_language_gate": mode},
            created_by=self.user,
            change_summary=f"test language gate {mode}",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.user)

    def _media_route(self, suffix=""):
        token = f"-{suffix}" if suffix else ""
        provider = ProviderProfile(
            key=f"media-gate-comfyui{token}",
            name="Media gate ComfyUI",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://approved.example",
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"media-gate-image{token}",
            version=1,
            provider=provider,
            model_identifier="sd_xl_base_1.0.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "sdxl",
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="primary-checkpoint",
            filename="sd_xl_base_1.0.safetensors",
            sha256="a" * 64,
            size_bytes=1,
            status=ModelArtifact.Status.AVAILABLE,
        )
        deployment = ModelDeployment(
            key=f"media-gate-deployment{token}",
            name="Media gate deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        template = WorkflowTemplate.objects.create(
            name="Media gate image workflow",
            slug=f"media-gate-image-workflow{token}",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            provider="comfyui",
            model="sd_xl_base_1.0.safetensors",
            definition=approved_minimax_reference_image_definition(),
            input_schema={
                "schema_version": 1,
                "workflow_mode": "text_to_image",
                "fixed_model": True,
                "required_dimension_multiple": 8,
            },
            is_system=True,
            is_active=True,
        )
        version = import_workflow_version(
            template=template,
            provider=provider,
            model_profile_version=profile,
            graph={
                "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "fixture", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
                "4": {"class_type": "CheckpointLoaderSimple", "inputs": {
                    "ckpt_name": artifact.filename,
                }},
                "5": {"class_type": "RandomNoise", "inputs": {
                    "noise_seed": 0,
                }},
            },
            input_contract={
                "schema_version": 2,
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": {
                    "prompt.positive": {"type": "string", "required": True},
                    "output.prefix": {"type": "string", "required": True},
                    "sampling.seed": {
                        "type": "integer", "required": True, "min": 0,
                    },
                    "image.width": {"type": "integer", "required": True},
                    "image.height": {"type": "integer", "required": True},
                    "model.primary-checkpoint.filename": {
                        "type": "model_artifact",
                        "required": True,
                        "requirement_alias": "primary-checkpoint",
                    },
                },
                "outputs": {"primary": {"node_id": "2", "kind": "image", "required": True}},
                "smoke_inputs": {
                    "prompt.positive": "smoke", "image.width": 512, "image.height": 512,
                },
            },
            semantic_bindings=[
                {
                    "semantic_key": "prompt.positive",
                    "node_id": "1",
                    "input_name": "text",
                    "value_type": "string",
                    "is_required": True,
                },
                {
                    "semantic_key": "output.prefix",
                    "node_id": "2",
                    "input_name": "filename_prefix",
                    "value_type": "string",
                    "is_required": True,
                },
                {
                    "semantic_key": "sampling.seed",
                    "node_id": "5",
                    "input_name": "noise_seed",
                    "value_type": "integer",
                    "is_required": True,
                },
                {
                    "semantic_key": "image.width",
                    "node_id": "3",
                    "input_name": "width",
                    "value_type": "integer",
                    "is_required": True,
                },
                {
                    "semantic_key": "image.height",
                    "node_id": "3",
                    "input_name": "height",
                    "value_type": "integer",
                    "is_required": True,
                },
                {
                    "semantic_key": "model.primary-checkpoint.filename",
                    "node_id": "4",
                    "input_name": "ckpt_name",
                    "value_type": "model_artifact",
                    "is_required": True,
                },
            ],
            model_requirements=[{
                "role": artifact.role,
                "alias": "primary-checkpoint",
                "artifact_id": str(artifact.id),
                "expected_filename": artifact.filename,
                "expected_sha256": artifact.sha256,
                "is_required": True,
            }],
        )
        version.validated_deployment = deployment
        version.status = WorkflowTemplateVersion.Status.PUBLISHED
        version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.is_current = True
        version.save(update_fields=(
            "validated_deployment", "status", "validation_state", "smoke_test_state",
            "is_current", "updated_at",
        ))
        return version, deployment

    def _use_ordered_routes(self, second_version, second_deployment):
        definition = {
            "schema_version": 1,
            "stages": [
                {
                    "key": "prompt_compile",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                },
                {
                    "key": "media_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "ordered_fallback",
                        "routes": [
                            {
                                "workflow_version_id": str(self.workflow_version.id),
                                "model_deployment_id": str(self.deployment.id),
                            },
                            {
                                "workflow_version_id": str(second_version.id),
                                "model_deployment_id": str(second_deployment.id),
                            },
                        ],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
            ],
        }
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))

    @staticmethod
    def _gate_report(*, blocked):
        return SimpleNamespace(
            blocks_enqueue=blocked,
            fields_requiring_english_rewrite=("narrative",) if blocked else (),
        )

    def _create_run(self, key, *, workflow_route_index=None):
        payload = {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": key,
        }
        if workflow_route_index is not None:
            payload["workflow_selection"] = {
                "schema_version": 1,
                "mode": "uniform",
                "route_index": workflow_route_index,
            }
        return self.client.post("/api/v1/production-runs/", payload, format="json")

    def test_recipe_media_route_options_are_safe_and_expose_dependency_gaps(self):
        response = self.client.get(
            f"/api/v1/production-recipes/{self.recipe.id}/media-route-options/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["recipe_version_id"], str(self.recipe.id))
        self.assertEqual(response.data["recipe_definition_sha256"], self.recipe.definition_sha256)
        self.assertEqual(len(response.data["routes"]), 1)
        route = response.data["routes"][0]
        self.assertEqual(route["route_index"], 0)
        self.assertEqual(route["workflow_label"], "Media gate image workflow · v1")
        self.assertEqual(route["deployment_label"], "Media gate deployment")
        self.assertEqual(route["output_kinds"], ["image"])
        self.assertTrue(route["is_selectable"])
        self.assertNotIn("driver_config", route)
        self.assertNotIn("runtime_config", route)

        self.media_prompt_binding.is_active = False
        self.media_prompt_binding.save(update_fields=("is_active", "updated_at"))
        missing_profile = self.client.get(
            f"/api/v1/production-recipes/{self.recipe.id}/media-route-options/",
        )
        self.assertEqual(missing_profile.status_code, 200, missing_profile.data)
        self.assertFalse(missing_profile.data["routes"][0]["is_selectable"])
        self.assertIn(
            "prompt_profile_binding_missing",
            missing_profile.data["routes"][0]["reason_codes"],
        )
        self.media_prompt_binding.is_active = True
        self.media_prompt_binding.save(update_fields=("is_active", "updated_at"))

        self.workflow_version.is_current = False
        self.workflow_version.save(update_fields=("is_current", "updated_at"))
        blocked = self.client.get(
            f"/api/v1/production-recipes/{self.recipe.id}/media-route-options/",
        )
        self.assertEqual(blocked.status_code, 200, blocked.data)
        blocked_route = blocked.data["routes"][0]
        self.assertFalse(blocked_route["is_selectable"])
        self.assertIn("workflow_not_current", blocked_route["reason_codes"])
        self.assertIn("工作流不是 current 版本", blocked_route["reasons"])

    def test_fixed_single_media_route_is_deterministically_frozen_without_user_selection(self):
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            response = self._create_run("fixed-route-auto-selection")

        self.assertEqual(response.status_code, 201, response.data)
        frozen = response.data["input_snapshot"]["workflow_selection"]
        self.assertEqual(frozen["mode"], "uniform")
        self.assertEqual(frozen["route"]["route_index"], 0)
        self.assertEqual(
            response.data["input_snapshot"]["workflow_selection_sha256"],
            json_contract(frozen).sha256,
        )

    def test_uniform_route_without_shots_rejects_non_video_workflow_before_run_creation(self):
        self.shot.delete()

        response = self._create_run("future-video-rejects-image-route")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不能用于 video 镜头", str(response.data))
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="future-video-rejects-image-route",
            ).exists(),
        )

    def test_uniform_route_without_shots_uses_project_default_h3_mode(self):
        self.shot.delete()
        self.project.production_settings = {"default_h3_mode": Shot.H3Mode.FL2VA}
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.workflow_version.template.output_kind = WorkflowTemplate.OutputKind.VIDEO
        self.workflow_version.template.save(update_fields=("output_kind", "updated_at"))
        assign_json_contract(self.workflow_version, "input_contract", {
            **self.workflow_version.input_contract,
            "h3_modes": [Shot.H3Mode.T2VA],
        })
        self.workflow_version.save(update_fields=(
            "input_contract", "input_contract_schema_version",
            "input_contract_size_bytes", "input_contract_sha256", "updated_at",
        ))
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=self.deployment.profile_version,
            operation="video.generate",
            mode=Shot.H3Mode.T2VA,
        )

        response = self._create_run("future-shot-h3-mode-mismatch")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(Shot.H3Mode.FL2VA, str(response.data))
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="future-shot-h3-mode-mismatch",
            ).exists(),
        )

    def test_downstream_manual_gates_reach_prompt_and_media_main_path(self):
        definition = {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "manual", "approval": "required"},
                {"key": "storyboard", "mode": "manual", "approval": "required"},
                {"key": "prompt_compile", "mode": "existing_pipeline", "approval": "auto"},
                {
                    "key": "media_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{
                            "workflow_version_id": str(self.workflow_version.id),
                            "model_deployment_id": str(self.deployment.id),
                        }],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
            ],
        }
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))
        created = self._create_run("downstream-manual-main-path")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["current_stage_key"], "script_input")

        script_gate = self.client.post(
            f"/api/v1/production-runs/{created.data['id']}/approve/",
            {
                "stage_key": "script_input",
                "decision": "approve",
                "comment": "existing episode selection reviewed",
            },
            format="json",
        )
        self.assertEqual(script_gate.status_code, 200, script_gate.data)
        self.assertEqual(script_gate.data["current_stage_key"], "storyboard")

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            storyboard_gate = self.client.post(
                f"/api/v1/production-runs/{created.data['id']}/approve/",
                {
                    "stage_key": "storyboard",
                    "decision": "approve",
                    "comment": "existing shots reviewed",
                },
                format="json",
            )

        self.assertEqual(storyboard_gate.status_code, 200, storyboard_gate.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        self.assertEqual(
            run.stages.get(stage_key="prompt_compile").status,
            StageRun.Status.SUCCEEDED,
        )
        self.assertTrue(PromptArtifact.objects.filter(project=self.project).exists())
        self.assertNotEqual(
            run.stages.get(stage_key="media_generation").status,
            StageRun.Status.PENDING,
        )

    def test_non_fixed_or_multi_candidate_media_route_requires_selection_before_create(self):
        second_version, second_deployment = self._media_route("required-selection")
        self._use_ordered_routes(second_version, second_deployment)

        missing_multi = self._create_run("missing-multi-route-selection")

        self.assertEqual(missing_multi.status_code, 400, missing_multi.data)
        self.assertEqual(
            missing_multi.data["error"]["code"],
            "route_selection_required",
        )
        self.assertIn("workflow_selection", missing_multi.data["error"]["details"])
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="missing-multi-route-selection",
            ).exists(),
        )

        definition = self.recipe.definition
        media_stage = next(
            item for item in definition["stages"] if item["key"] == "media_generation"
        )
        media_stage["routing"] = {
            **media_stage["routing"],
            "mode": "manual_choice",
            "routes": media_stage["routing"]["routes"][:1],
        }
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))

        missing_non_fixed = self._create_run("missing-non-fixed-selection")
        self.assertEqual(missing_non_fixed.status_code, 400, missing_non_fixed.data)
        self.assertEqual(
            missing_non_fixed.data["error"]["code"],
            "route_selection_required",
        )
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="missing-non-fixed-selection",
            ).exists(),
        )

    def test_media_prompt_compile_requires_one_active_profile_binding_for_actual_route(self):
        self._publish_language_gate("warn")
        self.media_prompt_binding.is_active = False
        self.media_prompt_binding.save(update_fields=("is_active", "updated_at"))

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "missing-media-prompt-profile",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "uniform",
                "route_index": 0,
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("缺少 active PromptProfile binding", str(response.data))
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="missing-media-prompt-profile",
            ).exists(),
        )
        self.assertFalse(PromptArtifact.objects.filter(project=self.project).exists())

    def test_media_prompt_freezes_exact_uniform_route_model_and_profile_binding(self):
        self._publish_language_gate("warn")
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "exact-media-prompt-profile",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "uniform",
                "route_index": 0,
            },
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        prompt = PromptArtifact.objects.get(project=self.project)
        model_profile = self.deployment.profile_version
        self.assertEqual(prompt.model, model_profile.model_identifier)
        self.assertEqual(
            prompt.request_options["model_profile_version_id"],
            str(model_profile.id),
        )
        self.assertEqual(prompt.request_options["selected_media_route_index"], 0)
        self.assertEqual(
            prompt.request_options["workflow_selection_sha256"],
            response.data["input_snapshot"]["workflow_selection_sha256"],
        )
        self.assertEqual(prompt.prompt_profile_binding_key, self.media_prompt_binding.key)
        self.assertEqual(
            prompt.structured_payload["target_model_profile"]["specification_sha256"],
            model_profile.specification_sha256,
        )
        self.assertEqual(
            prompt.structured_payload["prompt_profile"]["contract_sha256"],
            prompt.prompt_profile_contract_sha256,
        )

    def test_per_shot_workflow_selection_is_complete_frozen_and_retry_stable(self):
        second_shot = Shot.objects.create(
            episode=self.shot.episode,
            number=2,
            title="走廊尽头",
            narrative="主角走向走廊尽头。",
            media_type=Shot.MediaType.IMAGE,
        )
        second_version, second_deployment = self._media_route("per-shot")
        self._use_ordered_routes(second_version, second_deployment)
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "per-shot-selection",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "per_shot",
                "shot_routes": [
                    {"shot_id": str(self.shot.id), "route_index": 0},
                    {"shot_id": str(second_shot.id), "route_index": 1},
                ],
            },
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        frozen = response.data["input_snapshot"]["workflow_selection"]
        self.assertEqual(frozen["mode"], "per_shot")
        self.assertEqual(
            [row["route"]["route_index"] for row in frozen["shot_routes"]],
            [0, 1],
        )
        self.assertEqual(
            response.data["input_snapshot"]["workflow_selection_sha256"],
            json_contract(frozen).sha256,
        )

        run = ProductionRun.objects.get(pk=response.data["id"])
        first_profile, first_route_index, first_selection_sha256 = (
            _selected_media_model_profile(run, shot=self.shot)
        )
        second_profile, second_route_index, second_selection_sha256 = (
            _selected_media_model_profile(run, shot=second_shot)
        )
        self.assertEqual(first_profile.id, self.deployment.profile_version_id)
        self.assertEqual(second_profile.id, second_deployment.profile_version_id)
        self.assertEqual((first_route_index, second_route_index), (0, 1))
        self.assertEqual(
            first_selection_sha256,
            response.data["input_snapshot"]["workflow_selection_sha256"],
        )
        self.assertEqual(second_selection_sha256, first_selection_sha256)
        stage = run.stages.get(stage_key="media_generation")
        self.assertEqual(stage.routing_decision["state"], "selected_by_unit")
        self.assertEqual(
            stage.routing_decision["units"][f"shot:{second_shot.id}"]["route_index"],
            1,
        )
        _freeze_stage_input(run, stage)
        stage.refresh_from_db()
        self.assertEqual(stage.input_snapshot["workflow_selection"], frozen)
        self.assertEqual(
            stage.input_snapshot["workflow_selection_sha256"],
            response.data["input_snapshot"]["workflow_selection_sha256"],
        )

        prompt = SimpleNamespace(
            shot_id=second_shot.id,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            h3_mode=Shot.H3Mode.T2VA,
            structured_payload={},
        )
        candidates = _route_candidates(
            stage,
            unit_key="retry-exact-route",
            prompt=prompt,
            after_route_index=1,
        )
        self.assertEqual([item["index"] for item in candidates], [1])

    def test_per_shot_workflow_selection_rejects_missing_and_foreign_shots(self):
        second_shot = Shot.objects.create(
            episode=self.shot.episode,
            number=2,
            title="第二镜",
            narrative="第二镜。",
            media_type=Shot.MediaType.IMAGE,
        )
        missing = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "per-shot-missing",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "per_shot",
                "shot_routes": [{"shot_id": str(self.shot.id), "route_index": 0}],
            },
        }, format="json")
        self.assertEqual(missing.status_code, 400, missing.data)
        self.assertIn("缺少 1 个", str(missing.data))

        duplicate = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "per-shot-duplicate",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "per_shot",
                "shot_routes": [
                    {"shot_id": str(self.shot.id), "route_index": 0},
                    {"shot_id": str(self.shot.id), "route_index": 0},
                    {"shot_id": str(second_shot.id), "route_index": 0},
                ],
            },
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertIn("不能重复", str(duplicate.data))

        foreign_project = SeriesProject.objects.create(owner=self.user, title="其他项目")
        foreign_episode = Episode.objects.create(project=foreign_project, number=1, title="外部集")
        foreign_shot = Shot.objects.create(
            episode=foreign_episode,
            number=1,
            narrative="外部镜头",
            media_type=Shot.MediaType.IMAGE,
        )
        foreign = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "per-shot-foreign",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "per_shot",
                "shot_routes": [
                    {"shot_id": str(self.shot.id), "route_index": 0},
                    {"shot_id": str(second_shot.id), "route_index": 0},
                    {"shot_id": str(foreign_shot.id), "route_index": 0},
                ],
            },
        }, format="json")
        self.assertEqual(foreign.status_code, 400, foreign.data)
        self.assertIn("跨项目", str(foreign.data))

    def test_uniform_workflow_selection_revalidates_output_kind(self):
        self.shot.media_type = Shot.MediaType.VIDEO
        self.shot.h3_mode = Shot.H3Mode.T2VA
        self.shot.save(update_fields=("media_type", "h3_mode", "updated_at"))
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "uniform-output-mismatch",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "uniform",
                "route_index": 0,
            },
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不能用于 video 镜头", str(response.data))

    def test_uniform_workflow_selection_revalidates_workflow_lifecycle_and_binding(self):
        payload = {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "uniform-lifecycle-invalid",
            "workflow_selection": {
                "schema_version": 1,
                "mode": "uniform",
                "route_index": 0,
            },
        }
        self.workflow_version.is_current = False
        self.workflow_version.save(update_fields=("is_current", "updated_at"))
        not_current = self.client.post(
            "/api/v1/production-runs/", payload, format="json",
        )
        self.assertEqual(not_current.status_code, 400, not_current.data)
        self.assertIn("workflow 未发布或未通过 smoke-test", str(not_current.data))

        self.workflow_version.is_current = True
        self.workflow_version.validated_deployment = None
        self.workflow_version.save(update_fields=(
            "is_current", "validated_deployment", "updated_at",
        ))
        payload["idempotency_key"] = "uniform-binding-invalid"
        binding_drift = self.client.post(
            "/api/v1/production-runs/", payload, format="json",
        )
        self.assertEqual(binding_drift.status_code, 400, binding_drift.data)
        self.assertIn("部署绑定已漂移", str(binding_drift.data))

    def _delivery_recipe_definition(self):
        return {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "manual", "approval": "required"},
                {"key": "storyboard", "mode": "manual", "approval": "required"},
                {"key": "prompt_compile", "mode": "existing_pipeline", "approval": "auto"},
                {
                    "key": "media_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{
                            "workflow_version_id": str(self.workflow_version.id),
                            "model_deployment_id": str(self.deployment.id),
                        }],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
                {"key": "artifact_selection", "mode": "review", "approval": "required"},
                {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
                {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"},
            ],
        }

    def test_recipe_publish_requires_complete_delivery_closure(self):
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))

        valid = ProductionRecipeVersion(
            recipe_key="publish-complete-delivery",
            version=1,
            name="Publish complete delivery",
            status=ProductionRecipeVersion.Status.DRAFT,
        )
        assign_json_contract(valid, "definition", self._delivery_recipe_definition())
        valid.save()
        published = self.client.post(
            f"/api/v1/production-recipes/{valid.id}/publish/", {}, format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        valid.refresh_from_db()
        self.assertEqual(valid.status, ProductionRecipeVersion.Status.PUBLISHED)

        invalid_definitions = {}
        missing_delivery = self._delivery_recipe_definition()
        missing_delivery["stages"] = [
            stage for stage in missing_delivery["stages"] if stage["key"] != "delivery"
        ]
        invalid_definitions["missing-delivery"] = missing_delivery
        wrong_selection = self._delivery_recipe_definition()
        next(
            stage for stage in wrong_selection["stages"] if stage["key"] == "artifact_selection"
        )["mode"] = "skip"
        invalid_definitions["wrong-selection-mode"] = wrong_selection
        skipped_storyboard = self._delivery_recipe_definition()
        next(
            stage for stage in skipped_storyboard["stages"] if stage["key"] == "storyboard"
        )["mode"] = "skip"
        invalid_definitions["skipped-storyboard"] = skipped_storyboard
        wrong_order = self._delivery_recipe_definition()
        stages = wrong_order["stages"]
        assembly_index = next(i for i, stage in enumerate(stages) if stage["key"] == "assembly")
        delivery_index = next(i for i, stage in enumerate(stages) if stage["key"] == "delivery")
        stages[assembly_index], stages[delivery_index] = stages[delivery_index], stages[assembly_index]
        invalid_definitions["wrong-order"] = wrong_order

        for index, (label, definition) in enumerate(invalid_definitions.items(), start=1):
            with self.subTest(label=label):
                recipe = ProductionRecipeVersion(
                    recipe_key=f"publish-invalid-{label}",
                    version=index,
                    name=label,
                    status=ProductionRecipeVersion.Status.DRAFT,
                )
                assign_json_contract(recipe, "definition", definition)
                recipe.save()
                rejected = self.client.post(
                    f"/api/v1/production-recipes/{recipe.id}/publish/", {}, format="json",
                )
                self.assertEqual(rejected.status_code, 400, rejected.data)
                recipe.refresh_from_db()
                self.assertEqual(recipe.status, ProductionRecipeVersion.Status.DRAFT)

    def test_cost_authorization_materializes_one_real_task_idempotently(self):
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("authorized-media")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(created.data["blocked_code"], "unknown_cost")
        run = ProductionRun.objects.get(pk=created.data["id"])
        self.assertEqual(run.generation_task_count, 0)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=run).exists())
        self.assertFalse(GenerationTask.objects.filter(project=self.project).exists())

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            authorized = self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "1.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                    "comment": "approved test budget",
                },
                format="json",
            )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        self.assertEqual(GenerationTask.objects.filter(project=self.project).count(), 1)
        attempt = GenerationAttempt.objects.get(stage_run__production_run=run)
        self.assertIsNotNone(attempt.generation_task_id)
        self.assertEqual(attempt.committed_cost, Decimal("0.250000"))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            continued = self.client.post(
                f"/api/v1/production-runs/{run.id}/continue/", {}, format="json",
            )
        self.assertEqual(continued.status_code, 200, continued.data)
        self.assertEqual(GenerationTask.objects.filter(project=self.project).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=run).count(), 1)

    def test_prompt_compile_blocks_distinct_shots_when_profile_drops_all_distinguishing_semantics(self):
        self.media_prompt_binding.is_active = False
        self.media_prompt_binding.save(update_fields=("is_active", "updated_at"))
        camera_field = PromptFieldDefinition.objects.create(
            key="media_gate_camera_only",
            label="Camera only",
            category="camera",
            description="Deliberately incomplete per-shot fixture",
            impact="Would collapse distinct shots onto the same conditioning",
            value_type=PromptFieldDefinition.ValueType.TEXT,
        )
        camera_profile = PromptProfileVersion.objects.create(
            profile_key="media-gate-camera-only",
            version=1,
            name="Media gate camera only",
            model_family="sdxl",
            operation="image.generate",
            mode="",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=camera_profile,
            field_definition=camera_field,
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
        )
        refresh_prompt_profile_contract(camera_profile)
        camera_profile = publish_prompt_profile(camera_profile, actor=self.user)
        PromptProfileBinding.objects.create(
            key="media-gate-camera-only",
            name="Media gate camera only",
            operation="image.generate",
            model_family="sdxl",
            model_profile_version=self.deployment.profile_version,
            prompt_profile_version=camera_profile,
            priority=100,
            is_active=True,
        )
        self.shot.camera = "locked camera shared by both shots"
        self.shot.save(update_fields=("camera", "updated_at"))
        second = Shot.objects.create(
            episode=self.shot.episode,
            number=2,
            title="Different story beat",
            narrative="A different subject runs through a sunlit courtyard.",
            camera=self.shot.camera,
            media_type=Shot.MediaType.IMAGE,
        )

        created = self._create_run("duplicate-shot-conditioning")

        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(run.blocked_code, "duplicate_shot_conditioning")
        self.assertIn(str(self.shot.id), run.blocked_reason)
        self.assertIn(str(second.id), run.blocked_reason)
        self.assertFalse(PromptArtifact.objects.filter(project=self.project).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
        ).exists())

        # Simulate a run whose prompt_compile stage completed before this gate
        # was deployed.  Media materialization must re-check the immutable
        # PromptArtifacts instead of trusting the historical stage result.
        with patch(
            "studio.services.production_runs._enforce_distinct_shot_conditioning",
        ), patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            legacy_created = self._create_run("duplicate-shot-conditioning-legacy")
        self.assertEqual(legacy_created.status_code, 201, legacy_created.data)
        legacy_run = ProductionRun.objects.get(pk=legacy_created.data["id"])
        self.assertEqual(legacy_run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(legacy_run.blocked_code, "unknown_cost")
        self.assertEqual(PromptArtifact.objects.filter(project=self.project).count(), 2)
        self.assertEqual(GeneratedArtifact.objects.filter(
            production_run=legacy_run,
            kind=GeneratedArtifact.Kind.PROMPT,
        ).count(), 2)

        authorized = self.client.post(
            f"/api/v1/production-runs/{legacy_run.id}/authorize-cost/",
            {
                "stage_key": "media_generation",
                "allow_external_calls": True,
                "budget_limit": "1.0000",
                "approved_estimated_cost": "0.250000",
                "currency": "USD",
            },
            format="json",
        )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        legacy_run.refresh_from_db()
        self.assertEqual(legacy_run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(legacy_run.blocked_code, "duplicate_shot_conditioning")
        self.assertFalse(
            GenerationAttempt.objects.filter(stage_run__production_run=legacy_run).exists()
        )
        self.assertFalse(GenerationTask.objects.filter(project=self.project).exists())

        retried = self.client.post(
            f"/api/v1/production-runs/{legacy_run.id}/retry/", {}, format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        legacy_run.refresh_from_db()
        self.assertEqual(legacy_run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(legacy_run.blocked_code, "duplicate_shot_conditioning")
        self.assertFalse(
            GenerationAttempt.objects.filter(stage_run__production_run=legacy_run).exists()
        )

    def test_media_tasks_freeze_unique_seed_and_output_prefix_per_shot_and_variant(self):
        second = Shot.objects.create(
            episode=self.shot.episode,
            number=2,
            title="Second image",
            narrative="A second subject crosses a bright empty courtyard.",
            media_type=Shot.MediaType.IMAGE,
        )
        definition = dict(self.recipe.definition)
        definition["stages"] = [dict(item) for item in definition["stages"]]
        media_stage = next(
            item for item in definition["stages"] if item["key"] == "media_generation"
        )
        media_stage["routing"] = dict(media_stage["routing"])
        media_stage["routing"]["variant_count"] = 2
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("per-shot-execution-identity")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            authorized = self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "2.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                },
                format="json",
            )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        attempts = list(
            run.stages.get(stage_key="media_generation").generation_attempts
            .select_related("generation_task__generated_workflow")
            .order_by("unit_key")
        )
        self.assertEqual(len(attempts), 4)
        identities = [item.request_snapshot["execution_identity"] for item in attempts]
        self.assertEqual(len({item["seed"] for item in identities}), 4)
        self.assertEqual(len({item["output_prefix"] for item in identities}), 4)
        self.assertEqual(
            {item["prompt_artifact_id"] for item in identities},
            {
                item.request_snapshot["prompt_artifact_id"]
                for item in attempts
            },
        )
        self.assertEqual({item["variant_number"] for item in identities}, {1, 2})
        for attempt, identity in zip(attempts, identities, strict=True):
            task = attempt.generation_task
            workflow = task.generated_workflow
            semantics = workflow.injected_values["resolved_semantics"]
            self.assertEqual(task.seed, identity["seed"])
            self.assertEqual(semantics["sampling.seed"], identity["seed"])
            self.assertEqual(semantics["output.prefix"], identity["output_prefix"])
            self.assertEqual(
                workflow.workflow_json["5"]["inputs"]["noise_seed"],
                identity["seed"],
            )
            self.assertEqual(
                workflow.workflow_json["2"]["inputs"]["filename_prefix"],
                identity["output_prefix"],
            )
            self.assertIn(str(run.id), identity["output_prefix"])
            self.assertIn(identity["prompt_artifact_id"], identity["output_prefix"])

    def test_explicit_media_route_never_drifts_after_definitive_failure(self):
        second_version, second_deployment = self._media_route("fallback")
        self._use_ordered_routes(second_version, second_deployment)
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run(
                "comfy-explicit-route-no-fallback",
                workflow_route_index=0,
            )
        run = ProductionRun.objects.get(pk=created.data["id"])
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            authorized = self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "1.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                },
                format="json",
            )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        stage = run.stages.get(stage_key="media_generation")
        first = stage.generation_attempts.get(attempt_number=1)
        first_task = first.generation_task
        first_task.status = GenerationTask.Status.FAILED
        first_task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        first_task.outputs = []
        first_task.error_message = "remote terminal failure"
        first_task.save(update_fields=(
            "status", "submission_state", "outputs", "error_message", "updated_at",
        ))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            continued = self.client.post(
                f"/api/v1/production-runs/{run.id}/continue/", {}, format="json",
            )
        self.assertEqual(continued.status_code, 200, continued.data)
        attempts = list(stage.generation_attempts.order_by("attempt_number"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(first.route_index, 0)
        self.assertFalse(stage.generation_attempts.filter(
            model_deployment=second_deployment,
        ).exists())
        self.assertEqual(continued.data["status"], ProductionRun.Status.BLOCKED)
        self.assertEqual(continued.data["blocked_code"], "generation_tasks_failed")

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            replay = self.client.post(
                f"/api/v1/production-runs/{run.id}/continue/", {}, format="json",
            )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(stage.generation_attempts.count(), 1)

    def test_comfy_uncertain_submission_never_falls_back(self):
        second_version, second_deployment = self._media_route("uncertain")
        self._use_ordered_routes(second_version, second_deployment)
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run(
                "comfy-uncertain-no-fallback",
                workflow_route_index=0,
            )
        run = ProductionRun.objects.get(pk=created.data["id"])
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "1.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                },
                format="json",
            )
        stage = run.stages.get(stage_key="media_generation")
        first = stage.generation_attempts.get()
        task = first.generation_task
        task.status = GenerationTask.Status.FAILED
        task.submission_state = GenerationTask.SubmissionState.PREPARED
        task.submission_token = "persisted-submission-token"
        task.outputs = []
        task.save(update_fields=(
            "status", "submission_state", "submission_token", "outputs", "updated_at",
        ))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            response = self.client.post(
                f"/api/v1/production-runs/{run.id}/continue/", {}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], ProductionRun.Status.BLOCKED)
        self.assertEqual(stage.generation_attempts.count(), 1)
        first.refresh_from_db()
        definitive, code = _definitive_media_failure(first)
        self.assertFalse(definitive)
        self.assertEqual(code, "comfy_submission_state_uncertain")
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            retry = self.client.post(
                f"/api/v1/production-runs/{run.id}/retry/", {}, format="json",
            )
        self.assertEqual(retry.status_code, 409, retry.data)
        self.assertEqual(stage.generation_attempts.count(), 1)

    def test_explicit_media_route_failure_never_reprices_another_route(self):
        second_version, second_deployment = self._media_route("budget")
        self._use_ordered_routes(second_version, second_deployment)
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run(
                "comfy-no-reprice-fallback",
                workflow_route_index=0,
            )
        run = ProductionRun.objects.get(pk=created.data["id"])
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "0.4000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                },
                format="json",
            )
        stage = run.stages.get(stage_key="media_generation")
        first = stage.generation_attempts.get()
        task = first.generation_task
        task.status = GenerationTask.Status.FAILED
        task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        task.outputs = []
        task.save(update_fields=("status", "submission_state", "outputs", "updated_at"))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            response = self.client.post(
                f"/api/v1/production-runs/{run.id}/continue/", {}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], ProductionRun.Status.BLOCKED)
        self.assertEqual(response.data["blocked_code"], "generation_tasks_failed")
        self.assertEqual(stage.generation_attempts.count(), 1)
        self.assertEqual(GenerationTask.objects.filter(project=self.project).count(), 1)

    def test_language_block_materializes_no_authoritative_prompt_and_retry_keeps_frozen_policy(self):
        blocked = self._create_run("language-gate")
        self.assertEqual(blocked.status_code, 201, blocked.data)
        self.assertEqual(blocked.data["status"], ProductionRun.Status.BLOCKED)
        self.assertEqual(blocked.data["blocked_code"], "prompt_language_gate_blocked")
        run = ProductionRun.objects.get(pk=blocked.data["id"])
        self.assertFalse(PromptArtifact.objects.filter(project=self.project).exists())
        self.assertFalse(GenerationTask.objects.filter(project=self.project).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
            status=GeneratedArtifact.Status.SELECTED,
        ).exists())

        self._publish_language_gate("warn")
        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/", {}, format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(retried.data["blocked_code"], "prompt_language_gate_blocked")
        self.assertFalse(PromptArtifact.objects.filter(project=self.project).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
            status=GeneratedArtifact.Status.SELECTED,
        ).exists())
        self.assertFalse(GenerationTask.objects.filter(project=self.project).exists())

    def test_prompt_compile_retry_rejects_historical_manifest_and_recompiles(self):
        second_shot = Shot.objects.create(
            episode=self.shot.episode,
            number=2,
            title="站台远端",
            narrative="另一盏路灯在雨中闪烁。",
            media_type=Shot.MediaType.IMAGE,
        )
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("historical-blocked-prompt-recompile")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        prompt_stage = run.stages.get(stage_key="prompt_compile")
        media_stage = run.stages.get(stage_key="media_generation")
        self.assertFalse(media_stage.generation_attempts.exists())
        old_prompts = list(PromptArtifact.objects.filter(project=self.project))
        old_manifests = list(GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
            status=GeneratedArtifact.Status.SELECTED,
        ))
        self.assertEqual(len(old_prompts), 2)
        self.assertEqual(len(old_manifests), 2)
        old_manifest_by_group = {item.variant_group: item for item in old_manifests}

        prompt_stage.status = StageRun.Status.BLOCKED
        prompt_stage.reason_code = "prompt_language_gate_blocked"
        prompt_stage.reason = "historical localized label leaked into the model prompt"
        prompt_stage.save(update_fields=(
            "status", "reason_code", "reason", "updated_at",
        ))
        run.status = ProductionRun.Status.BLOCKED
        run.current_stage_key = "prompt_compile"
        run.blocked_code = prompt_stage.reason_code
        run.blocked_reason = prompt_stage.reason
        run.save(update_fields=(
            "status", "current_stage_key", "blocked_code", "blocked_reason", "updated_at",
        ))
        self.shot.narrative = "A detective waits beneath an isolated platform light."
        self.shot.save(update_fields=("narrative", "updated_at"))
        second_shot.narrative = "A second lamp flickers at the far end of the rainy platform."
        second_shot.save(update_fields=("narrative", "updated_at"))

        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/", {}, format="json",
        )

        self.assertEqual(retried.status_code, 200, retried.data)
        prompt_stage.refresh_from_db()
        self.assertEqual(prompt_stage.status, StageRun.Status.SUCCEEDED)
        self.assertEqual(
            GeneratedArtifact.objects.filter(
                pk__in=[item.pk for item in old_manifests],
                status=GeneratedArtifact.Status.REJECTED,
            ).count(),
            2,
        )
        selected = list(GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
            role="compiled_generation_prompt",
            status=GeneratedArtifact.Status.SELECTED,
        ))
        self.assertEqual(len(selected), 2)
        self.assertEqual(
            {item.variant_group for item in selected},
            {f"prompt:{self.shot.id}", f"prompt:{second_shot.id}"},
        )
        for manifest in selected:
            previous = old_manifest_by_group[manifest.variant_group]
            self.assertNotEqual(manifest.id, previous.id)
            self.assertEqual(manifest.variant_index, previous.variant_index + 1)
        authoritative = _prompt_rows_for_run(run)
        self.assertEqual(len(authoritative), 2)
        self.assertTrue(
            set(item.id for item in authoritative).isdisjoint(
                item.id for item in old_prompts
            ),
        )
        self.assertEqual(
            {str(item.id) for item in authoritative},
            {item.manifest_metadata["prompt_artifact_id"] for item in selected},
        )
        prompt_text = "\n".join(item.prompt_text for item in authoritative)
        self.assertIn("isolated platform light", prompt_text)
        self.assertIn("second lamp flickers", prompt_text)
        self.assertEqual(PromptArtifact.objects.filter(project=self.project).count(), 4)
        self.assertFalse(media_stage.generation_attempts.exists())

    def test_prompt_rows_for_run_ignores_rejected_audit_manifests(self):
        self._publish_language_gate("warn")
        created = self._create_run("selected-prompt-manifest-only")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        selected = GeneratedArtifact.objects.get(
            production_run=run,
            kind=GeneratedArtifact.Kind.PROMPT,
            status=GeneratedArtifact.Status.SELECTED,
        )
        metadata = dict(selected.manifest_metadata)
        contract = json_contract(metadata)
        GeneratedArtifact.objects.create(
            manifest_key=f"run:{run.id}:prompt-rejected-audit",
            production_run=run,
            stage_run=selected.stage_run,
            kind=GeneratedArtifact.Kind.PROMPT,
            role="compiled_generation_prompt",
            variant_group="prompt:rejected-audit",
            variant_index=1,
            status=GeneratedArtifact.Status.REJECTED,
            storage_backend="database",
            object_key=selected.object_key,
            mime_type=selected.mime_type,
            size_bytes=selected.size_bytes,
            sha256=selected.sha256,
            manifest_metadata=metadata,
            manifest_schema_version=contract.schema_version,
            manifest_metadata_size_bytes=contract.size_bytes,
            manifest_metadata_sha256=contract.sha256,
        )

        authoritative = _prompt_rows_for_run(run)

        self.assertEqual([item.id for item in authoritative], [
            PromptArtifact.objects.get(project=self.project).id,
        ])

    def test_media_retry_uses_frozen_language_policy_when_live_config_changes(self):
        self._publish_language_gate("warn")
        created = self._create_run("retry-language-gate")
        run = ProductionRun.objects.get(pk=created.data["id"])
        self.client.post(
            f"/api/v1/production-runs/{run.id}/authorize-cost/",
            {
                "stage_key": "media_generation",
                "allow_external_calls": True,
                "budget_limit": "1.0000",
                "approved_estimated_cost": "0.250000",
                "currency": "USD",
            },
            format="json",
        )
        prompt_sha256 = PromptArtifact.objects.get(project=self.project).content_hash
        task = GenerationTask.objects.get(project=self.project)
        task.status = GenerationTask.Status.FAILED
        task.error_message = "remote failed"
        task.outputs = []
        task.save(update_fields=("status", "error_message", "outputs", "updated_at"))
        stage = run.stages.get(stage_key="media_generation")
        stage.status = StageRun.Status.FAILED
        stage.reason_code = "generation_failed"
        stage.save(update_fields=("status", "reason_code", "updated_at"))
        run.status = ProductionRun.Status.FAILED
        run.current_stage_key = "media_generation"
        run.save(update_fields=("status", "current_stage_key", "updated_at"))

        self._publish_language_gate("block")

        response = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/", {}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.data["blocked_code"], "prompt_language_gate_blocked")
        self.assertEqual(
            PromptArtifact.objects.get(project=self.project).content_hash,
            prompt_sha256,
        )
        self.assertEqual(GenerationTask.objects.filter(project=self.project).count(), 2)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=stage).count(), 2)

    def test_frozen_route_survives_new_current_version_and_non_secret_config_change(self):
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("frozen-route-resume")
        run = ProductionRun.objects.get(pk=created.data["id"])
        stage = run.stages.get(stage_key="media_generation")
        route = stage.routing["routes"][0]
        prompt = PromptArtifact.objects.get(project=self.project)

        self.workflow_version.is_current = False
        self.workflow_version.save(update_fields=("is_current", "updated_at"))
        provider = self.deployment.profile_version.provider
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://approved.example",
            "request_timeout_seconds": 91,
        })
        provider.save(update_fields=(
            "driver_config", "driver_config_schema_version",
            "driver_config_size_bytes", "driver_config_sha256", "updated_at",
        ))
        self.deployment.name = "renamed after run creation"
        assign_json_contract(self.deployment, "runtime_config", {"poll_interval_seconds": 3})
        self.deployment.save(update_fields=(
            "name", "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))

        version, deployment = _load_frozen_media_route(route, prompt=prompt)
        compiled = compile_frozen_workflow(
            workflow_snapshot=route["workflow_snapshot"],
            prompt=prompt,
            project=prompt.project,
        )
        self.assertEqual(deployment.id, self.deployment.id)
        self.assertEqual(compiled.graph["1"]["inputs"]["text"], prompt.prompt_text)
        self.assertEqual(compiled.workflow_contract_sha256, version.execution_contract_sha256)
        self.assertNotEqual(provider.driver_config_sha256, route["provider_config_record_sha256"])

    def test_frozen_semantic_task_ignores_live_template_lifecycle_and_protects_snapshot(self):
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("frozen-semantic-lifecycle")
        run = ProductionRun.objects.get(pk=created.data["id"])
        self.workflow_version.template.is_active = False
        self.workflow_version.template.save(update_fields=("is_active", "updated_at"))
        self.workflow_version.status = WorkflowTemplateVersion.Status.RETIRED
        self.workflow_version.save(update_fields=("status", "updated_at"))

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            authorized = self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "1.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                    "comment": "frozen lifecycle test",
                },
                format="json",
            )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        task = GenerationTask.objects.get(project=self.project)
        prepared = prepare_workflow(task, Mock())
        self.assertEqual(prepared, task.generated_workflow.workflow_json)

        with transaction.atomic(), self.assertRaises(WorkflowBuildError):
            materialize_generation_task(
                user=self.user,
                workflow=task.generated_workflow,
                priority=0,
                max_attempts=1,
                capability_snapshot_overrides={"compiler_id": "forged.compiler"},
            )

    @override_settings(TASK_MAX_CONCURRENT=4)
    def test_local_host_capacity_and_generic_resolution_are_frozen_and_worker_verified(self):
        host = RuntimeHost.objects.create(
            key="media-capacity-local",
            name="Media capacity local",
            status=RuntimeHost.Status.ACTIVE,
        )
        self.deployment.runtime_host = host
        self.deployment.save(update_fields=("runtime_host", "updated_at"))
        details_contract = json_contract({
            "schema_version": 1,
            "gpus": [{"vram_total_bytes": 24 * 1024**3}],
            "comfyui": {"status": "ready"},
            "probe_errors": [],
        })
        captured_at = timezone.now()
        host_snapshot = HostCapabilitySnapshot.objects.create(
            host=host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            gpu_count=1,
            details=details_contract.value,
            details_schema_version=details_contract.schema_version,
            details_size_bytes=details_contract.size_bytes,
            details_sha256=details_contract.sha256,
            captured_at=captured_at,
            expires_at=captured_at + timedelta(minutes=10),
        )
        artifact = self.deployment.profile_version.artifacts.get(
            logical_name="primary-checkpoint",
        )
        RuntimeModelAvailability.objects.create(
            snapshot=host_snapshot,
            deployment=self.deployment,
            artifact=artifact,
            availability_key=f"deployment:{self.deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            reason="verified in controlled local filesystem",
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=RuntimeModelAvailability.ObservationSource.REMOTE_ATTESTATION,
        )
        ModelArtifact.objects.create(
            profile_version=self.deployment.profile_version,
            role=ModelArtifact.Role.LORA,
            logical_name="optional-unused-lora",
            filename="optional-unused.safetensors",
            status=ModelArtifact.Status.DECLARED,
        )
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.default_concurrency": 7,
                "production.default_resolution_width": 640,
                "production.default_resolution_height": 960,
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            created = self._create_run("local-host-capacity")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        with patch(
            "studio.services.production_runs.evaluate_prompt_language_gate",
            return_value=self._gate_report(blocked=False),
        ):
            authorized = self.client.post(
                f"/api/v1/production-runs/{run.id}/authorize-cost/",
                {
                    "stage_key": "media_generation",
                    "allow_external_calls": True,
                    "budget_limit": "1.0000",
                    "approved_estimated_cost": "0.250000",
                    "currency": "USD",
                },
                format="json",
            )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        task = GenerationTask.objects.get(project=self.project)
        capacity = task.capability_snapshot["execution_capacity"]
        self.assertEqual(capacity["schema_version"], 3)
        self.assertEqual(capacity["configured_concurrency"], 7)
        self.assertEqual(capacity["host_compatible_concurrency"], 2)
        self.assertEqual(capacity["hard_concurrency_ceiling_at_freeze"], 4)
        self.assertEqual(capacity["effective_concurrency"], 2)
        self.assertEqual(capacity["runtime_host_id"], str(host.id))
        self.assertEqual(
            capacity["host_capability_snapshot_id"],
            str(host_snapshot.id),
        )
        self.assertEqual(
            task.generated_workflow.workflow_json["3"]["inputs"]["width"],
            640,
        )
        self.assertEqual(
            task.generated_workflow.workflow_json["3"]["inputs"]["height"],
            960,
        )

        HostCapabilitySnapshot.objects.filter(pk=host_snapshot.pk).update(
            details_sha256="0" * 64,
        )
        self.assertIsNone(claim_next_task())
        task.refresh_from_db()
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertIn("主机能力快照校验失败", task.error_message)


class PerEpisodeAssemblyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="assembly-run-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="逐集母版")
        self.episodes = []
        for number in (1, 2):
            episode = Episode.objects.create(
                project=self.project,
                number=number,
                title=f"第 {number} 集",
            )
            Shot.objects.create(
                episode=episode,
                number=1,
                narrative="测试镜头",
                media_type=Shot.MediaType.VIDEO,
            )
            self.episodes.append(episode)
        self.recipe = ProductionRecipeVersion.objects.get(
            recipe_key="default-series-production",
            version=1,
        )
        config = json_contract({"schema_version": 1, "values": {}})
        input_snapshot = json_contract({"schema_version": 1})
        self.run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=self.recipe,
            idempotency_key="assembly-run",
            request_sha256="c" * 64,
            max_generation_tasks=10,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=input_snapshot.value,
            input_snapshot_schema_version=input_snapshot.schema_version,
            input_snapshot_size_bytes=input_snapshot.size_bytes,
            input_snapshot_sha256=input_snapshot.sha256,
        )
        routing = json_contract({
            "schema_version": 1,
            "mode": "unconfigured",
            "routes": [],
            "variant_count": 1,
            "evaluator": None,
        })
        decision = json_contract({
            "schema_version": 1,
            "state": "not_selected",
            "selected_route_index": None,
            "failures": [],
        })
        self.stage = StageRun.objects.create(
            production_run=self.run,
            stage_key="assembly",
            ordinal=1,
            mode="existing_pipeline",
            idempotency_key=f"{self.run.id}:assembly",
            routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes,
            routing_sha256=routing.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
        )

    def test_assembly_creates_one_job_per_episode_then_one_bound_bundle(self):
        jobs = []

        def create_job(**kwargs):
            episode = kwargs["episode"]
            job = AssemblyJob.objects.create(
                owner=self.user,
                project=self.project,
                episode=episode,
                scope=AssemblyJob.Scope.EPISODE,
                status=AssemblyJob.Status.SUCCEEDED,
                dry_run=False,
            )
            jobs.append(job)
            return job

        bundle = ReleaseBundle.objects.create(
            owner=self.user,
            project=self.project,
            created_by=self.user,
            production_run=self.run,
            version=1,
            title="逐集交付",
            status=ReleaseBundle.Status.SEALED,
            selection={"shots": [], "episodes": [], "series_assembly_id": None},
        )
        def record_master(job, *, production_run, stage_run):
            metadata = json_contract({"schema_version": 1, "assembly_job_id": str(job.id)})
            return GeneratedArtifact.objects.create(
                manifest_key=f"assembly:{job.id}",
                production_run=production_run,
                stage_run=stage_run,
                kind=GeneratedArtifact.Kind.ASSEMBLY,
                role="episode_master",
                variant_group=f"episode_master:{job.episode_id}",
                variant_index=1,
                status=GeneratedArtifact.Status.SELECTED,
                storage_backend="database",
                object_key=f"assembly/{job.id}.mp4",
                mime_type="video/mp4",
                size_bytes=1,
                sha256="a" * 64,
                manifest_metadata=metadata.value,
                manifest_schema_version=metadata.schema_version,
                manifest_metadata_size_bytes=metadata.size_bytes,
                manifest_metadata_sha256=metadata.sha256,
            )

        with (
            patch("studio.services.production_runs.create_assembly", side_effect=create_job) as create_mock,
            patch("studio.services.production_runs.record_assembly_artifact", side_effect=record_master),
        ):
            result = _execute_assembly(self.run, self.stage)

        self.assertEqual(result, "episode-masters:2")
        self.assertEqual(create_mock.call_count, 2)
        self.assertTrue(all(call.kwargs["episode"] is not None for call in create_mock.call_args_list))
        self.assertEqual(self.run.assembly_links.count(), 2)
        self.assertEqual({link.episode_id for link in self.run.assembly_links.all()}, {
            episode.id for episode in self.episodes
        })
        delivery_stage = StageRun.objects.create(
            production_run=self.run,
            stage_key="delivery",
            ordinal=2,
            mode="existing_pipeline",
            idempotency_key=f"{self.run.id}:delivery",
            routing=self.stage.routing,
            routing_schema_version=self.stage.routing_schema_version,
            routing_size_bytes=self.stage.routing_size_bytes,
            routing_sha256=self.stage.routing_sha256,
            routing_decision=self.stage.routing_decision,
            routing_decision_schema_version=self.stage.routing_decision_schema_version,
            routing_decision_size_bytes=self.stage.routing_decision_size_bytes,
            routing_decision_sha256=self.stage.routing_decision_sha256,
        )
        with patch(
            "studio.services.production_runs._seal_run_release_bundle",
            return_value=bundle,
        ) as seal_mock:
            self.assertEqual(
                _execute_delivery(self.run, delivery_stage),
                f"release-bundle:{bundle.id}",
            )
        seal_mock.assert_called_once()

    def test_each_variant_group_requires_its_own_immutable_selection_decision(self):
        routing = json_contract({
            "schema_version": 1,
            "mode": "fixed",
            "routes": [{"index": 0}],
            "variant_count": 2,
            "evaluator": None,
        })
        decision = json_contract({"schema_version": 1, "state": "not_selected"})
        stage = StageRun.objects.create(
            production_run=self.run,
            stage_key="media_generation",
            ordinal=2,
            mode="existing_pipeline",
            status=StageRun.Status.SUCCEEDED,
            idempotency_key=f"{self.run.id}:media_generation",
            routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes,
            routing_sha256=routing.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
        )

        def artifact(group, index):
            metadata = json_contract({"schema_version": 1, "group": group})
            return GeneratedArtifact.objects.create(
                manifest_key=f"selection:{group}:{index}",
                production_run=self.run,
                stage_run=stage,
                kind=GeneratedArtifact.Kind.IMAGE,
                role="primary_generation_output",
                variant_group=group,
                variant_index=index,
                status=(
                    GeneratedArtifact.Status.SELECTED
                    if index == 1 else GeneratedArtifact.Status.AVAILABLE
                ),
                storage_backend="managed_local",
                object_key=f"{group}/{index}.png",
                mime_type="image/png",
                size_bytes=8,
                sha256=str(index) * 64,
                manifest_metadata=metadata.value,
                manifest_schema_version=metadata.schema_version,
                manifest_metadata_size_bytes=metadata.size_bytes,
                manifest_metadata_sha256=metadata.sha256,
            )

        group_a = [artifact("shot-a", index) for index in (1, 2)]
        group_b = [artifact("shot-b", index) for index in (1, 2)]
        selection_stage = StageRun.objects.create(
            production_run=self.run,
            stage_key="artifact_selection",
            ordinal=3,
            mode="review",
            idempotency_key=f"{self.run.id}:artifact_selection",
            routing=decision.value,
            routing_schema_version=decision.schema_version,
            routing_size_bytes=decision.size_bytes,
            routing_sha256=decision.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
        )
        with self.assertRaisesMessage(Exception, "每个产物组"):
            _assert_stage_output_integrity(self.run, selection_stage)

        select_run_artifact(self.run, group_a[1], user=self.user)
        with self.assertRaisesMessage(Exception, "每个产物组"):
            _assert_stage_output_integrity(self.run, selection_stage)

        select_run_artifact(self.run, group_b[1], user=self.user)
        _assert_stage_output_integrity(self.run, selection_stage)
        self.assertEqual(stage.artifact_selection_decisions.count(), 2)
        first = stage.artifact_selection_decisions.get(variant_group="shot-a")
        self.assertEqual(first.artifact_id, group_a[1].id)
