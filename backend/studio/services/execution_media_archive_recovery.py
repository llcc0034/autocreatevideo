"""Explicit local archival retry of a retained successful production receipt.

Reuses the original queue and validators, never creates a model attempt.
"""
import hashlib

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ExecutionTextResponseReview, GenerationAttempt, GeneratedArtifact
from studio.services.execution_production_workflow_queue import (
    _locked_task_attempt, read_prepared_generation_task, read_prepared_generation_history,
    update_prepared_generation_lifecycle,
)
from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event


def _candidate(preparation):
    rows = list(GenerationAttempt.objects.filter(preparation=preparation, role="primary",
        retry_successor__isnull=True).select_related("generation_task", "stage_run__production_run")[:2])
    return rows[0] if len(rows) == 1 and rows[0].generation_task_id else None


def _reason(preparation, attempt, task):
    if (preparation.purpose != "production" or preparation.status != "dispatched"
            or attempt.status != "failed" or task.status != "failed"
            or task.submission_state != "terminal_succeeded"):
        return "仅支持远端已成功、本地归档失败的原生产任务。"
    if (attempt.ignore_late_result or attempt.cancel_requested_at is not None
            or attempt.stage_run.status not in {"running", "blocked", "awaiting_approval"}
            or attempt.stage_run.production_run.status not in {"running", "blocked", "awaiting_input", "awaiting_approval"}):
        return "原任务已取消或运行不再接收产物，不能恢复。"
    if GeneratedArtifact.objects.filter(generation_attempt=attempt).exists():
        return "此调用已有正式产物，请核对记录，不能重复归档。"
    return ""


def describe_media_archive_recovery(preparation):
    attempt = _candidate(preparation)
    if attempt is None or attempt.generation_task.submission_state != "terminal_succeeded" or attempt.status != "failed":
        return None
    task = attempt.generation_task
    row = ExecutionTextResponseReview.objects.filter(attempt=attempt).first()
    reason = _reason(preparation, attempt, task)
    if row is None:
        reason = "未保存原成功回执，不能重新收集或生成来替代。"
    return {"task_id": str(task.pk), "attempt_id": str(attempt.pk), "task_updated_at": task.updated_at,
        "receipt_sha256": row.original_sha256 if row else "", "can_recover": not reason, "reason": reason}


def recover_media_archive(*, preparation, actor, expected_preparation_revision, task_id, attempt_id,
                          task_updated_at, receipt_sha256):
    attempt = _candidate(preparation)
    if attempt is None or str(attempt.pk) != str(attempt_id) or str(attempt.generation_task_id) != str(task_id):
        raise ValidationError("原任务关联已变化，请重新读取。", code="execution_preparation_conflict")
    task = attempt.generation_task
    with _locked_task_attempt(task, attempt) as (stage, current_preparation, current_attempt, current):
        if ((stage.production_run.owner_id != actor.pk and not actor.is_staff) or not actor.is_active
                or current_preparation.revision != expected_preparation_revision
                or current.updated_at != task_updated_at):
            raise ValidationError("任务或权限已变化，请重新读取后确认。", code="execution_preparation_conflict")
        reason = _reason(current_preparation, current_attempt, current)
        if reason:
            raise ValidationError(reason, code="execution_preparation_conflict")
        read_prepared_generation_task(current)  # verifies frozen request/target ownership
        row = ExecutionTextResponseReview.objects.filter(attempt=current_attempt).first()
        if row is None or row.original_sha256 != receipt_sha256:
            raise ValidationError("原回执身份已变化，不能恢复。", code="execution_preparation_conflict")
        history = read_prepared_generation_history(current)  # verifies original private bytes
        if not history or history.get("status", {}).get("status_str") != "success" or history["status"].get("completed") is not True:
            raise ValidationError("原回执没有明确成功，不会重新提交。")
        before = {"task_id": str(current.pk), "attempt_id": str(current_attempt.pk),
            "status": current.status, "error_code": current_attempt.error_code,
            "error_message_sha256": hashlib.sha256(current_attempt.error_message.encode()).hexdigest(),
            "task_error_sha256": hashlib.sha256(current.error_message.encode()).hexdigest(),
            "finished_at": current.finished_at.isoformat() if current.finished_at else None,
            "receipt_sha256": receipt_sha256, "request_sha256": current_attempt.request_sha256}
        now = timezone.now()
        changed = update_prepared_generation_lifecycle(current, {
            "status": "queued", "finished_at": None, "heartbeat_at": now,
            "error_message": "仅重新归档已保存成功回执；不重新生成。", "remote_error": "",
        }, condition=type(current).objects.filter(pk=current.pk, status="failed", updated_at=task_updated_at), remote_present=True)
        if changed != 1:
            raise ValidationError("原任务已被处理，请重新读取。", code="execution_preparation_conflict")
        record_platform_lifecycle_event(stage.production_run, actor=actor, action="recover_media_archive",
            reason_code="production.retained_media_archive", before_summary=before,
            after_summary={"task_id": str(current.pk), "attempt_id": str(current_attempt.pk),
                "status": "queued", "receipt_sha256": receipt_sha256, "new_model_call": False})
        current_preparation.revision += 1
        current_preparation.save(update_fields=("revision", "updated_at"))
        return current_preparation
