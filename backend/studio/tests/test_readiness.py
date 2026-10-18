from datetime import timedelta
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.db import OperationalError
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

import studio.services.configuration as configuration_service
import studio.services.readiness as readiness_service
from studio.models import (
    ConfigRevision,
    ProductionRecipeVersion,
    WorkerLease,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.readiness import (
    collect_readiness,
)
from studio.services.security_boundaries import boundary_int
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

    def _set_worker_requirements(self, **overrides):
        values = {
            "runtime.readiness_generation_worker_required": True,
            "runtime.readiness_text_generation_worker_required": True,
            "runtime.readiness_assembly_worker_required": True,
        }
        values.update({
            f"runtime.readiness_{key}_worker_required": value
            for key, value in overrides.items()
        })
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values=values,
            created_by=self.superuser,
        )
        publish_config_revision(revision, published_by=self.superuser)







    def test_stale_unrequired_worker_is_diagnostic_not_global_503(self):
        self._set_worker_requirements(generation=False)
        stale = timezone.now() - timedelta(minutes=5)
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(updated_at=stale)
        with (
            patch("studio.services.readiness._binary_available", return_value=True),
            patch(
                "studio.services.readiness._readiness_runtime_values",
                return_value={
                    "worker_max_age_seconds": 10,
                    "minimum_disk_free_bytes": 1,
                    "generation_worker_required": False,
                    "text_generation_worker_required": True,
                    "assembly_worker_required": True,
                },
            ),
        ):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["diagnostics"]["generation_worker"]["ok"])
        self.assertFalse(payload["diagnostics"]["generation_worker"]["required"])
        self.assertIn("未设为必需", payload["diagnostics"]["generation_worker"]["message"])

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

    def test_missing_toolchain_or_comfyui_output_is_capability_diagnostic(self):
        self._set_worker_requirements(generation=False, assembly=False)
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
        self._set_worker_requirements(generation=False, assembly=False)
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
            ),
            patch("studio.services.readiness.shutil.disk_usage", side_effect=disk_usage),
            patch(
                "studio.services.readiness._readiness_runtime_values",
                return_value={
                    "worker_max_age_seconds": 30,
                    "minimum_disk_free_bytes": 100,
                    "generation_worker_required": False,
                    "text_generation_worker_required": True,
                    "assembly_worker_required": False,
                },
            ),
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

    def test_readiness_reuses_runtime_and_security_resolutions_per_request(self):
        target_boundary = "security.api.max_search_chars"
        original_boundary_value = boundary_int(target_boundary)
        capability_report = {
            "schema_version": 1,
            **{
                key: {
                    "state": "not_required",
                    "required": False,
                    "reason_codes": [],
                }
                for key in ("text", "image", "video", "assembly")
            },
        }
        observed_values = []

        def repeated_boundary_consumer(*_args, **_kwargs):
            first = boundary_int(target_boundary)
            second = boundary_int(target_boundary)
            self.assertEqual(first, second)
            observed_values.append(first)
            return capability_report

        original_runtime = readiness_service._readiness_runtime_values
        original_security = configuration_service.resolve_security_boundaries
        with (
            patch(
                "studio.services.readiness._readiness_runtime_values",
                wraps=original_runtime,
            ) as runtime_resolver,
            patch(
                "studio.services.readiness._capability_readiness_for_requirements",
                side_effect=repeated_boundary_consumer,
            ),
            patch.object(
                configuration_service,
                "resolve_security_boundaries",
                wraps=original_security,
            ) as security_resolver,
        ):
            payload = collect_readiness()
            first_target_call_count = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )

            revision = create_config_revision(
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                values={target_boundary: original_boundary_value + 1},
                created_by=self.superuser,
            )
            publish_config_revision(revision, published_by=self.superuser)
            target_calls_before_second_request = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )
            second_payload = collect_readiness()
            target_calls_after_second_request = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )

        self.assertEqual(
            payload["status"],
            "ready" if all(item["ok"] for item in payload["checks"].values()) else "not_ready",
        )
        self.assertEqual(payload["capabilities"], capability_report)
        self.assertEqual(second_payload["capabilities"], capability_report)
        self.assertEqual(runtime_resolver.call_count, 2)
        self.assertEqual(first_target_call_count, 1)
        self.assertEqual(
            target_calls_after_second_request - target_calls_before_second_request,
            1,
        )
        self.assertEqual(
            observed_values,
            [original_boundary_value, original_boundary_value + 1],
        )

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
