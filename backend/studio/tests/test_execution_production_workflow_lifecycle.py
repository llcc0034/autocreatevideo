"""Original Comfy Task/Attempt lifecycle and ledger, using isolated real rows.

Remote responses are controlled protocol observations, not GPU acceptance.
The full PNG/staging/provenance path is tested separately in artifacts tests.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionRun
from studio.services.comfyui import ComfyUIError, ComfyUIHTTPError
from studio.services.execution_production_workflow_queue import update_prepared_generation_lifecycle
from studio.services.task_queue import (
    _claimed_task_rows, _finalize_cancelled, claim_next_task, fail_claimed_task_preflight,
    process_task, recover_stale_tasks, TaskClaimLost,
)
from studio.tests import test_execution_production_workflow_queue as queue_fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionWorkflowLifecycleTests(TestCase):
    _create_version = queue_fixtures.ProductionWorkflowQueueTests._create_version
    resolve = queue_fixtures.ProductionWorkflowQueueTests.resolve
    setUp = queue_fixtures.ProductionWorkflowQueueTests.setUp

    def test_pause_after_claim_defers_unsubmitted_task_without_failure_or_cost_release(self):
        for state in ("awaiting_input", "blocked", "awaiting_approval"):
            with self.subTest(state=state):
                GenerationTask.objects.filter(pk=self.task.pk).update(status="running")
                self.task.refresh_from_db()
                from studio.services.task_queue import _ensure_task_deadlines
                _ensure_task_deadlines(self.task)
                ProductionRun.objects.filter(pk=self.run.pk).update(status=state)
                peer = Mock()
                result = process_task(self.task, peer)
                self.assertEqual(result.status, "queued")
                self.assertIn("等待处理", result.error_message)
                self.assertIsNone(result.finished_at)
                self.assertIsNone(result.deadline_started_at)
                self.assertFalse(result.submission_token)
                self.attempt.refresh_from_db()
                self.assertEqual(self.attempt.status, "queued")
                self.assertIsNone(self.attempt.submission_started_at)
                self.assert_cost(released=False)
                self.assertEqual(peer.mock_calls, [])
                self.assertEqual(GenerationAttempt.objects.count(), 1)
        ProductionRun.objects.filter(pk=self.run.pk).update(status="running")
        resumed = claim_next_task()
        self.assertEqual(resumed.pk, self.task.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertIsNone(resumed.submission_prepared_at)

    def test_pause_between_staging_and_post_waits_and_preserves_uploaded_receipts(self):
        from studio.services.execution_production_workflow_queue import begin_prepared_generation_submission
        from studio.services.task_queue import PreparedGenerationWaiting, defer_prepared_generation_task
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[{"receipt": "isolated-upload"}])
        self.task.refresh_from_db()
        ProductionRun.objects.filter(pk=self.run.pk).update(status="awaiting_input")
        with self.assertRaises(PreparedGenerationWaiting) as caught:
            begin_prepared_generation_submission(self.task)
        result = defer_prepared_generation_task(self.task, caught.exception)
        self.assertEqual(result.status, "queued")
        result.refresh_from_db()
        self.assertEqual(result.staged_inputs, [{"receipt": "isolated-upload"}])
        with self.assertRaises(TaskClaimLost):
            defer_prepared_generation_task(self.task, caught.exception)
        self.assertFalse(result.submission_token)

    def submitted(self, **changes):
        now = timezone.now()
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitted",
            submission_started_at=now, submitted_at=now, provider_request_id=str(self.attempt.pk))
        GenerationTask.objects.filter(pk=self.task.pk).update(submission_token=str(self.attempt.pk),
            comfy_prompt_id=str(self.attempt.pk), submission_state="submitted", **changes)
        self.task.refresh_from_db()
        peer = Mock(spec=["history", "is_queued", "cancel", "queue_prompt", "extract_outputs"])
        peer.history.return_value = None
        peer.is_queued.return_value = False
        return peer

    def assert_cost(self, *, released):
        self.run.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.actual_cost)
        self.assertEqual(self.run.actual_cost_total, Decimal("0"))
        self.assertEqual(self.attempt.committed_cost, Decimal("0") if released else self.attempt.estimated_cost)
        self.assertEqual(self.run.committed_cost_total, self.attempt.committed_cost)

    def test_exact_cancel_before_submission_uses_original_task_ledger_bridge(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation
        self.preparation.refresh_from_db()
        cancel_production_preparation(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.task.refresh_from_db(); self.attempt.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((self.task.status, self.attempt.status), ("cancelled", "cancelled"))
        self.assertEqual(self.run.status, "running")
        self.assert_cost(released=True)

    def test_exact_cancel_submitted_workflow_waits_for_remote_confirmation(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation
        self.submitted()
        self.preparation.refresh_from_db()
        cancel_production_preparation(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.task.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual((self.task.status, self.attempt.status), ("cancel_requested", "cancel_requested"))
        self.assertEqual(self.attempt.provider_request_id, str(self.attempt.pk))
        self.assertIsNone(self.attempt.finished_at)
        self.assert_cost(released=False)

    def test_preflight_failure_records_actual_attempt_even_when_request_is_invalid(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = fail_claimed_task_preflight(self.task, ValidationError("invalid frozen request"))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.submission_token)
        self.assert_cost(released=True)
        self.assertEqual(self.attempt.status, "failed")
        self.assertEqual(self.attempt.request_sha256, "0" * 64)  # diagnose, never repair
        self.assertEqual(self.attempt.error_code, "execution_workflow_failed")
        self.assertEqual(self.attempt.finished_at, result.finished_at)
        advance.assert_called_once_with(self.task.pk)

    def test_post_rejection_and_uncertainty_have_different_reservation_semantics(self):
        self.attempt.refresh_from_db()
        amount = self.attempt.committed_cost
        observations = [
            (ComfyUIHTTPError(400, "invalid prompt"), True),
            (ComfyUIHTTPError(503, "upstream failed after possible acceptance"), False),
            (ComfyUIError("connection lost after POST"), False),
            (SimpleNamespace(prompt_id="foreign-remote-id", node_errors={}), False),
        ]
        for response, rejected in observations:
            with self.subTest(response=response):
                GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="queued", submission_started_at=None,
                    submitted_at=None, provider_request_id="", committed_cost=amount, error_code="", finished_at=None)
                ProductionRun.objects.filter(pk=self.run.pk).update(committed_cost_total=amount)
                GenerationTask.objects.filter(pk=self.task.pk).update(status="running", submission_state="unprepared",
                    submission_token="", comfy_prompt_id="", submission_prepared_at=None, submission_submitted_at=None,
                    finished_at=None)
                self.task.refresh_from_db()
                peer = Mock(spec=["queue_prompt"])
                if isinstance(response, Exception):
                    peer.queue_prompt.side_effect = response
                else:
                    peer.queue_prompt.return_value = response
                with patch("studio.services.task_queue.prepare_workflow", return_value={}), \
                        patch("studio.services.task_queue.preflight_comfyui_workflow",
                            return_value=SimpleNamespace(as_dict=lambda: {"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})), \
                        patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                            side_effect=production_observations), \
                        patch("studio.services.task_queue.schedule_generation_task_run_advance"):
                    result = process_task(self.task, peer)
                self.assertEqual(result.status, "failed", result.error_message)
                self.assertEqual(result.submission_token, str(self.attempt.pk))
                self.assertEqual(result.submission_state, "terminal_failed" if rejected else "prepared")
                self.assert_cost(released=rejected)
                self.assertEqual(self.attempt.status, "failed")
                self.assertEqual(self.attempt.error_code, "execution_submission_rejected" if rejected else "uncertain_remote_response")
                self.assertFalse(self.attempt.provider_request_id)
                self.assertIsNone(self.attempt.submitted_at)
                peer.queue_prompt.assert_called_once()
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_cancel_waits_for_remote_confirmation_and_never_releases_submitted_cost(self):
        peer = self.submitted(status="cancel_requested")
        peer.is_queued.return_value = True
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(self.task, peer)
            self.assertEqual(result.status, "cancel_requested")
            self.assertIsNone(result.finished_at)
            self.assertEqual(result.submission_state, "submitted")
            self.assert_cost(released=False)
            self.assertEqual(self.attempt.status, "cancel_requested")
            self.assertTrue(self.attempt.ignore_late_result)
            peer.is_queued.return_value = False
            result = process_task(result, peer)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.submission_state, "terminal_cancelled")
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.status, "cancelled")
        peer.queue_prompt.assert_not_called()

    def test_cancel_before_submission_releases_only_the_original_reservation(self):
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            _finalize_cancelled(self.task, remote_confirmed=True)
        self.assertEqual(self.task.status, "cancelled")
        self.assert_cost(released=True)
        self.assertEqual(self.attempt.status, "cancelled")
        self.assertTrue(self.attempt.ignore_late_result)
        with self.assertRaises(TaskClaimLost):
            _finalize_cancelled(self.task, remote_confirmed=True)
        self.assert_cost(released=True)

    def test_missing_remote_history_never_clears_uncertainty_or_allocates_retry(self):
        peer = self.submitted(heartbeat_at=timezone.now() - timedelta(minutes=10))
        original_deadline = self.task.hard_deadline_at
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(self.task.submission_state, "submitted")
        self.assertEqual(self.task.submission_token, str(self.attempt.pk))
        self.assertEqual(self.task.hard_deadline_at, original_deadline)
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        self.assertEqual(GenerationAttempt.objects.filter(preparation=self.preparation).count(), 1)
        self.assertIsNone(claim_next_task())
        peer.queue_prompt.assert_not_called()

    def test_recovery_restores_same_attempt_without_rewinding_claims_or_deadlines(self):
        peer = self.submitted(status="failed")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="failed", error_code="uncertain_remote_response")
        peer.is_queued.return_value = True
        original_claims, original_deadline = self.task.attempts, self.task.hard_deadline_at
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "queued")
        self.assertEqual(self.task.attempts, original_claims)
        self.assertEqual(self.task.submission_state, "recovered")
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.status, "submitted")
        self.assertFalse(self.attempt.error_code)
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertEqual(claimed.attempts, original_claims + 1)
        self.assertEqual(claimed.hard_deadline_at, original_deadline)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=self.preparation).count(), 1)
        peer.queue_prompt.assert_not_called()

    def test_recovery_response_losing_claim_cannot_change_attempt_or_ledger(self):
        peer = self.submitted(heartbeat_at=timezone.now() - timedelta(minutes=10))
        def missing(*_):
            GenerationTask.objects.filter(pk=self.task.pk).update(heartbeat_at=timezone.now())
            return None
        peer.history.side_effect = missing
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.status, "submitted")
        self.assertFalse(self.attempt.error_code)

    def test_remote_success_with_invalid_output_is_not_uncertain_or_platform_success(self):
        peer = self.submitted()
        peer.history.return_value = {"status": {"completed": True, "status_str": "success"}}
        peer.extract_outputs.return_value = [{"test": "invalid returned file"}]
        with patch("studio.services.task_queue.inspect_task_output_media", side_effect=ValidationError("wrong media dimensions")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"), \
                patch("studio.services.task_queue.record_generation_task_artifacts") as archive:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.submission_state, "terminal_succeeded")
        self.assertEqual(result.remote_state, "succeeded")
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.status, "failed")
        self.assertEqual(self.attempt.error_code, "execution_output_invalid")
        with patch("studio.services.task_queue.comfy_client_for_task") as connect:
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
        connect.assert_not_called()
        archive.assert_not_called()
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_incomplete_remote_history_cannot_be_archived_as_completed(self):
        peer = self.submitted()
        peer.history.return_value = {"status": {"completed": False, "status_str": "running"}}
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"), \
                patch("studio.services.task_queue.record_generation_task_artifacts") as archive:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.submission_state, "submitted")
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        archive.assert_not_called()
        peer.extract_outputs.assert_not_called()

    def test_invalid_ledger_rolls_back_both_task_and_attempt(self):
        ProductionRun.objects.filter(pk=self.run.pk).update(committed_cost_total=Decimal("0"))
        with self.assertRaisesMessage(ValidationError, "账目不一致"):
            fail_claimed_task_preflight(self.task, ValueError("test preflight failure"))
        self.task.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.task.status, "running")
        self.assertEqual(self.attempt.status, "queued")
        self.assertIsNone(self.attempt.finished_at)
        self.assertGreater(self.attempt.committed_cost, 0)

    def test_terminal_write_uses_root_locks_and_rejects_false_cancel_or_rejection(self):
        self.submitted()
        from django.db.models import QuerySet
        expected = ["ProductionRun", "StageRun", "ExecutionPreparation", "GenerationAttempt", "GenerationTask"]
        original, locks = QuerySet.select_for_update, []
        def record(query, *args, **kwargs):
            if query.model.__name__ in expected:
                locks.append(query.model.__name__)
            return original(query, *args, **kwargs)
        with patch.object(QuerySet, "select_for_update", record), self.assertRaisesMessage(ValidationError, "取消尚未确认"):
            update_prepared_generation_lifecycle(self.task, {"status": "cancelled"}, condition=_claimed_task_rows(self.task))
        self.assertEqual(locks, expected)
        with self.assertRaisesMessage(ValidationError, "确认接受"):
            update_prepared_generation_lifecycle(self.task, {"status": "failed", "submission_state": "terminal_failed"},
                condition=_claimed_task_rows(self.task), rejected=True)
        self.assert_cost(released=False)
        self.assertEqual(self.attempt.status, "submitted")
