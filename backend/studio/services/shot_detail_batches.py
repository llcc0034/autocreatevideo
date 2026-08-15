"""Durable episode-wide orchestration for per-shot high-detail drafts.

The batch path deliberately reuses :mod:`shot_detail_drafts` for every model
call and every application.  HTTP requests only freeze source inputs and
enqueue work; the existing generation worker claims one item at a time.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from studio.models import (
    Episode,
    SeriesProject,
    Shot,
    ShotDetailDraft,
    ShotDetailDraftBatch,
    ShotDetailDraftBatchItem,
)
from studio.services.creative_drafts import (
    CreativeContextConflict,
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRuntimeUnavailable,
    get_creative_capability,
    normalise_creative_provider,
)
from studio.services.shot_detail_drafts import (
    commit_prepared_shot_detail_application,
    create_failed_shot_detail_draft,
    create_shot_detail_draft,
    finalize_prepared_shot_detail_application,
    prepare_locked_shot_detail_application,
    resolve_shot_detail_policy,
    shot_detail_context_hash,
    shot_detail_context_snapshot,
    shot_detail_policy,
)


TERMINAL_ITEM_STATUSES = {
    ShotDetailDraftBatchItem.Status.SUCCEEDED,
    ShotDetailDraftBatchItem.Status.FAILED,
    ShotDetailDraftBatchItem.Status.STALE,
    ShotDetailDraftBatchItem.Status.SKIPPED,
}


class ShotDetailBatchError(CreativeDraftError):
    pass


class ShotDetailBatchConflict(ShotDetailBatchError):
    pass


class ShotDetailBatchConfirmationRequired(ShotDetailBatchConflict):
    def __init__(self, message: str, *, details: dict[str, Any]):
        super().__init__(message, code="shot_detail_batch_confirmation_required")
        self.details = details


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_label(provider: str) -> str:
    return "DeepSeek API" if provider == "deepseek_api" else "本地 Qwen"


def _provider_queue_available(provider: str) -> bool:
    capabilities = get_creative_capability().get("providers", [])
    capability = next((
        item for item in capabilities
        if isinstance(item, dict) and item.get("id") == provider
    ), None)
    if capability is None or not capability.get("enabled"):
        return False
    if provider == "local_qwen":
        # A runtime probe can legitimately lose the shared local lock while a
        # Comfy/Qwen operation is active.  That is queue pressure, not missing
        # configuration; the worker will preserve and retry the item.
        temporarily_busy = (
            capability.get("runtime_probe_error_code") == "shared_resource_busy"
        )
        return bool(
            capability.get("model_verified")
            and (capability.get("runtime_ready") or temporarily_busy)
        )
    return bool(capability.get("configured"))


def _batch_request_matches(
    batch: ShotDetailDraftBatch, *, user, project: SeriesProject,
    episode: Episode, provider: str, variation_note: str, config_hash: str,
) -> bool:
    if not (
        batch.created_by_id == user.pk
        and batch.project_id == project.pk
        and batch.episode_id == episode.pk
    ):
        return False
    if batch.config_hash == config_hash:
        return True
    # Batches created before the idempotency contract was corrected included
    # the server policy in their fingerprint.  Accept their own frozen policy
    # so a later environment change cannot rotate a client's request key.
    legacy_hash = _json_hash({
        "provider": provider,
        "variation_note": variation_note,
        "policy": batch.policy_snapshot or {},
    })
    return batch.config_hash == legacy_hash


def create_shot_detail_batch(
    *, project: SeriesProject, episode: Episode, user, request_id: uuid.UUID,
    provider: str, variation_note: str = "", confirmation_acknowledged: bool = False,
) -> tuple[ShotDetailDraftBatch, bool]:
    """Freeze every shot and enqueue the video items.

    ``request_id`` is a globally unique idempotency key.  Reusing it for the
    same owner/project/episode and exact options returns the original batch;
    reusing it for another scope never leaks that batch.
    """
    provider = normalise_creative_provider(provider)
    policy = shot_detail_policy(provider)
    request_id = uuid.UUID(str(request_id))
    # Idempotency is defined exclusively by the client request.  Runtime
    # policy is frozen separately and may legitimately change after creation.
    config_hash = _json_hash({
        "provider": provider,
        "variation_note": variation_note,
    })

    existing = ShotDetailDraftBatch.objects.filter(request_id=request_id).first()
    if existing:
        if _batch_request_matches(
            existing, user=user, project=project, episode=episode,
            provider=provider, variation_note=variation_note, config_hash=config_hash,
        ):
            return existing, False
        raise ShotDetailBatchConflict(
            "request_id 已用于其他整集扩写请求",
            code="shot_detail_batch_request_id_conflict",
        )

    if episode.project_id != project.pk:
        raise ShotDetailBatchConflict(
            "剧集不属于当前项目", code="shot_detail_batch_scope_invalid",
        )
    if not _provider_queue_available(provider):
        raise CreativeRuntimeUnavailable(
            f"{_provider_label(provider)} 尚未配置就绪",
            code="shot_detail_provider_not_ready",
        )

    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=project.pk,
        )
        locked_episode = Episode.objects.select_for_update().get(
            pk=episode.pk, project=locked_project,
        )
        existing = ShotDetailDraftBatch.objects.select_for_update().filter(
            request_id=request_id,
        ).first()
        if existing:
            if _batch_request_matches(
                existing, user=user, project=locked_project, episode=locked_episode,
                provider=provider, variation_note=variation_note, config_hash=config_hash,
            ):
                return existing, False
            raise ShotDetailBatchConflict(
                "request_id 已用于其他整集扩写请求",
                code="shot_detail_batch_request_id_conflict",
            )
        if ShotDetailDraftBatch.objects.filter(
            episode=locked_episode,
            generation_status__in=(
                ShotDetailDraftBatch.GenerationStatus.QUEUED,
                ShotDetailDraftBatch.GenerationStatus.RUNNING,
            ),
        ).exists():
            raise ShotDetailBatchConflict(
                "当前剧集已有整集扩写任务正在处理",
                code="shot_detail_batch_active_conflict",
            )

        shots = list(
            Shot.objects.filter(episode=locked_episode)
            .select_related("scene")
            .prefetch_related("characters", "props")
            .order_by("number", "id")
        )
        video_count = sum(shot.media_type == Shot.MediaType.VIDEO for shot in shots)
        recommended = int(policy["recommended_max_shots"])
        if video_count > recommended and not confirmation_acknowledged:
            raise ShotDetailBatchConfirmationRequired(
                "镜头数超过所选模型的推荐单批数量，请确认预算与等待时间后继续",
                details={
                    "provider": provider,
                    "video_shot_count": video_count,
                    "total_shot_count": len(shots),
                    "recommended_max_shots": recommended,
                    "business_volume_limited": False,
                },
            )

        frozen: list[dict[str, Any]] = []
        manifest: list[dict[str, Any]] = []
        for ordinal, shot in enumerate(shots, 1):
            if shot.media_type == Shot.MediaType.VIDEO:
                snapshot = shot_detail_context_snapshot(
                    locked_project, locked_episode, shot, policy=policy,
                )
                context_hash = _json_hash(snapshot)
                item_status = ShotDetailDraftBatchItem.Status.PENDING
            else:
                snapshot = {
                    "shot": {
                        "id": str(shot.pk), "number": shot.number,
                        "media_type": shot.media_type,
                        "detailed_description": shot.detailed_description,
                    },
                }
                context_hash = _json_hash(snapshot)
                item_status = ShotDetailDraftBatchItem.Status.SKIPPED
            frozen.append({
                "shot": shot,
                "ordinal": ordinal,
                "snapshot": snapshot,
                "context_hash": context_hash,
                "status": item_status,
            })
            manifest.append({
                "shot_id": str(shot.pk), "ordinal": ordinal,
                "context_hash": context_hash, "status": item_status,
            })

        now = timezone.now()
        try:
            batch = ShotDetailDraftBatch.objects.create(
                project=locked_project,
                episode=locked_episode,
                created_by=user,
                request_id=request_id,
                provider=provider,
                variation_note=variation_note,
                source_manifest_hash=_json_hash(manifest),
                policy_snapshot=policy,
                config_hash=config_hash,
                generation_status=(
                    ShotDetailDraftBatch.GenerationStatus.QUEUED
                    if video_count else ShotDetailDraftBatch.GenerationStatus.COMPLETED
                ),
                total_count=len(frozen),
                pending_count=video_count,
                skipped_count=len(frozen) - video_count,
                finished_at=None if video_count else now,
            )
        except IntegrityError as exc:
            raise ShotDetailBatchConflict(
                "当前剧集已有整集扩写任务正在处理",
                code="shot_detail_batch_active_conflict",
            ) from exc
        ShotDetailDraftBatchItem.objects.bulk_create([
            ShotDetailDraftBatchItem(
                batch=batch,
                shot=item["shot"],
                ordinal=item["ordinal"],
                source_context_hash=item["context_hash"],
                before_snapshot=item["snapshot"],
                status=item["status"],
                max_attempts=int(policy["max_attempts"]),
                next_attempt_at=now,
                finished_at=(
                    now if item["status"] == ShotDetailDraftBatchItem.Status.SKIPPED
                    else None
                ),
                error_code=(
                    "shot_detail_non_video_skipped"
                    if item["status"] == ShotDetailDraftBatchItem.Status.SKIPPED else ""
                ),
                error_message=(
                    "非视频镜头不进入高细节扩写"
                    if item["status"] == ShotDetailDraftBatchItem.Status.SKIPPED else ""
                ),
            )
            for item in frozen
        ])
    return batch, True


def _refresh_locked_shot_detail_batch(
    batch: ShotDetailDraftBatch,
) -> ShotDetailDraftBatch:
    """Recompute one already-locked batch inside the caller's transaction."""
    counts = {
        row["status"]: row["count"]
        for row in batch.items.values("status").annotate(count=Count("id"))
    }
    pending = counts.get(ShotDetailDraftBatchItem.Status.PENDING, 0)
    running = counts.get(ShotDetailDraftBatchItem.Status.RUNNING, 0)
    succeeded = counts.get(ShotDetailDraftBatchItem.Status.SUCCEEDED, 0)
    failed = counts.get(ShotDetailDraftBatchItem.Status.FAILED, 0)
    stale = counts.get(ShotDetailDraftBatchItem.Status.STALE, 0)
    skipped = counts.get(ShotDetailDraftBatchItem.Status.SKIPPED, 0)
    remaining = pending + running
    batch.pending_count = remaining
    batch.succeeded_count = succeeded
    batch.failed_count = failed
    batch.stale_count = stale
    batch.skipped_count = skipped
    if remaining:
        batch.generation_status = (
            ShotDetailDraftBatch.GenerationStatus.RUNNING
            if batch.started_at else ShotDetailDraftBatch.GenerationStatus.QUEUED
        )
        batch.finished_at = None
    else:
        if failed or stale:
            batch.generation_status = (
                ShotDetailDraftBatch.GenerationStatus.COMPLETED_WITH_ERRORS
                if succeeded else ShotDetailDraftBatch.GenerationStatus.FAILED
            )
        else:
            batch.generation_status = ShotDetailDraftBatch.GenerationStatus.COMPLETED
        batch.finished_at = timezone.now()
    batch.revision += 1
    batch.save(update_fields=(
        "pending_count", "succeeded_count", "failed_count", "stale_count",
        "skipped_count", "generation_status", "finished_at", "revision", "updated_at",
    ))
    return batch


