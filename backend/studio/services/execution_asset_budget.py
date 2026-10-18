"""Current asset-root budget projection from original jobs, with no new ledger.

Must be called under locked_preparation_owners. That guard includes the real
parent root for auxiliary calls; allocation/settlement will use the same root
lock when dispatch is connected. This module does not queue or charge a call.
"""
from decimal import Decimal, InvalidOperation
import re

from django.core.exceptions import ValidationError
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When

from studio.models import AssetGenerationJob, ExecutionPreparation
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.provider_registry import driver_for
from studio.services.execution_preparation_owners import nonproduction_preparation_root


def _money(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError("费用必须是明确的非负数值，未知不能用0代替")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("费用数值无效") from exc
    if not result.is_finite() or result < 0 or result >= Decimal("100000000"):
        raise ValidationError("费用必须在正式金额字段可记录范围内")
    return result.quantize(Decimal("0.000001"))


def estimate_execution_request(deployment, transport):
    """Use the original driver pricing declaration, never call the provider."""
    if not isinstance(transport, JsonContract):
        raise ValidationError("费用估算必须使用本次核对后的传输合同")
    request = verify_json_contract(transport.value, schema_version=transport.schema_version, size_bytes=transport.size_bytes,
        sha256=transport.sha256, max_bytes=execution_request_max_bytes())
    value = request.value
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict) or not value.get("operation"):
        raise ValidationError("费用估算缺少明确请求")
    estimate = driver_for(deployment.provider).estimate(deployment, value["operation"], value["payload"])
    if not isinstance(estimate, dict) or type(estimate.get("schema_version")) is not int or estimate["schema_version"] != 1:
        raise ValidationError("模型驱动没有返回有效的费用估算合同")
    amount = _money(estimate.get("amount"))
    currency = estimate.get("currency")
    if not isinstance(currency, str) or (currency and not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", currency)) or (amount and not currency):
        raise ValidationError("费用币种缺失或非法，不能截断或推断换算")
    if not isinstance(estimate.get("basis"), str) or not estimate["basis"]:
        raise ValidationError("费用估算缺少明确来源")
    return json_contract({"schema_version": 1, "transport_sha256": request.sha256,
        "amount": str(amount) if amount is not None else None, "currency": currency, "basis": estimate["basis"]},
        max_bytes=execution_configuration_max_bytes())


def asset_budget_totals_query(root, preparation, exclude_job_id):
    """Aggregate narrow facts in SQL; no JSON/history materialization or N+1."""
    possibly_spent = (Q(status__in=("queued", "submitting", "submitted", "running", "cancel_requested", "uncertain"))
        | Q(submission_started_at__isnull=False) | ~Q(provider_request_id="") | ~Q(provider_request_id_sha256=""))
    # A succeeded task without actual/estimated cost is unknown, not free.
    possibly_spent |= Q(status="succeeded")
    amount = Case(When(actual_cost__isnull=False, then=F("actual_cost")),
        When(possibly_spent, then=F("estimated_cost")), default=Value(Decimal("0")),
        output_field=DecimalField(max_digits=20, decimal_places=6))
    selected = Q(parent_preparation=preparation)
    if isinstance(root, AssetGenerationJob):
        selected |= Q(pk=root.pk)
    return AssetGenerationJob.objects.filter(selected).exclude(pk=exclude_job_id).order_by().values(
        "cost_currency").annotate(amount=Sum(amount), unknown=Count("id", filter=possibly_spent & Q(actual_cost__isnull=True, estimated_cost__isnull=True)), count=Count("id"))


def read_asset_budget_state(job, estimate):
    """Project the parent's own call plus all actual auxiliary attempts once.

    Retrying a root creates a separately authorized request, per the original
    retry contract. Explicit auxiliary retries remain in this parent's total.
    """
    return read_preparation_budget_state(job.execution_preparations.get(), estimate)


def read_preparation_budget_state(preparation, estimate):
    """One accounting projection for asset and Smoke roots and their children.

    The original attempt holds only its own call's reservation/settlement.
    Child costs remain on child Jobs and are aggregated, never copied upward.
    """
    if not isinstance(estimate, JsonContract):
        raise ValidationError("预算需要核对后的本次估算")
    pricing = verify_json_contract(estimate.value, schema_version=estimate.schema_version, size_bytes=estimate.size_bytes,
        sha256=estimate.sha256, max_bytes=execution_configuration_max_bytes()).value
    if not isinstance(pricing, dict) or set(pricing) != {"schema_version", "transport_sha256", "amount", "currency", "basis"}:
        raise ValidationError("预算估算合同不完整")
    amount, currency = _money(pricing["amount"]), pricing["currency"]
    job = nonproduction_preparation_root(preparation)
    parent = ExecutionPreparation.objects.get(pk=job.parent_preparation_id) if isinstance(job, AssetGenerationJob) and job.parent_preparation_id else preparation
    root = nonproduction_preparation_root(parent)
    from studio.services.asset_generation import _is_external
    external = job.is_external if isinstance(job, AssetGenerationJob) else _is_external(job.deployment)
    blockers, totals, committed, complete = [], [], Decimal("0"), True
    def block(code, message):
        blockers.append({"code": code, "message": message})
    if external and not root.allow_external_calls:
        block("execution_external_authorization_required", "本次根请求尚未授权外部模型调用")
    if amount is None:
        block("execution_estimate_unknown", "当前调用费用未知，请先配置正式模型价格")
    total_rows = list(asset_budget_totals_query(root, parent, job.pk if isinstance(job, AssetGenerationJob) else None))
    if not isinstance(root, AssetGenerationJob) and preparation.pk != parent.pk:
        possible = root.status in {"queued", "submitting", "submitted", "running", "cancel_requested", "uncertain", "succeeded"} or root.submission_started_at is not None or bool(root.provider_request_id)
        prior_amount = root.actual_cost if root.actual_cost is not None else root.estimated_cost if possible else Decimal("0")
        total_rows.append({"cost_currency": root.cost_currency, "amount": prior_amount, "unknown": int(prior_amount is None), "count": 1})
    for row in total_rows:
        value = row["amount"] or Decimal("0")
        totals.append({"currency": row["cost_currency"], "amount": str(value), "unknown_count": row["unknown"], "count": row["count"]})
        if row["unknown"]:
            complete = False
            block("execution_prior_cost_unknown", "已有调用可能产生费用且金额未知，不能按零费用继续")
        if value and row["cost_currency"] != currency:
            complete = False
            block("execution_budget_currency_conflict", "主调用与辅助调用币种不同或缺失，不能合并预算或自动换算")
        else:
            committed += value
    projected = committed + amount if amount is not None and complete else None
    from studio.services.execution_resource_policy import read_resource_policy
    threshold = read_resource_policy().value["values"]["budget_by_currency"].get(currency)
    return json_contract({"schema_version": 1, "root_asset_job_id": str(root.pk) if isinstance(root, AssetGenerationJob) else None,
        "root_smoke_attempt_id": str(root.pk) if not isinstance(root, AssetGenerationJob) else None, "preparation_id": str(parent.pk),
        "job_id": str(job.pk), "estimate": pricing, "budget_limit": threshold,
        "allow_external_calls": root.allow_external_calls, "prior_totals": totals,
        "projected_total": str(projected) if projected is not None else None, "currency": currency, "blockers": blockers},
        max_bytes=execution_configuration_max_bytes())
