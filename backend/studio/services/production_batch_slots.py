from __future__ import annotations

from typing import Any

from django.db.models import Q

from studio.models import DeferredGenerationRequest, GenerationTask, Shot
from studio.services.shot_versions import (
    ShotVersionResolutionError,
    resolve_episode_shot_version,
    shot_version_number,
)


ACTIVE_TASK_STATUSES = {
    GenerationTask.Status.QUEUED,
    GenerationTask.Status.RUNNING,
    GenerationTask.Status.CANCEL_REQUESTED,
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _task_sort_key(task: GenerationTask) -> tuple[int, object, object, str]:
    snapshot = effective_batch_snapshot_for_task(task)
    return (
        _safe_int(snapshot.get("batch_index")),
        task.queued_at or task.created_at,
        task.created_at,
        str(task.id),
    )


def _deferred_sort_key(request: DeferredGenerationRequest) -> tuple[object, object, str]:
    return (request.updated_at, request.created_at, str(request.id))


def _direct_batch_snapshot(task: GenerationTask) -> dict[str, Any]:
    snapshot = task.capability_snapshot if isinstance(task.capability_snapshot, dict) else {}
    if not snapshot.get("batch_id"):
        return {}
    if _safe_int(snapshot.get("batch_index")) < 1 or _safe_int(snapshot.get("batch_size")) < 1:
        return {}
    return dict(snapshot)


def effective_batch_snapshot_for_task(task: GenerationTask) -> dict[str, Any]:
    """Return only batch metadata persisted on the task itself.

    Deferred materialization writes the complete child snapshot before the task
    becomes visible.  Missing metadata is therefore an explicit incomplete
    contract, never authority to reconstruct a task from neighbouring rows.
    """

    return _direct_batch_snapshot(task)


def production_batch_task_queryset(
    *,
    owner_id,
    project_id,
    batch_id: str,
):
    """Return all real tasks that belong to one exact production batch id."""

    return (
        GenerationTask.objects.filter(
            owner_id=owner_id,
            project_id=project_id,
        )
        .filter(
            capability_snapshot__batch_id=batch_id,
        )
        .select_related(
            "project",
            "episode",
            "deferred_generation_request__source_task",
            "deferred_generation_request__target_shot__episode",
            "generated_workflow__template",
            "generated_workflow__prompt_artifact__episode",
            "generated_workflow__prompt_artifact__shot__episode",
        )
    )


def production_batch_tasks(
    *,
    owner_id,
    project_id,
    batch_id: str,
) -> list[GenerationTask]:
    tasks = list(
        production_batch_task_queryset(
            owner_id=owner_id,
            project_id=project_id,
            batch_id=batch_id,
        )
    )
    return sorted(tasks, key=_task_sort_key)


def build_production_batch_slots(
    anchor_task: GenerationTask,
    *,
    known_tasks: list[GenerationTask] | None = None,
) -> list[dict[str, Any]]:
    """Expose authoritative slot status for an episode/selection batch.

    This is intentionally separate from scheduling.  A missing real task is
    represented as not_created unless a real active DeferredGenerationRequest
    proves that the slot is waiting on continuity materialization.
    """

    anchor_snapshot = effective_batch_snapshot_for_task(anchor_task)
    batch_id = str(anchor_snapshot.get("batch_id") or "")
    batch_size = _safe_int(anchor_snapshot.get("batch_size"))
    if not batch_id or batch_size <= 1:
        return []

    batch_kind = str(anchor_snapshot.get("batch_kind") or "")
    shot_version_key_value = str(anchor_snapshot.get("shot_version_key") or "")
    shot_version_label_value = str(anchor_snapshot.get("shot_version_label") or "")
    episode_id = str(anchor_snapshot.get("episode_id") or anchor_task.episode_id or "")
    task_queryset = production_batch_task_queryset(
        owner_id=anchor_task.owner_id,
        project_id=anchor_task.project_id,
        batch_id=batch_id,
    )
    tasks = list(known_tasks) if known_tasks is not None else list(task_queryset)
    if known_tasks is not None:
        known_ids = {task.id for task in known_tasks}
        tasks.extend([task for task in task_queryset if task.id not in known_ids])

    latest_task_by_index: dict[int, GenerationTask] = {}
    for task in sorted(tasks, key=_task_sort_key):
        snapshot = effective_batch_snapshot_for_task(task)
        if str(snapshot.get("batch_id") or "") != batch_id:
            continue
        index = _safe_int(snapshot.get("batch_index"))
        if 1 <= index <= batch_size:
            latest_task_by_index[index] = task

    shots_by_slot: dict[int, Shot] = {}
    frozen_shot_ids = anchor_snapshot.get("batch_shot_ids")
    frozen_version_key = str(anchor_snapshot.get("shot_version_key") or "")
    has_frozen_manifest = isinstance(frozen_shot_ids, list) and bool(frozen_shot_ids)
    if has_frozen_manifest:
        ordered_ids = [str(value) for value in frozen_shot_ids]
        if len(ordered_ids) == batch_size and len(set(ordered_ids)) == batch_size:
            frozen_rows = list(
                Shot.objects.filter(
                    id__in=ordered_ids,
                    episode_id=episode_id,
                ).select_related("episode")
            )
            frozen_by_id = {str(shot.id): shot for shot in frozen_rows}
            if len(frozen_by_id) == batch_size:
                shots_by_slot.update({
                    index + 1: frozen_by_id[shot_id]
                    for index, shot_id in enumerate(ordered_ids)
                })
    elif frozen_version_key and anchor_task.episode_id:
        try:
            resolved = resolve_episode_shot_version(
                anchor_task.episode,
                frozen_version_key,
            )
        except ShotVersionResolutionError:
            resolved = None
        if resolved is not None and len(resolved.shots) == batch_size:
            shots_by_slot.update({
                index + 1: shot for index, shot in enumerate(resolved.shots)
            })
    # Historical batches without a frozen manifest must not be reconstructed
    # from the episode's current shots.  Only persisted task/deferred evidence
    # below may assign a historical slot to a Shot.

    for task in latest_task_by_index.values():
        snapshot = effective_batch_snapshot_for_task(task)
        index = _safe_int(snapshot.get("batch_index"))
        shot = task.generated_workflow.prompt_artifact.shot
        if index and shot:
            shots_by_slot[index] = shot

    deferred_by_slot: dict[int, DeferredGenerationRequest] = {}
    deferred_queryset = (
        DeferredGenerationRequest.objects.filter(
            project_id=anchor_task.project_id,
            frame_slot=DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
        )
        .filter(
            Q(source_task__capability_snapshot__batch_id=batch_id)
            | Q(materialized_task__capability_snapshot__batch_id=batch_id)
        )
        .select_related("source_task", "target_shot__episode", "materialized_task")
    )
    for deferred in sorted(deferred_queryset, key=_deferred_sort_key):
        source_snapshot = effective_batch_snapshot_for_task(deferred.source_task)
        if str(source_snapshot.get("batch_id") or "") != batch_id:
            continue
        slot = _safe_int(source_snapshot.get("batch_index")) + 1
        if not 1 <= slot <= batch_size:
            continue
        deferred_by_slot[slot] = deferred
        shots_by_slot.setdefault(slot, deferred.target_shot)

    slots: list[dict[str, Any]] = []
    previous_slot_status = ""
    previous_deferred_request_id: str | None = None
    previous_task_status = ""
    for index in range(1, batch_size + 1):
        task = latest_task_by_index.get(index)
        shot = shots_by_slot.get(index)
        if task:
            snapshot = effective_batch_snapshot_for_task(task)
            slot_status = "cancelled" if task.status == GenerationTask.Status.CANCELLED else "materialized"
            message = (
                "该槽位已有真实 GenerationTask。"
                if slot_status == "materialized"
                else "该槽位任务已取消，可通过重试恢复后续链路。"
            )
            task_shot = task.generated_workflow.prompt_artifact.shot
            shot = shot or task_shot
            slot = {
                "batch_id": batch_id,
                "batch_kind": batch_kind or None,
                "shot_version_key": str(
                    snapshot.get("shot_version_key") or shot_version_key_value
                ),
                "shot_version_label": str(
                    snapshot.get("shot_version_label") or shot_version_label_value
                ),
                "batch_index": index,
                "batch_size": batch_size,
                "slot_status": slot_status,
                "message": message,
                "task_id": str(task.id),
                "task_status": task.status,
                "workflow_id": str(task.generated_workflow_id),
                "deferred_generation_request_id": None,
                "deferred_generation_status": None,
                "deferred_upstream_request_id": None,
                "episode_id": snapshot.get("episode_id") or (str(shot.episode_id) if shot else episode_id or None),
                "episode_number": snapshot.get("episode_number") or (shot.episode.number if shot else None),
                "episode_title": snapshot.get("episode_title") or (shot.episode.title if shot else ""),
                "shot_id": snapshot.get("shot_id") or (str(shot.id) if shot else None),
                "shot_number": snapshot.get("shot_number") or (
                    shot_version_number(shot) if shot else None
                ),
                "shot_title": snapshot.get("shot_title") or (shot.title if shot else ""),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        else:
            deferred = deferred_by_slot.get(index)
            if deferred and deferred.status in DeferredGenerationRequest.ACTIVE_STATUSES:
                slot_status = "waiting_deferred"
                previous_deferred_request_id = str(deferred.id)
                message = "等待 deferred 物化当前槽位的正式 GenerationTask。"
                workflow_mode = "deferred_first_frame_continuity"
            elif deferred and deferred.status == DeferredGenerationRequest.Status.CANCELLED:
                slot_status = (
                    "cancelled_by_upstream"
                    if deferred.source_task.status == GenerationTask.Status.CANCELLED
                    or deferred.error_code in {
                        "deferred_source_cancelled",
                        "deferred_source_cancelled_by_user",
                    }
                    else "cancelled"
                )
                previous_deferred_request_id = str(deferred.id)
                message = (
                    "上游任务已取消，后续连续生成已自动取消；重试上游任务后可恢复链路。"
                    if slot_status == "cancelled_by_upstream"
                    else "该槽位 deferred 已取消，可重新发起或重试上游任务恢复链路。"
                )
                workflow_mode = "deferred_first_frame_continuity"
            elif previous_slot_status in {"cancelled", "cancelled_by_upstream"} or previous_task_status == GenerationTask.Status.CANCELLED:
                slot_status = "cancelled_by_upstream"
                message = "上游任务已取消，后续链路已中断；重试上游任务后可恢复链路。"
                workflow_mode = "waiting_for_deferred_first_frame_chain"
            else:
                slot_status = "not_created"
                message = "没有 active deferred，也没有真实 GenerationTask；后续链路已中断，等待重试恢复。"
                workflow_mode = "not_created"
            slot = {
                "batch_id": batch_id,
                "batch_kind": batch_kind or None,
                "shot_version_key": shot_version_key_value,
                "shot_version_label": shot_version_label_value,
                "batch_index": index,
                "batch_size": batch_size,
                "slot_status": slot_status,
                "message": message,
                "workflow_mode": workflow_mode,
                "task_id": None,
                "task_status": None,
                "workflow_id": None,
                "deferred_generation_request_id": str(deferred.id) if deferred else None,
                "deferred_generation_status": deferred.status if deferred else None,
                "deferred_upstream_request_id": previous_deferred_request_id,
                "episode_id": str(shot.episode_id) if shot else episode_id or None,
                "episode_number": shot.episode.number if shot else None,
                "episode_title": shot.episode.title if shot else "",
                "shot_id": str(shot.id) if shot else None,
                "shot_number": shot_version_number(shot) if shot else None,
                "shot_title": shot.title if shot else "",
                "created_at": deferred.created_at if deferred else None,
                "updated_at": deferred.updated_at if deferred else None,
            }
        slots.append(slot)
        previous_slot_status = str(slot.get("slot_status") or "")
        previous_task_status = str(slot.get("task_status") or "")
        if slot.get("deferred_generation_request_id"):
            previous_deferred_request_id = str(slot["deferred_generation_request_id"])
    return slots
