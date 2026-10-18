from __future__ import annotations

from copy import copy
import math
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from studio.models import GenerationAttempt, GenerationAttemptResolution, ModelDeployment, ProductionRun
from studio.services.json_contracts import json_contract
from studio.services.model_profile_contracts import verified_frozen_model_profile_specification
from studio.services.provider_registry import credential_binding_sha256, deployment_supports_script_generation
from studio.services.security_boundaries import boundary_int
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS


TEXT_OPERATION = "script.generate"
TEXT_UNIT_KEY = "series-script"
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES

TEXT_OPERATIONS = frozenset({
    *FIELD_OPERATION_MODES,
    *PRODUCTION_TEXT_OPERATIONS,
})
TERMINAL_RUN_STATUSES = frozenset({
    ProductionRun.Status.SUCCEEDED,
    ProductionRun.Status.FAILED,
    ProductionRun.Status.CANCELLED,
})


def _text_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.text_max_bytes")


def append_attempt_resolution(
    attempt: GenerationAttempt,
    *,
    outcome: str,
    evidence: dict[str, Any],
    note: str = "",
    decided_by=None,
) -> GenerationAttemptResolution:
    contract = json_contract(evidence, max_bytes=_text_snapshot_max_bytes())
    revision = (
        attempt.resolutions.aggregate(value=Max("revision"))["value"] or 0
    ) + 1
    return GenerationAttemptResolution.objects.create(
        attempt=attempt,
        revision=revision,
        outcome=outcome,
        provider_request_id=attempt.provider_request_id,
        evidence=contract.value,
        evidence_schema_version=contract.schema_version,
        evidence_size_bytes=contract.size_bytes,
        evidence_sha256=contract.sha256,
        note=str(note or "")[:4000],
        decided_by=decided_by,
    )


def frozen_text_deployment(snapshot: dict[str, Any]) -> ModelDeployment:
    route = snapshot.get("route")
    if not isinstance(route, dict):
        raise ValidationError("文本 attempt 缺少冻结 route")
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(pk=route.get("model_deployment_id"), status=ModelDeployment.Status.ACTIVE).first()
    if deployment is None:
        raise ValidationError("冻结的文本模型部署不存在或未启用")
    provider = deployment.profile_version.provider
    checks = {
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "model_profile_version_id": str(deployment.profile_version_id),
        "provider_id": str(provider.id),
        "driver_type": provider.driver_type,
    }
    for key, current in checks.items():
        if str(route.get(key) or "") != str(current or ""):
            raise ValidationError(f"冻结 route 的 {key} 已漂移")
    if not provider.is_active or deployment.profile_version.status != deployment.profile_version.Status.PUBLISHED:
        raise ValidationError("冻结 route 的 provider/model 已停用")
    provider_config = route.get("provider_config")
    deployment_config = route.get("deployment_config")
    model_specification = verified_frozen_model_profile_specification(
        deployment.profile_version,
        route,
    )
    if (
        not isinstance(provider_config, dict)
        or not isinstance(deployment_config, dict)
    ):
        raise ValidationError("冻结 route 缺少 provider/deployment/model specification")
    provider_contract = json_contract(
        provider_config,
        max_bytes=_text_snapshot_max_bytes(),
    )
    deployment_contract = json_contract(
        deployment_config,
        max_bytes=_text_snapshot_max_bytes(),
    )
    if provider_contract.sha256 != route.get("provider_config_sha256"):
        raise ValidationError("冻结 route 的 provider_config hash 无效")
    if deployment_contract.sha256 != route.get("deployment_config_sha256"):
        raise ValidationError("冻结 route 的 deployment_config hash 无效")
    specification_contract = json_contract(
        model_specification,
        max_bytes=_text_snapshot_max_bytes(),
    )
    # Build an in-memory execution view.  Endpoint/model/timeout are immutable run
    # inputs; only enablement and credential binding are deliberately read live.
    frozen_provider = copy(provider)
    frozen_provider.driver_config = provider_config
    frozen_provider.driver_config_schema_version = provider_contract.schema_version
    frozen_provider.driver_config_size_bytes = provider_contract.size_bytes
    frozen_provider.driver_config_sha256 = provider_contract.sha256
    frozen_profile = copy(deployment.profile_version)
    frozen_profile.provider = frozen_provider
    frozen_profile.model_identifier = str(route.get("model_identifier") or "")
    frozen_profile.specification = model_specification
    frozen_profile.specification_schema_version = specification_contract.schema_version
    frozen_profile.specification_size_bytes = specification_contract.size_bytes
    frozen_profile.specification_sha256 = specification_contract.sha256
    frozen_deployment = copy(deployment)
    frozen_deployment.profile_version = frozen_profile
    frozen_deployment.runtime_config = deployment_config
    frozen_deployment.runtime_config_schema_version = deployment_contract.schema_version
    frozen_deployment.runtime_config_size_bytes = deployment_contract.size_bytes
    frozen_deployment.runtime_config_sha256 = deployment_contract.sha256
    if not deployment_supports_script_generation(frozen_deployment):
        raise ValidationError("冻结 deployment 没有安全文本 transport")
    return frozen_deployment


