from __future__ import annotations

from datetime import timedelta
import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ProviderProfile,
)
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OPERATIONS,
    async_operation_contract,
    deployment_target_sha256,
    validate_async_model_profile,
    validate_standard_payload,
)
from studio.services.configuration import resolve_configuration
from studio.services.deployment_smoke import (
    execute_script_generation_smoke_request,
    execute_script_structure_smoke_request,
    prepare_script_generation_smoke_request,
    prepare_script_structure_smoke_request,
    record_deployment_capability_snapshot,
)
from studio.services.deployment_smoke_contract import (
    DEPLOYMENT_SMOKE_OPERATIONS,
    DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
    TEXT_DEPLOYMENT_SMOKE_OPERATIONS,
)
from studio.services.deployment_smoke_resolution import (
    unresolved_deployment_smoke_attempt,
)
from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
)
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError, driver_for
from studio.services.prompt_normalization import (
    execute_prompt_normalization_smoke_request,
    prepare_prompt_normalization_smoke_request,
)
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.remote_artifacts import verify_remote_artifacts_for_smoke
from studio.services.script_generation_contract import SCRIPT_GENERATE_OPERATION
from studio.services.script_structure import SCRIPT_STRUCTURE_OPERATION


ACTIVE = (
    ModelDeploymentSmokeAttempt.Status.QUEUED,
    ModelDeploymentSmokeAttempt.Status.SUBMITTING,
    ModelDeploymentSmokeAttempt.Status.SUBMITTED,
    ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED,
)


