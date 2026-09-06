import hashlib
import json
import threading
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid
from unittest.mock import Mock, call, patch

from PIL import Image

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import close_old_connections
from django.db.models.deletion import ProtectedError
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.utils import timezone
from rest_framework.test import APIClient
from studio.tests.workflow_projector_fixtures import workflow_projector_reference

from studio.models import (
    Asset,
    ConfigDefinition,
    ConfigRevision,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    ProductionRecipeVersion,
    PromptProfileVersion,
    ProviderProfile,
    RuntimeHost,
    RuntimeModelAvailability,
    SeriesProject,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowSmokeAttempt,
    WorkflowSmokeAttemptResolution,
    WorkflowSmokeAssetBinding,
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
    ComfyUIDriver,
    ProviderDriverError,
    _PinnedHTTPSConnection,
    _comfyui_failure_message,
    _comfyui_poll_failure,
    _request_json,
    _resolved_provider_endpoint,
    credential_binding_sha256,
    validate_endpoint,
)
from studio.services.runtime_platform import capability_readiness
from studio.services.security_boundaries import boundary_int
from studio.services.secret_store import rotate_secret_binding
from studio.services.task_queue import comfy_client_for_task
from studio.services.workflow_smoke_queue import (
    _failure_outcome,
    claim_next_workflow_smoke,
    process_workflow_smoke,
)
from studio.services.workflow_versions import (
    _execution_target_sha256,
    import_workflow_version,
    verify_workflow_version_integrity,
)
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring
from studio.tests.factories import conditioning_contract


class ConfigurationAndProviderTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.user = get_user_model().objects.create_user(
            username="platform-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=self.user, title="平台配置测试", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)

    def test_workflow_smoke_http_4xx_after_submit_start_is_definitive(self):
        attempt = SimpleNamespace(
            submission_started_at=timezone.now(),
            submitted_at=None,
            provider_request_id="",
            status=WorkflowSmokeAttempt.Status.SUBMITTING,
        )

        outcome = _failure_outcome(
            attempt,
            ProviderDriverError(
                "ComfyUI HTTP 400: prompt validation failed",
                code="provider_request_rejected",
            ),
        )

        self.assertEqual(outcome[0], WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(outcome[1], "provider_request_rejected")

        post_submit_rejection = _failure_outcome(
            SimpleNamespace(
                submission_started_at=attempt.submission_started_at,
                submitted_at=timezone.now(),
                provider_request_id="remote-request-id",
                status=WorkflowSmokeAttempt.Status.RUNNING,
            ),
            ProviderDriverError(
                "ComfyUI HTTP 404 while polling",
                code="provider_request_rejected",
            ),
        )
        self.assertEqual(
            post_submit_rejection[0],
            WorkflowSmokeAttempt.Status.UNCERTAIN,
        )
        self.assertEqual(post_submit_rejection[1], "remote_state_uncertain")

        uncertain = _failure_outcome(
            attempt,
            ProviderDriverError(
                "submit transport timeout",
                code="comfyui_operation_failed",
                submission_uncertain=True,
            ),
        )
        self.assertEqual(uncertain[0], WorkflowSmokeAttempt.Status.UNCERTAIN)
        self.assertEqual(uncertain[1], "submission_uncertain")

        release_failure = _failure_outcome(
            attempt,
            ProviderDriverError(
                "resource release was not confirmed; /prompt was not called",
                code="comfyui_resource_release_unconfirmed",
                retryable=False,
                submission_uncertain=False,
            ),
        )
        self.assertEqual(release_failure[0], WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(
            release_failure[1],
            "comfyui_resource_release_unconfirmed",
        )

    def test_comfyui_history_failure_projects_node_reason_and_recovery(self):
        failure = _comfyui_poll_failure({
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [[
                    "execution_error",
                    {
                        "node_id": "6",
                        "node_type": "UntrustedRemoteNodeType",
                        "exception_type": "ValueError",
                        "exception_message": (
                            "REF2VA requires at least one reference media input "
                            "token=do-not-expose E:\\private\\model.safetensors"
                        ),
                        "traceback": ["Traceback with an internal filesystem path"],
                    },
                ]],
            },
        })
        message = _comfyui_failure_message(
            failure,
            graph={"6": {
                "class_type": "MiniMaxH3AudioConditioningT8",
                "inputs": {},
            }},
        )

        self.assertEqual(failure["kind"], "execution_error")
        self.assertNotIn("traceback", failure)
        self.assertIn("节点 6（MiniMaxH3AudioConditioningT8）", message)
        self.assertNotIn("UntrustedRemoteNodeType", message)
        self.assertIn("Ref2VA 模式至少需要 1 个参考图片、参考视频或参考音频", message)
        self.assertIn("本次 Smoke 输入", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("do-not-expose", message)
        self.assertNotIn("E:\\private", message)
        self.assertNotIn("Traceback", message)

    def test_comfyui_history_failure_without_safe_event_uses_empty_projection(self):
        self.assertEqual(_comfyui_poll_failure({
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", "not-an-object"]],
            },
        }), {})

    def test_workflow_smoke_failure_unwraps_coded_provider_message(self):
        attempt = SimpleNamespace(
            submission_started_at=timezone.now(),
            submitted_at=timezone.now(),
            provider_request_id="remote-request-id",
            status=WorkflowSmokeAttempt.Status.RUNNING,
        )
        try:
            try:
                raise ProviderDriverError(
                    "ComfyUI 节点 6 执行失败。原因：缺少参考素材。处理建议：补充素材。",
                    code="provider_smoke_failed",
                )
            except ProviderDriverError as provider_error:
                raise DjangoValidationError({
                    "detail": str(provider_error),
                }) from provider_error
        except DjangoValidationError as wrapped:
            outcome = _failure_outcome(attempt, wrapped)

        self.assertEqual(outcome[0], WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(outcome[1], "provider_smoke_failed")
        self.assertEqual(
            outcome[2],
            "ComfyUI 节点 6 执行失败。原因：缺少参考素材。处理建议：补充素材。",
        )
        self.assertNotIn("{'detail'", outcome[2])

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
    @patch("studio.services.provider_registry.requests.Session.request")
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

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
    @patch("studio.services.provider_registry._peer_address", return_value="10.20.30.40")
    @patch("studio.services.provider_registry.socket.getaddrinfo")
    @patch("studio.services.provider_registry.requests.Session.request")
    def test_provider_request_rejects_dns_rebinding_without_second_resolution(
        self, request, getaddrinfo, _peer_address,
    ):
        getaddrinfo.side_effect = [
            [(2, 1, 6, "", ("93.184.216.34", 443))],
            [(2, 1, 6, "", ("10.20.30.40", 443))],
        ]
        response = Mock()
        request.return_value = response

        with self.assertRaises(ProviderDriverError) as context:
            _request_json("POST", "https://approved.example/v1/test", timeout=1, payload={})

        self.assertEqual(context.exception.code, "provider_peer_mismatch")
        self.assertEqual(getaddrinfo.call_count, 1)
        response.close.assert_called_once()

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
    @patch("studio.services.provider_registry.socket.socket")
    @patch("studio.services.provider_registry.socket.getaddrinfo")
    def test_provider_https_connect_uses_filtered_numeric_address_and_original_host(
        self, getaddrinfo, socket_factory,
    ):
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        resolved = _resolved_provider_endpoint("https://approved.example")
        connection = _PinnedHTTPSConnection(
            host=resolved.hostname,
            port=resolved.port,
            timeout=1,
            resolved_addresses=resolved.addresses,
        )

        connected = connection._new_conn()

        self.assertIs(connected, socket_factory.return_value)
        self.assertEqual(connection.host, "approved.example")
        self.assertEqual(getaddrinfo.call_count, 1)
        socket_factory.assert_called_once_with(2, 1, 6)
        socket_factory.return_value.connect.assert_called_once_with(("93.184.216.34", 443))

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
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
            "pre_submit_resource_release": {
                "schema_version": 1,
                "enabled": True,
                "unload_models": True,
                "free_memory": True,
            },
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
        self.assertEqual(client.pre_submit_resource_release_policy, {
            "schema_version": 1,
            "enabled": True,
            "unload_models": True,
            "free_memory": True,
        })

        deployment.runtime_config = {"base_url": "http://127.0.0.1:9999"}
        deployment.save(update_fields=("runtime_config", "updated_at"))
        frozen_client = comfy_client_for_task(task)
        self.assertEqual(frozen_client.base_url, "http://127.0.0.1:8188")
        self.assertTrue(frozen_client.pre_submit_resource_release_policy["enabled"])

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

    def _reviewed_delete(self, resource_type: str, object_id):
        preview = self.client.post("/api/v1/registry-deletion-preview/", {
            "resource_type": resource_type,
            "object_id": str(object_id),
        }, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        selected = [
            row["dependency_key"] for row in preview.data["dependencies"]
            if row["disposition"] == "selectable_delete"
        ]
        return self.client.post("/api/v1/registry-deletion-execute/", {
            "resource_type": resource_type,
            "object_id": str(object_id),
            "expected_impact_sha256": preview.data["impact_sha256"],
            "selected_dependency_keys": selected,
            "confirmation_phrase": preview.data["confirmation_phrase"],
        }, format="json", HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup")

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
            "runtime_source": "comfyui",
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        deployment.save()
        return deployment

    def _provider_with_active_deployment(self, key: str):
        provider = ProviderProfile(
            key=f"{key}-provider",
            name=f"{key} provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,submit,poll",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"{key}-profile",
            version=1,
            provider=provider,
            model_identifier=f"{key}.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"{key}-deployment",
            name=f"{key} deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        return provider, profile, deployment

    def test_model_profile_options_are_server_authored(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        draft_only = ModelProfileVersion(
            profile_key="draft-only-registry-facts",
            version=1,
            provider=provider,
            model_identifier="draft-only.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft_only, "specification", {
            "schema_version": 1,
            "capabilities": ["draft.rogue"],
            "model_family": "draft-only-family",
        })
        draft_only.save()
        PromptProfileVersion.objects.create(
            profile_key="draft-only-prompt-profile",
            version=1,
            name="Draft-only prompt profile",
            model_family="draft-only-prompt-family",
            operation="image.text_to_image",
            status=PromptProfileVersion.Status.DRAFT,
        )
        published_legacy = ModelProfileVersion(
            profile_key="published-legacy-registry-facts",
            version=1,
            provider=provider,
            model_identifier="published-legacy.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(published_legacy, "specification", {
            "schema_version": 1,
            "capabilities": ["legacy.image.operation"],
            "model_family": "published-legacy-family",
        })
        published_legacy.save()
        response = self.client.get("/api/v1/model-profile-versions/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["specification_schema_version"], 1)
        self.assertIn(
            "audio",
            {item["value"] for item in response.data["modalities"]},
        )
        self.assertIn(
            "checkpoint",
            {item["value"] for item in response.data["artifact_roles"]},
        )
        capabilities = {
            item["value"]: item for item in response.data["capabilities"]
        }
        self.assertIn("prompt.normalize", capabilities)
        self.assertIn("video.reference_to_video", capabilities)
        self.assertFalse(capabilities["prompt.normalize"]["legacy"])
        self.assertNotIn("draft.rogue", capabilities)
        self.assertTrue(capabilities["legacy.image.operation"]["legacy"])
        self.assertNotIn("draft-only-family", response.data["model_families"])
        self.assertNotIn(
            "draft-only-prompt-family",
            response.data["model_families"],
        )
        self.assertIn("published-legacy-family", response.data["model_families"])
        drivers = {
            item["value"]: item for item in response.data["provider_drivers"]
        }
        self.assertIn(ProviderProfile.DriverType.COMFYUI, drivers)
        self.assertIn("submit", drivers[ProviderProfile.DriverType.COMFYUI]["operations"])

        non_staff = get_user_model().objects.create_user(
            username="model-options-viewer",
            password="StrongPass123!",
        )
        self.client.force_authenticate(non_staff)
        forbidden = self.client.get("/api/v1/model-profile-versions/options/")
        self.assertEqual(forbidden.status_code, 403, forbidden.data)
        self.client.force_authenticate(self.admin)

    def test_model_profile_version_numbers_are_allocated_by_server(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        payload = {
            "profile_key": "server-allocated-image",
            "version": 91,
            "provider": str(provider.id),
            "model_identifier": "server-allocated-v1.safetensors",
            "modality": ModelProfileVersion.Modality.IMAGE,
            "specification": {
                "capabilities": ["image.text_to_image"],
                "model_family": "server-allocated-image",
            },
        }

        unknown_capability = self.client.post(
            "/api/v1/model-profile-versions/",
            {
                **payload,
                "profile_key": "server-allocated-unknown-capability",
                "specification": {
                    "capabilities": ["user.registered.capability"],
                    "model_family": "server-allocated-unknown-capability",
                },
            },
            format="json",
        )
        self.assertEqual(unknown_capability.status_code, 400, unknown_capability.data)

        incompatible_capability = self.client.post(
            "/api/v1/model-profile-versions/",
            {
                **payload,
                "profile_key": "server-allocated-incompatible-capability",
                "specification": {
                    "capabilities": ["script.generate"],
                    "model_family": "server-allocated-incompatible-capability",
                },
            },
            format="json",
        )
        self.assertEqual(
            incompatible_capability.status_code,
            400,
            incompatible_capability.data,
        )

        unknown_family = self.client.post(
            "/api/v1/model-profile-versions/",
            {
                **payload,
                "profile_key": "server-allocated-family-selection",
                "specification": {
                    "capabilities": ["image.text_to_image"],
                    "model_family": "unregistered-strategy-family",
                },
            },
            format="json",
        )
        self.assertEqual(unknown_family.status_code, 400, unknown_family.data)

        created = self.client.post(
            "/api/v1/model-profile-versions/",
            payload,
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["version"], 1)
        self.assertEqual(created.data["specification"]["schema_version"], 1)

        duplicate = self.client.post(
            "/api/v1/model-profile-versions/",
            {**payload, "version": 92},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertIn("新建下一版本", str(duplicate.data))

        source = ModelProfileVersion.objects.get(pk=created.data["id"])
        retired_highest = ModelProfileVersion(
            profile_key=source.profile_key,
            version=7,
            provider=provider,
            model_identifier="server-allocated-v7.safetensors",
            modality=source.modality,
            status=ModelProfileVersion.Status.RETIRED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(retired_highest, "specification", source.specification)
        retired_highest.save()

        invalid_schema = self.client.post(
            f"/api/v1/model-profile-versions/{retired_highest.id}/draft-next/",
            {"specification": {**source.specification, "schema_version": 2}},
            format="json",
        )
        self.assertEqual(invalid_schema.status_code, 400, invalid_schema.data)

        next_version = self.client.post(
            f"/api/v1/model-profile-versions/{retired_highest.id}/draft-next/",
            {"model_identifier": "server-allocated-v8.safetensors"},
            format="json",
        )
        self.assertEqual(next_version.status_code, 201, next_version.data)
        self.assertEqual(next_version.data["version"], 8)
        self.assertEqual(next_version.data["profile_key"], source.profile_key)
        self.assertEqual(
            next_version.data["specification"],
            source.specification,
        )

        following = self.client.post(
            f"/api/v1/model-profile-versions/{retired_highest.id}/draft-next/",
            {},
            format="json",
        )
        self.assertEqual(following.status_code, 409, following.data)
        self.assertEqual(
            following.data["error"]["code"],
            "model_profile_source_stale",
        )
        duplicate_draft = self.client.post(
            f"/api/v1/model-profile-versions/{next_version.data['id']}/draft-next/",
            {},
            format="json",
        )
        self.assertEqual(duplicate_draft.status_code, 409, duplicate_draft.data)
        self.assertEqual(
            duplicate_draft.data["error"]["code"],
            "model_profile_draft_already_exists",
        )
        self.assertFalse(ModelProfileVersion.objects.filter(
            profile_key=source.profile_key,
            version=9,
        ).exists())

        version_edit = self.client.patch(
            f"/api/v1/model-profile-versions/{next_version.data['id']}/",
            {"version": 99},
            format="json",
        )
        self.assertEqual(version_edit.status_code, 400, version_edit.data)
        family_edit = self.client.patch(
            f"/api/v1/model-profile-versions/{next_version.data['id']}/",
            {"profile_key": "other-family"},
            format="json",
        )
        self.assertEqual(family_edit.status_code, 400, family_edit.data)

    def test_model_next_version_preserves_only_source_legacy_capabilities(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        source = ModelProfileVersion(
            profile_key="legacy-next-version",
            version=4,
            provider=provider,
            model_identifier="legacy-source.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(source, "specification", {
            "schema_version": 1,
            "capabilities": ["legacy.image.operation"],
            "model_family": "legacy-next-version",
        })
        source.save()

        created = self.client.post(
            f"/api/v1/model-profile-versions/{source.id}/draft-next/",
            {},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["version"], 5)
        self.assertEqual(
            created.data["specification"]["capabilities"],
            ["legacy.image.operation"],
        )
        preserved = self.client.patch(
            f"/api/v1/model-profile-versions/{created.data['id']}/",
            {"model_identifier": "legacy-edited.safetensors"},
            format="json",
        )
        self.assertEqual(preserved.status_code, 200, preserved.data)
        rejected = self.client.patch(
            f"/api/v1/model-profile-versions/{created.data['id']}/",
            {"specification": {
                **created.data["specification"],
                "capabilities": [
                    "legacy.image.operation",
                    "new.unregistered.operation",
                ],
            }},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

    def test_model_next_version_rejects_corrupted_source_contract(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        source = ModelProfileVersion(
            profile_key="corrupted-next-version",
            version=2,
            provider=provider,
            model_identifier="corrupted-source.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(source, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
            "model_family": "corrupted-next-version",
        })
        source.save()
        ModelProfileVersion.objects.filter(pk=source.pk).update(
            specification_sha256="0" * 64,
        )

        response = self.client.post(
            f"/api/v1/model-profile-versions/{source.id}/draft-next/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "model_profile_source_contract_invalid",
        )
        self.assertEqual(ModelProfileVersion.objects.filter(
            profile_key=source.profile_key,
        ).count(), 1)

    def test_provider_registry_fields_reject_new_free_text_values(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        accepted = self.client.patch(
            f"/api/v1/provider-profiles/{provider.id}/",
            {"name": "Local ComfyUI renamed"},
            format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        rejected_operation = self.client.patch(
            f"/api/v1/provider-profiles/{provider.id}/",
            {"operations": f"{provider.operations},user_defined_transport"},
            format="json",
        )
        self.assertEqual(
            rejected_operation.status_code,
            400,
            rejected_operation.data,
        )
        rejected_key = self.client.patch(
            f"/api/v1/provider-profiles/{provider.id}/",
            {"key": "renamed-provider-key"},
            format="json",
        )
        self.assertEqual(rejected_key.status_code, 400, rejected_key.data)

    def test_model_deployment_api_rejects_corrupted_provider_contract(self):
        provider, profile, _deployment = self._provider_with_active_deployment(
            "deployment-authoritative-validation",
        )
        valid = self.client.post("/api/v1/model-deployments/", {
            "key": "deployment-authoritative-valid",
            "name": "Deployment authoritative valid",
            "profile_version": str(profile.id),
            "runtime_config": {},
            "priority": 0,
            "is_default": False,
            "status": ModelDeployment.Status.ACTIVE,
        }, format="json")
        self.assertEqual(valid.status_code, 201, valid.data)

        ProviderProfile.objects.filter(pk=provider.pk).update(
            driver_config_sha256="0" * 64,
        )

        response = self.client.post("/api/v1/model-deployments/", {
            "key": "deployment-corrupt-provider-contract",
            "name": "Deployment corrupt provider contract",
            "profile_version": str(profile.id),
            "runtime_config": {},
            "priority": 0,
            "is_default": False,
            "status": ModelDeployment.Status.ACTIVE,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ModelDeployment.objects.filter(
            key="deployment-corrupt-provider-contract",
        ).exists())

    def test_provider_contract_change_revalidates_active_deployments_and_allows_disable(self):
        provider, _profile, _deployment = self._provider_with_active_deployment(
            "provider-dependent-validation",
        )
        updated_config = {
            **provider.driver_config,
            "timeout_seconds": 45,
        }
        accepted = self.client.patch(
            f"/api/v1/provider-profiles/{provider.id}/",
            {"driver_config": updated_config},
            format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        provider.refresh_from_db()
        original_config = dict(provider.driver_config)
        original_sha256 = provider.driver_config_sha256
        incomplete_config = {"runtime_source": "deployment"}
        endpoint = f"/api/v1/provider-profiles/{provider.id}/"

        rejected = self.client.patch(
            endpoint,
            {"driver_config": incomplete_config},
            format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        provider.refresh_from_db()
        self.assertTrue(provider.is_active)
        self.assertEqual(provider.driver_config, original_config)
        self.assertEqual(provider.driver_config_sha256, original_sha256)

        disabled = self.client.patch(
            endpoint,
            {"driver_config": incomplete_config, "is_active": False},
            format="json",
        )

        self.assertEqual(disabled.status_code, 200, disabled.data)
        provider.refresh_from_db()
        self.assertFalse(provider.is_active)
        self.assertEqual(provider.driver_config, incomplete_config)

        reactivated = self.client.patch(
            endpoint,
            {"is_active": True},
            format="json",
        )

        self.assertEqual(reactivated.status_code, 400, reactivated.data)
        provider.refresh_from_db()
        self.assertFalse(provider.is_active)
        self.assertEqual(provider.driver_config, incomplete_config)

    def test_deployment_with_invalid_execution_contract_can_still_be_disabled(self):
        provider, _profile, deployment = self._provider_with_active_deployment(
            "deployment-disable-invalid-contract",
        )
        ProviderProfile.objects.filter(pk=provider.pk).update(
            driver_config={"runtime_source": "deployment"},
        )

        disabled = self.client.patch(
            f"/api/v1/model-deployments/{deployment.id}/",
            {"status": ModelDeployment.Status.DISABLED},
            format="json",
        )

        self.assertEqual(disabled.status_code, 200, disabled.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.status, ModelDeployment.Status.DISABLED)
        rejected_reactivation = self.client.patch(
            f"/api/v1/model-deployments/{deployment.id}/",
            {"status": ModelDeployment.Status.ACTIVE},
            format="json",
        )
        self.assertEqual(rejected_reactivation.status_code, 400, rejected_reactivation.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.status, ModelDeployment.Status.DISABLED)

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
                self.assertEqual(deleted_profile.status_code, 405, deleted_profile.data)
                patched_artifact = self.client.patch(
                    f"/api/v1/model-artifacts/{artifact.id}/",
                    {"filename": f"forbidden-{lifecycle}.safetensors"},
                    format="json",
                )
                self.assertEqual(patched_artifact.status_code, 400, patched_artifact.data)
                deleted_artifact = self.client.delete(
                    f"/api/v1/model-artifacts/{artifact.id}/",
                )
                self.assertEqual(deleted_artifact.status_code, 405, deleted_artifact.data)

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
            {"filename": "nested\\draft-v2.safetensors"},
            format="json",
        )
        self.assertEqual(artifact_response.status_code, 200, artifact_response.data)
        self.assertEqual(artifact_response.data["filename"], "nested/draft-v2.safetensors")
        profile_response = self.client.patch(
            f"/api/v1/model-profile-versions/{profile.id}/",
            {"model_identifier": "draft-v2"},
            format="json",
        )
        self.assertEqual(profile_response.status_code, 200, profile_response.data)
        deleted_artifact = self._reviewed_delete(
            "model_artifact",
            artifact.id,
        )
        self.assertEqual(deleted_artifact.status_code, 200, deleted_artifact.data)

    def test_draft_artifact_locator_change_invalidates_verification_and_preserves_observation(self):
        provider = ProviderProfile.objects.get(key="local-comfyui")
        profile = ModelProfileVersion(
            profile_key="draft-artifact-relocation",
            version=1,
            provider=provider,
            model_identifier="draft-artifact-relocation-v1",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        host = RuntimeHost.objects.create(
            key="artifact-relocation-host",
            name="Artifact relocation host",
            status=RuntimeHost.Status.ACTIVE,
        )
        snapshot = HostCapabilitySnapshot(
            host=host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="relocated-checkpoint",
            filename="old/model.safetensors",
            sha256="a" * 64,
            size_bytes=128,
            status=ModelArtifact.Status.AVAILABLE,
            verified_snapshot=snapshot,
            verified_at=timezone.now(),
            verified_by=self.admin,
        )
        source_profile = self._profile("artifact-relocation-source")
        deployment = self._deployment(
            "artifact-relocation-source",
            source_profile,
            host,
        )
        availability = RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key="artifact-relocation-observation",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=128,
            observed_sha256="a" * 64,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )

        response = self.client.patch(
            f"/api/v1/model-artifacts/{artifact.id}/",
            {"filename": "new\\model.safetensors"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        artifact.refresh_from_db()
        self.assertEqual(artifact.filename, "new/model.safetensors")
        self.assertEqual(artifact.status, ModelArtifact.Status.DECLARED)
        self.assertIsNone(artifact.verified_snapshot_id)
        self.assertIsNone(artifact.verified_at)
        self.assertIsNone(artifact.verified_by_id)
        self.assertEqual(artifact.sha256, "a" * 64)
        self.assertEqual(artifact.size_bytes, 128)
        self.assertTrue(RuntimeModelAvailability.objects.filter(pk=availability.pk).exists())
        event = PlatformLifecycleAuditEvent.objects.get(
            object_type="model_artifact",
            object_id=artifact.id,
            action="update",
        )
        self.assertNotEqual(
            event.before_summary["locator_sha256"],
            event.after_summary["locator_sha256"],
        )
        self.assertNotIn("old/model.safetensors", json.dumps(event.before_summary))
        self.assertNotIn("new/model.safetensors", json.dumps(event.after_summary))
        blocked_delete = self.client.delete(
            f"/api/v1/model-artifacts/{artifact.id}/",
        )
        self.assertEqual(blocked_delete.status_code, 405, blocked_delete.data)
        blocked_profile_delete = self.client.delete(
            f"/api/v1/model-profile-versions/{profile.id}/",
        )
        self.assertEqual(blocked_profile_delete.status_code, 405, blocked_profile_delete.data)

    def test_deployment_retire_is_terminal_and_preserves_the_record(self):
        profile = self._profile("deployment-retire-profile")
        host = RuntimeHost.objects.create(
            key="deployment-retire-host",
            name="Deployment retire host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment(
            "deployment-retire-terminal",
            profile,
            host,
            status=ModelDeployment.Status.DISABLED,
        )
        deployment.is_default = True
        deployment.save(update_fields=("is_default", "updated_at"))

        response = self.client.post(
            f"/api/v1/model-deployments/{deployment.id}/retire/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.status, ModelDeployment.Status.RETIRED)
        self.assertFalse(deployment.is_default)
        self.assertEqual(self.client.patch(
            f"/api/v1/model-deployments/{deployment.id}/",
            {"name": "forbidden"},
            format="json",
        ).status_code, 400)
        self.assertEqual(self.client.delete(
            f"/api/v1/model-deployments/{deployment.id}/",
        ).status_code, 405)
        self.assertTrue(ModelDeployment.objects.filter(pk=deployment.pk).exists())
        self.assertTrue(PlatformLifecycleAuditEvent.objects.filter(
            object_type="model_deployment",
            object_id=deployment.id,
            action="retire",
        ).exists())

    def test_deployment_retire_rejects_live_recipe_reference(self):
        profile = self._profile("deployment-retire-referenced-profile")
        host = RuntimeHost.objects.create(
            key="deployment-retire-referenced-host",
            name="Deployment retire referenced host",
            status=RuntimeHost.Status.ACTIVE,
        )
        deployment = self._deployment(
            "deployment-retire-referenced",
            profile,
            host,
            status=ModelDeployment.Status.DISABLED,
        )
        recipe = ProductionRecipeVersion(
            recipe_key="deployment-retire-reference",
            version=1,
            name="Deployment retire reference",
            status=ProductionRecipeVersion.Status.DRAFT,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "model_deployment_id": str(deployment.id),
        })
        recipe.save()

        response = self.client.post(
            f"/api/v1/model-deployments/{deployment.id}/retire/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.status, ModelDeployment.Status.DISABLED)

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
        self.assertEqual(response.status_code, 405, response.data)

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
        deployment.status = ModelDeployment.Status.ERROR
        deployment.save(update_fields=("status", "updated_at"))
        blocked_error = self.client.post(
            f"/api/v1/model-profile-versions/{profile.id}/retire/", {}, format="json",
        )
        self.assertEqual(blocked_error.status_code, 409, blocked_error.data)
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
        ).status_code, 405)

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
        ).status_code, 405)

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
        ).status_code, 405)

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
        self.assertEqual(protected.status_code, 405, protected.data)
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
        self.assertEqual(protected.status_code, 405, protected.data)
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
            "driver_config": {
                "base_url": "http://127.0.0.1:8188",
                "timeout_seconds": 30,
                "poll_interval_seconds": 1,
                "max_poll_seconds": 60,
                "capability_ttl_seconds": 300,
            },
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
        deleted = self._reviewed_delete(
            "provider_profile",
            provider_id,
        )
        self.assertEqual(deleted.status_code, 200, deleted.data)

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
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
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

    def test_recipe_editor_catalog_and_successor_are_server_owned(self):
        options = self.client.get("/api/v1/production-recipes/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["definition_schema_version"], 1)
        stage_options = {
            item["value"]: item for item in options.data["stage_options"]
        }
        self.assertIn("script_generate", stage_options)
        self.assertIn("ai", stage_options["script_generate"]["modes"])
        self.assertTrue(stage_options["media_generation"]["requires_routing"])
        routed_preset_stages = [
            stage
            for preset in options.data["presets"]
            for stage in preset["stages"]
            if "routing" in stage
        ]
        self.assertTrue(routed_preset_stages)
        self.assertTrue(all(
            stage["routing"]["variant_count"] == 1
            for stage in routed_preset_stages
        ))

        created = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "server-owned-recipe-version",
            "version": 91,
            "name": "Server-owned recipe version",
            "definition": {"schema_version": 1, "stages": []},
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["version"], 1)
        duplicate = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "server-owned-recipe-version",
            "version": 92,
            "name": "Must use successor",
            "definition": {"schema_version": 1, "stages": []},
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)

        source = ProductionRecipeVersion.objects.get(pk=created.data["id"])
        source.status = ProductionRecipeVersion.Status.PUBLISHED
        source.published_at = timezone.now()
        source.published_by = self.admin
        source.save(update_fields=(
            "status", "published_at", "published_by", "updated_at",
        ))
        original_definition = source.definition
        ProductionRecipeVersion.objects.filter(pk=source.pk).update(
            definition={**original_definition, "unexpected_drift": True},
        )
        invalid_contract = self.client.post(
            f"/api/v1/production-recipes/{source.id}/clone-successor/",
            {"expected_definition_sha256": source.definition_sha256},
            format="json",
        )
        self.assertEqual(invalid_contract.status_code, 409, invalid_contract.data)
        self.assertEqual(
            invalid_contract.data["error"]["code"],
            "recipe_successor_source_contract_invalid",
        )
        self.assertFalse(ProductionRecipeVersion.objects.filter(
            recipe_key=source.recipe_key,
            status=ProductionRecipeVersion.Status.DRAFT,
        ).exists())
        ProductionRecipeVersion.objects.filter(pk=source.pk).update(
            definition=original_definition,
        )
        source.refresh_from_db()
        successor = self.client.post(
            f"/api/v1/production-recipes/{source.id}/clone-successor/",
            {"expected_definition_sha256": source.definition_sha256},
            format="json",
        )
        self.assertEqual(successor.status_code, 201, successor.data)
        self.assertEqual(successor.data["recipe_key"], source.recipe_key)
        self.assertEqual(successor.data["version"], 2)
        self.assertEqual(successor.data["status"], ProductionRecipeVersion.Status.DRAFT)
        version_edit = self.client.patch(
            f"/api/v1/production-recipes/{successor.data['id']}/",
            {"version": 99},
            format="json",
        )
        self.assertEqual(version_edit.status_code, 400, version_edit.data)
        conflict = self.client.post(
            f"/api/v1/production-recipes/{source.id}/clone-successor/",
            {"expected_definition_sha256": source.definition_sha256},
            format="json",
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(
            conflict.data["error"]["code"],
            "recipe_successor_draft_exists",
        )

    def test_config_definition_options_and_csv_contract_are_server_owned(self):
        ConfigDefinition.objects.create(
            key="catalog.existing",
            domain="catalog",
            value_type=ConfigDefinition.ValueType.INTEGER,
            description="Catalog option source",
            unit="items",
            impact_modules=["api", "worker"],
            allowed_scopes="system,project",
        )
        options = self.client.get("/api/v1/config-definitions/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn(
            ConfigDefinition.ValueType.INTEGER,
            {item["value"] for item in options.data["value_types"]},
        )
        self.assertIn("catalog", {
            item["value"] for item in options.data["domains"]
        })
        self.assertIn("items", {
            item["value"] for item in options.data["units"]
        })
        self.assertEqual(
            options.data["system_defaults"]["allowed_scopes"],
            "system,project,run",
        )
        self.assertEqual(
            options.data["custom_entry_fields"],
            ["domain", "unit", "impact_modules"],
        )

        created = self.client.post("/api/v1/config-definitions/", {
            "key": "catalog.canonical",
            "domain": "catalog",
            "value_type": "integer",
            "description": "Canonical selection test",
            "impact_modules": ["worker"],
            "allowed_scopes": "run,system,run",
            "allowed_default_sources": "workflow,model,workflow",
            "value_schema": {"minimum": 1},
            "initial_value": 1,
            "is_active": False,
            "needs_review": True,
            "review_reason": "client supplied lifecycle",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["allowed_scopes"], "system,run")
        self.assertEqual(
            created.data["allowed_default_sources"], "model,workflow",
        )
        self.assertFalse(created.data["has_values"])
        self.assertTrue(created.data["is_active"])
        self.assertFalse(created.data["needs_review"])
        self.assertEqual(created.data["review_reason"], "")
        self.assertIn("value_type", created.data["mutable_fields"])
        self.assertNotIn("configuration_class", created.data["mutable_fields"])
        locked_class = self.client.patch(
            f"/api/v1/config-definitions/{created.data['id']}/",
            {"configuration_class": "security_boundary"},
            format="json",
        )
        self.assertEqual(locked_class.status_code, 400, locked_class.data)
        self.assertIn("configuration_class", str(locked_class.data))
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"catalog.canonical": 2},
            created_by=self.admin,
        )
        self.assertEqual(revision.values.filter(
            definition_id=created.data["id"],
        ).count(), 1)
        ConfigDefinition.objects.filter(pk=created.data["id"]).update(
            allowed_scopes="run,system,run",
            allowed_default_sources="workflow,model,workflow",
        )
        same_structure = self.client.patch(
            f"/api/v1/config-definitions/{created.data['id']}/",
            {
                "domain": "catalog",
                "configuration_class": "standard",
                "value_type": "integer",
                "description": "Description-only update",
                "impact_modules": [" worker ", "worker"],
                "allowed_scopes": "run,system,run",
                "allowed_default_sources": "workflow,model,workflow",
                "restart_required": False,
                "is_required": False,
                "is_secret": False,
                "value_schema": {"minimum": 1},
                "initial_value": 1,
            },
            format="json",
        )
        self.assertEqual(same_structure.status_code, 200, same_structure.data)
        self.assertEqual(same_structure.data["description"], "Description-only update")
        self.assertEqual(same_structure.data["impact_modules"], ["worker"])
        self.assertTrue(same_structure.data["has_values"])
        self.assertNotIn("value_type", same_structure.data["mutable_fields"])
        structural_change = self.client.patch(
            f"/api/v1/config-definitions/{created.data['id']}/",
            {"domain": "changed"},
            format="json",
        )
        self.assertEqual(structural_change.status_code, 400, structural_change.data)
        renamed = self.client.patch(
            f"/api/v1/config-definitions/{created.data['id']}/",
            {"key": "catalog.renamed"},
            format="json",
        )
        self.assertEqual(renamed.status_code, 400, renamed.data)
        security = self.client.post("/api/v1/config-definitions/", {
            "key": "security.catalog.canonical",
            "domain": "security",
            "configuration_class": "security_boundary",
            "value_type": "integer",
            "description": "Security CSV canonicalization",
            "impact_modules": ["security"],
            "allowed_scopes": " system,system ",
            "allowed_default_sources": "",
            "is_required": True,
            "is_secret": False,
            "value_schema": {"minimum": 1},
            "initial_value": 1,
        }, format="json")
        self.assertEqual(security.status_code, 201, security.data)
        self.assertEqual(security.data["allowed_scopes"], "system")
        unknown_scope = self.client.post("/api/v1/config-definitions/", {
            "key": "catalog.unknown-scope",
            "domain": "catalog",
            "value_type": "integer",
            "description": "Unknown scope must fail closed",
            "impact_modules": [],
            "allowed_scopes": "system,tenant",
            "value_schema": {},
            "initial_value": 1,
        }, format="json")
        self.assertEqual(unknown_scope.status_code, 400, unknown_scope.data)
        self.assertIn("allowed_scopes", str(unknown_scope.data))

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

    @staticmethod
    def _different_page_size(current: int, maximum: int) -> int:
        return current + 1 if current < maximum else max(1, current - 1)

    def _node_metadata_limits(self) -> tuple[int, int, int]:
        values = resolve_configuration(required_keys=(
            "workflow.node_metadata_default_page_size",
            "workflow.node_metadata_input_preview_limit",
        )).values
        return (
            int(values["workflow.node_metadata_default_page_size"]),
            boundary_int("security.api.max_page_size"),
            int(values["workflow.node_metadata_input_preview_limit"]),
        )

    def test_node_metadata_v2_pages_the_complete_stable_catalog(self):
        _default_page_size, maximum_page_size, _input_preview_limit = self._node_metadata_limits()
        final_page_size = 3
        catalog_count = maximum_page_size * 2 + final_page_size
        driver = Mock()
        driver.invoke.return_value = self._object_info(catalog_count)
        pages = []
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for offset in range(0, catalog_count, maximum_page_size):
                response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                    "deployment_id": str(self.deployment.id),
                    "offset": offset,
                    "limit": maximum_page_size,
                })
                self.assertEqual(response.status_code, 200, response.data)
                pages.append(response.data)

        self.assertEqual([page["schema_version"] for page in pages], [2, 2, 2])
        self.assertEqual([page["total_count"] for page in pages], [catalog_count] * len(pages))
        self.assertEqual(
            [page["offset"] for page in pages],
            list(range(0, catalog_count, maximum_page_size)),
        )
        self.assertEqual(
            [len(page["items"]) for page in pages],
            [maximum_page_size, maximum_page_size, final_page_size],
        )
        self.assertEqual(
            [page["next_offset"] for page in pages],
            [maximum_page_size, maximum_page_size * 2, None],
        )
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
        _default_page_size, maximum_page_size, _input_preview_limit = self._node_metadata_limits()
        driver = Mock()
        driver.invoke.return_value = self._object_info(maximum_page_size)
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                "deployment_id": str(self.deployment.id),
                "offset": 0,
                "limit": maximum_page_size,
            })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_count"], maximum_page_size)
        self.assertEqual(len(response.data["items"]), maximum_page_size)
        self.assertIsNone(response.data["next_offset"])
        self.assertNotIn("truncated", response.data)

    def test_node_metadata_filters_and_hashes_the_full_catalog_before_slicing(self):
        _default_page_size, maximum_page_size, _input_preview_limit = self._node_metadata_limits()
        matching_start = 300
        page_size = min(maximum_page_size, 19)
        final_page_size = min(page_size, 15)
        matching_count = page_size * 2 + final_page_size
        driver = Mock()
        driver.invoke.return_value = self._object_info(matching_start + matching_count)
        pages = []
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for offset in range(0, matching_count, page_size):
                response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                    "deployment_id": str(self.deployment.id),
                    "search": "node-03",
                    "offset": offset,
                    "limit": page_size,
                })
                self.assertEqual(response.status_code, 200, response.data)
                pages.append(response.data)

        self.assertEqual([page["total_count"] for page in pages], [matching_count] * len(pages))
        self.assertEqual(
            [len(page["items"]) for page in pages],
            [page_size, page_size, final_page_size],
        )
        self.assertEqual([page["next_offset"] for page in pages], [page_size, page_size * 2, None])
        self.assertEqual(len({page["catalog_sha256"] for page in pages}), 1)
        self.assertEqual(
            [item["class_type"] for page in pages for item in page["items"]],
            [f"Node-{index:04d}" for index in range(matching_start, matching_start + matching_count)],
        )

    def test_node_metadata_query_contract_rejects_invalid_pagination(self):
        _default_page_size, maximum_page_size, _input_preview_limit = self._node_metadata_limits()
        driver = Mock()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            for params in (
                {"deployment_id": str(self.deployment.id), "offset": -1},
                {"deployment_id": str(self.deployment.id), "limit": 0},
                {"deployment_id": str(self.deployment.id), "limit": maximum_page_size + 1},
            ):
                with self.subTest(params=params):
                    response = self.client.get(
                        "/api/v1/runtime-hosts/node-metadata/",
                        params,
                    )
                    self.assertEqual(response.status_code, 400, response.data)
        driver.invoke.assert_not_called()

    def test_node_metadata_uses_configured_default_page_and_input_preview_limits(self):
        default_page_size, maximum_page_size, input_preview_limit = self._node_metadata_limits()
        configured_page_size = self._different_page_size(default_page_size, maximum_page_size)
        configured_preview_limit = self._different_page_size(
            input_preview_limit,
            boundary_int("security.comfy.client.list_max_items"),
        )
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "workflow.node_metadata_default_page_size": configured_page_size,
                "workflow.node_metadata_input_preview_limit": configured_preview_limit,
            },
            created_by=self.admin,
            change_summary="test configurable node-metadata limits",
        )
        publish_config_revision(revision, published_by=self.admin)
        driver = Mock()
        driver.invoke.return_value = {
            "Configured-Node": {
                "display_name": "Configured node",
                "category": "tests",
                "input": {
                    "required": {
                        f"required_{index}": ["STRING", {}]
                        for index in range(configured_preview_limit + 1)
                    },
                    "optional": {
                        f"optional_{index}": ["STRING", {}]
                        for index in range(configured_preview_limit + 1)
                    },
                },
                "output": ["IMAGE"],
            },
            **self._object_info(configured_page_size),
        }
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                "deployment_id": str(self.deployment.id),
            })

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["limit"], configured_page_size)
        configured = next(
            item for item in response.data["items"]
            if item["class_type"] == "Configured-Node"
        )
        self.assertEqual(len(configured["required_inputs"]), configured_preview_limit)
        self.assertEqual(len(configured["optional_inputs"]), configured_preview_limit)

    def test_node_metadata_search_uses_security_character_ceiling(self):
        driver = Mock()
        maximum_search_characters = boundary_int("security.api.max_search_chars")
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/runtime-hosts/node-metadata/", {
                "deployment_id": str(self.deployment.id),
                "search": "x" * (maximum_search_characters + 1),
            })

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("search", str(response.data))
        driver.invoke.assert_not_called()

    def _import(
        self,
        *,
        include_resolution_bindings=True,
        include_workflow_mode=True,
        conditioning_mode="",
        smoke_asset_binding=None,
        contract_extra=None,
        model_requirements=None,
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
                "schema_version": 3,
                **({"workflow_mode": "semantic"} if include_workflow_mode else {}),
                **workflow_projector_reference(),
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "resolution_strategy": {
                    "mode": "explicit_width_height",
                    "node_ids": ["3"],
                },
                "required_dimension_multiple": 8,
                "parameters": {
                    "prompt.positive": {"type": "string", "required": True, "min_length": 1},
                    "output.prefix": {"type": "string", "required": True},
                    "image.width": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "image.height": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                },
                "outputs": {"primary": {"node_id": "2", "kind": "image", "required": True}},
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
            "model_requirements": list(model_requirements or []),
        }
        if not include_resolution_bindings:
            payload["semantic_bindings"] = [
                item for item in payload["semantic_bindings"]
                if item["semantic_key"] not in {"image.width", "image.height"}
            ]
        if conditioning_mode:
            payload["input_contract"]["conditioning_contract"] = conditioning_contract(
                conditioning_mode,
            )
        if smoke_asset_binding is not None:
            payload["input_contract"]["parameters"]["asset.image.reference"] = {
                "type": "asset_image",
                "required": True,
                "input_slot": 1,
                "asset_category": smoke_asset_binding["category"],
                "media_kind": "image",
                "min_items": 1,
                "max_items": 1,
            }
            payload["semantic_bindings"].append({
                "semantic_key": "asset.image.reference",
                "node_id": "2",
                "input_name": "images",
                "value_type": "asset_image",
            })
        if contract_extra:
            payload["input_contract"].update(contract_extra)
        try:
            version = import_workflow_version(
                template=self.template,
                provider=self.provider,
                model_profile_version=self.profile,
                graph=payload["graph"],
                input_contract=payload["input_contract"],
                semantic_bindings=payload["semantic_bindings"],
                model_requirements=payload["model_requirements"],
            )
        except DjangoValidationError as exc:
            return SimpleNamespace(
                status_code=400,
                data=getattr(exc, "message_dict", None) or getattr(exc, "messages", None),
            )
        return SimpleNamespace(
            status_code=201,
            data={"id": str(version.id), "input_contract": version.input_contract},
        )

    def test_import_rejects_missing_or_invalid_workflow_mode_before_persisting(self):
        original_count = WorkflowTemplateVersion.objects.count()
        invalid_cases = (
            {"include_workflow_mode": False},
            {"contract_extra": {"workflow_mode": ""}},
            {"contract_extra": {"workflow_mode": "  "}},
            {"contract_extra": {"workflow_mode": 1}},
        )

        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                response = self._import(**kwargs)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("workflow_mode", str(response.data))
        self.assertEqual(WorkflowTemplateVersion.objects.count(), original_count)

    def test_domain_import_rejects_forbidden_secret_fields_before_persisting(self):
        original_count = WorkflowTemplateVersion.objects.count()
        cases = (
            (
                {"1": {"class_type": "CLIPTextEncode", "inputs": {"api_key": "secret"}}},
                {"workflow_mode": "semantic"},
            ),
            (
                {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "safe"}}},
                {"workflow_mode": "semantic", "access_token": "secret"},
            ),
        )
        for graph, input_contract in cases:
            with self.subTest(input_contract=input_contract):
                with self.assertRaises(DjangoValidationError):
                    import_workflow_version(
                        template=self.template,
                        provider=self.provider,
                        model_profile_version=self.profile,
                        graph=graph,
                        input_contract=input_contract,
                        semantic_bindings=[],
                        model_requirements=[],
                    )
        self.assertEqual(WorkflowTemplateVersion.objects.count(), original_count)

    def test_invalid_smoke_fixture_detail_keeps_the_current_fail_closed_schema(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)

        with patch(
            "studio.platform_serializers.workflow_smoke_fixture_schema",
            side_effect=DjangoValidationError("invalid fixture contract"),
        ):
            response = self.client.get(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["smoke_fixture_schema"], {
            "schema_version": 2,
            "available": False,
            "reason_code": "workflow_fixture_contract_invalid",
            "parameters": [],
            "asset_slots": [],
            "asset_groups": [],
            "dynamic_references": None,
            "dynamic_reference_modes": {},
        })

    def test_integrity_rejects_frozen_contract_without_workflow_mode(self):
        from studio.services.workflow_compiler import workflow_execution_contract
        from studio.services.workflow_versions import _binding_rows, _requirement_rows

        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version = WorkflowTemplateVersion.objects.get(pk=imported.data["id"])
        drifted_contract = dict(version.input_contract)
        drifted_contract.pop("workflow_mode")
        assign_json_contract(version, "input_contract", drifted_contract)
        _, version.execution_contract_sha256 = workflow_execution_contract(
            compiler_id=version.compiler_id,
            compiler_version=version.compiler_version,
            graph_sha256=version.graph_sha256,
            input_contract_sha256=version.input_contract_sha256,
            bindings=_binding_rows(version),
            requirements=_requirement_rows(version),
        )

        with self.assertRaisesRegex(DjangoValidationError, "workflow_mode"):
            verify_workflow_version_integrity(version)

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

    def _smoke_action(self, *, values=None, assets=None):
        return {
            "deployment_id": str(self.deployment.id),
            "fixture": {
                "schema_version": 2,
                "values": {} if values is None else values,
                "assets": [] if assets is None else assets,
                "references": {},
            },
        }

    def _claim_validated_workflow_smoke(self, worker_id):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        validation_driver = Mock()
        validation_driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=validation_driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        queued = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(),
            format="json",
        )
        self.assertEqual(queued.status_code, 202, queued.data)
        claimed = claim_next_workflow_smoke(worker_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(str(claimed.id), queued.data["id"])
        return version_id, claimed

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
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
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
            pre_submit_resource_release_policy=None,
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
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
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
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
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

    def test_reference_asset_live_validate_checks_slots_without_concrete_asset(self):
        imported = self._import(
            smoke_asset_binding={"category": "character"},
        )
        self.assertEqual(imported.status_code, 201, imported.data)
        contract = imported.data["input_contract"]
        slot = contract["parameters"]["asset.image.reference"]
        self.assertEqual(
            {key: slot[key] for key in (
                "input_slot", "media_kind", "min_items", "max_items",
            )},
            {"input_slot": 1, "media_kind": "image", "min_items": 1, "max_items": 1},
        )
        self.assertNotIn("smoke_inputs", contract)
        self.assertNotIn("smoke_asset_bindings", contract)
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()

        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.post(
                f"/api/v1/workflow-template-versions/{imported.data['id']}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        driver.invoke.assert_called_once_with(self.deployment, "object_info", {})

    def test_configured_resolution_live_validate_checks_every_executable_size_without_submission(self):
        from studio.services.workflow_resolution import infer_source_image_resolution, configure_source_image_resolution, CONFIGURED_RESOLUTION_ADAPTER
        graph = {
            '1': {'class_type':'LoadImage','inputs':{'image':'__AUTOPROMPT_ASSET__:managed'}},
            '2': {'class_type':'FluxKontextImageScale','inputs':{'image':['1',0]}},
            '3': {'class_type':'SaveImage','inputs':{'images':['2',0],'filename_prefix':'output'}},
        }
        bindings = [
            {'semantic_key':'asset.image.source','node_id':'1','input_name':'image','value_type':'asset_image','is_required':True},
            {'semantic_key':'output.prefix','node_id':'3','input_name':'filename_prefix','value_type':'string','is_required':True},
        ]
        policy = {'adapter':CONFIGURED_RESOLUTION_ADAPTER,'sizes':[{'width':512,'height':512},{'width':768,'height':512}]}
        contract = {
            **workflow_projector_reference(image_resolution=policy),
            'schema_version':3,'workflow_mode':'semantic','compiler':{'id':'generic.comfy','version':1},
            'required_dimension_multiple':16,
            'resolution_strategy':configure_source_image_resolution(infer_source_image_resolution(graph,bindings,'3'),policy),
            'outputs':{'primary':{'node_id':'3','kind':'image','required':True}},
            'parameters':{'asset.image.source':{'type':'asset_image','required':True,'input_slot':1,'asset_category':'image','media_kind':'image','min_items':1,'max_items':1},'output.prefix':{'type':'string','required':True}},
        }
        imported = import_workflow_version(template=self.template, provider=self.provider,
            model_profile_version=self.profile, graph=graph, input_contract=contract,
            semantic_bindings=bindings, model_requirements=[])
        object_info = {
            'LoadImage':{'input':{'required':{'image':[['example.png'],{'image_upload':True}]}},'output':['IMAGE','MASK']},
            'FluxKontextImageScale':{'input':{'required':{'image':['IMAGE']}},'output':['IMAGE']},
            'SaveImage':{'input':{'required':{'images':['IMAGE'],'filename_prefix':['STRING']}},'output':[],'output_node':True},
            'ImageScale':{'input':{'required':{'image':['IMAGE'],'width':['INT',{'min':64,'max':4096}],
                'height':['INT',{'min':64,'max':4096}],'upscale_method':[['lanczos']],'crop':[['center']]}},'output':['IMAGE']},
        }
        driver = Mock(); driver.invoke.return_value=object_info
        url=f"/api/v1/workflow-template-versions/{imported.id}/validate/"
        with patch('studio.services.workflow_versions.driver_for',return_value=driver):
            success=self.client.post(url,{'deployment_id':str(self.deployment.id)},format='json')
            self.assertEqual(success.status_code,200,success.data)
            version=WorkflowTemplateVersion.objects.get(pk=imported.id)
            self.assertEqual([row['size'] for row in version.validation_evidence['compiler_probes']],policy['sizes'])
            object_info['ImageScale']['input']['required']['width'][1]['max']=512
            rejected=self.client.post(url,{'deployment_id':str(self.deployment.id)},format='json')
            self.assertEqual(rejected.status_code,400,rejected.data)
            version.refresh_from_db();self.assertEqual(version.validation_state,'failed')
            del object_info['ImageScale']
            missing=self.client.post(url,{'deployment_id':str(self.deployment.id)},format='json')
            self.assertEqual(missing.status_code,400,missing.data)
        self.assertTrue(all(call.args[1]=='object_info' for call in driver.invoke.call_args_list))

    def test_stale_validation_failure_does_not_overwrite_newer_success(self):
        imported = self._import()
        version_id = imported.data["id"]

        def persist_newer_success(*_args, **_kwargs):
            newer = WorkflowTemplateVersion.objects.get(pk=version_id)
            newer.status = WorkflowTemplateVersion.Status.VALIDATED
            newer.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
            newer.validation_message = "newer validation passed"
            newer.validated_deployment = self.deployment
            newer.validation_evidence = {"generation": "newer"}
            newer.validation_evidence_sha256 = json_contract(
                newer.validation_evidence,
            ).sha256
            newer.save(update_fields=(
                "status", "validation_state", "validation_message",
                "validated_deployment", "validation_evidence",
                "validation_evidence_sha256", "updated_at",
            ))
            raise ProviderDriverError("older request failed")

        driver = Mock()
        driver.invoke.side_effect = persist_newer_success
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            response = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(version.validation_message, "newer validation passed")
        self.assertEqual(version.validation_evidence, {"generation": "newer"})

    def test_stale_validation_success_does_not_overwrite_newer_success(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()

        def persist_newer_success(*_args, **_kwargs):
            newer = WorkflowTemplateVersion.objects.get(pk=version_id)
            newer.status = WorkflowTemplateVersion.Status.VALIDATED
            newer.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
            newer.validation_message = "newer validation passed"
            newer.validated_deployment = self.deployment
            newer.validation_evidence = {"generation": "newer"}
            newer.validation_evidence_sha256 = json_contract(
                newer.validation_evidence,
            ).sha256
            newer.save(update_fields=(
                "status", "validation_state", "validation_message",
                "validated_deployment", "validation_evidence",
                "validation_evidence_sha256", "updated_at",
            ))
            return {"models": "newer request already completed"}

        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ), patch(
            "studio.services.workflow_versions._validate_model_inventory",
            side_effect=persist_newer_success,
        ):
            response = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(version.validation_message, "newer validation passed")
        self.assertEqual(version.validation_evidence, {"generation": "newer"})

    def test_generic_import_requires_width_and_height_semantic_bindings(self):
        rejected = self._import(include_resolution_bindings=False)

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("image.height", str(rejected.data))
        self.assertIn("image.width", str(rejected.data))

    def test_smoke_requires_validated_lifecycle_status(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        self.assertEqual(
            validated.data["version"]["status"],
            WorkflowTemplateVersion.Status.VALIDATED,
        )

        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        for lifecycle_status in (
            WorkflowTemplateVersion.Status.DRAFT,
            WorkflowTemplateVersion.Status.PUBLISHED,
            WorkflowTemplateVersion.Status.RETIRED,
        ):
            with self.subTest(status=lifecycle_status):
                version.status = lifecycle_status
                version.save(update_fields=("status", "updated_at"))
                response = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    self._smoke_action(values={"prompt.positive": "fixture A"}),
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("validated", str(response.data))
        self.assertFalse(WorkflowSmokeAttempt.objects.filter(workflow_version_id=version_id).exists())

    def test_smoke_enqueue_freezes_fixture_and_is_hash_idempotent(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        self.assertEqual(
            validated.data["version"]["status"],
            WorkflowTemplateVersion.Status.VALIDATED,
        )

        first = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        same = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        different = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture B"}),
            format="json",
        )

        self.assertEqual(first.status_code, 202, first.data)
        self.assertNotIn("fixture_values", first.data)
        self.assertEqual(len(first.data["fixture_values_sha256"]), 64)
        self.assertEqual(same.status_code, 202, same.data)
        self.assertEqual(same.data["id"], first.data["id"])
        self.assertEqual(different.status_code, 409, different.data)
        self.assertEqual(
            different.data["error"]["code"],
            "workflow_smoke_fixture_conflict",
        )
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 1)
        attempt = WorkflowSmokeAttempt.objects.get(pk=first.data["id"])
        self.assertEqual(attempt.fixture_values_schema_version, 2)
        self.assertEqual(
            attempt.fixture_values["parameters"]["prompt.positive"],
            "fixture A",
        )
        self.assertEqual(len(attempt.fixture_values_sha256), 64)
        self.assertEqual(len(attempt.fixture_sha256), 64)
        attempt.fixture_values = {
            **attempt.fixture_values,
            "parameters": {
                **attempt.fixture_values["parameters"],
                "prompt.positive": "mutated",
            },
        }
        with self.assertRaises(DjangoValidationError):
            attempt.save(update_fields=("fixture_values", "updated_at"))
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.fixture_values["parameters"]["prompt.positive"],
            "fixture A",
        )
        attempt.workflow_version_id = uuid.uuid4()
        with self.assertRaises(DjangoValidationError):
            attempt.save(update_fields=("workflow_version", "updated_at"))
        with self.assertRaises(DjangoValidationError):
            WorkflowSmokeAttempt.objects.filter(pk=attempt.pk).update(
                deployment_id=uuid.uuid4(),
            )

    def test_active_smoke_from_prior_validation_is_never_reused_or_submitted(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        created = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(created.status_code, 202, created.data)

        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        version.validation_evidence = {
            **version.validation_evidence,
            "revalidation_marker": "new-evidence",
        }
        version.validation_evidence_sha256 = json_contract(
            version.validation_evidence,
        ).sha256
        version.save(update_fields=(
            "validation_evidence", "validation_evidence_sha256", "updated_at",
        ))

        stale_reuse = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(stale_reuse.status_code, 409, stale_reuse.data)
        self.assertEqual(
            stale_reuse.data["error"]["code"],
            "workflow_smoke_fixture_conflict",
        )

        remote_execute = Mock()
        claimed = claim_next_workflow_smoke("stale-contract-worker")
        self.assertIsNotNone(claimed)
        with patch(
            "studio.services.workflow_smoke_queue.smoke_test_workflow_version",
            remote_execute,
        ):
            processed = process_workflow_smoke(claimed)
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        remote_execute.assert_not_called()
        version.refresh_from_db()
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )

    def test_pre_submission_smoke_timeout_updates_version_without_clearing_validation(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        created = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(created.status_code, 202, created.data)
        expired = timezone.now() - timedelta(seconds=1)
        WorkflowSmokeAttempt.objects.filter(pk=created.data["id"]).update(
            hard_deadline_at=expired,
            idle_deadline_at=expired,
        )

        claimed = claim_next_workflow_smoke("expired-smoke-worker")
        self.assertIsNotNone(claimed)
        processed = process_workflow_smoke(claimed)

        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        self.assertIn(processed.error_code, {"hard_timeout", "idle_timeout"})
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.FAILED,
        )
        self.assertIn("deadline", version.smoke_test_message)

    def test_workflow_smoke_cancel_preserves_validation_before_and_after_submit(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)

        queued = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(),
            format="json",
        )
        self.assertEqual(queued.status_code, 202, queued.data)
        cancelled = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{queued.data['id']}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.data)
        self.assertEqual(cancelled.data["status"], WorkflowSmokeAttempt.Status.CANCELLED)

        second = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(),
            format="json",
        )
        self.assertEqual(second.status_code, 202, second.data)
        running = WorkflowSmokeAttempt.objects.get(pk=second.data["id"])
        now = timezone.now()
        running.status = WorkflowSmokeAttempt.Status.RUNNING
        running.submission_started_at = now
        running.submitted_at = now
        running.provider_request_id = str(running.submission_token)
        running.save(update_fields=(
            "status", "submission_started_at", "submitted_at",
            "provider_request_id", "updated_at",
        ))
        requested = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{running.id}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(requested.status_code, 202, requested.data)
        self.assertEqual(
            requested.data["status"],
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        )
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )

    def test_workflow_smoke_cancel_after_claim_blocks_submission_callback(self):
        version_id, claimed = self._claim_validated_workflow_smoke(
            "pre-submit-cancel-worker",
        )
        operations = []
        cancel_response = None
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def invoke(_deployment, operation, _payload):
            nonlocal cancel_response
            operations.append(operation)
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                cancel_response = self.client.post(
                    f"/api/v1/workflow-smoke-attempts/{claimed.id}/cancel/",
                    {},
                    format="json",
                )
                return self._canvas_object_info()
            if operation == "submit":
                raise AssertionError("cancelled pre-submit attempt reached driver submit")
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            processed = process_workflow_smoke(claimed)

        self.assertIsNotNone(cancel_response)
        self.assertEqual(cancel_response.status_code, 202, cancel_response.data)
        self.assertEqual(
            cancel_response.data["status"],
            WorkflowSmokeAttempt.Status.CANCELLED,
        )
        self.assertEqual(operations, ["health", "object_info"])
        processed.refresh_from_db()
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.CANCELLED)
        self.assertEqual(processed.error_code, "cancelled_before_submission")
        self.assertIsNone(processed.submission_started_at)
        self.assertIsNone(processed.submitted_at)
        self.assertEqual(processed.provider_request_id, "")
        self.assertIsNone(processed.claim_token)
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )
        self.assertEqual(version.smoke_test_evidence, {})

    def test_workflow_smoke_cancel_during_submit_preserves_cancel_fact(self):
        version_id, claimed = self._claim_validated_workflow_smoke(
            "submit-cancel-worker",
        )
        request_id = str(claimed.submission_token)
        snapshots = {}
        operations = []
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def invoke(_deployment, operation, payload):
            operations.append(operation)
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                return self._canvas_object_info()
            if operation == "submit":
                submitting = WorkflowSmokeAttempt.objects.get(pk=claimed.pk)
                snapshots["submitting_status"] = submitting.status
                snapshots["submission_started_at"] = submitting.submission_started_at
                response = self.client.post(
                    f"/api/v1/workflow-smoke-attempts/{claimed.id}/cancel/",
                    {},
                    format="json",
                )
                snapshots["cancel_response"] = response
                requested = WorkflowSmokeAttempt.objects.get(pk=claimed.pk)
                snapshots["requested_status"] = requested.status
                snapshots["requested_at"] = requested.cancel_requested_at
                return {"request_id": payload["submission_token"]}
            if operation == "poll":
                submitted = WorkflowSmokeAttempt.objects.get(pk=claimed.pk)
                snapshots["poll_status"] = submitted.status
                snapshots["poll_requested_at"] = submitted.cancel_requested_at
                snapshots["provider_request_id"] = submitted.provider_request_id
                snapshots["submitted_at"] = submitted.submitted_at
                return {"status": "running"}
            if operation == "cancel":
                cancelling = WorkflowSmokeAttempt.objects.get(pk=claimed.pk)
                snapshots["cancel_status"] = cancelling.status
                snapshots["cancel_requested_at"] = cancelling.cancel_requested_at
                snapshots["cancel_request_id"] = payload["request_id"]
                return {"cancelled": True}
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            processed = process_workflow_smoke(claimed)

        self.assertEqual(
            snapshots["submitting_status"],
            WorkflowSmokeAttempt.Status.SUBMITTING,
        )
        self.assertIsNotNone(snapshots["submission_started_at"])
        self.assertEqual(snapshots["cancel_response"].status_code, 202)
        self.assertEqual(
            snapshots["requested_status"],
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        )
        self.assertEqual(
            snapshots["poll_status"],
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        )
        self.assertEqual(
            snapshots["cancel_status"],
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        )
        self.assertEqual(snapshots["provider_request_id"], request_id)
        self.assertIsNotNone(snapshots["submitted_at"])
        self.assertEqual(snapshots["cancel_request_id"], request_id)
        self.assertEqual(snapshots["poll_requested_at"], snapshots["requested_at"])
        self.assertEqual(snapshots["cancel_requested_at"], snapshots["requested_at"])
        self.assertEqual(
            operations,
            ["health", "object_info", "submit", "poll", "cancel"],
        )
        processed.refresh_from_db()
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.UNCERTAIN)
        self.assertEqual(processed.error_code, "remote_cancel_state_uncertain")
        self.assertEqual(processed.provider_request_id, request_id)
        self.assertEqual(processed.cancel_requested_at, snapshots["requested_at"])
        self.assertEqual(processed.result, {})
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )
        self.assertEqual(version.smoke_test_evidence, {})

    def test_workflow_smoke_cancel_after_final_poll_discards_collected_result(self):
        version_id, claimed = self._claim_validated_workflow_smoke(
            "post-poll-cancel-worker",
        )
        operations = []
        cancel_response = None
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def invoke(_deployment, operation, payload):
            nonlocal cancel_response
            operations.append(operation)
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                return self._canvas_object_info()
            if operation == "submit":
                return {"request_id": payload["submission_token"]}
            if operation == "poll":
                return {"status": "succeeded"}
            if operation == "collect":
                cancel_response = self.client.post(
                    f"/api/v1/workflow-smoke-attempts/{claimed.id}/cancel/",
                    {},
                    format="json",
                )
                return {"outputs": [{
                    "node_id": "2",
                    "filename": "smoke.png",
                    "subfolder": "",
                    "type": "output",
                }]}
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        with tempfile.TemporaryDirectory() as output_dir:
            Image.new("RGB", (2, 2), color="red").save(
                Path(output_dir) / "smoke.png",
            )
            with override_settings(COMFYUI_OUTPUT_DIR=output_dir), patch(
                "studio.services.workflow_versions.driver_for",
                return_value=driver,
            ):
                processed = process_workflow_smoke(claimed)

        self.assertIsNotNone(cancel_response)
        self.assertEqual(cancel_response.status_code, 202, cancel_response.data)
        self.assertEqual(
            cancel_response.data["status"],
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        )
        self.assertEqual(
            operations,
            ["health", "object_info", "submit", "poll", "collect"],
        )
        processed.refresh_from_db()
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.CANCELLED)
        self.assertEqual(
            processed.error_code,
            "workflow_smoke_cancelled_result_discarded",
        )
        self.assertEqual(processed.result, {})
        self.assertIsNotNone(processed.cancel_requested_at)
        self.assertIsNotNone(processed.finished_at)
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )
        self.assertEqual(version.smoke_test_evidence, {})

    def _collect_smoke_with_derived_output_contract(self, output_size):
        from dataclasses import replace
        from studio.services.workflow_versions import _materialize_smoke_graph
        version_id, claimed = self._claim_validated_workflow_smoke("derived-size-smoke-worker")
        driver = Mock()
        driver.capabilities.return_value = {"operations": ["health", "object_info", "submit", "poll", "cancel", "collect"]}

        def invoke(_deployment, operation, payload):
            if operation == "submit":
                return {"request_id": payload["submission_token"]}
            return {
                "health": {"system": {"comfyui_version": "test"}},
                "object_info": self._canvas_object_info(),
                "poll": {"status": "succeeded"},
                "collect": {"outputs": [{"node_id": "2", "filename": "smoke.png", "subfolder": "", "type": "output"}]},
            }[operation]

        # Exercise the collector with the new output contract; compilation from
        # real Asset bytes is covered independently by WorkflowResolutionTests.
        def compile_with_size(*args, **kwargs):
            compiled = _materialize_smoke_graph(*args, **kwargs)
            return replace(compiled, output_contract={
                **compiled.output_contract, "schema_version": 2,
                "resolution": {
                    "adapter": "comfy.flux_kontext_image_scale/v1",
                    "source_semantic_key": "asset.image.source", "source_asset_id": "fixture",
                    "source_checksum": "a" * 64, "source_width": 600, "source_height": 600,
                    "width": 1024, "height": 1024,
                },
            })

        driver.invoke.side_effect = invoke
        with tempfile.TemporaryDirectory() as output_dir:
            Image.new("RGB", output_size).save(Path(output_dir) / "smoke.png")
            with (
                override_settings(COMFYUI_OUTPUT_DIR=output_dir),
                patch("studio.services.workflow_versions.driver_for", return_value=driver),
                patch("studio.services.workflow_versions._materialize_smoke_graph", side_effect=compile_with_size),
            ):
                processed = process_workflow_smoke(claimed)
        return WorkflowTemplateVersion.objects.get(pk=version_id), processed

    def test_smoke_collector_rejects_wrong_derived_image_dimensions(self):
        version, processed = self._collect_smoke_with_derived_output_contract((64, 64))
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(processed.error_code, "provider_smoke_output_invalid")
        self.assertEqual(version.smoke_test_state, WorkflowTemplateVersion.ValidationState.FAILED)
        self.assertIn("实际图片尺寸", processed.error_message)

    def test_smoke_collector_persists_derived_size_evidence_on_success(self):
        version, processed = self._collect_smoke_with_derived_output_contract((1024, 1024))
        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.SUCCEEDED)
        self.assertEqual(processed.result["output_contract"], version.smoke_test_evidence["output_contract"])
        self.assertEqual(processed.result["output_contract"]["resolution"]["width"], 1024)

    def test_remote_comfyui_node_failure_is_persisted_with_recovery_advice(self):
        version_id, claimed = self._claim_validated_workflow_smoke(
            "specific-failure-smoke-worker",
        )
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def invoke(_deployment, operation, payload):
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                return self._canvas_object_info()
            if operation == "submit":
                return {"request_id": payload["submission_token"]}
            if operation == "poll":
                return {
                    "status": "failed",
                    "failure": {
                        "schema_version": 1,
                        "kind": "execution_error",
                        "node_id": "1",
                        "exception_type": "ValueError",
                        "exception_message": "required input text is missing",
                    },
                }
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            processed = process_workflow_smoke(claimed)

        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(processed.error_code, "provider_smoke_failed")
        self.assertIn("节点 1（CLIPTextEncode）", processed.error_message)
        self.assertIn("required input text is missing", processed.error_message)
        self.assertIn("处理建议", processed.error_message)
        self.assertNotIn("UntrustedRemoteNodeType", processed.error_message)
        self.assertNotIn("{'detail'", processed.error_message)
        self.assertEqual(processed.result, {})

        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.FAILED,
        )
        self.assertEqual(version.smoke_test_message, processed.error_message)
        detail = self.client.get(
            f"/api/v1/workflow-smoke-attempts/{processed.id}/",
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["error_message"], processed.error_message)
        self.assertEqual(detail.data["result"], {})
        self.assertNotIn("failure", detail.data)

    def test_comfyui_driver_poll_projects_only_safe_execution_failure(self):
        history = {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [[
                    "execution_error",
                    {
                        "prompt_id": "must-not-be-projected",
                        "node_id": "1",
                        "node_type": "CLIPTextEncode",
                        "exception_type": "ValueError",
                        "exception_message": "required input text is missing",
                        "traceback": ["private traceback"],
                        "current_inputs": {"text": "private prompt"},
                    },
                ]],
            },
        }
        with patch(
            "studio.services.comfyui.ComfyUIClient.history",
            return_value=history,
        ):
            result = ComfyUIDriver().invoke(
                self.deployment,
                "poll",
                {"request_id": "safe-request-id"},
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["node_id"], "1")
        self.assertEqual(
            set(result["failure"]),
            {
                "schema_version", "kind", "node_id",
                "exception_type", "exception_message",
            },
        )
        self.assertNotIn("must-not-be-projected", str(result))
        self.assertNotIn("private traceback", str(result))
        self.assertNotIn("private prompt", str(result))

    def test_remote_poll_transport_failure_is_uncertain_after_submission(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        created = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(created.status_code, 202, created.data)
        claimed = claim_next_workflow_smoke("uncertain-smoke-worker")
        self.assertIsNotNone(claimed)

        def fail_after_submit(*_args, **kwargs):
            kwargs["on_submission_started"]()
            kwargs["on_submitted"](str(claimed.submission_token))
            newer = WorkflowTemplateVersion.objects.get(pk=version_id)
            newer.validation_evidence = {
                **newer.validation_evidence,
                "generation": "newer-validation",
            }
            newer.validation_evidence_sha256 = json_contract(
                newer.validation_evidence,
            ).sha256
            newer.validation_message = "newer validation passed"
            newer.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            newer.smoke_test_message = "newer validation requires a new smoke"
            newer.smoke_test_evidence = {}
            newer.smoke_test_evidence_sha256 = ""
            newer.save(update_fields=(
                "validation_evidence", "validation_evidence_sha256",
                "validation_message", "smoke_test_state", "smoke_test_message",
                "smoke_test_evidence", "smoke_test_evidence_sha256", "updated_at",
            ))
            provider_error = ProviderDriverError(
                "poll transport unavailable",
                code="comfyui_operation_failed",
            )
            raise DjangoValidationError({"detail": "provider unavailable"}) from provider_error

        with patch(
            "studio.services.workflow_smoke_queue.smoke_test_workflow_version",
            side_effect=fail_after_submit,
        ):
            processed = process_workflow_smoke(claimed)

        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.UNCERTAIN)
        self.assertEqual(processed.error_code, "remote_state_uncertain")
        self.assertEqual(
            processed.provider_request_id,
            str(processed.submission_token),
        )
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )
        self.assertEqual(version.validation_message, "newer validation passed")
        self.assertEqual(version.smoke_test_message, "newer validation requires a new smoke")

        blocked = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(
            blocked.data["error"]["code"],
            "unresolved_workflow_smoke_submission",
        )
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 1)

        wrong_request = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{processed.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_failed",
                "provider_request_id": "another-request",
                "note": "已在 ComfyUI 历史中确认该请求失败。",
            },
            format="json",
        )
        self.assertEqual(wrong_request.status_code, 400, wrong_request.data)

        resolution_payload = {
            "outcome": "confirmed_remote_failed",
            "note": "已在 ComfyUI 历史中确认该请求失败。",
        }
        resolved = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{processed.id}/resolve-uncertain/",
            resolution_payload,
            format="json",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        self.assertNotIn("provider_request_id", resolved.data)
        self.assertEqual(
            resolved.data["provider_request_id_sha256"],
            hashlib.sha256(str(processed.submission_token).encode("utf-8")).hexdigest(),
        )
        replayed_resolution = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{processed.id}/resolve-uncertain/",
            resolution_payload,
            format="json",
        )
        self.assertEqual(replayed_resolution.status_code, 200, replayed_resolution.data)
        self.assertEqual(replayed_resolution.data["id"], resolved.data["id"])
        conflicting_resolution = self.client.post(
            f"/api/v1/workflow-smoke-attempts/{processed.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_succeeded",
                "note": "已在 ComfyUI 历史中确认该请求成功。",
            },
            format="json",
        )
        self.assertEqual(conflicting_resolution.status_code, 409, conflicting_resolution.data)

        still_not_publishable = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/publish/",
            {},
            format="json",
        )
        self.assertEqual(still_not_publishable.status_code, 400, still_not_publishable.data)

        retry = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"prompt.positive": "fixture A"}),
            format="json",
        )
        self.assertEqual(retry.status_code, 202, retry.data)
        self.assertNotEqual(retry.data["id"], str(processed.id))
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 2)

        resolution = WorkflowSmokeAttemptResolution.objects.get(pk=resolved.data["id"])
        resolution.note = "mutated resolution"
        with self.assertRaises(DjangoValidationError):
            resolution.save(update_fields=("note", "updated_at"))
        with self.assertRaises(DjangoValidationError):
            WorkflowSmokeAttemptResolution.objects.filter(pk=resolution.pk).update(
                note="mutated queryset",
            )

    def test_late_smoke_success_never_overwrites_newer_validation(self):
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
                    "width": ["INT", {}], "height": ["INT", {}],
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

        newer_evidence = {"generation": "newer-validation"}

        def invoke(_deployment, operation, payload):
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                return object_info
            if operation == "submit":
                return {"request_id": payload.get("submission_token")}
            if operation == "poll":
                changed_deployment = ModelDeployment.objects.get(pk=self.deployment.pk)
                assign_json_contract(changed_deployment, "runtime_config", {
                    "base_url": "http://127.0.0.1:8899",
                    "timeout_seconds": 12,
                    "poll_interval_seconds": 1,
                    "max_poll_seconds": 60,
                    "capability_ttl_seconds": 300,
                })
                changed_deployment.save(update_fields=(
                    "runtime_config", "runtime_config_schema_version",
                    "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
                ))
                newer = WorkflowTemplateVersion.objects.get(pk=version_id)
                newer.status = WorkflowTemplateVersion.Status.VALIDATED
                newer.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
                newer.validation_message = "newer validation passed"
                newer.validated_deployment = self.deployment
                newer.validation_evidence = newer_evidence
                newer.validation_evidence_sha256 = json_contract(newer_evidence).sha256
                newer.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
                newer.smoke_test_message = "newer validation requires a new smoke"
                newer.smoke_test_evidence = {}
                newer.smoke_test_evidence_sha256 = ""
                newer.save(update_fields=(
                    "status", "validation_state", "validation_message",
                    "validated_deployment", "validation_evidence",
                    "validation_evidence_sha256", "smoke_test_state",
                    "smoke_test_message", "smoke_test_evidence",
                    "smoke_test_evidence_sha256", "updated_at",
                ))
                return {"status": "succeeded"}
            if operation == "collect":
                return {"outputs": [{
                    "node_id": "2", "filename": "smoke.png",
                    "subfolder": "", "type": "output",
                }]}
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        with tempfile.TemporaryDirectory() as output_dir:
            Image.new("RGB", (2, 2), color="red").save(Path(output_dir) / "smoke.png")
            with override_settings(COMFYUI_OUTPUT_DIR=output_dir), patch(
                "studio.services.workflow_versions.driver_for",
                return_value=driver,
            ):
                validated = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)},
                    format="json",
                )
                self.assertEqual(validated.status_code, 200, validated.data)
                queued = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    self._smoke_action(),
                    format="json",
                )
                self.assertEqual(queued.status_code, 202, queued.data)
                processed = process_workflow_smoke(
                    claim_next_workflow_smoke("late-smoke-worker"),
                )

        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        self.assertEqual(processed.error_code, "workflow_smoke_superseded")
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(version.validation_evidence, newer_evidence)
        self.assertEqual(version.validation_message, "newer validation passed")
        self.assertEqual(
            version.smoke_test_state,
            WorkflowTemplateVersion.ValidationState.NOT_RUN,
        )
        self.assertEqual(version.smoke_test_message, "newer validation requires a new smoke")

    def test_invalid_smoke_fixture_does_not_clear_live_validation(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)
        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        evidence_sha = version.validation_evidence_sha256

        rejected = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
            self._smoke_action(values={"parameter.not_declared": 1}),
            format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 0)
        version.refresh_from_db()
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(version.validation_evidence_sha256, evidence_sha)
        self.assertEqual(
            version.status,
            WorkflowTemplateVersion.Status.VALIDATED,
        )

    def test_smoke_asset_drift_fails_before_provider_and_keeps_validation(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "reference.png"
            source.write_bytes(b"frozen-reference")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = Asset.objects.create(
                owner=None,
                kind=Asset.Kind.REFERENCE,
                name="Frozen smoke reference",
                local_path=str(source),
                mime_type="image/png",
                checksum=checksum,
                is_active=True,
            )
            imported = self._import(
                smoke_asset_binding={"category": "character"},
            )
            self.assertEqual(imported.status_code, 201, imported.data)
            version_id = imported.data["id"]
            driver = Mock()
            driver.invoke.return_value = self._canvas_object_info()
            with override_settings(ASSET_LIBRARY_ROOT=root), patch(
                "studio.services.workflow_versions.driver_for",
                return_value=driver,
            ):
                validated = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/validate/",
                    {"deployment_id": str(self.deployment.id)},
                    format="json",
                )
                self.assertEqual(validated.status_code, 200, validated.data)
                version = WorkflowTemplateVersion.objects.get(pk=version_id)
                validation_sha = version.validation_evidence_sha256
                queued = self.client.post(
                    f"/api/v1/workflow-template-versions/{version_id}/smoke-test/",
                    self._smoke_action(assets=[{
                        "semantic_key": "asset.image.reference",
                        "asset_id": str(asset.id),
                    }]),
                    format="json",
                )
                self.assertEqual(queued.status_code, 202, queued.data)
                binding = WorkflowSmokeAssetBinding.objects.get(
                    attempt_id=queued.data["id"],
                )
                binding.category = "mutated"
                with self.assertRaises(DjangoValidationError):
                    binding.save(update_fields=("category", "updated_at"))
                source.write_bytes(b"drifted-reference")
                provider_factory = Mock()
                with patch(
                    "studio.services.workflow_versions.driver_for",
                    provider_factory,
                ):
                    processed = process_workflow_smoke(
                        claim_next_workflow_smoke("asset-drift-worker"),
                    )

        self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.FAILED)
        self.assertIn("漂移", processed.error_message)
        provider_factory.assert_not_called()
        version.refresh_from_db()
        self.assertEqual(
            version.validation_state,
            WorkflowTemplateVersion.ValidationState.PASSED,
        )
        self.assertEqual(version.validation_evidence_sha256, validation_sha)

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
                    self._smoke_action(),
                    format="json",
                )
                self.assertEqual(smoked.status_code, 202, smoked.data)
                self.assertIn("Location", smoked.headers)
                attempt = claim_next_workflow_smoke("test-worker")
                self.assertIsNotNone(attempt)
                processed = process_workflow_smoke(attempt)
                self.assertEqual(processed.status, WorkflowSmokeAttempt.Status.SUCCEEDED)
                processed.refresh_from_db()
                version = WorkflowTemplateVersion.objects.get(pk=version_id)
                self.assertEqual(
                    processed.result["fixture_sha256"],
                    processed.fixture_sha256,
                )
                self.assertEqual(len(processed.result["compiled_graph_sha256"]), 64)
                self.assertEqual(
                    version.smoke_test_evidence["fixture_sha256"],
                    processed.fixture_sha256,
                )
                self.assertEqual(
                    version.smoke_test_evidence["compiled_graph_sha256"],
                    processed.result["compiled_graph_sha256"],
                )
        published = self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/publish/",
            {},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], WorkflowTemplateVersion.Status.PUBLISHED)
        publish_event = PlatformLifecycleAuditEvent.objects.get(
            object_type="workflow_template_version",
            object_id=version_id,
            action="publish",
        )
        self.assertEqual(
            publish_event.before_summary["validated_deployment_id"],
            str(self.deployment.id),
        )
        self.assertEqual(
            publish_event.after_summary["validated_deployment_id"],
            str(self.deployment.id),
        )
        self.assertEqual(
            publish_event.before_summary["status"],
            WorkflowTemplateVersion.Status.VALIDATED,
        )
        self.assertEqual(
            publish_event.after_summary["status"],
            WorkflowTemplateVersion.Status.PUBLISHED,
        )

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
                    self._smoke_action(), format="json",
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
                    self._smoke_action(), format="json",
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
        self.assertEqual(version.validation_state, "passed")
        self.assertEqual(version.validated_deployment_id, self.deployment.id)
        self.assertTrue(version.validation_evidence)
        self.assertEqual(self.client.post(
            f"/api/v1/workflow-template-versions/{version_id}/publish/", {}, format="json",
        ).status_code, 400)
    def test_collection_post_is_not_a_workflow_creation_entry(self):
        original_count = WorkflowTemplateVersion.objects.count()
        response = self.client.post(
            "/api/v1/workflow-template-versions/",
            {"graph": {"1": {"class_type": "SaveImage", "inputs": {}}}},
            format="json",
        )

        self.assertEqual(response.status_code, 405, response.data)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), original_count)

    def test_domain_import_rejects_graph_over_configured_limit(self):
        with self.assertRaises(DjangoValidationError):
            import_workflow_version(
                template=self.template,
                provider=self.provider,
                model_profile_version=self.profile,
                graph={
                    "1": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": "x" * (64 * 1024)},
                    },
                },
                input_contract={"workflow_mode": "semantic"},
                semantic_bindings=[],
                model_requirements=[],
            )

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
            "poll_interval_seconds": 1, "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
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
        response = self._import(model_requirements=[{
                "role": foreign.role,
                "alias": "foreign",
                "artifact_id": str(foreign.id),
                "expected_filename": foreign.filename,
                "expected_sha256": foreign.sha256,
                "is_required": False,
            }])
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
                self._smoke_action(), format="json",
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


