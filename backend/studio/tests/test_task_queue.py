from pathlib import Path
import tempfile
import hashlib
import subprocess
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from PIL import Image

from studio.models import (
    Asset,
    ConfigRevision,
    GeneratedWorkflow,
    GenerationAttempt,
    GenerationTask,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PromptArtifact,
    Shot,
    StageRun,
    WorkerLease,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.comfyui import (
    ComfyUIClient,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUIHTTPError,
    ComfyUIPreSubmitResourceReleaseError,
    ProgressSocketEvent,
    QueueResult,
)
from studio.tests.media_prompt_fixtures import (
    MediaPromptOptions,
    build_test_image_prompt,
    build_test_video_prompt,
)
from studio.services.production_pipeline import (
    capture_queue_capacity_advisory,
)
from studio.services.provenance import verified_task_manifest
from studio.serializers import GenerationTaskSerializer
from studio.services.task_queue import (
    _authorized_asset_source,
    _task_execution_monitoring,
    _wait_for_task_completion,
    _mark_task_meaningful_progress,
    claim_next_task,
    prepare_workflow,
    process_task,
    recover_stale_tasks,
    task_wait_timeout,
)
from studio.services.execution_monitoring import (
    empty_execution_monitoring_contract,
    execution_monitoring_contract_sha256,
    resolve_execution_monitoring_contract,
)
from studio.services.media_capabilities import empty_conditioning_contract
from studio.services.json_contracts import assign_json_contract, canonical_json_bytes, json_contract
from studio.tests.factories import build_story, conditioning_contract


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

    def history(self, prompt_id):
        return {"outputs": {"9": {"videos": [{"filename": "episode.mp4", "subfolder": "show"}]}}}

    def is_queued(self, prompt_id):
        return False

    def extract_outputs(self, history):
        path = Path(settings.COMFYUI_OUTPUT_DIR) / "show" / "episode.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error",
                "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
            timeout=30,
        )
        return [{"node_id": "9", "kind": "videos", "filename": "episode.mp4", "subfolder": "show"}]


class ExistingHistoryClient(FakeClient):
    queued = False

    def history(self, prompt_id):
        return {"outputs": {"9": {"videos": [{"filename": "existing.mp4"}]}}}

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queued = True
        return super().queue_prompt(workflow, client_id, prompt_id)


class ResourceReleaseClient(FakeClient):
    def __init__(self):
        self.release_calls = 0
        self.queue_calls = 0

    def release_resources_before_submit(self):
        self.release_calls += 1
        return {
            "schema_version": 1,
            "status": "succeeded",
            "policy_sha256": "a" * 64,
            "unload_models": True,
            "free_memory": True,
            "attempted_at": "2026-08-27T01:00:00+00:00",
            "completed_at": "2026-08-27T01:00:01+00:00",
            "error_code": "",
            "http_status": 200,
        }

    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        self.queue_calls += 1
        return super().queue_prompt(workflow, client_id, prompt_id)


class FailingResourceReleaseClient(ResourceReleaseClient):
    def release_resources_before_submit(self):
        self.release_calls += 1
        audit = {
            "schema_version": 1,
            "status": "failed",
            "policy_sha256": "a" * 64,
            "unload_models": True,
            "free_memory": True,
            "attempted_at": "2026-08-27T01:00:00+00:00",
            "completed_at": "2026-08-27T01:00:01+00:00",
            "error_code": "comfyui_resource_release_unconfirmed",
            "http_status": None,
        }
        raise ComfyUIPreSubmitResourceReleaseError(
            audit["error_code"],
            "test release failure; /prompt was not called",
            audit=audit,
        )


class ExistingHistoryReleaseGuardClient(ExistingHistoryClient):
    release_calls = 0

    def release_resources_before_submit(self):
        self.release_calls += 1
        raise AssertionError("recovery must not release global resources")


class InvalidMediaClient(FakeClient):
    def extract_outputs(self, history):
        path = Path(settings.COMFYUI_OUTPUT_DIR) / "show" / "episode.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-video")
        return [{"node_id": "9", "kind": "videos", "filename": "episode.mp4", "subfolder": "show"}]