def _config(deployment):
    return {
        **(deployment.profile_version.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }


def _prepare_text_smoke(deployment: ModelDeployment, operation: str) -> dict:
    if operation == SCRIPT_GENERATE_OPERATION:
        return prepare_script_generation_smoke_request(deployment)
    if operation == PROMPT_NORMALIZE_OPERATION:
        return prepare_prompt_normalization_smoke_request(deployment)
    if operation == SCRIPT_STRUCTURE_OPERATION:
        return prepare_script_structure_smoke_request(deployment)
    raise ValidationError({"operation": "deployment 未声明该文本烟测能力"})


def _deadline_seconds(deployment: ModelDeployment, operation: str) -> float:
    config = _config(deployment)
    if operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS:
        return max(1.0, float(config.get("timeout_seconds", 60)))
    return max(10.0, float(config.get("max_poll_seconds", 3600)))


@transaction.atomic
def enqueue_deployment_smoke(
    deployment: ModelDeployment,
    *,
    operation: str,
) -> tuple[ModelDeploymentSmokeAttempt, bool]:
    deployment = ModelDeployment.objects.select_for_update().select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    existing = ModelDeploymentSmokeAttempt.objects.filter(
        deployment=deployment, operation=operation, status__in=ACTIVE,
    ).order_by("created_at").first()
    if existing is not None:
        return existing, False
    unresolved = unresolved_deployment_smoke_attempt(
        deployment,
        operation=operation,
    )
    if unresolved is not None:
        raise ValidationError({
            "code": "unresolved_smoke_submission",
            "detail": (
                f"已有 {operation} smoke 提交状态不确定，必须先人工消歧"
            ),
        })
    if deployment.status != ModelDeployment.Status.ACTIVE:
        raise ValidationError("只有 active deployment 可以执行烟测")
    attempt_id = uuid.uuid4()
    idempotency_key = f"deployment-smoke:{attempt_id}"
    if operation in ASYNC_MEDIA_OPERATIONS:
        if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
            raise ValidationError("async media 持久烟测只支持 async_rest deployment")
        operations = validate_async_model_profile(deployment.profile_version)
        if operation not in operations:
            raise ValidationError({"operation": "deployment 未声明该 async media 能力"})
        contract = async_operation_contract(deployment.profile_version, operation)
        payload = dict(contract["smoke_inputs"])
        payload.update({
            "model": deployment.profile_version.model_identifier,
            "schema_version": 1,
            "idempotency_key": idempotency_key,
        })
        validate_standard_payload(contract, payload)
        estimate = driver_for(deployment.provider).estimate(deployment, operation, payload)
        if not isinstance(estimate, dict) or estimate.get("amount") is None:
            raise ValidationError("真实烟测前必须配置可确定的低成本 pricing")
        prepared = {
            "request_snapshot": json_contract({
                "schema_version": 1,
                "operation": operation,
                "output_kind": contract["output_kind"],
                "payload": payload,
            }).value,
            "request_sha256": json_contract(payload).sha256,
            "target_sha256": deployment_target_sha256(deployment),
            "estimated_cost": estimate.get("amount"),
            "cost_currency": str(estimate.get("currency") or "")[:12],
        }
    elif operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS:
        try:
            prepared = _prepare_text_smoke(deployment, operation)
        except ProviderDriverError as exc:
            raise ValidationError({
                "operation": f"文本烟测提交前校验失败: {exc.code}",
            }) from exc
    else:
        raise ValidationError({"operation": "deployment smoke operation 不受支持"})
    config = _config(deployment)
    hard_seconds = _deadline_seconds(deployment, operation)
    configured_idle = float(resolve_configuration(required_keys=(
        "runtime.task_idle_timeout_seconds",
    )).values["runtime.task_idle_timeout_seconds"])
    now = timezone.now()
    deadlines = initial_deadlines(
        idle_seconds=min(configured_idle, hard_seconds),
        hard_seconds=hard_seconds,
        now=now,
        marker="phase:queued",
    )
    try:
        attempt = ModelDeploymentSmokeAttempt.objects.create(
            id=attempt_id,
            deployment=deployment,
            operation=operation,
            status=ModelDeploymentSmokeAttempt.Status.QUEUED,
            request_sha256=prepared["request_sha256"],
            target_sha256=prepared["target_sha256"],
            estimated_cost=prepared.get("estimated_cost"),
            cost_currency=str(prepared.get("cost_currency") or "")[:12],
            idempotency_key=idempotency_key,
            request_snapshot=prepared["request_snapshot"],
            submission_started_at=now,
            deadline_started_at=deadlines.started_at,
            hard_deadline_at=deadlines.hard_deadline_at,
            idle_deadline_at=deadlines.idle_deadline_at,
            last_meaningful_progress_at=deadlines.last_meaningful_progress_at,
            progress_marker=deadlines.progress_marker,
        )
    except IntegrityError:
        return ModelDeploymentSmokeAttempt.objects.get(
            deployment=deployment, operation=operation, status__in=ACTIVE,
        ), False
    return attempt, True


def enqueue_async_deployment_smoke(
    deployment: ModelDeployment,
    *,
    operation: str,
) -> tuple[ModelDeploymentSmokeAttempt, bool]:
    """Compatibility alias; all operations now share the durable queue."""

    return enqueue_deployment_smoke(deployment, operation=operation)


@transaction.atomic
def claim_next_deployment_smoke(worker_id: str) -> ModelDeploymentSmokeAttempt | None:
    now = timezone.now()
    attempt = (
        ModelDeploymentSmokeAttempt.objects.select_for_update()
        .select_related("deployment__profile_version__provider")
        .filter(
            operation__in=DEPLOYMENT_SMOKE_OPERATIONS,
            status__in=ACTIVE,
            claim_token__isnull=True,
        )
        .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        .order_by("created_at")
        .first()
    )
    if attempt is None:
        return None
    attempt.claim_token = uuid.uuid4()
    attempt.worker_id = str(worker_id)[:160]
    attempt.claimed_at = now
    attempt.heartbeat_at = now
    attempt.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return attempt


def _release_schedule(attempt, claim_token, *, remote_status: str, progress_fraction=None):
    config = _config(attempt.deployment)
    with transaction.atomic():
        locked = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token:
            return locked
        now = timezone.now()
        normalized = str(remote_status or "")[:120]
        marker = ""
        if normalized and normalized != locked.remote_status:
            marker = f"status:{normalized}"
        if progress_fraction is not None:
            previous = -1.0
            if "|progress:" in locked.progress_marker:
                try:
                    previous = float(locked.progress_marker.rsplit("|progress:", 1)[1])
                except (TypeError, ValueError):
                    pass
            if float(progress_fraction) > previous:
                marker = f"status:{normalized}|progress:{float(progress_fraction):.6f}"
        updates = meaningful_deadline_updates(
            locked,
            marker=marker,
            idle_seconds=min(
                float(config.get("max_poll_seconds", 3600)),
                float(resolve_configuration(required_keys=(
                    "runtime.task_idle_timeout_seconds",
                )).values["runtime.task_idle_timeout_seconds"]),
            ),
            now=now,
        ) if marker else {}
        for field, value in updates.items():
            setattr(locked, field, value)
        locked.remote_status = normalized
        locked.last_polled_at = now
        locked.poll_count += 1
        locked.next_poll_at = now + timedelta(seconds=float(config.get("poll_interval_seconds", 2)))
        locked.claim_token = None
        locked.worker_id = ""
        locked.claimed_at = None
        locked.heartbeat_at = now
        locked.save(update_fields=(
            "remote_status", "last_polled_at", "poll_count", "next_poll_at",
            "claim_token", "worker_id", "claimed_at", "heartbeat_at",
            *updates.keys(), "updated_at",
        ))
        return locked


def _finalize(
    attempt,
    claim_token,
    *,
    status,
    snapshot_status,
    evidence,
    code="",
    message="",
    provider_request_id_sha256="",
):
    with transaction.atomic():
        # Match enqueue's deployment -> attempt lock order.  The snapshot must
        # not be appended until the worker claim is proven current; otherwise
        # a recovered slow worker could leave orphan pass evidence.
        locked_deployment = ModelDeployment.objects.select_for_update().get(
            pk=attempt.deployment_id,
        )
        locked = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token:
            return locked
        snapshot = record_deployment_capability_snapshot(
            locked_deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=snapshot_status,
            operations=[attempt.operation],
            evidence=evidence,
            expected_target_sha256=attempt.target_sha256,
        )
        now = timezone.now()
        locked.status = status
        locked.final_snapshot = snapshot
        if provider_request_id_sha256:
            locked.provider_request_id_sha256 = str(provider_request_id_sha256)[:64]
        locked.error_code = str(code)[:100]
        locked.error_message = str(message)[:2000]
        locked.finished_at = now
        locked.heartbeat_at = now
        locked.next_poll_at = None
        locked.claim_token = None
        locked.worker_id = ""
        locked.claimed_at = None
        locked.save(update_fields=(
            "status", "final_snapshot", "error_code", "error_message", "finished_at",
            "provider_request_id_sha256", "heartbeat_at", "next_poll_at",
            "claim_token", "worker_id", "claimed_at",
            "updated_at",
        ))
        return locked


def _execute_text_smoke(attempt: ModelDeploymentSmokeAttempt) -> dict:
    if attempt.operation == SCRIPT_GENERATE_OPERATION:
        return execute_script_generation_smoke_request(
            attempt.deployment,
            attempt.request_snapshot,
        )
    if attempt.operation == PROMPT_NORMALIZE_OPERATION:
        return execute_prompt_normalization_smoke_request(
            attempt.deployment,
            attempt.request_snapshot,
        )
    if attempt.operation == SCRIPT_STRUCTURE_OPERATION:
        return execute_script_structure_smoke_request(
            attempt.deployment,
            attempt.request_snapshot,
        )
    raise ValidationError({"operation": "文本 deployment smoke operation 不受支持"})


def _finalize_text_result(attempt, claim_token, result):
    evidence = {
        **(result.get("evidence") if isinstance(result.get("evidence"), dict) else {}),
        "schema_version": 1,
        "smoke_attempt_id": str(attempt.id),
        "estimated_cost": (
            str(attempt.estimated_cost) if attempt.estimated_cost is not None else None
        ),
        "estimated_currency": attempt.cost_currency,
        "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
    }
    return _finalize(
        attempt,
        claim_token,
        status=str(result.get("status") or ModelDeploymentSmokeAttempt.Status.FAILED),
        snapshot_status=str(
            result.get("snapshot_status")
            or ModelDeploymentCapabilitySnapshot.Status.FAILED
        ),
        evidence=evidence,
        code=str(result.get("error_code") or ""),
        message=str(result.get("error_message") or ""),
        provider_request_id_sha256=str(
            result.get("provider_request_id_sha256") or ""
        ),
    )


def process_deployment_smoke(attempt: ModelDeploymentSmokeAttempt):
    claim_token = attempt.claim_token
    if claim_token is None:
        raise ValidationError("deployment smoke 未持有 claim")
    deployment = attempt.deployment
    driver = driver_for(deployment.provider)
    snapshot = attempt.request_snapshot
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
    if (
        not isinstance(payload, dict)
        or snapshot.get("operation") != attempt.operation
        or json_contract(payload).sha256 != attempt.request_sha256
    ):
        return _finalize(
            attempt, claim_token,
            status=ModelDeploymentSmokeAttempt.Status.FAILED,
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={"schema_version": 1, "execution_submitted": False, "error_code": "request_snapshot_invalid"},
            code="request_snapshot_invalid", message="deployment smoke 请求快照无效",
        )
    if deployment_target_sha256(deployment) != attempt.target_sha256:
        return _finalize(
            attempt,
            claim_token,
            status=ModelDeploymentSmokeAttempt.Status.FAILED,
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "error_code": "deployment_target_changed",
            },
            code="deployment_target_changed",
            message="deployment smoke 目标在提交前发生变化。",
        )
    kind = expired_kind(attempt)
    if kind and attempt.status not in (
        ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED,
    ):
        if not attempt.provider_request_id:
            return _finalize(
                attempt, claim_token,
                status=ModelDeploymentSmokeAttempt.Status.FAILED,
                snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                evidence={"schema_version": 1, "execution_submitted": False, "error_code": f"{kind}_timeout"},
                code=f"{kind}_timeout", message=f"烟测达到持久化 {kind} deadline",
            )
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=claim_token).update(
            status=ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED,
            cancel_requested_at=timezone.now(), updated_at=timezone.now(),
        )
        attempt.refresh_from_db()
    try:
        if attempt.status == ModelDeploymentSmokeAttempt.Status.QUEUED:
            with transaction.atomic():
                locked = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
                if locked.claim_token != claim_token:
                    return locked
                if locked.status != ModelDeploymentSmokeAttempt.Status.QUEUED:
                    return locked
                locked.status = ModelDeploymentSmokeAttempt.Status.SUBMITTING
                locked.submission_started_at = locked.submission_started_at or timezone.now()
                locked.save(update_fields=("status", "submission_started_at", "updated_at"))
            attempt.status = ModelDeploymentSmokeAttempt.Status.SUBMITTING
            if attempt.operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS:
                try:
                    return _finalize_text_result(
                        attempt,
                        claim_token,
                        _execute_text_smoke(attempt),
                    )
                except ValidationError as exc:
                    return _finalize(
                        attempt,
                        claim_token,
                        status=ModelDeploymentSmokeAttempt.Status.FAILED,
                        snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                        evidence={
                            "schema_version": 1,
                            "execution_submitted": False,
                            "error_code": "text_smoke_pre_submit_invalid",
                            "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
                        },
                        code="text_smoke_pre_submit_invalid",
                        message="文本烟测在远端调用前未通过冻结目标校验。",
                    )
            response = driver.invoke(deployment, "submit", payload)
            request_id = str(response.get("request_id") or "") if isinstance(response, dict) else ""
            if not request_id:
                raise ProviderDriverError(
                    "远端提交响应缺少 request_id",
                    code="submission_uncertain",
                    submission_uncertain=True,
                )
            with transaction.atomic():
                locked = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
                if locked.claim_token != claim_token:
                    return locked
                now = timezone.now()
                locked.status = ModelDeploymentSmokeAttempt.Status.SUBMITTED
                locked.provider_request_id = request_id[:191]
                locked.provider_request_id_sha256 = hashlib.sha256(request_id.encode()).hexdigest()
                locked.submitted_at = now
                locked.remote_status = str(response.get("status") or "submitted")[:120]
                updates = meaningful_deadline_updates(
                    locked,
                    marker="phase:submitted",
                    idle_seconds=min(
                        float(_config(deployment).get("max_poll_seconds", 3600)),
                        float(resolve_configuration(required_keys=(
                            "runtime.task_idle_timeout_seconds",
                        )).values["runtime.task_idle_timeout_seconds"]),
                    ),
                    now=now,
                )
                for field, value in updates.items():
                    setattr(locked, field, value)
                locked.next_poll_at = now
                locked.claim_token = None
                locked.worker_id = ""
                locked.claimed_at = None
                locked.heartbeat_at = now
                locked.save(update_fields=(
                    "status", "provider_request_id", "provider_request_id_sha256",
                    "submitted_at", "remote_status", "next_poll_at", "claim_token",
                    "worker_id", "claimed_at", "heartbeat_at", *updates.keys(), "updated_at",
                ))
                return locked
        if attempt.status == ModelDeploymentSmokeAttempt.Status.SUBMITTING:
            return _finalize(
                attempt, claim_token,
                status=ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
                snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                evidence={"schema_version": 1, "execution_submitted": True, "submission_uncertain": True, "error_code": "submission_uncertain"},
                code="submission_uncertain",
                message="worker 在单提交边界后恢复；禁止自动再次 POST，必须核验远端任务。",
            )
        if attempt.status == ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED:
            if not attempt.provider_request_id:
                return _finalize(
                    attempt, claim_token,
                    status=ModelDeploymentSmokeAttempt.Status.CANCELLED,
                    snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                    evidence={"schema_version": 1, "execution_submitted": False, "error_code": "cancelled"},
                    code="cancelled", message="烟测已取消",
                )
            response = driver.invoke(deployment, "cancel", {"request_id": attempt.provider_request_id})
            return _finalize(
                attempt, claim_token,
                status=ModelDeploymentSmokeAttempt.Status.CANCELLED,
                snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                evidence={"schema_version": 1, "execution_submitted": True, "error_code": "cancelled"},
                code="cancelled", message="远端取消已请求",
            )
        response = driver.invoke(deployment, "poll", {"request_id": attempt.provider_request_id})
        remote_status = str(response.get("status") or "")
        if response.get("terminal_failure") or response.get("terminal_cancelled"):
            return _finalize(
                attempt, claim_token,
                status=ModelDeploymentSmokeAttempt.Status.FAILED,
                snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
                evidence={"schema_version": 1, "execution_submitted": True, "request_id_sha256": attempt.provider_request_id_sha256, "error_code": "smoke_remote_failed"},
                code="smoke_remote_failed", message="烟测远端任务未成功",
            )
        if not response.get("terminal_success"):
            return _release_schedule(
                attempt, claim_token,
                remote_status=remote_status,
                progress_fraction=response.get("progress_fraction"),
            )
        collected = driver.invoke(deployment, "collect", {"request_id": attempt.provider_request_id})
        expected_kind = str(snapshot.get("output_kind") or "")
        max_bytes = 64 * 1024 * 1024 if expected_kind == "image" else 512 * 1024 * 1024
        facts = verify_remote_artifacts_for_smoke(
            deployment_id=deployment.id,
            artifacts=collected.get("artifacts"),
            provider_config=_config(deployment),
            expected_kind=expected_kind,
            max_bytes=max_bytes,
        )
        return _finalize(
            attempt, claim_token,
            status=ModelDeploymentSmokeAttempt.Status.SUCCEEDED,
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_id_sha256": attempt.provider_request_id_sha256,
                "request_sha256": attempt.request_sha256,
                "artifact_facts": facts,
                "estimated_cost": str(attempt.estimated_cost) if attempt.estimated_cost is not None else None,
                "estimated_currency": attempt.cost_currency,
                "actual_cost": collected.get("actual_cost"),
                "actual_currency": collected.get("currency") or "",
            },
        )
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain) or attempt.status == ModelDeploymentSmokeAttempt.Status.SUBMITTING
        return _finalize(
            attempt, claim_token,
            status=(ModelDeploymentSmokeAttempt.Status.UNCERTAIN if uncertain else ModelDeploymentSmokeAttempt.Status.FAILED),
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={
                "schema_version": 1,
                "execution_submitted": attempt.status != ModelDeploymentSmokeAttempt.Status.QUEUED,
                "submission_uncertain": uncertain,
                "request_id_sha256": attempt.provider_request_id_sha256,
                "error_code": exc.code,
            },
            code=exc.code, message=str(exc),
        )
    except Exception:
        crossed_boundary = attempt.status in {
            ModelDeploymentSmokeAttempt.Status.SUBMITTING,
            ModelDeploymentSmokeAttempt.Status.SUBMITTED,
            ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED,
        }
        code = "provider_unclassified_failure"
        return _finalize(
            attempt,
            claim_token,
            status=(
                ModelDeploymentSmokeAttempt.Status.UNCERTAIN
                if crossed_boundary else ModelDeploymentSmokeAttempt.Status.FAILED
            ),
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={
                "schema_version": 1,
                "execution_submitted": crossed_boundary,
                "submission_uncertain": crossed_boundary,
                "request_id_sha256": attempt.provider_request_id_sha256,
                "error_code": code,
            },
            code=code,
            message=(
                "烟测提交状态不确定，必须人工消歧。"
                if crossed_boundary else "烟测在提交前发生未分类错误。"
            ),
        )