def refresh_shot_detail_batch(batch_id) -> ShotDetailDraftBatch:
    with transaction.atomic():
        batch = ShotDetailDraftBatch.objects.select_for_update().get(pk=batch_id)
        return _refresh_locked_shot_detail_batch(batch)


def claim_next_shot_detail_item(worker_id: str) -> ShotDetailDraftBatchItem | None:
    """Claim exactly one due item; serial processing is intentional."""
    now = timezone.now()
    with transaction.atomic():
        due_items = ShotDetailDraftBatchItem.objects.filter(
            batch_id=OuterRef("pk"),
            status=ShotDetailDraftBatchItem.Status.PENDING,
            next_attempt_at__lte=now,
        )
        batch = (
            ShotDetailDraftBatch.objects.select_for_update(skip_locked=True)
            .filter(
                generation_status__in=(
                    ShotDetailDraftBatch.GenerationStatus.QUEUED,
                    ShotDetailDraftBatch.GenerationStatus.RUNNING,
                ),
            )
            .annotate(has_due_item=Exists(due_items))
            .filter(has_due_item=True)
            .order_by("created_at", "id")
            .first()
        )
        if batch is None:
            return None
        item = (
            ShotDetailDraftBatchItem.objects.select_for_update(skip_locked=True)
            .select_related("batch", "batch__project", "batch__episode", "batch__created_by", "shot")
            .filter(
                batch=batch,
                status=ShotDetailDraftBatchItem.Status.PENDING,
                next_attempt_at__lte=now,
            )
            .order_by("ordinal", "id")
            .first()
        )
        if item is None:
            return None
        item.status = ShotDetailDraftBatchItem.Status.RUNNING
        item.attempts += 1
        item.worker_id = str(worker_id)[:160]
        item.claim_token = uuid.uuid4()
        item.claimed_at = now
        item.heartbeat_at = now
        item.finished_at = None
        item.error_code = ""
        item.error_message = ""
        item.save(update_fields=(
            "status", "attempts", "worker_id", "claim_token", "claimed_at",
            "heartbeat_at", "finished_at", "error_code", "error_message", "updated_at",
        ))
        if batch.started_at is None:
            batch.started_at = now
        batch.generation_status = ShotDetailDraftBatch.GenerationStatus.RUNNING
        batch.revision += 1
        batch.save(update_fields=(
            "started_at", "generation_status", "revision", "updated_at",
        ))
        return item


