from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import time
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import GeneratedArtifact, GenerationAttempt, GenerationAttemptResolution, ProductionRun
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OPERATIONS,
    async_operation_contract,
    frozen_async_deployment,
    validate_standard_payload,
)
from studio.services.json_contracts import canonical_json_bytes, verify_json_contract
from studio.services.provider_registry import ProviderDriverError, driver_for
from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
    validate_deadline_seconds,
)
from studio.services.remote_artifacts import archive_remote_artifacts
from studio.services.text_generation_queue import append_attempt_resolution
from studio.services.production_run_auto_advance import schedule_production_run_advance


STALE_CLAIM_MINUTES = 10


def _attempt_deadline_seconds(attempt: GenerationAttempt) -> tuple[float, float]:
    """Return the frozen production idle/hard pair for this attempt.

    New attempts always carry the pair in their integrity-protected request
    snapshot. The stage snapshot branch is the upgrade path for attempts that
    were queued before the request contract gained this field.
    """
    snapshot = _snapshot(attempt)
    deadline_snapshot = snapshot.get("execution_deadlines")
    if isinstance(deadline_snapshot, dict):
        return validate_deadline_seconds(
            idle_seconds=deadline_snapshot.get("idle_timeout_seconds"),
            hard_seconds=deadline_snapshot.get("hard_timeout_seconds"),
        )
    stage = attempt.stage_run
    verify_json_contract(
        stage.config_snapshot,
        schema_version=stage.config_snapshot_schema_version,
        size_bytes=stage.config_snapshot_size_bytes,
        sha256=stage.config_snapshot_sha256,
    )
    stage_snapshot = stage.config_snapshot if isinstance(stage.config_snapshot, dict) else {}
    route_values = (stage_snapshot.get("routes") or {}).get(str(attempt.route_index))
    if not isinstance(route_values, dict):
        route_values = stage_snapshot.get("base")
    values = route_values.get("values") if isinstance(route_values, dict) else None
    if not isinstance(values, dict):
        raise ValidationError("异步媒体 attempt 缺少冻结的任务时限配置")
    return validate_deadline_seconds(
        idle_seconds=values.get("runtime.task_idle_timeout_seconds"),
        hard_seconds=values.get("production.task_timeout_seconds"),
    )


def _config(deployment) -> dict:
    return {
        **(deployment.profile_version.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }


@transaction.atomic
def claim_next_async_media_attempt(worker_id: str) -> GenerationAttempt | None:
    now = timezone.now()
    attempt = (
        GenerationAttempt.objects.select_for_update()
        .select_related("stage_run__production_run", "model_deployment__profile_version__provider")
        .filter(
            operation__in=ASYNC_MEDIA_OPERATIONS,
            status__in=(
                GenerationAttempt.Status.QUEUED,
                GenerationAttempt.Status.SUBMITTED,
                GenerationAttempt.Status.CANCEL_REQUESTED,
            ),
            claim_token__isnull=True,
        )
        .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        .order_by("created_at")
        .first()
    )
    if attempt is None:
        return None
    if attempt.deadline_started_at is None:
        configured_idle, hard_seconds = _attempt_deadline_seconds(attempt)
        deadlines = initial_deadlines(
            idle_seconds=configured_idle,
            hard_seconds=hard_seconds,
            now=now,
            marker="phase:claimed",
        )
        attempt.deadline_started_at = deadlines.started_at
        attempt.hard_deadline_at = deadlines.hard_deadline_at
        attempt.idle_deadline_at = deadlines.idle_deadline_at
        attempt.last_meaningful_progress_at = deadlines.last_meaningful_progress_at
        attempt.progress_marker = deadlines.progress_marker
    attempt.claim_token = uuid.uuid4()
    attempt.worker_id = str(worker_id)[:160]
    attempt.claimed_at = now
    attempt.heartbeat_at = now
    attempt.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at",
        "deadline_started_at", "hard_deadline_at", "idle_deadline_at",
        "last_meaningful_progress_at", "progress_marker", "updated_at",
    ))
    return attempt