@transaction.atomic
def recover_stale_deployment_smokes(minutes: int = 10) -> int:
    cutoff = timezone.now() - timedelta(seconds=max(60, int(minutes) * 60))
    rows = list(ModelDeploymentSmokeAttempt.objects.select_for_update().filter(
        operation__in=DEPLOYMENT_SMOKE_OPERATIONS,
        status__in=ACTIVE,
        claim_token__isnull=False,
        heartbeat_at__lt=cutoff,
    ))
    now = timezone.now()
    for row in rows:
        row.claim_token = None
        row.worker_id = ""
        row.claimed_at = None
        row.heartbeat_at = now
        row.next_poll_at = now
        row.save(update_fields=(
            "claim_token", "worker_id", "claimed_at", "heartbeat_at", "next_poll_at", "updated_at",
        ))
    return len(rows)


@transaction.atomic
def request_deployment_smoke_cancel(
    attempt: ModelDeploymentSmokeAttempt,
) -> ModelDeploymentSmokeAttempt:
    """Persist cancellation without pretending a synchronous POST can be undone."""

    attempt = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.operation not in DEPLOYMENT_SMOKE_OPERATIONS:
        raise ValidationError("deployment smoke operation 不支持后台取消")
    if attempt.status not in ACTIVE:
        raise ValidationError("deployment smoke 已是终态")
    now = timezone.now()
    if attempt.status == ModelDeploymentSmokeAttempt.Status.QUEUED:
        attempt.status = ModelDeploymentSmokeAttempt.Status.CANCELLED
        attempt.finished_at = now
        attempt.error_code = "cancelled_before_submission"
        attempt.error_message = "烟测在远端提交前取消。"
        attempt.next_poll_at = None
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
    elif attempt.operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS:
        raise ValidationError(
            "文本烟测已进入单次提交边界，不能安全撤回；请等待终态或人工消歧",
        )
    else:
        attempt.status = ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED
        attempt.cancel_requested_at = attempt.cancel_requested_at or now
        attempt.next_poll_at = now
    attempt.save(update_fields=(
        "status", "finished_at", "error_code", "error_message",
        "cancel_requested_at", "next_poll_at", "claim_token", "worker_id",
        "claimed_at", "updated_at",
    ))
    return attempt