@skipUnlessDBFeature("has_select_for_update")
class ModelProfileNextVersionConcurrencyTests(TransactionTestCase):
    """Exercise the family-anchor row lock on transactional databases."""

    reset_sequences = True

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="model-next-version-concurrency-admin",
            password="StrongPass123!",
            email="model-next-version-concurrency@example.test",
        )
        provider = ProviderProfile(
            key="model-next-version-concurrency-provider",
            name="Model next version concurrency provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,submit,poll",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        provider.save()
        source = ModelProfileVersion(
            profile_key="concurrent-next-version",
            version=1,
            provider=provider,
            model_identifier="concurrent-v1.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(source, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
            "model_family": "concurrent-next-version",
        })
        source.save()
        self.source_id = source.pk

    def test_concurrent_next_version_creates_one_draft(self):
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results: list[tuple[int, str]] = []
        failures: list[str] = []

        def create_next_version():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.admin.pk)
                client = APIClient()
                client.force_authenticate(actor)
                barrier.wait(timeout=10)
                response = client.post(
                    f"/api/v1/model-profile-versions/{self.source_id}/draft-next/",
                    {},
                    format="json",
                )
                error_code = ""
                if isinstance(response.data, dict):
                    error = response.data.get("error")
                    if isinstance(error, dict):
                        error_code = str(error.get("code") or "")
                with result_lock:
                    results.append((response.status_code, error_code))
            except BaseException as exc:  # pragma: no cover - thread handoff
                with result_lock:
                    failures.append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=create_next_version, daemon=True)
            for _index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(sorted(status for status, _code in results), [201, 409])
        self.assertEqual(
            [code for status, code in results if status == 409],
            ["model_profile_source_stale"],
        )
        self.assertEqual(
            ModelProfileVersion.objects.filter(
                profile_key="concurrent-next-version",
                version=2,
                status=ModelProfileVersion.Status.DRAFT,
            ).count(),
            1,
        )