def _release_claim(attempt_id, claim_token) -> None:
    GenerationAttempt.objects.filter(pk=attempt_id, claim_token=claim_token).update(
        claim_token=None,
        worker_id="",
        claimed_at=None,
        heartbeat_at=timezone.now(),
        updated_at=timezone.now(),
    )


def _snapshot(attempt: GenerationAttempt) -> dict:
    verify_json_contract(
        attempt.request_snapshot,
        schema_version=attempt.request_snapshot_schema_version,
        size_bytes=attempt.request_snapshot_size_bytes,
        sha256=attempt.request_snapshot_sha256,
    )
    if attempt.request_sha256 != attempt.request_snapshot_sha256:
        raise ValidationError("async attempt request identity 已漂移")
    snapshot = attempt.request_snapshot
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != 1
        or snapshot.get("transport") != "restricted_async_rest"
        or snapshot.get("operation") not in ASYNC_MEDIA_OPERATIONS
        or not isinstance(snapshot.get("route"), dict)
        or not isinstance(snapshot.get("payload"), dict)
    ):
        raise ValidationError("async attempt request snapshot 无效")
    return snapshot


@transaction.atomic
def _fail(
    attempt_id,
    claim_token,
    *,
    code: str,
    message: str,
    release_reservation: bool = False,
) -> GenerationAttempt:
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run").get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    if release_reservation and attempt.committed_cost:
        run = ProductionRun.objects.select_for_update().get(
            pk=attempt.stage_run.production_run_id,
        )
        run.committed_cost_total = max(
            Decimal("0"), run.committed_cost_total - attempt.committed_cost,
        )
        run.save(update_fields=("committed_cost_total", "updated_at"))
        attempt.committed_cost = Decimal("0")
    attempt.status = GenerationAttempt.Status.FAILED
    attempt.error_code = str(code)[:100]
    attempt.error_message = str(message)[:2000]
    attempt.finished_at = timezone.now()
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.heartbeat_at = attempt.finished_at
    attempt.next_poll_at = None
    attempt.save(update_fields=(
        "status", "error_code", "error_message", "finished_at", "committed_cost", "claim_token",
        "worker_id", "claimed_at", "heartbeat_at", "next_poll_at", "updated_at",
    ))
    schedule_production_run_advance(attempt.stage_run.production_run_id)
    return attempt


@transaction.atomic
def _cancelled(attempt_id, claim_token, *, code: str, message: str) -> GenerationAttempt:
    attempt = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    if code == "late_result_ignored":
        append_attempt_resolution(
            attempt,
            outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
            evidence={
                "schema_version": 1,
                "attempt_id": str(attempt.id),
                "provider_request_id": attempt.provider_request_id,
                "remote_status": attempt.remote_status,
            },
            note="Late async media result ignored after cancellation",
        )
    attempt.status = GenerationAttempt.Status.CANCELLED
    attempt.error_code = str(code)[:100]
    attempt.error_message = str(message)[:2000]
    attempt.finished_at = timezone.now()
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.heartbeat_at = attempt.finished_at
    attempt.next_poll_at = None
    attempt.save(update_fields=(
        "status", "error_code", "error_message", "finished_at", "claim_token",
        "worker_id", "claimed_at", "heartbeat_at", "next_poll_at", "updated_at",
    ))
    schedule_production_run_advance(attempt.stage_run.production_run_id)
    return attempt


