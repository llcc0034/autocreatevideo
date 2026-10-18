"""Stop exact production preparations through the original queues and ledger.

Preparation cancellation stops further preparation; it is NOT remote terminal
evidence. Running workers retain their claims and settle late responses normally.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask, ProductionAssemblyLink, ProductionRun, StageRun
from studio.services.execution_auxiliary_requests import production_field_unit, read_production_field_context
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparations import read_execution_preparation_detail


def cancellation_attempts(query):
    """Narrow state/ledger rows; cancellation never needs request JSON."""
    return query.select_related("stage_run").only("id", "stage_run_id", "stage_run__production_run_id",
        "preparation_id", "parent_preparation_id", "role", "operation", "generation_task_id", "status",
        "committed_cost", "actual_cost", "submission_started_at", "provider_request_id", "cancel_requested_at",
        "ignore_late_result", "request_sha256", "error_code", "error_message", "claim_token", "worker_id",
        "claimed_at", "next_poll_at", "finished_at", "updated_at").order_by("id")


def _release_unsubmitted(run, attempt):
    if (attempt.submission_started_at is not None or attempt.provider_request_id
            or attempt.actual_cost is not None or attempt.committed_cost is None or attempt.committed_cost < 0
            or attempt.committed_cost > run.committed_cost_total):
        raise ValidationError("任务提交事实或预留账目不一致，不能按未提交取消")
    run.committed_cost_total -= attempt.committed_cost
    attempt.committed_cost = Decimal("0")


def cancel_production_attempts(*, run, attempts, actor):
    """Caller holds Run -> Stage -> Preparation -> Attempt locks.

    Shared by whole-run and exact-preparation cancellation. No remote I/O and
    no invented bill; uncertain submissions keep their original state/budget.
    """
    from studio.services.text_generation_queue import append_attempt_resolution, TEXT_OPERATIONS
    from studio.services.async_media_queue import ASYNC_MEDIA_OPERATIONS
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("取消必须在原运行事务内执行")
    now = timezone.now()
    for attempt in attempts:
        if attempt.stage_run.production_run_id != run.pk:
            raise ValidationError("取消调用不属于当前运行")
        if attempt.status in {"succeeded", "cancelled"} or (
                attempt.status == "failed" and attempt.error_code != "uncertain_remote_response"):
            continue
        if attempt.ignore_late_result and attempt.cancel_requested_at is not None:
            continue  # Idempotent receipt; do not repeat remote cancellation.
        if attempt.generation_task_id is not None:
            from studio.services.execution_production_workflow_queue import update_prepared_generation_lifecycle
            task = GenerationTask.objects.select_for_update().get(pk=attempt.generation_task_id)
            if task.status in {"succeeded", "cancelled"}:
                raise ValidationError("工作流任务与调用终态不一致，不能覆盖原事实")
            status = "cancel_requested" if task.submission_token else "cancelled"
            updates = {"status": status, "heartbeat_at": now,
                "remote_state": "cancelling" if task.submission_token else "cancelled"}
            if status == "cancelled":
                updates["finished_at"] = now
            update_prepared_generation_lifecycle(task, updates,
                condition=GenerationTask.objects.filter(pk=task.pk, status=task.status))
            attempt.refresh_from_db()
            run.refresh_from_db(fields=("committed_cost_total",))
        else:
            if attempt.operation not in TEXT_OPERATIONS | ASYNC_MEDIA_OPERATIONS:
                raise ValidationError("此调用没有已接通的原取消协议，未修改准备或账目")
            if attempt.status == "awaiting_review":
                attempt.status, attempt.next_poll_at = "cancelled", None
                attempt.error_code, attempt.error_message = "execution_review_cancelled", "已停止处理保留的响应；原始正文和费用记录仍保留。"
            elif attempt.submission_started_at is None and attempt.status in {"queued", "running"}:
                _release_unsubmitted(run, attempt)
                attempt.status, attempt.finished_at = "cancelled", now
                attempt.claim_token, attempt.worker_id, attempt.claimed_at = None, "", None
                attempt.next_poll_at = None
                attempt.error_code = "execution_cancelled_before_submit"
                attempt.error_message = "执行准备已停止；本次调用尚未提交。"
            elif attempt.operation in ASYNC_MEDIA_OPERATIONS and attempt.status == "submitted":
                attempt.status, attempt.next_poll_at = "cancel_requested", now
                attempt.error_code, attempt.error_message = "cancel_requested", "已请求原队列取消远端任务，尚未确认终态。"
            elif attempt.error_code != "uncertain_remote_response":
                attempt.error_code = "cancel_requested_ignore_late"
                attempt.error_message = "已停止准备；提交边界中的结果不再采用，仍记录真实费用。"
            attempt.ignore_late_result, attempt.cancel_requested_at = True, now
            attempt.save()
            run.save(update_fields=("committed_cost_total", "updated_at"))
        append_attempt_resolution(attempt, outcome="cancel_requested_ignore_late",
            evidence={"schema_version": 1, "attempt_id": str(attempt.pk),
                "preparation_id": str(attempt.preparation_id), "request_sha256": attempt.request_sha256,
                "provider_request_id": attempt.provider_request_id, "policy": "stop_preparation_then_original_queue"},
            note="停止执行准备；远端状态与真实费用仍以原调用为准。", decided_by=actor)


def stop_production_preparations(preparations, *, actor):
    """Frozen inputs/fields stay intact. Original task claims are NOT cleared."""
    now = timezone.now()
    for preparation in preparations:
        if preparation.status == "cancelled":
            continue
        preparation.status = "cancelled"
        preparation.cancelled_at, preparation.cancelled_by = now, actor
        preparation.reason_code = "execution_preparation_cancelled"
        preparation.reason = "已停止此准备；已提交调用仍由原任务确认取消或结算，既有结果未被改写。"
        preparation.next_check_at, preparation.claim_token, preparation.claim_until = None, None, None
        preparation.revision += 1
        preparation.save(update_fields=("status", "cancelled_at", "cancelled_by", "reason_code", "reason",
            "next_check_at", "claim_token", "claim_until", "revision", "updated_at"))


def production_cancellation_state(preparation, detail=None):
    """Read-only capability for the original detail page; no remote probe."""
    if not preparation.stage_run_id:
        return None
    stage, reason = StageRun.objects.only("id", "production_run_id", "status").get(pk=preparation.stage_run_id), ""
    run = ProductionRun.objects.only("id", "status").get(pk=stage.production_run_id)
    if preparation.status == "cancelled":
        reason = "此准备已停止；远端任务是否结束及实际费用仍以原调用记录为准。"
    elif run.status in {"succeeded", "failed", "cancelled"} or stage.status == "succeeded":
        reason = "运行或阶段已结束；不能用取消改写已有结果。"
    elif ProductionAssemblyLink.objects.filter(production_run=run).exists():
        reason = "本轮已经提交装配；后续变更请新建制作轮次。"
    elif read_production_field_context(preparation, detail) is not None:
        reason = "这是辅助准备，请在对应主准备停止整组字段生成。"
    elif GenerationAttempt.objects.filter(preparation=preparation, status="succeeded").exists():
        reason = "主调用已经成功；请通过重新审核或新准备重做，不能取消成功事实。"
    return {"allowed": not reason, "reason": reason or "只停止此主准备及其辅助准备；其他候选不受影响，已提交调用仍可能产生费用。",
        "cancelled_at": preparation.cancelled_at,
        "cancelled_by": str(preparation.cancelled_by_id) if preparation.cancelled_by_id else None}


def cancel_production_preparation(*, preparation_id, expected_preparation_revision, actor):
    if type(expected_preparation_revision) is not int or expected_preparation_revision < 1:
        raise ValidationError("必须提供明确的准备修订号")
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if not parent.stage_run_id:
            raise ValidationError("此入口只取消生产准备；素材与验收请使用原任务取消入口")
        stage = StageRun.objects.only("id", "production_run_id").get(pk=parent.stage_run_id)
        run = ProductionRun.objects.only("id", "committed_cost_total").get(pk=stage.production_run_id)
        if parent.status == "cancelled":
            return parent
        if parent.revision != expected_preparation_revision:
            raise ValidationError("准备已变化，请重新读取后决定是否取消", code="execution_preparation_conflict")
        detail = read_execution_preparation_detail(parent)
        state = production_cancellation_state(parent, detail)
        if not state["allowed"]:
            raise ValidationError(state["reason"])
        units = [production_field_unit(parent.pk, row["sha256"]) for row in detail.resolved_inputs["field_requests"]]
        children = list(ExecutionPreparation.objects.select_for_update().filter(stage_run=stage, unit_key__in=units).order_by("id"))
        for child in children:
            identity = read_production_field_context(child)
            if identity is None or identity[0].pk != parent.pk:
                raise ValidationError("辅助准备归属不一致，不能按候选名称猜测取消范围")
        preparations = [parent, *children]
        ids = {row.pk for row in preparations}
        attempts = list(cancellation_attempts(GenerationAttempt.objects.select_for_update(of=("self",)).filter(
            Q(preparation_id__in=ids) | Q(parent_preparation=parent))))
        for attempt in attempts:
            if (attempt.preparation_id not in ids or attempt.stage_run_id != stage.pk
                    or (attempt.preparation_id == parent.pk and (attempt.role != "primary" or attempt.parent_preparation_id))
                    or (attempt.preparation_id != parent.pk and (attempt.role != "auxiliary" or attempt.parent_preparation_id != parent.pk))):
                raise ValidationError("实际调用与主/辅助准备归属不一致，未执行取消")
        stop_production_preparations(preparations, actor=actor)
        cancel_production_attempts(run=run, attempts=attempts, actor=actor)
        return parent