def _finish_item(
    item_id, *, status: str, draft: ShotDetailDraft | None = None,
    error_code: str = "", error_message: str = "", retry: bool = False,
    restore_attempt: bool = False, expected_claim_token=None,
) -> ShotDetailDraftBatchItem:
    with transaction.atomic():
        batch_id = ShotDetailDraftBatchItem.objects.values_list(
            "batch_id", flat=True,
        ).get(pk=item_id)
        batch = ShotDetailDraftBatch.objects.select_for_update().get(pk=batch_id)
        item = ShotDetailDraftBatchItem.objects.select_for_update().get(
            pk=item_id, batch_id=batch_id,
        )
        if expected_claim_token is not None and (
            item.status != ShotDetailDraftBatchItem.Status.RUNNING
            or item.claim_token != expected_claim_token
        ):
            return item
        if draft is not None:
            item.draft = draft
        if restore_attempt:
            item.attempts = max(0, item.attempts - 1)
        item.error_code = str(error_code or "")[:80]
        item.error_message = str(error_message or "")[:10_000]
        item.worker_id = ""
        item.claim_token = None
        item.claimed_at = None
        item.heartbeat_at = None
        if retry:
            retry_seconds = float((batch.policy_snapshot or {}).get("retry_seconds", 5))
            item.status = ShotDetailDraftBatchItem.Status.PENDING
            item.next_attempt_at = timezone.now() + timedelta(seconds=max(0, retry_seconds))
            item.finished_at = None
        else:
            item.status = status
            item.finished_at = timezone.now()
        item.save()
        _refresh_locked_shot_detail_batch(batch)
        return item


