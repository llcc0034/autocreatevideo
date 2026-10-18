"""Retry confirmed failed primaries via the original prepared dispatch path."""
from django.core.exceptions import ValidationError
from django.db import transaction
from copy import deepcopy

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionMaterialFulfillment
from studio.services.execution_production_attempts import (
    current_primary_attempts, require_retryable_attempt, retry_check_queryset, attempt_has_artifacts,
)
from studio.services.execution_production_dispatch import dispatch_production_execution


def _is_material_stage(stage):
    return (stage.production_run.plan_source == "recipe" and stage.stage_key == "bible_image_generation"
        and stage.mode == "existing_pipeline")


def is_material_recovery(stage):
    return _is_material_stage(stage) and stage.reason_code == "waiting_material_recovery"


def stage_retry_attempts(stage):
    """One target set for the original context, preview and retry writer.

    During material takeover only wholly failed units need new calls. A failed
    peer of a successful/pending candidate is not permission to rerun that unit.
    No selection, latest version, or independent recovery queue is inferred.
    """
    current = current_primary_attempts(stage=stage)
    failed = current.filter(status="failed")
    if stage.production_run.plan_source == "recipe":
        from studio.services.production_recipe_routing import superseded_recipe_preparation_ids
        failed = failed.exclude(preparation_id__in=superseded_recipe_preparation_ids(stage))
    if _is_material_stage(stage):
        failed = failed.exclude(unit_key__in=current.exclude(status="failed").values("unit_key"))
        failed = failed.exclude(unit_key__in=ProductionMaterialFulfillment.objects.filter(
            stage_run=stage).values("unit_key"))
    return failed


def material_failure_can_be_skipped(attempt):
    from studio.services.production_runs import _definitive_media_failure
    return (attempt.status == "failed" and not attempt.ignore_late_result
        and attempt.preparation.status != "cancelled" and not attempt_has_artifacts(attempt)
        and _definitive_media_failure(attempt)[0])


def require_skipped_material_failures(stage, calls):
    if not _is_material_stage(stage):
        return
    skipped = calls.filter(status="failed").exclude(pk__in=stage_retry_attempts(stage).values("pk"))
    for attempt in retry_check_queryset(skipped).iterator(chunk_size=100):
        if not material_failure_can_be_skipped(attempt):
            raise ValidationError("已有成功候选的单元仍有取消、结果未知或归档不完整的失败同伴，请核对原任务，不能忽略。")


def require_stage_retry_status(run, stage):
    from studio.services.production_execution_recovery import MANUAL_RECOVERY
    from studio.services.execution_resource_policy import REVIEW_REQUIRED
    reviewing = (run.status == stage.status == "awaiting_approval"
        and run.blocked_code == stage.reason_code == REVIEW_REQUIRED)
    recovering = (is_material_recovery(stage) and run.status == "awaiting_input" and stage.status == "running"
        and run.blocked_code == stage.reason_code)
    manual_recovery = (run.status == "awaiting_input" and stage.status == "blocked"
        and run.blocked_code == stage.reason_code and stage.reason_code in MANUAL_RECOVERY)
    if not reviewing and not recovering and not manual_recovery and (run.status not in {"blocked", "failed"} or stage.status not in {"blocked", "failed"}):
        raise ValidationError("运行或阶段状态已改变，请重新读取后确认重试。")


def stage_retry_context(stage):
    if stage.stage_key == "assembly":
        from studio.services.production_assembly_retry import assembly_retry_context
        return {"expected_stage_id": str(stage.pk), "expected_attempt_ids": [], **assembly_retry_context(stage)}
    return {"expected_stage_id": str(stage.pk), "expected_attempt_ids": [str(pk) for pk in
        stage_retry_attempts(stage).order_by("id").values_list("pk", flat=True)]}


def retry_stage_executions(run, stage, *, expected_attempt_ids, actor):
    from studio.services.production_runs import advance_production_run
    from studio.services.execution_resource_policy import REVIEW_REQUIRED
    expected = {str(key) for key in expected_attempt_ids}
    if len(expected) != len(expected_attempt_ids) or not expected:
        raise ValidationError("请明确本次确认重试的失败调用，不能自动推断或重复指定。")
    targets = list(GenerationAttempt.objects.filter(stage_run=stage, pk__in=expected, role="primary")
        .select_related("execution_instance_version").order_by("preparation_id", "attempt_number"))
    if len(targets) != len(expected):
        raise ValidationError("重试目标不属于本阶段的主调用。")
    successors = {str(key) for key in GenerationAttempt.objects.filter(retry_of_id__in=expected)
        .values_list("retry_of_id", flat=True)}
    if successors == expected:
        return run  # Exact action replay, even if its successor has since failed.
    if successors:
        raise ValidationError("重试范围已部分变化，请重新读取当前失败调用后确认。")
    require_stage_retry_status(run, stage)
    current = list(current_primary_attempts(stage=stage).select_related("execution_instance_version"))
    if any(row.status == "cancelled" or row.ignore_late_result for row in current):
        raise ValidationError("当前阶段包含已取消的调用，请先处理原任务，不能将取消当成失败重发。")
    failures = set(stage_retry_context(stage)["expected_attempt_ids"])
    if expected - successors != failures:
        raise ValidationError("失败调用范围已变化，请重新读取并确认。")
    require_skipped_material_failures(stage, current_primary_attempts(stage=stage))
    batch = transaction.savepoint()
    for target in targets:
        if str(target.pk) not in successors:
            require_retryable_attempt(target)
    stage.status, stage.reason_code, stage.reason, stage.finished_at = "running", "", "", None
    run.status, run.blocked_code, run.blocked_reason, run.finished_at = "running", "", "", None
    stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "updated_at"))
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "finished_at", "updated_at"))
    for target in targets:
        prep = ExecutionPreparation.objects.get(pk=target.preparation_id)
        result = dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=actor, retry_of_id=target.pk)
        if result is None:
            prep.refresh_from_db()
            code, reason = prep.reason_code, prep.reason
            # Keep the exact projected batch exposure for explicit review even
            # though partial allocations/reservations must be rolled back.
            from studio.services.json_contracts import assign_json_contract
            from studio.services.execution_instances import execution_configuration_max_bytes
            review = deepcopy(prep.contract.evaluation) if code == REVIEW_REQUIRED else None
            review_fields = {key: getattr(prep, key) for key in (
                "revision", "evaluation_revision", "evaluated_at", "reason_code", "reason")}
            transaction.savepoint_rollback(batch)
            if review is not None:
                prep.refresh_from_db()
                for key, value in review_fields.items():
                    setattr(prep, key, value)
                prep.next_check_at = prep.claim_token = prep.claim_until = None
                detail = prep.contract
                assign_json_contract(detail, "evaluation", review, max_bytes=execution_configuration_max_bytes())
                detail.save()
                prep.save()
            # Allocation and reservations are all-or-none for the confirmed
            # failed set. Otherwise blocked later units strand queued siblings.
            status = "awaiting_approval" if review is not None else "blocked"
            stage.status, stage.reason_code, stage.reason = status, code, reason
            run.status, run.blocked_code, run.blocked_reason = status, code, reason
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            return run
    transaction.savepoint_commit(batch)
    run.refresh_from_db()
    return advance_production_run(run)
