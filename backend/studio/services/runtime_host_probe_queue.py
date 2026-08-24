from __future__ import annotations

from datetime import timedelta
import logging
import time
import uuid
from typing import Callable

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from studio.models import (
    HostCapabilitySnapshot,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    WorkerLease,
)
from studio.services.configuration import ConfigurationResolutionError
from studio.services.runtime_platform import probe_runtime_host


logger = logging.getLogger(__name__)

ACTIVE = (
    RuntimeHostProbeAttempt.Status.QUEUED,
    RuntimeHostProbeAttempt.Status.RUNNING,
    RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED,
)
HOST_LEASE_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 2.0
DEFAULT_STALE_SECONDS = 300


class RuntimeHostProbeCancelled(Exception):
    """The durable attempt was cancelled at a safe probe checkpoint."""


class RuntimeHostProbeClaimLost(Exception):
    """A recovered worker no longer owns the durable attempt fence."""


def _snapshot_for_attempt(attempt: RuntimeHostProbeAttempt) -> HostCapabilitySnapshot | None:
    return HostCapabilitySnapshot.objects.filter(probe_attempt=attempt).first()


def _mark_succeeded_from_snapshot(
    attempt: RuntimeHostProbeAttempt,
    *,
    now=None,
) -> RuntimeHostProbeAttempt:
    now = now or timezone.now()
    attempt.status = RuntimeHostProbeAttempt.Status.SUCCEEDED
    attempt.progress_marker = "phase:succeeded"
    attempt.finished_at = attempt.finished_at or now
    attempt.heartbeat_at = now
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.error_code = ""
    attempt.error_message = ""
    attempt.save(update_fields=(
        "status", "progress_marker", "finished_at", "heartbeat_at",
        "claim_token", "worker_id", "claimed_at", "error_code",
        "error_message", "updated_at",
    ))
    return attempt


@transaction.atomic
def enqueue_runtime_host_probe(
    host: RuntimeHost,
    *,
    created_by=None,
    trigger: str = RuntimeHostProbeAttempt.Trigger.MANUAL,
    due_only: bool = False,
) -> tuple[RuntimeHostProbeAttempt | None, bool]:
    host = RuntimeHost.objects.select_for_update().get(pk=host.pk)
    if host.status == RuntimeHost.Status.DISABLED:
        raise ValidationError("已禁用主机不能执行探测；请先通过受控状态入口启用")
    existing = RuntimeHostProbeAttempt.objects.filter(
        host=host,
        status__in=ACTIVE,
    ).order_by("created_at").first()
    if existing is not None:
        return existing, False
    if due_only:
        now = timezone.now()
        latest = HostCapabilitySnapshot.objects.filter(host=host).order_by(
            "-captured_at", "-revision",
        ).first()
        if latest is not None and (
            latest.expires_at is None or latest.expires_at > now
        ):
            return None, False
    try:
        with transaction.atomic():
            attempt = RuntimeHostProbeAttempt.objects.create(
                host=host,
                created_by=created_by,
                trigger=trigger,
            )
    except IntegrityError:
        attempt = RuntimeHostProbeAttempt.objects.get(host=host, status__in=ACTIVE)
        return attempt, False
    return attempt, True


def enqueue_due_runtime_host_probes() -> int:
    created = 0
    host_ids = list(RuntimeHost.objects.exclude(
        status=RuntimeHost.Status.DISABLED,
    ).order_by("key").values_list("id", flat=True))
    for host_id in host_ids:
        _attempt, was_created = enqueue_runtime_host_probe(
            RuntimeHost(pk=host_id),
            created_by=None,
            trigger=RuntimeHostProbeAttempt.Trigger.SCHEDULED,
            due_only=True,
        )
        created += int(was_created)
    return created


