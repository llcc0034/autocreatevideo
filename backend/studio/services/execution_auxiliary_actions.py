"""Owner-bound batch status and explicit retry, shared by original roots."""
from django.core.exceptions import ValidationError
from django.db.models import F, Window
from django.db.models.functions import RowNumber

from studio.models import AssetGenerationJob, GenerationAttempt
from studio.services.execution_auxiliary_requests import read_preparation_field_batch, read_production_field_context
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparation_owners import nonproduction_preparation_root
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.registry_reference_lock import registry_reference_writer


def read_auxiliary_batches(parent):
    detail = read_execution_preparation_detail(parent)
    if parent.purpose == "resource_smoke":
        from studio.services.execution_preparations import preparation_execution_definition
        preparation_execution_definition(parent, detail=detail)
        if parent.auxiliary_asset_jobs.exists():
            raise ValidationError("基础资源准备不能隐含辅助实例调用")
        # Required Profile fixture fields remain visible in field_slots and
        # resolved_inputs; there are no planned auxiliary task batches here.
        return []
    batches = detail.resolved_inputs["field_requests"]
    if not batches:
        return []
    mutable = parent.request_frozen_at is None and parent.status != "cancelled"
    if parent.stage_run_id:
        from studio.services.execution_production_auxiliaries import production_field_children
        root_open = parent.stage_run.status == "running" and (parent.stage_run.production_run.status == "running"
            or parent.stage_run.production_run.status == "awaiting_input"
            and parent.stage_run.production_run.blocked_code == "execution_preparation_review_required")
        children = list(production_field_children(parent))
        tasks = {task.preparation_id: task for task in GenerationAttempt.objects.filter(
            parent_preparation=parent, preparation_id__in=[row.pk for row in children]).order_by().only(
                "preparation_id", "status", "claim_token", "error_code", "error_message", "attempt_number",
                "is_external", "actual_cost", "committed_cost", "submission_started_at", "role",
                "ignore_late_result", "cancel_requested_at", "generation_task_id", "result_sha256")}
        latest = {}
        for child in children:
            _, batch, identity = read_production_field_context(child)
            latest[batch.sha256] = (tasks.get(child.pk), child, identity["attempt_number"])
    else:
        root_open = nonproduction_preparation_root(parent).status == "preparing"
        tasks = list(AssetGenerationJob.objects.filter(parent_preparation=parent).annotate(
            batch_rank=Window(expression=RowNumber(), partition_by=[F("field_batch_key")], order_by=F("attempt_number").desc())
        ).filter(batch_rank=1).only("field_batch_key", "attempt_number", "status", "claim_token", "error_code", "error_message"))
        from studio.models import ExecutionPreparation
        preparations = {row.asset_job_id: row for row in ExecutionPreparation.objects.filter(asset_job__in=tasks)}
        latest = {task.field_batch_key: (task, preparations.get(task.pk), task.attempt_number) for task in tasks}
    result = []
    for envelope in batches:
        batch, purpose, _ = read_preparation_field_batch(parent, envelope["sha256"])
        task, child, ordinal = latest.get(batch.sha256, (None, None, 0))
        retry_statuses = {"failed", "cancelled", "awaiting_review"} if parent.stage_run_id else {"failed", "cancelled"}
        can_retry = bool(mutable and root_open and task is not None and task.status in retry_statuses
            and task.claim_token is None and task.error_code != "uncertain_remote_response")
        if not parent.stage_run_id and mutable and root_open and task is not None:
            can_retry = can_retry or (task.status == "uncertain" and task.claim_token is None)
        requires_ack = bool(task is not None and task.status == "uncertain")
        reason = task.error_message if task else ""
        if parent.stage_run_id and task is not None and can_retry:
            from studio.services.execution_production_auxiliaries import production_field_retry_requires_acknowledgement
            try:
                requires_ack = production_field_retry_requires_acknowledgement(task)
            except ValidationError as exc:
                can_retry, reason = False, "；".join(exc.messages)
        if not reason and child is not None and (task is None or task.status == "preparing"):
            reason = child.reason
            if reason and child.reason_code:
                reason = f"辅助准备：[{child.reason_code}] {reason}"
        if task is None and child is None:
            reason = "尚未开始；检查并继续后按本次授权准备辅助任务。"
        result.append({"field_batch_key": batch.sha256, "purpose": purpose,
            "field_keys": [row["field_contract"]["field_key"] for row in batch.value["items"]],
            "attempt_number": ordinal, "task_id": task.pk if task else None, "preparation_id": child.pk if child else None,
            "status": task.status if task else child.status if child else "not_started",
            "reason": reason,
            "can_retry": can_retry, "requires_uncertain_acknowledgement": requires_ack})
    return result


@registry_reference_writer
def retry_preparation_field_batch(*, preparation_id, expected_preparation_revision, field_batch_key, attempt_number, actor,
        acknowledged_uncertain_task_id=None):
    from studio.services.execution_asset_auxiliaries import create_asset_field_child
    from studio.services.execution_production_auxiliaries import create_production_field_child
    from studio.services.execution_advancement import advance_execution_preparation

    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if parent.revision != expected_preparation_revision:
            raise ValidationError("准备已变化，请重新读取后确认重试", code="execution_preparation_conflict")
        batch = next((row for row in read_auxiliary_batches(parent) if row["field_batch_key"] == field_batch_key), None)
        if batch is None or not batch["can_retry"] or attempt_number != batch["attempt_number"] + 1:
            raise ValidationError("此批次不能重试，或尝试序号已变化；请处理原任务后重新读取")
        uncertain = batch["requires_uncertain_acknowledgement"]
        if uncertain and str(acknowledged_uncertain_task_id) != str(batch["task_id"]):
            raise ValidationError("必须明确确认保留此未决调用及未知费用，再授权新增自动尝试")
        if not uncertain and acknowledged_uncertain_task_id is not None:
            raise ValidationError("未决确认与当前批次不一致，请重新读取", code="execution_preparation_conflict")
        if parent.stage_run_id:
            run = parent.stage_run.production_run
            if run.status == "awaiting_input" and run.blocked_code == "execution_preparation_review_required":
                run.status, run.blocked_code, run.blocked_reason = "running", "", ""
                run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        allocate = create_production_field_child if parent.stage_run_id else create_asset_field_child
        allocate(preparation_id=parent.pk, expected_preparation_revision=parent.revision,
            field_batch_key=field_batch_key, attempt_number=attempt_number, actor=actor,
            **({"acknowledged_uncertain_task_id": acknowledged_uncertain_task_id} if uncertain else {}))
    parent.refresh_from_db()
    advanced = advance_execution_preparation(preparation_id=parent.pk, expected_preparation_revision=parent.revision, actor=actor)
    if parent.stage_run_id:
        from studio.services.production_run_auto_advance import schedule_recipe_run_advance
        schedule_recipe_run_advance(parent.stage_run.production_run_id)
    return advanced
