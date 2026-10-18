"""Live, system-owned business thresholds; never rewrite a frozen request.

Owners retain their existing accounting and external-call authorization. A
confirmation is bounded by request, policy revision and projected exposure;
it is not a reusable permission to ignore future limits.
"""
from decimal import Decimal
import re

from django.core.exceptions import ValidationError

from studio.services.configuration import resolve_configuration
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import json_contract, assign_json_contract, verify_json_contract


RESOURCE_POLICY = "execution.resources.review_policy"
REVIEW_REQUIRED = "execution_resource_confirmation_required"


def validate_resource_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {"budget_by_currency", "call_count", "output_length"}
            or any(type(policy[key]) is not int or policy[key] < 1 for key in ("call_count", "output_length"))
            or not isinstance(policy["budget_by_currency"], dict)):
        raise ValidationError("系统资源审核策略缺失或无效，请在系统设置中修正；不使用模型或配方默认额度")
    for currency, amount in policy["budget_by_currency"].items():
        if (not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", currency) or not isinstance(amount, str)
                or not re.fullmatch(r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,4})?", amount)):
            raise ValidationError("系统预算提醒需使用明确币种和非负金额字符串，最多4位小数")


def read_resource_policy():
    resolved = resolve_configuration(required_keys=(RESOURCE_POLICY,))
    policy, source = resolved.values[RESOURCE_POLICY], resolved.sources[RESOURCE_POLICY]
    if source.get("scope") != "system" or not source.get("revision_id"):
        raise ValidationError("资源策略必须来自启用的系统配置修订")
    validate_resource_policy(policy)
    return json_contract({"schema_version": 1, "values": policy, "source": source},
        max_bytes=execution_configuration_max_bytes())


def actual_cost_review_message(amount, currency):
    """Actual bills are facts, never rejected or changed to fit an estimate."""
    if amount is None or amount == 0:
        return ""
    threshold = read_resource_policy().value["values"]["budget_by_currency"].get(currency)
    if threshold is not None and amount <= Decimal(threshold):
        return ""
    boundary = f"超过当前系统提醒值 {threshold}" if threshold is not None else "该币种尚未设置系统提醒值"
    return f"实际费用 {amount} {currency}，{boundary}；真实结果和账目已保留，请人工核对，不会因此判定生成失败。"


def pause_production_actual_overage(run, stage):
    message = actual_cost_review_message(run.actual_cost_total, run.cost_currency)
    if not message:
        return
    # Existing explicit cost approval action records the decision; do not
    # mark the successful provider call failed or throw away its candidate.
    stage.status, stage.reason_code, stage.reason = "awaiting_approval", "actual_budget_exceeded", message
    stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
    run.status, run.blocked_code, run.blocked_reason = "awaiting_approval", stage.reason_code, message
    run.current_stage_key = stage.stage_key
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "current_stage_key", "updated_at"))


def verify_confirmation(record):
    if record is not None:
        evidence = verify_json_contract(record.evidence, schema_version=record.evidence_schema_version,
            size_bytes=record.evidence_size_bytes, sha256=record.evidence_sha256,
            max_bytes=execution_configuration_max_bytes()).value
        if evidence.get("fingerprint") != record.fingerprint or evidence.get("policy_sha256") != record.policy_sha256:
            raise ValidationError("资源确认记录与原评估不一致，请核对记录")
    return record


