"""Current Preparation-backed durable polling and submission failure tests.

Archive, capacity, claim recovery, staging and recipe delivery have their own
execution_production_workflow_* suites. A controlled remote history below is
not a media artifact or GPU acceptance result.
"""
from contextlib import contextmanager
from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ConfigRevision, GenerationAttempt, GenerationTask
from studio.services.comfyui import ComfyUIError, ComfyUIHTTPError, ProgressSocketEvent
from studio.services.comfyui_contract import ComfyUIContractError, ComfyUIContractUnavailable
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.execution_production_workflow_queue import read_prepared_generation_task
from studio.services.task_queue import (
    _open_task_progress_socket, _wait_for_task_completion, process_task,
    RemoteExecutionStalled, SubmissionUncertain, TaskCancellationFinalized,
)
from studio.tests import test_execution_production_workflow_queue as fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",), TASK_PROGRESS_WEBSOCKET_ENABLED=True)
class TaskQueueTests(TestCase):
    _create_version = fixtures.ProductionWorkflowQueueTests._create_version
    resolve = fixtures.ProductionWorkflowQueueTests.resolve
    stage_workflow = fixtures.ProductionWorkflowQueueTests.stage_workflow
    setUp = fixtures.ProductionWorkflowQueueTests.setUp

    def runtime(self):
        revision = create_config_revision(scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"runtime.task_remote_poll_seconds": 0.05, "runtime.task_heartbeat_seconds": 0.05,
                "runtime.task_health_check_interval_seconds": 0, "runtime.task_health_failure_limit": 2,
                "runtime.task_remote_missing_grace_seconds": 1, "runtime.task_cancel_grace_seconds": 1},
            created_by=None, change_summary="isolated durable transport test", clone_published=True)
        publish_config_revision(revision, published_by=None)

    def submitted(self):
        now, token = timezone.now(), str(self.attempt.pk)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitted",
            submission_started_at=now, submitted_at=now, provider_request_id=token)
        GenerationTask.objects.filter(pk=self.task.pk).update(submission_token=token,
            comfy_prompt_id=token, submission_state="submitted", client_id=str(self.preparation.pk))
        self.task.refresh_from_db()
        peer = Mock(spec=["history", "is_queued", "health", "cancel", "queue_prompt",
            "open_progress_socket", "read_progress_event"])
        peer.history.return_value = {"status": {"completed": True, "status_str": "success"}}
        peer.is_queued.return_value = True
        peer.health.return_value = {}
        peer.cancel.return_value = None
        peer.read_progress_event.return_value = None
        return peer

    def wait(self, peer, socket=None):
        # Advance only polling time; the original DB heartbeats, cancellation
        # and frozen deadlines are still checked on every iteration.
        ticks = count(1, 0.2)
        with patch("studio.services.task_queue.time",
                SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)):
            return _wait_for_task_completion(self.task, peer, self.task.submission_token,
                progress_socket=socket)

    def test_wait_requires_durable_history_queue_and_progress_contracts(self):
        for peer in (SimpleNamespace(history=lambda _: None), SimpleNamespace(is_queued=lambda _: True)):
            with self.subTest(peer=peer), self.assertRaisesMessage(ComfyUIError, "durable history/is_queued"):
                self.wait(peer)
        with self.assertRaisesMessage(ComfyUIError, "read_progress_event"):
            self.wait(SimpleNamespace(history=lambda _: None, is_queued=lambda _: True), Mock())

    def test_history_is_not_an_archived_success_and_never_reposts(self):
        peer = self.submitted()
        self.assertTrue(self.wait(peer)["status"]["completed"])
        self.task.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual((self.task.status, self.attempt.status), ("running", "submitted"))
        self.assertEqual(self.task.outputs, [])
        self.assertFalse(self.task.generated_artifacts.exists())
        peer.queue_prompt.assert_not_called()

    def test_sampler_progress_is_persisted_without_sliding_node_deadlines(self):
        self.runtime()
        peer = self.submitted()
        graph = read_prepared_generation_task(self.task).media["payload"]["compiled_workflow"]["graph"]
        node = next(key for key, row in graph.items() if row["class_type"] == "LoadImage")
        peer.read_progress_event.side_effect = [
            ProgressSocketEvent(telemetry_state="receiving", event_type="progress",
                prompt_id=self.task.submission_token, node_id=node, fraction=f) for f in (0.2, 0.5, 0.8)]
        seen = []
        def history(_):
            row = GenerationTask.objects.get(pk=self.task.pk)
            seen.append((row.comfy_progress, row.progress, row.idle_deadline_at,
                row.hard_deadline_at, row.progress_telemetry_state))
            return {"status": {"completed": True}} if len(seen) == 3 else None
        peer.history.side_effect = history
        hard = self.task.hard_deadline_at
        self.wait(peer, Mock())
        self.assertEqual([row[0] for row in seen], [20, 50, 80])
        self.assertTrue(all(10 < row[1] < 100 and row[4] == "receiving" for row in seen))
        self.assertEqual(len({row[2] for row in seen}), 1)
        self.assertTrue(all(row[3] == hard for row in seen))
        self.task.refresh_from_db()
        self.assertEqual((self.task.progress_node_id, self.task.progress_source), (node, "comfyui"))

    def test_read_failure_closes_socket_without_blocking_http_history(self):
        peer, socket = self.submitted(), Mock()
        peer.read_progress_event.side_effect = RuntimeError("isolated socket reset")
        self.assertTrue(self.wait(peer, socket)["status"]["completed"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress_telemetry_state, "unavailable")
        self.assertIn("socket reset", self.task.progress_telemetry_error)
        socket.close.assert_called_once()
        peer.queue_prompt.assert_not_called()

    def test_open_failure_is_visible_but_does_not_block_durable_polling(self):
        peer = self.submitted()
        peer.open_progress_socket.side_effect = RuntimeError("isolated handshake refused")
        self.assertIsNone(_open_task_progress_socket(self.task, peer))
        self.wait(peer)
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress_telemetry_state, "unavailable")
        self.assertIn("handshake refused", self.task.progress_telemetry_error)
        self.assertEqual(self.task.status, "running")

    def test_transient_poll_errors_recover_without_new_submission(self):
        self.runtime()
        peer = self.submitted()
        peer.history.side_effect = [ComfyUIError("history timeout"), None, {"status": {"completed": True}}]
        peer.is_queued.side_effect = [ComfyUIError("queue timeout"), True]
        self.wait(peer)
        self.task.refresh_from_db()
        self.assertEqual((peer.history.call_count, peer.health.call_count), (3, 2))
        self.assertEqual(self.task.remote_error, "")
        self.assertEqual(self.task.submission_token, str(self.attempt.pk))
        peer.queue_prompt.assert_not_called()
        peer.cancel.assert_not_called()

    def test_poll_errors_still_use_bounded_health_detection_not_fake_remote_terminal(self):
        self.runtime()
        peer = self.submitted()
        peer.history.side_effect = [ComfyUIError("history timeout")] * 5
        peer.is_queued.side_effect = ComfyUIError("queue timeout")
        peer.health.side_effect = ComfyUIError("health unavailable")
        with self.assertRaises(RemoteExecutionStalled) as caught:
            self.wait(peer)
        self.assertEqual(caught.exception.remote_status["reason"], "health_check_failed")
        self.assertFalse(caught.exception.remote_status["terminal_confirmed"])
        self.assertEqual(peer.health.call_count, 2)
        peer.cancel.assert_called_once_with(str(self.attempt.pk))
        peer.queue_prompt.assert_not_called()
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.stalled_at)
        self.assertEqual(self.task.submission_state, "submitted")

    def test_health_success_resets_consecutive_failure_budget(self):
        self.runtime()
        peer = self.submitted()
        peer.history.side_effect = [None, None, None, {"status": {"completed": True}}]
        peer.health.side_effect = [ComfyUIError("first"), {}, ComfyUIError("separate")]
        self.wait(peer)
        self.assertEqual(peer.health.call_count, 3)
        peer.cancel.assert_not_called()

    def test_unhealthy_peer_cancellation_requires_confirmed_queue_absence(self):
        self.runtime()
        peer = self.submitted()
        peer.history.return_value = None
        peer.health.side_effect = ComfyUIError("isolated unhealthy peer")
        peer.is_queued.side_effect = [True, True, False]
        with self.assertRaises(RemoteExecutionStalled) as caught:
            self.wait(peer)
        self.assertTrue(caught.exception.remote_status["terminal_confirmed"])
        peer.cancel.assert_called_once()
        peer.queue_prompt.assert_not_called()

    def test_database_cancel_is_observed_during_polling(self):
        self.runtime()
        peer = self.submitted()
        def cancel_in_history(_):
            GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
            return None
        peer.history.side_effect = cancel_in_history
        peer.is_queued.side_effect = lambda _: peer.cancel.call_count == 0
        with self.assertRaises(TaskCancellationFinalized):
            self.wait(peer)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        peer.cancel.assert_called_once()
        peer.queue_prompt.assert_not_called()

    def test_remote_missing_grace_never_resubmits(self):
        self.runtime()
        peer = self.submitted()
        peer.history.side_effect = [None] * 10
        peer.is_queued.return_value = False
        with self.assertRaises(SubmissionUncertain):
            self.wait(peer)
        peer.queue_prompt.assert_not_called()
        peer.cancel.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.remote_state, "missing")
        self.assertEqual(self.task.submission_token, str(self.attempt.pk))

    def test_cancel_timeout_keeps_remote_unresolved(self):
        self.runtime()
        peer = self.submitted()
        GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
        peer.history.side_effect = [None] * 10
        with self.assertRaises(RemoteExecutionStalled) as caught:
            self.wait(peer)
        self.assertEqual(caught.exception.remote_status["reason"], "cancel_timeout")
        self.assertFalse(caught.exception.remote_status["terminal_confirmed"])
        peer.cancel.assert_called_once()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancel_requested")

    @contextmanager
    def submission_peer(self, *, post_error=None, preflight_error=None):
        graph = self.stage_workflow()
        peer = Mock(spec=["queue_prompt"])
        peer.queue_prompt.side_effect = post_error
        report = SimpleNamespace(as_dict=lambda: {"node_count": len(graph), "link_count": 0,
            "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})
        # Transport boundary only, never a successful artifact mock. Original
        # staging/admission/token/rejection services and persistence execute.
        with patch("studio.services.task_queue.prepare_workflow", return_value=graph), \
                patch("studio.services.task_queue.preflight_comfyui_workflow",
                    return_value=report, side_effect=preflight_error), \
                patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                    side_effect=production_observations):
            yield peer

    def test_definitive_http_rejection_is_not_uncertain_or_automatically_retried(self):
        with self.submission_peer(post_error=ComfyUIHTTPError(400, '{"error":"prompt_outputs_failed_validation"}')) as peer:
            result = process_task(self.task, peer)
        self.assertEqual((result.status, result.submission_state), ("failed", "terminal_failed"))
        self.assertIn("prompt_outputs_failed_validation", result.error_message)
        self.assertEqual(result.submission_token, str(self.attempt.pk))
        peer.queue_prompt.assert_called_once()

    def test_transport_loss_preserves_token_without_automatic_repost(self):
        with self.submission_peer(post_error=ComfyUIError("connection reset")) as peer:
            result = process_task(self.task, peer)
        self.assertEqual((result.status, result.submission_state), ("failed", "prepared"))
        self.assertEqual(result.submission_token, str(self.attempt.pk))
        self.assertIn("人工核验", result.error_message)
        peer.queue_prompt.assert_called_once()

    def test_http_server_error_is_uncertain_not_definitive_rejection(self):
        with self.submission_peer(post_error=ComfyUIHTTPError(503, "controlled server error")) as peer:
            result = process_task(self.task, peer)
        self.assertEqual((result.status, result.submission_state), ("failed", "prepared"))
        self.assertIn("是否已接受未知", result.error_message)
        peer.queue_prompt.assert_called_once()

    def test_live_contract_mismatch_has_no_token_or_post(self):
        with self.submission_peer(preflight_error=ComfyUIContractError(["controlled mismatch"])) as peer:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.submission_token)
        self.assertIn("controlled mismatch", result.error_message)
        peer.queue_prompt.assert_not_called()

    def test_unreachable_preflight_requires_explicit_retry_not_claim_count_retry(self):
        with self.submission_peer(preflight_error=ComfyUIContractUnavailable("controlled unavailable")) as peer:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.submission_token)
        self.assertEqual(result.attempts, 1)
        peer.queue_prompt.assert_not_called()

    def test_persisted_remote_token_missing_at_recovery_never_posts_again(self):
        peer = self.submitted()
        peer.history.return_value, peer.is_queued.return_value = None, False
        result = process_task(self.task, peer)
        self.assertEqual((result.status, result.submission_state), ("failed", "submitted"))
        self.assertEqual(result.submission_token, str(self.attempt.pk))
        peer.queue_prompt.assert_not_called()
        self.assertFalse(result.generated_artifacts.exists())

    def test_terminal_remote_error_does_not_archive_outputs(self):
        peer = self.submitted()
        peer.history.return_value = {"status": {"status_str": "error", "completed": False,
            "messages": [["execution_error", {"exception_message": "controlled remote failure"}]]}}
        result = process_task(self.task, peer)
        self.assertEqual((result.status, result.submission_state), ("failed", "terminal_failed"))
        self.assertIn("controlled remote failure", result.error_message)
        peer.queue_prompt.assert_not_called()
        self.assertFalse(result.generated_artifacts.exists())
