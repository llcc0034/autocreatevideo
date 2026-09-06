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
    ModelArtifact,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    WorkerLease,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    resolve_configuration,
)
from studio.services.json_contracts import assign_json_contract, verify_json_contract
from studio.services.runtime_platform import probe_runtime_host
from studio.services.draft_model_artifact_attestation import (
    load_draft_attestation_context,
)
from studio.services.security_boundaries import boundary_int


logger = logging.getLogger(__name__)


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")

ACTIVE = (
    RuntimeHostProbeAttempt.Status.QUEUED,
    RuntimeHostProbeAttempt.Status.RUNNING,
    RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED,
)
RUNTIME_HOST_PROBE_POLICY_KEYS = (
    "runtime.host_probe_heartbeat_seconds",
    "runtime.host_probe_lease_seconds",
    "runtime.host_probe_stale_seconds",
    "runtime.host_probe_refresh_buffer_seconds",
)


def _runtime_host_probe_policy() -> dict[str, float]:
    values = resolve_configuration(
        required_keys=RUNTIME_HOST_PROBE_POLICY_KEYS,
    ).values
    policy: dict[str, float] = {}
    for key in RUNTIME_HOST_PROBE_POLICY_KEYS:
        value = values[key]
        if type(value) not in {int, float} or float(value) < 0:
            raise ConfigurationResolutionError(
                f"运行主机探测配置 {key} 必须是非负数字",
                code="runtime_host_probe_policy_invalid",
                missing_keys=(key,),
            )
        policy[key] = float(value)
    heartbeat = policy["runtime.host_probe_heartbeat_seconds"]
    lease = policy["runtime.host_probe_lease_seconds"]
    stale = policy["runtime.host_probe_stale_seconds"]
    if heartbeat <= 0 or lease <= heartbeat or stale < lease:
        raise ConfigurationResolutionError(
            "运行主机探测配置必须满足 0 < heartbeat < lease <= stale",
            code="runtime_host_probe_policy_invalid",
            missing_keys=RUNTIME_HOST_PROBE_POLICY_KEYS[:3],
        )
    return policy


class RuntimeHostProbeCancelled(Exception):
    """The durable attempt was cancelled at a safe probe checkpoint."""


class RuntimeHostProbeClaimLost(Exception):
    """A recovered worker no longer owns the durable attempt fence."""


def _capability_snapshot_refresh_is_due(
    snapshot: HostCapabilitySnapshot | None,
    *,
    now,
) -> bool:
    if snapshot is None:
        return True
    if snapshot.expires_at is None:
        return False

    policy = _runtime_host_probe_policy()
    lead_seconds = policy["runtime.host_probe_lease_seconds"]
    attempt = snapshot.probe_attempt
    if attempt is not None and attempt.started_at is not None:
        observed_seconds = max(
            0.0,
            (snapshot.captured_at - attempt.started_at).total_seconds(),
        )
        lead_seconds = max(
            lead_seconds,
            observed_seconds + policy["runtime.host_probe_refresh_buffer_seconds"],
        )

    snapshot_ttl_seconds = max(
        0.0,
        (snapshot.expires_at - snapshot.captured_at).total_seconds(),
    )
    lead_seconds = min(lead_seconds, snapshot_ttl_seconds)
    return snapshot.expires_at <= now + timedelta(seconds=lead_seconds)


def _latest_snapshot_for_refresh(host: RuntimeHost) -> HostCapabilitySnapshot | None:
    return (
        HostCapabilitySnapshot.objects.select_related("probe_attempt")
        .filter(host=host)
        .order_by("-captured_at", "-revision")
        .first()
    )


def _snapshot_for_attempt(attempt: RuntimeHostProbeAttempt) -> HostCapabilitySnapshot | None:
    return HostCapabilitySnapshot.objects.filter(probe_attempt=attempt).first()