def resource_review(preparation, request, root, policy):
    """Same exposure before allocation and before submission; no second ledger."""
    from studio.models import ExecutionResourceConfirmation
    value = policy.value["values"]
    budget = root.get("budget")
    if not isinstance(budget, dict):
        return None  # The collector's unknown/invalid-accounting gate remains.
    issues = []
    projected, currency = budget.get("projected_total"), budget.get("currency")
    threshold = value["budget_by_currency"].get(currency)
    if projected is not None and Decimal(projected) > 0 and threshold is None:
        issues.append({"code": "budget_not_configured", "message": f"系统未设置 {currency} 预算提醒值，请核对本次费用并人工确认。"})
    elif projected is not None and threshold is not None and Decimal(projected) > Decimal(threshold):
        issues.append({"code": "budget_exceeded", "message": f"预计本轮累计 {projected} {currency}，超过系统提醒值 {threshold} {currency}。"})
    task = root.get("task_budget")
    calls = task["allocated"] + task["requested"] if task else sum(row["count"] for row in budget.get("prior_totals", [])) + 1
    if calls > value["call_count"]:
        issues.append({"code": "call_count_exceeded", "message": f"本次调用后累计 {calls} 次，超过系统提醒值 {value['call_count']} 次。"})
    transport = request.value["transport"]
    tokens = transport.get("parameters", {}).get("values", {}).get("max_tokens")
    if type(tokens) is int and tokens > value["output_length"]:
        issues.append({"code": "output_tokens_exceeded", "message": f"本次请求输出额度 {tokens} token，超过系统提醒值 {value['output_length']} token。"})
    # Do not include mutable preparation revision or own reservation: these
    # change during freeze/allocation without changing financial exposure.
    # Field compilation audit revisions can change when the same reviewed
    # fields are checked again. Bind the exact outbound request and target,
    # not those incidental compilation timestamps/revision wrappers.
    submission = {key: transport[key] for key in ("execution_instance_version_id", "target_fingerprint",
        "operation", "driver_type", "idempotency_key", "parameters", "payload", "output")}
    basis = json_contract({"preparation_id": str(preparation.pk), "intent_sha256": preparation.intent_sha256,
        "provider_request_sha256": json_contract(submission, max_bytes=execution_request_max_bytes()).sha256,
        "policy_sha256": policy.sha256, "projected_total": projected, "currency": currency,
        "projected_calls": calls, "estimate": {key: budget["estimate"][key] for key in ("amount", "currency", "basis")}}, max_bytes=execution_configuration_max_bytes())
    # Concurrent allocations below the call limit do not change what the
    # owner approved. Retain the exact observed count as evidence, but bind
    # the fingerprint to it only when that count actually needs approval.
    fingerprint = json_contract({**basis.value,
        "projected_calls": calls if calls > value["call_count"] else None},
        max_bytes=execution_configuration_max_bytes()).sha256
    confirmation = ExecutionResourceConfirmation.objects.filter(preparation=preparation, fingerprint=fingerprint).first() if issues else None
    verify_confirmation(confirmation)
    return {"fingerprint": fingerprint, "policy_sha256": policy.sha256, "policy": policy.value,
        "basis": basis.value, "issues": issues, "confirmed": confirmation is not None,
        "confirmation_id": str(confirmation.pk) if confirmation else None,
        "confirmed_at": confirmation.created_at.isoformat() if confirmation else None}


def confirm_resource_review(*, preparation_id, expected_preparation_revision, fingerprint, actor):
    from studio.models import ExecutionResourceConfirmation
    from studio.services.execution_preparation_fields import _locked_preparations
    from studio.services.execution_preparations import read_execution_preparation_detail
    from studio.services.registry_reference_lock import registry_reference_write_guard
    with registry_reference_write_guard(), _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        if preparation.status == "cancelled":
            raise ValidationError("准备已取消，不能确认")
        detail = read_execution_preparation_detail(preparation)
        review = (detail.evaluation or {}).get("root_checks", {}).get("resource_review")
        if (not review or not review["issues"] or review["fingerprint"] != fingerprint
                or review["policy_sha256"] != read_resource_policy().sha256):
            raise ValidationError("资源策略或评估已改变，请重新检查后确认", code="execution_preparation_conflict")
        existing = ExecutionResourceConfirmation.objects.filter(preparation=preparation, fingerprint=fingerprint).first()
        verify_confirmation(existing)
        if existing:
            return preparation  # Idempotent acknowledgement, never dispatch.
        if preparation.revision != expected_preparation_revision:
            raise ValidationError("准备已变化，请重新读取后确认", code="execution_preparation_conflict")
        record = ExecutionResourceConfirmation(preparation=preparation, fingerprint=fingerprint,
            policy_sha256=review["policy_sha256"], confirmed_by=actor)
        assign_json_contract(record, "evidence", review, max_bytes=execution_configuration_max_bytes())
        record.full_clean()
        record.save()
        # Confirming never wakes a worker. A separate explicit continue action
        # reevaluates prices/exposure/policy and all existing authorization gates.
        preparation.next_check_at = None
        preparation.claim_token = preparation.claim_until = None
        preparation.revision += 1
        preparation.save(update_fields=("next_check_at", "claim_token", "claim_until", "revision", "updated_at"))
        return preparation


def describe_resource_review(preparation, detail):
    from studio.models import ExecutionResourceConfirmation
    review = (detail.evaluation or {}).get("root_checks", {}).get("resource_review")
    if not review or not review["issues"]:
        return None
    confirmation = ExecutionResourceConfirmation.objects.filter(preparation=preparation, fingerprint=review["fingerprint"]).first()
    verify_confirmation(confirmation)
    return {"fingerprint": review["fingerprint"], "policy_current": read_resource_policy().sha256 == review["policy_sha256"],
        "issues": review["issues"], "projected_calls": review["basis"]["projected_calls"],
        "projected_total": review["basis"]["projected_total"], "currency": review["basis"]["currency"],
        "confirmed": confirmation is not None, "confirmed_at": confirmation.created_at.isoformat() if confirmation else None}
