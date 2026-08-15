from pathlib import Path
import tempfile
import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from studio.models import (
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    Shot,
    WorkerLease,
    WorkflowTemplate,
)
from studio.services.comfyui import (
    ComfyUIClient,
    ComfyUIError,
    ComfyUIExecutionError,
    ProgressSocketEvent,
    QueueResult,
)
from studio.services.h3_prompts import (
    H3PromptOptions,
    build_consistent_image_prompt,
    build_h3_video_prompt,
)
from studio.serializers import GenerationTaskSerializer
from studio.services.task_queue import (
    _authorized_asset_source,
    claim_next_task,
    process_task,
    recover_stale_tasks,
)
from studio.services.workflow import build_workflow
from studio.tests.factories import build_story
from studio.views import ProductionQueueConfirmationRequired, _assert_queue_capacity


class FakeClient:
    def object_info(self):
        return {
            "SaveImage": {
                "input": {"required": {}},
                "output": [],
            },
        }

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        return QueueResult(prompt_id or "prompt-1", 1, {})

    def wait_for_completion(self, prompt_id, timeout=900):
        return {"outputs": {"9": {"videos": [{"filename": "episode.mp4", "subfolder": "show"}]}}}

    def extract_outputs(self, history):
        path = Path(settings.COMFYUI_OUTPUT_DIR) / "show" / "episode.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-generated-video")
        return [{"node_id": "9", "kind": "videos", "filename": "episode.mp4", "subfolder": "show"}]


class ExistingHistoryClient(FakeClient):
    queued = False

    def history(self, prompt_id):
        return {"outputs": {"9": {"videos": [{"filename": "existing.mp4"}]}}}

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queued = True
        return super().queue_prompt(workflow, client_id, prompt_id)


class SDXLImageClient(FakeClient):
    def __init__(self):
        self.submitted_workflow = None

    def object_info(self):
        # Local import avoids exposing another TestCase class to this module's
        # unittest discovery while reusing the exact reviewed live-shape fixture.
        from studio.tests.test_comfyui_contract import ComfyUIContractTests
        return ComfyUIContractTests.sdxl_object_info()

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.submitted_workflow = workflow
        return QueueResult(prompt_id or "image-prompt-1", 1, {})

    def wait_for_completion(self, prompt_id, timeout=900):
        path = (
            Path(settings.COMFYUI_OUTPUT_DIR)
            / "autoprompt/image-test"
            / "shot-01_00001_.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-generated-image")
        return {"outputs": {"7": {"images": [{
            "filename": "shot-01_00001_.png",
            "subfolder": "autoprompt/image-test",
            "type": "output",
        }]}}}

    def extract_outputs(self, history):
        return ComfyUIClient.extract_outputs(history)


class MissingPreparedClient(FakeClient):
    queued = False

    def history(self, prompt_id):
        return None

    def is_queued(self, prompt_id):
        return False

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queued = True
        raise AssertionError("ambiguous persisted submission must never be posted again")


class ContractMismatchClient(FakeClient):
    def __init__(self):
        self.queue_calls = 0

    def object_info(self):
        return {
            "LoadImage": {
                "input": {"required": {"image": [["present.png"], {"image_upload": True}]}},
                "output": ["IMAGE", "MASK"],
            },
        }

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queue_calls += 1
        raise AssertionError("a contract mismatch must never reach POST /prompt")


class ContractUnavailableClient(FakeClient):
    def __init__(self):
        self.queue_calls = 0

    def object_info(self):
        raise ComfyUIError("connection refused")

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queue_calls += 1
        raise AssertionError("an unavailable contract must never reach POST /prompt")


class TerminalFailureClient(FakeClient):
    def wait_for_completion(self, prompt_id, timeout=900):
        raise ComfyUIExecutionError(
            "ComfyUI execution failed: CUDA out of memory",
            remote_status={"status_str": "error", "completed": False},
        )


class StickyHealthFailureClient(FakeClient):
    def __init__(self):
        self.cancel_calls = 0

    def history(self, prompt_id):
        return None

    def is_queued(self, prompt_id):
        return True

    def health(self):
        raise ComfyUIError("/system_stats: CUDA sticky out of memory")

    def cancel(self, prompt_id):
        self.cancel_calls += 1

    def wait_for_completion(self, prompt_id, timeout=900):
        raise AssertionError("durable clients must use heartbeat polling")