def _amount(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderDriverError("provider 实际费用无效", code="actual_cost_invalid") from exc
    if not result.is_finite() or result < 0:
        raise ProviderDriverError("provider 实际费用无效", code="actual_cost_invalid")
    return result


def _resolved_actual_cost(attempt: GenerationAttempt, collected: dict, config: dict) -> tuple[Decimal, str]:
    actual = _amount(collected.get("actual_cost"))
    currency = str(collected.get("currency") or attempt.cost_currency or "").strip().upper()
    pricing = config.get("pricing") if isinstance(config.get("pricing"), dict) else {}
    if actual is None and set(pricing) == {"currency", "fixed_per_request"}:
        actual = attempt.committed_cost
        currency = str(pricing.get("currency") or currency).strip().upper()
    if actual is None:
        raise ProviderDriverError(
            "provider 未返回实际费用，需要管理员对账",
            code="actual_cost_reconciliation_required",
        )
    if actual > 0 and not currency:
        raise ProviderDriverError("provider 实际费用缺少币种", code="actual_cost_invalid")
    return actual, currency


@transaction.atomic
def _succeed(
    attempt_id,
    claim_token,
    *,
    artifacts: list[GeneratedArtifact],
    actual_cost: Decimal,
    currency: str,
) -> GenerationAttempt:
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run").get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    run = ProductionRun.objects.select_for_update().get(pk=attempt.stage_run.production_run_id)
    if run.cost_currency and currency and run.cost_currency != currency:
        raise ProviderDriverError("实际费用币种与运行币种冲突", code="actual_cost_currency_conflict")
    if attempt.actual_cost is None:
        reserved = attempt.committed_cost or Decimal("0")
        attempt.actual_cost = actual_cost
        attempt.cost_currency = currency
        attempt.committed_cost = Decimal("0")
        run.actual_cost_total += actual_cost
        run.committed_cost_total = max(
            Decimal("0"), run.committed_cost_total - reserved,
        )
        if currency and not run.cost_currency:
            run.cost_currency = currency
        run.save(update_fields=(
            "committed_cost_total", "actual_cost_total", "cost_currency", "updated_at",
        ))
    if attempt.ignore_late_result:
        append_attempt_resolution(
            attempt,
            outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
            evidence={
                "schema_version": 1,
                "attempt_id": str(attempt.id),
                "provider_request_id": attempt.provider_request_id,
                "artifact_sha256": [item.sha256 for item in artifacts],
            },
            note="Late async media result ignored after cancellation",
        )
        attempt.status = GenerationAttempt.Status.CANCELLED
        attempt.error_code = "late_result_ignored"
        attempt.error_message = "运行已取消，晚到结果未进入正式可选产物。"
        GeneratedArtifact.objects.filter(
            id__in=[item.id for item in artifacts],
        ).update(status=GeneratedArtifact.Status.REJECTED, updated_at=timezone.now())
    else:
        attempt.status = GenerationAttempt.Status.SUCCEEDED
        first = sorted(artifacts, key=lambda item: item.variant_index)[0]
        if not GeneratedArtifact.objects.filter(
            production_run=run,
            variant_group=first.variant_group,
            status=GeneratedArtifact.Status.SELECTED,
        ).exists():
            first.status = GeneratedArtifact.Status.SELECTED
            first.save(update_fields=("status", "updated_at"))
        attempt.result_sha256 = hashlib.sha256(canonical_json_bytes([
            {"id": str(item.id), "sha256": item.sha256} for item in artifacts
        ])).hexdigest()
        attempt.error_code = ""
        attempt.error_message = ""
        if run.budget_limit is not None and run.actual_cost_total > run.budget_limit:
            stage = attempt.stage_run
            stage.status = stage.Status.AWAITING_APPROVAL
            stage.reason_code = "actual_budget_exceeded"
            stage.reason = (
                f"实际累计费用 {run.actual_cost_total} {run.cost_currency} "
                f"超过预算 {run.budget_limit}；已保留真实产物，发布前必须提高预算。"
            )
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = run.Status.AWAITING_APPROVAL
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
    attempt.finished_at = timezone.now()
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.heartbeat_at = attempt.finished_at
    attempt.next_poll_at = None
    attempt.save(update_fields=(
        "status", "result_sha256", "committed_cost", "actual_cost", "cost_currency", "error_code",
        "error_message", "finished_at", "claim_token", "worker_id", "claimed_at",
        "heartbeat_at", "next_poll_at", "updated_at",
    ))
    schedule_production_run_advance(attempt.stage_run.production_run_id)
    return attempt


def _schedule(
    attempt: GenerationAttempt,
    claim_token,
    *,
    status: str,
    remote_status: str,
    progress_fraction: float | None = None,
) -> GenerationAttempt:
    config = _config(frozen_async_deployment(_snapshot(attempt)["route"]))
    configured_idle, _hard_seconds = _attempt_deadline_seconds(attempt)
    interval = float(config.get("poll_interval_seconds", 2))
    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token:
            return locked
        locked.status = status
        normalized_status = str(remote_status)[:120]
        observed = timezone.now()
        marker = ""
        if normalized_status and normalized_status != locked.remote_status:
            marker = f"status:{normalized_status}"
        if progress_fraction is not None:
            previous = -1.0
            if "|progress:" in locked.progress_marker:
                try:
                    previous = float(locked.progress_marker.rsplit("|progress:", 1)[1])
                except (TypeError, ValueError):
                    previous = -1.0
            if float(progress_fraction) > previous:
                marker = f"status:{normalized_status}|progress:{float(progress_fraction):.6f}"
        deadline_updates = meaningful_deadline_updates(
            locked,
            marker=marker,
            idle_seconds=configured_idle,
            now=observed,
        ) if marker else {}
        locked.remote_status = normalized_status
        locked.last_polled_at = observed
        locked.poll_count += 1
        locked.next_poll_at = locked.last_polled_at + timedelta(seconds=interval)
        locked.claim_token = None
        locked.worker_id = ""
        locked.claimed_at = None
        locked.heartbeat_at = locked.last_polled_at
        for field, value in deadline_updates.items():
            setattr(locked, field, value)
        locked.save(update_fields=(
            "status", "remote_status", "last_polled_at", "poll_count", "next_poll_at",
            "claim_token", "worker_id", "claimed_at", "heartbeat_at",
            *deadline_updates.keys(), "updated_at",
        ))
        return locked


def _submit(attempt: GenerationAttempt, claim_token, snapshot: dict, deployment, driver) -> GenerationAttempt:
    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token or locked.status != GenerationAttempt.Status.QUEUED:
            return locked
        locked.status = GenerationAttempt.Status.SUBMITTING
        locked.submission_started_at = timezone.now()
        locked.heartbeat_at = locked.submission_started_at
        locked.save(update_fields=("status", "submission_started_at", "heartbeat_at", "updated_at"))
    try:
        response = driver.invoke(deployment, "submit", snapshot["payload"])
    except ProviderDriverError as exc:
        code = "uncertain_remote_response" if exc.submission_uncertain else exc.code
        return _fail(
            attempt.pk,
            claim_token,
            code=code,
            message=(
                "远端提交响应不确定；禁止自动再次 POST，必须核验远端任务。"
                if exc.submission_uncertain else str(exc)
            ),
            release_reservation=not exc.submission_uncertain,
        )
    request_id = str(response.get("request_id") or "") if isinstance(response, dict) else ""
    if not request_id:
        return _fail(
            attempt.pk,
            claim_token,
            code="uncertain_remote_response",
            message="远端提交响应缺少 request_id；禁止自动再次 POST。",
        )
    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token or locked.status != GenerationAttempt.Status.SUBMITTING:
            return locked
        locked.status = (
            GenerationAttempt.Status.CANCEL_REQUESTED
            if locked.ignore_late_result
            else GenerationAttempt.Status.SUBMITTED
        )
        locked.provider_request_id = request_id[:191]
        locked.remote_status = str(response.get("status") or "submitted")[:120]
        locked.submitted_at = timezone.now()
        configured_idle, _hard_seconds = _attempt_deadline_seconds(locked)
        deadline_updates = meaningful_deadline_updates(
            locked,
            marker="phase:submitted",
            idle_seconds=configured_idle,
            now=locked.submitted_at,
        )
        for field, value in deadline_updates.items():
            setattr(locked, field, value)
        locked.next_poll_at = locked.submitted_at
        locked.claim_token = None
        locked.worker_id = ""
        locked.claimed_at = None
        locked.heartbeat_at = locked.submitted_at
        locked.save(update_fields=(
            "status", "provider_request_id", "remote_status", "submitted_at", "next_poll_at",
            "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
            *deadline_updates.keys(),
        ))
        return locked


def _collect(attempt: GenerationAttempt, claim_token, snapshot: dict, deployment, driver) -> GenerationAttempt:
    last_heartbeat = [0.0]

    def heartbeat() -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - last_heartbeat[0] < 5:
            return
        updated = GenerationAttempt.objects.filter(
            pk=attempt.pk,
            claim_token=claim_token,
        ).update(heartbeat_at=timezone.now(), updated_at=timezone.now())
        if not updated:
            raise ProviderDriverError("async attempt claim 已失效", code="async_claim_lost")
        last_heartbeat[0] = now_monotonic

    try:
        collected = driver.invoke(deployment, "collect", {"request_id": attempt.provider_request_id})
        artifacts = collected.get("artifacts") if isinstance(collected, dict) else None
        archived = archive_remote_artifacts(
            attempt,
            artifacts=artifacts,
            provider_config=_config(deployment),
            expected_kind=str(snapshot["output_kind"]),
            heartbeat=heartbeat,
        )
        actual, currency = _resolved_actual_cost(attempt, collected, _config(deployment))
        return _succeed(
            attempt.pk,
            claim_token,
            artifacts=archived,
            actual_cost=actual,
            currency=currency,
        )
    except ProviderDriverError as exc:
        if exc.code == "late_result_ignored":
            return _cancelled(
                attempt.pk,
                claim_token,
                code="late_result_ignored",
                message="运行取消后返回的远端产物已清理并忽略。",
            )
        return _fail(attempt.pk, claim_token, code=exc.code, message=str(exc))


def _poll(attempt: GenerationAttempt, claim_token, snapshot: dict, deployment, driver) -> GenerationAttempt:
    if expired_kind(attempt):
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.claim_token == claim_token:
                locked.status = GenerationAttempt.Status.CANCEL_REQUESTED
                locked.cancel_requested_at = locked.cancel_requested_at or timezone.now()
                locked.next_poll_at = timezone.now()
                locked.claim_token = None
                locked.worker_id = ""
                locked.claimed_at = None
                locked.save(update_fields=(
                    "status", "cancel_requested_at", "next_poll_at", "claim_token",
                    "worker_id", "claimed_at", "updated_at",
                ))
                return locked
    try:
        response = driver.invoke(deployment, "poll", {"request_id": attempt.provider_request_id})
    except ProviderDriverError as exc:
        if exc.retryable:
            return _schedule(
                attempt,
                claim_token,
                status=GenerationAttempt.Status.SUBMITTED,
                remote_status=attempt.remote_status,
            )
        return _fail(attempt.pk, claim_token, code=exc.code, message=str(exc))
    remote_status = str(response.get("status") or "")
    progress_fraction = response.get("progress_fraction")
    if response.get("terminal_cancelled"):
        return _cancelled(attempt.pk, claim_token, code="remote_cancelled", message="远端任务已取消")
    if response.get("terminal_failure"):
        return _fail(attempt.pk, claim_token, code="remote_generation_failed", message="远端任务明确失败")
    if response.get("terminal_success"):
        # A claim performs one provider operation only. Persist terminal status and
        # let the next claim perform collect/download/archive idempotently.
        return _schedule(
            attempt,
            claim_token,
            status=GenerationAttempt.Status.SUBMITTED,
            remote_status=remote_status,
            progress_fraction=progress_fraction,
        )
    return _schedule(
        attempt,
        claim_token,
        status=GenerationAttempt.Status.SUBMITTED,
        remote_status=remote_status,
        progress_fraction=progress_fraction,
    )


def _cancel(attempt: GenerationAttempt, claim_token, deployment, driver) -> GenerationAttempt:
    if not attempt.provider_request_id:
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.claim_token == claim_token:
                locked.status = GenerationAttempt.Status.CANCELLED
                locked.finished_at = timezone.now()
                locked.claim_token = None
                locked.worker_id = ""
                locked.claimed_at = None
                locked.save(update_fields=(
                    "status", "finished_at", "claim_token", "worker_id", "claimed_at", "updated_at",
                ))
                schedule_production_run_advance(locked.stage_run.production_run_id)
                return locked
    protocol = "cancel" if attempt.cancel_submitted_at is None else "poll"
    try:
        response = driver.invoke(deployment, protocol, {"request_id": attempt.provider_request_id})
    except ProviderDriverError as exc:
        if exc.retryable:
            return _schedule(
                attempt,
                claim_token,
                status=GenerationAttempt.Status.CANCEL_REQUESTED,
                remote_status=attempt.remote_status,
            )
        return _fail(attempt.pk, claim_token, code=exc.code, message=str(exc))
    if protocol == "cancel":
        GenerationAttempt.objects.filter(pk=attempt.pk, claim_token=claim_token).update(
            cancel_submitted_at=timezone.now(), updated_at=timezone.now(),
        )
    remote_status = str(response.get("status") or "")
    if response.get("terminal_cancelled") or response.get("terminal_failure"):
        return _cancelled(attempt.pk, claim_token, code="remote_cancelled", message="远端取消已确认")
    if response.get("terminal_success"):
        return _cancelled(attempt.pk, claim_token, code="late_result_ignored", message="取消后的远端结果已忽略")
    return _schedule(
        attempt,
        claim_token,
        status=GenerationAttempt.Status.CANCEL_REQUESTED,
        remote_status=remote_status,
    )


def process_async_media_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    claim_token = attempt.claim_token
    if claim_token is None:
        raise ValidationError("async attempt 未持有 claim")
    deadline_kind = expired_kind(attempt)
    if deadline_kind and attempt.status != GenerationAttempt.Status.CANCEL_REQUESTED:
        if not attempt.provider_request_id:
            return _fail(
                attempt.pk,
                claim_token,
                code=f"{deadline_kind}_timeout",
                message=f"异步媒体任务达到持久化 {deadline_kind} deadline，未提交远端任务。",
                release_reservation=True,
            )
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.claim_token == claim_token:
                observed = timezone.now()
                locked.status = GenerationAttempt.Status.CANCEL_REQUESTED
                locked.cancel_requested_at = locked.cancel_requested_at or observed
                locked.next_poll_at = observed
                locked.claim_token = None
                locked.worker_id = ""
                locked.claimed_at = None
                locked.error_code = f"{deadline_kind}_timeout"
                locked.error_message = f"异步媒体任务达到持久化 {deadline_kind} deadline，正在取消远端任务。"
                locked.save(update_fields=(
                    "status", "cancel_requested_at", "next_poll_at", "claim_token",
                    "worker_id", "claimed_at", "error_code", "error_message", "updated_at",
                ))
                return locked
    try:
        snapshot = _snapshot(attempt)
        deployment = frozen_async_deployment(snapshot["route"])
        if snapshot["operation"] != attempt.operation:
            raise ValidationError("async attempt operation 与冻结快照不一致")
        contract = async_operation_contract(deployment.profile_version, attempt.operation)
        validate_standard_payload(contract, snapshot["payload"])
        driver = driver_for(deployment.profile_version.provider)
        if attempt.status == GenerationAttempt.Status.QUEUED:
            return _submit(attempt, claim_token, snapshot, deployment, driver)
        if attempt.status == GenerationAttempt.Status.CANCEL_REQUESTED:
            return _cancel(attempt, claim_token, deployment, driver)
        if attempt.status == GenerationAttempt.Status.SUBMITTED:
            terminal_success = set(_config(deployment).get("terminal_success") or [])
            if attempt.remote_status in terminal_success:
                return _collect(attempt, claim_token, snapshot, deployment, driver)
            return _poll(attempt, claim_token, snapshot, deployment, driver)
        return attempt
    except (ProviderDriverError, ValidationError) as exc:
        return _fail(
            attempt.pk,
            claim_token,
            code=getattr(exc, "code", "async_attempt_invalid"),
            message=str(exc),
        )


def fail_unhandled_async_media_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    """Contain an unexpected worker error without persisting exception text or URLs."""
    if attempt.claim_token is None:
        return attempt
    return _fail(
        attempt.pk,
        attempt.claim_token,
        code="async_worker_internal_error",
        message="异步媒体 worker 出现未处理错误；详细信息仅保留在受控服务日志。",
    )


@transaction.atomic
def recover_stale_async_media_attempts(minutes: int = STALE_CLAIM_MINUTES) -> dict[str, int]:
    now = timezone.now()
    requested_seconds = max(60, int(minutes) * 60)
    cutoff = now - timedelta(seconds=requested_seconds)
    recovered = 0
    uncertain = 0
    terminal_run_ids = set()
    attempts = list(GenerationAttempt.objects.select_for_update().filter(
        operation__in=ASYNC_MEDIA_OPERATIONS,
        claim_token__isnull=False,
        heartbeat_at__lt=cutoff,
    ))
    for attempt in attempts:
        snapshot = attempt.request_snapshot if isinstance(attempt.request_snapshot, dict) else {}
        route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
        provider_config = route.get("provider_config") if isinstance(route.get("provider_config"), dict) else {}
        deployment_config = route.get("deployment_config") if isinstance(route.get("deployment_config"), dict) else {}
        try:
            request_timeout = float(deployment_config.get(
                "timeout_seconds", provider_config.get("timeout_seconds", 60),
            ))
        except (TypeError, ValueError):
            request_timeout = 60
        # A CLI recovery setting is an operator preference, not permission to
        # steal a claim that can still be inside the driver's bounded request.
        # The hard floor also covers checksum/ffprobe work after a large download.
        safe_seconds = max(requested_seconds, request_timeout + 60, 15 * 60)
        heartbeat_at = attempt.heartbeat_at or attempt.claimed_at
        if heartbeat_at is not None and (now - heartbeat_at).total_seconds() < safe_seconds:
            continue
        if attempt.status == GenerationAttempt.Status.SUBMITTING and not attempt.provider_request_id:
            attempt.status = GenerationAttempt.Status.FAILED
            attempt.error_code = "uncertain_remote_response"
            attempt.error_message = "worker 在提交边界后失联；禁止自动再次 POST，必须核验远端任务。"
            attempt.finished_at = timezone.now()
            uncertain += 1
            terminal_run_ids.add(attempt.stage_run.production_run_id)
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
        attempt.heartbeat_at = timezone.now()
        attempt.save(update_fields=(
            "status", "error_code", "error_message", "finished_at", "claim_token",
            "worker_id", "claimed_at", "heartbeat_at", "updated_at",
        ))
        if attempt.status in {
            GenerationAttempt.Status.SUCCEEDED,
            GenerationAttempt.Status.FAILED,
            GenerationAttempt.Status.CANCELLED,
        }:
            terminal_run_ids.add(attempt.stage_run.production_run_id)
        recovered += 1
    for run_id in terminal_run_ids:
        schedule_production_run_advance(run_id)
    return {"recovered": recovered, "uncertain": uncertain}
