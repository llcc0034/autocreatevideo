from __future__ import annotations

from datetime import timedelta
from io import StringIO
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    RuntimeModelAvailability,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.runtime_host_probe_queue import (
    claim_next_runtime_host_probe,
    enqueue_runtime_host_probe,
    process_runtime_host_probe,
    recover_stale_runtime_host_probes,
    request_runtime_host_probe_cancel,
)


class RuntimeHostProbeQueueTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="runtime-probe-admin",
            password="StrongPass123!",
            email="runtime-probe@example.test",
        )
        self.user = get_user_model().objects.create_user(
            username="runtime-probe-user",
            password="StrongPass123!",
        )
        self.host = RuntimeHost.objects.create(
            key="runtime-probe-test-host",
            name="Runtime probe test host",
            status=RuntimeHost.Status.UNAVAILABLE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _snapshot(self, attempt: RuntimeHostProbeAttempt, *, revision: int = 1):
        snapshot = HostCapabilitySnapshot(
            host=self.host,
            probe_attempt=attempt,
            revision=revision,
            probe_status=HostCapabilitySnapshot.ProbeStatus.PARTIAL,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        assign_json_contract(snapshot, "details", {
            "schema_version": 1,
            "probe_errors": ["nvidia-smi_not_found"],
        })
        snapshot.save()
        return snapshot

    def test_probe_api_enqueues_once_returns_202_and_never_executes_probe_inline(self):
        with patch(
            "studio.services.runtime_host_probe_queue.probe_runtime_host",
        ) as execute_probe:
            first = self.client.post(
                f"/api/v1/runtime-hosts/{self.host.id}/probe/",
                {},
                format="json",
            )
            repeated = self.client.post(
                f"/api/v1/runtime-hosts/{self.host.id}/probe/",
                {},
                format="json",
            )

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(repeated.status_code, 202, repeated.data)
        self.assertEqual(first.data["id"], repeated.data["id"])
        self.assertEqual(first.data["status"], RuntimeHostProbeAttempt.Status.QUEUED)
        self.assertTrue(first["Location"].endswith(f"/{first.data['id']}/"))
        self.assertEqual(RuntimeHostProbeAttempt.objects.filter(host=self.host).count(), 1)
        execute_probe.assert_not_called()

    def test_probe_attempt_collection_is_admin_only_and_cancel_is_idempotent(self):
        attempt, _ = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        response = self.client.post(
            f"/api/v1/runtime-host-probe-attempts/{attempt.id}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["status"], RuntimeHostProbeAttempt.Status.CANCELLED)
        repeated = self.client.post(
            f"/api/v1/runtime-host-probe-attempts/{attempt.id}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(repeated.status_code, 202, repeated.data)
        self.assertEqual(repeated.data["status"], RuntimeHostProbeAttempt.Status.CANCELLED)

        self.client.force_authenticate(self.user)
        denied = self.client.get("/api/v1/runtime-host-probe-attempts/")
        self.assertEqual(denied.status_code, 403, denied.data)

    def test_worker_persists_snapshot_and_terminal_state_with_crash_recovery_link(self):
        attempt, _ = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        claimed = claim_next_runtime_host_probe("runtime-probe-worker")
        self.assertEqual(claimed.id, attempt.id)

        def fake_probe(host, *, lease_renew, probe_attempt, commit_guard):
            lease_renew()
            with transaction.atomic():
                commit_guard()
                return self._snapshot(probe_attempt)

        with patch(
            "studio.services.runtime_host_probe_queue.probe_runtime_host",
            side_effect=fake_probe,
        ):
            result = process_runtime_host_probe(claimed)

        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.SUCCEEDED)
        self.assertIsNotNone(result.finished_at)
        self.assertIsNone(result.claim_token)
        snapshot = HostCapabilitySnapshot.objects.get(probe_attempt=result)
        self.assertEqual(snapshot.host, self.host)

        RuntimeHostProbeAttempt.objects.filter(pk=result.pk).update(
            status=RuntimeHostProbeAttempt.Status.RUNNING,
            finished_at=None,
        )
        recovered = recover_stale_runtime_host_probes()
        result.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.SUCCEEDED)

    def test_running_cancel_is_observed_at_chunk_checkpoint_without_snapshot(self):
        attempt, _ = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        claimed = claim_next_runtime_host_probe("runtime-probe-cancel-worker")

        def fake_probe(host, *, lease_renew, probe_attempt, commit_guard):
            request_runtime_host_probe_cancel(
                RuntimeHostProbeAttempt.objects.get(pk=probe_attempt.pk),
            )
            lease_renew()
            raise AssertionError("cancel checkpoint should stop the probe")

        with patch(
            "studio.services.runtime_host_probe_queue.HEARTBEAT_INTERVAL_SECONDS",
            0,
        ), patch(
            "studio.services.runtime_host_probe_queue.probe_runtime_host",
            side_effect=fake_probe,
        ):
            result = process_runtime_host_probe(claimed)

        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.CANCELLED)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(HostCapabilitySnapshot.objects.filter(probe_attempt=attempt).exists())

    def test_stale_claim_is_released_for_worker_recovery(self):
        attempt, _ = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        claimed = claim_next_runtime_host_probe("dead-runtime-probe-worker")
        RuntimeHostProbeAttempt.objects.filter(pk=claimed.pk).update(
            heartbeat_at=timezone.now() - timedelta(minutes=10),
        )

        recovered = recover_stale_runtime_host_probes(stale_seconds=60)

        attempt.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(attempt.status, RuntimeHostProbeAttempt.Status.RUNNING)
        self.assertIsNone(attempt.claim_token)
        self.assertEqual(attempt.progress_marker, "phase:recovered")
        reclaimed = claim_next_runtime_host_probe("replacement-runtime-probe-worker")
        self.assertEqual(reclaimed.id, attempt.id)
        self.assertIsNotNone(reclaimed.claim_token)

    def test_failure_is_terminal_and_does_not_expose_internal_path(self):
        attempt, _ = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        claimed = claim_next_runtime_host_probe("runtime-probe-failure-worker")
        with self.assertLogs(
            "studio.services.runtime_host_probe_queue",
            level="ERROR",
        ), patch(
            "studio.services.runtime_host_probe_queue.probe_runtime_host",
            side_effect=OSError(r"E:\private\models\secret.safetensors"),
        ):
            result = process_runtime_host_probe(claimed)

        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.FAILED)
        self.assertEqual(result.error_code, "runtime_host_probe_failed")
        self.assertNotIn("secret.safetensors", result.error_message)
        self.assertFalse(HostCapabilitySnapshot.objects.filter(probe_attempt=attempt).exists())

    def test_snapshot_api_returns_trusted_size_and_full_hash_without_path(self):
        attempt = RuntimeHostProbeAttempt.objects.create(
            host=self.host,
            created_by=self.admin,
            status=RuntimeHostProbeAttempt.Status.SUCCEEDED,
            finished_at=timezone.now(),
        )
        snapshot = self._snapshot(attempt)
        profile = ModelProfileVersion.objects.get(profile_key="sdxl-image", version=1)
        deployment = ModelDeployment(
            key="runtime-probe-observed-deployment",
            name="Runtime probe observed deployment",
            profile_version=profile,
            runtime_host=self.host,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
        })
        deployment.save()
        artifact = ModelArtifact.objects.create(
            profile_version=profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="Observed artifact",
            filename="observed.safetensors",
            sha256="a" * 64,
            size_bytes=75_000_000_000,
        )
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=deployment,
            artifact=artifact,
            availability_key=f"deployment:{deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            reason="主机已从受控本地文件系统验证 size/hash",
            observed_size_bytes=75_000_000_000,
            observed_sha256="b" * 64,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )

        response = self.client.get(
            f"/api/v1/runtime-hosts/{self.host.id}/latest-snapshot/",
        )

        self.assertEqual(response.status_code, 200, response.data)
        fact = response.data["model_availability"][0]
        self.assertEqual(fact["observed_size_bytes"], 75_000_000_000)
        self.assertEqual(fact["observed_sha256"], "b" * 64)
        self.assertEqual(
            fact["observation_source"],
            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        self.assertFalse(any("path" in key.lower() for key in fact))

    def test_platform_maintenance_claims_durable_probe_attempts(self):
        terminal = RuntimeHostProbeAttempt(
            id=uuid.uuid4(),
            host=self.host,
            status=RuntimeHostProbeAttempt.Status.SUCCEEDED,
        )
        output = StringIO()
        with patch(
            "studio.management.commands.run_platform_maintenance.recover_stale_runtime_host_probes",
            return_value=1,
        ) as recover, patch(
            "studio.management.commands.run_platform_maintenance.enqueue_due_runtime_host_probes",
            return_value=2,
        ) as enqueue, patch(
            "studio.management.commands.run_platform_maintenance.claim_next_runtime_host_probe",
            side_effect=[terminal, None],
        ) as claim, patch(
            "studio.management.commands.run_platform_maintenance.process_runtime_host_probe",
            return_value=terminal,
        ) as process:
            call_command("run_platform_maintenance", once=True, stdout=output)

        recover.assert_called_once_with()
        enqueue.assert_called_once_with()
        self.assertEqual(claim.call_count, 2)
        process.assert_called_once()
        self.assertIn("recovered=1 queued=2 processed=1", output.getvalue())
