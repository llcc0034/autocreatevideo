"""Production recovery over the original prepared calls, not another queue.

The frozen policy owns retries and budget. Definitive failures use explicit
replacement calls; a recovered original request only reconciles parent state.
"""
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import GenerationAttempt
from studio.services.json_contracts import verify_json_contract
from studio.services.security_boundaries import boundary_int

RETRY_WAIT = "execution_retry_wait"
RETRY_EXHAUSTED = "execution_retry_exhausted"
RETRY_MANUAL = "execution_retry_requires_action"
MANUAL_RECOVERY = {RETRY_EXHAUSTED, RETRY_MANUAL, "assembly_failed", "assembly_partial_failure"}


def reconcile_recovered_media_stage(stage):
    """Clear only a stale remote-uncertainty gate under the original Run/Stage locks.

    Called after Task/Attempt recovery writes its facts within the same atomic
    write, or by maintenance under the same owner locks. No provider call,
    retry allocation, approval, billing or frozen-request mutation occurs here.
    """
    from django.db.models import Q
    from studio.services.production_run_auto_advance import schedule_recipe_run_advance

    run = stage.production_run
    if (run.plan_source != "recipe" or run.status != "blocked" or stage.status != "blocked"
            or run.blocked_code != "uncertain_remote_response" or stage.reason_code != run.blocked_code
            or run.current_stage_key != stage.stage_key):
        return False
    current = GenerationAttempt.objects.filter(stage_run=stage, retry_successor__isnull=True)
    # A recovered sibling must not hide another unresolved request or a
    # cancellation. Auxiliary uncertainty is equally real.
    if current.filter(Q(error_code="uncertain_remote_response") | Q(ignore_late_result=True)
            | Q(status__in=("cancel_requested", "cancelled"))).exists():
        return False
    evidence = current.filter(role="primary", error_code="", status__in=("submitted", "succeeded"),
        submission_started_at__isnull=False, submitted_at__isnull=False,
        preparation__status="dispatched", preparation__stage_run=stage,
        generation_task__owner_id=run.owner_id, generation_task__project_id=run.project_id,
        generation_task__status__in=("queued", "running", "succeeded"),
        generation_task__submission_state__in=("recovered", "terminal_succeeded"),
    ).values("id", "provider_request_id", "generation_task__submission_token", "generation_task__comfy_prompt_id")
    if not any(str(row["id"]) == row["provider_request_id"] == row["generation_task__submission_token"]
            == row["generation_task__comfy_prompt_id"] for row in evidence):
        return False
    stage.status, stage.reason_code, stage.reason = "running", "", ""
    stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
    run.status, run.blocked_code, run.blocked_reason = "running", "", ""
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
    # Re-enter the existing scheduler only after all recovery writes commit.
    # It rechecks the original plan and handles any other real error normally.
    schedule_recipe_run_advance(run.pk)
    return True


