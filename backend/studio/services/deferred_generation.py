from __future__ import annotations

import hashlib
import json
import mimetypes
import subprocess
import tempfile
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from studio.models import (
    Asset,
    DeferredGenerationRequest,
    GenerationTask,
    PromptArtifact,
    SeriesProject,
    Shot,
    WorkflowTemplate,
)
from studio.services.h3_prompts import PromptBuildError, capture_consistency_snapshot
from studio.services.production_pipeline import (
    ProductionQueueConfirmationRequired,
    assert_queue_capacity,
    compile_prompt_input,
    materialize_compiled_prompt,
    materialize_generated_workflow,
    materialize_generation_task,
    workflow_template_contract_sha256,
)
from studio.services.production_batch_slots import effective_batch_snapshot_for_task
from studio.services.shot_versions import (
    ShotVersionResolutionError,
    resolve_episode_shot_version,
    shot_version_key,
    shot_version_label,
    shot_version_number,
)
from studio.services.provenance import (
    ProvenanceError,
    resolve_declared_task_output,
    sha256_file,
)
from studio.services.workflow import WorkflowBuildError


DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION = 1
DEFERRED_PROVISIONAL_PROMPT_MAX_CHARS = 65_536
DEFERRED_FRAME_EXTRACT_TIMEOUT_SECONDS = 120
DEFERRED_GENERATION_PURPOSE = "deferred_generation"


class DeferredGenerationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT

    def __init__(self, *, code: str, detail: str, **extra):
        self.default_code = code
        payload = {"detail": detail, **extra}
        super().__init__(detail=payload, code=code)
        self.detail = payload


class DeferredGenerationUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self, *, code: str, detail: str, **extra):
        self.default_code = code
        payload = {"detail": detail, **extra}
        super().__init__(detail=payload, code=code)
        self.detail = payload


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ref2va_picture_count_after_first_frame_reservation(target_shot: Shot) -> int:
    bindings = target_shot.reference_bindings if isinstance(target_shot.reference_bindings, dict) else {}
    return (
        1
        + len(bindings.get("characters") or [])
        + len(bindings.get("props") or [])
        + (1 if bindings.get("scene") else 0)
    )


def _frame_slot_compatible(target_shot: Shot, frame_slot: str) -> bool:
    if target_shot.media_type != Shot.MediaType.VIDEO:
        return False
    if frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME:
        return target_shot.h3_mode in {Shot.H3Mode.I2VA, Shot.H3Mode.FL2VA} or (
            target_shot.h3_mode == Shot.H3Mode.REF2VA
            and target_shot.requires_first_frame_continuity
            and _ref2va_picture_count_after_first_frame_reservation(target_shot) <= 9
        )
    if frame_slot == DeferredGenerationRequest.FrameSlot.LAST_FRAME:
        return target_shot.h3_mode in {Shot.H3Mode.L2VA, Shot.H3Mode.FL2VA}
    return False


def _source_extraction_slot_for_target(frame_slot: str) -> str:
    """Map a target slot to the source frame that should be extracted.

    A downstream first-frame continuity request must consume the upstream
    video's ending state.  The target slot remains ``first_frame`` for audit
    and prompt binding; the extracted source frame is the previous shot tail.
    """

    if frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME:
        return DeferredGenerationRequest.FrameSlot.LAST_FRAME
    return frame_slot


def _select_deferred_target_template(
    *,
    project: SeriesProject,
    target_shot: Shot,
    preferred_template: WorkflowTemplate | None = None,
) -> WorkflowTemplate:
    queryset = WorkflowTemplate.objects.select_for_update().filter(
        provider="comfyui",
        output_kind=WorkflowTemplate.OutputKind.VIDEO,
        is_active=True,
    ).filter(
        # Global system templates plus tenant templates owned by the project
        # owner.  Staff-only hidden templates are intentionally not guessed.
        Q(owner__isnull=True) | Q(owner_id=project.owner_id),
    )
    candidates = []
    for template in queryset:
        schema = template.input_schema if isinstance(template.input_schema, dict) else {}
        modes = schema.get("h3_modes") or []
        if modes and target_shot.h3_mode not in modes:
            continue
        candidates.append(template)
    if not candidates:
        raise DeferredGenerationUnavailable(
            code="deferred_target_template_unavailable",
            detail=f"没有 active ComfyUI 视频模板支持目标分镜模式 {target_shot.h3_mode}。",
        )
    if preferred_template is not None:
        for candidate in candidates:
            if candidate.pk == preferred_template.pk:
                return candidate
    candidates.sort(key=lambda item: (
        not bool((item.input_schema or {}).get("h3_modes")),
        not bool((item.input_schema or {}).get("is_default")),
        item.owner_id is not None,
        -item.version,
        item.slug,
    ))
    return candidates[0]


