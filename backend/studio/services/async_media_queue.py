from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import time
import uuid
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt, GenerationAttemptResolution, ProductionRun
from studio.services.async_media_contract import ASYNC_MEDIA_OPERATIONS
from studio.services.execution_production_media import read_prepared_production_media, production_media_peer
from studio.services.execution_production_dispatch import begin_production_execution_submission
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import canonical_json_bytes
from studio.services.provider_registry import (
    ProviderDriverError,
    ProviderCollectResponseError,
    driver_for,
    restricted_async_rest_runtime_budget,
)
from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
    validate_deadline_seconds,
)
from studio.services.remote_artifacts import archive_remote_artifacts
from studio.services.text_generation_queue import append_attempt_resolution
from studio.services.production_run_auto_advance import schedule_production_run_advance
from studio.services.security_boundaries import boundary_int


def _attempt_deadline_seconds(attempt: GenerationAttempt) -> tuple[float, float]:
    """Return the integrity-protected idle/hard pair frozen for this attempt."""
    snapshot = _snapshot(attempt)
    deadline_snapshot = snapshot.get("execution_deadlines")
    if not isinstance(deadline_snapshot, dict):
        raise ValidationError("异步媒体 attempt 缺少冻结的任务时限配置")
    return validate_deadline_seconds(
        idle_seconds=deadline_snapshot.get("idle_timeout_seconds"),
        hard_seconds=deadline_snapshot.get("hard_timeout_seconds"),
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
            execution_instance_version__deployment__profile_version__provider__driver_type="async_rest",
            role="primary",
            status__in=(
                GenerationAttempt.Status.QUEUED,
                GenerationAttempt.Status.SUBMITTED,
                GenerationAttempt.Status.CANCEL_REQUESTED,
            ),
            claim_token__isnull=True,
        )
        .exclude(status="queued", error_code="execution_resource_confirmation_required")
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
        "claim_token", "worker_id", "claimed_at", "heartbeat_at",
        "updated_at",
    ))
    return attempt


def _snapshot(attempt: GenerationAttempt) -> dict:
    return read_prepared_production_media(attempt)


def _attempt_root_locked(function):
    @wraps(function)
    def call(attempt_id, *args, **kwargs):
        stage_id = GenerationAttempt.objects.values_list("stage_run_id", flat=True).get(pk=attempt_id)
        with locked_preparation_owner_rows([("stage_run_id", stage_id)]):
            return function(attempt_id, *args, **kwargs)
    return call


@_attempt_root_locked
def _initialize_attempt_deadlines(attempt_id, claim_token):
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run__production_run").get(pk=attempt_id)
    if attempt.claim_token != claim_token or attempt.status not in {"queued", "submitted", "cancel_requested"}:
        return attempt
    if attempt.deadline_started_at is None:
        if attempt.submission_started_at is not None or attempt.provider_request_id:
            raise ValidationError("已提交媒体任务缺少原始时限，不能重置执行时间")
        idle, hard = _attempt_deadline_seconds(attempt)
        value = initial_deadlines(idle_seconds=idle, hard_seconds=hard, now=timezone.now(), marker="phase:claimed")
        attempt.deadline_started_at, attempt.hard_deadline_at = value.started_at, value.hard_deadline_at
        attempt.idle_deadline_at = value.idle_deadline_at
        attempt.last_meaningful_progress_at, attempt.progress_marker = value.last_meaningful_progress_at, value.progress_marker
        attempt.save(update_fields=("deadline_started_at", "hard_deadline_at", "idle_deadline_at",
            "last_meaningful_progress_at", "progress_marker", "updated_at"))
    return attempt


@_attempt_root_locked
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
        if attempt.committed_cost > run.committed_cost_total:
            raise ValidationError("媒体任务预留与运行账目不一致")
        run.committed_cost_total -= attempt.committed_cost
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


