"""Shared atomic pending-field storage and child observations; no queue or billing."""
from datetime import timedelta
import hashlib

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils import timezone

from studio.models import AssetGenerationJob, ExecutionFieldValue, GenerationAttempt
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_fields import _editable, _field_context, _locked_preparations, _touch, _verify_value
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.json_contracts import assign_json_contract, json_contract


def verify_production_field_result(attempt):
    """Successful settlement and all pending fields were committed atomically."""
    from studio.services.execution_auxiliary_requests import read_attempt_field_batch
    from studio.services.execution_preparation_fields import _field_context, _verify_value
    from studio.services.execution_field_contracts import validate_execution_field_response
    batch = read_attempt_field_batch(attempt)
    parent = attempt.parent_preparation
    if attempt.status != "succeeded" or attempt.ignore_late_result:
        raise ValidationError("字段必须来自已成功且未忽略的辅助任务")
    values = list(parent.field_values.filter(origin_attempt=attempt))
    indexed = {(value.unit_key, value.field_key): value for value in values}
    if len(values) != len(batch.value["items"]) or len(indexed) != len(values):
        raise ValidationError("辅助结果与完整字段批次不一致，不能部分采用")
    items = []
    for item in batch.value["items"]:
        value = indexed.get((item["unit_key"], item["field_contract"]["field_key"]))
        if value is None or value.origin_kind != "model" or value.origin_asset_job_id:
            raise ValidationError("辅助结果的字段来源不一致")
        _verify_value(_field_context(parent, value.unit_key, value.field_key), value)
        items.append({"request_id": item["request_id"], "text": value.text})
    parsed = validate_execution_field_response(batch, {"items": items})
    if json_contract(parsed["result"], max_bytes=execution_configuration_max_bytes()).sha256 != attempt.result_sha256:
        raise ValidationError("字段正文与辅助任务已保存的结果摘要不一致")
    return values


def save_pending_auxiliary_fields(parent, child, batch, parsed, actor):
    """Caller holds root/Preparation/task locks; full batch or no fields."""
    if isinstance(child, AssetGenerationJob):
        origin = {"origin_asset_job": child}
    elif isinstance(child, GenerationAttempt):
        origin = {"origin_attempt": child}
    else:
        raise ValidationError("字段必须来自原任务，不能制造任务来源")
    if child.parent_preparation_id != parent.pk:
        raise ValidationError("字段回写目标与原任务的父准备不一致")
    if isinstance(child, AssetGenerationJob) and parent.auxiliary_asset_jobs.filter(
            auxiliary_purpose=child.auxiliary_purpose, field_batch_key=child.field_batch_key,
            attempt_number__gt=child.attempt_number).exists():
        # A later explicit attempt owns this batch. Preserve any late original
        # response/cost, but never let it inject fields into the resumed chain.
        return []
    items = {row["request_id"]: row for row in batch.value["items"]}
    existing = list(parent.field_values.select_for_update().filter(**origin).order_by("id"))
    expected = {(row["unit_key"], row["field_contract"]["field_key"]): row for row in items.values()}
    returned = {(items[row["request_id"]]["unit_key"], items[row["request_id"]]["field_contract"]["field_key"]): row["text"] for row in parsed["result"]["items"]}
    if set(returned) != set(expected) or len(parsed["result"]["items"]) != len(expected):
        raise ValidationError("字段结果必须覆盖完整批次，不能部分落库")
    if existing:
        if len(existing) != len(expected) or {(row.unit_key, row.field_key): row.text for row in existing} != returned:
            raise ValidationError("子任务已有字段记录与完整响应不一致，不能补写或覆盖")
        for value in existing:
            _verify_value(_field_context(parent, value.unit_key, value.field_key), value)
        return existing
    if parent.status == "cancelled" or parent.request_frozen_at is not None or child.ignore_late_result:
        # The real successful response/cost stays on the child. It cannot
        # restore a cancelled request or overwrite already-frozen choices.
        if not child.ignore_late_result:
            child.ignore_late_result = True
            child.save(update_fields=("ignore_late_result", "updated_at"))
        return []
    values = []
    for (unit_key, field_key), text in returned.items():
        context = _field_context(parent, unit_key, field_key)
        revision = (parent.field_values.filter(unit_key=unit_key, field_key=field_key).aggregate(latest=Max("revision"))["latest"] or 0) + 1
        value = ExecutionFieldValue(preparation=parent, unit_key=unit_key, field_key=field_key, revision=revision,
            source_sha256=context["source_sha256"], field_contract_sha256=context["field_contract_sha256"],
            text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            origin_kind="model", **origin, created_by=actor, status="pending")
        _verify_value(context, value)
        value.save()
        values.append(value)
    _touch(parent)
    return values


