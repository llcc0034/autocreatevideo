from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    RecommendationCandidate,
    RecommendationDecision,
    RuntimeHost,
    RuntimeModelAvailability,
    SeriesProject,
    StageRun,
    WorkerLease,
    WorkflowTemplate,
    WorkflowModelRequirement,
    WorkflowTemplateVersion,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    _validate_activation_value,
    activate_config_revision,
    create_config_revision,
    publish_config_revision,
    resolve_configuration,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.production_runs import _freeze_stage_config
from studio.services.runtime_platform import (
    RecommendationStale,
    _gpu_probe,
    capability_readiness,
    decide_recommendation,
    probe_runtime_host,
    recommend_for_host,
)
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.platform_serializers import RecommendationDecisionSerializer
from studio.management.commands.probe_runtime_hosts import (
    _claim_host,
    _release_host_lease,
    _renew_host_lease,
)
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring


class ConfigurationLayeringTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="config-layer-owner", password="StrongPass123!",
        )
        self.other = get_user_model().objects.create_user(
            username="config-layer-other", password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=self.user, title="Config layers", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)

    def _definition(
        self,
        key: str,
        *,
        restart_required: bool = False,
        allowed_scopes: str = "system,project,run",
        schema: dict | None = None,
    ) -> ConfigDefinition:
        definition = ConfigDefinition(
            key=key,
            domain="test",
            value_type=ConfigDefinition.ValueType.INTEGER if schema is None else ConfigDefinition.ValueType.STRING,
            allowed_scopes=allowed_scopes,
            allowed_default_sources="model,workflow" if not restart_required else "",
            restart_required=restart_required,
            value_schema=schema or {"minimum": 0, "maximum": 100},
        )
        assign_json_contract(definition, "value_schema", definition.value_schema)
        definition.save()
        return definition

    def test_queue_recommendation_definitions_are_explicitly_non_blocking(self):
        descriptions = dict(
            ConfigDefinition.objects.filter(key__in=(
                "production.recommended_max_pending",
                "production.recommended_max_active_per_user",
            )).values_list("key", "description")
        )
        self.assertEqual(len(descriptions), 2)
        for description in descriptions.values():
            self.assertIn("仅生成告警并继续", description)
            self.assertIn("不作为生产阻断条件", description)

    def _model_and_workflow(self, key: str):
        provider = ModelProfileVersion.objects.get(profile_key="deepseek-text", version=1).provider
        model = ModelProfileVersion(
            profile_key=f"{key}-model",
            version=1,
            provider=provider,
            model_identifier=f"{key}-model-id",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "config_defaults": {key: 10},
        })
        model.save()
        template = WorkflowTemplate.objects.create(
            slug=f"{key.replace('.', '-')}-workflow",
            version=1,
            name="Layer workflow",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        workflow = WorkflowTemplateVersion(
            template=template,
            version=1,
            model_profile_version=model,
        )
        assign_json_contract(workflow, "graph", {})
        assign_json_contract(workflow, "input_contract", {
            "schema_version": 1,
            "config_defaults": {key: 20},
        })
        workflow.save()
        return model, workflow

    def _run(self, snapshot: dict) -> ProductionRun:
        recipe = ProductionRecipeVersion.objects.filter(status="published").first()
        if recipe is None:
            recipe = ProductionRecipeVersion(
                recipe_key="test-layer-recipe",
                version=1,
                name="Test layer recipe",
                status=ProductionRecipeVersion.Status.PUBLISHED,
            )
            assign_json_contract(recipe, "definition", {
                "schema_version": 1,
                "stages": [{"key": "script_input", "mode": "confirmed_revision"}],
            })
            recipe.save()
        contract = json_contract(
            snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
        )
        input_contract = json_contract({"schema_version": 1})
        return ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=recipe,
            idempotency_key="configuration-layer-test",
            request_sha256="1" * 64,
            config_snapshot=contract.value,
            config_snapshot_schema_version=contract.schema_version,
            config_snapshot_size_bytes=contract.size_bytes,
            config_snapshot_sha256=contract.sha256,
            input_snapshot=input_contract.value,
            input_snapshot_schema_version=input_contract.schema_version,
            input_snapshot_size_bytes=input_contract.size_bytes,
            input_snapshot_sha256=input_contract.sha256,
        )

    def test_five_layers_report_source_and_stage_snapshot_stays_frozen(self):
        key = "test.layered.value"
        self._definition(key)
        model, workflow = self._model_and_workflow(key)

        workflow_only = resolve_configuration(
            model_profile_version=model,
            workflow_version=workflow,
        )
        self.assertEqual(workflow_only.values[key], 20)
        self.assertEqual(workflow_only.sources[key]["source"], "workflow")
        self.assertEqual(workflow_only.sources[key]["id"], str(workflow.id))
        self.assertEqual(workflow_only.sources[key]["version"], 1)
        self.assertEqual(len(workflow_only.sources[key]["hash"]), 64)

        system = create_config_revision(
            scope_type="system", values={key: 30}, created_by=self.user,
        )
        publish_config_revision(system, published_by=self.user)
        project_revision = create_config_revision(
            scope_type="project", project=self.project, values={key: 40}, created_by=self.user,
        )
        publish_config_revision(project_revision, published_by=self.user)
        base = resolve_configuration(project=self.project)
        run = self._run(base.snapshot())
        run_revision = create_config_revision(
            scope_type="run", run=run, values={key: 50}, created_by=self.user,
        )
        publish_config_revision(run_revision, published_by=self.user)
        effective = resolve_configuration(
            run=run, model_profile_version=model, workflow_version=workflow,
        )
        self.assertEqual(effective.values[key], 50)
        self.assertEqual(effective.sources[key]["source"], "run")

        deployment = ModelDeployment(
            key="layered-text-deployment",
            name="Layered text deployment",
            profile_version=model,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        run_snapshot = effective.snapshot()
        run_contract = json_contract(
            run_snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
        )
        run.config_snapshot = run_snapshot
        run.config_snapshot_schema_version = run_contract.schema_version
        run.config_snapshot_size_bytes = run_contract.size_bytes
        run.config_snapshot_sha256 = run_contract.sha256
        run.save(update_fields=(
            "config_snapshot", "config_snapshot_schema_version",
            "config_snapshot_size_bytes", "config_snapshot_sha256", "updated_at",
        ))
        routing = json_contract({
            "schema_version": 1,
            "mode": "fixed",
            "routes": [{
                "index": 0,
                "model_deployment_id": str(deployment.id),
                "workflow_version_id": str(workflow.id),
            }],
            "variant_count": 1,
            "evaluator": None,
        })
        decision = json_contract({"schema_version": 1, "state": "selected"})
        stage = StageRun.objects.create(
            production_run=run,
            stage_key="layered",
            ordinal=1,
            mode="ai",
            idempotency_key=f"{run.id}:layered",
            routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes,
            routing_sha256=routing.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
            selected_route_index=0,
        )
        _freeze_stage_config(run, stage)
        frozen_sha = stage.config_snapshot_sha256
        frozen_value = stage.config_snapshot["routes"]["0"]["values"][key]

        newer = create_config_revision(
            scope_type="project", project=self.project, values={key: 60}, created_by=self.user,
        )
        publish_config_revision(newer, published_by=self.user)
        _freeze_stage_config(run, stage)
        stage.refresh_from_db()
        self.assertEqual(stage.config_snapshot_sha256, frozen_sha)
        self.assertEqual(stage.config_snapshot["routes"]["0"]["values"][key], frozen_value)

    def test_project_put_patch_unset_empty_replace_and_owner_permission(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = f"/api/v1/projects/{self.project.id}/config/"
        put = client.put(url, {"values": {"production.default_multi_shot": True}}, format="json")
        self.assertEqual(put.status_code, 200, put.data)
        patch_response = client.patch(
            url,
            {"values": {"production.task_max_attempts": 4}},
            format="json",
        )
        self.assertEqual(patch_response.status_code, 200, patch_response.data)
        unset = client.patch(
            url,
            {"unset_keys": ["production.default_multi_shot"]},
            format="json",
        )
        self.assertEqual(unset.status_code, 200, unset.data)
        self.assertEqual(unset.data["effective"]["values"]["production.default_multi_shot"], False)
        cleared = client.put(url, {"values": {}}, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.data)
        latest = ConfigRevision.objects.filter(project=self.project, activation_state="active").get()
        self.assertEqual(latest.values.count(), 0)

        client.force_authenticate(self.other)
        self.assertEqual(client.get(url).status_code, 404)

    def test_restart_required_publish_waits_for_explicit_activation(self):
        key = "test.runtime.relative_path"
        self._definition(
            key,
            restart_required=True,
            allowed_scopes="system",
            schema={"format": "safe-relative-path", "minLength": 1},
        )
        old_active = ConfigRevision.objects.get(scope_type="system", activation_state="active")
        revision = create_config_revision(
            scope_type="system", values={key: "runtime/cache"}, created_by=self.user,
        )
        with override_settings(AUTOPROMPT_BOOT_ID="boot-a"):
            publish_config_revision(revision, published_by=self.user)
        revision.refresh_from_db()
        old_active.refresh_from_db()
        self.assertEqual(revision.activation_state, "pending_activation")
        self.assertEqual(old_active.activation_state, "active")
        self.assertNotIn(key, resolve_configuration().values)

        with override_settings(AUTOPROMPT_BOOT_ID="boot-a"):
            with self.assertRaises(ValidationError):
                activate_config_revision(revision, activated_by=self.user)
        revision.refresh_from_db()
        self.assertEqual(revision.activation_state, "pending_activation")
        with override_settings(AUTOPROMPT_BOOT_ID="boot-b"):
            activate_config_revision(revision, activated_by=self.user)
        revision.refresh_from_db()
        old_active.refresh_from_db()
        self.assertEqual(revision.activation_state, "active")
        self.assertEqual(old_active.activation_state, "inactive")
        self.assertEqual(resolve_configuration().values[key], "runtime/cache")

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(
            f"/api/v1/projects/{self.project.id}/config/",
            {"values": {key: "other"}},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_stale_draft_publish_returns_conflict_and_keeps_current_active(self):
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        first = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"production.default_multi_shot": True},
            created_by=self.user,
        )
        second = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"production.default_multi_shot": False},
            created_by=self.user,
        )
        publish_config_revision(second, published_by=self.user)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            f"/api/v1/config-revisions/{first.id}/publish/", {}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "stale_config_base")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, ConfigRevision.Status.DRAFT)
        self.assertEqual(second.activation_state, ConfigRevision.ActivationState.ACTIVE)
        self.assertEqual(ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).count(), 1)

    def test_second_pending_revision_is_rejected(self):
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        key = "test.runtime.pending_path"
        self._definition(
            key, restart_required=True, allowed_scopes="system",
            schema={"format": "safe-relative-path", "minLength": 1},
        )
        first = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={key: "runtime/first"}, created_by=self.user,
        )
        second = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={key: "runtime/second"}, created_by=self.user,
        )
        with override_settings(AUTOPROMPT_BOOT_ID="pending-boot-a"):
            publish_config_revision(first, published_by=self.user)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            f"/api/v1/config-revisions/{second.id}/publish/", {}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "config_activation_pending")
        second.refresh_from_db()
        self.assertEqual(second.status, ConfigRevision.Status.DRAFT)
        self.assertEqual(ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            activation_state=ConfigRevision.ActivationState.PENDING,
        ).count(), 1)

    def test_complete_system_publish_clears_only_0046_review_markers(self):
        migrated = ConfigDefinition.objects.get(key="production.default_concurrency")
        migrated.needs_review = True
        migrated.review_reason = "0046 reviewed legacy runtime value"
        migrated.save(update_fields=("needs_review", "review_reason", "updated_at"))
        unrelated = ConfigDefinition.objects.get(
            key="production.default_resolution_width",
        )
        unrelated.needs_review = True
        unrelated.review_reason = "manual license review"
        unrelated.save(update_fields=("needs_review", "review_reason", "updated_at"))

        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={},
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

        migrated.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertFalse(migrated.needs_review)
        self.assertEqual(migrated.review_reason, "")
        self.assertTrue(unrelated.needs_review)
        self.assertEqual(unrelated.review_reason, "manual license review")

    def test_pending_system_activation_fails_closed_if_new_required_key_is_missing(self):
        restart = self._definition(
            "test.runtime.required_path",
            restart_required=True,
            allowed_scopes="system",
            schema={"format": "safe-relative-path", "minLength": 1},
        )
        restart.is_required = True
        restart.save(update_fields=("is_required", "updated_at"))
        old_active = ConfigRevision.objects.get(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        )
        candidate = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={restart.key: "runtime/required"},
            created_by=self.user,
        )
        with override_settings(AUTOPROMPT_BOOT_ID="required-boot-a"):
            publish_config_revision(candidate, published_by=self.user)

        late = self._definition(
            "test.runtime.late_required",
            allowed_scopes="system",
        )
        late.is_required = True
        late.save(update_fields=("is_required", "updated_at"))
        with override_settings(AUTOPROMPT_BOOT_ID="required-boot-b"):
            with self.assertRaisesMessage(ValidationError, late.key):
                activate_config_revision(candidate, activated_by=self.user)

        candidate.refresh_from_db()
        old_active.refresh_from_db()
        self.assertEqual(
            candidate.activation_state,
            ConfigRevision.ActivationState.FAILED,
        )
        self.assertEqual(
            old_active.activation_state,
            ConfigRevision.ActivationState.ACTIVE,
        )
        with self.assertRaises(ConfigurationResolutionError):
            resolve_configuration()

    def test_0046_review_marker_clears_only_after_pending_revision_activates(self):
        definition = ConfigDefinition.objects.get(key="production.default_concurrency")
        definition.needs_review = True
        definition.review_reason = "0046 pending restart review"
        definition.save(update_fields=("needs_review", "review_reason", "updated_at"))
        restart = self._definition(
            "test.runtime.review_path",
            restart_required=True,
            allowed_scopes="system",
            schema={"format": "safe-relative-path", "minLength": 1},
        )
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={restart.key: "runtime/review"},
            created_by=self.user,
        )
        with override_settings(AUTOPROMPT_BOOT_ID="review-boot-a"):
            publish_config_revision(revision, published_by=self.user)
        definition.refresh_from_db()
        self.assertTrue(definition.needs_review)

        with override_settings(AUTOPROMPT_BOOT_ID="review-boot-b"):
            activate_config_revision(revision, activated_by=self.user)
        definition.refresh_from_db()
        self.assertFalse(definition.needs_review)
        self.assertEqual(definition.review_reason, "")

    def test_restart_paths_reject_windows_drive_relative_and_unc_values(self):
        definition = self._definition(
            "test.runtime.safe_path",
            restart_required=True,
            allowed_scopes="system",
            schema={"format": "safe-relative-path", "minLength": 1},
        )
        for unsafe in (r"C:relative\cache", r"\\server\share\cache", r"..\outside"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValidationError):
                _validate_activation_value(definition, unsafe)

    @override_settings(TASK_MAX_CONCURRENT=2)
    def test_production_capability_uses_project_config_and_labels_hard_limits(self):
        revision = create_config_revision(
            scope_type="project",
            project=self.project,
            values={
                "production.default_concurrency": 7,
                "production.task_timeout_seconds": 2000,
                # Recommendations are independent advisory values; their
                # relative ordering must never make this capability API fail.
                "production.recommended_max_pending": 1,
                "production.recommended_max_active_per_user": 2,
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.get(
            "/api/v1/capabilities/production/",
            {"project_id": str(self.project.id)},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["configured_concurrency"], 7)
        self.assertEqual(response.data["deployment_max_concurrent"], 2)
        self.assertEqual(response.data["max_concurrent"], 2)
        self.assertEqual(response.data["configured_task_timeout_seconds"], 2000)
        self.assertEqual(response.data["task_timeout_seconds"], 2000)
        self.assertEqual(response.data["task_idle_timeout_seconds"], 1800)
        self.assertEqual(response.data["queue_guidance_enforcement"], "advisory")
        self.assertEqual(response.data["queue_advisories"], [])
        self.assertNotIn("confirmation_required", response.data)
        self.assertFalse(response.data["business_volume_limited"])
        self.assertEqual(
            response.data["configuration_sources"]["production.default_concurrency"],
            "project",
        )
        self.assertRegex(response.data["configuration_public_sha256"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(response.data["conditioning_modes"], list)
        self.assertRegex(
            response.data["conditioning_contract_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(response.data["default_conditioning_mode"], "")

        client.force_authenticate(self.other)
        denied = client.get(
            "/api/v1/capabilities/production/",
            {"project_id": str(self.project.id)},
        )
        self.assertEqual(denied.status_code, 404)


class HostRecommendationTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.admin = get_user_model().objects.create_superuser(
            username="host-admin", password="StrongPass123!", email="host@example.test",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=self.admin, title="Host recommendation", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        self.host = RuntimeHost.objects.get(key="local")
        self.provider = ModelProfileVersion.objects.get(
            profile_key="sdxl-image", version=1,
        ).provider
        recipe = ProductionRecipeVersion(
            recipe_key="recommendation-base-recipe",
            version=1,
            name="Recommendation base recipe",
            status=ProductionRecipeVersion.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
                {"key": "script_review", "mode": "review", "approval": "required"},
                {"key": "bible_review", "mode": "review", "approval": "required"},
                {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
                {"key": "storyboard_review", "mode": "review", "approval": "required"},
                {"key": "prompt_compile", "mode": "existing_pipeline", "approval": "auto"},
                {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto"},
                {"key": "artifact_selection", "mode": "review", "approval": "required"},
                {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
                {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"},
            ],
        })
        recipe.save()

    def _deployment(self, key: str, *, priority: int = 0, is_default: bool = False):
        profile = ModelProfileVersion(
            profile_key=f"profile-{key}",
            version=1,
            provider=self.provider,
            model_identifier=f"model-{key}",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "operations": ["text_to_image"],
        })
        profile.save()
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name=f"artifact-{key}",
            filename=f"{key}.safetensors",
            sha256="a" * 64,
            size_bytes=1,
            status=ModelArtifact.Status.AVAILABLE,
        )
        template = WorkflowTemplate.objects.create(
            slug=f"workflow-{key}",
            version=1,
            name=f"Workflow {key}",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        workflow = WorkflowTemplateVersion(
            template=template,
            version=1,
            provider=self.provider,
            model_profile_version=profile,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            is_current=True,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
        )
        assign_json_contract(workflow, "graph", {})
        assign_json_contract(workflow, "input_contract", {
            "schema_version": 1,
            "capability": "image.text_to_image",
        })
        workflow.save()
        WorkflowModelRequirement.objects.create(
            workflow_version=workflow,
            role=artifact.role,
            alias="primary-checkpoint",
            artifact=artifact,
            expected_filename=artifact.filename,
            expected_sha256=artifact.sha256,
            is_required=True,
        )
        deployment = ModelDeployment(
            key=key,
            name=key,
            profile_version=profile,
            runtime_host=self.host,
            priority=priority,
            is_default=is_default,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
        })
        deployment.save()
        workflow.validated_deployment = deployment
        workflow.save(update_fields=("validated_deployment", "updated_at"))
        return deployment, artifact, workflow

    def _snapshot(self):
        details = {
            "schema_version": 1,
            "gpus": [{"vram_total_bytes": 24 * 1024**3}],
            "comfyui": {"status": "partial"},
            "probe_errors": [],
        }
        contract = json_contract(details)
        return HostCapabilitySnapshot.objects.create(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.PARTIAL,
            gpu_count=1,
            details=contract.value,
            details_schema_version=contract.schema_version,
            details_size_bytes=contract.size_bytes,
            details_sha256=contract.sha256,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def test_four_candidate_states_and_override_decision_create_drafts(self):
        recommended_deployment, recommended_artifact, _ = self._deployment(
            "rec-a", priority=100, is_default=True,
        )
        compatible_deployment, compatible_artifact, _ = self._deployment("rec-b", priority=50)
        smoke_deployment, smoke_artifact, _ = self._deployment("rec-c", priority=10)
        unavailable_deployment, unavailable_artifact, _ = self._deployment("rec-d", priority=1)
        snapshot = self._snapshot()
        for deployment, artifact, availability_status in (
            (recommended_deployment, recommended_artifact, RuntimeModelAvailability.Status.AVAILABLE),
            (compatible_deployment, compatible_artifact, RuntimeModelAvailability.Status.AVAILABLE),
            (smoke_deployment, smoke_artifact, RuntimeModelAvailability.Status.UNKNOWN),
            (unavailable_deployment, unavailable_artifact, RuntimeModelAvailability.Status.MISSING),
        ):
            RuntimeModelAvailability.objects.create(
                snapshot=snapshot,
                deployment=deployment,
                artifact=artifact,
                availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
                status=availability_status,
                reason=availability_status,
                **({
                    "observed_size_bytes": artifact.size_bytes,
                    "observed_sha256": artifact.sha256,
                    "observation_source": RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
                } if availability_status == RuntimeModelAvailability.Status.AVAILABLE else {}),
            )

        recommendation = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        )
        states = set(recommendation.candidates.values_list("status", flat=True))
        self.assertEqual(states, {
            RecommendationCandidate.Status.RECOMMENDED,
            RecommendationCandidate.Status.COMPATIBLE,
            RecommendationCandidate.Status.SMOKE_TEST_REQUIRED,
            RecommendationCandidate.Status.UNAVAILABLE,
        })
        reason_codes = set(recommendation.candidates.values_list("reason_code", flat=True))
        self.assertIn("recommended_best_match", reason_codes)
        self.assertIn("artifact_inventory_unverified", reason_codes)
        self.assertIn("artifact_missing", reason_codes)

        candidate = recommendation.candidates.get(status=RecommendationCandidate.Status.RECOMMENDED)
        decision = decide_recommendation(
            recommendation,
            candidate=candidate,
            decision=RecommendationDecision.Decision.OVERRIDE,
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            project=None,
            override_values={
                "configuration": {"production.task_max_attempts": 2},
            },
            reason="管理员接受路由并收紧尝试次数",
            decided_by=self.admin,
        )
        self.assertEqual(decision.config_revision.status, ConfigRevision.Status.DRAFT)
        config_values = {
            row.definition.key: row.value
            for row in decision.config_revision.values.select_related("definition")
        }
        for key, value in candidate.suggested_values["configuration"].items():
            self.assertEqual(config_values[key], value)
        self.assertEqual(config_values["production.task_max_attempts"], 2)
        self.assertEqual(decision.recipe_version.status, ProductionRecipeVersion.Status.DRAFT)
        self.assertEqual(
            decision.recipe_version.definition["stages"][6]["routing"]["routes"][0]["model_deployment_id"],
            str(recommended_deployment.id),
        )
        client = APIClient()
        client.force_authenticate(self.admin)
        detail = client.get(f"/api/v1/recommendations/{recommendation.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(len(detail.data["candidates"]), 4)
        unavailable = recommendation.candidates.get(
            status=RecommendationCandidate.Status.UNAVAILABLE,
        )
        rejected = client.post(
            f"/api/v1/recommendations/{recommendation.id}/decide/",
            {
                "candidate_id": str(unavailable.id),
                "decision": "reject",
                "scope_type": "system",
                "override_values": {},
                "reason": "管理员拒绝不可用候选",
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 201, rejected.data)

        assign_json_contract(decision, "override_values", {"api_token": "must-not-leak"})
        decision.save(update_fields=(
            "override_values", "override_values_schema_version",
            "override_values_size_bytes", "override_values_sha256", "updated_at",
        ))
        serialized = RecommendationDecisionSerializer(decision).data
        self.assertNotIn("must-not-leak", str(serialized))
        self.assertTrue(serialized["override_values"]["api_token_configured"])

    def test_recommendation_cache_changes_when_catalog_fingerprint_changes(self):
        deployment, artifact, _workflow = self._deployment("catalog-change", priority=50)
        snapshot = self._snapshot()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            reason="available",
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        first = recommend_for_host(snapshot, project=self.project, created_by=self.admin)
        artifact.filename = "catalog-change-v2.safetensors"
        artifact.save(update_fields=("filename", "updated_at"))
        second = recommend_for_host(snapshot, project=self.project, created_by=self.admin)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.input_sha256, second.input_sha256)

    def test_expired_recommendation_returns_409_without_creating_any_draft(self):
        deployment, artifact, _workflow = self._deployment("stale-expiry", priority=50)
        snapshot = self._snapshot()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            reason="available",
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        recommendation = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        )
        candidate = recommendation.candidates.get(deployment=deployment)
        HostCapabilitySnapshot.objects.filter(pk=snapshot.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        before = {
            "decisions": RecommendationDecision.objects.count(),
            "configs": ConfigRevision.objects.count(),
            "recipes": ProductionRecipeVersion.objects.count(),
        }
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            f"/api/v1/recommendations/{recommendation.id}/decide/",
            {
                "candidate_id": str(candidate.id),
                "decision": RecommendationDecision.Decision.ACCEPT,
                "scope_type": ConfigRevision.ScopeType.SYSTEM,
                "override_values": {},
                "reason": "must revalidate before writing",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "recommendation_stale")
        self.assertEqual(RecommendationDecision.objects.count(), before["decisions"])
        self.assertEqual(ConfigRevision.objects.count(), before["configs"])
        self.assertEqual(ProductionRecipeVersion.objects.count(), before["recipes"])

    def test_catalog_change_rejects_decision_before_writing_drafts(self):
        deployment, artifact, _workflow = self._deployment("stale-catalog", priority=50)
        snapshot = self._snapshot()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            reason="available",
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        recommendation = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        )
        candidate = recommendation.candidates.get(deployment=deployment)
        deployment.priority += 1
        deployment.save(update_fields=("priority", "updated_at"))
        before = (
            RecommendationDecision.objects.count(),
            ConfigRevision.objects.count(),
            ProductionRecipeVersion.objects.count(),
        )

        with self.assertRaises(RecommendationStale):
            decide_recommendation(
                recommendation,
                candidate=candidate,
                decision=RecommendationDecision.Decision.ACCEPT,
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                project=None,
                override_values={},
                reason="catalog drift must fail closed",
                decided_by=self.admin,
            )
        self.assertEqual(
            (
                RecommendationDecision.objects.count(),
                ConfigRevision.objects.count(),
                ProductionRecipeVersion.objects.count(),
            ),
            before,
        )

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
    def test_workflowless_async_video_uses_declared_t2v_capability(self):
        credential = create_test_secret(
            key="recommend.async-video",
            actor=self.admin,
            value="test-secret",
        )
        provider = ProviderProfile(
            key="recommend-async-video",
            name="Recommendation async video",
            driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="video",
            operations="video.text_to_video",
            credential=credential,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://api.example.com",
            "health_path": "/health",
            "submit_path": "/jobs",
            "poll_path": "/jobs/{request_id}",
            "cancel_path": "/jobs/{request_id}/cancel",
            "collect_path": "/jobs/{request_id}",
            "request_id_field": "id",
            "status_field": "status",
            "artifacts_field": "artifacts",
            "artifact_url_field": "url",
            "artifact_kind_field": "kind",
            "artifact_id_field": "id",
            "artifact_sha256_field": "sha256",
            "actual_cost_field": "actual_cost",
            "currency_field": "currency",
            "idempotency_header": "Idempotency-Key",
            "terminal_success": ["succeeded"],
            "terminal_failure": ["failed"],
            "terminal_cancelled": ["cancelled"],
            "artifact_allowed_hosts": ["cdn.example.com"],
            "request_field_map": {
                "model": "model", "prompt": "prompt", "width": "width", "height": "height",
            },
            "timeout_seconds": 30,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
            "pricing": {"currency": "USD", "fixed_per_request": "0.010000"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key="recommend-async-video",
            version=1,
            provider=provider,
            model_identifier="video-cloud-v1",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        operation_contract = {
            "output_kind": "video",
            "request_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "model", "width", "height"],
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "model": {"type": "string", "minLength": 1, "maxLength": 255},
                    "width": {"type": "integer", "minimum": 64, "maximum": 4096},
                    "height": {"type": "integer", "minimum": 64, "maximum": 4096},
                },
            },
            "smoke_inputs": {
                "prompt": "smoke", "model": "video-cloud-v1", "width": 64, "height": 64,
            },
        }
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["video.text_to_video"],
            "async_media": {
                "schema_version": 1,
                "operations": {"video.text_to_video": operation_contract},
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key="recommend-async-video",
            name="Recommendation async video",
            profile_version=profile,
            credential=credential,
            priority=100,
            is_default=True,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        snapshot = self._snapshot()
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["video.text_to_video"],
            evidence={"schema_version": 1, "execution_submitted": True},
        )
        recommendation = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        )

        candidate = recommendation.candidates.get(deployment=deployment)
        self.assertEqual(candidate.capability_key, "video.text_to_video")
        self.assertEqual(candidate.status, RecommendationCandidate.Status.RECOMMENDED)
        self.assertIsNone(candidate.workflow_version_id)

    def test_text_recommendation_requires_credential_and_fresh_health_evidence(self):
        profile = ModelProfileVersion.objects.get(profile_key="deepseek-text", version=1)
        deployment = ModelDeployment(
            key="recommend-text-health",
            name="Recommendation text health",
            profile_version=profile,
            priority=200,
            is_default=True,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {
            "runtime_source": "openai_compatible_endpoint",
            "base_url": "http://127.0.0.1:19001",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 30,
            "capability_ttl_seconds": 3600,
        })
        deployment.save()
        snapshot = self._snapshot()

        missing_credential = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        ).candidates.get(deployment=deployment)
        self.assertEqual(missing_credential.status, RecommendationCandidate.Status.UNAVAILABLE)
        deployment.credential = create_test_secret(
            key="recommend.text-health",
            actor=self.admin,
            value="test-secret",
        )
        deployment.save(update_fields=("credential", "updated_at"))
        without_evidence = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        ).candidates.get(deployment=deployment)
        self.assertEqual(
            without_evidence.status,
            RecommendationCandidate.Status.SMOKE_TEST_REQUIRED,
        )
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["chat_completion"],
            evidence={"schema_version": 1, "execution_submitted": False},
        )
        ready = recommend_for_host(
            snapshot, project=self.project, created_by=self.admin,
        ).candidates.get(deployment=deployment)
        self.assertEqual(ready.status, RecommendationCandidate.Status.RECOMMENDED)

    def test_text_recommendation_rejects_fresh_health_for_incomplete_execution_contract(self):
        profile = ModelProfileVersion.objects.get(profile_key="deepseek-text", version=1)
        deployment = ModelDeployment(
            key="recommend-text-invalid-contract",
            name="Recommendation text invalid contract",
            profile_version=profile,
            credential=create_test_secret(
                key="recommend.text-invalid-contract",
                actor=self.admin,
                value="test-secret",
            ),
            priority=201,
            is_default=True,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {
            "runtime_source": "openai_compatible_endpoint",
            "base_url": "http://127.0.0.1:19002",
            "capability_ttl_seconds": 3600,
        })
        deployment.save()
        fresh_health = record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["chat_completion"],
            evidence={"schema_version": 1, "execution_submitted": False},
        )
        self.assertGreater(fresh_health.expires_at, timezone.now())

        candidate = recommend_for_host(
            self._snapshot(), project=self.project, created_by=self.admin,
        ).candidates.get(deployment=deployment)

        self.assertEqual(candidate.status, RecommendationCandidate.Status.UNAVAILABLE)
        self.assertEqual(
            candidate.reason_code,
            "deployment_execution_contract_invalid",
        )

    def test_capability_readiness_requires_verified_inventory_and_smoke_workflow(self):
        deployment, artifact, workflow = self._deployment("strict-ready", priority=50)
        snapshot = self._snapshot()
        availability = RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.UNKNOWN,
            reason="unknown",
        )
        blocked = capability_readiness(snapshot)
        self.assertEqual(blocked["capabilities"]["image"]["state"], "blocked")

        availability.status = RuntimeModelAvailability.Status.AVAILABLE
        availability.observed_size_bytes = artifact.size_bytes
        availability.observed_sha256 = artifact.sha256
        availability.observation_source = (
            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
        )
        availability.save(update_fields=(
            "status", "observed_size_bytes", "observed_sha256",
            "observation_source", "updated_at",
        ))
        ready = capability_readiness(snapshot)
        self.assertEqual(ready["capabilities"]["image"]["state"], "ready")

        workflow.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        workflow.save(update_fields=("smoke_test_state", "updated_at"))
        smoke = capability_readiness(snapshot)
        self.assertEqual(smoke["capabilities"]["image"]["state"], "smoke_required")

    @patch("studio.platform_views.enqueue_runtime_host_probe")
    def test_runtime_host_get_does_not_trigger_probe(self, enqueue_runtime_host_probe):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(f"/api/v1/runtime-hosts/{self.host.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        enqueue_runtime_host_probe.assert_not_called()

    def test_gpu_probe_timeout_comes_from_published_runtime_configuration(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"runtime.gpu_probe_timeout_seconds": 1.25},
            created_by=self.admin,
        )
        publish_config_revision(revision, published_by=self.admin)
        with patch(
            "studio.services.runtime_platform._gpu_probe",
            return_value=([], "nvidia-smi_not_found"),
        ) as gpu_probe, patch(
            "studio.services.runtime_platform._comfy_probe",
            return_value=(
                {"status": "not_configured", "version": "", "deployments": []},
                [],
                [],
            ),
        ):
            snapshot = probe_runtime_host(self.host)

        gpu_probe.assert_called_once_with(timeout_seconds=1.25)
        self.assertEqual(
            snapshot.details["runtime_configuration"]["gpu_probe_timeout_seconds"],
            1.25,
        )
        self.assertTrue(
            snapshot.details["runtime_configuration"]["resolution_sha256"],
        )

    @patch("studio.services.runtime_platform.shutil.which", return_value="nvidia-smi")
    @patch("studio.services.runtime_platform.subprocess.run")
    def test_gpu_probe_passes_exact_timeout_to_subprocess(self, run, _which):
        run.return_value = Mock(returncode=0, stdout="")

        gpus, error = _gpu_probe(timeout_seconds=1.25)

        self.assertEqual(gpus, [])
        self.assertEqual(error, "nvidia-smi_no_devices")
        self.assertEqual(run.call_args.kwargs["timeout"], 1.25)

    @patch("studio.services.runtime_platform._gpu_probe", return_value=([], "nvidia-smi_not_found"))
    @patch("studio.services.runtime_platform.driver_for")
    def test_multi_deployment_probe_preserves_partial_failure(self, driver_for, _gpu_probe):
        good, good_artifact, _ = self._deployment("probe-good", priority=20)
        bad, _bad_artifact, _ = self._deployment("probe-bad", priority=10)
        driver = Mock()

        def invoke(deployment, operation, payload):
            if deployment.id == bad.id:
                raise RuntimeError("offline")
            if operation == "health":
                return {"system": {"comfyui_version": "test-version"}}
            if operation == "object_info":
                return {"SaveImage": {}}
            if operation == "models":
                artifact = deployment.profile_version.artifacts.first()
                return {
                    "items": [artifact.filename]
                    if payload.get("folder") == "checkpoints" else [],
                }
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        driver_for.return_value = driver
        snapshot = probe_runtime_host(self.host)
        comfy = snapshot.details["comfyui"]
        self.assertEqual(comfy["deployment_count"], 2)
        self.assertEqual(comfy["status"], "partial")
        self.assertEqual(snapshot.probe_status, HostCapabilitySnapshot.ProbeStatus.PARTIAL)
        self.assertEqual(snapshot.model_availability.values("deployment").distinct().count(), 2)
        self.assertIn("comfyui_probe_failed:probe-bad", snapshot.details["probe_errors"])

    @patch("studio.services.runtime_platform._gpu_probe", return_value=([], "nvidia-smi_not_found"))
    @patch("studio.services.runtime_platform.driver_for")
    def test_loopback_probe_streams_exact_artifact_facts_from_controlled_comfy_root(
        self,
        driver_for,
        _gpu_probe,
    ):
        deployment, artifact, _ = self._deployment("probe-observed", priority=30)
        payload = b"verified-model-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        artifact.filename = f"nested/{artifact.filename}"
        artifact.sha256 = digest
        artifact.size_bytes = len(payload)
        artifact.save(update_fields=("filename", "sha256", "size_bytes", "updated_at"))
        driver = Mock()
        driver.invoke.side_effect = lambda current, operation, request: (
            {"system": {"comfyui_version": "test-version"}}
            if operation == "health" else
            {"SaveImage": {}}
            if operation == "object_info" else
            {"items": [artifact.filename.replace("/", "\\")] if request.get("folder") == "checkpoints" else []}
        )
        driver_for.return_value = driver
        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output = comfy_root / "output"
            checkpoint_root = comfy_root / "models" / "checkpoints"
            output.mkdir(parents=True)
            model_file = checkpoint_root / artifact.filename
            model_file.parent.mkdir(parents=True)
            model_file.write_bytes(payload)
            with override_settings(COMFYUI_OUTPUT_DIR=output):
                snapshot = probe_runtime_host(self.host)

        observed = snapshot.model_availability.get(
            deployment=deployment,
            artifact=artifact,
        )
        self.assertEqual(observed.status, RuntimeModelAvailability.Status.AVAILABLE)
        self.assertEqual(
            observed.observation_source,
            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        self.assertEqual(observed.observed_size_bytes, len(payload))
        self.assertEqual(observed.observed_sha256, digest)

    @patch("studio.services.runtime_platform._host_fingerprint")
    @patch("studio.services.runtime_platform._gpu_probe")
    @patch("studio.services.runtime_platform.driver_for")
    def test_remote_probe_never_writes_local_hardware_or_trusts_name_only_inventory(
        self,
        driver_for,
        gpu_probe,
        host_fingerprint,
    ):
        remote = RuntimeHost.objects.create(
            key="remote-probe-host",
            name="Remote probe host",
            fingerprint_sha256="f" * 64,
        )
        deployment, artifact, _ = self._deployment("remote-probe", priority=30)
        deployment.runtime_host = remote
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://192.0.2.10:8188",
        })
        deployment.save(update_fields=(
            "runtime_host", "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))
        driver = Mock()
        driver.invoke.side_effect = lambda current, operation, request: (
            {"system": {"comfyui_version": "remote-version"}}
            if operation == "health" else
            {"SaveImage": {}}
            if operation == "object_info" else
            {"items": [artifact.filename] if request.get("folder") == "checkpoints" else []}
        )
        driver_for.return_value = driver

        snapshot = probe_runtime_host(remote)

        remote.refresh_from_db()
        observed = snapshot.model_availability.get(
            deployment=deployment,
            artifact=artifact,
        )
        gpu_probe.assert_not_called()
        host_fingerprint.assert_not_called()
        self.assertEqual(remote.fingerprint_sha256, "f" * 64)
        self.assertEqual(snapshot.cpu_name, "")
        self.assertEqual(snapshot.details["os"], {})
        self.assertEqual(observed.status, RuntimeModelAvailability.Status.UNKNOWN)
        self.assertEqual(
            observed.observation_source,
            RuntimeModelAvailability.ObservationSource.NAME_ONLY,
        )
        self.assertIsNone(observed.observed_size_bytes)
        self.assertEqual(observed.observed_sha256, "")

    def test_host_probe_lease_serializes_due_decision_and_can_be_renewed(self):
        lease_key = f"runtime-host-probe:{self.host.id}"
        WorkerLease.objects.create(
            key=lease_key,
            holder_id="other-holder",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        claimed, reason = _claim_host(self.host, holder_id="this-holder", due_only=True)
        self.assertFalse(claimed)
        self.assertEqual(reason, "lease_held")

        WorkerLease.objects.filter(pk=lease_key).update(expires_at=timezone.now() - timedelta(seconds=1))
        claimed, reason = _claim_host(self.host, holder_id="this-holder", due_only=True)
        self.assertTrue(claimed)
        self.assertEqual(reason, "claimed")
        previous_expiry = WorkerLease.objects.get(pk=lease_key).expires_at
        _renew_host_lease(lease_key, holder_id="this-holder")
        self.assertGreaterEqual(WorkerLease.objects.get(pk=lease_key).expires_at, previous_expiry)
        _release_host_lease(lease_key, holder_id="this-holder")
        lease = WorkerLease.objects.get(pk=lease_key)
        self.assertEqual(lease.holder_id, "")
        self.assertIsNone(lease.expires_at)
