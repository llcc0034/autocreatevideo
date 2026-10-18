"""Explicit Comfy primary retry authorization; uncertainty is never a failure fact."""
from django.core.exceptions import ValidationError

from studio.models import ExecutionTextResponseReview, GenerationAttempt
from studio.services.execution_production_attempts import require_unfulfilled_material_unit
from studio.services.execution_production_workflow_queue import _locked_task_attempt, read_prepared_generation_task
from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event
from studio.services.registry_reference_lock import registry_reference_write_guard


def require_uncertain_media_retry(attempt):
    preparation, task, stage = attempt.preparation, attempt.generation_task, attempt.stage_run
    require_unfulfilled_material_unit(stage_id=stage.pk, unit_key=attempt.unit_key)
    if (attempt.role != "primary" or not attempt.execution_instance_version.workflow_version_id or task is None
            or preparation.purpose != "production" or preparation.status != "dispatched"
            or attempt.status != "failed" or attempt.error_code != "uncertain_remote_response"
            or attempt.ignore_late_result or attempt.cancel_requested_at is not None or attempt.claim_token is not None
            or task.status != "failed" or task.submission_state not in {"prepared", "submitted", "recovered"}
            or not task.submission_token or task.submission_token != str(attempt.pk)
            or task.comfy_prompt_id != task.submission_token or attempt.submission_started_at is None
            or task.outputs or attempt.artifacts.exists() or task.generated_artifacts.exists()
            or ExecutionTextResponseReview.objects.filter(attempt=attempt).exists()
            or GenerationAttempt.objects.filter(retry_of=attempt).exists()
            or stage.status not in {"running", "blocked", "failed", "awaiting_approval"}
            or stage.production_run.status not in {"running", "blocked", "failed", "awaiting_input", "awaiting_approval"}):
        raise ValidationError("仅支持已回收为远端未决、无在途领取或结果的 ComfyUI 主调用；已有回执请恢复原任务。",
            code="execution_preparation_conflict")


def describe_media_uncertain_retry(preparation):
    rows = list(GenerationAttempt.objects.filter(preparation=preparation, role="primary", retry_successor__isnull=True)
        .select_related("generation_task", "stage_run__production_run", "execution_instance_version")[:2])
    if len(rows) != 1 or rows[0].error_code != "uncertain_remote_response" or not rows[0].generation_task_id:
        return None
    attempt = rows[0]
    reason = ""
    try:
        require_uncertain_media_retry(attempt)
    except ValidationError as exc:
        reason = "；".join(exc.messages)
    return {"task_id": str(attempt.generation_task_id), "attempt_id": str(attempt.pk),
        "task_updated_at": attempt.generation_task.updated_at, "request_sha256": attempt.request_sha256,
        "can_retry": not reason, "reason": reason}


def retry_uncertain_media(*, preparation, actor, expected_preparation_revision, task_id, attempt_id,
                          task_updated_at, request_sha256, acknowledged):
    if acknowledged is not True:
        raise ValidationError("必须明确接受原结果及费用仍未知、可能重复执行的风险。")
    attempt = GenerationAttempt.objects.select_related("generation_task").filter(
        pk=attempt_id, preparation=preparation, generation_task_id=task_id).first()
    if attempt is None:
        raise ValidationError("原主调用关联已变化。", code="execution_preparation_conflict")
    with registry_reference_write_guard(), _locked_task_attempt(attempt.generation_task, attempt) as (stage, prep, old, task):
        if not actor.is_active or (stage.production_run.owner_id != actor.pk and not actor.is_staff):
            raise ValidationError("无权新增此运行的调用。", code="execution_preparation_conflict")
        # Exact replay has one successor, even after that successor finishes.
        successor = GenerationAttempt.objects.filter(retry_of=old).first()
        if successor is not None:
            if (successor.retry_requested_by_id != actor.pk or old.request_sha256 != request_sha256
                    or old.error_code != "uncertain_remote_response" or not old.ignore_late_result):
                raise ValidationError("重试关联已变化。", code="execution_preparation_conflict")
            return prep
        if prep.revision != expected_preparation_revision or task.updated_at != task_updated_at or old.request_sha256 != request_sha256:
            raise ValidationError("原任务观察或准备已变化，请重新读取并确认。", code="execution_preparation_conflict")
        require_uncertain_media_retry(old)
        read_prepared_generation_task(task)  # Verify exact frozen request, bytes, unit and owner.
        run = stage.production_run
        stage.status, stage.reason_code, stage.reason, stage.finished_at = "running", "", "", None
        run.status, run.blocked_code, run.blocked_reason, run.finished_at = "running", "", "", None
        stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "updated_at"))
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "finished_at", "updated_at"))
        from studio.services.execution_production_dispatch import dispatch_production_execution
        successor = dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=actor, retry_of_id=old.pk, acknowledged_uncertain_attempt_id=old.pk)
        if successor is None:
            prep.refresh_from_db()
            raise ValidationError(f"新尝试未创建：{prep.reason}", code="execution_preparation_conflict")
        # No status, request, result, token or monetary fact on the old call is rewritten.
        # Prevent a late original result from becoming an adopted candidate.
        old.ignore_late_result = True
        old.save(update_fields=("ignore_late_result", "updated_at"))
        record_platform_lifecycle_event(run, actor=actor, action="retry_uncertain_media",
            reason_code="production.uncertain_media_explicit_retry",
            before_summary={"attempt_id": str(old.pk), "task_id": str(task.pk), "status": old.status,
                "error_code": old.error_code, "request_sha256": old.request_sha256},
            after_summary={"attempt_id": str(successor.pk), "task_id": str(successor.generation_task_id),
                "status": successor.status, "request_sha256": successor.request_sha256, "new_model_call": True})
        prep.refresh_from_db()
        prep.revision += 1
        prep.save(update_fields=("revision", "updated_at"))
        from studio.services.production_run_auto_advance import schedule_production_run_advance
        schedule_production_run_advance(run.pk)
        return prep