def _execution_overrides_from_source_task(
    *,
    source_task: GenerationTask,
    target_template: WorkflowTemplate,
) -> dict[str, Any]:
    """Rebuild safe execution overrides from a source task's frozen snapshot."""

    snapshot = source_task.capability_snapshot
    if not isinstance(snapshot, dict):
        return {}
    overrides: dict[str, Any] = {}
    profile = snapshot.get("resolution_profile")
    if isinstance(profile, str) and profile:
        overrides["RESOLUTION_PROFILE"] = profile
    for snapshot_key, override_key in (
        ("resolution_multiple", "RESOLUTION_MULTIPLE"),
        ("width", "WIDTH"),
        ("height", "HEIGHT"),
    ):
        value = snapshot.get(snapshot_key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            overrides[override_key] = value
    workflow_parameters = snapshot.get("workflow_parameters")
    parameter_definitions = (
        (target_template.input_schema or {}).get("user_parameters") or {}
    )
    if isinstance(workflow_parameters, dict) and isinstance(parameter_definitions, dict):
        for key in parameter_definitions:
            if isinstance(key, str) and key in workflow_parameters:
                overrides[key] = deepcopy(workflow_parameters[key])
    return overrides


def _deferred_target_workflow_overrides(
    *,
    source_task: GenerationTask,
    target_template: WorkflowTemplate,
    selected_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inherited = _execution_overrides_from_source_task(
        source_task=source_task,
        target_template=target_template,
    )
    if selected_overrides is None:
        return inherited
    if not isinstance(selected_overrides, dict):
        raise ValidationError({"target_workflow_overrides": "必须是 JSON 对象。"})
    return {**inherited, **deepcopy(selected_overrides)}


def _source_task_has_primary_video_contract(source_task: GenerationTask) -> bool:
    snapshot = source_task.capability_snapshot
    if not isinstance(snapshot, dict):
        return False
    contract = snapshot.get("output_contract")
    if not isinstance(contract, dict):
        return False
    primary = contract.get(DeferredGenerationRequest.SOURCE_OUTPUT_ROLE_PRIMARY_VIDEO)
    return bool(
        contract.get("schema_version") == 1
        and isinstance(primary, dict)
        and primary.get("media_kind") == "video"
        and primary.get("required") is True
        and isinstance(primary.get("node_id"), str)
        and primary["node_id"]
    )


def _compile_deferred_target(
    *, project: SeriesProject, target_shot: Shot,
):
    snapshot = capture_consistency_snapshot(project)
    provisional_mode = (
        Shot.H3Mode.FL2VA
        if target_shot.h3_mode in {Shot.H3Mode.I2VA, Shot.H3Mode.L2VA}
        else target_shot.h3_mode
    )
    return compile_prompt_input(
        project=project,
        shots=[target_shot],
        prompt_type=PromptArtifact.PromptType.VIDEO,
        mode=provisional_mode,
        include_audio=target_shot.include_audio,
        duration_overrides={str(target_shot.id): target_shot.duration_ms},
        consistency_snapshot=snapshot,
    )


def _request_fingerprint(
    *,
    project: SeriesProject,
    source_task: GenerationTask,
    target_shot: Shot,
    frame_slot: str,
    target_template: WorkflowTemplate,
    target_template_hash: str,
    target_input_fingerprint: str,
    target_workflow_overrides: dict[str, Any],
    priority: int,
    max_attempts: int,
    extended_generation_confirmed: bool,
) -> str:
    return _canonical_sha256({
        "schema_version": DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION,
        "project_id": str(project.id),
        "source_task_id": str(source_task.id),
        "source_output_role": DeferredGenerationRequest.SOURCE_OUTPUT_ROLE_PRIMARY_VIDEO,
        "target_shot_id": str(target_shot.id),
        "frame_slot": frame_slot,
        "target_template_id": str(target_template.id),
        "target_template_hash": target_template_hash,
        "target_input_fingerprint": target_input_fingerprint,
        "target_workflow_overrides": target_workflow_overrides,
        "priority": priority,
        "max_attempts": max_attempts,
        "extended_generation_confirmed": extended_generation_confirmed,
    })


def create_deferred_generation_request(
    *,
    user,
    project: SeriesProject,
    source_task: GenerationTask,
    target_shot: Shot,
    frame_slot: str,
    idempotency_key: str,
    preferred_template: WorkflowTemplate | None = None,
    target_workflow_overrides: dict[str, Any] | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    extended_generation_confirmed: bool = False,
) -> tuple[DeferredGenerationRequest, bool]:
    """Create or replay one deferred first/last-frame dependency.

    This function deliberately stops before PromptArtifact/GeneratedWorkflow/
    GenerationTask creation.  Formal production is worker materialization after
    the source video output has a verified signed manifest.
    """

    with transaction.atomic():
        project = SeriesProject.objects.select_for_update().get(pk=project.pk)
        if not user.is_staff and project.owner_id != user.id:
            raise PermissionError("project is not owned by the request user")
        source_task = GenerationTask.objects.select_for_update().select_related(
            "project", "generated_workflow__template",
        ).get(pk=source_task.pk)
        target_shot = Shot.objects.select_for_update().select_related(
            "episode__project",
        ).get(pk=target_shot.pk)
        if source_task.project_id != project.id:
            raise DeferredGenerationConflict(
                code="deferred_generation_source_terminal",
                detail="上游任务不属于当前项目。",
            )
        if target_shot.episode.project_id != project.id:
            raise DeferredGenerationConflict(
                code="deferred_generation_target_incompatible",
                detail="目标分镜不属于当前项目。",
            )
        if source_task.kind != PromptArtifact.PromptType.VIDEO:
            raise DeferredGenerationConflict(
                code="deferred_generation_source_contract_missing",
                detail="上游任务不是视频任务，无法作为首尾帧来源。",
            )
        if source_task.status in {
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        }:
            raise DeferredGenerationConflict(
                code="deferred_generation_source_terminal",
                detail="上游任务已经失败或取消，不能创建延迟连续生成请求。",
            )
        if not _source_task_has_primary_video_contract(source_task):
            raise DeferredGenerationConflict(
                code="deferred_generation_source_contract_missing",
                detail="上游任务缺少冻结 primary_video 输出契约；请重新创建上游任务。",
            )
        if not _frame_slot_compatible(target_shot, frame_slot):
            raise DeferredGenerationConflict(
                code="deferred_generation_target_incompatible",
                detail="目标分镜模式不支持所选首帧/尾帧槽位；系统不会自动改模式。",
            )
        target_template = _select_deferred_target_template(
            project=project,
            target_shot=target_shot,
            preferred_template=preferred_template,
        )
        target_template_hash = workflow_template_contract_sha256(target_template)
        target_workflow_overrides = _deferred_target_workflow_overrides(
            source_task=source_task,
            target_template=target_template,
            selected_overrides=target_workflow_overrides,
        )
        try:
            compiled = _compile_deferred_target(
                project=project, target_shot=target_shot,
            )
        except PromptBuildError as exc:
            raise ValidationError({"target_shot_id": str(exc)}) from exc
        if len(compiled.prompt_text) > DEFERRED_PROVISIONAL_PROMPT_MAX_CHARS:
            raise ValidationError({
                "target_shot_id": (
                    "目标分镜临时提示词超过 64KiB；请缩短分镜或 Bible 描述。"
                ),
            })
        request_fingerprint = _request_fingerprint(
            project=project,
            source_task=source_task,
            target_shot=target_shot,
            frame_slot=frame_slot,
            target_template=target_template,
            target_template_hash=target_template_hash,
            target_input_fingerprint=compiled.input_fingerprint,
            target_workflow_overrides=target_workflow_overrides,
            priority=priority,
            max_attempts=max_attempts,
            extended_generation_confirmed=extended_generation_confirmed,
        )
        existing = DeferredGenerationRequest.objects.select_for_update().filter(
            project=project,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise DeferredGenerationConflict(
                    code="deferred_generation_idempotency_conflict",
                    detail="该 idempotency_key 已用于不同的延迟生成请求。",
                    idempotency_key=idempotency_key,
                )
            return existing, False
        active = DeferredGenerationRequest.objects.select_for_update().filter(
            target_shot=target_shot,
            frame_slot=frame_slot,
            status__in=DeferredGenerationRequest.ACTIVE_STATUSES,
        ).first()
        if active is not None:
            raise DeferredGenerationConflict(
                code="deferred_generation_active_conflict",
                detail="目标分镜的该帧槽位已有未完成的延迟生成请求。",
                deferred_generation_id=str(active.id),
            )
        try:
            request = DeferredGenerationRequest.objects.create(
                owner=user,
                project=project,
                source_task=source_task,
                target_shot=target_shot,
                frame_slot=frame_slot,
                target_template=target_template,
                target_template_hash=target_template_hash,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                target_input_fingerprint=compiled.input_fingerprint,
                target_workflow_overrides=target_workflow_overrides,
                status=DeferredGenerationRequest.Status.WAITING_UPSTREAM,
                provisional_prompt_text=compiled.prompt_text,
                provisional_negative_prompt=compiled.negative_prompt,
                provisional_prompt_sha256=hashlib.sha256(
                    compiled.prompt_text.encode("utf-8"),
                ).hexdigest(),
                priority=priority,
                max_attempts=max_attempts,
                extended_generation_confirmed=extended_generation_confirmed,
                next_attempt_at=timezone.now(),
            )
        except IntegrityError as exc:
            raise DeferredGenerationConflict(
                code="deferred_generation_active_conflict",
                detail="并发请求已占用同一目标帧槽位，请刷新后重试。",
            ) from exc
        return request, True


def target_requires_deferred_first_frame(target_shot: Shot) -> bool:
    """Return whether target generation must wait for a previous-shot tail frame."""

    return bool(
        target_shot.media_type == Shot.MediaType.VIDEO
        and target_shot.requires_first_frame_continuity
        and _frame_slot_compatible(
            target_shot,
            DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
        )
    )


def previous_video_shot_for(target_shot: Shot) -> Shot | None:
    try:
        version = resolve_episode_shot_version(
            target_shot.episode,
            shot_version_key(target_shot),
        )
    except ShotVersionResolutionError:
        return None
    target_index = next(
        (index for index, shot in enumerate(version.shots) if shot.pk == target_shot.pk),
        -1,
    )
    if target_index <= 0:
        return None
    return next(
        (
            shot for shot in reversed(version.shots[:target_index])
            if shot.media_type == Shot.MediaType.VIDEO
        ),
        None,
    )


def latest_viable_video_task_for_shot(*, user, project: SeriesProject, shot: Shot) -> GenerationTask | None:
    return (
        GenerationTask.objects.filter(
            owner=user,
            project=project,
            kind=PromptArtifact.PromptType.VIDEO,
            generated_workflow__prompt_artifact__shot=shot,
        )
        .exclude(status__in={
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        })
        .select_related("project", "generated_workflow__template")
        .order_by("-created_at")
        .first()
    )


def _deferred_matches_request_key(
    request: DeferredGenerationRequest,
    *,
    request_key: str,
) -> bool:
    """Return whether an active deferred request belongs to the same batch request."""

    if not request_key:
        return True
    snapshot = effective_batch_snapshot_for_task(request.source_task)
    return str(snapshot.get("idempotency_key") or "") == request_key


def active_first_frame_deferred_for(
    target_shot: Shot,
    *,
    request_key: str = "",
) -> DeferredGenerationRequest | None:
    queryset = (
        DeferredGenerationRequest.objects.filter(
            target_shot=target_shot,
            frame_slot=DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
            status__in=DeferredGenerationRequest.ACTIVE_STATUSES,
        )
        .select_related("source_task")
        .order_by("-created_at")
    )
    if not request_key:
        return queryset.first()
    for request in queryset:
        if _deferred_matches_request_key(request, request_key=request_key):
            return request
    return None


def auto_deferred_idempotency_key(
    *,
    source_task: GenerationTask,
    target_shot: Shot,
    request_key: str = "",
) -> str:
    digest = hashlib.sha256(
        (
            f"{request_key}|{source_task.id}|{target_shot.id}|"
            f"{DeferredGenerationRequest.FrameSlot.FIRST_FRAME}|"
            f"{DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION}"
        ).encode("utf-8"),
    ).hexdigest()[:24]
    return f"auto-continuity-{digest}-{str(source_task.id)[:8]}-{str(target_shot.id)[:8]}"


def ensure_auto_first_frame_deferred(
    *,
    user,
    project: SeriesProject,
    source_task: GenerationTask,
    target_shot: Shot,
    priority: int = 0,
    max_attempts: int = 3,
    extended_generation_confirmed: bool = False,
    request_key: str = "",
    preferred_template: WorkflowTemplate | None = None,
    target_workflow_overrides: dict[str, Any] | None = None,
) -> tuple[DeferredGenerationRequest, bool]:
    """Create or reuse the first-frame deferred request for one target shot."""

    if not target_requires_deferred_first_frame(target_shot):
        raise DeferredGenerationConflict(
            code="deferred_generation_target_incompatible",
            detail="目标分镜未启用可物化的首帧承接要求。",
        )
    active = active_first_frame_deferred_for(
        target_shot,
        request_key=request_key,
    )
    if active is not None:
        return active, False
    return create_deferred_generation_request(
        user=user,
        project=project,
        source_task=source_task,
        target_shot=target_shot,
        frame_slot=DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
        idempotency_key=auto_deferred_idempotency_key(
            source_task=source_task,
            target_shot=target_shot,
            request_key=request_key,
        ),
        preferred_template=preferred_template,
        target_workflow_overrides=target_workflow_overrides,
        priority=priority,
        max_attempts=max_attempts,
        extended_generation_confirmed=extended_generation_confirmed,
    )


def ensure_next_first_frame_deferred_for_task(
    task: GenerationTask,
    *,
    request_key: str = "",
    extended_generation_confirmed: bool | None = None,
) -> tuple[DeferredGenerationRequest, bool] | None:
    """If a completed/queued source shot is followed by a dependent shot, register it."""

    if task.kind != PromptArtifact.PromptType.VIDEO:
        return None
    prompt = task.generated_workflow.prompt_artifact
    source_shot = prompt.shot
    if source_shot is None or source_shot.media_type != Shot.MediaType.VIDEO:
        return None
    if not source_shot.requires_last_frame_continuity:
        return None
    try:
        version = resolve_episode_shot_version(
            source_shot.episode,
            shot_version_key(source_shot),
        )
    except ShotVersionResolutionError:
        return None
    source_index = next(
        (index for index, shot in enumerate(version.shots) if shot.pk == source_shot.pk),
        -1,
    )
    if source_index < 0:
        return None
    target_shot = next(
        (
            shot for shot in version.shots[source_index + 1:]
            if shot.media_type == Shot.MediaType.VIDEO
        ),
        None,
    )
    if target_shot is None or not target_requires_deferred_first_frame(target_shot):
        return None
    return ensure_auto_first_frame_deferred(
        user=task.owner,
        project=task.project,
        source_task=task,
        target_shot=target_shot,
        priority=task.priority,
        max_attempts=task.max_attempts,
        extended_generation_confirmed=(
            bool((task.capability_snapshot or {}).get("extended_generation_confirmed"))
            if extended_generation_confirmed is None else extended_generation_confirmed
        ),
        request_key=request_key or str((task.capability_snapshot or {}).get("idempotency_key") or ""),
    )


def cancel_deferred_generation_request(
    *, request: DeferredGenerationRequest, user,
) -> DeferredGenerationRequest:
    with transaction.atomic():
        locked = DeferredGenerationRequest.objects.select_for_update().select_related(
            "project",
        ).get(pk=request.pk)
        if not user.is_staff and locked.owner_id != user.id:
            raise PermissionError("request is not owned by the request user")
        if (
            locked.status not in {
                DeferredGenerationRequest.Status.PREPARING_PROMPT,
                DeferredGenerationRequest.Status.WAITING_UPSTREAM,
            }
            or locked.claim_token
        ):
            raise DeferredGenerationConflict(
                code="deferred_generation_cancel_conflict",
                detail="该延迟生成请求已经进入执行或终态，不能取消。",
            )
        locked.status = DeferredGenerationRequest.Status.CANCELLED
        locked.finished_at = timezone.now()
        locked.error_code = ""
        locked.error_message = ""
        locked.save(update_fields=(
            "status", "finished_at", "error_code", "error_message", "updated_at",
        ))
        return locked


def cascade_cancel_downstream_generation(
    *, source_task: GenerationTask, user,
) -> dict[str, Any]:
    """Cancel deferred continuity work that depends on ``source_task``.

    Cancelling a task must stop later continuity work, but it must not destroy
    the audit trail.  Deferred rows are terminalized as cancelled; already
    materialized child tasks that have not reached a terminal state are marked
    cancelled or cancel-requested, then their own downstream dependencies are
    walked recursively.  A later retry creates a new task id, so
    ``ensure_next_first_frame_deferred_for_task`` can register a fresh
    dependency without being blocked by these historical cancelled rows.
    """

    now = timezone.now()
    terminal_task_statuses = {
        GenerationTask.Status.SUCCEEDED,
        GenerationTask.Status.FAILED,
        GenerationTask.Status.CANCELLED,
    }
    terminal_submission_states = {
        GenerationTask.SubmissionState.TERMINAL_FAILED,
        GenerationTask.SubmissionState.TERMINAL_CANCELLED,
    }
    summary: dict[str, Any] = {
        "deferred_requests_cancelled": 0,
        "tasks_cancelled": 0,
        "tasks_cancel_requested": 0,
        "deferred_request_ids": [],
        "task_ids": [],
    }
    visited_task_ids: set[str] = set()

    def _mark_child_task_cancelled(task: GenerationTask) -> bool:
        if task.status in terminal_task_statuses:
            return task.status in {
                GenerationTask.Status.FAILED,
                GenerationTask.Status.CANCELLED,
            }
        remote_token = task.submission_token or task.comfy_prompt_id
        needs_remote_cancel = bool(remote_token) and (
            task.status in {
                GenerationTask.Status.RUNNING,
                GenerationTask.Status.CANCEL_REQUESTED,
            }
            or task.submission_state not in terminal_submission_states
        )
        if needs_remote_cancel:
            task.status = GenerationTask.Status.CANCEL_REQUESTED
            task.heartbeat_at = now
            task.remote_state = GenerationTask.RemoteState.CANCELLING
            task.remote_error = "上游任务已取消，等待 worker 取消远端任务。"
            task.error_message = "上游任务已取消，后续任务自动取消。"
            task.save(update_fields=(
                "status", "heartbeat_at", "remote_state", "remote_error",
                "error_message", "updated_at",
            ))
            summary["tasks_cancel_requested"] += 1
        else:
            task.status = GenerationTask.Status.CANCELLED
            task.finished_at = now
            task.heartbeat_at = now
            task.progress = 0
            task.remote_state = GenerationTask.RemoteState.CANCELLED
            task.remote_error = ""
            task.error_message = "上游任务已取消，后续任务自动取消。"
            if task.submission_token:
                task.submission_state = GenerationTask.SubmissionState.TERMINAL_CANCELLED
            task.save(update_fields=(
                "status", "finished_at", "heartbeat_at", "progress",
                "remote_state", "remote_error", "error_message",
                "submission_state", "updated_at",
            ))
            summary["tasks_cancelled"] += 1
        summary["task_ids"].append(str(task.id))
        return True

    def _walk(task_id) -> None:
        key = str(task_id)
        if key in visited_task_ids:
            return
        visited_task_ids.add(key)
        requests = list(
            DeferredGenerationRequest.objects.select_for_update()
            .select_related("materialized_task")
            .filter(
                source_task_id=task_id,
                owner_id=source_task.owner_id,
            )
            .order_by("created_at", "id")
        )
        for deferred in requests:
            if deferred.status in DeferredGenerationRequest.ACTIVE_STATUSES:
                deferred.status = DeferredGenerationRequest.Status.CANCELLED
                deferred.finished_at = now
                deferred.error_code = "deferred_source_cancelled_by_user"
                deferred.error_message = "上游任务已取消，后续连续生成请求自动取消。"
                deferred.claim_token = ""
                deferred.worker_id = ""
                deferred.claimed_at = None
                deferred.heartbeat_at = now
                deferred.save(update_fields=(
                    "status", "finished_at", "error_code", "error_message",
                    "claim_token", "worker_id", "claimed_at", "heartbeat_at",
                    "updated_at",
                ))
                summary["deferred_requests_cancelled"] += 1
                summary["deferred_request_ids"].append(str(deferred.id))
                continue
            child = deferred.materialized_task
            if child is None:
                continue
            should_walk_child = (
                child.status in {
                    GenerationTask.Status.QUEUED,
                    GenerationTask.Status.RUNNING,
                    GenerationTask.Status.CANCEL_REQUESTED,
                    GenerationTask.Status.FAILED,
                    GenerationTask.Status.CANCELLED,
                }
            )
            if child.status not in terminal_task_statuses:
                should_walk_child = _mark_child_task_cancelled(child)
            if should_walk_child:
                _walk(child.id)

    if not getattr(user, "is_staff", False) and source_task.owner_id != user.id:
        raise PermissionError("task is not owned by the request user")
    with transaction.atomic():
        _walk(source_task.id)
    return summary


class _DerivedFrameFileGuard:
    """Delete a newly stored derived frame if DB materialization rolls back."""

    def __init__(self):
        self._storage = None
        self._name = ""

    def __enter__(self):
        return self

    def track(self, field_file) -> None:
        self._storage = field_file.storage
        self._name = str(field_file.name or "")

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None and self._storage is not None and self._name:
            try:
                self._storage.delete(self._name)
            except Exception:
                pass
        return False


def extract_last_decodable_frame(
    source_path: Path,
    frame_slot: str,
) -> Path:
    """Extract one PNG frame from a verified video output.

    The caller owns the returned temporary file and must delete it after the
    derived Asset is safely copied into managed storage.
    """

    source_path = Path(source_path)
    if frame_slot not in set(DeferredGenerationRequest.FrameSlot.values):
        raise ValueError("unsupported deferred frame slot")
    if not source_path.is_file():
        raise FileNotFoundError("source video output is unavailable")
    handle = tempfile.NamedTemporaryFile(
        prefix="autoprompt-deferred-frame-",
        suffix=".png",
        delete=False,
    )
    output_path = Path(handle.name)
    handle.close()
    command = [
        str(getattr(settings, "FFMPEG_BINARY", "ffmpeg")),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if frame_slot == DeferredGenerationRequest.FrameSlot.LAST_FRAME:
        command += ["-sseof", "-3"]
    command += [
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-update",
        "1",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DEFERRED_FRAME_EXTRACT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("frame extraction timed out") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("frame extractor is unavailable") from exc
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("frame extraction failed")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("frame extractor did not create a valid image")
    return output_path


def _terminal_source_statuses() -> set[str]:
    return {
        GenerationTask.Status.SUCCEEDED,
        GenerationTask.Status.FAILED,
        GenerationTask.Status.CANCELLED,
    }


def claim_next_deferred_request(worker_id: str) -> DeferredGenerationRequest | None:
    """Claim one due deferred request whose source task is terminal."""

    now = timezone.now()
    with transaction.atomic():
        request = (
            DeferredGenerationRequest.objects.select_for_update()
            .select_related("source_task")
            .filter(
                status=DeferredGenerationRequest.Status.WAITING_UPSTREAM,
                claim_token="",
                next_attempt_at__lte=now,
                source_task__status__in=_terminal_source_statuses(),
            )
            .order_by("-priority", "created_at")
            .first()
        )
        if request is None:
            return None
        request.claim_token = uuid.uuid4().hex
        request.worker_id = worker_id[:120]
        request.claimed_at = now
        request.heartbeat_at = now
        request.started_at = request.started_at or now
        request.attempts = min(request.attempts + 1, request.max_attempts)
        if request.source_task.status == GenerationTask.Status.SUCCEEDED:
            request.status = DeferredGenerationRequest.Status.EXTRACTING
        request.save(update_fields=(
            "claim_token", "worker_id", "claimed_at", "heartbeat_at",
            "started_at", "attempts", "status", "updated_at",
        ))
        return request


def recover_stale_deferred_requests(minutes: int = 30) -> int:
    """Release lost worker claims without touching completed requests."""

    cutoff = timezone.now() - timedelta(minutes=max(1, int(minutes)))
    with transaction.atomic():
        queryset = DeferredGenerationRequest.objects.select_for_update().filter(
            status__in=(
                DeferredGenerationRequest.Status.EXTRACTING,
                DeferredGenerationRequest.Status.MATERIALIZING,
            ),
            claim_token__gt="",
            heartbeat_at__lt=cutoff,
        )
        count = 0
        for request in queryset:
            request.status = DeferredGenerationRequest.Status.WAITING_UPSTREAM
            request.claim_token = ""
            request.worker_id = ""
            request.claimed_at = None
            request.heartbeat_at = None
            request.next_attempt_at = timezone.now()
            request.save(update_fields=(
                "status", "claim_token", "worker_id", "claimed_at",
                "heartbeat_at", "next_attempt_at", "updated_at",
            ))
            count += 1
        return count


def _finalize_deferred(
    request: DeferredGenerationRequest,
    *,
    status_value: str,
    error_code: str = "",
    error_message: str = "",
) -> DeferredGenerationRequest:
    with transaction.atomic():
        locked = DeferredGenerationRequest.objects.select_for_update().get(pk=request.pk)
        locked.status = status_value
        locked.error_code = error_code
        locked.error_message = error_message[:2000]
        locked.claim_token = ""
        locked.worker_id = ""
        locked.claimed_at = None
        locked.heartbeat_at = timezone.now()
        locked.finished_at = timezone.now()
        locked.save(update_fields=(
            "status", "error_code", "error_message", "claim_token",
            "worker_id", "claimed_at", "heartbeat_at", "finished_at",
            "updated_at",
        ))
        return locked


def _mark_deferred_retryable_failure(
    request: DeferredGenerationRequest,
    *,
    code: str,
    message: str,
) -> DeferredGenerationRequest:
    with transaction.atomic():
        locked = DeferredGenerationRequest.objects.select_for_update().get(pk=request.pk)
        terminal = locked.attempts >= locked.max_attempts
        locked.status = (
            DeferredGenerationRequest.Status.FAILED
            if terminal else DeferredGenerationRequest.Status.WAITING_UPSTREAM
        )
        locked.error_code = code
        locked.error_message = message[:2000]
        locked.claim_token = ""
        locked.worker_id = ""
        locked.claimed_at = None
        locked.heartbeat_at = timezone.now()
        locked.next_attempt_at = timezone.now() + timedelta(seconds=30)
        if terminal:
            locked.finished_at = timezone.now()
        locked.save(update_fields=(
            "status", "error_code", "error_message", "claim_token",
            "worker_id", "claimed_at", "heartbeat_at", "next_attempt_at",
            "finished_at", "updated_at",
        ))
        return locked


def _create_derived_frame_asset(
    *,
    request: DeferredGenerationRequest,
    frame_path: Path,
    source_output: dict,
    file_guard: _DerivedFrameFileGuard,
) -> Asset:
    slot_label = "首帧" if request.frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME else "尾帧"
    artifact_sha256 = sha256_file(frame_path)
    asset = Asset(
        owner=request.owner,
        style=request.project.style,
        kind=Asset.Kind.REFERENCE,
        name=(
            f"{request.target_shot.title or f'镜头{request.target_shot.number}'} · "
            f"自动{slot_label}连续帧"
        )[:160],
        description=(
            "由延迟连续生成请求从上游视频 primary_video 输出中抽取，"
            "仅作为下游镜头首/尾帧参考。"
        ),
        prompt_hint=f"Use this generated frame as the strict {request.frame_slot} continuity anchor.",
        mime_type=mimetypes.guess_type(frame_path.name)[0] or "image/png",
        checksum=artifact_sha256,
        metadata={
            "generation_purpose": DEFERRED_GENERATION_PURPOSE,
            "deferred_generation": {
                "schema_version": DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION,
                "request_id": str(request.id),
                "source_task_id": str(request.source_task_id),
                "source_output_role": request.source_output_role,
                "source_output_index": source_output["output_index"],
                "source_artifact_sha256": source_output["artifact_sha256"],
                "source_manifest_sha256": source_output["manifest_sha256"],
                "target_shot_id": str(request.target_shot_id),
                "frame_slot": request.frame_slot,
                "request_fingerprint": request.request_fingerprint,
                "target_input_fingerprint": request.target_input_fingerprint,
            },
        },
    )
    with frame_path.open("rb") as stream:
        asset.file.save(f"deferred-{request.id}-{request.frame_slot}.png", File(stream), save=False)
    file_guard.track(asset.file)
    asset.save()
    copied_path = Path(asset.file.path)
    if sha256_file(copied_path) != asset.checksum:
        asset.file.delete(save=False)
        asset.delete()
        raise RuntimeError("derived frame checksum mismatch")
    return asset


def _deferred_bindings_with_asset(
    *, target_shot: Shot, frame_slot: str, asset: Asset,
) -> dict:
    bindings = deepcopy(target_shot.reference_bindings or {})
    if (
        target_shot.h3_mode == Shot.H3Mode.REF2VA
        and frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME
    ):
        field = "first_frame_reference_asset_id"
    else:
        field = (
            "first_frame_asset_id"
            if frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME
            else "last_frame_asset_id"
        )
    bindings[field] = str(asset.id)
    return bindings


def _deferred_batch_capability_overrides(
    *, source_task: GenerationTask, target_shot: Shot,
) -> dict[str, Any]:
    snapshot = effective_batch_snapshot_for_task(source_task)
    batch_id = str(snapshot.get("batch_id") or "")
    batch_kind = str(snapshot.get("batch_kind") or "")
    if not batch_id or snapshot.get("batch_role") not in {"child", "retry"}:
        return {}
    try:
        source_index = int(snapshot.get("batch_index") or 0)
        batch_size = int(snapshot.get("batch_size") or 0)
    except (TypeError, ValueError):
        return {}
    target_index = source_index + 1
    if source_index < 1 or batch_size < target_index:
        return {}
    return {
        "batch_id": batch_id,
        "batch_kind": batch_kind or None,
        "batch_role": "child",
        "batch_index": target_index,
        "batch_size": batch_size,
        "shot_version_key": str(
            snapshot.get("shot_version_key") or shot_version_key(target_shot)
        ),
        "shot_version_label": str(
            snapshot.get("shot_version_label") or shot_version_label(target_shot)
        ),
        "batch_shot_ids": [
            str(value) for value in snapshot.get("batch_shot_ids", [])
        ],
        "episode_id": str(target_shot.episode_id),
        "episode_number": target_shot.episode.number,
        "episode_title": target_shot.episode.title,
        "shot_ids": [str(target_shot.id)],
        "shot_id": str(target_shot.id),
        "shot_number": shot_version_number(target_shot),
        "shot_title": target_shot.title,
        "include_audio": target_shot.include_audio,
        "requested_duration_ms": target_shot.duration_ms,
        "idempotency_key": str(snapshot.get("idempotency_key") or ""),
        "request_fingerprint": str(snapshot.get("request_fingerprint") or ""),
        "extended_generation_confirmed": bool(
            snapshot.get("extended_generation_confirmed", False)
        ),
    }


def process_deferred_request(request: DeferredGenerationRequest) -> DeferredGenerationRequest:
    """Process one claimed deferred request through derived frame materialization."""

    request = DeferredGenerationRequest.objects.select_related(
        "owner",
        "project__style",
        "source_task__generated_workflow__template",
        "target_shot__episode__project",
        "target_template",
    ).get(pk=request.pk)
    if request.source_task.status == GenerationTask.Status.FAILED:
        return _finalize_deferred(
            request,
            status_value=DeferredGenerationRequest.Status.FAILED,
            error_code="deferred_source_failed",
            error_message="上游视频任务已失败。",
        )
    if request.source_task.status == GenerationTask.Status.CANCELLED:
        return _finalize_deferred(
            request,
            status_value=DeferredGenerationRequest.Status.CANCELLED,
            error_code="deferred_source_cancelled",
            error_message="上游视频任务已取消。",
        )
    frame_path: Path | None = None
    try:
        source_output = resolve_declared_task_output(
            request.source_task,
            request.source_output_role,
        )
        frame_path = extract_last_decodable_frame(
            source_output["path"],
            _source_extraction_slot_for_target(request.frame_slot),
        )
        with _DerivedFrameFileGuard() as file_guard, transaction.atomic():
            # Keep the same high-level lock order used by production builds:
            # Project -> Shot -> Deferred request -> source Task -> Template.
            # The frame extraction above is deliberately outside this section.
            project = SeriesProject.objects.select_for_update().get(pk=request.project_id)
            target_shot = Shot.objects.select_for_update().select_related(
                "episode__project",
            ).get(pk=request.target_shot_id)
            locked = DeferredGenerationRequest.objects.select_for_update().select_related(
                "owner",
                "source_task",
                "target_template",
            ).get(pk=request.pk)
            if locked.status != DeferredGenerationRequest.Status.EXTRACTING:
                return locked
            if locked.project_id != project.id or locked.target_shot_id != target_shot.id:
                raise ValueError("延迟生成请求的项目或目标分镜已损坏。")
            source_task = GenerationTask.objects.select_for_update().get(pk=locked.source_task_id)
            target_template = WorkflowTemplate.objects.select_for_update().get(pk=locked.target_template_id)
            locked.status = DeferredGenerationRequest.Status.MATERIALIZING
            locked.heartbeat_at = timezone.now()
            locked.save(update_fields=("status", "heartbeat_at", "updated_at"))
            if workflow_template_contract_sha256(target_template) != locked.target_template_hash:
                locked.status = DeferredGenerationRequest.Status.STALE
                locked.error_code = "deferred_target_template_changed"
                locked.error_message = "目标模板契约已变化，请重新创建延迟请求。"
                locked.claim_token = ""
                locked.finished_at = timezone.now()
                locked.save(update_fields=(
                    "status", "error_code", "error_message", "claim_token",
                    "finished_at", "updated_at",
                ))
                return locked
            compiled_without_frame = _compile_deferred_target(
                project=project,
                target_shot=target_shot,
            )
            if compiled_without_frame.input_fingerprint != locked.target_input_fingerprint:
                locked.status = DeferredGenerationRequest.Status.STALE
                locked.error_code = "deferred_target_stale"
                locked.error_message = "目标分镜、Bible 或视觉上下文已变化，请重新创建延迟请求。"
                locked.claim_token = ""
                locked.finished_at = timezone.now()
                locked.save(update_fields=(
                    "status", "error_code", "error_message", "claim_token",
                    "finished_at", "updated_at",
                ))
                return locked
            derived_asset = _create_derived_frame_asset(
                request=locked,
                frame_path=frame_path,
                source_output=source_output,
                file_guard=file_guard,
            )
            bindings = _deferred_bindings_with_asset(
                target_shot=target_shot,
                frame_slot=locked.frame_slot,
                asset=derived_asset,
            )
            asset_pool = list(target_shot.reference_assets.all()) + [derived_asset]
            compiled_with_frame = compile_prompt_input(
                project=project,
                shots=[target_shot],
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode=target_shot.h3_mode,
                include_audio=target_shot.include_audio,
                duration_overrides={str(target_shot.id): target_shot.duration_ms},
                single_shot_reference_bindings=bindings,
                single_shot_reference_assets=asset_pool,
            )
            target_workflow_overrides = _deferred_target_workflow_overrides(
                source_task=source_task,
                target_template=target_template,
                selected_overrides=locked.target_workflow_overrides,
            )
            prompt = materialize_compiled_prompt(
                compiled=compiled_with_frame,
                created_by=locked.owner,
                request_options_additions={
                    "generation_purpose": DEFERRED_GENERATION_PURPOSE,
                    "deferred_generation": {
                        "schema_version": DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION,
                        "request_id": str(locked.id),
                        "source_task_id": str(source_task.id),
                        "source_output_role": locked.source_output_role,
                        "source_output_index": source_output["output_index"],
                        "source_artifact_sha256": source_output["artifact_sha256"],
                        "source_manifest_sha256": source_output["manifest_sha256"],
                        "derived_asset_id": str(derived_asset.id),
                        "derived_asset_sha256": derived_asset.checksum,
                        "target_shot_id": str(target_shot.id),
                        "frame_slot": locked.frame_slot,
                        "request_fingerprint": locked.request_fingerprint,
                        "target_input_fingerprint": locked.target_input_fingerprint,
                        "target_workflow_overrides": target_workflow_overrides,
                    },
                },
            )
            workflow = materialize_generated_workflow(
                user=locked.owner,
                template=target_template,
                prompt=prompt,
                overrides=target_workflow_overrides,
            )
            assert_queue_capacity(
                locked.owner,
                additional=1,
                extended_generation_confirmed=locked.extended_generation_confirmed,
            )
            batch_overrides = _deferred_batch_capability_overrides(
                source_task=source_task,
                target_shot=target_shot,
            )
            task = materialize_generation_task(
                user=locked.owner,
                workflow=workflow,
                priority=locked.priority,
                max_attempts=locked.max_attempts,
                capability_snapshot_overrides={
                    "extended_generation_confirmed": bool(
                        locked.extended_generation_confirmed
                    ),
                    **batch_overrides,
                    "deferred_generation": {
                        "schema_version": DEFERRED_GENERATION_REQUEST_SCHEMA_VERSION,
                        "request_id": str(locked.id),
                        "source_task_id": str(source_task.id),
                        "source_output_role": locked.source_output_role,
                        "source_output_index": source_output["output_index"],
                        "source_artifact_sha256": source_output["artifact_sha256"],
                        "source_manifest_sha256": source_output["manifest_sha256"],
                        "derived_asset_id": str(derived_asset.id),
                        "derived_asset_sha256": derived_asset.checksum,
                        "target_shot_id": str(target_shot.id),
                        "frame_slot": locked.frame_slot,
                        "request_fingerprint": locked.request_fingerprint,
                        "target_input_fingerprint": locked.target_input_fingerprint,
                        "target_workflow_overrides": target_workflow_overrides,
                    },
                },
            )
            locked.derived_asset = derived_asset
            locked.materialized_task = task
            locked.resolved_source_output_index = source_output["output_index"]
            locked.source_artifact_sha256 = source_output["artifact_sha256"]
            locked.source_manifest_sha256 = source_output["manifest_sha256"]
            locked.status = DeferredGenerationRequest.Status.MATERIALIZED
            locked.error_code = ""
            locked.error_message = ""
            locked.claim_token = ""
            locked.worker_id = ""
            locked.claimed_at = None
            locked.heartbeat_at = timezone.now()
            locked.finished_at = timezone.now()
            locked.save(update_fields=(
                "derived_asset", "materialized_task", "resolved_source_output_index",
                "source_artifact_sha256", "source_manifest_sha256", "status",
                "error_code", "error_message", "claim_token", "worker_id",
                "claimed_at", "heartbeat_at", "finished_at", "updated_at",
            ))
            return locked
    except ProductionQueueConfirmationRequired as exc:
        return _mark_deferred_retryable_failure(
            request,
            code="production_queue_confirmation_required",
            message=str(exc.detail.get("detail") if isinstance(exc.detail, dict) else exc),
        )
    except (ProvenanceError, FileNotFoundError) as exc:
        return _finalize_deferred(
            request,
            status_value=DeferredGenerationRequest.Status.FAILED,
            error_code="deferred_source_primary_video_missing",
            error_message=str(exc),
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return _mark_deferred_retryable_failure(
            request,
            code="deferred_frame_extract_failed",
            message=str(exc),
        )
    except (PromptBuildError, WorkflowBuildError, ValueError) as exc:
        return _finalize_deferred(
            request,
            status_value=DeferredGenerationRequest.Status.FAILED,
            error_code="deferred_materialization_failed",
            error_message=str(exc),
        )
    finally:
        if frame_path is not None:
            frame_path.unlink(missing_ok=True)