def queued_text_attempts():
    return GenerationAttempt.objects.filter(operation__in=TEXT_OPERATIONS,
        status="queued", committed_cost__isnull=False, stage_run__status="running",
        stage_run__production_run__status="running").exclude(error_code="execution_resource_confirmation_required").filter(
            Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=timezone.now()))


def claim_next_text_attempt(worker_id: str) -> GenerationAttempt | None:
    from studio.models import ExecutionPreparation
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 160:
        raise ValidationError("需要有效的原worker身份")
    query = queued_text_attempts()
    candidate = query.order_by("created_at", "pk").only("id", "stage_run_id", "preparation_id").first()
    if candidate is None:
        return None
    # Do not lock Attempt first: all prepared execution mutations share the
    # original Run -> Stage -> Preparation -> Attempt order.
    with locked_preparation_owner_rows([("stage_run_id", candidate.stage_run_id)]):
        ExecutionPreparation.objects.select_for_update().get(pk=candidate.preparation_id)
        attempt = query.select_for_update(of=("self",), skip_locked=True).filter(pk=candidate.pk).first()
        if attempt is None:
            return None
        attempt.status, attempt.worker_id, attempt.claim_token = "running", worker_id, uuid.uuid4()
        attempt.claimed_at = attempt.heartbeat_at = timezone.now()
        attempt.save(update_fields=("status", "worker_id", "claim_token", "claimed_at", "heartbeat_at", "updated_at"))
        return attempt


@transaction.atomic
def release_text_attempt_claim(attempt: GenerationAttempt) -> bool:
    """Return a locally-blocked, not-yet-submitted claim to the queue."""

    updated = GenerationAttempt.objects.filter(
        pk=attempt.pk,
        status=GenerationAttempt.Status.RUNNING,
        claim_token=attempt.claim_token,
    ).update(
        status=GenerationAttempt.Status.QUEUED,
        worker_id="",
        claim_token=None,
        claimed_at=None,
        heartbeat_at=None,
        updated_at=timezone.now(),
    )
    return updated == 1