@_attempt_root_locked
def _cancelled(attempt_id, claim_token, *, code: str, message: str) -> GenerationAttempt:
    attempt = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    if attempt.submission_started_at is None and attempt.committed_cost:
        run = ProductionRun.objects.select_for_update().get(pk=attempt.stage_run.production_run_id)
        if attempt.committed_cost > run.committed_cost_total:
            raise ValidationError("未提交媒体任务的预留账目不一致")
        run.committed_cost_total -= attempt.committed_cost
        run.save(update_fields=("committed_cost_total", "updated_at"))
        attempt.committed_cost = Decimal("0")
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
        "worker_id", "claimed_at", "heartbeat_at", "next_poll_at", "committed_cost", "updated_at",
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


def _resolved_actual_cost(attempt: GenerationAttempt, collected: dict, config: dict) -> tuple[Decimal | None, str]:
    actual = _amount(collected.get("actual_cost"))
    if actual is None:
        return None, attempt.cost_currency
    currency = collected.get("currency")
    if not isinstance(currency, str) or currency != attempt.cost_currency:
        raise ProviderDriverError("provider 实际费用缺少币种", code="actual_cost_invalid")
    return actual, currency


def _settle_actual_bill(attempt, run, actual_cost, currency):
    """Locked original ledger; absent bills remain unknown, repeat bills must agree."""
    if actual_cost is None:
        return
    actual_cost = _amount(actual_cost)
    if not currency or currency != attempt.cost_currency or (actual_cost and run.cost_currency and run.cost_currency != currency):
        raise ProviderDriverError("实际费用币种与运行币种冲突", code="actual_cost_currency_conflict")
    if attempt.actual_cost is not None:
        if attempt.actual_cost != actual_cost:
            raise ValidationError("同次媒体调用的实付回执不一致，不能覆盖原账目")
        return
    reserved = attempt.committed_cost
    if reserved is None or reserved > run.committed_cost_total:
        raise ValidationError("媒体任务预留与本次运行账目不一致")
    attempt.actual_cost, attempt.cost_currency, attempt.committed_cost = actual_cost, currency, Decimal("0")
    run.actual_cost_total += actual_cost
    run.committed_cost_total -= reserved
    if actual_cost:
        run.cost_currency = currency
    run.save(update_fields=("committed_cost_total", "actual_cost_total", "cost_currency", "updated_at"))
    attempt.save(update_fields=("actual_cost", "cost_currency", "committed_cost", "updated_at"))


@_attempt_root_locked
def _record_actual_bill(attempt_id, claim_token, actual_cost, currency):
    attempt = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.claim_token != claim_token or attempt.status not in {"submitted", "cancel_requested"}:
        raise ProviderDriverError("费用回执对应的任务租约已失效", code="async_claim_lost")
    run = ProductionRun.objects.select_for_update().get(pk=attempt.stage_run.production_run_id)
    _settle_actual_bill(attempt, run, actual_cost, currency)