def record_preparation_child_wait(preparation_id, actor, children):
    """Current original child facts, not an invented main admission verdict."""
    policies = read_execution_policies()
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        _editable(parent)
        detail = read_execution_preparation_detail(parent)
        latest = {}
        for row in children:
            latest[(row["auxiliary_purpose"], row["field_batch_key"])] = row["status"]
        recoveries = [row["recovery"] for row in children if row.get("recovery")]
        unsafe = any(row["status"] in {"failed", "cancelled", "uncertain", "blocked"} and not row.get("recovery") for row in children)
        if recoveries and not unsafe:
            manual = [row for row in recoveries if row["state"] == "manual"]
            chosen = manual[0] if manual else min(recoveries, key=lambda row: row["next_retry_at"])
            status, code, reason, retryable = ("awaiting_review" if manual else "preparing",
                chosen["code"], "辅助生成：" + chosen["reason"], not bool(manual))
        elif any(state in {"failed", "cancelled", "uncertain", "blocked"} for state in latest.values()):
            status, code, reason, retryable = ("blocked", "execution_auxiliary_requires_action",
                "辅助任务失败、被阻断、已取消或远端状态不确定，请处理该批次；不会自动再次调用", False)
        elif any(state == "awaiting_review" for state in latest.values()):
            status, code, reason, retryable = ("awaiting_review", "execution_auxiliary_review_required",
                "字段辅助任务等待人工处理。请在任务与记录中核对其用量或保留的响应；输出不完整时可在此主准备人工填写并确认字段，不会自动重试。", False)
        elif any(state != "succeeded" for state in latest.values()):
            status, code, reason, retryable = ("preparing", "execution_auxiliary_pending",
                "正在准备或等待字段辅助任务；主调用尚未提交", True)
        else:
            return None
        facts = [{**row, "id": str(row["id"])} for row in children]
        body = {"schema_version": 1, "phase": "auxiliary", "status": status, "reason_code": code,
            "reason": reason, "children": facts}
        previous = detail.evaluation
        if parent.evaluation_revision == parent.revision and isinstance(previous, dict) and {
                key: value for key, value in previous.items() if key not in {"preparation_revision", "evaluated_at"}} == body:
            # Refresh only the scanner deadline. Identical observations do not
            # consume an authoring revision or invalidate a user's pending form.
            parent.next_check_at = timezone.now() + timedelta(seconds=policies.recheck_interval_seconds) if retryable else None
            parent.save(update_fields=("next_check_at", "updated_at"))
            return parent
        parent.revision += 1
        parent.status, parent.reason_code, parent.reason = status, code, reason
        parent.evaluation_revision, parent.evaluated_at = parent.revision, timezone.now()
        parent.next_check_at = parent.evaluated_at + timedelta(seconds=policies.recheck_interval_seconds) if retryable else None
        assign_json_contract(detail, "evaluation", {**body, "preparation_revision": parent.revision,
            "evaluated_at": parent.evaluated_at.isoformat()}, max_bytes=execution_configuration_max_bytes())
        parent.save()
        detail.save()
        return parent