class TransientPollFailureClient(FakeClient):
    def __init__(self, task_id):
        self.task_id = task_id
        self.history_calls = 0
        self.queue_calls = 0
        self.queued = False

    def history(self, prompt_id):
        self.history_calls += 1
        if self.history_calls == 1:
            raise ComfyUIError("GET /history read timed out")
        if self.history_calls == 2:
            return None
        return {"outputs": {"9": {"videos": [{
            "filename": "episode.mp4", "subfolder": "show",
        }]}}}

    def is_queued(self, prompt_id):
        self.queue_calls += 1
        if self.queue_calls == 1:
            raise ComfyUIError("GET /queue read timed out")
        return True

    def health(self):
        return {"ok": True}

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queued = True
        raise AssertionError("a persisted prompt must never be submitted again")


class PollAndHealthFailureClient(FakeClient):
    def __init__(self):
        self.health_calls = 0
        self.cancel_calls = 0
        self.queued = False

    def history(self, prompt_id):
        raise ComfyUIError("GET /history read timed out")

    def is_queued(self, prompt_id):
        raise ComfyUIError("GET /queue read timed out")

    def health(self):
        self.health_calls += 1
        raise ComfyUIError("GET /system_stats read timed out")

    def cancel(self, prompt_id):
        self.cancel_calls += 1

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queued = True
        raise AssertionError("a persisted prompt must never be submitted again")


class CancellationDuringPollClient(FakeClient):
    def __init__(self, task_id):
        self.task_id = task_id
        self.requested = False
        self.cancelled = False

    def history(self, prompt_id):
        if not self.requested:
            GenerationTask.objects.filter(pk=self.task_id).update(
                status=GenerationTask.Status.CANCEL_REQUESTED,
            )
            self.requested = True
        return None

    def is_queued(self, prompt_id):
        return not self.cancelled

    def health(self):
        return {"ok": True}

    def cancel(self, prompt_id):
        self.cancelled = True


class _ProgressSocket:
    closed = False

    def close(self):
        self.closed = True


class ProgressReportingClient(FakeClient):
    def __init__(self, task_id):
        self.task_id = task_id
        self.socket = _ProgressSocket()
        self.history_calls = 0
        self.progress_reads = 0
        self.observed_progress = 0
        self.observed_source = ""
        self.observed_comfy_progress = None
        self.observed_node_id = ""
        self.observed_telemetry_state = ""

    def open_progress_socket(self, client_id, timeout=0.1):
        return self.socket

    def read_progress_event(self, progress_socket, prompt_id, timeout=0.05):
        self.progress_reads += 1
        if self.progress_reads == 1:
            return ProgressSocketEvent(
                telemetry_state="receiving",
                event_type="progress_state",
                prompt_id=prompt_id,
                node_id="44",
                fraction=0.5,
            )
        persisted = GenerationTask.objects.get(pk=self.task_id)
        self.observed_progress = persisted.progress
        self.observed_source = persisted.progress_source
        self.observed_comfy_progress = persisted.comfy_progress
        self.observed_node_id = persisted.progress_node_id
        self.observed_telemetry_state = persisted.progress_telemetry_state
        return None

    def history(self, prompt_id):
        self.history_calls += 1
        if self.history_calls < 2:
            return None
        return {"outputs": {"9": {"videos": [{
            "filename": "episode.mp4", "subfolder": "show",
        }]}}}

    def is_queued(self, prompt_id):
        return self.history_calls < 2

    def health(self):
        return {"ok": True}


class ProgressReadFailureClient(ProgressReportingClient):
    def read_progress_event(self, progress_socket, prompt_id, timeout=0.05):
        self.progress_reads += 1
        if self.progress_reads == 1:
            raise RuntimeError("socket reset")
        return None


class ProgressOpenFailureClient(ProgressReportingClient):
    def open_progress_socket(self, client_id, timeout=0.1):
        raise RuntimeError("handshake refused")


