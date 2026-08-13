import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.tests.factories import build_story


@override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
class GenerationTaskRetryAPITests(TestCase):
    def setUp(self):
        user, project, _, _, episode, shot, _ = build_story()
        result = build_h3_video_prompt(project, [shot], H3PromptOptions())
        prompt = PromptArtifact.objects.create(
            project=project,
            episode=episode,
            shot=shot,
            created_by=user,
            prompt_type="video",
            prompt_text=result.text,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.create(
            name="retry-system-template",
            slug="retry-system-template",
            output_kind="video",
            is_system=True,
            definition={"1": {"class_type": "SaveImage", "inputs": {}}},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            episode=episode,
            prompt_artifact=prompt,
            template=template,
            created_by=user,
            workflow_json=template.definition,
            injected_values={
                "SEED": 42,
                "WIDTH": 416,
                "HEIGHT": 736,
                "FPS": 24,
                "FRAMES": 124,
                "EFFECTIVE_DURATION_MS": 5167,
                "RESOLUTION_PROFILE": "0.3",
                "RESOLUTION_MULTIPLE": 20,
            },
            content_hash="a" * 64,
        )
        self.user = user
        self.task = GenerationTask.objects.create(
            owner=user,
            project=project,
            episode=episode,
            generated_workflow=workflow,
            kind="video",
            status=GenerationTask.Status.FAILED,
            attempts=1,
            max_attempts=2,
            priority=7,
            backend="comfy_local_h3",
            model_profile="local_base_official",
            workflow_version=template.version,
            seed=42,
            effective_frames=124,
            effective_duration_ms=5167,
            capability_snapshot={
                "resolution_profile": "0.3",
                "resolution_multiple": 20,
                "width": 416,
                "height": 736,
                "fps": 24,
            },
            error_message="pre-submission failure",
        )
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_retry_clones_exact_workflow_and_preserves_failed_source(self):
        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

        self.task.refresh_from_db()
        retry = GenerationTask.objects.get(pk=response.data["id"])
        self.assertEqual(self.task.status, GenerationTask.Status.FAILED)
        self.assertEqual(self.task.attempts, 1)
        self.assertEqual(retry.status, GenerationTask.Status.QUEUED)
        self.assertEqual(retry.generated_workflow_id, self.task.generated_workflow_id)
        self.assertEqual(retry.priority, self.task.priority)
        self.assertEqual(retry.max_attempts, self.task.max_attempts)
        self.assertEqual(retry.seed, self.task.seed)
        self.assertEqual(retry.effective_frames, self.task.effective_frames)
        self.assertEqual(retry.effective_duration_ms, self.task.effective_duration_ms)
        self.assertEqual(retry.capability_snapshot["resolution_profile"], "0.3")
        self.assertEqual(retry.capability_snapshot["resolution_multiple"], 20)
        self.assertEqual(retry.capability_snapshot["retry_of"], str(self.task.id))
        self.assertEqual(retry.submission_token, "")
        self.assertEqual(retry.comfy_prompt_id, "")
        self.assertEqual(retry.outputs, [])

        duplicate = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertIn(str(retry.id), str(duplicate.data))

    def test_definitive_remote_failure_can_be_retried_with_new_token(self):
        self.task.submission_token = "old-terminal-token"
        self.task.comfy_prompt_id = "old-terminal-token"
        self.task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        self.task.save(update_fields=(
            "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        retry = GenerationTask.objects.get(pk=response.data["id"])
        self.assertEqual(retry.generated_workflow_id, self.task.generated_workflow_id)
        self.assertEqual(retry.submission_state, GenerationTask.SubmissionState.UNPREPARED)
        self.assertEqual(retry.submission_token, "")

    @patch("studio.views.ComfyUIClient")
    def test_uncertain_remote_submission_is_never_retried(self, client_factory):
        self.task.submission_token = "uncertain-token"
        self.task.comfy_prompt_id = "uncertain-token"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.save(update_fields=(
            "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        remote = client_factory.return_value
        remote.health.return_value = {"ok": True}
        remote.history.return_value = None
        remote.is_queued.return_value = True
        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("duplicate GPU work", str(response.data))
        self.assertEqual(GenerationTask.objects.count(), 1)

    @patch("studio.views.ComfyUIClient")
    def test_missing_prompt_after_healthy_comfyui_restart_can_be_retried(self, client_factory):
        self.task.submission_token = "stale-after-restart"
        self.task.comfy_prompt_id = "stale-after-restart"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.remote_state = GenerationTask.RemoteState.DEGRADED
        self.task.save(update_fields=(
            "submission_token", "comfy_prompt_id", "submission_state", "remote_state", "updated_at",
        ))
        remote = client_factory.return_value
        remote.health.return_value = {"ok": True}
        remote.history.return_value = None
        remote.is_queued.return_value = False

        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.task.refresh_from_db()
        self.assertEqual(
            self.task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        retry = GenerationTask.objects.get(pk=response.data["id"])
        self.assertEqual(retry.submission_state, GenerationTask.SubmissionState.UNPREPARED)
        self.assertEqual(retry.submission_token, "")

    @patch("studio.views.ComfyUIClient")
    def test_unhealthy_comfyui_keeps_uncertain_submission_blocked(self, client_factory):
        self.task.submission_token = "unhealthy-remote-token"
        self.task.comfy_prompt_id = "unhealthy-remote-token"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.save(update_fields=(
            "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        from studio.services.comfyui import ComfyUIError
        client_factory.return_value.health.side_effect = ComfyUIError("CUDA context unavailable")

        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(GenerationTask.objects.count(), 1)

    @patch("studio.views.ComfyUIClient")
    def test_repeated_cancel_stays_pending_while_remote_prompt_is_still_running(self, client_factory):
        self.task.status = GenerationTask.Status.RUNNING
        self.task.submission_token = "cancel-still-running"
        self.task.comfy_prompt_id = "cancel-still-running"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.save(update_fields=(
            "status", "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        remote = client_factory.return_value
        remote.history.return_value = None
        remote.is_queued.return_value = True

        first = self.client.post(f"/api/v1/tasks/{self.task.id}/cancel/", {}, format="json")
        second = self.client.post(f"/api/v1/tasks/{self.task.id}/cancel/", {}, format="json")

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(second.status_code, 202, second.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, GenerationTask.Status.CANCEL_REQUESTED)
        self.assertEqual(self.task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertIsNone(self.task.finished_at)

    @patch("studio.views.ComfyUIClient")
    def test_cancel_terminalizes_only_after_prompt_leaves_remote_queue(self, client_factory):
        self.task.status = GenerationTask.Status.RUNNING
        self.task.submission_token = "cancel-gone"
        self.task.comfy_prompt_id = "cancel-gone"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.save(update_fields=(
            "status", "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        remote = client_factory.return_value
        remote.history.return_value = None
        remote.is_queued.return_value = False

        response = self.client.post(f"/api/v1/tasks/{self.task.id}/cancel/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, GenerationTask.Status.CANCELLED)
        self.assertEqual(
            self.task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertIsNotNone(self.task.finished_at)

    def test_succeeded_task_is_never_retried(self):
        self.task.status = GenerationTask.Status.SUCCEEDED
        self.task.outputs = [{"filename": "finished.mp4"}]
        self.task.save(update_fields=("status", "outputs", "updated_at"))
        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(GenerationTask.objects.count(), 1)

    def test_legacy_terminal_failure_migration_is_strictly_scoped(self):
        self.task.submission_token = "legacy-oom-token"
        self.task.comfy_prompt_id = "legacy-oom-token"
        self.task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        self.task.error_message = "ComfyUI 执行失败: CUDA out of memory"
        self.task.save(update_fields=(
            "submission_token", "comfy_prompt_id", "submission_state", "error_message", "updated_at",
        ))

        def historical_task(*, token: str, error: str, status: str = GenerationTask.Status.FAILED):
            return GenerationTask.objects.create(
                owner=self.task.owner,
                project=self.task.project,
                episode=self.task.episode,
                generated_workflow=self.task.generated_workflow,
                kind=self.task.kind,
                status=status,
                submission_token=token,
                comfy_prompt_id=token,
                submission_state=GenerationTask.SubmissionState.SUBMITTED,
                error_message=error,
            )

        missing_token = historical_task(
            token="",
            error="ComfyUI 执行失败: CUDA out of memory",
        )
        timeout = historical_task(
            token="legacy-timeout-token",
            error="等待 ComfyUI 任务超时",
        )
        still_running = historical_task(
            token="legacy-running-token",
            error="ComfyUI 执行失败: stale text",
            status=GenerationTask.Status.RUNNING,
        )

        migration = importlib.import_module(
            "studio.migrations.0009_classify_legacy_terminal_failures"
        )
        migration.classify_legacy_terminal_failures(django_apps, None)

        self.task.refresh_from_db()
        missing_token.refresh_from_db()
        timeout.refresh_from_db()
        still_running.refresh_from_db()
        self.assertEqual(
            self.task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
        )
        self.assertEqual(missing_token.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(timeout.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(still_running.submission_state, GenerationTask.SubmissionState.SUBMITTED)
