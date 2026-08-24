from datetime import timedelta
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
    RuntimeHost,
    WorkerLease,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.readiness import _workflow_catalog_check, collect_readiness
from studio.services.worker_heartbeat import touch_worker_heartbeat
from studio.tests.secret_fixtures import TEST_SECRET_ENV


User = get_user_model()


class ReadinessTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        WorkerLease.objects.create(key="generation-worker-heartbeat")
        WorkerLease.objects.create(key="assembly-worker-heartbeat")
        self.runtime_settings = override_settings(
            MEDIA_ROOT=Path(self.temp_dir.name),
            COMFYUI_OUTPUT_DIR=self.temp_dir.name,
            FFMPEG_BINARY="ffmpeg-test",
            FFPROBE_BINARY="ffprobe-test",
            READINESS_MIN_DISK_FREE_BYTES=1,
        )
        self.runtime_settings.enable()
        self.addCleanup(self.runtime_settings.disable)
        self.keyring = patch.dict(os.environ, TEST_SECRET_ENV, clear=False)
        self.keyring.start()
        self.addCleanup(self.keyring.stop)
        self.superuser = User.objects.create_superuser(
            username="readiness-superadmin",
            email="readiness@example.test",
            password="ReadinessPass123!",
        )

    def _published_local_workflow(self):
        provider = ProviderProfile(
            key="readiness-comfyui",
            name="Readiness ComfyUI",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {"base_url": "http://127.0.0.1:8188"})
        provider.save()

        profile = ModelProfileVersion(
            profile_key="readiness-image",
            version=1,
            provider=provider,
            model_identifier="readiness-image",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {"capabilities": ["image.text_to_image"]})
        profile.save()

        host = RuntimeHost.objects.create(key="readiness-host", name="Readiness host")
        deployment = ModelDeployment(
            key="readiness-deployment",
            name="Readiness deployment",
            profile_version=profile,
            runtime_host=host,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {"base_url": "http://127.0.0.1:8188"})
        deployment.save()

        template = WorkflowTemplate.objects.create(
            name="Readiness workflow",
            slug="readiness-workflow",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            is_system=True,
        )
        version = WorkflowTemplateVersion(
            template=template,
            version=1,
            provider=provider,
            model_profile_version=profile,
            validated_deployment=deployment,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
            is_current=True,
        )
        assign_json_contract(version, "graph", {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"}},
        })
        assign_json_contract(version, "input_contract", {"schema_version": 1})
        version.save()
        return version, deployment

    def test_catalog_without_published_local_workflow_does_not_require_gpu(self):
        WorkflowTemplateVersion.objects.update(
            status=WorkflowTemplateVersion.Status.DRAFT,
            is_current=False,
        )

        result = _workflow_catalog_check()

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_required"])
        self.assertNotIn("cuda", str(result).lower())

    def test_published_local_workflow_uses_bound_deployment_and_live_contract(self):
        version, deployment = self._published_local_workflow()
        driver = Mock()
        driver.invoke.side_effect = lambda bound, operation, payload: (
            {"system": {"comfyui_version": "test"}, "devices": []}
            if operation == "health"
            else {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}]}},
                    "output": ["CONDITIONING"],
                    "output_name": ["CONDITIONING"],
                },
            }
        )

        with patch("studio.services.readiness.driver_for", return_value=driver):
            result = _workflow_catalog_check()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["local_required"])
        self.assertEqual(result["failures"], {})
        self.assertEqual(
            [call.args[:2] for call in driver.invoke.call_args_list],
            [(deployment, "health"), (deployment, "object_info")],
        )
        self.assertEqual(result["production_workflow_count"], 1)
        self.assertEqual(version.validated_deployment_id, deployment.id)

    def test_missing_bound_deployment_is_explicitly_not_ready(self):
        version, _ = self._published_local_workflow()
        version.validated_deployment = None
        version.save(update_fields=["validated_deployment", "updated_at"])

        result = _workflow_catalog_check()

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["failures"][str(version.id)],
            "validated_deployment_unavailable",
        )

    def test_live_schema_drift_is_explicitly_not_ready(self):
        version, _ = self._published_local_workflow()
        driver = Mock()
        driver.invoke.side_effect = lambda deployment, operation, payload: (
            {} if operation == "health" else {}
        )

        with patch("studio.services.readiness.driver_for", return_value=driver):
            result = _workflow_catalog_check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][str(version.id)], "live_contract_unavailable")

    @override_settings(READINESS_WORKER_MAX_AGE_SECONDS=10)
    def test_stale_unrequired_worker_is_diagnostic_not_global_503(self):
        stale = timezone.now() - timedelta(minutes=5)
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(updated_at=stale)
        with patch("studio.services.readiness._binary_available", return_value=True):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "ready")
        self.assertFalse(payload["diagnostics"]["generation_worker"]["ok"])
        self.assertIn("没有新鲜心跳", payload["diagnostics"]["generation_worker"]["message"])

    def test_worker_heartbeat_touch_creates_and_refreshes_lease(self):
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").delete()
        touch_worker_heartbeat("generation-worker-heartbeat")
        first = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(
            updated_at=first - timedelta(minutes=1),
        )

        touch_worker_heartbeat("generation-worker-heartbeat")

        refreshed = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        self.assertGreaterEqual(refreshed, first)

    @override_settings(
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
    )
    def test_missing_toolchain_or_comfyui_output_is_capability_diagnostic(self):
        with patch("studio.services.readiness._binary_available", return_value=False):
            toolchain = collect_readiness()
        self.assertEqual(toolchain["status"], "ready")
        self.assertFalse(toolchain["diagnostics"]["toolchain"]["ok"])

        missing_output = Path(self.temp_dir.name) / "missing-comfy-output"
        with self.settings(COMFYUI_OUTPUT_DIR=missing_output):
            output = collect_readiness()
        self.assertEqual(output["status"], "ready")
        self.assertFalse(output["diagnostics"]["comfyui_output"]["ok"])

    def test_comfyui_output_uses_the_storage_minimum(self):
        root = Path(self.temp_dir.name)
        media = root / "media"
        output = root / "comfy-output"
        media.mkdir()
        output.mkdir()

        def disk_usage(path):
            return SimpleNamespace(free=1_000 if Path(path) == media else 99)

        with (
            self.settings(
                MEDIA_ROOT=media,
                COMFYUI_OUTPUT_DIR=output,
                READINESS_MIN_DISK_FREE_BYTES=100,
                READINESS_GENERATION_WORKER_REQUIRED=False,
                READINESS_ASSEMBLY_WORKER_REQUIRED=False,
            ),
            patch("studio.services.readiness.shutil.disk_usage", side_effect=disk_usage),
        ):
            payload = collect_readiness()

        self.assertTrue(payload["checks"]["storage"]["ok"])
        self.assertFalse(payload["diagnostics"]["comfyui_output"]["ok"])
        self.assertEqual(payload["diagnostics"]["comfyui_output"]["minimum_free_bytes"], 100)

    def test_readiness_endpoint_status_follows_payload(self):
        ready_payload = {"status": "ready", "checks": {}}
        unavailable_payload = {"status": "not_ready", "checks": {"database": {"ok": False}}}
        with patch("studio.health_views.collect_readiness", return_value=ready_payload):
            ready = self.client.get("/api/v1/health/ready/")
        with patch("studio.health_views.collect_readiness", return_value=unavailable_payload):
            unavailable = self.client.get("/api/v1/health/ready/")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(unavailable.status_code, 503)

    def test_production_readiness_is_superuser_only_and_empty_catalog_is_unconfigured(self):
        ProductionRecipeVersion.objects.update(status=ProductionRecipeVersion.Status.RETIRED)
        client = APIClient()
        client.force_authenticate(self.superuser)
        response = client.get("/api/v1/production-readiness/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "unconfigured")
        self.assertIn("published_recipe_missing", response.data["reason_codes"])

        member = User.objects.create_user(
            username="readiness-member",
            password="StrongPass123!",
        )
        client.force_authenticate(member)
        forbidden = client.get("/api/v1/production-readiness/")
        self.assertEqual(forbidden.status_code, 403)

    def test_readiness_requires_an_active_usable_superuser(self):
        self.superuser.set_unusable_password()
        self.superuser.save(update_fields=("password",))

        payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["access_control"]["ok"])
        self.assertEqual(payload["checks"]["access_control"]["active_superuser_count"], 0)

    def test_readiness_validates_keyring_even_without_secret_references(self):
        with patch.dict(os.environ, {
            "AUTOPROMPT_CONFIG_MASTER_KEYS": "",
            "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID": "",
        }, clear=False):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["secret_store"]["ok"])
        self.assertIn("secret_keyring_unconfigured", payload["checks"]["secret_store"]["failure_codes"])

    def test_liveness_endpoint_does_not_require_database_workers_or_gpu(self):
        WorkerLease.objects.all().delete()

        response = self.client.get("/api/v1/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")

    def test_database_failure_keeps_liveness_up_and_readiness_structured(self):
        with patch(
            "studio.services.readiness.connection.cursor",
            side_effect=OperationalError("database unavailable"),
        ):
            live = self.client.get("/api/v1/health/live/")
            ready = self.client.get("/api/v1/health/ready/")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 503)
        self.assertFalse(ready.data["checks"]["database"]["ok"])
        self.assertFalse(ready.data["checks"]["migrations"]["ok"])
        self.assertEqual(ready.data["diagnostics"], {})
        self.assertEqual(
            ready.data["capabilities"]["text"]["reason_codes"],
            ["database_or_migrations_unavailable"],
        )