def process_shot_detail_item(item: ShotDetailDraftBatchItem) -> ShotDetailDraftBatchItem:
    """Run one item through the existing single-shot generation service."""
    item = ShotDetailDraftBatchItem.objects.select_related(
        "batch", "batch__project", "batch__episode", "batch__created_by", "shot",
    ).get(pk=item.pk)
    if item.status != ShotDetailDraftBatchItem.Status.RUNNING:
        return item
    batch = item.batch
    claim_token = item.claim_token
    frozen_policy = resolve_shot_detail_policy(
        batch.provider, dict(batch.policy_snapshot or {}),
    )

    def finish(**kwargs):
        return _finish_item(
            item.pk, expected_claim_token=claim_token, **kwargs,
        )

    try:
        current_hash = shot_detail_context_hash(
            batch.project, batch.episode, item.shot, policy=frozen_policy,
        )
        if current_hash != item.source_context_hash:
            return finish(
                status=ShotDetailDraftBatchItem.Status.STALE,
                error_code="shot_detail_source_stale",
                error_message="镜头、相邻连续性或绑定 Bible 已在入队后变化",
            )
        draft = create_shot_detail_draft(
            project=batch.project,
            episode=batch.episode,
            shot=item.shot,
            user=batch.created_by,
            variation_note=batch.variation_note,
            provider=batch.provider,
            expected_context_hash=item.source_context_hash,
            request_id=f"shot-detail-batch-{item.pk}-attempt-{item.attempts}",
            batch_item=item,
            policy_override=frozen_policy,
        )
        final_status = (
            ShotDetailDraftBatchItem.Status.SUCCEEDED
            if draft.status == ShotDetailDraft.Status.DRAFT
            else ShotDetailDraftBatchItem.Status.FAILED
        )
        return finish(
            status=final_status, draft=draft,
            error_code=("" if final_status == ShotDetailDraftBatchItem.Status.SUCCEEDED else "shot_detail_generation_failed"),
            error_message=("" if final_status == ShotDetailDraftBatchItem.Status.SUCCEEDED else draft.error_message),
        )
    except CreativeResourceBusy as exc:
        return finish(
            status=ShotDetailDraftBatchItem.Status.PENDING,
            error_code=exc.code or "shared_resource_busy",
            error_message="共享生成资源正忙，任务已保留并等待重试",
            retry=True, restore_attempt=True,
        )
    except CreativeContextConflict as exc:
        return finish(
            status=ShotDetailDraftBatchItem.Status.STALE,
            error_code=exc.code or "shot_detail_source_stale",
            error_message="镜头、相邻连续性或绑定 Bible 已在生成期间变化",
        )
    except CreativeDraftError as exc:
        try:
            draft = create_failed_shot_detail_draft(
                project=batch.project,
                episode=batch.episode,
                shot=item.shot,
                user=batch.created_by,
                variation_note=batch.variation_note,
                error=str(exc),
                error_code=exc.code,
                runtime_metadata=exc.runtime_metadata,
                attempt_context_snapshot=exc.attempt_context_snapshot,
                attempt_context_hash=exc.attempt_context_hash,
                source_drift_during_attempt=exc.source_drift_during_attempt,
                provider=batch.provider,
                batch_item=item,
                policy_override=frozen_policy,
            )
        except CreativeContextConflict:
            return finish(
                status=ShotDetailDraftBatchItem.Status.STALE,
                error_code="shot_detail_source_stale",
                error_message="保存失败记录前镜头上下文已变化",
            )
        except Exception:
            retry = item.attempts < item.max_attempts
            return finish(
                status=ShotDetailDraftBatchItem.Status.FAILED,
                error_code="shot_detail_failure_record_failed",
                error_message=(
                    f"{_provider_label(batch.provider)} 扩写失败且未能保存候选记录"
                    + ("，任务将自动重试" if retry else "，请查看服务日志")
                ),
                retry=retry,
            )
        if draft.status == ShotDetailDraft.Status.DRAFT:
            return finish(
                status=ShotDetailDraftBatchItem.Status.SUCCEEDED, draft=draft,
                error_code=exc.code,
                error_message=f"{_provider_label(batch.provider)} 输出已转为可人工修订候选",
            )
        retry = item.attempts < item.max_attempts
        return finish(
            status=ShotDetailDraftBatchItem.Status.FAILED,
            draft=draft,
            error_code=exc.code,
            error_message=(
                f"{_provider_label(batch.provider)} 本次扩写失败"
                + ("，任务将自动重试" if retry else "，已保留失败记录")
            ),
            retry=retry,
        )
    except Exception:
        retry = item.attempts < item.max_attempts
        return finish(
            status=ShotDetailDraftBatchItem.Status.FAILED,
            error_code="shot_detail_worker_failed",
            error_message=(
                f"{_provider_label(batch.provider)} 扩写任务发生内部错误"
                + ("，任务将自动重试" if retry else "，请查看服务日志")
            ),
            retry=retry,
        )