@transaction.atomic
def claim_next_runtime_host_probe(worker_id: str) -> RuntimeHostProbeAttempt | None:
    WorkerLease.objects.get_or_create(key="runtime-host-probe-claim")
    WorkerLease.objects.select_for_update().get(key="runtime-host-probe-claim")
    now = timezone.now()
    completed = list(RuntimeHostProbeAttempt.objects.select_for_update().filter(
        status__in=ACTIVE,
        final_snapshot__isnull=False,
    ))
    for attempt in completed:
        _mark_succeeded_from_snapshot(attempt, now=now)
    attempt = (
        RuntimeHostProbeAttempt.objects.select_for_update()
        .select_related("host")
        .filter(status__in=ACTIVE, claim_token__isnull=True)
        .annotate(_priority=Case(
            When(status=RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED, then=Value(0)),
            When(status=RuntimeHostProbeAttempt.Status.RUNNING, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ))
        .order_by("_priority", "created_at")
        .first()
    )
    if attempt is None:
        return None
    attempt.claim_token = uuid.uuid4()
    attempt.worker_id = str(worker_id)[:160]
    attempt.claimed_at = now
    attempt.heartbeat_at = now
    if attempt.status == RuntimeHostProbeAttempt.Status.QUEUED:
        attempt.status = RuntimeHostProbeAttempt.Status.RUNNING
        attempt.started_at = attempt.started_at or now
        attempt.progress_marker = "phase:probing"
    attempt.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "status",
        "started_at", "progress_marker", "updated_at",
    ))
    return attempt


def claim_runtime_host_lease(
    host: RuntimeHost,
    *,
    holder_id: str,
    due_only: bool,
    attempt_id=None,
) -> tuple[bool, str]:
    lease_key = f"runtime-host-probe:{host.id}"
    now = timezone.now()
    with transaction.atomic():
        lease, _ = WorkerLease.objects.get_or_create(key=lease_key)
        lease = WorkerLease.objects.select_for_update().get(pk=lease.pk)
        if (
            lease.holder_id
            and lease.holder_id != holder_id
            and lease.expires_at
            and lease.expires_at > now
        ):
            return False, "lease_held"
        active_attempt = RuntimeHostProbeAttempt.objects.filter(
            host=host,
            status__in=ACTIVE,
        ).first()
        if active_attempt is not None and active_attempt.id != attempt_id:
            return False, "attempt_active"
        latest = HostCapabilitySnapshot.objects.filter(host=host).order_by(
            "-captured_at", "-revision",
        ).first()
        if due_only and latest is not None and (
            latest.expires_at is None or latest.expires_at > now
        ):
            return False, "not_due"
        lease.holder_id = str(holder_id)[:64]
        lease.expires_at = now + timedelta(seconds=HOST_LEASE_SECONDS)
        lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
    return True, "claimed"


def renew_runtime_host_lease(lease_key: str, *, holder_id: str) -> None:
    now = timezone.now()
    updated = WorkerLease.objects.filter(pk=lease_key, holder_id=holder_id).update(
        expires_at=now + timedelta(seconds=HOST_LEASE_SECONDS),
        updated_at=now,
    )
    if updated != 1:
        raise RuntimeHostProbeClaimLost("runtime host probe lease was lost")


def release_runtime_host_lease(lease_key: str, *, holder_id: str) -> None:
    WorkerLease.objects.filter(pk=lease_key, holder_id=holder_id).update(
        holder_id="",
        expires_at=None,
        updated_at=timezone.now(),
    )


def _heartbeat_attempt(attempt_id, claim_token, *, marker: str = "phase:probing") -> None:
    with transaction.atomic():
        attempt = RuntimeHostProbeAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.claim_token != claim_token:
            raise RuntimeHostProbeClaimLost("runtime host probe claim was recovered")
        if attempt.status == RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED:
            raise RuntimeHostProbeCancelled("runtime host probe cancellation requested")
        if attempt.status != RuntimeHostProbeAttempt.Status.RUNNING:
            raise RuntimeHostProbeClaimLost("runtime host probe is no longer running")
        attempt.heartbeat_at = timezone.now()
        attempt.progress_marker = marker
        attempt.save(update_fields=("heartbeat_at", "progress_marker", "updated_at"))


