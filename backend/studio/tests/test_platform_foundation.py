from io import StringIO
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid
from unittest.mock import Mock, call, patch

from PIL import Image

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    ConfigRevision,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    ProductionRecipeVersion,
    ProviderProfile,
    RuntimeHost,
    RuntimeModelAvailability,
    SeriesProject,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowSmokeAttempt,
    WorkflowSemanticBinding,
)
from studio.services.comfyui import ComfyUIError
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
    resolve_configuration,
)
from studio.services.generated_artifacts import _archive_task_output
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.json_contracts import (
    assign_json_contract,
    canonical_json_bytes,
    find_forbidden_secret_fields,
    json_contract,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    _request_json,
    credential_binding_sha256,
    validate_endpoint,
)
from studio.services.runtime_platform import capability_readiness
from studio.services.secret_store import rotate_secret_binding
from studio.services.task_queue import comfy_client_for_task
from studio.services.workflow_smoke_queue import (
    claim_next_workflow_smoke,
    process_workflow_smoke,
)
from studio.services.workflow_versions import _execution_target_sha256
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring


class ConfigurationAndProviderTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.user = get_user_model().objects.create_user(
            username="platform-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="平台配置测试")

    def test_project_revision_overrides_system_with_explicit_source(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={"production.default_multi_shot": True},
            created_by=self.user,
            change_summary="项目显式路由",
        )
        publish_config_revision(revision, published_by=self.user)

        resolved = resolve_configuration(project=self.project)

        self.assertIs(resolved.values["production.default_multi_shot"], True)
        self.assertEqual(
            resolved.sources["production.default_multi_shot"]["scope"],
            ConfigRevision.ScopeType.PROJECT,
        )
        self.assertEqual(len(resolved.sha256), 64)

    def test_legacy_import_requires_explicit_publish_and_preserves_active_history(self):
        active_before = ConfigRevision.objects.get(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        )
        revision_count = ConfigRevision.objects.count()
        idle_before = resolve_configuration(
            required_keys=("runtime.task_idle_timeout_seconds",),
        ).values["runtime.task_idle_timeout_seconds"]
        output = StringIO()
        with patch.dict(
            os.environ, {
                "ALLOW_PUBLIC_REGISTRATION": "true",
                "TASK_GENERATION_TIMEOUT_SECONDS": "90",
            }, clear=True,
        ):
            call_command(
                "import_legacy_runtime_config",
                skip_deployments=True,
                skip_secrets=True,
                stdout=output,
            )
        self.assertEqual(ConfigRevision.objects.count(), revision_count)
        preview = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(preview["mode"], "dry-run")
        self.assertEqual(preview["review_required"], [
            "deadline_pair_adjustment", "system_configuration_draft",
        ])
        self.assertEqual(preview["controlled_adjustments"], [{
            "action": "include_in_same_reviewed_revision",
            "config_key": "runtime.task_idle_timeout_seconds",
            "constraint": (
                "runtime.task_idle_timeout_seconds <= production.task_timeout_seconds"
            ),
            "from": idle_before,
            "reason_code": "legacy_hard_timeout_below_current_idle",
            "source_scope": "system",
            "to": 90,
        }])
        self.assertIn("legacy_text_endpoint_contract_missing", output.getvalue())
        self.assertNotIn("legacy-local-qwen", output.getvalue())
        self.assertNotIn("legacy-deepseek", output.getvalue())

        with patch.dict(
            os.environ, {
                "ALLOW_PUBLIC_REGISTRATION": "true",
                "TASK_GENERATION_TIMEOUT_SECONDS": "90",
            }, clear=True,
        ):
            call_command(
                "import_legacy_runtime_config",
                apply=True,
                skip_deployments=True,
                skip_secrets=True,
                stdout=StringIO(),
            )
        active_before.refresh_from_db()
        self.assertEqual(active_before.activation_state, ConfigRevision.ActivationState.ACTIVE)
        self.assertTrue(ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            status=ConfigRevision.Status.DRAFT,
        ).exists())
        draft = ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            status=ConfigRevision.Status.DRAFT,
        ).latest("revision")
        self.assertEqual(draft.values.get(
            definition__key="runtime.task_idle_timeout_seconds",
        ).value, 90)
        self.assertEqual(resolve_configuration(
            required_keys=("runtime.task_idle_timeout_seconds",),
        ).values["runtime.task_idle_timeout_seconds"], idle_before)

        with patch.dict(
            os.environ, {
                "ALLOW_PUBLIC_REGISTRATION": "true",
                "TASK_GENERATION_TIMEOUT_SECONDS": "90",
            }, clear=True,
        ):
            call_command(
                "import_legacy_runtime_config",
                apply=True,
                publish_config=True,
                skip_deployments=True,
                skip_secrets=True,
                stdout=StringIO(),
            )
        self.assertIs(
            resolve_configuration(
                required_keys=("access.public_registration_enabled",),
            ).values["access.public_registration_enabled"],
            True,
        )
        task_timeout = resolve_configuration(
            required_keys=("production.task_timeout_seconds",),
        ).values["production.task_timeout_seconds"]
        self.assertEqual(task_timeout, 90)
        self.assertIsInstance(task_timeout, int)
        self.assertEqual(resolve_configuration(
            required_keys=("runtime.task_idle_timeout_seconds",),
        ).values["runtime.task_idle_timeout_seconds"], 90)
        self.assertEqual(ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).count(), 1)

    def test_legacy_import_rejects_unpublishable_deadline_pair_without_writes(self):
        revision_count = ConfigRevision.objects.count()
        with patch.dict(
            os.environ, {"TASK_GENERATION_TIMEOUT_SECONDS": "5"}, clear=True,
        ), self.assertRaisesMessage(
            CommandError,
            "无法生成满足正式 idle 取值范围的受控联动草稿",
        ):
            call_command(
                "import_legacy_runtime_config",
                apply=True,
                skip_deployments=True,
                skip_secrets=True,
                stdout=StringIO(),
            )
        self.assertEqual(ConfigRevision.objects.count(), revision_count)

    def test_legacy_import_preview_marks_retired_runtime_keys_without_draft_claims(self):
        retired_keys = {
            "QWEN_TRANSLATION_RUNTIME_PYTHON",
            "QWEN_TRANSLATION_DEVICE",
            "QWEN_CREATIVE_RUNTIME_PYTHON",
            "QWEN_CREATIVE_DEVICE",
            "QWEN_GPU_LAYERS",
            "COMFYUI_CLIENT_ID",
            "DEEPSEEK_ENABLED",
            "DEEPSEEK_MAX_RETRIES",
            "DEEPSEEK_CREATIVE_BATCH_SHOTS",
        }
        output = StringIO()
        with patch.dict(os.environ, {key: "legacy-value" for key in retired_keys}, clear=True):
            call_command(
                "import_legacy_runtime_config",
                skip_deployments=True,
                skip_secrets=True,
                stdout=output,
            )

        preview = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(set(preview["retired_keys"]), retired_keys)
        self.assertEqual(preview["deployments"], [])
        self.assertEqual(preview["draft_model_profiles"], [])
        self.assertNotIn("deepseek_profile_and_deployment", preview["review_required"])
        self.assertIs(preview["safe_to_remove"], False)

    def test_provider_api_rejects_plaintext_secret_keys(self):
        admin = get_user_model().objects.create_superuser(
            username="platform-admin",
            password="StrongPass123!",
            email="admin@example.test",
        )
        client = APIClient()
        client.force_authenticate(admin)

        response = client.post("/api/v1/provider-profiles/", {
            "key": "unsafe-openai",
            "name": "Unsafe provider",
            "driver_type": ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            "modalities": "text",
            "operations": "chat_completion",
            "driver_config": {
                "base_url": "https://example.test",
                "api_key": "must-not-be-stored",
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProviderProfile.objects.filter(key="unsafe-openai").exists())

    def test_provider_api_never_returns_credential_reference(self):
        admin = get_user_model().objects.create_superuser(
            username="secret-admin", password="StrongPass123!", email="secret@example.test",
        )
        client = APIClient()
        client.force_authenticate(admin)
        provider = ProviderProfile.objects.get(key="local-comfyui")
        credential = create_test_secret(
            key="platform.provider.credential",
            actor=admin,
            value="test-provider-secret",
        )
        response = client.patch(
            f"/api/v1/provider-profiles/{provider.id}/",
            {"credential_id": str(credential.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("credential_ref", response.data)
        self.assertNotIn("credential_id", response.data)
        self.assertNotIn("test-provider-secret", str(response.data))
        self.assertIs(response.data["credential_configured"], True)

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
    def test_provider_rejects_host_outside_deployment_allowlist(self):
        with self.assertRaisesMessage(Exception, "不在部署允许列表"):
            validate_endpoint("https://unapproved.example")

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
    @patch("studio.services.provider_registry.socket.getaddrinfo")
    def test_provider_rejects_allowlisted_hostname_resolving_to_private_address(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("10.20.30.40", 443))]
        with self.assertRaises(ProviderDriverError) as context:
            validate_endpoint("https://approved.example", resolve_dns=True)
        self.assertEqual(context.exception.code, "provider_private_address_forbidden")

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("localhost",))
    @patch("studio.services.provider_registry.socket.getaddrinfo")
    def test_explicit_loopback_hostname_must_resolve_only_to_loopback(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with self.assertRaises(ProviderDriverError) as context:
            validate_endpoint("http://localhost", resolve_dns=True)
        self.assertEqual(context.exception.code, "provider_loopback_mismatch")

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
    @patch("studio.services.provider_registry._peer_address", return_value="")
    @patch("studio.services.provider_registry.socket.getaddrinfo")
    @patch("studio.services.provider_registry.requests.request")
    def test_provider_request_fails_closed_when_peer_address_is_unverifiable(
        self, request, getaddrinfo, _peer_address,
    ):
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        response = Mock()
        response.content = b"{}"
        request.return_value = response
        with self.assertRaises(ProviderDriverError) as context:
            _request_json("POST", "https://approved.example/v1/test", timeout=1, payload={})
        self.assertEqual(context.exception.code, "provider_peer_unverifiable")
        response.close.assert_called_once()

    def test_task_client_uses_frozen_config_allows_live_non_secret_drift_and_rejects_credential_drift(self):
        profile = ModelProfileVersion.objects.get(
            profile_key="minimax-h3-video",
            version=1,
        )
        host = RuntimeHost.objects.create(key="test-local", name="Test local")
        credential = create_test_secret(
            key="platform.task.credential",
            actor=self.user,
            value="task-secret-v1",
        )
        deployment = ModelDeployment(
            key="test-h3-deployment",
            name="Test H3",
            profile_version=profile,
            runtime_host=host,
            credential=credential,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 12,
        })
        deployment.save()
        provider_config = dict(deployment.profile_version.provider.driver_config or {})
        deployment_config = dict(deployment.runtime_config)
        task = SimpleNamespace(capability_snapshot={
            "model_deployment_id": str(deployment.id),
            "model_profile_version_id": str(deployment.profile_version_id),
            "provider_profile_id": str(deployment.profile_version.provider_id),
            "provider_config_sha256": deployment.profile_version.provider.driver_config_sha256,
            "deployment_config_sha256": deployment.runtime_config_sha256,
            "provider_config": provider_config,
            "deployment_config": deployment_config,
            "frozen_execution_config_sha256": hashlib.sha256(canonical_json_bytes({
                "provider_config": provider_config,
                "deployment_config": deployment_config,
            })).hexdigest(),
            "effective_timeout_seconds": 12,
            "credential_binding_sha256": credential_binding_sha256(deployment),
            "endpoint_is_external": False,
        })

        client = comfy_client_for_task(task)
        self.assertEqual(client.base_url, "http://127.0.0.1:8188")
        self.assertEqual(client.timeout, 12)

        deployment.runtime_config = {"base_url": "http://127.0.0.1:9999"}
        deployment.save(update_fields=("runtime_config", "updated_at"))
        frozen_client = comfy_client_for_task(task)
        self.assertEqual(frozen_client.base_url, "http://127.0.0.1:8188")

        rotate_secret_binding(
            credential,
            secret_value="task-secret-v2",
            actor=self.user,
            reason="test_rotation",
        )
        with self.assertRaises(ComfyUIError):
            comfy_client_for_task(task)


class PlatformCatalogLifecycleApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="catalog-lifecycle-admin",
            password="StrongPass123!",
            email="catalog-lifecycle@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _profile(self, key: str, *, modality=ModelProfileVersion.Modality.IMAGE):
        profile = ModelProfileVersion(
            profile_key=key,
            version=1,
            provider=ProviderProfile.objects.get(key="local-comfyui"),
            model_identifier=f"{key}-identifier",
            modality=modality,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        return profile

    def _deployment(self, key: str, profile, host, *, status=ModelDeployment.Status.DISABLED):
        deployment = ModelDeployment(
            key=key,
            name=key,
            profile_version=profile,
            runtime_host=host,
            status=status,
        )
        assign_json_contract(deployment, "runtime_config", {
            "schema_version": 1,
            "runtime_source": "comfyui",
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
        })
        deployment.save()
        return deployment

    def test_published_and_retired_model_versions_and_bound_artifacts_are_immutable(self):
        profile = ModelProfileVersion.objects.get(profile_key="sdxl-image", version=1)
        artifact = profile.artifacts.first()
        self.assertIsNotNone(artifact)
        for lifecycle in (
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        ):
            with self.subTest(lifecycle=lifecycle):
                ModelProfileVersion.objects.filter(pk=profile.pk).update(status=lifecycle)
                patched_profile = self.client.patch(
                    f"/api/v1/model-profile-versions/{profile.id}/",
                    {"model_identifier": f"forbidden-{lifecycle}"},
                    format="json",
                )
                self.assertEqual(patched_profile.status_code, 400, patched_profile.data)
                deleted_profile = self.client.delete(
                    f"/api/v1/model-profile-versions/{profile.id}/",
                )
                self.assertEqual(deleted_profile.status_code, 400, deleted_profile.data)
                patched_artifact = self.client.patch(
                    f"/api/v1/model-artifacts/{artifact.id}/",
                    {"filename": f"forbidden-{lifecycle}.safetensors"},
                    format="json",
                )
                self.assertEqual(patched_artifact.status_code, 400, patched_artifact.data)
                deleted_artifact = self.client.delete(
                    f"/api/v1/model-artifacts/{artifact.id}/",
                )
                self.assertEqual(deleted_artifact.status_code, 400, deleted_artifact.data)

    def test_draft_model_and_artifact_remain_editable(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        profile = ModelProfileVersion(
            profile_key="draft-lifecycle-profile",
            version=1,
            provider=provider,
            model_identifier="draft-v1",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="draft-checkpoint",
            filename="draft-v1.safetensors",
        )

        artifact_response = self.client.patch(
            f"/api/v1/model-artifacts/{artifact.id}/",
            {"filename": "draft-v2.safetensors"},
            format="json",
        )
        self.assertEqual(artifact_response.status_code, 200, artifact_response.data)
        profile_response = self.client.patch(
            f"/api/v1/model-profile-versions/{profile.id}/",
            {"model_identifier": "draft-v2"},
            format="json",
        )
        self.assertEqual(profile_response.status_code, 200, profile_response.data)
        deleted_artifact = self.client.delete(f"/api/v1/model-artifacts/{artifact.id}/")
        self.assertEqual(deleted_artifact.status_code, 204)

    def test_versioned_legacy_template_and_published_recipe_cannot_be_deleted(self):
        version = WorkflowTemplateVersion.objects.filter(template__isnull=False).first()
        self.assertIsNotNone(version)
        with self.assertRaises(ProtectedError):
            version.template.delete()

        recipe = ProductionRecipeVersion(
            recipe_key="published-delete-guard",
            version=1,
            name="Published delete guard",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {"schema_version": 1, "stages": []})
        recipe.save()
        response = self.client.delete(f"/api/v1/production-recipes/{recipe.id}/")
        self.assertEqual(response.status_code, 400, response.data)

    def test_model_workflow_and_recipe_retire_are_terminal_lifecycle_actions(self):
        profile = self._profile("retire-lifecycle-profile")
        host = RuntimeHost.objects.create(
            key="retire-lifecycle-host", name="Retire lifecycle host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment(
            "retire-lifecycle-deployment", profile, host,
            status=ModelDeployment.Status.ACTIVE,
        )
        blocked = self.client.post(
            f"/api/v1/model-profile-versions/{profile.id}/retire/", {}, format="json",
        )
        self.assertEqual(blocked.status_code, 409, blocked.data)
        deployment.status = ModelDeployment.Status.DISABLED
        deployment.save(update_fields=("status", "updated_at"))
        retired = self.client.post(
            f"/api/v1/model-profile-versions/{profile.id}/retire/", {}, format="json",
        )
        self.assertEqual(retired.status_code, 200, retired.data)
        self.assertEqual(retired.data["status"], ModelProfileVersion.Status.RETIRED)
        self.assertEqual(self.client.patch(
            f"/api/v1/model-profile-versions/{profile.id}/",
            {"model_identifier": "forbidden"}, format="json",
        ).status_code, 400)
        self.assertEqual(self.client.delete(
            f"/api/v1/model-profile-versions/{profile.id}/",
        ).status_code, 400)

        workflow = WorkflowTemplateVersion.objects.first()
        self.assertIsNotNone(workflow)
        WorkflowTemplateVersion.objects.filter(pk=workflow.pk).update(
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            is_current=True,
        )
        retired_workflow = self.client.post(
            f"/api/v1/workflow-template-versions/{workflow.id}/retire/", {}, format="json",
        )
        self.assertEqual(retired_workflow.status_code, 200, retired_workflow.data)
        self.assertEqual(retired_workflow.data["status"], WorkflowTemplateVersion.Status.RETIRED)
        self.assertIs(retired_workflow.data["is_current"], False)
        self.assertEqual(self.client.delete(
            f"/api/v1/workflow-template-versions/{workflow.id}/",
        ).status_code, 400)

        recipe = ProductionRecipeVersion(
            recipe_key="retire-lifecycle-recipe", version=1,
            name="Retire lifecycle recipe",
            status=ProductionRecipeVersion.Status.PUBLISHED,
            published_at=timezone.now(), published_by=self.admin,
        )
        assign_json_contract(recipe, "definition", {"schema_version": 1, "stages": []})
        recipe.save()
        retired_recipe = self.client.post(
            f"/api/v1/production-recipes/{recipe.id}/retire/", {}, format="json",
        )
        self.assertEqual(retired_recipe.status_code, 200, retired_recipe.data)
        self.assertEqual(retired_recipe.data["status"], ProductionRecipeVersion.Status.RETIRED)
        self.assertEqual(self.client.patch(
            f"/api/v1/production-recipes/{recipe.id}/",
            {"name": "forbidden"}, format="json",
        ).status_code, 400)
        self.assertEqual(self.client.delete(
            f"/api/v1/production-recipes/{recipe.id}/",
        ).status_code, 400)

    def test_deployment_evidence_is_protected_from_delete(self):
        profile = self._profile("deployment-evidence-profile")
        host = RuntimeHost.objects.create(
            key="deployment-evidence-host", name="Deployment evidence host",
            status=RuntimeHost.Status.ACTIVE,
        )
        smoke_deployment = self._deployment("deployment-smoke-evidence", profile, host)
        record_deployment_capability_snapshot(
            smoke_deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["health"],
            evidence={"schema_version": 1, "execution_submitted": False},
        )
        evidence = self.client.get(
            f"/api/v1/model-deployments/{smoke_deployment.id}/evidence/",
        )
        self.assertEqual(evidence.status_code, 200, evidence.data)
        self.assertEqual(len(evidence.data["results"]), 1)
        self.assertFalse(evidence.data["results"][0]["credential_bound"])
        self.assertNotIn(
            "credential_binding_sha256",
            json.dumps(evidence.data, default=str),
        )
        filtered_evidence = self.client.get(
            f"/api/v1/model-deployments/{smoke_deployment.id}/evidence/",
            {"status": ModelDeploymentCapabilitySnapshot.Status.PASSED},
        )
        self.assertEqual(filtered_evidence.status_code, 200, filtered_evidence.data)
        self.assertEqual(filtered_evidence.data["count"], 1)
        protected = self.client.delete(
            f"/api/v1/model-deployments/{smoke_deployment.id}/",
        )
        self.assertEqual(protected.status_code, 409, protected.data)
        self.assertTrue(ModelDeployment.objects.filter(pk=smoke_deployment.pk).exists())
        self.assertTrue(smoke_deployment.capability_snapshots.exists())
        self.assertFalse(PlatformLifecycleAuditEvent.objects.filter(
            object_type="model_deployment",
            object_id=smoke_deployment.id,
            action="delete",
        ).exists())

        availability_deployment = self._deployment(
            "deployment-availability-evidence", profile, host,
        )
        snapshot = HostCapabilitySnapshot(
            host=host, revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(), expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot, deployment=availability_deployment,
            availability_key="deployment-availability-evidence",
            status=RuntimeModelAvailability.Status.AVAILABLE,
        )
        protected = self.client.delete(
            f"/api/v1/model-deployments/{availability_deployment.id}/",
        )
        self.assertEqual(protected.status_code, 409, protected.data)
        self.assertTrue(ModelDeployment.objects.filter(pk=availability_deployment.pk).exists())
        self.assertTrue(availability_deployment.runtime_availability.exists())

    def test_artifact_verification_uses_fresh_host_snapshot_evidence(self):
        profile = self._profile("artifact-verification-profile")
        artifact = ModelArtifact.objects.create(
            profile_version=profile, role=ModelArtifact.Role.CHECKPOINT,
            logical_name="verified-checkpoint", filename="verified.safetensors",
            sha256="a" * 64, size_bytes=128,
        )
        host = RuntimeHost.objects.create(
            key="artifact-verification-host", name="Artifact verification host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment("artifact-verification-deployment", profile, host)
        snapshot = HostCapabilitySnapshot(
            host=host, revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(), expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot, deployment=deployment, artifact=artifact,
            availability_key="verified-checkpoint",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=128,
            observed_sha256="a" * 64,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        response = self.client.post(
            f"/api/v1/model-artifacts/{artifact.id}/verify/",
            {"host_snapshot_id": str(snapshot.id)}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], ModelArtifact.Status.AVAILABLE)
        self.assertEqual(str(response.data["verified_snapshot"]), str(snapshot.id))
        artifact.refresh_from_db()
        self.assertEqual(artifact.verified_by, self.admin)
        self.assertIsNotNone(artifact.verified_at)

    def test_name_only_artifact_availability_cannot_verify_model_bytes(self):
        profile = self._profile("name-only-artifact-profile")
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="name-only-checkpoint",
            filename="name-only.safetensors",
            sha256="a" * 64,
            size_bytes=128,
        )
        host = RuntimeHost.objects.create(
            key="name-only-artifact-host",
            name="Name-only artifact host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment("name-only-artifact-deployment", profile, host)
        snapshot = HostCapabilitySnapshot(
            host=host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key="name-only-checkpoint",
            status=RuntimeModelAvailability.Status.AVAILABLE,
        )

        response = self.client.post(
            f"/api/v1/model-artifacts/{artifact.id}/verify/",
            {"host_snapshot_id": str(snapshot.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, ModelArtifact.Status.DECLARED)

    def test_runtime_host_status_is_guarded_and_deployment_activation_requires_active_host(self):
        profile = self._profile("host-lifecycle-profile")
        host = RuntimeHost.objects.create(
            key="host-lifecycle-host", name="Host lifecycle host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment(
            "host-lifecycle-deployment", profile, host,
            status=ModelDeployment.Status.ACTIVE,
        )
        blocked = self.client.post(
            f"/api/v1/runtime-hosts/{host.id}/status/", {"is_active": False}, format="json",
        )
        self.assertEqual(blocked.status_code, 409, blocked.data)
        deployment.status = ModelDeployment.Status.DISABLED
        deployment.save(update_fields=("status", "updated_at"))
        disabled = self.client.post(
            f"/api/v1/runtime-hosts/{host.id}/status/", {"is_active": False}, format="json",
        )
        self.assertEqual(disabled.status_code, 200, disabled.data)
        self.assertEqual(disabled.data["status"], RuntimeHost.Status.DISABLED)
        before = host.capability_snapshots.count()
        rejected_probe = self.client.post(f"/api/v1/runtime-hosts/{host.id}/probe/", {}, format="json")
        self.assertEqual(rejected_probe.status_code, 409, rejected_probe.data)
        self.assertEqual(host.capability_snapshots.count(), before)
        enabled = self.client.post(
            f"/api/v1/runtime-hosts/{host.id}/status/", {"is_active": True}, format="json",
        )
        self.assertEqual(enabled.status_code, 200, enabled.data)
        self.assertEqual(enabled.data["status"], RuntimeHost.Status.UNAVAILABLE)
        repeated = self.client.post(
            f"/api/v1/runtime-hosts/{host.id}/status/", {"is_active": True}, format="json",
        )
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["status"], RuntimeHost.Status.UNAVAILABLE)
        create_response = self.client.post("/api/v1/model-deployments/", {
            "key": "host-lifecycle-rejected",
            "name": "Host lifecycle rejected",
            "profile_version": str(profile.id),
            "runtime_host": str(host.id),
            "runtime_config": {
                "schema_version": 1,
                "runtime_source": "comfyui",
                "base_url": "http://127.0.0.1:8188",
                "timeout_seconds": 30,
            },
            "priority": 0,
            "is_default": False,
            "status": ModelDeployment.Status.ACTIVE,
        }, format="json")
        self.assertEqual(create_response.status_code, 400, create_response.data)

    def test_members_only_read_published_recipe_versions(self):
        recipes = []
        for index, lifecycle in enumerate((
            ProductionRecipeVersion.Status.DRAFT,
            ProductionRecipeVersion.Status.PUBLISHED,
            ProductionRecipeVersion.Status.RETIRED,
        ), start=1):
            recipe = ProductionRecipeVersion(
                recipe_key=f"member-visible-recipe-{index}", version=1,
                name=f"Member visible recipe {index}", status=lifecycle,
                published_at=timezone.now() if lifecycle != ProductionRecipeVersion.Status.DRAFT else None,
                published_by=self.admin if lifecycle != ProductionRecipeVersion.Status.DRAFT else None,
            )
            assign_json_contract(recipe, "definition", {"schema_version": 1, "stages": []})
            recipe.save()
            recipes.append(recipe)
        member = get_user_model().objects.create_user(
            username="recipe-member", password="StrongPass123!",
        )
        member_client = APIClient()
        member_client.force_authenticate(member)
        listing = member_client.get("/api/v1/production-recipes/")
        self.assertEqual(listing.status_code, 200, listing.data)
        ids = {str(item["id"]) for item in listing.data["results"]}
        self.assertIn(str(recipes[1].id), ids)
        self.assertNotIn(str(recipes[0].id), ids)
        self.assertNotIn(str(recipes[2].id), ids)
        self.assertEqual(member_client.get(
            f"/api/v1/production-recipes/{recipes[0].id}/",
        ).status_code, 404)
        self.assertEqual(member_client.get(
            f"/api/v1/production-recipes/{recipes[1].id}/",
        ).status_code, 200)

    def test_contract_json_rejects_plaintext_secrets_without_blocking_token_limits(self):
        forbidden_cases = {
            "client_secret", "clientSecret", "CLIENT_SECRET", "clientsecret",
            "secret-key", "SECRETKEY", "access_key", "accessKey",
            "api_secret", "apiSecret", "private_key", "privateKey",
            "auth_token", "authToken", "bearer_token", "bearerToken",
        }
        for key in forbidden_cases:
            with self.subTest(key=key):
                self.assertEqual(
                    find_forbidden_secret_fields({"nested": [{key: "plaintext"}]}),
                    [f"nested[0].{key}"],
                )
        self.assertEqual(find_forbidden_secret_fields({
            "max_output_tokens": 4096,
            "token-limit": 1024,
            "tokenUsage": {"prompt": 12, "completion": 24},
            "usage_tokens": 36,
        }), [])

        provider = ProviderProfile.objects.get(key="local-comfyui")
        rejected = self.client.post("/api/v1/model-profile-versions/", {
            "profile_key": "unsafe-contract-profile", "version": 1,
            "provider": str(provider.id), "model_identifier": "unsafe-contract",
            "modality": ModelProfileVersion.Modality.TEXT,
            "specification": {"schema_version": 1, "api_key": "plaintext"},
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        allowed = self.client.post("/api/v1/model-profile-versions/", {
            "profile_key": "safe-token-limit-profile", "version": 1,
            "provider": str(provider.id), "model_identifier": "safe-token-limit",
            "modality": ModelProfileVersion.Modality.TEXT,
            "specification": {"schema_version": 1, "max_output_tokens": 4096},
        }, format="json")
        self.assertEqual(allowed.status_code, 201, allowed.data)
        recipe = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "unsafe-contract-recipe", "version": 1,
            "name": "Unsafe contract recipe",
            "definition": {"schema_version": 1, "password": "plaintext", "stages": []},
        }, format="json")
        self.assertEqual(recipe.status_code, 400, recipe.data)
        camel_recipe = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "unsafe-camel-contract-recipe", "version": 1,
            "name": "Unsafe camel contract recipe",
            "definition": {
                "schema_version": 1,
                "provider": {"clientSecret": "plaintext"},
                "stages": [],
            },
        }, format="json")
        self.assertEqual(camel_recipe.status_code, 400, camel_recipe.data)

    def test_platform_lifecycle_audit_is_append_only_safe_and_staff_read_only(self):
        headers = {"HTTP_X_AUDIT_REASON_CODE": "planned_change"}
        created = self.client.post("/api/v1/provider-profiles/", {
            "key": "lifecycle-audit-provider",
            "name": "Lifecycle audit provider",
            "driver_type": ProviderProfile.DriverType.COMFYUI,
            "modalities": "image,video",
            "operations": "health,submit,poll",
            "driver_config": {},
            "is_active": True,
        }, format="json", **headers)
        self.assertEqual(created.status_code, 201, created.data)
        provider_id = created.data["id"]
        updated = self.client.patch(
            f"/api/v1/provider-profiles/{provider_id}/",
            {"is_active": False},
            format="json",
            **headers,
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        deleted = self.client.delete(
            f"/api/v1/provider-profiles/{provider_id}/",
            **{"HTTP_X_AUDIT_REASON_CODE": "lifecycle_cleanup"},
        )
        self.assertEqual(deleted.status_code, 204, deleted.data)

        events = PlatformLifecycleAuditEvent.objects.filter(
            object_type="provider_profile",
            object_id=provider_id,
        ).order_by("created_at")
        self.assertEqual(list(events.values_list("action", flat=True)), [
            "create", "update", "delete",
        ])
        self.assertEqual(events[0].actor, self.admin)
        self.assertEqual(events[0].reason_code, "planned_change")
        self.assertEqual(events[2].reason_code, "lifecycle_cleanup")
        self.assertEqual(len(events[0].before_sha256), 64)
        self.assertEqual(len(events[0].after_sha256), 64)
        self.assertEqual(len(events[1].before_sha256), 64)
        self.assertEqual(len(events[1].after_sha256), 64)
        self.assertNotEqual(events[1].before_sha256, events[1].after_sha256)
        self.assertEqual(len(events[2].after_sha256), 64)
        serialized = json.dumps(list(events.values(
            "before_summary", "after_summary",
        )), default=str)
        for forbidden in (
            "base_url", "endpoint", "storage_uri", "filename", "driver_config",
            "runtime_config", "secret", "password", "token",
        ):
            self.assertNotIn(forbidden, serialized.casefold())

        event = events[0]
        event.reason_code = "tampered"
        with self.assertRaises(DjangoValidationError):
            event.save()
        with self.assertRaises(DjangoValidationError):
            event.delete()
        with self.assertRaises(DjangoValidationError):
            PlatformLifecycleAuditEvent.objects.filter(pk=event.pk).update(
                reason_code="tampered",
            )
        with self.assertRaises(DjangoValidationError):
            PlatformLifecycleAuditEvent.objects.filter(pk=event.pk).delete()

        staff = get_user_model().objects.create_user(
            username="lifecycle-audit-staff",
            password="StrongPass123!",
            is_staff=True,
        )
        staff_client = APIClient()
        staff_client.force_authenticate(staff)
        listing = staff_client.get(
            "/api/v1/platform-lifecycle-audit-events/",
            {"object_type": "provider_profile", "object_id": provider_id},
        )
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(listing.data["count"], 3)
        self.assertEqual(
            set(listing.data["results"][0]),
            {
                "id", "actor", "actor_username", "action", "object_type",
                "object_id", "reason_code", "before_sha256", "after_sha256",
                "before_summary", "after_summary", "created_at",
            },
        )
        member = get_user_model().objects.create_user(
            username="lifecycle-audit-member", password="StrongPass123!",
        )
        member_client = APIClient()
        member_client.force_authenticate(member)
        denied = member_client.get("/api/v1/platform-lifecycle-audit-events/")
        self.assertEqual(denied.status_code, 403, denied.data)
        self.assertEqual(denied.data["error"]["status_code"], 403)

    def test_config_revision_creator_publisher_and_activator_are_independently_audited(self):
        definition = self.client.post("/api/v1/config-definitions/", {
            "key": "audit.runtime.relative_path",
            "domain": "runtime",
            "value_type": "string",
            "description": "Lifecycle audit activation test",
            "impact_modules": [],
            "restart_required": True,
            "allowed_scopes": "system",
            "allowed_default_sources": "",
            "is_required": False,
            "is_secret": False,
            "is_active": True,
            "value_schema": {"type": "string", "format": "safe-relative-path"},
            "initial_value": None,
        }, format="json", HTTP_X_AUDIT_REASON_CODE="planned_change")
        self.assertEqual(definition.status_code, 201, definition.data)

        draft = self.client.post("/api/v1/config-revisions/", {
            "scope_type": "system",
            "values": {"audit.runtime.relative_path": "runtime/cache"},
            "unset_keys": [],
            "change_summary": "separate lifecycle actors",
            "publish": False,
        }, format="json", HTTP_X_AUDIT_REASON_CODE="configuration_correction")
        self.assertEqual(draft.status_code, 201, draft.data)
        revision_id = draft.data["id"]

        publisher = get_user_model().objects.create_user(
            username="lifecycle-publisher",
            password="StrongPass123!",
            is_staff=True,
        )
        publisher_client = APIClient()
        publisher_client.force_authenticate(publisher)
        with override_settings(AUTOPROMPT_BOOT_ID="lifecycle-boot-a"):
            published = publisher_client.post(
                f"/api/v1/config-revisions/{revision_id}/publish/",
                {},
                format="json",
                HTTP_X_AUDIT_REASON_CODE="validation_completed",
            )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["activation_state"], "pending_activation")

        activator = get_user_model().objects.create_user(
            username="lifecycle-activator",
            password="StrongPass123!",
            is_staff=True,
        )
        activator_client = APIClient()
        activator_client.force_authenticate(activator)
        with override_settings(AUTOPROMPT_BOOT_ID="lifecycle-boot-b"):
            activated = activator_client.post(
                f"/api/v1/config-revisions/{revision_id}/activate/",
                {},
                format="json",
                HTTP_X_AUDIT_REASON_CODE="planned_change",
            )
        self.assertEqual(activated.status_code, 200, activated.data)
        self.assertEqual(activated.data["activation_state"], "active")

        events = PlatformLifecycleAuditEvent.objects.filter(
            object_type="config_revision",
            object_id=revision_id,
        ).order_by("created_at")
        self.assertEqual(list(events.values_list("action", "actor__username")), [
            ("create", self.admin.username),
            ("publish", publisher.username),
            ("activate", activator.username),
        ])
        revision = ConfigRevision.objects.get(pk=revision_id)
        self.assertEqual(revision.created_by, self.admin)


class WorkflowVersionApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="workflow-admin",
            password="StrongPass123!",
            email="workflow@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.provider = ProviderProfile.objects.get(key="local-comfyui")
        self.profile = ModelProfileVersion.objects.get(profile_key="sdxl-image", version=1)
        self.host = RuntimeHost.objects.create(key="workflow-local", name="Workflow local")
        self.deployment = ModelDeployment(
            key="workflow-sdxl",
            name="Workflow SDXL",
            profile_version=self.profile,
            runtime_host=self.host,
        )
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 10,
        })
        self.deployment.save()
        self.template = WorkflowTemplate.objects.create(
            owner=None,
            slug="platform-workflow-test",
            version=1,
            name="Platform workflow test",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            definition={"1": {"class_type": "TestNode", "inputs": {"text": "legacy"}}},
            input_schema={},
            is_system=True,
            is_active=False,
        )

    @staticmethod
    def _object_info(count: int) -> dict:
        return {
            f"Node-{index:04d}": {
                "display_name": f"Node {index:04d}",
                "category": "tests",
                "input": {
                    "required": {"prompt": ["STRING", {}]},
                    "optional": {"seed": ["INT", {}]},
                },
                "output": ["IMAGE"],
            }
            for index in reversed(range(count))
        }

    def test_node_metadata_v2_pages_the_complete_stable_catalog(self):
        driver = Mock()
        driver.invoke.return_value = self._object_info(451)
        pages = []
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for offset in (0, 200, 400):
                response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                    "deployment_id": str(self.deployment.id),
                    "offset": offset,
                    "limit": 200,
                })
                self.assertEqual(response.status_code, 200, response.data)
                pages.append(response.data)

        self.assertEqual([page["schema_version"] for page in pages], [2, 2, 2])
        self.assertEqual([page["total_count"] for page in pages], [451, 451, 451])
        self.assertEqual([page["offset"] for page in pages], [0, 200, 400])
        self.assertEqual([len(page["items"]) for page in pages], [200, 200, 51])
        self.assertEqual([page["next_offset"] for page in pages], [200, 400, None])
        self.assertEqual(len({page["catalog_sha256"] for page in pages}), 1)
        class_types = [
            item["class_type"]
            for page in pages
            for item in page["items"]
        ]
        self.assertEqual(class_types, sorted(class_types))
        self.assertEqual(len(class_types), len(set(class_types)))
        self.assertEqual(driver.invoke.call_count, 3)

    def test_node_metadata_exact_page_boundary_is_complete(self):
        driver = Mock()
        driver.invoke.return_value = self._object_info(200)
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                "deployment_id": str(self.deployment.id),
                "offset": 0,
                "limit": 200,
            })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_count"], 200)
        self.assertEqual(len(response.data["items"]), 200)
        self.assertIsNone(response.data["next_offset"])
        self.assertNotIn("truncated", response.data)

    def test_node_metadata_filters_and_hashes_the_full_catalog_before_slicing(self):
        driver = Mock()
        driver.invoke.return_value = self._object_info(250)
        pages = []
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for offset in (0, 20, 40):
                response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                    "deployment_id": str(self.deployment.id),
                    "search": "node-02",
                    "offset": offset,
                    "limit": 20,
                })
                self.assertEqual(response.status_code, 200, response.data)
                pages.append(response.data)

        self.assertEqual([page["total_count"] for page in pages], [50, 50, 50])
        self.assertEqual([len(page["items"]) for page in pages], [20, 20, 10])
        self.assertEqual([page["next_offset"] for page in pages], [20, 40, None])
        self.assertEqual(len({page["catalog_sha256"] for page in pages}), 1)
        self.assertEqual(
            [item["class_type"] for page in pages for item in page["items"]],
            [f"Node-{index:04d}" for index in range(200, 250)],
        )

    def test_node_metadata_query_contract_rejects_invalid_pagination(self):
        driver = Mock()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for params in (
                {"deployment_id": str(self.deployment.id), "offset": -1},
                {"deployment_id": str(self.deployment.id), "limit": 0},
                {"deployment_id": str(self.deployment.id), "limit": 201},
            ):
                with self.subTest(params=params):
                    response = self.client.get(
                        "/api/v1/runtime-hosts/node-metadata/",
                        params,
                    )
                    self.assertEqual(response.status_code, 400, response.data)
        driver.invoke.assert_not_called()

    def _import(
        self,
        *,
        include_resolution_bindings=True,
        h3_mode="",
        smoke_asset_binding=None,
        contract_extra=None,
    ):
        payload = {
            "template": str(self.template.id),
            "provider": str(self.provider.id),
            "model_profile_version": str(self.profile.id),
            "graph": {
                "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"}},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "smoke", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            "input_contract": {
                "schema_version": 2,
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": {
                    "prompt.positive": {"type": "string", "required": True, "min_length": 1},
                    "output.prefix": {"type": "string", "required": True},
                    "image.width": {"type": "integer", "required": True},
                    "image.height": {"type": "integer", "required": True},
                },
                "outputs": {"primary": {"node_id": "2", "kind": "image", "required": True}},
                "smoke_inputs": {
                    "prompt.positive": "hello", "image.width": 512, "image.height": 512,
                },
            },
            "semantic_bindings": [
                {
                    "semantic_key": "prompt.positive",
                    "node_id": "1",
                    "input_name": "text",
                    "value_type": "string",
                },
                {
                    "semantic_key": "output.prefix",
                    "node_id": "2",
                    "input_name": "filename_prefix",
                    "value_type": "string",
                },
                {
                    "semantic_key": "image.width",
                    "node_id": "3",
                    "input_name": "width",
                    "value_type": "integer",
                },
                {
                    "semantic_key": "image.height",
                    "node_id": "3",
                    "input_name": "height",
                    "value_type": "integer",
                },
            ],
            "model_requirements": [],
        }
        if not include_resolution_bindings:
            payload["semantic_bindings"] = [
                item for item in payload["semantic_bindings"]
                if item["semantic_key"] not in {"image.width", "image.height"}
            ]
        if h3_mode:
            payload["input_contract"]["h3_modes"] = [h3_mode]
            payload["input_contract"]["smoke_inputs"]["H3_MODE"] = h3_mode
        if smoke_asset_binding is not None:
            payload["input_contract"]["smoke_asset_bindings"] = [smoke_asset_binding]
            payload["input_contract"]["parameters"]["asset.image.reference"] = {
                "type": "asset_image",
                "required": True,
                "asset_category": smoke_asset_binding["category"],
            }
            payload["semantic_bindings"].append({
                "semantic_key": "asset.image.reference",
                "node_id": "2",
                "input_name": "images",
                "value_type": "asset_image",
            })
        if contract_extra:
            payload["input_contract"].update(contract_extra)
        return self.client.post(
            "/api/v1/workflow-template-versions/",
            payload,
            format="json",
        )

    @staticmethod
    def _canvas_object_info():
        return {
            "CLIPTextEncode": {
                "input": {"required": {"text": ["STRING", {}]}},
                "output": [],
                "output_name": [],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["STRING", {}],
                    "filename_prefix": ["STRING", {}],
                }},
                "output": [],
                "output_name": [],
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {}],
                    "height": ["INT", {}],
                    "batch_size": ["INT", {}],
                }},
                "output": [],
                "output_name": [],
            },
        }

    def test_saved_workflow_exports_ui_canvas_without_submitting_work(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        url = f"/api/v1/workflow-template-versions/{version_id}/comfyui-workflow/"

        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch("studio.platform_views.driver_for", return_value=driver):
            response = self.client.get(url, {"deployment_id": str(self.deployment.id)})
            downloaded = self.client.get(url, {
                "download": "true",
                "deployment_id": str(self.deployment.id),
            })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["workflow_version_id"], version_id)
        self.assertEqual(response.data["format"], "comfyui_ui")
        self.assertTrue(response.data["import_ready"])
        self.assertFalse(response.data["direct_open_supported"])
        self.assertIsNone(response.data["direct_open_url"])
        self.assertEqual(response.data["open_mode"], "download_import")
        self.assertEqual(response.data["deployment_id"], str(self.deployment.id))
        self.assertEqual(response.data["deployment_name"], self.deployment.name)
        self.assertEqual(response.data["deployment_source"], "explicit_draft")
        self.assertEqual(response.data["workflow"]["version"], 0.4)
        self.assertEqual(
            response.data["workflow"]["extra"]["autoprompt"]["workflow_version_id"],
            version_id,
        )
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment;", downloaded.headers["Content-Disposition"])
        self.assertEqual(
            downloaded.headers["X-AutoPrompt-Deployment-Id"],
            str(self.deployment.id),
        )
        self.assertEqual(json.loads(downloaded.content)["version"], 0.4)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 0)
        self.assertEqual(driver.invoke.call_count, 2)
        self.assertEqual(
            driver.invoke.call_args_list,
            [
                call(self.deployment, "object_info", {}),
                call(self.deployment, "object_info", {}),
            ],
        )

    def test_draft_workflow_canvas_export_requires_explicit_comfy_deployment(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        driver_factory = Mock()
        with patch("studio.platform_views.driver_for", driver_factory):
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/comfyui-workflow/",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("显式选择", str(response.data))
        driver_factory.assert_not_called()

    def test_draft_workflow_canvas_export_uses_selected_deployment_not_default(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        self.deployment.is_default = True
        self.deployment.save(update_fields=("is_default", "updated_at"))
        alternate_host = RuntimeHost.objects.create(
            key="workflow-alternate",
            name="Workflow alternate",
        )
        alternate = ModelDeployment(
            key="workflow-sdxl-alternate",
            name="Workflow SDXL alternate",
            profile_version=self.profile,
            runtime_host=alternate_host,
            is_default=False,
        )
        assign_json_contract(alternate, "runtime_config", {
            "base_url": "http://127.0.0.1:8288",
            "timeout_seconds": 12,
        })
        alternate.save()
        comfy_client = Mock()
        comfy_client.object_info.return_value = self._canvas_object_info()

        with patch(
            "studio.services.comfyui.ComfyUIClient",
            return_value=comfy_client,
        ) as client_factory:
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/comfyui-workflow/",
                {"deployment_id": str(alternate.id)},
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["deployment_id"], str(alternate.id))
        client_factory.assert_called_once_with(
            base_url="http://127.0.0.1:8288",
            timeout=12.0,
            endpoint_prevalidated=True,
        )
        comfy_client.object_info.assert_called_once_with()

    def _mark_workflow_evidence_bound(self, version_id, *, status_value):
        version = WorkflowTemplateVersion.objects.prefetch_related(
            "model_requirements__artifact",
        ).get(pk=version_id)
        evidence = {
            "schema_version": 1,
            "deployment_id": str(self.deployment.id),
            "execution_target_sha256": _execution_target_sha256(
                version,
                self.deployment,
            ),
        }
        version.status = status_value
        version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.validated_deployment = self.deployment
        version.validation_evidence = evidence
        version.validation_evidence_sha256 = json_contract(evidence).sha256
        version.save(update_fields=(
            "status", "validation_state", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "updated_at",
        ))
        return version

    def test_validated_and_published_canvas_exports_are_pinned_to_evidence_deployment(self):
        alternate_host = RuntimeHost.objects.create(
            key="workflow-evidence-alternate",
            name="Workflow evidence alternate",
        )
        alternate = ModelDeployment(
            key="workflow-sdxl-evidence-alternate",
            name="Workflow SDXL evidence alternate",
            profile_version=self.profile,
            runtime_host=alternate_host,
        )
        assign_json_contract(alternate, "runtime_config", {
            "base_url": "http://127.0.0.1:8388",
            "timeout_seconds": 12,
        })
        alternate.save()

        for status_value in (
            WorkflowTemplateVersion.Status.VALIDATED,
            WorkflowTemplateVersion.Status.PUBLISHED,
        ):
            with self.subTest(status=status_value):
                imported = self._import()
                self.assertEqual(imported.status_code, 201, imported.data)
                version = self._mark_workflow_evidence_bound(
                    imported.data["id"],
                    status_value=status_value,
                )
                driver = Mock()
                driver.invoke.return_value = self._canvas_object_info()
                url = f"/api/v1/workflow-template-versions/{version.id}/comfyui-workflow/"
                with patch("studio.platform_views.driver_for", return_value=driver):
                    response = self.client.get(url)
                    mismatched = self.client.get(
                        url,
                        {"deployment_id": str(alternate.id)},
                    )

                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["deployment_id"], str(self.deployment.id))
                self.assertEqual(response.data["deployment_source"], "validated")
                self.assertEqual(mismatched.status_code, 400, mismatched.data)
                driver.invoke.assert_called_once_with(
                    self.deployment,
                    "object_info",
                    {},
                )

    def test_canvas_export_rejects_inactive_validated_deployment_before_network(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version = self._mark_workflow_evidence_bound(
            imported.data["id"],
            status_value=WorkflowTemplateVersion.Status.VALIDATED,
        )
        self.deployment.status = ModelDeployment.Status.DISABLED
        self.deployment.save(update_fields=("status", "updated_at"))
        driver_factory = Mock()
        with patch("studio.platform_views.driver_for", driver_factory):
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{version.id}/comfyui-workflow/",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("停用", str(response.data))
        driver_factory.assert_not_called()

    def test_canvas_export_rejects_validated_deployment_config_drift_before_network(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version = self._mark_workflow_evidence_bound(
            imported.data["id"],
            status_value=WorkflowTemplateVersion.Status.VALIDATED,
        )
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8488",
            "timeout_seconds": 15,
        })
        self.deployment.save(update_fields=(
            "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))
        driver_factory = Mock()
        with patch("studio.platform_views.driver_for", driver_factory):
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{version.id}/comfyui-workflow/",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("漂移", str(response.data))
        driver_factory.assert_not_called()

    def test_saved_workflow_canvas_export_requires_staff_before_comfyui_read(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        user = get_user_model().objects.create_user(
            username="workflow-export-user",
            password="StrongPass123!",
        )
        self.client.force_authenticate(user)
        driver_factory = Mock()

        with patch("studio.platform_views.driver_for", driver_factory):
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/comfyui-workflow/",
                {"deployment_id": str(self.deployment.id)},
            )

        self.assertEqual(response.status_code, 403, response.data)
        driver_factory.assert_not_called()

    def test_saved_workflow_canvas_export_reports_object_info_failure(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        url = (
            f"/api/v1/workflow-template-versions/{imported.data['id']}"
            "/comfyui-workflow/"
        )
        driver = Mock()
        driver.invoke.side_effect = ProviderDriverError(
            "offline diagnostic must not leak",
        )
        with patch("studio.platform_views.driver_for", return_value=driver):
            response = self.client.get(
                url,
                {"deployment_id": str(self.deployment.id)},
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["error"]["code"], "comfyui_object_info_unavailable")
        self.assertNotIn("offline diagnostic must not leak", str(response.data))
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 0)

    def test_import_rejects_nested_camel_case_plaintext_secret(self):
        response = self._import(contract_extra={
            "provider": {"credentials": [{"bearerToken": "plaintext"}]},
        })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("bearerToken", str(response.data))

    def test_ref2va_live_validate_rejects_missing_smoke_reference_before_network(self):
        imported = self._import(h3_mode="Ref2VA")
        self.assertEqual(imported.status_code, 201, imported.data)
        driver = Mock()

        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.post(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Ref2VA", str(response.data))
        driver.invoke.assert_not_called()

    def test_ref2va_live_validate_accepts_managed_reference_before_network_probe(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "managed-reference.png"
            source.write_bytes(b"managed-reference")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = Asset.objects.create(
                owner=None,
                kind=Asset.Kind.REFERENCE,
                name="Managed smoke reference",
                local_path=str(source),
                mime_type="image/png",
                checksum=checksum,
                is_active=True,
            )
            imported = self._import(
                h3_mode="Ref2VA",
                smoke_asset_binding={
                    "asset_id": str(asset.id),
                    "category": "character",
                    "checksum": checksum,
                },
            )
            self.assertEqual(imported.status_code, 201, imported.data)
            driver = Mock()
            driver.invoke.side_effect = ProviderDriverError(
                "probe reached after preflight",
                code="provider_request_failed",
            )
            with override_settings(ASSET_LIBRARY_ROOT=root), patch(
                "studio.services.workflow_versions.driver_for",
                return_value=driver,
            ):
                response = self.client.post(
                    f"/api/v1/workflow-template-versions/{imported.data['id']}/validate/",
                    {"deployment_id": str(self.deployment.id)},
                    format="json",
                )

        self.assertEqual(response.status_code, 400, response.data)
        driver.invoke.assert_called_once_with(self.deployment, "object_info", {})

    def test_generic_import_requires_width_and_height_semantic_bindings(self):
        rejected = self._import(include_resolution_bindings=False)

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("image.height", str(rejected.data))
        self.assertIn("image.width", str(rejected.data))

    def test_import_validate_smoke_publish_and_list_hides_graph(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        object_info = {
            "CLIPTextEncode": {
                "input": {"required": {"text": ["STRING", {}]}},
                "output": [],
                "output_name": [],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["STRING", {}],
                    "filename_prefix": ["STRING", {}],
                }},
                "output": [],
                "output_name": [],
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}],
                }},
                "output": [],
                "output_name": [],
            },
        }
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }
        def invoke(_deployment, operation, _payload):
            return {
                "health": {"system": {"comfyui_version": "test"}},
                "object_info": object_info,
                "submit": {"request_id": _payload.get("submission_token")},
                "poll": {"status": "succeeded"},
                "collect": {"outputs": [{
                    "node_id": "2", "filename": "smoke.png", "subfolder": "", "type": "output",
                }]},
            }[operation]
        driver.invoke.side_effect = invoke
        with tempfile.TemporaryDirectory() as output_dir:
            Image.new("RGB", (2, 2), color="red").save(Path(output_dir) / "smoke.png")
            with override_settings(COMFYUI_OUTPUT_DIR=output_dir), patch(
                "studio.services.workflow_versions.driver_for", return_value=driver,
            ):
                validated = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)},
                    format="json",
                )
                self.assertEqual(validated.status_code, 200, validated.data)
                smoked = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    {"deployment_id": str(self.deployment.id)},
                    format="json",
                )
                self.assertEqual(smoked.status_code, 202, smoked.data)
                self.assertIn("Location", smoked.headers)
                attempt = claim_next_workflow_smoke("test-worker")
                self.assertIsNotNone(attempt)
                processed = process_workflow_smoke(attempt)
                self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.SUCCEEDED)
        published = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/publish/",
            {},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], WorkflowTemplateVersion.Status.PUBLISHED)

        listing = self.client.get("/api/v1/workflow-template-versions/")
        self.assertEqual(listing.status_code, 200)
        row = next(item for item in listing.data["results"] if item["id"] == version_id)
        self.assertNotIn("graph", row)
        detail = self.client.get(f"/api/v1/workflow-template-versions/{version_id}/")
        self.assertIn("graph", detail.data)
        rejected_update = self.client.patch(
            f"/api/v1/workflow-template-versions/{version_id}/",
            {"graph": {}},
            format="json",
        )
        self.assertEqual(rejected_update.status_code, 405)

    def test_failed_revalidation_and_resmoke_clear_stale_passed_evidence(self):
        imported = self._import()
        version_id = imported.data["id"]
        object_info = {
            "CLIPTextEncode": {
                "input": {"required": {"text": ["STRING", {}]}},
                "output": [],
                "output_name": [],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["STRING", {}],
                    "filename_prefix": ["STRING", {}],
                }},
                "output": [],
                "output_name": [],
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {}],
                    "height": ["INT", {}],
                    "batch_size": ["INT", {}],
                }},
                "output": [],
                "output_name": [],
            },
        }
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def success(_deployment, operation, _payload):
            return {
                "health": {"system": {"comfyui_version": "test"}},
                "object_info": object_info,
                "submit": {"request_id": _payload.get("submission_token")},
                "poll": {"status": "succeeded"},
                "collect": {"outputs": [{
                    "node_id": "2", "filename": "smoke.png", "subfolder": "",
                }]},
            }[operation]

        with tempfile.TemporaryDirectory() as output_dir:
            Image.new("RGB", (2, 2), color="green").save(Path(output_dir) / "smoke.png")
            with override_settings(COMFYUI_OUTPUT_DIR=output_dir), patch(
                "studio.services.workflow_versions.driver_for", return_value=driver,
            ):
                driver.invoke.side_effect = success
                self.assertEqual(self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)}, format="json",
                ).status_code, 200)
                driver.invoke.side_effect = ProviderDriverError("object_info unavailable")
                self.assertEqual(self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)}, format="json",
                ).status_code, 400)
                version = WorkflowTemplateVersion.objects.get(pk=version_id)
                self.assertEqual(version.validation_state, "failed")
                self.assertIsNone(version.validated_deployment_id)
                self.assertEqual(version.validation_evidence, {})
                self.assertEqual(version.smoke_test_evidence, {})

                driver.invoke.side_effect = success
                self.assertEqual(self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)}, format="json",
                ).status_code, 200)
                self.assertEqual(self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    {"deployment_id": str(self.deployment.id)}, format="json",
                ).status_code, 202)
                self.assertEqual(
                    process_workflow_smoke(
                        claim_next_workflow_smoke("success-worker"),
                    ).status,
                    WorkflowSmokeAttempt.Status.SUCCEEDED,
                )
                driver.invoke.side_effect = ProviderDriverError("smoke unavailable")
                self.assertEqual(self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    {"deployment_id": str(self.deployment.id)}, format="json",
                ).status_code, 202)
                self.assertEqual(
                    process_workflow_smoke(
                        claim_next_workflow_smoke("failure-worker"),
                    ).status,
                    WorkflowSmokeAttempt.Status.FAILED,
                )
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(version.smoke_test_state, "failed")
        self.assertEqual(version.smoke_test_evidence, {})
        self.assertEqual(version.validation_state, "not_run")
        self.assertIsNone(version.validated_deployment_id)
        self.assertEqual(version.validation_evidence, {})
        self.assertEqual(self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/publish/", {}, format="json",
        ).status_code, 400)
    def test_import_rejects_graph_over_64k(self):
        response = self.client.post("/api/v1/workflow-template-versions/", {
            "template": str(self.template.id),
            "provider": str(self.provider.id),
            "model_profile_version": str(self.profile.id),
            "graph": {
                "1": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": "x" * (64 * 1024)},
                },
            },
            "input_contract": {},
            "semantic_bindings": [],
            "model_requirements": [],
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)

    def test_validate_rejects_deployment_for_different_model_profile(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        h3_profile = ModelProfileVersion.objects.get(profile_key="minimax-h3-video", version=1)
        wrong = ModelDeployment(
            key="workflow-h3-wrong",
            name="Wrong H3 deployment",
            profile_version=h3_profile,
            runtime_host=self.host,
        )
        assign_json_contract(wrong, "runtime_config", {
            "base_url": "http://127.0.0.1:8188", "timeout_seconds": 10,
        })
        wrong.save()
        response = self.client.post(
            f"/api/v1/workflow-template-versions/{imported.data['id']}/validate/",
            {"deployment_id": str(wrong.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("精确绑定", str(response.data))

    def test_import_rejects_artifact_from_different_model_profile(self):
        foreign = ModelProfileVersion.objects.get(profile_key="minimax-h3-video", version=1).artifacts.first()
        response = self.client.post("/api/v1/workflow-template-versions/", {
            "template": str(self.template.id),
            "provider": str(self.provider.id),
            "model_profile_version": str(self.profile.id),
            "graph": {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"}}},
            "input_contract": {"schema_version": 1},
            "semantic_bindings": [],
            "model_requirements": [{
                "role": foreign.role,
                "alias": "foreign",
                "artifact_id": str(foreign.id),
                "expected_filename": foreign.filename,
                "expected_sha256": foreign.sha256,
                "is_required": True,
            }],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("必须属于", str(response.data))

    def test_contract_only_driver_cannot_publish(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        object_info = {
            "CLIPTextEncode": {
                "input": {"required": {"text": ["STRING", {}]}},
                "output": [],
                "output_name": [],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["STRING", {}],
                    "filename_prefix": ["STRING", {}],
                }},
                "output": [],
                "output_name": [],
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {}],
                    "height": ["INT", {}],
                    "batch_size": ["INT", {}],
                }},
                "output": [],
                "output_name": [],
            },
        }
        driver = Mock()
        driver.invoke.return_value = object_info
        driver.capabilities.return_value = {"operations": ["health", "object_info"]}
        with patch("studio.services.workflow_versions.driver_for", return_value=driver):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/validate/",
                {"deployment_id": str(self.deployment.id)}, format="json",
            )
            self.assertEqual(validated.status_code, 200, validated.data)
            smoked = self.client.post(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/smoke-test/",
                {"deployment_id": str(self.deployment.id)}, format="json",
            )
            self.assertEqual(smoked.status_code, 202, smoked.data)
            processed = process_workflow_smoke(
                claim_next_workflow_smoke("unsupported-worker"),
            )
            self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        published = self.client.post(
            f"/api/v1/workflow-template-versions/{imported.data['id']}/publish/", {}, format="json",
        )
        self.assertEqual(published.status_code, 400, published.data)

    def test_binding_edit_invalidates_all_live_and_smoke_evidence(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version = WorkflowTemplateVersion.objects.get(pk=imported.data["id"])
        version.status = WorkflowTemplateVersion.Status.PUBLISHED
        version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.validation_evidence = {"execution_contract_sha256": version.execution_contract_sha256}
        version.validation_evidence_sha256 = "a" * 64
        version.smoke_test_evidence = {"execution_contract_sha256": version.execution_contract_sha256}
        version.smoke_test_evidence_sha256 = "b" * 64
        version.is_current = True
        version.save()

        binding = WorkflowSemanticBinding.objects.get(
            workflow_version=version,
            semantic_key="prompt.positive",
        )
        binding.is_required = False
        binding.save(update_fields=("is_required", "updated_at"))

        version.refresh_from_db()
        self.assertEqual(version.status, WorkflowTemplateVersion.Status.DRAFT)
        self.assertEqual(version.validation_state, WorkflowTemplateVersion.ValidationState.NOT_RUN)
        self.assertEqual(version.smoke_test_state, WorkflowTemplateVersion.ValidationState.NOT_RUN)
        self.assertEqual(version.validation_evidence, {})
        self.assertEqual(version.smoke_test_evidence, {})
        self.assertFalse(version.is_current)


class PlatformTrustFoundationTests(TestCase):
    def test_bootstrap_workflows_never_claim_validation_or_smoke(self):
        versions = WorkflowTemplateVersion.objects.filter(template__is_system=True)
        self.assertTrue(versions.exists())
        self.assertFalse(versions.exclude(status=WorkflowTemplateVersion.Status.DRAFT).exists())
        self.assertFalse(versions.exclude(validation_state="not_run").exists())
        self.assertFalse(versions.exclude(smoke_test_state="not_run").exists())

    def test_managed_archive_survives_source_removal(self):
        with tempfile.TemporaryDirectory() as comfy_dir, tempfile.TemporaryDirectory() as media_dir:
            source = Path(comfy_dir) / "smoke.png"
            Image.new("RGB", (2, 2), color="blue").save(source)
            original = source.read_bytes()
            import hashlib
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            task = SimpleNamespace(
                id=uuid.uuid4(), owner_id=uuid.uuid4(), project_id=uuid.uuid4(),
                outputs=[{"filename": "smoke.png", "subfolder": "", "type": "output"}],
            )
            with override_settings(COMFYUI_OUTPUT_DIR=comfy_dir, MEDIA_ROOT=media_dir):
                archived = _archive_task_output(
                    task, 0, task.outputs[0], expected_kind="image",
                    expected_sha256=digest, max_bytes=1024,
                )
                source.unlink()
                managed = Path(media_dir) / archived["object_key"]
                self.assertEqual(managed.read_bytes(), original)
                self.assertEqual(archived["storage_backend"], "managed_local")

    def test_no_gpu_with_unverified_cloud_text_deployment_requires_smoke(self):
        host = RuntimeHost.objects.create(key="cpu-only", name="CPU only")
        snapshot = HostCapabilitySnapshot(
            host=host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.PARTIAL,
            gpu_count=0,
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1, "gpus": []})
        snapshot.save()
        profile = ModelProfileVersion.objects.get(profile_key="deepseek-text", version=1)
        cloud = ModelDeployment(
            key="cloud-text-test",
            name="Cloud text test",
            profile_version=profile,
            runtime_host=None,
        )
        assign_json_contract(cloud, "runtime_config", {})
        cloud.save()

        readiness = capability_readiness(snapshot)

        self.assertEqual(readiness["capabilities"]["text"]["state"], "smoke_required")
        self.assertEqual(readiness["capabilities"]["text"]["cloud_deployment_count"], 0)
        self.assertNotEqual(readiness["capabilities"]["text"]["state"], "blocked")
