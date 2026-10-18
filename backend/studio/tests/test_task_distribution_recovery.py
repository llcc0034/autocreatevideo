from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction, IntegrityError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask
from studio.services.distribution import DistributionClaimLost, DistributionError, verified_distribution_manifest
from studio.services.provenance import sha256_file
from studio.services.task_distribution_recovery import (
    DistributionTaskHeartbeat, claim_task_distribution, process_task_distribution, recover_stale_task_distributions,
    request_distribution_recovery, run_distribution_tasks, schedule_initial_distribution,
)
from studio.tests import test_execution_task_distribution as fixtures


class TaskDistributionRecoveryTests(TestCase):
    resolve = fixtures.PreparedTaskDistributionTests.resolve
    mark_submitted = fixtures.PreparedTaskDistributionTests.mark_submitted
    archive = fixtures.PreparedTaskDistributionTests.archive
    require_font = fixtures.PreparedTaskDistributionTests.require_font

    def setUp(self):
        fixtures.PreparedTaskDistributionTests.setUp(self)
        self.url = f"/api/v1/production-runs/{self.run.pk}/distribution-recovery/"

    def request(self, revision=0):
        return self.client.post(self.url, {"task_id": str(self.task.pk), "expected_revision": revision}, format="json")

    def test_public_request_original_worker_real_output_and_same_revision_replay(self):
        self.require_font()
        original = (GenerationAttempt.objects.count(), ExecutionPreparation.objects.count(), self.task.attempts, sha256_file(self.output))
        with patch("studio.services.task_distribution_recovery.prepare_all_task_distributions") as render:
            response = self.request()
        self.assertEqual(response.status_code, 202, response.data)
        render.assert_not_called()
        self.assertEqual(response.data["state"], "queued")
        self.assertEqual(self.request().status_code, 200)
        claimed = claim_task_distribution()
        self.assertIsNone(claim_task_distribution())
        result = process_task_distribution(claimed)
        self.assertEqual(result.distribution_state, "ready", result.distribution_error)
        self.assertEqual(verified_distribution_manifest(result, 0)["verification"]["status"], "verified")
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.request(1).status_code, 409)
        self.assertIsNone(claim_task_distribution())
        self.task.refresh_from_db()
        self.assertEqual(original, (GenerationAttempt.objects.count(), ExecutionPreparation.objects.count(), self.task.attempts, sha256_file(self.output)))

    def test_failure_is_explicit_and_only_new_revision_retries_without_generation(self):
        self.assertEqual(self.request().status_code, 202)
        claimed = claim_task_distribution()
        with patch("studio.services.distribution._resolve_font", side_effect=DistributionError("Missing disclosure font")):
            result = process_task_distribution(claimed)
        self.assertEqual((result.status, result.distribution_state), ("succeeded", "failed"))
        self.assertEqual(result.outputs[0]["distribution"]["status"], "failed")
        self.assertEqual(self.request().status_code, 200)
        self.assertIsNone(claim_task_distribution())
        self.assertEqual(self.request(1).status_code, 202)
        self.assertEqual(self.request(0).status_code, 409)
        self.require_font()
        result = process_task_distribution(claim_task_distribution())
        self.assertEqual(result.distribution_state, "ready", result.distribution_error)

    def test_stale_claim_is_failed_not_automatically_requeued_and_late_result_cannot_write(self):
        self.request()
        claimed = claim_task_distribution()
        GenerationTask.objects.filter(pk=claimed.pk).update(distribution_heartbeat_at=timezone.now() - timedelta(days=10))
        self.assertEqual(recover_stale_task_distributions(), 1)
        self.assertEqual(recover_stale_task_distributions(), 0)
        self.assertIsNone(claim_task_distribution())
        with self.assertRaises(DistributionClaimLost):
            process_task_distribution(claimed)
        self.assertEqual(self.request(1).status_code, 202)
        newer = claim_task_distribution()
        with self.assertRaises(DistributionClaimLost):
            process_task_distribution(claimed)
        self.assertNotEqual(newer.distribution_claim, claimed.distribution_claim)

    def test_source_corruption_stays_failed_and_never_falls_back_to_older_task_inputs(self):
        self.request()
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with patch("studio.services.distribution._render_image") as render:
            result = process_task_distribution(claim_task_distribution())
        self.assertEqual(result.distribution_state, "failed")
        self.assertEqual(result.status, "succeeded")
        render.assert_not_called()

    def test_initial_phase_is_committed_with_archive_and_rolls_back_together(self):
        self.task.refresh_from_db()
        with self.assertRaises(RuntimeError), transaction.atomic():
            schedule_initial_distribution(self.task)
            raise RuntimeError("original archive transaction rejected")
        self.task.refresh_from_db()
        self.assertEqual(self.task.distribution_state, "not_requested")
        with transaction.atomic():
            schedule_initial_distribution(self.task)
            schedule_initial_distribution(self.task)
        self.assertEqual(GenerationTask.objects.get(pk=self.task.pk).distribution_revision, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenerationTask.objects.filter(pk=self.task.pk).update(distribution_state="running")

    def test_run_scope_permissions_terminal_and_strict_input(self):
        stranger = get_user_model().objects.create_user(username="other-distribution-owner")
        client = APIClient(); client.force_authenticate(stranger)
        self.assertEqual(client.get(self.url).status_code, 404)
        self.assertEqual(client.post(self.url, {"task_id": str(self.task.pk), "expected_revision": 0}, format="json").status_code, 404)
        for changes in ({"expected_revision": False}, {"expected_revision": "0"}, {"extra": "ignored?"}, {"expected_revision": -1}):
            response = self.client.post(self.url, {"task_id": str(self.task.pk), "expected_revision": 0, **changes}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.run.status = "cancelled"; self.run.save(update_fields=("status", "updated_at"))
        self.assertEqual(self.request().status_code, 400)
        self.assertIsNone(claim_task_distribution())

    def test_paginated_read_is_narrow_indexed_no_files_or_writes(self):
        with CaptureQueriesContext(connection) as queries, patch("studio.services.distribution.verified_task_manifest") as probe:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["state"], "not_requested")
        probe.assert_not_called()
        task_queries = [row["sql"] for row in queries if 'FROM "studio_generationtask"' in row["sql"]]
        self.assertTrue(task_queries)
        for query in task_queries:
            self.assertNotIn('"outputs"', query)
            self.assertNotIn('"staged_inputs"', query)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        query = GenerationTask.objects.filter(distribution_state="queued", status="succeeded").order_by("distribution_requested_at", "id").values("id")
        self.assertIn("idx_task_distribution_work", query.explain())
        self.assertEqual(self.client.get(self.url, {"legacy_route": "x"}).status_code, 400)

    def test_cancel_after_request_stops_local_phase_without_rendering(self):
        self.request()
        self.run.status = "cancelled"; self.run.save(update_fields=("status", "updated_at"))
        with patch("studio.services.task_distribution_recovery.prepare_all_task_distributions") as render:
            result = process_task_distribution(claim_task_distribution())
        render.assert_not_called()
        self.assertEqual((result.status, result.distribution_state), ("succeeded", "failed"))

    def test_long_render_with_fresh_task_and_file_heartbeats_is_not_reclaimed(self):
        import json
        import os
        import time
        from studio.services import distribution
        self.request(); claimed = claim_task_distribution()
        GenerationTask.objects.filter(pk=claimed.pk).update(distribution_started_at=timezone.now() - timedelta(days=10),
            distribution_heartbeat_at=timezone.now() - timedelta(days=10))
        heartbeat = DistributionTaskHeartbeat(claimed)
        heartbeat._touch()
        self.assertEqual(recover_stale_task_distributions(), 0)
        lock = self.output.parent / "heartbeat-fixture.lock"
        document = distribution._lock_document("actual-publisher")
        document["started_at_epoch"] = time.time() - 86400
        lock.write_text(json.dumps(document), encoding="utf-8")
        os.utime(lock, (time.time() - 86400, time.time() - 86400))
        self.assertTrue(distribution._lock_is_stale(lock))
        distribution._PublicationLockHeartbeat(lock, "actual-publisher")._touch()
        self.assertFalse(distribution._lock_is_stale(lock))
        document["owner_token"] = "new-publisher"
        lock.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(DistributionClaimLost):
            distribution._PublicationLockHeartbeat(lock, "actual-publisher")._touch()

    def test_original_generation_worker_processes_publication_without_comfy_claim(self):
        self.require_font(); self.request()
        command = "studio.management.commands.run_generation_worker."
        with patch(command + "_claim_generation_work") as gpu, \
                patch(command + "recover_stale_tasks", return_value=0), \
                patch(command + "recover_stale_workflow_smokes", return_value=0), \
                patch(command + "recover_stale_asset_generation_jobs", return_value={"released": 0, "uncertain": 0}), \
                patch(command + "reconcile_uncertain_comfy_asset_jobs", return_value={"resumed": 0, "terminal": 0}):
            call_command("run_generation_worker", "--once")
        gpu.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.distribution_state, "ready", self.task.distribution_error)