@_attempt_root_locked
def _succeed(
    attempt_id,
    claim_token,
    *,
    artifacts: list[GeneratedArtifact],
    actual_cost: Decimal | None,
    currency: str,
) -> GenerationAttempt:
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run").get(pk=attempt_id)
    if attempt.claim_token != claim_token or attempt.status not in {"submitted", "cancel_requested"}:
        return attempt
    run = ProductionRun.objects.select_for_update().get(pk=attempt.stage_run.production_run_id)
    ids = [item.pk for item in artifacts]
    current_artifacts = list(GeneratedArtifact.objects.select_for_update().filter(pk__in=ids,
        generation_attempt=attempt, stage_run=attempt.stage_run, production_run=run).order_by("variant_index", "id"))
    if (not ids or len(set(ids)) != len(ids) or len(current_artifacts) != len(ids)
            or any(row.status != "available" or not row.sha256 or not row.manifest_metadata_sha256 or not row.size_bytes
                for row in current_artifacts)):
        raise ValidationError("媒体成功必须对应本次调用完整且尚未采用的真实候选")
    artifacts = current_artifacts
    if (actual_cost or attempt.committed_cost) and run.cost_currency and currency and run.cost_currency != currency:
        raise ProviderDriverError("实际费用币种与运行币种冲突", code="actual_cost_currency_conflict")
    _settle_actual_bill(attempt, run, actual_cost, currency)
    if attempt.ignore_late_result or attempt.status == "cancel_requested" or run.status in {"cancelled", "failed", "succeeded"}:
        attempt.ignore_late_result = True
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
        attempt.result_sha256 = hashlib.sha256(canonical_json_bytes([
            {"id": str(item.id), "sha256": item.sha256} for item in artifacts
        ])).hexdigest()
        attempt.error_code = ""
        attempt.error_message = ""
        from studio.services.execution_resource_policy import pause_production_actual_overage
        pause_production_actual_overage(run, attempt.stage_run)
    attempt.finished_at = timezone.now()
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.heartbeat_at = attempt.finished_at
    attempt.next_poll_at = None
    attempt.save(update_fields=(
        "status", "result_sha256", "committed_cost", "actual_cost", "cost_currency", "error_code",
        "error_message", "finished_at", "claim_token", "worker_id", "claimed_at",
        "heartbeat_at", "next_poll_at", "ignore_late_result", "updated_at",
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
    config = _config(production_media_peer(attempt))
    runtime_budget = restricted_async_rest_runtime_budget(config)
    configured_idle, _hard_seconds = _attempt_deadline_seconds(attempt)
    interval = float(runtime_budget["poll_interval_seconds"])
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
    from studio.services.execution_production_media import production_submission_payload
    payload = production_submission_payload(attempt, snapshot)
    allowed, attempt = begin_production_execution_submission(attempt)
    if not allowed:
        return attempt
    try:
        response = driver.invoke(deployment, "submit", payload)
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
    request_id = response.get("request_id") if isinstance(response, dict) else None
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 191:
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
        locked.provider_request_id = request_id
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


def _read_collect_receipt(attempt):
    from studio.services.execution_text_response_review import read_collect_receipt, read_retained_text_response
    row = ExecutionTextResponseReview.objects.filter(attempt=attempt).first()
    if row is None:
        return None, ""
    original, diagnostics, _, _ = read_retained_text_response(row)
    return read_collect_receipt(original, request_id=attempt.provider_request_id), diagnostics.get("validation_code", "")


@_attempt_root_locked
def _retain_collect_receipt(attempt_id, claim_token, collected, *, terminal_success, validation_code=""):
    """Commit the original call's observation before media validation/download."""
    from django.core.files.storage import default_storage
    from studio.services.execution_response_storage import store_response, ExecutionResponseStorageError
    from studio.services.execution_text_response_review import COLLECT_RECEIPT_KIND, read_collect_receipt
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.json_contracts import assign_json_contract

    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run__production_run", "preparation").get(pk=attempt_id)
    if attempt.claim_token != claim_token or claim_token is None:
        raise ProviderDriverError("原回执返回前领取已失效", code="async_claim_lost")
    if not isinstance(collected, dict) or collected.get("request_id") != attempt.provider_request_id:
        raise ValidationError("生产 REST 回执与原远端请求身份不一致")
    if (attempt.status == "cancel_requested" or attempt.cancel_requested_at is not None or attempt.ignore_late_result
            or attempt.stage_run.status == "cancelled" or attempt.stage_run.production_run.status == "cancelled"):
        return False
    snapshot = read_prepared_production_media(attempt)
    if (attempt.status != "submitted" or snapshot["driver_type"] != "async_rest"
            or attempt.submission_started_at is None or attempt.submitted_at is None
            or not attempt.provider_request_id or attempt.remote_status not in terminal_success):
        raise ValidationError("生产 REST 回执缺少原请求的确切成功提交事实")
    try:
        text = json.dumps(collected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("实际收集响应不是可保存的 JSON 回执") from exc
    read_collect_receipt(text, request_id=attempt.provider_request_id)
    existing, existing_code = _read_collect_receipt(attempt)
    if existing is not None:
        if existing != collected or existing_code != validation_code:
            raise ValidationError("原调用已保留不同回执，不能覆盖")
        return True
    row = ExecutionTextResponseReview(attempt=attempt)
    assign_json_contract(row, "diagnostics", {"schema_version": 1, "body_kind": COLLECT_RECEIPT_KIND,
        "response_source": "async_rest_collect", "provider_request_id": attempt.provider_request_id,
        "preparation_id": str(attempt.preparation_id), "request_sha256": attempt.request_sha256,
        **({"validation_code": validation_code} if validation_code else {})},
        max_bytes=execution_configuration_max_bytes())
    key, row.original_size, row.original_sha256 = store_response(text)
    row.original_file = key
    try:
        row.save()
    except Exception as exc:
        default_storage.delete(key)  # Only this failed write's newly created object.
        raise ExecutionResponseStorageError("实际 REST 回执登记失败；不会自动重跑") from exc
    return True


def _record_collect_bill(attempt, claim_token, collected, deployment):
    actual, currency = _resolved_actual_cost(attempt, collected, _config(deployment))
    _record_actual_bill(attempt.pk, claim_token, actual, currency)
    return actual, currency


def _collect(attempt: GenerationAttempt, claim_token, snapshot: dict, deployment, driver) -> GenerationAttempt:
    from studio.services.execution_response_storage import ExecutionResponseStorageError
    last_heartbeat = [0.0]
    heartbeat_seconds = float(resolve_configuration(
        required_keys=("runtime.task_heartbeat_seconds",),
    ).values["runtime.task_heartbeat_seconds"])
    if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
        raise ValidationError("异步媒体归档心跳间隔必须是正有限秒数")

    def heartbeat() -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - last_heartbeat[0] < heartbeat_seconds:
            return
        updated = GenerationAttempt.objects.filter(
            pk=attempt.pk,
            claim_token=claim_token,
        ).update(heartbeat_at=timezone.now(), updated_at=timezone.now())
        if not updated:
            raise ProviderDriverError("async attempt claim 已失效", code="async_claim_lost")
        last_heartbeat[0] = now_monotonic

    collected, validation_code = None, ""
    retained = True
    try:
        collected, validation_code = _read_collect_receipt(attempt)
        if collected is None:
            try:
                collected = driver.invoke(deployment, "collect", {"request_id": attempt.provider_request_id})
            except ProviderCollectResponseError as exc:
                collected, validation_code = exc.collect_receipt, exc.code
            retained = _retain_collect_receipt(attempt.pk, claim_token, collected,
                terminal_success=_config(deployment)["terminal_success"], validation_code=validation_code)
        if validation_code:
            if not retained:
                return _cancelled(attempt.pk, claim_token, code="late_result_ignored", message="取消后的无效回执已忽略")
            raise ProviderDriverError("原收集响应未通过驱动合同校验；实际回执已保留", code=validation_code)
        artifacts = collected.get("artifacts") if isinstance(collected, dict) else None
        if not isinstance(collected, dict):
            raise ProviderDriverError("媒体收取响应无效", code="artifact_response_invalid")
        # A known real bill belongs to the remote call even if downloading its
        # artifact fails or the user cancels before the bytes arrive.
        try:
            actual, currency = _record_collect_bill(attempt, claim_token, collected, deployment)
        except (ProviderDriverError, ValidationError) as exc:
            if not retained:
                return _cancelled(attempt.pk, claim_token, code=getattr(exc, "code", None) or "actual_cost_conflict",
                    message="调用已取消；晚到回执费用无效或与原账目冲突，未改写费用，请核对原调用。")
            raise
        if not retained:
            return _cancelled(attempt.pk, claim_token, code="late_result_ignored", message="取消后的远端产物已忽略")
        archived = archive_remote_artifacts(
            attempt,
            artifacts=artifacts,
            provider_config=_config(deployment),
            expected_kind=str(snapshot["output_kind"]),
            heartbeat=heartbeat,
        )
        return _succeed(
            attempt.pk,
            claim_token,
            artifacts=archived,
            actual_cost=actual,
            currency=currency,
        )
    except ExecutionResponseStorageError:
        billing_note = ""
        # The private receipt failing to persist cannot erase an otherwise
        # valid, already received bill. Never infer it from a rejected body.
        if isinstance(collected, dict) and collected.get("request_id") == attempt.provider_request_id and not validation_code:
            try:
                _record_collect_bill(attempt, claim_token, collected, deployment)
            except (ProviderDriverError, ValidationError):
                billing_note = " 同时无法确认回执费用，原账目未被覆盖，请核对。"
        return _fail(attempt.pk, claim_token, code="execution_response_storage_failed",
            message="原始媒体回执存储校验失败；未重收取或伪装素材成功。" + billing_note)
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
        if attempt.submission_started_at is not None:
            return _fail(attempt.pk, claim_token, code="uncertain_remote_response",
                message="提交已开始但缺少远端标识，需要核对原请求；不能按未提交取消。")
        return _cancelled(attempt.pk, claim_token, code="cancelled_before_submission", message="媒体请求尚未提交，已取消。")
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
    # Claim first, validate in the worker: a damaged request must terminate its
    # own job, not repeatedly kill the claimant and starve the whole queue.
    try:
        attempt = _initialize_attempt_deadlines(attempt.pk, claim_token)
    except ValidationError:
        attempt.refresh_from_db()
        return _fail(attempt.pk, claim_token, code="async_attempt_invalid",
            message="媒体任务的冻结请求或时限无效，请查看原准备；未使用当前默认值补齐。",
            release_reservation=attempt.submission_started_at is None and not attempt.provider_request_id)
    if attempt.claim_token != claim_token or attempt.status not in {"queued", "submitted", "cancel_requested"}:
        return attempt
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
        deployment = production_media_peer(attempt)
        if snapshot["operation"] != attempt.operation:
            raise ValidationError("async attempt operation 与冻结快照不一致")
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
            code=getattr(exc, "code", None) or "async_attempt_invalid",
            message=str(exc),
            release_reservation=attempt.submission_started_at is None and not attempt.provider_request_id,
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
def recover_stale_async_media_attempts(minutes: float | None = None) -> dict[str, int]:
    now = timezone.now()
    if minutes is None:
        configured_seconds = resolve_configuration(
            required_keys=("runtime.task_idle_timeout_seconds",),
        ).values["runtime.task_idle_timeout_seconds"]
        requested_seconds = float(configured_seconds)
    else:
        requested_seconds = float(minutes) * 60
    if not math.isfinite(requested_seconds) or requested_seconds <= 0:
        raise ValueError("异步媒体陈旧任务回收阈值必须是正有限秒数")
    cutoff = now - timedelta(seconds=requested_seconds)
    recovered = 0
    uncertain = 0
    terminal_run_ids = set()
    attempts = list(GenerationAttempt.objects.select_for_update().filter(
        operation__in=ASYNC_MEDIA_OPERATIONS,
        execution_instance_version__deployment__profile_version__provider__driver_type="async_rest",
        role="primary",
        claim_token__isnull=False,
        heartbeat_at__lt=cutoff,
    ))
    for attempt in attempts:
        # A CLI recovery setting is an operator preference, not permission to
        # steal a claim that can still be inside the driver's bounded request.
        # Verify the exact prepared target before reading its driver budget.
        # A changed peer must not be used to recover a different remote job.
        # The shared validator also applies the current Security Boundary.
        deployment = production_media_peer(attempt)
        runtime_budget = restricted_async_rest_runtime_budget(_config(deployment))
        safe_seconds = max(
            requested_seconds,
            float(runtime_budget["timeout_seconds"]),
        )
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