@transaction.atomic
def _release_attempt_claim(attempt_id, claim_token, *, marker: str) -> RuntimeHostProbeAttempt:
    attempt = RuntimeHostProbeAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.heartbeat_at = timezone.now()
    attempt.progress_marker = marker
    attempt.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at",
        "progress_marker", "updated_at",
    ))
    return attempt


@transaction.atomic
def _finish_terminal(
    attempt_id,
    claim_token,
    *,
    status: str,
    code: str = "",
    message: str = "",
) -> RuntimeHostProbeAttempt:
    attempt = RuntimeHostProbeAttempt.objects.select_for_update().get(pk=attempt_id)
    snapshot = _snapshot_for_attempt(attempt)
    if snapshot is not None:
        return _mark_succeeded_from_snapshot(attempt)
    if attempt.claim_token != claim_token:
        return attempt
    now = timezone.now()
    attempt.status = status
    attempt.progress_marker = f"phase:{status}"
    attempt.finished_at = now
    attempt.heartbeat_at = now
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.error_code = str(code)[:100]
    attempt.error_message = str(message)[:2000]
    attempt.save(update_fields=(
        "status", "progress_marker", "finished_at", "heartbeat_at",
        "claim_token", "worker_id", "claimed_at", "error_code",
        "error_message", "updated_at",
    ))
    return attempt


def process_runtime_host_probe(
    attempt: RuntimeHostProbeAttempt,
    *,
    worker_heartbeat: Callable[[], None] | None = None,
) -> RuntimeHostProbeAttempt:
    claim_token = attempt.claim_token
    if claim_token is None:
        raise ValidationError("runtime host probe 未持有 claim")
    if attempt.status == RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED:
        return _finish_terminal(
            attempt.pk,
            claim_token,
            status=RuntimeHostProbeAttempt.Status.CANCELLED,
            code="cancelled_before_probe",
            message="主机探测在执行前已取消。",
        )

    lease_holder = f"hp-{claim_token.hex}"
    lease_key = f"runtime-host-probe:{attempt.host_id}"
    claimed, _reason = claim_runtime_host_lease(
        attempt.host,
        holder_id=lease_holder,
        due_only=False,
        attempt_id=attempt.id,
    )
    if not claimed:
        return _release_attempt_claim(
            attempt.pk,
            claim_token,
            marker="phase:waiting_host_lease",
        )

    last_control = 0.0

    def control(*, force: bool = False) -> None:
        nonlocal last_control
        current = time.monotonic()
        if not force and current - last_control < HEARTBEAT_INTERVAL_SECONDS:
            return
        _heartbeat_attempt(attempt.pk, claim_token)
        renew_runtime_host_lease(lease_key, holder_id=lease_holder)
        if worker_heartbeat is not None:
            worker_heartbeat()
        last_control = current

    try:
        control(force=True)
        snapshot = probe_runtime_host(
            attempt.host,
            lease_renew=control,
            probe_attempt=attempt,
            commit_guard=lambda: _heartbeat_attempt(
                attempt.pk,
                claim_token,
                marker="phase:persisting",
            ),
        )
    except RuntimeHostProbeCancelled:
        return _finish_terminal(
            attempt.pk,
            claim_token,
            status=RuntimeHostProbeAttempt.Status.CANCELLED,
            code="cancelled",
            message="主机探测已在安全检查点取消，未生成新快照。",
        )
    except RuntimeHostProbeClaimLost:
        return RuntimeHostProbeAttempt.objects.get(pk=attempt.pk)
    except ConfigurationResolutionError as exc:
        missing = "、".join(exc.missing_keys) or "未知配置项"
        return _finish_terminal(
            attempt.pk,
            claim_token,
            status=RuntimeHostProbeAttempt.Status.FAILED,
            code=exc.code,
            message=f"主机探测缺少有效配置：{missing}。",
        )
    except ValidationError as exc:
        messages = getattr(exc, "messages", None) or ["主机状态在探测期间发生变化。"]
        return _finish_terminal(
            attempt.pk,
            claim_token,
            status=RuntimeHostProbeAttempt.Status.FAILED,
            code="runtime_host_state_changed",
            message="；".join(str(item) for item in messages)[:2000],
        )
    except Exception:
        logger.exception("runtime host probe attempt %s failed", attempt.pk)
        return _finish_terminal(
            attempt.pk,
            claim_token,
            status=RuntimeHostProbeAttempt.Status.FAILED,
            code="runtime_host_probe_failed",
            message="主机探测执行失败，未生成新快照；请检查受控维护 worker 日志。",
        )
    finally:
        release_runtime_host_lease(lease_key, holder_id=lease_holder)

    with transaction.atomic():
        locked = RuntimeHostProbeAttempt.objects.select_for_update().get(pk=attempt.pk)
        persisted = _snapshot_for_attempt(locked)
        if persisted is None or persisted.id != snapshot.id:
            return _finish_terminal(
                attempt.pk,
                claim_token,
                status=RuntimeHostProbeAttempt.Status.FAILED,
                code="probe_snapshot_missing",
                message="主机探测没有形成可追溯快照。",
            )
        return _mark_succeeded_from_snapshot(locked)