class ConfigDefinitionRevisionConcurrencyTests(TransactionTestCase):
    """A first revision cannot land between definition validation and save."""

    reset_sequences = True

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="config-definition-concurrency-admin",
            password="StrongPass123!",
            email="config-definition-concurrency@example.test",
        )
        self.definition = ConfigDefinition(
            key="concurrency.definition",
            domain="initial",
            value_type=ConfigDefinition.ValueType.INTEGER,
            description="Definition updated concurrently with first revision",
            impact_modules=["api"],
            allowed_scopes="system",
            value_schema={"minimum": 1},
            initial_value=1,
        )
        assign_json_contract(self.definition, "value_schema", {"minimum": 1})
        assign_json_contract(self.definition, "initial_value", 1)
        self.definition.save()

    def test_revision_writer_waits_for_definition_update_contract(self):
        from studio.platform_serializers import ConfigDefinitionSerializer

        original_validate = ConfigDefinitionSerializer.validate
        validated = threading.Event()
        release_update = threading.Event()
        revision_started = threading.Event()
        revision_finished = threading.Event()
        failures: list[str] = []
        response_status: list[int] = []

        def paused_validate(serializer, attrs):
            result = original_validate(serializer, attrs)
            if serializer.instance and serializer.instance.pk == self.definition.pk:
                validated.set()
                if not release_update.wait(timeout=10):
                    raise AssertionError("definition update was not released")
            return result

        def update_definition():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.admin.pk)
                client = APIClient()
                client.force_authenticate(actor)
                response = client.patch(
                    f"/api/v1/config-definitions/{self.definition.pk}/",
                    {"domain": "updated-before-revision"},
                    format="json",
                )
                response_status.append(response.status_code)
            except BaseException as exc:  # pragma: no cover - thread handoff
                failures.append(repr(exc))
            finally:
                close_old_connections()

        def create_first_revision():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.admin.pk)
                revision_started.set()
                create_config_revision(
                    scope_type=ConfigRevision.ScopeType.SYSTEM,
                    values={self.definition.key: 2},
                    created_by=actor,
                )
                revision_finished.set()
            except BaseException as exc:  # pragma: no cover - thread handoff
                failures.append(repr(exc))
            finally:
                close_old_connections()

        with patch.object(ConfigDefinitionSerializer, "validate", paused_validate):
            update_thread = threading.Thread(target=update_definition, daemon=True)
            update_thread.start()
            self.assertTrue(validated.wait(timeout=10))
            revision_thread = threading.Thread(target=create_first_revision, daemon=True)
            revision_thread.start()
            self.assertTrue(revision_started.wait(timeout=10))
            self.assertFalse(revision_finished.wait(timeout=0.25))
            release_update.set()
            update_thread.join(timeout=20)
            revision_thread.join(timeout=20)

        self.assertFalse(update_thread.is_alive())
        self.assertFalse(revision_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(response_status, [200])
        self.assertTrue(revision_finished.is_set())
        self.definition.refresh_from_db()
        self.assertEqual(self.definition.domain, "updated-before-revision")
        self.assertTrue(self.definition.values.exists())


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
