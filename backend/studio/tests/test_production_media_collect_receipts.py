"""Original production queue/HTTP with real PNG archival and isolated peers."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt, GenerationTask
from studio.services.comfyui import ComfyUIClient
from studio.services.execution_production_workflow_queue import retain_prepared_generation_history
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response, describe_text_response_review
from studio.services.task_queue import process_task, recover_stale_tasks, claim_next_task, TaskClaimLost
from studio.tests import test_execution_production_workflow_artifacts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionMediaCollectReceiptTests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.history = {"status": {"status_str": "success", "completed": True},
            "outputs": {"7": {"images": [{key: value for key, value in self.receipt.items() if key != "node_id"}]}},
            "trace": {"secret": "private-credential", "url": "https://private.invalid/receipt"}}
        self.peer.history = Mock(return_value=self.history)
        self.peer.extract_outputs = ComfyUIClient.extract_outputs
        for method in ("is_queued", "queue_prompt", "release_resources_before_submit", "upload_image"):
            setattr(self.peer, method, Mock(side_effect=AssertionError("No duplicate remote operation: " + method)))

    def cycle(self, task=None):
        with patch("studio.services.task_distribution_recovery.prepare_all_task_distributions", return_value=[]), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            return process_task(task or self.task, self.peer)

    def detail(self):
        return self.client.get(f"/api/v1/task-center/preparation/{self.preparation.pk}/",
            {"attempt_id": str(self.attempt.pk)})

    def stored(self):
        row = ExecutionTextResponseReview.objects.get(attempt=self.attempt)
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(json.loads(original)["history"], self.peer.history.return_value)
        self.assertEqual(json.loads(original)["request_id"], str(self.attempt.pk))
        self.assertEqual(diagnostics["response_source"], "comfyui_history")
        self.assertIsNone(row.confirmed_at)
        return row

    def test_success_archives_actual_png_once_and_reads_receipt_plus_media(self):
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        row = self.stored()
        self.assertEqual(GeneratedArtifact.objects.get().status, "available")
        self.peer.history.assert_called_once_with(str(self.attempt.pk))
        self.cycle()
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.peer.history.assert_called_once()
        response = self.detail()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["output"]["state"], "available")
        self.assertEqual(len(response.data["output"]["media"]), 1)
        self.assertIn(str(self.attempt.pk), response.data["output"]["text"])
        self.assertIsNone(describe_text_response_review(self.preparation))
        self.assertTrue(row.original_file)
        lookup = ExecutionTextResponseReview.objects.filter(attempt=self.attempt).order_by("pk")[:1]
        self.assertNotIn("SELECT *", str(lookup.query).upper())
        if connection.vendor == "sqlite":
            plan = lookup.explain().upper()
            self.assertIn("SEARCH", plan)
            self.assertIn("USING INDEX", plan)
            self.assertNotIn("SCAN STUDIO_EXECUTIONTEXTRESPONSEREVIEW", plan)

    def test_invalid_empty_duplicate_wrong_node_and_remote_error_remain_readable(self):
        cases = [
            {**self.history, "outputs": {}},
            {**self.history, "outputs": ["malformed-node-collection"]},
            {**self.history, "outputs": {"7": {"images": self.history["outputs"]["7"]["images"] * 2}}},
            {**self.history, "outputs": {"wrong": self.history["outputs"]["7"]}},
            {**self.history, "status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"secret": "private-credential"}]]}},
        ]
        for history in cases:
            with self.subTest(history=history), transaction.atomic():
                self.task.refresh_from_db()
                self.peer.history.return_value = history
                result = self.cycle()
                self.assertEqual(result.status, "failed", result.error_message)
                self.stored()
                self.assertFalse(GeneratedArtifact.objects.exists())
                self.attempt.refresh_from_db()
                self.assertIsNone(self.attempt.actual_cost)
                with CaptureQueriesContext(connection) as queries:
                    response = self.detail()
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["output"]["state"], "available")
                self.assertEqual(response.data["output"]["media"], [])
                body = response.data["output"]["text"]
                self.assertNotIn("private-credential", body)
                self.assertNotIn("private.invalid", body)
                self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
                self.assertIsNone(claim_next_task())
                transaction.set_rollback(True)

    def test_in_progress_or_malformed_state_is_not_a_terminal_receipt(self):
        for status in ({"status_str": "running", "completed": False}, [], None):
            with self.subTest(status=status), transaction.atomic():
                self.task.refresh_from_db()
                self.peer.history.return_value = {"status": status, "outputs": {}}
                result = self.cycle()
                self.assertEqual(result.status, "failed")
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(GeneratedArtifact.objects.exists())
                transaction.set_rollback(True)

    def test_storage_failure_is_distinct_and_does_not_create_candidate(self):
        with patch("studio.services.execution_response_storage.store_response", side_effect=ExecutionResponseStorageError("isolated store failure")):
            result = self.cycle()
        self.assertEqual((result.status, result.submission_state), ("failed", "terminal_succeeded"))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.error_code, "execution_response_storage_failed")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_row_write_failure_cleans_only_new_object(self):
        from django.core.files.storage import default_storage
        with patch.object(ExecutionTextResponseReview, "save", side_effect=RuntimeError("isolated row failure")), \
                patch.object(default_storage, "delete", wraps=default_storage.delete) as cleanup:
            result = self.cycle()
        self.assertEqual(result.status, "failed")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.error_code, "execution_response_storage_failed")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        cleanup.assert_called_once()
        self.assertFalse(default_storage.exists(cleanup.call_args.args[0]))

    def test_archive_rollback_does_not_rollback_received_history(self):
        with patch.object(GeneratedArtifact, "save", side_effect=RuntimeError("isolated archive failure")):
            result = self.cycle()
        self.assertEqual(result.status, "failed")
        self.stored()
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")

    def crash_after_receipt(self):
        with patch("studio.services.task_queue.inspect_task_output_media", side_effect=SystemExit("crash after durable receipt")):
            with self.assertRaises(SystemExit):
                self.cycle()
        row = self.stored()
        GenerationTask.objects.filter(pk=self.task.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        self.peer.history.reset_mock()
        self.peer.history.side_effect = AssertionError("No history GET after durable receipt")
        return row

    def test_stale_archive_resume_uses_original_receipt_with_zero_remote_io(self):
        row = self.crash_after_receipt()
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No client needed for recovery")):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
        claim = claim_next_task()
        self.assertEqual(claim.pk, self.task.pk)
        result = self.cycle(claim)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(ExecutionTextResponseReview.objects.get().original_file, row.original_file)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.peer.history.assert_not_called()

    def test_missing_stored_object_fails_without_recollection(self):
        row = self.crash_after_receipt()
        from django.core.files.storage import default_storage
        default_storage.delete(row.original_file)  # Isolated fixture object only.
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No remote fallback")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
        advance.assert_called_once_with(self.task.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.error_code, "execution_response_storage_failed")
        self.assertIsNone(claim_next_task())
        self.peer.history.assert_not_called()
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_claim_loss_and_request_mismatch_cannot_retain(self):
        for mutate in (
            lambda: GenerationTask.objects.filter(pk=self.task.pk).update(attempts=2),
            lambda: GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64),
        ):
            with transaction.atomic():
                mutate()
                with self.assertRaises((TaskClaimLost, ValidationError)):
                    retain_prepared_generation_history(self.task, self.history)
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                transaction.set_rollback(True)

    def test_waiting_root_does_not_block_original_result_but_cancelled_root_settles(self):
        for status in ("awaiting_input", "blocked", "awaiting_approval", "cancelled"):
            with self.subTest(status=status), transaction.atomic():
                self.task.refresh_from_db()
                type(self.run).objects.filter(pk=self.run.pk).update(status=status)
                if status in {"blocked", "awaiting_approval"}:
                    type(self.stage).objects.filter(pk=self.stage.pk).update(status=status)
                result = self.cycle()
                self.assertEqual(result.status, "cancelled" if status == "cancelled" else "succeeded",
                    result.error_message)
                self.assertEqual(ExecutionTextResponseReview.objects.exists(), status != "cancelled")
                self.assertEqual(GeneratedArtifact.objects.exists(), status != "cancelled")
                self.assertIsNotNone(result.finished_at)
                self.assertIsNone(claim_next_task())
                transaction.set_rollback(True)

    def archive_failure(self):
        with patch.object(GeneratedArtifact, "save", side_effect=RuntimeError("isolated archive failure")):
            self.assertEqual(self.cycle().status, "failed")
        self.preparation.refresh_from_db()
        self.task.refresh_from_db()
        row = self.stored()
        return {"expected_preparation_revision": self.preparation.revision, "task_id": str(self.task.pk),
            "attempt_id": str(self.attempt.pk), "task_updated_at": self.task.updated_at.isoformat(),
            "receipt_sha256": row.original_sha256}

    def test_explicit_archive_recovery_reuses_original_receipt_and_audits_failure(self):
        from studio.models import PlatformLifecycleAuditEvent
        payload = self.archive_failure()
        url = f"/api/v1/execution-preparations/{self.preparation.pk}/recover-media-archive/"
        type(self.run).objects.filter(pk=self.run.pk).update(status="blocked")
        type(self.stage).objects.filter(pk=self.stage.pk).update(status="blocked")
        self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        event = PlatformLifecycleAuditEvent.objects.get(action="recover_media_archive")
        self.assertEqual(event.before_summary["error_code"], "execution_output_invalid")
        self.assertNotIn("error_message", event.before_summary)
        self.assertEqual(len(event.before_summary["error_message_sha256"]), 64)
        self.assertEqual(event.before_summary["receipt_sha256"], payload["receipt_sha256"])
        self.assertFalse(event.after_summary["new_model_call"])
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 409)
        self.peer.history.reset_mock()
        self.peer.history.side_effect = AssertionError("No remote history for local recovery")
        claim = claim_next_task()
        self.assertEqual(claim.pk, self.task.pk)
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No client or DNS for archival")), \
                patch("studio.services.task_distribution_recovery.prepare_all_task_distributions", return_value=[]), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(claim)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.peer.history.assert_not_called()
        self.peer.queue_prompt.assert_not_called()

    def test_archive_recovery_rejects_changed_receipt_cancelled_and_foreign_owner(self):
        payload = self.archive_failure()
        url = f"/api/v1/execution-preparations/{self.preparation.pk}/recover-media-archive/"
        self.assertEqual(self.client.post(url, {**payload, "receipt_sha256": "0" * 64}, format="json").status_code, 409)
        other = get_user_model().objects.create_user(username="foreign-archive-reader")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 404)
        self.client.force_authenticate(self.user)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(ignore_late_result=True)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 409)
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_archive_recovery_missing_bytes_does_not_fetch_or_submit(self):
        from django.core.files.storage import default_storage
        payload = self.archive_failure()
        default_storage.delete(self.stored().original_file)  # Isolated fixture only.
        response = self.client.post(f"/api/v1/execution-preparations/{self.preparation.pk}/recover-media-archive/", payload, format="json")
        self.assertIn(response.status_code, (400, 409))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertIsNone(claim_next_task())

    def test_archive_recovery_staff_can_recover_another_owners_task(self):
        payload = self.archive_failure()
        staff = get_user_model().objects.create_user(username="archive-operator", is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.post(f"/api/v1/execution-preparations/{self.preparation.pk}/recover-media-archive/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_cancel_recovered_archive_preserves_remote_success_without_client(self):
        payload = self.archive_failure()
        response = self.client.post(f"/api/v1/execution-preparations/{self.preparation.pk}/recover-media-archive/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        claim = claim_next_task()
        GenerationTask.objects.filter(pk=claim.pk).update(status="cancel_requested")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(ignore_late_result=True, cancel_requested_at=timezone.now())
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No remote client for completed task")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(claim)
        self.assertEqual((result.status, result.submission_state, result.remote_state), ("cancelled", "terminal_succeeded", "succeeded"))
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.ignore_late_result)

    def test_receipt_correction_and_foreign_owner_are_rejected(self):
        self.peer.history.return_value = {**self.history, "outputs": {}}
        self.cycle()
        row = self.stored()
        response = self.client.post(f"/api/v1/execution-preparations/{self.preparation.pk}/complete-text-response/", {
            "expected_preparation_revision": self.preparation.revision, "review_id": str(row.pk),
            "original_sha256": row.original_sha256, "text": "Not a media candidate"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        row.refresh_from_db()
        self.assertIsNone(row.confirmed_at)
        other = get_user_model().objects.create_user(username="foreign-receipt-reader")
        self.client.force_authenticate(other)
        self.assertEqual(self.detail().status_code, 404)

    def test_cancel_after_success_check_or_error_history_keeps_original_settlement(self):
        for remote_error in (False, True):
            with self.subTest(remote_error=remote_error), transaction.atomic():
                self.task.refresh_from_db()
                history = deepcopy(self.history)
                if remote_error:
                    history["status"] = {"status_str": "error", "completed": False}
                self.peer.history.return_value = history
                original = retain_prepared_generation_history
                def cancel_before_store(task, returned):
                    GenerationTask.objects.filter(pk=task.pk).update(status="cancel_requested")
                    return original(task, returned)
                with patch("studio.services.task_queue.retain_prepared_generation_history", side_effect=cancel_before_store), \
                        patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
                    result = process_task(self.task, self.peer)
                self.assertEqual(result.status, "cancelled")
                self.assertEqual(result.submission_state, "terminal_failed" if remote_error else "terminal_succeeded")
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(GeneratedArtifact.objects.exists())
                advance.assert_called_once_with(self.task.pk)
                transaction.set_rollback(True)

    def test_stored_error_recovery_advances_once_and_stale_cas_does_not(self):
        self.history["status"] = {"status_str": "error", "completed": False}
        retain_prepared_generation_history(self.task, self.history)
        GenerationTask.objects.filter(pk=self.task.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        for lost in (True, False):
            with self.subTest(lost=lost), transaction.atomic():
                from studio.services.execution_production_workflow_queue import read_prepared_generation_history
                def read(task):
                    history = read_prepared_generation_history(task)
                    if lost:
                        GenerationTask.objects.filter(pk=task.pk).update(heartbeat_at=timezone.now())
                    return history
                with patch("studio.services.task_queue.read_prepared_generation_history", side_effect=read), \
                        patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No peer lookup")), \
                        patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
                    self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0 if lost else 1)
                if lost:
                    advance.assert_not_called()
                else:
                    advance.assert_called_once_with(self.task.pk)
                    self.assertEqual(GenerationAttempt.objects.get(pk=self.attempt.pk).status, "failed")
                transaction.set_rollback(True)

    def test_failed_file_verification_removes_new_object_and_preserves_existing(self):
        from studio.services.execution_response_storage import store_response
        from django.core.files.storage import default_storage
        existing, _, _ = store_response("existing isolated response")
        with patch("studio.services.execution_response_storage.read_response", side_effect=ExecutionResponseStorageError("digest failed")), \
                patch.object(default_storage, "delete", wraps=default_storage.delete) as cleanup:
            with self.assertRaises(ExecutionResponseStorageError):
                store_response("new response fails readback")
        cleanup.assert_called_once()
        self.assertNotEqual(cleanup.call_args.args[0], existing)
        self.assertFalse(default_storage.exists(cleanup.call_args.args[0]))
        self.assertTrue(default_storage.exists(existing))