def _mark_succeeded_from_snapshot(
    attempt: RuntimeHostProbeAttempt,
    *,
    now=None,
) -> RuntimeHostProbeAttempt:
    now = now or timezone.now()
    attestation_failed = False
    request_contract_invalid = False
    if (
        attempt.probe_kind
        == RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
    ):
        try:
            verify_json_contract(
                attempt.request_snapshot,
                schema_version=attempt.request_snapshot_schema_version,
                size_bytes=attempt.request_snapshot_size_bytes,
                sha256=attempt.request_snapshot_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
        except ValidationError:
            request_contract_invalid = True
            artifact_ids = []
        else:
            artifact_ids = [
                row.get("artifact_id")
                for row in attempt.request_snapshot.get("artifacts", [])
                if isinstance(row, dict)
            ]
        snapshot = _snapshot_for_attempt(attempt)
        attestation_failed = (
            snapshot is None
            or not artifact_ids
            or ModelArtifact.objects.filter(
                pk__in=artifact_ids,
                status=ModelArtifact.Status.AVAILABLE,
                verified_snapshot=snapshot,
            ).count() != len(set(artifact_ids))
        )
    attempt.status = (
        RuntimeHostProbeAttempt.Status.FAILED
        if attestation_failed else RuntimeHostProbeAttempt.Status.SUCCEEDED
    )
    attempt.progress_marker = (
        "phase:failed" if attestation_failed else "phase:succeeded"
    )
    attempt.finished_at = attempt.finished_at or now
    attempt.heartbeat_at = now
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.error_code = (
        "attestation_request_contract_invalid"
        if request_contract_invalid else
        "draft_artifact_attestation_failed" if attestation_failed else ""
    )
    attempt.error_message = (
        "模型证明请求快照的 size/hash 合同损坏。"
        if request_contract_invalid else
        "草稿模型文件未形成完整的本机 size/hash 证明；请查看最终主机快照。"
        if attestation_failed else ""
    )
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
        if existing.probe_kind != RuntimeHostProbeAttempt.ProbeKind.CAPABILITY:
            if due_only:
                return None, False
            raise ValidationError(
                "主机已有草稿模型证明任务；完成或取消后才能启动能力探测",
            )
        return existing, False
    if due_only:
        now = timezone.now()
        latest = _latest_snapshot_for_refresh(host)
        if not _capability_snapshot_refresh_is_due(latest, now=now):
            return None, False
    try:
        with transaction.atomic():
            attempt = RuntimeHostProbeAttempt(
                host=host,
                created_by=created_by,
                trigger=trigger,
                probe_kind=RuntimeHostProbeAttempt.ProbeKind.CAPABILITY,
            )
            assign_json_contract(
                attempt,
                "request_snapshot",
                {
                    "schema_version": 1,
                    "probe_kind": RuntimeHostProbeAttempt.ProbeKind.CAPABILITY,
                    "host_id": str(host.id),
                },
                max_bytes=_stage_snapshot_max_bytes(),
            )
            attempt.save()
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
        latest = _latest_snapshot_for_refresh(host)
        if due_only and not _capability_snapshot_refresh_is_due(latest, now=now):
            return False, "not_due"
        lease.holder_id = str(holder_id)[:64]
        lease_seconds = _runtime_host_probe_policy()["runtime.host_probe_lease_seconds"]
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
    return True, "claimed"


def renew_runtime_host_lease(lease_key: str, *, holder_id: str) -> None:
    now = timezone.now()
    lease_seconds = _runtime_host_probe_policy()["runtime.host_probe_lease_seconds"]
    updated = WorkerLease.objects.filter(pk=lease_key, holder_id=holder_id).update(
        expires_at=now + timedelta(seconds=lease_seconds),
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

    heartbeat_interval = _runtime_host_probe_policy()[
        "runtime.host_probe_heartbeat_seconds"
    ]

    def control(*, force: bool = False) -> None:
        nonlocal last_control
        current = time.monotonic()
        if not force and current - last_control < heartbeat_interval:
            return
        _heartbeat_attempt(attempt.pk, claim_token)
        renew_runtime_host_lease(lease_key, holder_id=lease_holder)
        if worker_heartbeat is not None:
            worker_heartbeat()
        last_control = current

    try:
        control(force=True)
        attestation_context = None
        if (
            attempt.probe_kind
            == RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
        ):
            attestation_context = load_draft_attestation_context(attempt)

        def commit_guard() -> None:
            _heartbeat_attempt(
                attempt.pk,
                claim_token,
                marker="phase:persisting",
            )
            if attestation_context is not None:
                # Re-lock and compare deployment/profile/artifact declarations
                # inside the snapshot transaction. A late endpoint or draft
                # mutation therefore produces no snapshot and no artifact fill.
                load_draft_attestation_context(attempt, for_update=True)

        probe_kwargs = {
            "lease_renew": control,
            "probe_attempt": attempt,
            "commit_guard": commit_guard,
        }
        if attestation_context is not None:
            probe_kwargs["attestation_context"] = attestation_context
        snapshot = probe_runtime_host(attempt.host, **probe_kwargs)
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
    stale_seconds: int | None = None,
) -> int:
    now = timezone.now()
    if stale_seconds is None:
        stale_seconds = int(
            _runtime_host_probe_policy()["runtime.host_probe_stale_seconds"]
        )
    recovered = 0
    completed = list(RuntimeHostProbeAttempt.objects.select_for_update().filter(
        status__in=ACTIVE,
        final_snapshot__isnull=False,
    ))
    for attempt in completed:
        _mark_succeeded_from_snapshot(attempt, now=now)
        recovered += 1
    if type(stale_seconds) is not int or stale_seconds <= 0:
        raise ConfigurationResolutionError(
            "运行主机探测 stale_seconds 必须是正整数",
            code="runtime_host_probe_policy_invalid",
            missing_keys=("runtime.host_probe_stale_seconds",),
        )
    cutoff = now - timedelta(seconds=stale_seconds)
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
