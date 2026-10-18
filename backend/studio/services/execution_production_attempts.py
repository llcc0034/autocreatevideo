"""Explicit primary retry chains; never choose a latest successful result."""
from django.core.exceptions import ValidationError
from django.db.models import BooleanField, Case, Exists, F, OuterRef, Value, When

from studio.models import (ExecutionFieldValue, ExecutionTextResponseReview, GeneratedArtifact,
    GenerationAttempt, GenerationAttemptResolution, ProductionMaterialFulfillment)


def superseded_uncertain_task_ids():
    """Only explicitly authorized, same-request successors release the queue breaker.

    The original remote result and cost remain unknown; ignore_late_result alone
    (also used for cancellation) must never authorize another GPU submission.
    """
    return GenerationAttempt.objects.filter(
        role="primary", status="failed", error_code="uncertain_remote_response", ignore_late_result=True,
        generation_task__isnull=False, retry_successor__retry_requested_by__isnull=False,
        retry_successor__preparation_id=F("preparation_id"),
        retry_successor__stage_run_id=F("stage_run_id"),
        retry_successor__unit_key=F("unit_key"),
        retry_successor__request_sha256=F("request_sha256"),
    ).order_by().values("generation_task_id")


def current_primary_attempts(*, stage, preparation_ids=None):
    rows = GenerationAttempt.objects.filter(stage_run=stage, role="primary", retry_successor__isnull=True)
    if preparation_ids is not None:
        rows = rows.filter(preparation_id__in=preparation_ids)
    return rows


def retry_check_queryset(query):
    """Batch the same retry facts without loading requests or task output bodies."""
    return query.select_related("execution_instance_version", "generation_task", "preparation").annotate(
        retry_material_fulfilled=Exists(ProductionMaterialFulfillment.objects.filter(
            stage_run_id=OuterRef("stage_run_id"), unit_key=OuterRef("unit_key"))),
        retry_has_artifacts=Exists(GeneratedArtifact.objects.filter(generation_attempt_id=OuterRef("pk"))),
        retry_has_field_values=Exists(ExecutionFieldValue.objects.filter(origin_attempt_id=OuterRef("pk"))),
        retry_has_text_response=Exists(ExecutionTextResponseReview.objects.filter(attempt_id=OuterRef("pk"))),
        retry_has_resolutions=Exists(GenerationAttemptResolution.objects.filter(attempt_id=OuterRef("pk"))),
        retry_task_has_artifacts=Exists(GeneratedArtifact.objects.filter(generation_task_id=OuterRef("generation_task_id"))),
        retry_task_has_outputs=Case(When(generation_task__outputs=[], then=Value(False)),
            default=Value(True), output_field=BooleanField()),
    ).only("id", "stage_run_id", "unit_key", "status", "role", "error_code", "ignore_late_result",
        "submission_started_at", "submitted_at", "provider_request_id", "remote_status", "claim_token", "result_sha256", "finished_at",
        "actual_cost", "committed_cost", "is_external", "operation", "preparation_id", "preparation__id", "preparation__status",
        "execution_instance_version_id", "execution_instance_version__id", "execution_instance_version__workflow_version_id",
        "generation_task_id", "generation_task__id", "generation_task__status", "generation_task__submission_state",
        "generation_task__submission_token", "generation_task__comfy_prompt_id")


def attempt_has_artifacts(attempt):
    return attempt.retry_has_artifacts if hasattr(attempt, "retry_has_artifacts") else attempt.artifacts.exists()


def require_retryable_attempt(attempt, *, automatic=False):
    require_unfulfilled_material_unit(stage_id=attempt.stage_run_id, unit_key=attempt.unit_key,
        fulfilled=getattr(attempt, "retry_material_fulfilled", None))
    require_retryable_failure(attempt, automatic=automatic)


