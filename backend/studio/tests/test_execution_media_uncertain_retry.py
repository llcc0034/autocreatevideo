"""Isolated real Task/Attempt rows; no remote model requests."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, GenerationTask, PlatformLifecycleAuditEvent
from studio.services.execution_media_uncertain_retry import describe_media_uncertain_retry
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_production_workflow_queue import update_prepared_generation_lifecycle
from studio.services.execution_production_attempts import superseded_uncertain_task_ids, validate_retry_link
from studio.services.task_queue import claim_next_task, recover_stale_tasks
from studio.tests import test_execution_production_workflow_lifecycle as fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class UncertainMediaRetryTests(TestCase):
    _create_version = fixtures.ProductionWorkflowLifecycleTests._create_version
    resolve = fixtures.ProductionWorkflowLifecycleTests.resolve
    submitted = fixtures.ProductionWorkflowLifecycleTests.submitted

    def setUp(self):
        fixtures.ProductionWorkflowLifecycleTests.setUp(self)
        self.submitted(status="running")
        update_prepared_generation_lifecycle(self.task,
            {"status": "failed", "finished_at": timezone.now(), "remote_state": "degraded"},
            condition=GenerationTask.objects.filter(pk=self.task.pk))
        self.task.refresh_from_db(); self.attempt.refresh_from_db(); self.preparation.refresh_from_db()
        self.client = APIClient(); self.client.force_authenticate(self.user)
        self.url = f"/api/v1/execution-preparations/{self.preparation.pk}/retry-uncertain-media/"
        self.payload = {"expected_preparation_revision": self.preparation.revision,
            "task_id": str(self.task.pk), "attempt_id": str(self.attempt.pk),
            "task_updated_at": self.task.updated_at.isoformat(), "request_sha256": self.attempt.request_sha256,
            "acknowledged": True}

    def post(self, payload=None):
        # Retain real dispatch/link/ledger/task allocation, isolate external admission observations.
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=lambda *args, **kwargs: production_observations(*args)), \
                patch("studio.services.production_run_auto_advance.schedule_production_run_advance"):
            return self.client.post(self.url, self.payload if payload is None else payload, format="json")

    def test_explicit_new_attempt_preserves_unknown_facts_and_original_reservation(self):
        original = (self.attempt.status, self.attempt.error_code, self.attempt.actual_cost,
            self.attempt.committed_cost, self.attempt.provider_request_id, self.attempt.submitted_at, self.attempt.request_sha256)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        new = GenerationAttempt.objects.get(retry_of=self.attempt)
        validate_retry_link(new)
        self.assertEqual(new.retry_requested_by, self.user)
        self.assertEqual(new.preparation_id, self.preparation.pk)
        self.assertEqual(new.request_sha256, self.attempt.request_sha256)
        self.assertNotEqual(new.generation_task_id, self.task.pk)
        self.assertEqual(new.status, "queued")
        self.attempt.refresh_from_db(); self.run.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual(original, (self.attempt.status, self.attempt.error_code, self.attempt.actual_cost,
            self.attempt.committed_cost, self.attempt.provider_request_id, self.attempt.submitted_at, self.attempt.request_sha256))
        self.assertTrue(self.attempt.ignore_late_result)
        self.assertEqual(self.run.committed_cost_total, self.attempt.committed_cost + new.committed_cost)
        self.assertEqual(self.task.submission_state, "submitted")
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(PlatformLifecycleAuditEvent.objects.filter(action="retry_uncertain_media").count(), 1)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(GenerationTask.objects.count(), 2)

    def test_read_does_not_retry_and_automatic_retry_still_rejects(self):
        self.assertTrue(describe_media_uncertain_retry(self.preparation)["can_retry"])
        with self.assertRaises(ValidationError):
            dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user,
                retry_of_id=self.attempt.pk, automatic=True)
        with self.assertRaises(ValidationError):
            dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user,
                retry_of_id=self.attempt.pk, automatic=True, acknowledged_uncertain_attempt_id=self.attempt.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_authorized_successor_is_actually_claimed_without_reclaiming_original(self):
        self.assertIsNone(claim_next_task())
        self.assertEqual(self.post().status_code, 200)
        new = GenerationAttempt.objects.get(retry_of=self.attempt)
        claimed = claim_next_task()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, new.generation_task_id)
        self.assertEqual(claimed.submission_state, "unprepared")
        self.task.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(self.task.submission_state, "submitted")
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        self.assertIsNone(claim_next_task())

    def test_ignore_flag_alone_or_mismatched_successor_does_not_release_global_breaker(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(ignore_late_result=True)
        self.assertFalse(superseded_uncertain_task_ids().exists())
        self.assertIsNone(claim_next_task())
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(ignore_late_result=False)
        self.assertEqual(self.post().status_code, 200)
        new = GenerationAttempt.objects.get(retry_of=self.attempt)
        self.assertEqual(list(superseded_uncertain_task_ids()), [{"generation_task_id": self.task.pk}])
        GenerationAttempt.objects.filter(pk=new.pk).update(request_sha256="0"*64)
        self.assertFalse(superseded_uncertain_task_ids().exists())
        self.assertIsNone(claim_next_task())

    def test_unrelated_unresolved_task_still_blocks_authorized_successor(self):
        self.assertEqual(self.post().status_code, 200)
        GenerationTask.objects.create(owner=self.user, project=self.task.project, kind="image",
            status="failed", submission_state="submitted", submission_token="other-unresolved")
        self.assertIsNone(claim_next_task())

    def test_acknowledgment_revision_observation_and_request_required(self):
        for changes in ({"acknowledged": False}, {"expected_preparation_revision": self.preparation.revision+1},
                {"request_sha256": "0"*64}, {"task_updated_at": (self.task.updated_at-timedelta(seconds=1)).isoformat()}):
            with self.subTest(changes=changes):
                response = self.post({**self.payload, **changes})
                self.assertIn(response.status_code, (400, 409), response.data)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_actual_receipt_active_cancelled_or_successful_task_cannot_be_retried(self):
        for changes in ({"status": "running"}, {"status": "queued"}, {"status": "cancel_requested"},
                {"submission_state": "terminal_succeeded"}, {"submission_state": "terminal_failed"},
                {"outputs": [{"filename": "retained.png"}]}, {"submission_token": "foreign"}):
            with self.subTest(changes=changes), transaction.atomic():
                GenerationTask.objects.filter(pk=self.task.pk).update(**changes)
                self.assertEqual(self.post().status_code, 409)
                transaction.set_rollback(True)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_late_observation_cannot_requeue_original_or_release_unknown_cost(self):
        self.assertEqual(self.post().status_code, 200)
        self.attempt.refresh_from_db(); original_cost = self.attempt.committed_cost
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No original polling")):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
        self.assertEqual(update_prepared_generation_lifecycle(self.task,
            {"status": "queued", "submission_state": "terminal_succeeded"},
            condition=GenerationTask.objects.filter(pk=self.task.pk), remote_present=True), 0)
        self.task.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        self.assertEqual(self.attempt.committed_cost, original_cost)

    def test_foreign_owner_cannot_retry(self):
        self.client.force_authenticate(get_user_model().objects.create_user(username="foreign-uncertain"))
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_cancelled_run_or_attempt_cannot_retry(self):
        for model, changes in ((type(self.run), {"status": "cancelled"}),
                (GenerationAttempt, {"ignore_late_result": True}), (GenerationAttempt, {"cancel_requested_at": timezone.now()})):
            with self.subTest(changes=changes), transaction.atomic():
                model.objects.filter(pk=self.run.pk if model is type(self.run) else self.attempt.pk).update(**changes)
                self.assertEqual(self.post().status_code, 409)
                transaction.set_rollback(True)

    def test_real_admission_does_not_exempt_other_uncertain_calls_or_resource_checks(self):
        from studio.services.execution_production_admission import collect_production_execution_admission
        from studio.services.json_contracts import json_contract
        request = json_contract(self.preparation.contract.prepared_request)
        def codes(**kwargs):
            return {item["code"] for item in collect_production_execution_admission(
                self.preparation, self.definition, request, **kwargs).hard_blockers}
        self.assertIn("execution_submission_unresolved", codes())
        authorized = codes(retry_of=self.attempt, acknowledged_uncertain_attempt_id=self.attempt.pk)
        self.assertNotIn("execution_submission_unresolved", authorized)
        self.assertTrue(authorized, "explicit retry does not certify this fixture's missing resources")
        self.assertEqual(self.post().status_code, 200)
        self.preparation.refresh_from_db()  # The formal retry persisted a new admission evaluation/revision.
        self.assertNotIn("execution_submission_unresolved", codes())
        new = GenerationAttempt.objects.get(retry_of=self.attempt)
        GenerationAttempt.objects.filter(pk=new.pk).update(status="failed", error_code="uncertain_remote_response")
        self.assertIn("execution_submission_unresolved", codes())

    def test_admission_rejection_rolls_back_new_attempt_and_running_transition(self):
        type(self.run).objects.filter(pk=self.run.pk).update(status="awaiting_input")
        type(self.stage).objects.filter(pk=self.stage.pk).update(status="blocked")
        response = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.run.refresh_from_db(); self.attempt.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_input")
        self.assertFalse(self.attempt.ignore_late_result)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertFalse(PlatformLifecycleAuditEvent.objects.filter(action="retry_uncertain_media").exists())