def recover_stale_shot_detail_items(stale_minutes: int | None = None) -> int:
    """Recover abandoned claims without discarding a committed draft."""
    if stale_minutes is None:
        stale_minutes = int(shot_detail_policy()["stale_minutes"])
    cutoff = timezone.now() - timedelta(minutes=max(1, int(stale_minutes)))
    item_ids = list(
        ShotDetailDraftBatchItem.objects.filter(
            status=ShotDetailDraftBatchItem.Status.RUNNING,
        ).filter(Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True)).values_list(
            "id", flat=True,
        )
    )
    recovered = 0
    for item_id in item_ids:
        with transaction.atomic():
            batch_id = ShotDetailDraftBatchItem.objects.values_list(
                "batch_id", flat=True,
            ).filter(pk=item_id).first()
            if batch_id is None:
                continue
            batch = ShotDetailDraftBatch.objects.select_for_update().get(pk=batch_id)
            item = ShotDetailDraftBatchItem.objects.select_for_update().select_related(
                "draft",
            ).get(pk=item_id, batch_id=batch_id)
            if item.status != ShotDetailDraftBatchItem.Status.RUNNING:
                continue
            if item.draft_id and item.draft.status in {
                ShotDetailDraft.Status.DRAFT,
                ShotDetailDraft.Status.CONFIRMED,
                ShotDetailDraft.Status.APPLIED,
            }:
                item.status = ShotDetailDraftBatchItem.Status.SUCCEEDED
                item.error_code = ""
                item.error_message = ""
                item.finished_at = timezone.now()
            elif item.attempts < item.max_attempts:
                item.status = ShotDetailDraftBatchItem.Status.PENDING
                item.next_attempt_at = timezone.now()
                item.finished_at = None
                item.error_code = "shot_detail_worker_claim_recovered"
                item.error_message = "工作进程中断，任务已恢复排队"
            else:
                item.status = ShotDetailDraftBatchItem.Status.FAILED
                item.error_code = "shot_detail_worker_claim_expired"
                item.error_message = "工作进程中断且已达到重试次数"
                item.finished_at = timezone.now()
            item.worker_id = ""
            item.claim_token = None
            item.claimed_at = None
            item.heartbeat_at = None
            item.save()
            _refresh_locked_shot_detail_batch(batch)
        recovered += 1
    return recovered


