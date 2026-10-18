from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    ModelDeployment,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
)
from studio.services.deployment_smoke_contract import (
    DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
)
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import normalize_audit_note


def _deployment_smoke_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def has_operator_resolution(attempt) -> bool:
    outcomes = ModelDeploymentSmokeAttemptResolution.OPERATOR_OUTCOMES
    cached = getattr(attempt, "_prefetched_objects_cache", {}).get("resolutions")
    if cached is not None:
        return any(row.outcome in outcomes for row in cached)
    return attempt.resolutions.filter(outcome__in=outcomes).exists()


def unresolved_deployment_smoke_attempt(
    deployment: ModelDeployment,
    *,
    operation: str | None = None,
) -> ModelDeploymentSmokeAttempt | None:
    """Return the newest single-submit boundary awaiting operator evidence."""

    queryset = ModelDeploymentSmokeAttempt.objects.filter(
        deployment=deployment,
        status__in=(
            ModelDeploymentSmokeAttempt.Status.SUBMITTING,
            ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
        ),
    ).without_operator_resolution()
    if operation:
        queryset = queryset.filter(operation=operation)
    return queryset.order_by("-created_at").first()


@transaction.atomic
def resolve_deployment_smoke_attempt(
    deployment: ModelDeployment,
    *,
    smoke_attempt_id,
    outcome: str,
    note: str,
    user,
    provider_request_id: str = "",
    actual_cost: Decimal | None = None,
    cost_currency: str = "",
    expected_operation: str | None = None,
) -> ModelDeploymentSmokeAttemptResolution:
    """Append immutable operator evidence without replaying the paid request."""

    if not getattr(user, "is_staff", False):
        raise ValidationError("只有 staff 可以消歧 deployment smoke")
    normalized_note = normalize_audit_note(note)
    if outcome not in ModelDeploymentSmokeAttemptResolution.OPERATOR_OUTCOMES:
        raise ValidationError({"outcome": "smoke 消歧结果无效"})
    request_id = str(provider_request_id or "").strip()
    if len(request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})
    parsed_cost: Decimal | None = None
    if actual_cost is not None:
        try:
            parsed_cost = Decimal(str(actual_cost))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"actual_cost": "实际成本必须是有限非负数"}) from exc
        if not parsed_cost.is_finite() or parsed_cost < 0:
            raise ValidationError({"actual_cost": "实际成本必须是有限非负数"})
    if outcome == ModelDeploymentSmokeAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        if request_id:
            raise ValidationError({
                "provider_request_id": "确认未提交时不能提供 provider request id",
            })
        if parsed_cost not in (None, Decimal("0")):
            raise ValidationError({"actual_cost": "确认未提交时实际成本必须为空或 0"})
    normalized_currency = str(cost_currency or "").strip().upper()
    if parsed_cost and not normalized_currency:
        raise ValidationError({"cost_currency": "实际成本大于 0 时必须提供币种"})
    if len(normalized_currency) > 12:
        raise ValidationError({"cost_currency": "币种过长"})

    # Match the original worker's owner -> attempt -> preparation lock order.
    ModelDeployment.objects.select_for_update().get(pk=deployment.pk)
    try:
        attempt = ModelDeploymentSmokeAttempt.objects.select_for_update().get(
            pk=smoke_attempt_id,
            deployment=deployment,
        )
    except (ModelDeploymentSmokeAttempt.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError({"smoke_attempt_id": "smoke attempt 不存在"}) from exc
    from studio.models import ExecutionPreparation
    from studio.services.execution_smoke_evidence import read_prepared_smoke_context
    try:
        prep = ExecutionPreparation.objects.select_for_update().get(deployment_smoke=attempt)
    except ExecutionPreparation.DoesNotExist as exc:
        raise ValidationError({"smoke_attempt_id": "烟测缺少原始执行准备，不能人工消歧"}) from exc
    _detail, identity = read_prepared_smoke_context(attempt, prep)
    prepared_identity = {"scope": attempt.scope, "preparation_id": str(prep.pk),
        "execution_instance_version_id": identity.version_id, "prepared_request_sha256": prep.request_sha256,
        "definition_sha256": identity.definition_sha256}
    if expected_operation and attempt.operation != expected_operation:
        raise ValidationError({"smoke_attempt_id": "smoke attempt operation 不匹配"})
    if attempt.status not in {
        ModelDeploymentSmokeAttempt.Status.SUBMITTING,
        ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
    }:
        raise ValidationError("只有 submitting/uncertain smoke attempt 可以人工消歧")
    if has_operator_resolution(attempt):
        raise ValidationError("该 smoke attempt 已完成消歧，审计事实不可覆盖")

    provider_request_hash = (
        hashlib.sha256(request_id.encode("utf-8")).hexdigest() if request_id else ""
    )
    evidence = {
        "schema_version": 1,
        "smoke_attempt_id": str(attempt.id),
        "deployment_id": str(attempt.deployment_id),
        "operation": attempt.operation,
        "attempt_status": attempt.status,
        "request_sha256": attempt.request_sha256,
        "target_sha256": attempt.target_sha256,
        "outcome": outcome,
        "provider_request_id_sha256": provider_request_hash,
        "actual_cost": str(parsed_cost) if parsed_cost is not None else None,
        "cost_currency": normalized_currency,
        "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
        "resolved_at": timezone.now().isoformat(),
        **prepared_identity,
    }
    evidence_contract = json_contract(
        evidence,
        max_bytes=_deployment_smoke_snapshot_max_bytes(),
    )
    revision = (attempt.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1
    return ModelDeploymentSmokeAttemptResolution.objects.create(
        attempt=attempt,
        revision=revision,
        outcome=outcome,
        provider_request_id_sha256=provider_request_hash,
        evidence=evidence_contract.value,
        evidence_schema_version=evidence_contract.schema_version,
        evidence_size_bytes=evidence_contract.size_bytes,
        evidence_sha256=evidence_contract.sha256,
        note=normalized_note,
        decided_by=user,
    )
