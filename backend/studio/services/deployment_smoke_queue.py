"""Original durable resource/instance queue; preparation owns request creation."""
from __future__ import annotations

from datetime import timedelta
import math
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import ModelDeployment, ModelDeploymentSmokeAttempt
from studio.services.configuration import resolve_configuration
from studio.services.deployment_smoke import (
    async_media_smoke_max_poll_interval_seconds,
    async_media_smoke_max_poll_seconds,
)
from studio.services.execution_deadlines import meaningful_deadline_updates
from studio.services.provider_registry import restricted_async_rest_runtime_budget


ACTIVE = (
    ModelDeploymentSmokeAttempt.Status.PREPARING,
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




def async_smoke_deadline_seconds(deployment: ModelDeployment) -> float:
    config = _config(deployment)
    runtime_budget = restricted_async_rest_runtime_budget(config)
    return float(min(
        async_media_smoke_max_poll_seconds(),
        int(runtime_budget["max_poll_seconds"]),
    ))




@transaction.atomic
def claim_next_deployment_smoke(worker_id: str) -> ModelDeploymentSmokeAttempt | None:
    now = timezone.now()
    attempt = (
        ModelDeploymentSmokeAttempt.objects.select_for_update()
        .select_related("deployment__profile_version__provider")
        .filter(
            status__in=ACTIVE,
            claim_token__isnull=True,
        )
        .exclude(status=ModelDeploymentSmokeAttempt.Status.PREPARING)
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
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return attempt


def _release_schedule(attempt, claim_token, *, remote_status: str, progress_fraction=None):
    config = _config(attempt.deployment)
    runtime_budget = restricted_async_rest_runtime_budget(config)
    smoke_poll_seconds = min(
        async_media_smoke_max_poll_seconds(),
        int(runtime_budget["max_poll_seconds"]),
    )
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
                float(smoke_poll_seconds),
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
        locked.next_poll_at = now + timedelta(seconds=min(
            async_media_smoke_max_poll_interval_seconds(),
            float(runtime_budget["poll_interval_seconds"]),
        ))
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




def process_deployment_smoke(attempt: ModelDeploymentSmokeAttempt):
    from studio.services.execution_smoke_worker import process_prepared_deployment_smoke
    return process_prepared_deployment_smoke(attempt)


@transaction.atomic
def recover_stale_deployment_smokes(minutes: float) -> int:
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or not math.isfinite(float(minutes))
        or float(minutes) <= 0
    ):
        raise ValueError("deployment smoke 陈旧任务回收阈值必须是正有限分钟数")
    cutoff = timezone.now() - timedelta(minutes=float(minutes))
    rows = list(ModelDeploymentSmokeAttempt.objects.select_for_update().filter(
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


def request_deployment_smoke_cancel(attempt: ModelDeploymentSmokeAttempt, *, actor) -> ModelDeploymentSmokeAttempt:
    from studio.services.execution_smoke_dispatch import request_prepared_smoke_cancel
    return request_prepared_smoke_cancel(attempt, actor)