class PersistedRemoteClient:
    def __init__(self, *, present=True, history=None):
        self.present = present
        self.remote_history = history
        self.queued = False
        self.cancel_calls = 0

    def history(self, prompt_id):
        return self.remote_history

    def is_queued(self, prompt_id):
        return self.present

    def queue_prompt(self, *args, **kwargs):
        self.queued = True
        raise AssertionError("stale submitted tasks must never be posted again")

    def cancel(self, prompt_id):
        self.cancel_calls += 1


class UnavailablePersistedRemoteClient(PersistedRemoteClient):
    def history(self, prompt_id):
        raise ComfyUIError("GET /history read timed out")

    def is_queued(self, prompt_id):
        raise AssertionError("queue lookup must not run after history lookup fails")


class ConcurrentRetryClient(PersistedRemoteClient):
    def __init__(self, task_id, *, fail_lookup=False):
        super().__init__(present=True)
        self.task_id = task_id
        self.fail_lookup = fail_lookup
        self.retry_id = None

    def _terminalize_and_create_retry(self):
        source = GenerationTask.objects.get(pk=self.task_id)
        GenerationTask.objects.filter(pk=source.pk).update(
            submission_state=GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            remote_state=GenerationTask.RemoteState.MISSING,
            remote_error="Remote prompt absent after successful ComfyUI reconciliation",
        )
        retry = GenerationTask.objects.create(
            owner=source.owner,
            project=source.project,
            episode=source.episode,
            generated_workflow=source.generated_workflow,
            kind=source.kind,
            capability_snapshot={
                **(source.capability_snapshot or {}),
                "retry_of": str(source.id),
                "batch_role": "retry",
            },
        )
        self.retry_id = retry.id

    def history(self, prompt_id):
        if self.fail_lookup:
            self._terminalize_and_create_retry()
            raise ComfyUIError("GET /history read timed out")
        return None

    def is_queued(self, prompt_id):
        self._terminalize_and_create_retry()
        return True


class TaskQueueTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.override = override_settings(
            COMFYUI_OUTPUT_DIR=str(root / "comfy-output"),
            MEDIA_ROOT=root / "media",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        Path(settings.COMFYUI_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    def _task(self):
        user, project, _, _, episode, shot1, _ = build_story()
        result = build_h3_video_prompt(project, [shot1], H3PromptOptions())
        prompt = PromptArtifact.objects.create(
            project=project, episode=episode, shot=shot1, created_by=user,
            prompt_type="video", prompt_text=result.text, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.create(
            name="mock", slug="mock", output_kind="video", is_system=True,
            definition={"1": {"class_type": "SaveImage", "inputs": {}}},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project, episode=episode, prompt_artifact=prompt, template=template,
            created_by=user, workflow_json=template.definition, content_hash="a" * 64,
        )
        return GenerationTask.objects.create(
            owner=user, project=project, episode=episode, generated_workflow=workflow, kind="video",
        )

    def _image_task(self):
        call_command("seed_data", verbosity=0)
        user, project, _, _, episode, shot, _ = build_story(username="image-queue")
        shot.media_type = Shot.MediaType.IMAGE
        shot.save(update_fields=("media_type", "updated_at"))
        result = build_consistent_image_prompt(project, shot)
        prompt = PromptArtifact.objects.create(
            project=project, episode=episode, shot=shot, created_by=user,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            model=result.payload["model"], prompt_text=result.text,
            negative_prompt="blurry, duplicate person, watermark",
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-reference-image",
        )
        workflow_json, injected, content_hash = build_workflow(
            template, prompt, project, {
                "RESOLUTION_PROFILE": "0.98",
                "RESOLUTION_MULTIPLE": 32,
                "WIDTH": 768,
                "HEIGHT": 1344,
            },
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project, episode=episode, prompt_artifact=prompt,
            template=template, created_by=user, workflow_json=workflow_json,
            injected_values=injected, content_hash=content_hash,
        )
        return GenerationTask.objects.create(
            owner=user, project=project, episode=episode,
            generated_workflow=workflow, kind=GenerationTask.Kind.IMAGE,
        )

    def test_claim_and_complete(self):
        self._task()
        task = claim_next_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.status, GenerationTask.Status.RUNNING)
        result_task = process_task(task, FakeClient())
        self.assertEqual(result_task.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(result_task.progress, 100)
        self.assertEqual(result_task.outputs[0]["filename"], "episode.mp4")
        self.assertEqual(result_task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(result_task.comfy_prompt_id, result_task.submission_token)
        preflight = result_task.capability_snapshot["comfyui_contract_preflight"]
        self.assertEqual(preflight["status"], "passed")
        self.assertEqual(preflight["node_count"], 1)
        self.assertEqual(preflight["class_types"], ["SaveImage"])
        self.assertEqual(len(preflight["schema_sha256"]), 64)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_PROGRESS_WEBSOCKET_ENABLED=True,
    )
    def test_websocket_sampler_progress_is_persisted_before_completion(self):
        pending = self._task()
        task = claim_next_task()
        client = ProgressReportingClient(pending.id)

        result = process_task(task, client, wait_timeout=5)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertGreaterEqual(client.observed_progress, 52)
        self.assertLessEqual(client.observed_progress, 95)
        self.assertEqual(client.observed_source, GenerationTask.ProgressSource.COMFYUI)
        self.assertEqual(client.observed_comfy_progress, 50)
        self.assertEqual(client.observed_node_id, "44")
        self.assertEqual(
            client.observed_telemetry_state,
            GenerationTask.ProgressTelemetryState.RECEIVING,
        )
        self.assertEqual(result.progress, 100)
        self.assertEqual(result.progress_source, GenerationTask.ProgressSource.TERMINAL)
        self.assertEqual(result.comfy_progress, 50)
        self.assertTrue(client.socket.closed)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_PROGRESS_WEBSOCKET_ENABLED=True,
    )
    def test_websocket_read_failure_is_persisted_without_failing_task(self):
        pending = self._task()
        task = claim_next_task()
        client = ProgressReadFailureClient(pending.id)

        result = process_task(task, client, wait_timeout=5)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertEqual(
            result.progress_telemetry_state,
            GenerationTask.ProgressTelemetryState.UNAVAILABLE,
        )
        self.assertIn("socket reset", result.progress_telemetry_error)
        self.assertTrue(client.socket.closed)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_PROGRESS_WEBSOCKET_ENABLED=True,
    )
    def test_websocket_open_failure_is_persisted_without_failing_task(self):
        pending = self._task()
        task = claim_next_task()
        client = ProgressOpenFailureClient(pending.id)

        result = process_task(task, client, wait_timeout=5)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(
            result.progress_telemetry_state,
            GenerationTask.ProgressTelemetryState.UNAVAILABLE,
        )
        self.assertIn("handshake refused", result.progress_telemetry_error)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_PROGRESS_WEBSOCKET_ENABLED=False,
    )
    def test_transient_history_and_queue_timeouts_keep_tracking_persisted_prompt(self):
        task = self._task()
        task.status = GenerationTask.Status.RUNNING
        task.attempts = 1
        task.submission_token = "fbac10a6-3e1b-4ce2-8f9d-e9531f962c06"
        task.comfy_prompt_id = task.submission_token
        task.submission_state = GenerationTask.SubmissionState.SUBMITTED
        task.save(update_fields=(
            "status", "attempts", "submission_token", "comfy_prompt_id",
            "submission_state", "updated_at",
        ))
        client = TransientPollFailureClient(task.id)

        result = process_task(task, client, wait_timeout=5)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertEqual(result.submission_token, task.submission_token)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.SUCCEEDED)
        self.assertGreaterEqual(client.history_calls, 3)
        self.assertGreaterEqual(client.queue_calls, 1)
        self.assertFalse(client.queued)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_HEALTH_FAILURE_LIMIT=2,
        TASK_PROGRESS_WEBSOCKET_ENABLED=False,
    )
    def test_poll_timeouts_still_run_bounded_health_failure_detection(self):
        task = self._task()
        token = "b6eb2d85-da37-4542-ad45-6a5f63cf12eb"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.RUNNING,
            attempts=1,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        task.refresh_from_db()
        client = PollAndHealthFailureClient()

        result = process_task(task, client, wait_timeout=5)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.submission_token, token)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.RECOVERED)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertIn("health check failed 2 consecutive times", result.error_message)
        self.assertEqual(client.health_calls, 2)
        self.assertEqual(client.cancel_calls, 1)
        self.assertFalse(client.queued)

    def test_sdxl_image_task_preflights_submits_and_persists_png_output(self):
        self._image_task()
        task = claim_next_task()
        client = SDXLImageClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(result.kind, GenerationTask.Kind.IMAGE)
        self.assertEqual(result.outputs[0]["node_id"], "7")
        self.assertEqual(result.outputs[0]["kind"], "images")
        self.assertEqual(result.outputs[0]["filename"], "shot-01_00001_.png")
        self.assertEqual(result.outputs[0]["subfolder"], "autoprompt/image-test")
        self.assertTrue(result.outputs[0]["provenance"]["ai_generated"])
        self.assertEqual(len(result.outputs[0]["provenance"]["artifact_sha256"]), 64)
        self.assertIsNotNone(client.submitted_workflow)
        self.assertEqual(
            client.submitted_workflow["1"]["inputs"]["ckpt_name"],
            "sd_xl_base_1.0.safetensors",
        )
        self.assertNotIn("LoadImage", {
            node["class_type"] for node in client.submitted_workflow.values()
        })
        preflight = result.capability_snapshot["comfyui_contract_preflight"]
        self.assertEqual(preflight["status"], "passed")
        self.assertEqual(preflight["node_count"], 7)

    def test_live_contract_mismatch_blocks_before_token_and_prompt_post(self):
        self._task()
        task = claim_next_task()
        client = ContractMismatchClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.UNPREPARED)
        self.assertEqual(result.submission_token, "")
        self.assertEqual(result.comfy_prompt_id, "")
        self.assertEqual(client.queue_calls, 0)
        self.assertIn("入队前契约预检失败", result.error_message)
        self.assertIn("GPU 未提交", result.error_message)
        self.assertTrue(GenerationTaskSerializer(result).data["can_retry"])

    def test_unreachable_object_info_requeues_without_token_or_prompt_post(self):
        self._task()
        task = claim_next_task()
        client = ContractUnavailableClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.QUEUED)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.UNPREPARED)
        self.assertEqual(result.submission_token, "")
        self.assertEqual(result.comfy_prompt_id, "")
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertEqual(client.queue_calls, 0)
        self.assertIn("/object_info", result.error_message)
        self.assertIn("可安全重试", result.error_message)

    def test_unreachable_object_info_exhaustion_fails_without_ambiguous_submission(self):
        pending = self._task()
        pending.max_attempts = 1
        pending.save(update_fields=("max_attempts", "updated_at"))
        task = claim_next_task()
        client = ContractUnavailableClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.UNPREPARED)
        self.assertEqual(result.submission_token, "")
        self.assertEqual(client.queue_calls, 0)
        self.assertTrue(GenerationTaskSerializer(result).data["can_retry"])

    def test_claim_mutex_enforces_single_running_task(self):
        first = self._task()
        GenerationTask.objects.create(
            owner=first.owner, project=first.project, episode=first.episode,
            generated_workflow=first.generated_workflow, kind="video",
        )
        claimed = claim_next_task()
        self.assertIsNotNone(claimed)
        self.assertTrue(WorkerLease.objects.filter(pk="generation-claim").exists())
        self.assertIsNone(claim_next_task())
        self.assertEqual(
            GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING).count(), 1,
        )

    @override_settings(
        TASK_RECOMMENDED_MAX_PENDING=100,
        TASK_RECOMMENDED_MAX_ACTIVE_PER_USER=1,
    )
    def test_per_user_queue_guidance_requires_confirmation_but_does_not_cap_volume(self):
        task = self._task()
        with self.assertRaises(ProductionQueueConfirmationRequired):
            with transaction.atomic():
                _assert_queue_capacity(task.owner)
        with transaction.atomic():
            snapshot = _assert_queue_capacity(
                task.owner,
                extended_generation_confirmed=True,
            )
        self.assertEqual(snapshot["recommended_max_active_per_user"], 1)

    def test_existing_comfy_prompt_history_prevents_duplicate_queue(self):
        task = self._task()
        task.status = GenerationTask.Status.RUNNING
        task.comfy_prompt_id = "already-submitted"
        task.attempts = 1
        task.save(update_fields=("status", "comfy_prompt_id", "attempts", "updated_at"))
        client = ExistingHistoryClient()
        result = process_task(task, client)
        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertFalse(client.queued)

    def test_prepared_submission_missing_remotely_fails_without_second_post(self):
        task = self._task()
        task.status = GenerationTask.Status.RUNNING
        task.attempts = 1
        task.submission_token = "8c72b638-0b55-4b01-96a1-51d9c7e77a1f"
        task.comfy_prompt_id = task.submission_token
        task.submission_state = GenerationTask.SubmissionState.PREPARED
        task.save(update_fields=(
            "status", "attempts", "submission_token", "comfy_prompt_id", "submission_state", "updated_at",
        ))
        client = MissingPreparedClient()
        result = process_task(task, client)
        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertIn("不会自动再次 POST", result.error_message)
        self.assertFalse(client.queued)

    def test_definitive_execution_failure_is_terminal_and_manually_retryable(self):
        self._task()
        task = claim_next_task()
        result = process_task(task, TerminalFailureClient())
        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            result.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
        )
        self.assertTrue(result.submission_token)
        self.assertIn("CUDA out of memory", result.error_message)

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_HEALTH_FAILURE_LIMIT=2,
    )
    def test_consecutive_health_failures_end_sticky_oom_without_waiting_for_timeout(self):
        self._task()
        task = claim_next_task()
        client = StickyHealthFailureClient()

        result = process_task(task, client, wait_timeout=60)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertIsNotNone(result.heartbeat_at)
        self.assertIsNotNone(result.stalled_at)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertIn("health check failed 2 consecutive times", result.error_message)
        self.assertEqual(client.cancel_calls, 1)
        self.assertFalse(GenerationTaskSerializer(result).data["can_retry"])

        GenerationTask.objects.create(
            owner=result.owner,
            project=result.project,
            episode=result.episode,
            generated_workflow=result.generated_workflow,
            kind=result.kind,
        )
        self.assertIsNone(claim_next_task())

    @override_settings(
        TASK_REMOTE_POLL_SECONDS=0.01,
        TASK_HEARTBEAT_SECONDS=0.01,
        TASK_HEALTH_CHECK_INTERVAL_SECONDS=0,
        TASK_HEALTH_FAILURE_LIMIT=3,
    )
    def test_database_cancel_is_observed_during_remote_polling(self):
        self._task()
        task = claim_next_task()
        client = CancellationDuringPollClient(task.id)

        result = process_task(task, client, wait_timeout=60)

        self.assertEqual(result.status, GenerationTask.Status.CANCELLED)
        self.assertEqual(
            result.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.CANCELLED)
        self.assertTrue(client.cancelled)
        self.assertIsNotNone(result.finished_at)

    def test_stale_submitted_task_reattaches_to_same_remote_prompt_without_repost(self):
        task = self._task()
        old = timezone.now() - timedelta(hours=1)
        token = "58fc558d-0307-47fe-8d87-c1f2d0d7a979"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.RUNNING,
            attempts=1,
            started_at=old,
            heartbeat_at=old,
            updated_at=old,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        client = PersistedRemoteClient(present=True)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.QUEUED)
        self.assertEqual(task.submission_token, token)
        self.assertEqual(task.attempts, 0)
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.RECOVERED)
        self.assertFalse(client.queued)

    def test_failed_submitted_task_reattaches_to_same_remote_prompt_without_repost(self):
        task = self._task()
        token = "18e7ee9e-100c-4b6c-bb4a-ed639ee40c67"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            finished_at=timezone.now(),
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.FAILED,
            error_message="GET /history read timed out",
        )
        client = PersistedRemoteClient(present=True)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.QUEUED)
        self.assertEqual(task.submission_token, token)
        self.assertEqual(task.attempts, 0)
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.RECOVERED)
        self.assertFalse(client.queued)

    def test_failed_missing_submission_is_terminalized_and_unblocks_fresh_task(self):
        task = self._task()
        token = "3eaad3c3-7e11-4b30-aef8-af03e8a96999"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            finished_at=timezone.now(),
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.DEGRADED,
            error_message="GET /history read timed out",
        )
        fresh_task = GenerationTask.objects.create(
            owner=task.owner,
            project=task.project,
            episode=task.episode,
            generated_workflow=task.generated_workflow,
            kind=task.kind,
        )
        client = PersistedRemoteClient(present=False)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)
        claimed = claim_next_task()

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.MISSING)
        self.assertIn("retried safely", task.error_message)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, fresh_task.id)
        self.assertFalse(client.queued)

    def test_failed_submission_stays_blocked_when_remote_reconciliation_errors(self):
        task = self._task()
        token = "08b88ea4-443f-4ebc-b573-90c082ef96ae"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            finished_at=timezone.now(),
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.FAILED,
            error_message="worker polling stopped",
        )
        fresh_task = GenerationTask.objects.create(
            owner=task.owner,
            project=task.project,
            episode=task.episode,
            generated_workflow=task.generated_workflow,
            kind=task.kind,
        )
        client = UnavailablePersistedRemoteClient()

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        fresh_task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertIn("read timed out", task.remote_error)
        self.assertIn("automatic resubmission is blocked", task.error_message)
        self.assertEqual(fresh_task.status, GenerationTask.Status.QUEUED)
        self.assertIsNone(claim_next_task())
        self.assertFalse(client.queued)

    def test_concurrent_retry_terminalization_prevents_source_reattachment(self):
        task = self._task()
        token = "0da1a9bc-e21c-41d5-85f8-b3401f0653d6"
        finished_at = timezone.now()
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            finished_at=finished_at,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.DEGRADED,
        )
        client = ConcurrentRetryClient(task.id)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        retry = GenerationTask.objects.get(pk=client.retry_id)
        self.assertEqual(recovered, 0)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(task.finished_at, finished_at)
        self.assertEqual(retry.status, GenerationTask.Status.QUEUED)
        self.assertEqual(retry.capability_snapshot["retry_of"], str(task.id))
        self.assertFalse(client.queued)

    def test_concurrent_retry_terminalization_prevents_error_overwrite(self):
        task = self._task()
        token = "75ecb57d-a35a-4218-9051-188df9ea0d11"
        finished_at = timezone.now()
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            finished_at=finished_at,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.DEGRADED,
        )
        client = ConcurrentRetryClient(task.id, fail_lookup=True)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        retry = GenerationTask.objects.get(pk=client.retry_id)
        self.assertEqual(recovered, 0)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.MISSING)
        self.assertEqual(task.finished_at, finished_at)
        self.assertEqual(retry.status, GenerationTask.Status.QUEUED)
        self.assertEqual(retry.capability_snapshot["retry_of"], str(task.id))
        self.assertFalse(client.queued)

    def test_max_attempt_submitted_reattachment_preempts_higher_priority_fresh_task(self):
        task = self._task()
        token = "3b79c6b7-fb7a-4e2a-b2d2-d21dd8497657"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.FAILED,
            attempts=1,
            max_attempts=1,
            finished_at=timezone.now(),
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            remote_state=GenerationTask.RemoteState.FAILED,
            error_message="GET /history read timed out",
        )
        fresh_task = GenerationTask.objects.create(
            owner=task.owner,
            project=task.project,
            episode=task.episode,
            generated_workflow=task.generated_workflow,
            kind=task.kind,
            priority=100,
        )
        client = PersistedRemoteClient(present=True)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)
        claimed = claim_next_task()
        completion_client = ExistingHistoryClient()
        result = process_task(claimed, completion_client)

        self.assertEqual(recovered, 1)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task.id)
        self.assertEqual(claimed.submission_token, token)
        self.assertEqual(claimed.attempts, claimed.max_attempts)
        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(result.submission_token, token)
        self.assertEqual(result.attempts, result.max_attempts)
        fresh_task.refresh_from_db()
        self.assertEqual(fresh_task.status, GenerationTask.Status.QUEUED)
        self.assertFalse(client.queued)
        self.assertFalse(completion_client.queued)

    def test_stale_submitted_task_missing_remotely_fails_without_repost(self):
        task = self._task()
        old = timezone.now() - timedelta(hours=1)
        token = "39e87f58-0492-4e20-946f-17f8748c3a21"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.RUNNING,
            attempts=1,
            started_at=old,
            heartbeat_at=old,
            updated_at=old,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        client = PersistedRemoteClient(present=False)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertIsNotNone(task.stalled_at)
        self.assertIn("automatic resubmission is blocked", task.error_message)
        self.assertFalse(client.queued)

    @override_settings(TASK_CANCEL_GRACE_SECONDS=1)
    def test_cancel_requested_reconciles_after_cancel_grace_without_waiting_for_stale_threshold(self):
        task = self._task()
        requested_at = timezone.now() - timedelta(seconds=2)
        token = "5f0ab055-d52a-4f19-8eea-b7b57a6da712"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
            updated_at=requested_at,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        client = PersistedRemoteClient(
            present=False,
            history={
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [["execution_interrupted", {"prompt_id": token}]],
                },
            },
        )

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(task.status, GenerationTask.Status.CANCELLED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.CANCELLED)
        self.assertIsNotNone(task.finished_at)

    @override_settings(TASK_CANCEL_GRACE_SECONDS=1)
    def test_cancel_requested_stays_unresolved_while_remote_prompt_is_still_queued(self):
        task = self._task()
        requested_at = timezone.now() - timedelta(seconds=2)
        token = "3d26f312-0b98-4b3c-ab75-471838208818"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
            updated_at=requested_at,
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        client = PersistedRemoteClient(present=True, history=None)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 0)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(task.status, GenerationTask.Status.CANCEL_REQUESTED)
        self.assertEqual(task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.CANCELLING)
        self.assertIsNone(task.finished_at)

    @override_settings(TASK_CANCEL_GRACE_SECONDS=30)
    def test_recent_cancel_request_is_not_reconciled_before_grace_period(self):
        task = self._task()
        token = "e3a41ec3-9bd2-43e4-b81f-bda4b69035b4"
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
            updated_at=timezone.now(),
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        client = PersistedRemoteClient(present=False, history=None)

        recovered = recover_stale_tasks(older_than_minutes=30, client=client)

        task.refresh_from_db()
        self.assertEqual(recovered, 0)
        self.assertEqual(client.cancel_calls, 0)
        self.assertEqual(task.status, GenerationTask.Status.CANCEL_REQUESTED)

    def test_staging_revalidates_asset_tenant_ownership(self):
        task = self._task()
        other = get_user_model().objects.create_user(username="asset-attacker", password="StrongPass123!")
        asset = Asset.objects.create(
            owner=other, kind=Asset.Kind.REFERENCE, name="其他租户素材", local_path="C:/forged.png",
        )
        with self.assertRaisesRegex(ComfyUIError, "不属于当前任务租户"):
            _authorized_asset_source(task, str(asset.id))

    def test_staging_accepts_only_owner_scoped_uploaded_media(self):
        with tempfile.TemporaryDirectory() as temp:
            media_root = Path(temp) / "media"
            asset_root = Path(temp) / "library"
            media_root.mkdir()
            asset_root.mkdir()
            with override_settings(MEDIA_ROOT=media_root, ASSET_LIBRARY_ROOT=asset_root):
                task = self._task()
                asset = Asset.objects.create(
                    owner=task.owner, kind=Asset.Kind.REFERENCE, name="上传素材",
                    file=SimpleUploadedFile("reference.png", b"safe-local-test"),
                    checksum=hashlib.sha256(b"safe-local-test").hexdigest(),
                )
                _, path = _authorized_asset_source(task, str(asset.id))
                self.assertTrue(path.is_relative_to(media_root / "assets" / str(task.owner_id)))
                global_in_media = media_root / "global.png"
                global_in_media.write_bytes(b"not-global-library")
                global_asset = Asset.objects.create(
                    owner=None, kind=Asset.Kind.REFERENCE, name="伪全局素材",
                    local_path=str(global_in_media),
                )
                with self.assertRaisesRegex(ComfyUIError, "越出租户授权目录"):
                    _authorized_asset_source(task, str(global_asset.id))