def production_retry_policy(run):
    saved = verify_json_contract(run.config_snapshot,
        schema_version=run.config_snapshot_schema_version, size_bytes=run.config_snapshot_size_bytes,
        sha256=run.config_snapshot_sha256, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    values = saved.value.get("values", {})
    maximum = values.get("production.task_max_attempts")
    interval = values.get("execution.preparation.recheck_interval_seconds")
    if type(maximum) is not int or maximum < 1 or type(interval) is not int or interval < 1:
        raise ValidationError("本轮缺少有效的最大尝试次数或恢复检查间隔配置，不能猜测重试策略。")
    if maximum > boundary_int("security.production.max_automatic_retries", minimum=1):
        raise ValidationError("本轮最大尝试次数超过当前自动重试安全上限，请人工核对配置。")
    return maximum, interval


def production_retry_decision(attempt):
    """Read only. Unknown responses, cancellation and usable output never resend."""
    from studio.services.execution_production_attempts import require_retryable_failure
    result = {"state": "manual", "attempts": 0, "max_attempts": None,
        "next_retry_at": None, "reason": "", "code": RETRY_MANUAL}
    result["attempts"] = (attempt.attempt_number if attempt.role == "auxiliary" else
        GenerationAttempt.objects.filter(preparation_id=attempt.preparation_id, role="primary").count())
    try:
        maximum, interval = production_retry_policy(attempt.stage_run.production_run)
        result["max_attempts"] = maximum
        require_retryable_failure(attempt, automatic=True, allow_auxiliary=True)
        if attempt.preparation.status == "cancelled":
            raise ValidationError("执行准备已取消，不会自动重新提交。")
        if attempt.finished_at is None:
            raise ValidationError("缺少明确结束时间，请核对原调用。")
    except ValidationError as exc:
        result["reason"] = "；".join(exc.messages)
        if attempt.error_code in {"uncertain_remote_response", "actual_cost_reconciliation_required"}:
            result["code"] = attempt.error_code  # Keep the existing human resolution entry reachable.
        return result
    used = result["attempts"]
    if used >= maximum:
        result.update(code=RETRY_EXHAUSTED, reason=f"已尝试 {used}/{maximum} 次（含首次），自动重试已耗尽，请人工处理。")
        return result
    due = attempt.finished_at + timedelta(seconds=interval)
    result.update(state="retry", code=RETRY_WAIT, next_retry_at=due.isoformat(),
        reason=f"已尝试 {used}/{maximum} 次（含首次）；下一次自动重试不早于 {timezone.localtime(due).isoformat(timespec='seconds')}。")
    return result


def retry_due(decision):
    return decision["state"] == "retry" and timezone.now() >= datetime.fromisoformat(decision["next_retry_at"])


def advance_primary_retry(preparation, actor):
    from studio.services.execution_production_attempts import current_primary_attempts
    from studio.services.execution_production_dispatch import dispatch_production_execution
    stage = preparation.stage_run
    if stage.production_run.plan_source != "recipe" or stage.status != "running" or stage.production_run.status != "running":
        return
    current = list(current_primary_attempts(stage=stage, preparation_ids=[preparation.pk]))
    if len(current) != 1 or current[0].status != "failed":
        return
    from studio.services.production_execution_retry import stage_retry_attempts
    if not stage_retry_attempts(stage).filter(pk=current[0].pk).exists():
        return
    decision = production_retry_decision(current[0])
    if retry_due(decision):
        dispatch_production_execution(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=actor,
            retry_of_id=current[0].pk, automatic=True)


def failed_primary_recovery(stage, preparation_ids):
    """The producer presents the exact same policy used by the dispatcher."""
    from studio.services.production_execution_retry import stage_retry_attempts
    rows = stage_retry_attempts(stage).filter(preparation_id__in=preparation_ids)
    decisions = []
    for row in rows.select_related("stage_run__production_run", "preparation", "execution_instance_version", "generation_task"):
        decision = production_retry_decision(row)
        gate = preparation_retry_gate(row.preparation)
        if gate:
            decision.update(state="manual", code=gate[0], reason=gate[1])
        decisions.append(decision)
    if not decisions:
        return None
    manual = [row for row in decisions if row["state"] == "manual"]
    chosen = manual[0] if manual else min(decisions, key=lambda row: row["next_retry_at"])
    return chosen["code"], f"{len(manual) if manual else len(decisions)} 个失败调用：{chosen['reason']} 已成功任务与产物保留，不会重新生成。"


def preparation_retry_gate(preparation):
    from studio.services.execution_preparations import read_execution_preparation_detail
    evaluation = read_execution_preparation_detail(preparation).evaluation
    if preparation.reason_code and (preparation.status == "blocked" or
            evaluation.get("admission", {}).get("can_submit") is False):
        return preparation.reason_code, f"重试尚未提交：{preparation.reason} 请打开执行准备处理。"
    return None


def active_primary_retry_feedback(stage, preparation_ids):
    from studio.services.execution_production_attempts import current_primary_attempts
    rows = current_primary_attempts(stage=stage, preparation_ids=preparation_ids).filter(
        retry_of__isnull=False, status__in=("queued", "running", "submitting", "submitted"))
    count = rows.count()
    if not count:
        return None
    for row in rows.select_related("preparation"):
        gate = preparation_retry_gate(row.preparation)
        if gate:
            from studio.services.production_runs import StageBlocked
            raise StageBlocked(*gate, awaiting_approval=gate[0] == "execution_resource_confirmation_required")
    maximum, _ = production_retry_policy(stage.production_run)
    return "execution_retry_running", f"{count} 个失败调用已进入重试队列或正在重试；每个原请求最多 {maximum} 次（含首次）。成功任务和已采用字段不会重跑。"
