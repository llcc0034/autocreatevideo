"""Read-only paginated impact of the existing stage retry command."""
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Value

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
from studio.services.execution_production_attempts import current_primary_attempts, require_retryable_attempt, retry_check_queryset
from studio.services.production_execution_retry import (
    require_stage_retry_status, stage_retry_context, is_material_recovery,
    require_skipped_material_failures, material_failure_can_be_skipped,
)


def retry_preview_sources(stage):
    calls = current_primary_attempts(stage=stage)
    preparations = ExecutionPreparation.objects.filter(stage_run=stage).exclude(
        execution_instance_version__operation__in=FIELD_OPERATION_MODES,
    ).annotate(has_primary=Exists(GenerationAttempt.objects.filter(preparation_id=OuterRef("pk"), role="primary")))
    return calls, preparations.filter(has_primary=False)


def retry_preview_rows(calls, preparations):
    # One bounded page includes both submitted calls and not-yet-submitted units.
    return calls.order_by().values("id", "created_at").annotate(kind=Value("call")).union(
        preparations.order_by().values("id", "created_at").annotate(kind=Value("preparation")), all=True,
    ).order_by("created_at", "id", "kind")


def retry_call_impact(attempt):
    if attempt.status == "succeeded":
        return "skip", "已成功，跳过；已有产物及选择保持不变。"
    if attempt.preparation.status == "cancelled" or attempt.status == "cancelled" or attempt.ignore_late_result:
        return "attention", "调用或准备已停止，不作为失败重发；请查看原任务。"
    if attempt.status == "failed":
        try:
            require_retryable_attempt(attempt)
        except ValidationError as exc:
            return "attention", "；".join(exc.messages)
        return "retry", "明确失败，可沿原准备创建新调用；复用原提示词、素材、参数和种子。"
    if attempt.error_code == "uncertain_remote_response" or attempt.status == "uncertain":
        return "attention", "远端结果未知，请恢复原任务查询；不会直接重新提交。"
    if attempt.status == "awaiting_review":
        return "attention", "已返回但仍待处理，请核对原响应；阶段重试不会重跑此调用。"
    return "skip", "调用尚未结束，本次跳过；继续查看原任务，不重复提交。"


def retry_preview_summary(run, stage, calls, preparations):
    context = stage_retry_context(stage)
    has_calls = calls.exists()
    reason = ""
    try:
        require_stage_retry_status(run, stage)
        if not has_calls and (run.status not in {"blocked", "failed"} or stage.status not in {"blocked", "failed"}):
            raise ValidationError("无主调用时只有受阻或失败阶段可以沿原流程继续。")
        if calls.filter(status="cancelled").exists() or calls.filter(ignore_late_result=True).exists():
            raise ValidationError("当前阶段包含已停止的调用，请先处理原任务；本次整批不会提交。")
        if has_calls and not context["expected_attempt_ids"]:
            raise ValidationError("没有失败主调用可重试；输入准备问题请处理后使用“继续 / 检查阶段”。")
        if not has_calls and preparations.filter(status="cancelled").exists():
            raise ValidationError("阶段包含已停止的准备，原流程不会继续；请先查看准备记录。")
        require_skipped_material_failures(stage, calls)
        for attempt in retry_check_queryset(calls.filter(pk__in=context["expected_attempt_ids"])).iterator(chunk_size=100):
            disposition, message = retry_call_impact(attempt)
            if disposition != "retry":
                raise ValidationError("失败集合仍有需处理的调用，本次整批不会提交。" + message)
    except ValidationError as exc:
        reason = "；".join(exc.messages)
    return {"retry_context": context, "can_submit": not bool(reason), "reason": reason,
        "mode": "primary_calls" if has_calls else "stage_flow",
        "call_count": calls.count(), "preparation_count": preparations.count(),
        "message": ("仅重试尚未履约且候选全部失败的素材单元；已履约、已有成功候选或仍在执行的单元跳过，不改原选择和主图依据。"
            if is_material_recovery(stage) else
            "仅重试确认的失败主调用；未提交的准备不在此批重试内，处理后使用“继续 / 检查阶段”。"
            if has_calls else "尚无主调用；本次进入原阶段流程，并非重新发送成功调用。辅助任务仍按原流程处理。")}


def serialize_retry_preview_page(rows, calls, preparations, *, stage):
    retry_ids = set(stage_retry_context(stage)["expected_attempt_ids"])
    from studio.services.production_recipe_routing import superseded_recipe_preparation_ids
    superseded_ids = superseded_recipe_preparation_ids(stage)
    attempts = {str(row.pk): row for row in retry_check_queryset(calls.filter(
        pk__in=[row["id"] for row in rows if row["kind"] == "call"]))}
    pending = {str(row.pk): row for row in preparations.filter(
        pk__in=[row["id"] for row in rows if row["kind"] == "preparation"]).only("id", "unit_key", "status", "reason_code")}
    result = []
    for row in rows:
        key = str(row["id"])
        if row["kind"] == "call":
            item = attempts[key]
            disposition, message = retry_call_impact(item)
            if item.preparation_id in superseded_ids:
                disposition, message = "skip", "此调用所在路线已按冻结配置回退；保留原失败记录，不重发旧路线。"
            elif item.status == "failed" and str(item.pk) not in retry_ids and material_failure_can_be_skipped(item):
                disposition = "skip"
                message = ("本素材单元已明确选用成图满足，不重发其失败调用；原调用与费用记录保留。"
                    if item.retry_material_fulfilled else
                    "同一素材单元已有成功候选或仍在执行，不重发其失败同伴；原调用记录保留。")
            preparation_id, attempt_id, code = item.preparation_id, item.pk, item.error_code
        else:
            item = pending[key]
            disposition, preparation_id, attempt_id, code = "attention", item.pk, None, item.reason_code
            message = ("准备已停止，不会继续生成；请查看准备记录。" if item.status == "cancelled" else
                "尚未创建主调用；请查看准备原因，处理后沿原准备继续，不重做成功的辅助字段。")
        result.append({"id": key, "kind": row["kind"], "unit_key": item.unit_key,
            "preparation_id": preparation_id, "attempt_id": attempt_id, "status": item.status,
            "error_code": code, "disposition": disposition, "message": message})
    return result