class InvalidExistingHistoryClient(ExistingHistoryClient, InvalidMediaClient):
    def extract_outputs(self, history):
        return InvalidMediaClient.extract_outputs(self, history)


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

    def history(self, prompt_id):
        path = (
            Path(settings.COMFYUI_OUTPUT_DIR)
            / "autoprompt/image-test"
            / "shot-01_00001_.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), color=(20, 40, 60)).save(path, format="PNG")
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
    def history(self, prompt_id):
        # The real history endpoint returns a terminal record; it does not
        # raise the transport exception used for unavailable history queries.
        return {"status": {"status_str": "error", "completed": False,
            "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]}}


class PromptHTTPRejectionClient(FakeClient):
    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        del workflow, client_id, prompt_id
        raise ComfyUIHTTPError(
            400,
            '{"error":"prompt_outputs_failed_validation","node_id":"44"}',
        )


class PromptTransportFailureClient(FakeClient):
    def queue_prompt(self, workflow, client_id=None, prompt_id=None):
        del workflow, client_id, prompt_id
        raise ComfyUIError("connection reset before /prompt response")


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


class RepeatedNodeProgressClient(ProgressReportingClient):
    def __init__(self, task_id):
        super().__init__(task_id)
        self.observed_idle_deadlines = []
        self.observed_hard_deadlines = []

    def read_progress_event(self, progress_socket, prompt_id, timeout=0.05):
        self.progress_reads += 1
        if self.progress_reads > 1:
            persisted = GenerationTask.objects.get(pk=self.task_id)
            self.observed_idle_deadlines.append(persisted.idle_deadline_at)
            self.observed_hard_deadlines.append(persisted.hard_deadline_at)
        if self.progress_reads == 1:
            return ProgressSocketEvent(
                telemetry_state="receiving",
                event_type="progress_state",
                prompt_id=prompt_id,
                node_id="44",
                fraction=0.2,
            )
        if self.progress_reads == 2:
            return ProgressSocketEvent(
                telemetry_state="connected",
                event_type="executing",
                prompt_id=prompt_id,
                node_id="44",
                fraction=None,
            )
        if self.progress_reads == 3:
            return ProgressSocketEvent(
                telemetry_state="receiving",
                event_type="progress_state",
                prompt_id=prompt_id,
                node_id="44",
                fraction=0.8,
            )
        return None

    def history(self, prompt_id):
        self.history_calls += 1
        if self.history_calls < 4:
            return None
        return {"outputs": {"9": {"videos": [{
            "filename": "episode.mp4", "subfolder": "show",
        }]}}}

    def is_queued(self, prompt_id):
        return self.history_calls < 4


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


class ConcurrentTerminalizationClient(PersistedRemoteClient):
    def __init__(self, task_id, *, fail_lookup=False):
        super().__init__(present=True)
        self.task_id = task_id
        self.fail_lookup = fail_lookup

    def _terminalize_source(self):
        GenerationTask.objects.filter(pk=self.task_id).update(
            submission_state=GenerationTask.SubmissionState.TERMINAL_FAILED,
            remote_state=GenerationTask.RemoteState.MISSING,
            remote_error="Remote prompt absent after successful ComfyUI reconciliation",
        )

    def history(self, prompt_id):
        if self.fail_lookup:
            self._terminalize_source()
            raise ComfyUIError("GET /history read timed out")
        return None

    def is_queued(self, prompt_id):
        self._terminalize_source()
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

    @staticmethod
    def _recover_stale_with_client(client, *, older_than_minutes=30):
        with patch(
            "studio.services.task_queue.comfy_client_for_task",
            return_value=client,
        ):
            return recover_stale_tasks(older_than_minutes=older_than_minutes)

    def _task(self):
        user, project, _, _, episode, shot1, _ = build_story()
        result = build_test_video_prompt(
            project, [shot1], MediaPromptOptions(
                model_identifier="minimax-h3-test", language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=True,
            project=project, episode=episode, shot=shot1, created_by=user,
            prompt_type="video", model="fictional-queue-video-v1",
            total_duration_ms=result.total_duration_ms,
            prompt_text=result.text, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
        )
        graph = {"1": {"class_type": "SaveImage", "inputs": {}}}
        template = WorkflowTemplate.objects.create(
            name="mock", slug="mock", output_kind="video", is_system=True,
            model="fictional-queue-video-v1", definition=graph,
        )
        graph_contract = json_contract(graph)
        input_contract = {
            "schema_version": 2,
            "compiler": {"id": "generic.comfy", "version": 1},
            "conditioning_contract": conditioning_contract("T2VA"),
        }
        input_contract_json = json_contract(input_contract)
        workflow_contract_sha256 = "b" * 64
        workflow_version = WorkflowTemplateVersion.objects.create(
            template=template,
            version=(
                max(
                    template.platform_versions.values_list("version", flat=True),
                    default=0,
                ) + 1
            ),
            graph=graph,
            graph_size_bytes=graph_contract.size_bytes,
            graph_sha256=graph_contract.sha256,
            input_contract=input_contract,
            input_contract_size_bytes=input_contract_json.size_bytes,
            input_contract_sha256=input_contract_json.sha256,
            compiler_id="generic.comfy",
            compiler_version=1,
            semantic_bindings_sha256=json_contract([]).sha256,
            model_requirements_sha256=json_contract([]).sha256,
            execution_contract_sha256=workflow_contract_sha256,
        )
        monitoring = empty_execution_monitoring_contract()
        mode_contract = input_contract["conditioning_contract"]["modes"][0]
        semantics = {
            "prompt.positive": result.text,
            "image.width": project.width,
            "image.height": project.height,
            "video.fps": project.fps,
            "video.duration_ms": shot1.duration_ms,
            "video.frames": max(1, round(shot1.duration_ms * project.fps / 1000)),
            "audio.enabled": prompt.include_audio,
            "sampling.seed": project.continuity_seed,
        }
        semantics_sha256 = json_contract(semantics).sha256
        empty_bindings_sha256 = json_contract([]).sha256
        empty_requirements_sha256 = json_contract([]).sha256
        route_snapshot_sha256 = "e" * 64
        execution_metadata = {
            "schema_version": 1,
            "workflow_version_id": str(workflow_version.id),
            "workflow_version": workflow_version.version,
            "template": {
                "id": str(template.id),
                "slug": template.slug,
                "name": template.name,
                "output_kind": template.output_kind,
            },
            "model_identifier": "fictional-queue-video-v1",
            "workflow_mode": "semantic",
            "source_workflow": "",
            "required_dimension_multiple": 1,
            "conditioning_contract": input_contract["conditioning_contract"],
            "conditioning_contract_sha256": json_contract(
                input_contract["conditioning_contract"],
            ).sha256,
            "conditioning_mode_contract": mode_contract,
            "conditioning_mode_contract_sha256": json_contract(mode_contract).sha256,
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                monitoring,
            ),
        }
        workflow = GeneratedWorkflow.objects.create(
            project=project, episode=episode, prompt_artifact=prompt, template=template,
            workflow_template_version=workflow_version,
            created_by=user, workflow_json=graph,
            injected_values={
                "schema_version": 2,
                "resolved_semantics": semantics,
                "asset_bindings": [],
                "model_requirements": [],
                "output_contract": {
                    "schema_version": 1,
                    "primary": {"node_id": "9", "kind": "video", "required": True},
                },
                "execution_metadata": execution_metadata,
                "execution_metadata_sha256": json_contract(execution_metadata).sha256,
            },
            content_hash=graph_contract.sha256,
            compiler_id="generic.comfy",
            compiler_version=1,
            workflow_contract_sha256=workflow_contract_sha256,
            route_snapshot_sha256=route_snapshot_sha256,
            resolved_semantics_sha256=semantics_sha256,
            asset_bindings_sha256=empty_bindings_sha256,
            model_requirements_sha256=empty_requirements_sha256,
        )
        task = GenerationTask.objects.create(
            owner=user, project=project, episode=episode,
            generated_workflow=workflow, kind="video",
            max_attempts=3,
            backend="comfyui_local", model_profile="fictional-queue-video-v1",
            effective_frames=semantics["video.frames"],
            effective_duration_ms=round(
                semantics["video.frames"] * 1000 / semantics["video.fps"],
            ),
            capability_snapshot={
                "workflow_hash": graph_contract.sha256,
                "generated_workflow_id": str(workflow.id),
                "prompt_artifact_id": str(prompt.id),
                "prompt_content_hash": prompt.content_hash,
                "template_id": str(template.id),
                "template_slug": template.slug,
                "model": execution_metadata["model_identifier"],
                "conditioning_mode": prompt.conditioning_mode,
                "effective_conditioning_mode": mode_contract["key"],
                "prompt_template_key": str(
                    prompt.structured_payload.get("prompt_template_key") or ""
                ),
                "workflow_mode": execution_metadata["workflow_mode"],
                "source_workflow": execution_metadata["source_workflow"],
                "workflow_parameters": semantics,
                "resolution_profile": "semantic_contract",
                "resolution_multiple": 1,
                "width": project.width,
                "height": project.height,
                "shot_ids": [str(shot1.id)],
                "shot_count": 1,
                "whole_episode": False,
                "workflow_contract_sha256": workflow_contract_sha256,
                "route_snapshot_sha256": route_snapshot_sha256,
                "asset_bindings": [],
                "asset_bindings_sha256": empty_bindings_sha256,
                "continuity_assets": [],
                "continuity_assets_sha256": empty_bindings_sha256,
                "resolved_semantics_sha256": semantics_sha256,
                "model_requirements": [],
                "model_requirements_sha256": empty_requirements_sha256,
                "fps": project.fps,
                "requested_duration_ms": shot1.duration_ms,
                "include_audio": prompt.include_audio,
                "output_contract": workflow.injected_values["output_contract"],
                "execution_metadata": execution_metadata,
                "execution_metadata_sha256": json_contract(execution_metadata).sha256,
                "conditioning_contract": input_contract["conditioning_contract"],
                "conditioning_contract_sha256": execution_metadata[
                    "conditioning_contract_sha256"
                ],
                "conditioning_mode_contract": mode_contract,
                "conditioning_mode_contract_sha256": execution_metadata[
                    "conditioning_mode_contract_sha256"
                ],
                "execution_monitoring": monitoring,
                "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                    monitoring,
                ),
            },
        )
        return self._freeze_capacity(task)

    def _publish_runtime(self, values: dict) -> None:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values=values,
            created_by=None,
            change_summary="task queue runtime test",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=None)

    def _image_task(self):
        call_command("seed_data", verbosity=0)
        user, project, _, _, episode, shot, _ = build_story(username="image-queue")
        shot.media_type = Shot.MediaType.IMAGE
        shot.save(update_fields=("media_type", "updated_at"))
        result = build_test_image_prompt(project, shot)
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=False,
            project=project, episode=episode, shot=shot, created_by=user,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            total_duration_ms=0,
            model=result.payload["model"], prompt_text=result.text,
            negative_prompt="blurry, duplicate person, watermark",
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-reference-image",
        )
        # Fresh installs intentionally bootstrap unreviewed workflows paused.
        # This worker contract test explicitly supplies its reviewed fixture.
        template.is_active = True
        template.model = "sd_xl_base_1.0.safetensors"
        template.input_schema = {
            **(template.input_schema or {}),
            "workflow_mode": "text_to_image",
            "fixed_model": True,
            "required_dimension_multiple": 8,
            "user_parameters": {
                "RESOLUTION_PROFILE": {
                    "type": "select", "default": "0.98", "options": ["0.98"],
                },
                "RESOLUTION_MULTIPLE": {
                    "type": "integer", "default": 32, "min": 8, "max": 128, "step": 4,
                },
                "WIDTH": {
                    "type": "integer", "default": 768, "min": 32, "max": 2048, "step": 8,
                },
                "HEIGHT": {
                    "type": "integer", "default": 1344, "min": 32, "max": 2048, "step": 8,
                },
            },
        }
        template.save(update_fields=("is_active", "model", "input_schema", "updated_at"))
        injected = {
            "PROMPT": prompt.prompt_text,
            "NEGATIVE_PROMPT": prompt.negative_prompt,
            "MODEL": template.model,
            "SEED": project.continuity_seed,
            "WIDTH": 768,
            "HEIGHT": 1344,
            "OUTPUT_PREFIX": f"autoprompt/{project.id}/{prompt.id}",
            "RESOLUTION_PROFILE": "0.98",
            "RESOLUTION_MULTIPLE": 32,
        }

        def materialize_fixture(value):
            if isinstance(value, dict):
                return {
                    key: materialize_fixture(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [materialize_fixture(item) for item in value]
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                return injected.get(value[2:-1], value)
            return value

        workflow_json = materialize_fixture(template.definition)
        graph_contract = json_contract(workflow_json)
        input_contract = {
            "schema_version": 2,
            "compiler": {"id": "generic.comfy", "version": 1},
        }
        input_contract_json = json_contract(input_contract)
        workflow_contract_sha256 = "d" * 64
        workflow_version = WorkflowTemplateVersion.objects.create(
            template=template,
            version=(
                max(
                    template.platform_versions.values_list("version", flat=True),
                    default=0,
                ) + 1
            ),
            graph=workflow_json,
            graph_size_bytes=graph_contract.size_bytes,
            graph_sha256=graph_contract.sha256,
            input_contract=input_contract,
            input_contract_size_bytes=input_contract_json.size_bytes,
            input_contract_sha256=input_contract_json.sha256,
            compiler_id="generic.comfy",
            compiler_version=1,
            semantic_bindings_sha256=json_contract([]).sha256,
            model_requirements_sha256=json_contract([]).sha256,
            execution_contract_sha256=workflow_contract_sha256,
        )
        monitoring = empty_execution_monitoring_contract()
        no_conditioning = empty_conditioning_contract()
        image_semantics_sha256 = json_contract(injected).sha256
        empty_bindings_sha256 = json_contract([]).sha256
        empty_requirements_sha256 = json_contract([]).sha256
        route_snapshot_sha256 = "f" * 64
        execution_metadata = {
            "schema_version": 1,
            "workflow_version_id": str(workflow_version.id),
            "template": {
                "slug": template.slug,
                "output_kind": template.output_kind,
            },
            "model_identifier": template.model,
            "conditioning_contract": no_conditioning,
            "conditioning_contract_sha256": json_contract(no_conditioning).sha256,
            "conditioning_mode_contract": None,
            "conditioning_mode_contract_sha256": "",
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                monitoring,
            ),
        }
        workflow = GeneratedWorkflow.objects.create(
            project=project, episode=episode, prompt_artifact=prompt,
            template=template, workflow_template_version=workflow_version,
            created_by=user, workflow_json=workflow_json,
            injected_values={
                "schema_version": 2,
                "resolved_semantics": injected,
                "asset_bindings": [],
                "model_requirements": [],
                "output_contract": {
                    "schema_version": 1,
                    "primary": {"node_id": "7", "kind": "image", "required": True},
                },
                "execution_metadata": execution_metadata,
                "execution_metadata_sha256": json_contract(execution_metadata).sha256,
            },
            content_hash=graph_contract.sha256,
            compiler_id="generic.comfy",
            compiler_version=1,
            workflow_contract_sha256=workflow_contract_sha256,
            route_snapshot_sha256=route_snapshot_sha256,
            resolved_semantics_sha256=image_semantics_sha256,
            asset_bindings_sha256=empty_bindings_sha256,
            model_requirements_sha256=empty_requirements_sha256,
        )
        task = GenerationTask.objects.create(
            owner=user, project=project, episode=episode,
            generated_workflow=workflow, kind=GenerationTask.Kind.IMAGE,
            backend="comfyui_local", model_profile=template.model,
            max_attempts=3,
            capability_snapshot={
                "workflow_hash": graph_contract.sha256,
                "workflow_contract_sha256": workflow_contract_sha256,
                "route_snapshot_sha256": route_snapshot_sha256,
                "asset_bindings": [],
                "asset_bindings_sha256": empty_bindings_sha256,
                "resolved_semantics_sha256": image_semantics_sha256,
                "model_requirements": [],
                "model_requirements_sha256": empty_requirements_sha256,
                "output_contract": workflow.injected_values["output_contract"],
                "execution_metadata": execution_metadata,
                "execution_metadata_sha256": json_contract(execution_metadata).sha256,
                "conditioning_contract": no_conditioning,
                "conditioning_contract_sha256": execution_metadata[
                    "conditioning_contract_sha256"
                ],
                "conditioning_mode_contract": None,
                "conditioning_mode_contract_sha256": "",
                "execution_monitoring": monitoring,
                "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                    monitoring,
                ),
            },
        )
        return self._freeze_capacity(task)

    def _freeze_capacity(self, task, *, configured=2, effective=2, timeout=120):
        config_sha = "c" * 64
        deployment = ModelDeployment.objects.filter(
            key="queue-capacity-deployment",
        ).first()
        if deployment is None:
            deployment = ModelDeployment(
                key="queue-capacity-deployment",
                name="Queue capacity deployment",
                profile_version=ModelProfileVersion.objects.get(
                    profile_key="sdxl-image",
                    version=1,
                ),
                status=ModelDeployment.Status.ACTIVE,
            )
            assign_json_contract(deployment, "runtime_config", {})
            deployment.save()
        capacity = {
            "schema_version": 3,
            "frozen_at": timezone.now().isoformat(),
            "configured_concurrency": configured,
            "effective_concurrency": effective,
            "host_compatible_concurrency": None,
            "hard_concurrency_ceiling_at_freeze": 4,
            "runtime_host_id": None,
            "host_capability_snapshot_id": None,
            "host_capability_snapshot_revision": None,
            "host_capability_snapshot_probe_status": None,
            "host_capability_snapshot_sha256": "",
            "host_capability_snapshot_captured_at": None,
            "host_capability_snapshot_expires_at": None,
            "configured_task_timeout_seconds": timeout,
            "effective_task_timeout_seconds": timeout,
            "task_idle_timeout_seconds": min(60, timeout),
            "config_snapshot_sha256": config_sha,
        }
        monitoring = empty_execution_monitoring_contract()
        provider_config = dict(deployment.provider.driver_config or {})
        deployment_config = dict(deployment.runtime_config or {})
        provider_config_sha256 = json_contract(provider_config).sha256
        deployment_config_sha256 = json_contract(deployment_config).sha256
        deployment_summary = {
            "schema_version": 1,
            "route_index": 0,
            "model_deployment_id": str(deployment.id),
            "model_profile_version_id": str(deployment.profile_version_id),
            "provider_profile_id": str(deployment.provider.id),
            "provider_config_sha256": provider_config_sha256,
            "deployment_config_sha256": deployment_config_sha256,
            "credential_binding_sha256": "",
            "endpoint_is_external": False,
            "effective_timeout_seconds": timeout,
            "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                monitoring,
            ),
        }
        task.capability_snapshot = {
            **(task.capability_snapshot or {}),
            "production_run_id": "00000000-0000-0000-0000-000000000001",
            "model_deployment_id": str(deployment.id),
            "model_profile_version_id": str(deployment.profile_version_id),
            "provider_profile_id": str(deployment.provider.id),
            "provider_config": provider_config,
            "deployment_config": deployment_config,
            "provider_config_sha256": provider_config_sha256,
            "deployment_config_sha256": deployment_config_sha256,
            "frozen_execution_config_sha256": json_contract({
                "provider_config": provider_config,
                "deployment_config": deployment_config,
            }).sha256,
            "credential_binding_sha256": "",
            "endpoint_is_external": False,
            "effective_timeout_seconds": timeout,
            "route_index": 0,
            "deployment_summary": deployment_summary,
            "deployment_summary_sha256": json_contract(deployment_summary).sha256,
            "config_snapshot_sha256": config_sha,
            "execution_capacity": capacity,
            "execution_capacity_sha256": hashlib.sha256(
                canonical_json_bytes(capacity)
            ).hexdigest(),
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": execution_monitoring_contract_sha256(
                monitoring,
            ),
        }
        task.save(update_fields=("capability_snapshot", "updated_at"))
        return task

    def test_wait_requires_durable_history_and_queue_contract(self):
        task = self._task()

        with self.assertRaisesRegex(
            ComfyUIError,
            "durable history/is_queued",
        ):
            _wait_for_task_completion(task, object(), "prompt-id")

    def _freeze_monitoring(self, task, monitoring):
        digest = execution_monitoring_contract_sha256(monitoring)
        injected = dict(task.generated_workflow.injected_values or {})
        metadata = dict(injected.get("execution_metadata") or {})
        metadata.update({
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": digest,
        })
        injected.update({
            "execution_metadata": metadata,
            "execution_metadata_sha256": json_contract(metadata).sha256,
        })
        task.generated_workflow.injected_values = injected
        task.generated_workflow.save(update_fields=("injected_values", "updated_at"))
        task.capability_snapshot = {
            **(task.capability_snapshot or {}),
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": digest,
        }
        task.save(update_fields=("capability_snapshot", "updated_at"))
        return task

    def test_node_transition_uses_frozen_override_once_without_renewing_hard_deadline(self):
        task = self._freeze_capacity(self._task(), timeout=7_200)
        graph = {
            "9": {"class_type": "LoadImage", "inputs": {}},
            "10": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
            "11": {"class_type": "CreateVideo", "inputs": {}},
        }
        task.generated_workflow.workflow_json = graph
        task.generated_workflow.save(update_fields=("workflow_json", "updated_at"))
        monitoring = resolve_execution_monitoring_contract(
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 300,
                    "node_type_overrides": {
                        "SamplerCustomAdvanced": 3_600,
                    },
                    "node_overrides": {"11": 900},
                },
            },
            workflow_graph=graph,
        )
        self._freeze_monitoring(task, monitoring)
        started = timezone.now()
        hard_deadline = started + timedelta(seconds=7_200)
        task.deadline_started_at = started
        task.hard_deadline_at = hard_deadline
        task.idle_deadline_at = started + timedelta(seconds=60)
        task.last_meaningful_progress_at = started
        task.progress_marker = "phase:submitted"
        task.save(update_fields=(
            "deadline_started_at", "hard_deadline_at",
            "idle_deadline_at", "last_meaningful_progress_at", "progress_marker",
            "updated_at",
        ))

        entered_sampler = started + timedelta(seconds=10)
        self.assertTrue(_mark_task_meaningful_progress(
            task,
            "node:10",
            node_id="10",
            now=entered_sampler,
        ))
        self.assertEqual(
            task.idle_deadline_at,
            entered_sampler + timedelta(seconds=3_600),
        )
        first_idle_deadline = task.idle_deadline_at

        # Repeated running/progress events for the same node use the same
        # marker and cannot create a sliding window.
        self.assertFalse(_mark_task_meaningful_progress(
            task,
            "node:10",
            node_id="10",
            now=started + timedelta(seconds=1_000),
        ))
        self.assertEqual(task.idle_deadline_at, first_idle_deadline)

        entered_encode = started + timedelta(seconds=1_100)
        self.assertTrue(_mark_task_meaningful_progress(
            task,
            "node:11",
            node_id="11",
            now=entered_encode,
        ))
        self.assertEqual(
            task.idle_deadline_at,
            entered_encode + timedelta(seconds=900),
        )
        self.assertEqual(task.hard_deadline_at, hard_deadline)

    def test_claim_uses_frozen_monitoring_default_for_initial_idle_deadline(self):
        task = self._freeze_capacity(self._task(), timeout=7_200)
        monitoring = resolve_execution_monitoring_contract(
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 300,
                    "node_type_overrides": {},
                    "node_overrides": {},
                },
            },
            workflow_graph=task.generated_workflow.workflow_json,
        )
        self._freeze_monitoring(task, monitoring)

        claimed = claim_next_task()

        self.assertEqual(claimed.id, task.id)
        self.assertEqual(
            claimed.idle_deadline_at - claimed.deadline_started_at,
            timedelta(seconds=300),
        )
        self.assertEqual(
            claimed.hard_deadline_at - claimed.deadline_started_at,
            timedelta(seconds=7_200),
        )

    def test_initial_monitoring_window_is_capped_by_frozen_hard_deadline(self):
        task = self._freeze_capacity(self._task(), timeout=120)
        monitoring = resolve_execution_monitoring_contract(
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 5_400,
                    "node_type_overrides": {},
                    "node_overrides": {},
                },
            },
            workflow_graph=task.generated_workflow.workflow_json,
        )
        self._freeze_monitoring(task, monitoring)

        claimed = claim_next_task()

        self.assertEqual(claimed.id, task.id)
        self.assertEqual(claimed.idle_deadline_at, claimed.hard_deadline_at)
        self.assertEqual(
            claimed.hard_deadline_at - claimed.deadline_started_at,
            timedelta(seconds=120),
        )

    def test_task_missing_frozen_monitoring_contract_fails_closed(self):
        task = self._freeze_capacity(self._task())
        task.capability_snapshot.pop("execution_monitoring")
        task.capability_snapshot.pop("execution_monitoring_sha256")
        task.save(update_fields=("capability_snapshot", "updated_at"))

        with self.assertRaisesMessage(
            ComfyUIError,
            "GenerationTask 缺少冻结 execution monitoring",
        ):
            _task_execution_monitoring(task)
        self.assertIsNone(claim_next_task())
        task.refresh_from_db()
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertIn("GenerationTask 缺少冻结", task.error_message)

    def test_workflow_missing_frozen_monitoring_contract_fails_closed(self):
        task = self._freeze_capacity(self._task())
        injected = dict(task.generated_workflow.injected_values or {})
        metadata = dict(injected.get("execution_metadata") or {})
        metadata.pop("execution_monitoring")
        metadata.pop("execution_monitoring_sha256")
        injected["execution_metadata"] = metadata
        injected["execution_metadata_sha256"] = json_contract(metadata).sha256
        task.generated_workflow.injected_values = injected
        task.generated_workflow.save(update_fields=("injected_values", "updated_at"))

        with self.assertRaisesMessage(
            ComfyUIError,
            "GeneratedWorkflow 缺少冻结 execution monitoring",
        ):
            _task_execution_monitoring(task)
        self.assertIsNone(claim_next_task())
        task.refresh_from_db()
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertIn("GeneratedWorkflow 缺少冻结", task.error_message)

    def _link_media_run_with_review_gate(self, task):
        recipe_contract = json_contract({"schema_version": 1, "stages": []})
        recipe = ProductionRecipeVersion.objects.create(
            recipe_key=f"task-auto-advance-{task.id}",
            version=1,
            name="Task auto advance fixture",
            definition=recipe_contract.value,
            definition_schema_version=recipe_contract.schema_version,
            definition_size_bytes=recipe_contract.size_bytes,
            definition_sha256=recipe_contract.sha256,
        )
        run_input = json_contract({
            "schema_version": 1,
            "episode_ids": [str(task.episode_id)],
        })
        config = json_contract({
            "schema_version": 1,
            "values": {"artifact.max_bytes": 10 * 1024 * 1024},
        })
        run = ProductionRun.objects.create(
            owner=task.owner,
            project=task.project,
            recipe_version=recipe,
            idempotency_key=f"task-auto-advance-{task.id}",
            request_sha256="d" * 64,
            status=ProductionRun.Status.RUNNING,
            current_stage_key="media_generation",
            max_generation_tasks=1,
            generation_task_count=1,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=run_input.value,
            input_snapshot_schema_version=run_input.schema_version,
            input_snapshot_size_bytes=run_input.size_bytes,
            input_snapshot_sha256=run_input.sha256,
        )

        def stage_row(*, key, ordinal, mode, status, approval_required=False):
            stage = StageRun(
                production_run=run,
                stage_key=key,
                ordinal=ordinal,
                mode=mode,
                status=status,
                approval_required=approval_required,
                idempotency_key=f"{run.id}:{ordinal}:{key}",
            )
            for field, payload in (
                ("routing", {"schema_version": 1, "variant_count": 1}),
                ("routing_decision", {"schema_version": 1}),
                ("config_snapshot", {"schema_version": 1}),
                ("input_snapshot", {"schema_version": 1, "stage_key": key}),
            ):
                assign_json_contract(stage, field, payload)
            stage.save()
            return stage

        media_stage = stage_row(
            key="media_generation",
            ordinal=1,
            mode="existing_pipeline",
            status=StageRun.Status.RUNNING,
        )
        review_gate = stage_row(
            key="artifact_selection",
            ordinal=2,
            mode="review",
            status=StageRun.Status.PENDING,
            approval_required=True,
        )
        prompt_id = task.generated_workflow.prompt_artifact_id
        request = json_contract({
            "schema_version": 1,
            "prompt_artifact_id": str(prompt_id),
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=media_stage,
            unit_key=f"task:{task.id}",
            attempt_number=1,
            generation_task=task,
            operation="media.generate",
            status=GenerationAttempt.Status.QUEUED,
            request_sha256=request.sha256,
            request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
        )
        task.capability_snapshot = {
            **(task.capability_snapshot or {}),
            "production_run_id": str(run.id),
            "stage_run_id": str(media_stage.id),
            "generation_attempt_id": str(attempt.id),
            "prompt_artifact_id": str(prompt_id),
        }
        task.save(update_fields=("capability_snapshot", "updated_at"))
        return run, media_stage, review_gate

    def test_claim_and_complete(self):
        pending = self._task()
        # These immutable execution-plan facts are frozen together with
        # resolved_semantics by the shared task fixture.  The encoder is still
        # allowed to return a container whose observed clock differs.
        expected_frames = pending.effective_frames
        expected_duration_ms = pending.effective_duration_ms
        task = claim_next_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.status, GenerationTask.Status.RUNNING)
        result_task = process_task(task, FakeClient())
        self.assertEqual(
            result_task.status, GenerationTask.Status.SUCCEEDED, result_task.error_message,
        )
        self.assertEqual(result_task.progress, 100)
        self.assertEqual(result_task.outputs[0]["filename"], "episode.mp4")
        self.assertEqual(result_task.effective_frames, expected_frames)
        self.assertEqual(result_task.effective_duration_ms, expected_duration_ms)
        observed = result_task.outputs[0]["observed_media"]
        self.assertEqual(observed["observation_source"], "ffprobe")
        self.assertEqual(observed["width"], 64)
        self.assertEqual(observed["height"], 64)
        self.assertGreater(observed["frames"], 0)
        self.assertGreater(observed["duration_ms"], 0)
        self.assertNotEqual(observed["frames"], result_task.effective_frames)
        self.assertEqual(result_task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(result_task.comfy_prompt_id, result_task.submission_token)
        preflight = result_task.capability_snapshot["comfyui_contract_preflight"]
        self.assertEqual(preflight["status"], "passed")
        self.assertEqual(preflight["node_count"], 1)
        self.assertEqual(preflight["class_types"], ["SaveImage"])
        self.assertEqual(len(preflight["schema_sha256"]), 64)
        artifact = result_task.generated_artifacts.get()
        self.assertEqual(artifact.mime_type, "video/mp4")
        self.assertEqual(
            artifact.sha256,
            result_task.outputs[0]["provenance"]["artifact_sha256"],
        )
        self.assertTrue((Path(settings.MEDIA_ROOT) / artifact.object_key).is_file())
        self.assertEqual(artifact.manifest_metadata["observed_media"], observed)
        self.assertEqual(
            verified_task_manifest(result_task, 0)["artifact"]["observed_media"],
            observed,
        )
        serialized = GenerationTaskSerializer(result_task).data
        self.assertEqual(
            serialized["input_parameters"]["effective_frames"], expected_frames,
        )
        self.assertEqual(
            serialized["input_parameters"]["effective_duration_ms"],
            expected_duration_ms,
        )
        self.assertEqual(serialized["output_media"][0]["observed_media"], observed)

    def test_prepare_workflow_rejects_missing_frozen_asset_bindings_without_prompt_fallback(self):
        task = self._task()
        snapshot = dict(task.capability_snapshot)
        snapshot.pop("asset_bindings")
        snapshot.pop("asset_bindings_sha256")
        task.capability_snapshot = snapshot
        task.save(update_fields=("capability_snapshot", "updated_at"))

        with self.assertRaisesRegex(
            ComfyUIError,
            "asset_bindings .*缺失.*哈希不一致",
        ):
            prepare_workflow(task, FakeClient())

    def test_prepare_workflow_rejects_frozen_asset_bindings_hash_drift(self):
        task = self._task()
        task.capability_snapshot = {
            **task.capability_snapshot,
            "asset_bindings_sha256": "0" * 64,
        }
        task.save(update_fields=("capability_snapshot", "updated_at"))

        with self.assertRaisesRegex(
            ComfyUIError,
            "asset_bindings .*漂移.*哈希不一致",
        ):
            prepare_workflow(task, FakeClient())

    def test_terminal_comfy_task_auto_advances_run_into_artifact_review_gate(self):
        pending = self._task()
        run, media_stage, review_gate = self._link_media_run_with_review_gate(pending)
        self._freeze_capacity(pending)
        pending.capability_snapshot = {
            **pending.capability_snapshot,
            "production_run_id": str(run.id),
        }
        pending.save(update_fields=("capability_snapshot", "updated_at"))
        task = claim_next_task()
        self.assertEqual(task.id, pending.id)

        with self.captureOnCommitCallbacks(execute=True):
            result = process_task(task, FakeClient())

        self.assertEqual(
            result.status,
            GenerationTask.Status.SUCCEEDED,
            result.error_message,
        )
        run.refresh_from_db()
        media_stage.refresh_from_db()
        review_gate.refresh_from_db()
        self.assertEqual(media_stage.status, StageRun.Status.SUCCEEDED)
        self.assertEqual(review_gate.status, StageRun.Status.AWAITING_APPROVAL)
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(run.current_stage_key, "artifact_selection")

    def test_known_comfy_failure_auto_blocks_linked_media_stage(self):
        pending = self._task()
        run, media_stage, review_gate = self._link_media_run_with_review_gate(pending)
        self._freeze_capacity(pending)
        pending.capability_snapshot = {
            **pending.capability_snapshot,
            "production_run_id": str(run.id),
        }
        pending.save(update_fields=("capability_snapshot", "updated_at"))
        task = claim_next_task()

        with self.captureOnCommitCallbacks(execute=True):
            result = process_task(task, TerminalFailureClient())

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        run.refresh_from_db()
        media_stage.refresh_from_db()
        review_gate.refresh_from_db()
        self.assertEqual(media_stage.status, StageRun.Status.BLOCKED)
        self.assertEqual(media_stage.reason_code, "generation_tasks_failed")
        self.assertEqual(review_gate.status, StageRun.Status.PENDING)
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(run.current_stage_key, "media_generation")

    def test_invalid_media_archive_rolls_back_success_and_fails_task(self):
        self._task()
        task = claim_next_task()

        with self.assertLogs("studio.services.task_queue", level="ERROR"):
            result = process_task(task, InvalidMediaClient())

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.progress, 0)
        self.assertIn("GeneratedArtifact manifest", result.error_message)
        self.assertFalse(result.generated_artifacts.exists())

    @override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=True)
    def test_websocket_sampler_progress_is_persisted_before_completion(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
        })
        pending = self._task()
        task = claim_next_task()
        client = ProgressReportingClient(pending.id)

        result = process_task(task, client)

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

    @override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=True)
    def test_same_node_progress_running_and_heartbeat_do_not_slide_deadlines(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
        })
        pending = self._freeze_capacity(self._task(), timeout=300)
        task = claim_next_task()
        client = RepeatedNodeProgressClient(pending.id)

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertGreaterEqual(len(client.observed_idle_deadlines), 3)
        self.assertEqual(len(set(client.observed_idle_deadlines)), 1)
        self.assertEqual(len(set(client.observed_hard_deadlines)), 1)
        self.assertEqual(result.comfy_progress, 80)
        self.assertTrue(client.socket.closed)

    @override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=True)
    def test_websocket_read_failure_is_persisted_without_failing_task(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
        })
        pending = self._task()
        task = claim_next_task()
        client = ProgressReadFailureClient(pending.id)

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertEqual(
            result.progress_telemetry_state,
            GenerationTask.ProgressTelemetryState.UNAVAILABLE,
        )
        self.assertIn("socket reset", result.progress_telemetry_error)
        self.assertTrue(client.socket.closed)

    @override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=True)
    def test_websocket_open_failure_is_persisted_without_failing_task(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
        })
        pending = self._task()
        task = claim_next_task()
        client = ProgressOpenFailureClient(pending.id)

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertEqual(
            result.progress_telemetry_state,
            GenerationTask.ProgressTelemetryState.UNAVAILABLE,
        )
        self.assertIn("handshake refused", result.progress_telemetry_error)

    @override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=False)
    def test_transient_history_and_queue_timeouts_keep_tracking_persisted_prompt(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
        })
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

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
        self.assertEqual(result.submission_token, task.submission_token)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.SUCCEEDED)
        self.assertGreaterEqual(client.history_calls, 3)
        self.assertGreaterEqual(client.queue_calls, 1)
        self.assertFalse(client.queued)

    @override_settings(
        TASK_PROGRESS_WEBSOCKET_ENABLED=False,
    )
    def test_poll_timeouts_still_run_bounded_health_failure_detection(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
            "runtime.task_health_failure_limit": 2,
        })
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

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.submission_token, token)
        self.assertEqual(result.submission_state, GenerationTask.SubmissionState.RECOVERED)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertIn("health check failed 2 consecutive times", result.error_message)
        self.assertEqual(client.health_calls, 2)
        self.assertEqual(client.cancel_calls, 1)
        self.assertFalse(client.queued)

    def test_sdxl_image_task_preflights_submits_and_persists_png_output(self):
        pending = self._image_task()
        pending.seed = 4242
        pending.workflow_version = 7
        pending.capability_snapshot = {
            **pending.capability_snapshot,
            "model": "sd_xl_base_1.0.safetensors",
            "template_slug": "minimax-reference-image",
            "workflow_mode": "text_to_image",
            "workflow_parameters": {
                "sampling.seed": 4242,
                "sampling.steps": 28,
                "sampling.sampler": "euler_ancestral",
                "sampling.scheduler": "normal",
                "sampling.cfg_scale": 6.5,
                "sampling.denoise": 1.0,
                "credential_ref": "must-not-leak",
                "output_path": "C:\\private\\result.png",
            },
            "width": 768,
            "height": 1344,
        }
        pending.save(update_fields=(
            "seed", "workflow_version", "capability_snapshot", "updated_at",
        ))
        task = claim_next_task()
        client = SDXLImageClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED, result.error_message)
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
        artifact = result.generated_artifacts.get()
        self.assertEqual(artifact.mime_type, "image/png")
        self.assertEqual(
            artifact.sha256,
            result.outputs[0]["provenance"]["artifact_sha256"],
        )
        self.assertTrue((Path(settings.MEDIA_ROOT) / artifact.object_key).is_file())
        self.assertEqual(artifact.comparison_parameters, {
            "schema_version": 1,
            "model": "sd_xl_base_1.0.safetensors",
            "workflow": {
                "template": "minimax-reference-image",
                "version": 7,
                "mode": "text_to_image",
            },
            "generation": {"seed": 4242, "width": 768, "height": 1344},
            "sampling": {
                "seed": 4242,
                "steps": 28,
                "sampler": "euler_ancestral",
                "scheduler": "normal",
                "cfg_scale": 6.5,
                "denoise": 1.0,
            },
        })
        frozen_comparison = str(artifact.comparison_parameters)
        self.assertNotIn("credential", frozen_comparison)
        self.assertNotIn("private", frozen_comparison)

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
            max_attempts=3,
        )
        claimed = claim_next_task()
        self.assertIsNotNone(claimed)
        self.assertTrue(WorkerLease.objects.filter(pk="generation-claim").exists())
        self.assertIsNone(claim_next_task())
        self.assertEqual(
            GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING).count(), 1,
        )

    @override_settings(TASK_MAX_CONCURRENT=4)
    def test_frozen_business_concurrency_limits_claims_below_deployment_ceiling(self):
        first = self._freeze_capacity(self._task(), configured=2, effective=2)
        second = self._freeze_capacity(GenerationTask.objects.create(
            owner=first.owner, project=first.project, episode=first.episode,
            generated_workflow=first.generated_workflow, kind="video",
            max_attempts=3,
        ), configured=2, effective=2)
        third = self._freeze_capacity(GenerationTask.objects.create(
            owner=first.owner, project=first.project, episode=first.episode,
            generated_workflow=first.generated_workflow, kind="video",
            max_attempts=3,
        ), configured=2, effective=2)

        self.assertEqual(claim_next_task().id, first.id)
        self.assertIn(claim_next_task().id, {second.id, third.id})
        self.assertIsNone(claim_next_task())

    @override_settings(TASK_MAX_CONCURRENT=1)
    def test_database_frozen_hard_timeout_cannot_be_overridden_by_caller(self):
        first = self._freeze_capacity(self._task(), configured=3, effective=3, timeout=120)
        self._freeze_capacity(GenerationTask.objects.create(
            owner=first.owner, project=first.project, episode=first.episode,
            generated_workflow=first.generated_workflow, kind="video",
            max_attempts=3,
        ), configured=3, effective=3, timeout=120)

        claimed = claim_next_task()
        self.assertEqual(claimed.id, first.id)
        self.assertLessEqual(task_wait_timeout(claimed, 300), 120)
        self.assertGreater(task_wait_timeout(claimed, 1), 100)
        self.assertIsNone(claim_next_task())

    @override_settings(TASK_MAX_CONCURRENT=4)
    def test_corrupt_platform_capacity_fails_closed_before_claim(self):
        task = self._freeze_capacity(self._task())
        task.capability_snapshot["execution_capacity_sha256"] = "0" * 64
        task.save(update_fields=("capability_snapshot", "updated_at"))

        self.assertIsNone(claim_next_task())
        task.refresh_from_db()
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertIn("execution capacity", task.error_message)

    def test_queue_recommendation_overage_is_an_advisory_and_does_not_cap_volume(self):
        task = self._task()
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=task.project,
            values={
                "production.recommended_max_pending": 1,
                "production.recommended_max_active_per_user": 1,
            },
            created_by=task.owner,
        )
        publish_config_revision(revision, published_by=task.owner)
        with transaction.atomic():
            snapshot = capture_queue_capacity_advisory(
                task.owner,
                additional=1,
                project=task.project,
            )
        self.assertEqual(snapshot["enforcement"], "advisory")
        self.assertEqual(snapshot["recommended_max_active_per_user"], 1)
        self.assertEqual(snapshot["requested_task_count"], 1)
        self.assertEqual(snapshot["advisories"], [
            {
                "schema_version": 1,
                "code": "recommended_max_pending_exceeded",
                "severity": "warning",
                "scope": "platform",
                "measured": 2,
                "recommended_maximum": 1,
            },
            {
                "schema_version": 1,
                "code": "recommended_max_active_per_user_exceeded",
                "severity": "warning",
                "scope": "user",
                "measured": 2,
                "recommended_maximum": 1,
            },
        ])

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

    def test_configured_resource_release_runs_once_before_new_prompt_and_is_audited(self):
        self._task()
        task = claim_next_task()
        client = ResourceReleaseClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(client.release_calls, 1)
        self.assertEqual(client.queue_calls, 1)
        rows = result.capability_snapshot[
            "comfyui_pre_submit_resource_release_attempts"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertFalse(rows[0]["submission_token_persisted"])
        self.assertEqual(len(rows[0]["audit_sha256"]), 64)

    def test_resource_release_failure_is_terminal_before_prompt_and_preserves_audit(self):
        self._task()
        task = claim_next_task()
        client = FailingResourceReleaseClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.DEGRADED)
        self.assertFalse(result.submission_token)
        self.assertFalse(result.comfy_prompt_id)
        self.assertEqual(client.release_calls, 1)
        self.assertEqual(client.queue_calls, 0)
        rows = result.capability_snapshot[
            "comfyui_pre_submit_resource_release_attempts"
        ]
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertEqual(
            rows[-1]["error_code"],
            "comfyui_resource_release_unconfirmed",
        )
        self.assertIn("/prompt was not called", result.error_message)

    def test_existing_prompt_recovery_never_runs_resource_release(self):
        task = self._task()
        task.status = GenerationTask.Status.RUNNING
        task.comfy_prompt_id = "already-submitted"
        task.attempts = 1
        task.save(update_fields=("status", "comfy_prompt_id", "attempts", "updated_at"))
        client = ExistingHistoryReleaseGuardClient()

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.SUCCEEDED)
        self.assertEqual(client.release_calls, 0)
        self.assertNotIn(
            "comfyui_pre_submit_resource_release_attempts",
            result.capability_snapshot,
        )

    def test_legacy_prompt_id_only_archive_failure_is_not_requeued(self):
        task = self._task()
        task.status = GenerationTask.Status.RUNNING
        task.comfy_prompt_id = "legacy-submitted"
        task.attempts = 1
        task.save(update_fields=("status", "comfy_prompt_id", "attempts", "updated_at"))
        client = InvalidExistingHistoryClient()

        with self.assertLogs("studio.services.task_queue", level="ERROR"):
            result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(result.comfy_prompt_id, "legacy-submitted")
        self.assertEqual(result.submission_token, "")
        self.assertFalse(result.generated_artifacts.exists())
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

    def test_prompt_http_rejection_is_terminal_and_preserves_response_detail(self):
        self._task()
        task = claim_next_task()

        result = process_task(task, PromptHTTPRejectionClient())
        serialized = GenerationTaskSerializer(result).data

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            result.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
        )
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.FAILED)
        self.assertTrue(result.submission_token)
        self.assertIn("ComfyUI HTTP 400", result.error_message)
        self.assertIn("prompt_outputs_failed_validation", result.error_message)
        self.assertTrue(serialized["can_retry"])
        self.assertFalse(serialized["retry_requires_reconciliation"])

    def test_prompt_transport_failure_remains_submission_uncertain(self):
        self._task()
        task = claim_next_task()

        result = process_task(task, PromptTransportFailureClient())
        serialized = GenerationTaskSerializer(result).data

        self.assertEqual(result.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            result.submission_state,
            GenerationTask.SubmissionState.PREPARED,
        )
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.MISSING)
        self.assertTrue(result.submission_token)
        self.assertIn("远端是否已接受未知", result.error_message)
        self.assertFalse(serialized["can_retry"])
        self.assertTrue(serialized["retry_requires_reconciliation"])

    def test_consecutive_health_failures_end_sticky_oom_without_waiting_for_timeout(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
            "runtime.task_health_failure_limit": 2,
        })
        self._task()
        task = claim_next_task()
        client = StickyHealthFailureClient()

        result = process_task(task, client)

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
            max_attempts=3,
        )
        self.assertIsNone(claim_next_task())

    def test_database_cancel_is_observed_during_remote_polling(self):
        self._publish_runtime({
            "runtime.task_remote_poll_seconds": 0.05,
            "runtime.task_heartbeat_seconds": 0.05,
            "runtime.task_health_check_interval_seconds": 0,
            "runtime.task_health_failure_limit": 3,
        })
        self._task()
        task = claim_next_task()
        client = CancellationDuringPollClient(task.id)

        result = process_task(task, client)

        self.assertEqual(result.status, GenerationTask.Status.CANCELLED)
        self.assertEqual(
            result.submission_state,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        )
        self.assertEqual(result.remote_state, GenerationTask.RemoteState.CANCELLED)
        self.assertTrue(client.cancelled)
        self.assertIsNotNone(result.finished_at)
        self.assertFalse(GenerationTaskSerializer(result).data["can_retry"])

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

        recovered = self._recover_stale_with_client(client)

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

        recovered = self._recover_stale_with_client(client)

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
            backend=task.backend,
            model_profile=task.model_profile,
            workflow_version=task.workflow_version,
            seed=task.seed,
            max_attempts=3,
            capability_snapshot=dict(task.capability_snapshot or {}),
        )
        client = PersistedRemoteClient(present=False)

        recovered = self._recover_stale_with_client(client)
        claimed = claim_next_task()

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
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
            max_attempts=3,
        )
        client = UnavailablePersistedRemoteClient()

        recovered = self._recover_stale_with_client(client)

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

    def test_concurrent_terminalization_prevents_source_reattachment(self):
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
        client = ConcurrentTerminalizationClient(task.id)

        recovered = self._recover_stale_with_client(client)

        task.refresh_from_db()
        self.assertEqual(recovered, 0)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
        )
        self.assertEqual(task.finished_at, finished_at)
        self.assertFalse(GenerationTask.objects.filter(
            capability_snapshot__retry_of=str(task.id),
        ).exists())
        self.assertFalse(client.queued)

    def test_concurrent_terminalization_prevents_error_overwrite(self):
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
        client = ConcurrentTerminalizationClient(task.id, fail_lookup=True)

        recovered = self._recover_stale_with_client(client)

        task.refresh_from_db()
        self.assertEqual(recovered, 0)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(
            task.submission_state,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
        )
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.MISSING)
        self.assertEqual(task.finished_at, finished_at)
        self.assertFalse(GenerationTask.objects.filter(
            capability_snapshot__retry_of=str(task.id),
        ).exists())
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
            backend=task.backend,
            model_profile=task.model_profile,
            workflow_version=task.workflow_version,
            seed=task.seed,
            max_attempts=3,
            capability_snapshot=dict(task.capability_snapshot or {}),
            priority=100,
        )
        client = PersistedRemoteClient(present=True)

        recovered = self._recover_stale_with_client(client)
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

    @override_settings(TASK_MAX_CONCURRENT=1)
    def test_persisted_submission_reattaches_while_fresh_pool_is_full(self):
        task = self._freeze_capacity(self._task(), configured=1, effective=1)
        token = "914e12c3-6b5e-4c97-a482-c83105a6467e"
        GenerationTask.objects.filter(pk=task.pk).update(
            submission_token=token,
            comfy_prompt_id=token,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        GenerationTask.objects.create(
            owner=task.owner,
            project=task.project,
            episode=task.episode,
            generated_workflow=task.generated_workflow,
            kind=task.kind,
            status=GenerationTask.Status.RUNNING,
            attempts=1,
            max_attempts=3,
        )

        claimed = claim_next_task()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task.id)
        self.assertEqual(claimed.submission_token, token)

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

        recovered = self._recover_stale_with_client(client)

        task.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(task.status, GenerationTask.Status.FAILED)
        self.assertEqual(task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertIsNotNone(task.stalled_at)
        self.assertIn("automatic resubmission is blocked", task.error_message)
        self.assertFalse(client.queued)

    def test_cancel_requested_reconciles_after_cancel_grace_without_waiting_for_stale_threshold(self):
        self._publish_runtime({"runtime.task_cancel_grace_seconds": 1})
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

        recovered = self._recover_stale_with_client(client)

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

    def test_cancel_requested_stays_unresolved_while_remote_prompt_is_still_queued(self):
        self._publish_runtime({"runtime.task_cancel_grace_seconds": 1})
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

        recovered = self._recover_stale_with_client(client)

        task.refresh_from_db()
        self.assertEqual(recovered, 0)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(task.status, GenerationTask.Status.CANCEL_REQUESTED)
        self.assertEqual(task.submission_state, GenerationTask.SubmissionState.SUBMITTED)
        self.assertEqual(task.remote_state, GenerationTask.RemoteState.CANCELLING)
        self.assertIsNone(task.finished_at)

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

        recovered = self._recover_stale_with_client(client)

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
        with self.assertRaisesRegex(ComfyUIError, "参考素材不存在、已停用或当前用户无权使用"):
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
