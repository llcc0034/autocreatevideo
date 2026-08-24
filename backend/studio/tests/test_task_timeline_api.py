from datetime import datetime, timedelta, timezone as datetime_timezone

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ConfigRevision,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.tests.factories import build_story


class GenerationTaskTimelineAPITests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            _,
            _,
            self.episode,
            self.shot,
            _,
        ) = build_story("task-timeline")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="task-timeline-template",
            slug="task-timeline-template",
            output_kind="video",
            definition={"1": {"class_type": "Mock", "inputs": {}}},
            is_system=True,
        )
        self.prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type="video",
            prompt_text="timeline fixture",
            structured_payload={},
            consistency_snapshot={},
            request_options={"shot_ids": [str(self.shot.id)]},
            content_hash="1" * 64,
        )
        self.workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=self.prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            content_hash="2" * 64,
        )
    def create_task(self, **overrides):
        values = {
            "owner": self.user,
            "project": self.project,
            "episode": self.episode,
            "generated_workflow": self.workflow,
            "kind": GenerationTask.Kind.VIDEO,
        }
        values.update(overrides)
        return GenerationTask.objects.create(**values)

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
        self.assertEqual(timeline[-1]["occurred_at"], response.data["finished_at"])
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

    def test_retry_task_timeline_is_linked_to_the_real_failed_source(self):
        source = self.create_task(
            status=GenerationTask.Status.FAILED,
            submission_state=GenerationTask.SubmissionState.TERMINAL_FAILED,
            error_message="verified remote failure",
        )
        retry = self.create_task(
            status=GenerationTask.Status.QUEUED,
            capability_snapshot={"retry_of": str(source.id)},
        )

        response = self.client.get(f"/api/v1/tasks/{retry.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["retry_of"], str(source.id))
        self.assertEqual(
            [item["event"] for item in response.data["timeline"]],
            ["retry_created", "queued"],
        )
        self.assertEqual(
            response.data["timeline"][0]["details"],
            {"source_task_id": str(source.id)},
        )

    def test_retryable_attempt_finish_is_reported_as_requeued_not_terminal_failure(self):
        task = self.create_task(status=GenerationTask.Status.QUEUED, attempts=1, max_attempts=3)
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
        self.assertTrue(active.data["can_cancel"])
        self.assertFalse(active.data["can_retry"])
        self.assertFalse(active.data["retry_requires_reconciliation"])
        self.assertIsNotNone(active.data["heartbeat_at"])

        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            remote_state=GenerationTask.RemoteState.CANCELLING,
        )
        cancelling = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(cancelling.status_code, 200, cancelling.data)
        self.assertTrue(cancelling.data["can_cancel"])

        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            finished_at=timezone.now(),
            stalled_at=timezone.now(),
            remote_state=GenerationTask.RemoteState.DEGRADED,
        )
        failed = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertFalse(failed.data["can_cancel"])
        self.assertFalse(failed.data["can_retry"])
        self.assertTrue(failed.data["retry_requires_reconciliation"])
        self.assertIn("stalled", [item["event"] for item in failed.data["timeline"]])