def apply_shot_detail_batch(
    *, batch: ShotDetailDraftBatch, user, expected_batch_revision: int,
    items: list[dict[str, Any]], language_mode: str,
    review_acknowledged: bool = False,
) -> tuple[ShotDetailDraftBatch, list[Any]]:
    """Atomically confirm/apply every generated video item in one episode.

    Generation may finish with per-shot failures, but episode application has
    no partial-write mode: every non-skipped item and every submitted revision
    must pass preflight before the first formal ``Shot`` is changed.
    """
    submitted: dict[str, dict[str, Any]] = {}
    for entry in items:
        item_id = str(entry["item_id"])
        if item_id in submitted:
            raise ShotDetailBatchConflict(
                "整集应用项目不能重复", code="shot_detail_batch_apply_items_invalid",
            )
        submitted[item_id] = entry

    batch_identity = ShotDetailDraftBatch.objects.only(
        "project_id", "episode_id",
    ).get(pk=batch.pk)
    with transaction.atomic():
        project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=batch_identity.project_id,
        )
        episode = Episode.objects.select_for_update().get(
            pk=batch_identity.episode_id, project=project,
        )
        locked_batch = ShotDetailDraftBatch.objects.select_for_update().get(
            pk=batch.pk,
            project=project,
            episode=episode,
        )
        if locked_batch.created_by_id != user.pk and not getattr(user, "is_staff", False):
            raise ShotDetailBatchConflict(
                "无权应用该整集扩写批次", code="shot_detail_batch_forbidden",
            )
        if locked_batch.revision != expected_batch_revision:
            raise ShotDetailBatchConflict(
                "整集扩写批次已变化，请刷新对比后再应用",
                code="shot_detail_batch_revision_conflict",
            )
        if locked_batch.apply_status == ShotDetailDraftBatch.ApplyStatus.COMPLETE:
            raise ShotDetailBatchConflict(
                "该整集扩写批次已应用",
                code="shot_detail_batch_already_applied",
            )
        batch_items = list(
            ShotDetailDraftBatchItem.objects.select_for_update()
            .select_related("draft", "shot")
            .filter(batch=locked_batch)
            .order_by("ordinal")
        )
        eligible = [
            item for item in batch_items
            if item.status != ShotDetailDraftBatchItem.Status.SKIPPED
        ]
        invalid = [
            item for item in eligible
            if item.status != ShotDetailDraftBatchItem.Status.SUCCEEDED
            or item.draft_id is None
        ]
        if invalid:
            raise ShotDetailBatchConflict(
                "整集扩写仍有未完成、失败或已陈旧的镜头；本次未写入任何镜头",
                code="shot_detail_batch_not_fully_generated",
            )
        expected_ids = {str(item.pk) for item in eligible}
        if set(submitted) != expected_ids:
            raise ShotDetailBatchConflict(
                "整集应用必须包含本批次全部视频镜头",
                code="shot_detail_batch_apply_items_incomplete",
            )

        shot_ids = [item.shot_id for item in eligible]
        shots = {
            shot.pk: shot
            for shot in Shot.objects.select_for_update().select_related("scene").prefetch_related(
                "characters", "props",
            ).filter(episode=episode, pk__in=shot_ids).order_by("number", "id")
        }
        draft_ids = [item.draft_id for item in eligible]
        drafts = {
            draft.pk: draft
            for draft in ShotDetailDraft.objects.select_for_update().filter(
                pk__in=draft_ids, episode=episode,
            )
        }
        prepared = []
        for item in eligible:
            entry = submitted[str(item.pk)]
            draft = drafts.get(item.draft_id)
            shot = shots.get(item.shot_id)
            if (
                draft is None or shot is None
                or str(entry["draft_id"]) != str(item.draft_id)
            ):
                raise ShotDetailBatchConflict(
                    "整集应用候选与当前批次不一致",
                    code="shot_detail_batch_apply_candidate_conflict",
                )
            prepared.append(prepare_locked_shot_detail_application(
                draft=draft,
                project=project,
                episode=episode,
                shot=shot,
                user=user,
                language_mode=language_mode,
                expected_revision=int(entry["expected_revision"]),
                review_acknowledged=review_acknowledged,
                allow_draft=True,
            ))

        # No formal write occurs above this line.  The outer atomic block rolls
        # back every shot, translation and audit change if any later write fails.
        apply_run_id = uuid.uuid4()
        for plan in prepared:
            commit_prepared_shot_detail_application(plan)
        for plan in prepared:
            finalize_prepared_shot_detail_application(plan)
        now = timezone.now()
        ShotDetailDraftBatchItem.objects.filter(
            pk__in=[item.pk for item in eligible],
        ).update(apply_run_id=apply_run_id, applied_at=now)
        locked_batch.applied_count = len(eligible)
        locked_batch.apply_status = ShotDetailDraftBatch.ApplyStatus.COMPLETE
        locked_batch.revision += 1
        locked_batch.save(update_fields=(
            "applied_count", "apply_status", "revision", "updated_at",
        ))
    return locked_batch, prepared
