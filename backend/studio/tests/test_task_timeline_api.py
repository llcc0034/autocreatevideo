from datetime import datetime, timedelta, timezone as datetime_timezone
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ConfigRevision,
    GenerationAttempt,
    GenerationTask,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_workflow_inspection import inspect_prepared_task_inputs
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_production_workflow_requests as fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class GenerationTaskTimelineAPITests(TestCase):
    _create_version = fixtures.ProductionWorkflowRequestTests._create_version
    resolve = fixtures.ProductionWorkflowRequestTests.resolve

    def setUp(self):
        # Allocate through the current Run / Preparation / Attempt dispatcher.
        # This API suite changes only persisted lifecycle facts, not the frozen
        # request contract. Controlled admission is not real GPU acceptance.
        fixtures.ProductionWorkflowRequestTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_task(self, **overrides):
        task = self.attempt.generation_task
        if overrides:
            GenerationTask.objects.filter(pk=task.pk).update(**overrides)
            task.refresh_from_db()
        return task

    def test_completed_task_exposes_persisted_times_durations_and_full_timeline(self):
        task = self.create_task()
        queued_at = datetime(2026, 8, 8, 2, 0, tzinfo=datetime_timezone.utc)
        started_at = queued_at + timedelta(seconds=5)
        prepared_at = queued_at + timedelta(seconds=7)
        submitted_at = queued_at + timedelta(seconds=8, milliseconds=250)
        finished_at = queued_at + timedelta(minutes=2, seconds=10, milliseconds=500)
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.SUCCEEDED,
            progress=100,
            attempts=1,
            queued_at=queued_at,
            started_at=started_at,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            submission_prepared_at=prepared_at,
            submission_submitted_at=submitted_at,
            submission_token="prompt-123",
            comfy_prompt_id="prompt-123",
            finished_at=finished_at,
        )

        response = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("created_at", response.data)
        self.assertEqual(response.data["queued_at"], "2026-08-08T10:00:00+08:00")
        self.assertEqual(response.data["started_at"], "2026-08-08T10:00:05+08:00")
        self.assertEqual(response.data["finished_at"], "2026-08-08T10:02:10.500000+08:00")
        self.assertEqual(response.data["completed_at"], response.data["finished_at"])
        self.assertEqual(response.data["queue_wait_ms"], 5_000)
        self.assertEqual(response.data["duration_ms"], 125_500)
        self.assertEqual(response.data["total_duration_ms"], 130_500)

        timeline = response.data["timeline"]
        self.assertEqual(
            [item["event"] for item in timeline],
            ["queued", "started", "workflow_prepared", "comfyui_submitted", "succeeded"],
        )
        self.assertEqual(timeline[1]["details"], {"attempt": 1})
        self.assertEqual(timeline[3]["details"], {
            "submission_state": GenerationTask.SubmissionState.SUBMITTED,
            "prompt_id": "prompt-123",
        })
        self.assertNotIn("occurred_at", timeline[-1])
        self.assertEqual(timeline[-1]["timestamp"], response.data["finished_at"])
        self.assertEqual(timeline[-1]["status"], GenerationTask.Status.SUCCEEDED)

    def test_queued_task_does_not_invent_unpersisted_events_or_durations(self):
        task = self.create_task(status=GenerationTask.Status.QUEUED)

        response = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["started_at"])
        self.assertIsNone(response.data["finished_at"])
        self.assertIsNone(response.data["completed_at"])
        self.assertIsNone(response.data["queue_wait_ms"])
        self.assertIsNone(response.data["duration_ms"])
        self.assertIsNone(response.data["total_duration_ms"])
        self.assertEqual([item["event"] for item in response.data["timeline"]], ["queued"])
        parameters = {row["key"]: row for row in response.data["input_parameters"]["parameters"]}
        self.assertEqual(parameters["audio.enabled"]["value"], True)
        self.assertEqual(parameters["audio.enabled"]["value_source"], "workflow.static_semantics")
        self.assertEqual(parameters["audio.enabled"]["rule"], {"const": True})

    def test_retry_task_timeline_is_linked_to_the_real_failed_source(self):
        source = self.create_task(
            status=GenerationTask.Status.FAILED,
            submission_state=GenerationTask.SubmissionState.TERMINAL_FAILED,
            error_message="verified remote failure",
        )
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(
            status="failed", committed_cost=0, actual_cost=0,
            error_code="execution_workflow_failed")
        self.preparation.refresh_from_db()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=lambda prep, definition, request, **_: production_observations(prep, definition, request)):
            successor = dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision,
                actor=self.user, retry_of_id=self.attempt.pk)
        retry = successor.generation_task

        response = self.client.get(f"/api/v1/tasks/{retry.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(successor.retry_of.generation_task_id, source.pk)
        self.assertEqual(response.data["execution"]["attempt_id"], str(successor.pk))
        self.assertEqual(response.data["execution"]["attempt_number"], 2)
        self.assertNotIn("retry_of", response.data)
        self.assertEqual(
            [item["event"] for item in response.data["timeline"]],
            ["queued"],
        )

    def test_input_projection_rejects_changed_static_value_or_source(self):
        detail, definition, request = read_frozen_execution_request(self.preparation)
        for field, replacement in (("values", False), ("sources", "request")):
            broken = deepcopy(request.value)
            broken["parameters"][field]["audio.enabled"] = replacement
            with self.subTest(field=field), patch(
                    "studio.services.execution_workflow_inspection.read_frozen_execution_request",
                    return_value=(detail, definition, json_contract(broken))), self.assertRaises(ValidationError):
                inspect_prepared_task_inputs(self.create_task())

    def test_retryable_attempt_finish_is_reported_as_requeued_not_terminal_failure(self):
        task = self.create_task(status=GenerationTask.Status.QUEUED, attempts=1)
        queued_at = datetime(2026, 8, 8, 3, 0, tzinfo=datetime_timezone.utc)
        started_at = queued_at + timedelta(seconds=2)
        finished_at = queued_at + timedelta(seconds=4)
        GenerationTask.objects.filter(pk=task.pk).update(
            queued_at=queued_at,
            started_at=started_at,
            finished_at=finished_at,
            error_message="pre-submission failure",
        )

        response = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["duration_ms"], 2_000)
        self.assertEqual(response.data["timeline"][-1]["event"], "attempt_failed_requeued")
        self.assertEqual(response.data["timeline"][-1]["status"], GenerationTask.Status.QUEUED)

    def test_runtime_capabilities_expose_heartbeat_stall_cancel_and_reconciliation(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"runtime.task_stall_threshold_seconds": 1},
            created_by=None,
            change_summary="task timeline stall threshold test",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=None)
        task = self.create_task(
            status=GenerationTask.Status.RUNNING,
            submission_token="active-token",
            comfy_prompt_id="active-token",
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        old = timezone.now() - timedelta(seconds=10)
        GenerationTask.objects.filter(pk=task.pk).update(
            heartbeat_at=old,
            remote_state=GenerationTask.RemoteState.RUNNING,
        )

        active = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(active.status_code, 200, active.data)
        self.assertTrue(active.data["stalled"])
        # Actions belong to the original ProductionRun, not a second task API.
        self.assertEqual(active.data["execution"]["production_run_id"], str(self.run.pk))
        self.assertNotIn("can_cancel", active.data)
        self.assertNotIn("can_retry", active.data)
        self.assertFalse(active.data["retry_requires_reconciliation"])
        self.assertIsNotNone(active.data["heartbeat_at"])

        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            remote_state=GenerationTask.RemoteState.CANCELLING,
        )
        cancelling = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(cancelling.status_code, 200, cancelling.data)
        self.assertEqual(cancelling.data["status"], "cancel_requested")

        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            finished_at=timezone.now(),
            stalled_at=timezone.now(),
            remote_state=GenerationTask.RemoteState.DEGRADED,
        )
        failed = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(failed.status_code, 200, failed.data)
        self.assertEqual(failed.data["status"], "failed")
        self.assertTrue(failed.data["retry_requires_reconciliation"])
        self.assertIn("stalled", [item["event"] for item in failed.data["timeline"]])