@transaction.atomic
def request_runtime_host_probe_cancel(
    attempt: RuntimeHostProbeAttempt,
) -> RuntimeHostProbeAttempt:
    attempt = RuntimeHostProbeAttempt.objects.select_for_update().get(pk=attempt.pk)
    if _snapshot_for_attempt(attempt) is not None:
        return _mark_succeeded_from_snapshot(attempt)
    if attempt.status not in ACTIVE:
        return attempt
    now = timezone.now()
    attempt.cancel_requested_at = attempt.cancel_requested_at or now
    if attempt.status == RuntimeHostProbeAttempt.Status.QUEUED:
        attempt.status = RuntimeHostProbeAttempt.Status.CANCELLED
        attempt.finished_at = now
        attempt.progress_marker = "phase:cancelled"
        attempt.error_code = "cancelled_before_probe"
        attempt.error_message = "主机探测在 worker 领取前取消。"
    else:
        attempt.status = RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED
        attempt.progress_marker = "phase:cancel_requested"
    attempt.save(update_fields=(
        "status", "cancel_requested_at", "finished_at", "progress_marker",
        "error_code", "error_message", "updated_at",
    ))
    return attempt


@transaction.atomic
def recover_stale_runtime_host_probes(
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> int:
    now = timezone.now()
    recovered = 0
    completed = list(RuntimeHostProbeAttempt.objects.select_for_update().filter(
        status__in=ACTIVE,
        final_snapshot__isnull=False,
    ))
    for attempt in completed:
        _mark_succeeded_from_snapshot(attempt, now=now)
        recovered += 1
    cutoff = now - timedelta(seconds=max(60, int(stale_seconds)))
    stale = list(RuntimeHostProbeAttempt.objects.select_for_update().filter(
        status__in=ACTIVE,
        claim_token__isnull=False,
    ).filter(Q(heartbeat_at__isnull=True) | Q(heartbeat_at__lt=cutoff)))
    for attempt in stale:
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
        attempt.heartbeat_at = now
        attempt.progress_marker = (
            "phase:cancel_requested"
            if attempt.status == RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED
            else "phase:recovered"
        )
        attempt.save(update_fields=(
            "claim_token", "worker_id", "claimed_at", "heartbeat_at",
            "progress_marker", "updated_at",
        ))
        recovered += 1
    return recovered
