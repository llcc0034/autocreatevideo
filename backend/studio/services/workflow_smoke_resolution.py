from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    ModelDeployment,
    WorkflowSmokeAttempt,
    WorkflowSmokeAttemptResolution,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import normalize_audit_note


class WorkflowSmokeResolutionConflict(ValidationError):
    code = "workflow_smoke_resolution_conflict"

    def __init__(self, message: str):
        super().__init__(message, code=type(self).code)


def unresolved_workflow_smoke_attempts():
    """Return unresolved remote workflow executions that still own the GPU gate."""

    return WorkflowSmokeAttempt.objects.filter(
        status=WorkflowSmokeAttempt.Status.UNCERTAIN,
        resolutions__isnull=True,
    )


def unresolved_workflow_smoke_attempt(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
) -> WorkflowSmokeAttempt | None:
    return unresolved_workflow_smoke_attempts().filter(
        workflow_version=version,
        deployment=deployment,
    ).order_by("created_at").first()


@transaction.atomic
def resolve_workflow_smoke_attempt(
    attempt: WorkflowSmokeAttempt,
    *,
    outcome: str,
    note: str,
    provider_request_id: str,
    user,
) -> tuple[WorkflowSmokeAttemptResolution, bool]:
    """Append one immutable operator conclusion without synthesizing success."""

    if not getattr(user, "is_staff", False):
        raise ValidationError("只有 staff 可以消歧 workflow smoke")
    normalized_note = normalize_audit_note(note)
    if outcome not in set(WorkflowSmokeAttemptResolution.Outcome.values):
        raise ValidationError({"outcome": "workflow smoke 消歧结果无效"})
    supplied_request_id = str(provider_request_id or "").strip()
    if len(supplied_request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})

    seed = WorkflowSmokeAttempt.objects.only(
        "workflow_version_id", "deployment_id",
    ).get(pk=attempt.pk)
    ModelDeployment.objects.select_for_update(of=("self",)).get(
        pk=seed.deployment_id,
    )
    WorkflowTemplateVersion.objects.select_for_update(of=("self",)).get(
        pk=seed.workflow_version_id,
    )
    attempt = WorkflowSmokeAttempt.objects.select_for_update(of=("self",)).get(
        pk=attempt.pk,
    )
    if attempt.status != WorkflowSmokeAttempt.Status.UNCERTAIN:
        raise WorkflowSmokeResolutionConflict(
            "只有 unresolved uncertain workflow smoke attempt 可以人工消歧",
        )

    existing = attempt.resolutions.order_by("revision").first()
    known_request_id = str(attempt.provider_request_id or "").strip()
    if known_request_id and supplied_request_id and supplied_request_id != known_request_id:
        raise ValidationError({
            "provider_request_id": "provider request id 与冻结 attempt 不一致",
        })
    effective_request_id = known_request_id or supplied_request_id
    if outcome == WorkflowSmokeAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        if effective_request_id or attempt.submitted_at:
            raise ValidationError({
                "outcome": "已有远端 request 事实，不能确认未提交",
            })
    elif not effective_request_id:
        raise ValidationError({
            "provider_request_id": "确认远端终态必须提供或复用冻结 request id",
        })
    from studio.models import ExecutionPreparation
    from studio.services.execution_smoke_evidence import read_prepared_smoke_context
    try:
        prep = ExecutionPreparation.objects.select_for_update().get(workflow_smoke=attempt)
    except ExecutionPreparation.DoesNotExist as exc:
        raise ValidationError("烟测缺少原始执行准备，不能人工消歧") from exc
    detail, identity = read_prepared_smoke_context(attempt, prep)
    expected_token = detail.prepared_request["transport"]["idempotency_key"]
    prepared_identity = {"scope": attempt.scope, "preparation_id": str(prep.pk),
        "execution_instance_version_id": identity.version_id,
        "prepared_request_sha256": prep.request_sha256, "definition_sha256": identity.definition_sha256}
    if effective_request_id and effective_request_id != expected_token:
        raise ValidationError({"provider_request_id": "provider request id 不属于该次冻结请求的 submission token"})

    request_hash = (
        hashlib.sha256(effective_request_id.encode("utf-8")).hexdigest()
        if effective_request_id else ""
    )
    if existing is not None:
        if (
            existing.outcome == outcome
            and existing.note == normalized_note
            and existing.provider_request_id_sha256 == request_hash
        ):
            return existing, False
        raise WorkflowSmokeResolutionConflict(
            "该 workflow smoke attempt 已完成不同结论的人工消歧",
        )

    evidence = {
        "schema_version": 1,
        "workflow_smoke_attempt_id": str(attempt.id),
        "workflow_version_id": str(attempt.workflow_version_id),
        "deployment_id": str(attempt.deployment_id),
        "attempt_status": attempt.status,
        "attempt_error_code": attempt.error_code,
        "graph_sha256": attempt.graph_sha256,
        "execution_contract_sha256": attempt.execution_contract_sha256,
        "validation_evidence_sha256": attempt.validation_evidence_sha256,
        "target_sha256": attempt.target_sha256,
        "fixture_values_sha256": attempt.fixture_values_sha256,
        "fixture_sha256": attempt.fixture_sha256,
        "provider_request_id_sha256": request_hash,
        "outcome": outcome,
        "resolved_at": timezone.now().isoformat(),
        **prepared_identity,
    }
    contract = json_contract(
        evidence,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    )
    revision = (attempt.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1
    resolution = WorkflowSmokeAttemptResolution.objects.create(
        attempt=attempt,
        revision=revision,
        outcome=outcome,
        provider_request_id_sha256=request_hash,
        evidence=contract.value,
        evidence_schema_version=contract.schema_version,
        evidence_size_bytes=contract.size_bytes,
        evidence_sha256=contract.sha256,
        note=normalized_note,
        decided_by=user,
    )
    if outcome == WorkflowSmokeAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        # This is an explicit operator conclusion, not an estimated settlement.
        # The original uncertain result is retained, while its held cost is zero.
        attempt.actual_cost = Decimal("0")
        attempt.save(update_fields=("actual_cost", "updated_at"))
    return resolution, True