def recover_stale_text_attempts(minutes: float) -> dict[str, int]:
    from studio.models import ExecutionPreparation
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(float(minutes)) or minutes <= 0:
        raise ValueError("文本 attempt 陈旧任务回收阈值必须是正有限分钟数")
    cutoff = timezone.now() - timedelta(minutes=float(minutes))
    query = GenerationAttempt.objects.filter(operation__in=TEXT_OPERATIONS,
        status__in=("running", "submitting", "submitted", "cancel_requested"), heartbeat_at__lt=cutoff)
    identities = list(query.order_by("stage_run_id", "pk").values_list("id", "stage_run_id", "preparation_id"))
    requeued, uncertain = 0, 0
    for attempt_id, stage_id, preparation_id in identities:
        with locked_preparation_owner_rows([("stage_run_id", stage_id)]) as owners:
            stage = owners[("stage_run_id", str(stage_id))]
            ExecutionPreparation.objects.select_for_update().get(pk=preparation_id, stage_run=stage)
            attempt = query.select_for_update(of=("self",)).filter(pk=attempt_id).first()
            if attempt is None:
                continue
            run = ProductionRun.objects.get(pk=stage.production_run_id)
            if run.status in TERMINAL_RUN_STATUSES or stage.status == "cancelled" or attempt.ignore_late_result or attempt.status == "cancel_requested":
                submitted = attempt.submission_started_at is not None
                attempt.status, attempt.ignore_late_result = "failed" if submitted else "cancelled", True
                attempt.error_code = "uncertain_remote_response" if submitted else "execution_cancelled_before_submit"
                attempt.error_message = "准备已停止；失联调用的远端结果仍不确定，保留预留，不会重新提交。" if submitted else "准备已停止；未提交的陈旧任务已取消。"
                if attempt.submission_started_at is None and attempt.committed_cost is not None:
                    if attempt.committed_cost > run.committed_cost_total:
                        raise ValidationError("陈旧未提交任务的费用预留不一致")
                    run.committed_cost_total -= attempt.committed_cost
                    attempt.committed_cost = Decimal("0")
                    run.save(update_fields=("committed_cost_total", "updated_at"))
                attempt.finished_at = timezone.now()
                attempt.claim_token, attempt.worker_id, attempt.claimed_at = None, "", None
                attempt.save()
                append_attempt_resolution(attempt, outcome=GenerationAttemptResolution.Outcome.CANCEL_REQUESTED_IGNORE_LATE,
                    evidence={"schema_version": 1, "attempt_id": str(attempt.pk), "request_sha256": attempt.request_sha256,
                        "execution_submitted": attempt.submission_started_at is not None, "actual_cost": None,
                        "actual_cost_source": "unknown", "reason": "stopped_run_stale_attempt"},
                    note="Stale claim stopped without adopting output or inferring billing")
                uncertain += int(submitted)
                continue
            if attempt.status == "running" and attempt.submission_started_at is None:
                attempt.status, attempt.worker_id, attempt.claim_token = "queued", "", None
                attempt.claimed_at = attempt.heartbeat_at = None
                attempt.save(update_fields=("status", "worker_id", "claim_token", "claimed_at", "heartbeat_at", "updated_at"))
                requeued += 1
                continue
            attempt.status, attempt.error_code = "failed", "uncertain_remote_response"
            attempt.error_message = "worker 在持久化提交边界后失联；禁止自动重复 POST。"
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=("status", "error_code", "error_message", "finished_at", "updated_at"))
            stage.status, stage.reason_code, stage.reason = "blocked", attempt.error_code, attempt.error_message
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status, run.blocked_code, run.blocked_reason = "blocked", stage.reason_code, stage.reason
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            uncertain += 1
    return {"requeued": requeued, "uncertain": uncertain}


def fail_unhandled_text_attempt(attempt: GenerationAttempt, exc: Exception) -> None:
    current = GenerationAttempt.objects.filter(pk=attempt.pk).only(
        "status", "claim_token", "operation", "stage_run_id", "preparation_id", "submission_started_at",
    ).first()
    if current is None or current.claim_token != attempt.claim_token:
        return
    if current.operation in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES}:
        from studio.services.execution_production_text_worker import finish_production_text
        finish_production_text(current, status="failed",
            code="uncertain_remote_response" if current.submission_started_at is not None else "execution_worker_failed")
        return
    raise ValidationError("text worker 收到不支持的 operation")


def process_text_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    if attempt.operation in TEXT_OPERATIONS:
        from studio.services.execution_production_text_worker import process_prepared_production_text
        return process_prepared_production_text(attempt)
    raise ValidationError("text worker 收到不支持的 operation")
