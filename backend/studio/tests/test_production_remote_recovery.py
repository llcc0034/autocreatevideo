"""Parent-state synchronization with controlled remote observations, no GPU calls."""
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings

from studio.models import GenerationAttempt, GenerationTask, ProductionRecipeVersion, ProductionRun, StageRun
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.production_execution_recovery import reconcile_recovered_media_stage
from studio.services.production_run_auto_advance import reconcile_recovered_recipe_runs
from studio.services.task_queue import recover_stale_tasks
from studio.tests import test_execution_production_workflow_lifecycle as lifecycle
from studio.tests import test_execution_production_workflow_queue as queue


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionRemoteRecoveryTests(TestCase):
    _create_version = queue.ProductionWorkflowQueueTests._create_version
    resolve = queue.ProductionWorkflowQueueTests.resolve
    submitted = lifecycle.ProductionWorkflowLifecycleTests.submitted
    another_queued_call = queue.ProductionWorkflowQueueTests.another_queued_call

    def setUp(self):
        queue.ProductionWorkflowQueueTests.setUp(self)
        # A root-classification fixture, never submitted to the recipe executor.
        self.recipe = ProductionRecipeVersion.objects.create(recipe_key="recovery-state-fixture",
            version=1, name="Isolated recovery state")

    def block(self, *, mode="recipe", run_status="blocked", stage_status="blocked", code="uncertain_remote_response"):
        # The real prepared-task fixture starts in manual mode. Only root-state
        # classification is under test here; the original scheduler is patched,
        # not a fake recipe/Plan or a second provider submission.
        ProductionRun.objects.filter(pk=self.run.pk).update(plan_source=mode,
            recipe_version=self.recipe if mode == "recipe" else None, status=run_status,
            blocked_code=code, blocked_reason="original reason", current_stage_key=self.stage.stage_key)
        StageRun.objects.filter(pk=self.stage.pk).update(status=stage_status, reason_code=code, reason="original reason")

    def reconcile(self):
        with locked_preparation_owner_rows([("stage_run_id", self.stage.pk)]) as owners:
            return reconcile_recovered_media_stage(owners[("stage_run_id", str(self.stage.pk))])

    def recovered(self):
        self.submitted(status="running")
        GenerationTask.objects.filter(pk=self.task.pk).update(submission_state="recovered")
        self.block()

    def test_original_recovery_updates_parent_atomically_and_wakes_after_commit(self):
        peer = self.submitted(status="failed")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="failed", error_code="uncertain_remote_response")
        self.block()
        peer.is_queued.return_value = True
        before = (self.attempt.request_sha256, self.task.hard_deadline_at, self.task.attempts)
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer), \
                patch("studio.services.production_run_auto_advance._advance_active_recipe_run") as advance:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
                advance.assert_not_called()
            advance.assert_called_once_with(self.run.pk)
        self.run.refresh_from_db(); self.stage.refresh_from_db()
        self.attempt.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual((self.run.status, self.stage.status), ("running", "running"))
        self.assertEqual((self.run.blocked_code, self.stage.reason_code), ("", ""))
        self.assertEqual((self.attempt.status, self.task.submission_state), ("submitted", "recovered"))
        self.assertEqual((self.attempt.request_sha256, self.task.hard_deadline_at, self.task.attempts), before)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.assertIsNone(self.attempt.actual_cost)
        peer.queue_prompt.assert_not_called()

    def test_maintenance_catches_up_already_recovered_request_once(self):
        self.recovered()
        with patch("studio.services.production_run_auto_advance._advance_active_recipe_run") as advance:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(reconcile_recovered_recipe_runs(heartbeat=lambda: None), 1)
                self.assertEqual(reconcile_recovered_recipe_runs(heartbeat=lambda: None), 0)
            advance.assert_called_once_with(self.run.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_missing_remote_request_keeps_parent_blocked_and_never_resubmits(self):
        peer = self.submitted(status="failed")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="failed", error_code="uncertain_remote_response")
        self.block()
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"), \
                patch("studio.services.production_run_auto_advance._advance_active_recipe_run") as advance:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
                self.assertEqual(reconcile_recovered_recipe_runs(heartbeat=lambda: None), 0)
            advance.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.blocked_code), ("blocked", "uncertain_remote_response"))
        peer.queue_prompt.assert_not_called()

    def test_maintenance_worklist_uses_original_run_status_index(self):
        from django.db import connection
        query = ProductionRun.objects.filter(plan_source="recipe", status="blocked",
            blocked_code="uncertain_remote_response").order_by("id").values_list("id", "current_stage_key")
        self.assertEqual(query.query.values_select, ("id", "current_stage_key"))
        if connection.vendor == "sqlite":
            explanation = query.explain()
            self.assertIn("idx_run_auto_advance", explanation)
            self.assertNotIn("TEMP B-TREE", explanation)

    def test_archived_original_request_also_reconciles_without_reopening_it(self):
        self.recovered()
        GenerationTask.objects.filter(pk=self.task.pk).update(status="succeeded", submission_state="terminal_succeeded")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="succeeded")
        with patch("studio.services.production_run_auto_advance.schedule_recipe_run_advance"):
            self.assertTrue(self.reconcile())
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "succeeded")

    def test_unresolved_peer_and_cancellation_still_block(self):
        _, other, _ = self.another_queued_call()
        self.recovered()
        for changes in ({"status": "failed", "error_code": "uncertain_remote_response", "ignore_late_result": False},
                {"status": "cancel_requested", "error_code": "", "ignore_late_result": False},
                {"status": "submitted", "error_code": "", "ignore_late_result": True}):
            GenerationAttempt.objects.filter(pk=other.pk).update(**changes)
            with self.subTest(changes=changes):
                self.assertFalse(self.reconcile())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "blocked")

    def test_manual_approval_terminal_and_other_reasons_are_not_reopened(self):
        self.recovered()
        cases = [{"mode": "manual"}, {"code": "stage_validation_failed"},
            {"run_status": "awaiting_approval"}, {"stage_status": "awaiting_approval"},
            {"run_status": "awaiting_input"}, {"run_status": "failed"},
            {"run_status": "cancelled"}, {"stage_status": "cancelled"}, {"run_status": "succeeded"}]
        for changes in cases:
            self.block(**changes)
            with self.subTest(changes=changes):
                self.assertFalse(self.reconcile())

    def test_missing_or_mismatched_original_receipt_cannot_resume(self):
        self.recovered()
        changes = [{"submission_state": "submitted"}, {"submission_token": "different-request"},
            {"comfy_prompt_id": "different-request"}, {"status": "failed"}]
        for values in changes:
            self.task.refresh_from_db()
            before = {key: getattr(self.task, key) for key in values}
            GenerationTask.objects.filter(pk=self.task.pk).update(**values)
            with self.subTest(values=values):
                self.assertFalse(self.reconcile())
            GenerationTask.objects.filter(pk=self.task.pk).update(**before)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(error_code="uncertain_remote_response")
        self.assertFalse(self.reconcile())

    def test_foreign_stage_and_rollback_do_not_publish_recovery(self):
        self.recovered()
        ProductionRun.objects.filter(pk=self.run.pk).update(current_stage_key="another-stage")
        self.assertFalse(self.reconcile())
        self.block()
        with patch("studio.services.production_run_auto_advance._advance_active_recipe_run") as advance:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ValueError), transaction.atomic():
                    self.assertTrue(self.reconcile())
                    raise ValueError("rollback all recovery facts")
            advance.assert_not_called()
        self.run.refresh_from_db(); self.stage.refresh_from_db()
        self.assertEqual((self.run.status, self.stage.status), ("blocked", "blocked"))

    def test_maintenance_rechecks_concurrent_cancellation_under_owner_lock(self):
        self.recovered()
        def cancel():
            ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
        self.assertEqual(reconcile_recovered_recipe_runs(heartbeat=cancel), 0)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "blocked")