def require_retryable_failure(attempt, *, automatic=False, allow_auxiliary=False):
    """Original failure evidence; historical route reads do not authorize calls."""
    if automatic and attempt.error_code == "execution_response_storage_failed":
        raise ValidationError("模型已返回但原文存储失败，请先检查文件存储；不能自动换路再次调用。",
            code="execution_response_storage_failed")
    local_execution = not attempt.is_external
    roles = {"primary", "auxiliary"} if allow_auxiliary else {"primary"}
    if (attempt.role not in roles or attempt.status != "failed" or attempt.ignore_late_result
            or attempt_has_artifacts(attempt) or attempt.committed_cost is None or attempt.committed_cost != 0
            or (attempt.actual_cost is None and not local_execution and not unsubmitted_text_failure(attempt))):
        raise ValidationError("只能重试已明确失败且预留已释放的调用；外部调用还需核对费用，已有产物或取消中的任务不能重发。")
    if attempt.role == "auxiliary" and (attempt.claim_token or attempt.result_sha256 or attempt.execution_field_values.exists()):
        raise ValidationError("辅助调用仍被领取或已有结果，不能自动重发。")
    from studio.services.async_media_contract import ASYNC_MEDIA_OPERATIONS
    if attempt.execution_instance_version.workflow_version_id or attempt.operation in ASYNC_MEDIA_OPERATIONS:
        from studio.services.production_runs import _definitive_media_failure
        definitive, _ = _definitive_media_failure(attempt)
        if not definitive:
            raise ValidationError("媒体任务尚不具备明确失败边界，请先恢复原任务或核对远端状态。")
    elif attempt.error_code in {"uncertain_remote_response", "actual_cost_reconciliation_required"}:
        raise ValidationError("远端提交或费用尚未核对，不能重发。")


def unsubmitted_text_failure(attempt):
    """A persisted pre-POST failure is not an unknown external bill.

    Only the original text worker's local validation failure qualifies. Never
    infer non-submission from a missing receipt after submission has started.
    This predicate does not settle cost or rewrite the failed attempt.
    """
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
    if (attempt.operation not in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES}
            or attempt.status != "failed" or attempt.error_code != "execution_request_invalid"
            or attempt.submission_started_at is not None or attempt.submitted_at is not None
            or attempt.provider_request_id or attempt.remote_status or attempt.claim_token
            or attempt.result_sha256 or attempt.generation_task_id or attempt.ignore_late_result
            or attempt.finished_at is None or attempt.committed_cost != 0):
        return False
    fields = attempt.retry_has_field_values if hasattr(attempt, "retry_has_field_values") else attempt.execution_field_values.exists()
    response = attempt.retry_has_text_response if hasattr(attempt, "retry_has_text_response") else hasattr(attempt, "text_response_review")
    resolutions = attempt.retry_has_resolutions if hasattr(attempt, "retry_has_resolutions") else attempt.resolutions.exists()
    return not (attempt_has_artifacts(attempt) or fields or response or resolutions)


def require_unfulfilled_material_unit(*, stage_id, unit_key, fulfilled=None):
    if fulfilled is None:
        fulfilled = ProductionMaterialFulfillment.objects.filter(stage_run_id=stage_id, unit_key=unit_key).exists()
    if fulfilled:
        raise ValidationError("此单元已明确选用成图满足；不能再创建生成准备或重发原失败调用")


def validate_retry_link(attempt):
    if not attempt.retry_of_id:
        return
    previous = GenerationAttempt.objects.get(pk=attempt.retry_of_id)
    if (attempt.pk == previous.pk or attempt.role != "primary" or previous.role != "primary"
            or previous.status != "failed" or attempt.attempt_number <= previous.attempt_number
            or any(getattr(attempt, key) != getattr(previous, key) for key in (
                "preparation_id", "stage_run_id", "execution_instance_version_id", "unit_key",
                "route_index", "operation", "request_sha256", "request_snapshot_sha256"))):
        raise ValidationError("重试必须引用同一准备、路线与冻结请求的原失败主调用。")
