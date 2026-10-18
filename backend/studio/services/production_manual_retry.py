"""One explicit manual retry; original preparation, dispatch and command receipt."""
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_production_attempts import require_retryable_attempt
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.production_manual_commands import ManualActionResult


def retry_manual_primary(*, run, stage, plan, request, actor):
    """Called inside the manual command's Run -> Stage transaction.

    A preparation receipt means the command was evaluated, not that a model
    succeeded or even queued. Admission can retain a blocker on that same
    preparation; handling it and retrying needs a new explicit command.
    """
    payload = request["payload"]
    if set(payload) != {"preparation_id", "expected_preparation_revision", "expected_attempt_id"}:
        raise ValidationError("重试只接收原准备、准备修订和明确失败调用；不能修改输入、提示词、参数或素材")
    try:
        preparation_id = UUID(payload["preparation_id"])
        attempt_id = UUID(payload["expected_attempt_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("重试必须指定有效的准备和失败调用UUID") from exc
    revision = payload["expected_preparation_revision"]
    if type(revision) is not int or revision < 1 or request["expected_selection_revision"] is not None:
        raise ValidationError("重试必须提供准备修订，且不修改采用修订")
    if (stage.mode != "manual" or run.status not in {"awaiting_input", "running", "blocked", "failed", "awaiting_approval"}
            or stage.status not in {"running", "blocked", "failed", "awaiting_approval"}):
        raise ValidationError("运行或阶段已停止、已采用或未开始，不能重发此主调用")
    preparation = ExecutionPreparation.objects.select_for_update().filter(pk=preparation_id, stage_run=stage).first()
    if preparation is None or preparation.unit_key != request["unit_key"]:
        raise ValidationError("重试准备不属于本次阶段和制作对象")
    detail = read_execution_preparation_detail(preparation)
    requested = detail.intent["request"]["requested"]
    if requested.get("scope") != request["scope"] or requested.get("run_plan_sha256") != plan.sha256:
        raise ValidationError("重试范围与原准备的制作对象或执行计划不一致")
    target = GenerationAttempt.objects.select_related("execution_instance_version").filter(
        pk=attempt_id, preparation=preparation, stage_run=stage, unit_key=preparation.unit_key, role="primary").first()
    if target is None or preparation.status == "cancelled" or preparation.request_frozen_at is None:
        raise ValidationError("没有对应的冻结主调用，或原准备已停止；不能重试辅助调用或未提交准备")
    # Different command keys still cannot allocate two successors for one
    # failed target. Same-key receipt replay is handled before this adapter.
    if GenerationAttempt.objects.filter(retry_of=target).exists():
        return ManualActionResult("preparation", preparation.pk)
    if preparation.revision != revision:
        raise ValidationError("准备修订已变化，请重新读取后确认", code="run_plan_source_conflict")
    require_retryable_attempt(target)
    if list(GenerationAttempt.objects.filter(preparation=preparation, role="primary",
            retry_successor__isnull=True).values_list("pk", flat=True)) != [target.pk]:
        raise ValidationError("原准备的当前主调用已变化，请重新读取")
    now = timezone.now()
    stage.status, stage.reason_code, stage.reason, stage.finished_at = "running", "", "", None
    stage.started_at = stage.started_at or now
    stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "started_at", "updated_at"))
    run.status, run.blocked_code, run.blocked_reason, run.finished_at = "running", "", "", None
    run.started_at = run.started_at or now
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "finished_at", "started_at", "updated_at"))
    dispatch_production_execution(preparation_id=preparation.pk, expected_preparation_revision=revision,
        actor=actor, retry_of_id=target.pk)
    # No stage advancement, field reconciliation, recompilation or adoption.
    return ManualActionResult("preparation", preparation.pk)
