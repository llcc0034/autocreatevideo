from __future__ import annotations

from datetime import timedelta
import math
import uuid

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from studio.models import (
    AssetGenerationJob,
    GenerationTask,
    WorkflowSmokeAttempt,
    WorkerLease,
)
from studio.services.workflow_smoke_resolution import (
    unresolved_workflow_smoke_attempts,
)
from studio.services.execution_production_attempts import superseded_uncertain_task_ids


ACTIVE = (
    WorkflowSmokeAttempt.Status.PREPARING,
    WorkflowSmokeAttempt.Status.QUEUED,
    WorkflowSmokeAttempt.Status.SUBMITTING,
    WorkflowSmokeAttempt.Status.RUNNING,
    WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
)



@transaction.atomic
def claim_next_workflow_smoke(worker_id: str) -> WorkflowSmokeAttempt | None:
    WorkerLease.objects.get_or_create(key="generation-claim")
    WorkerLease.objects.select_for_update().get(key="generation-claim")
    if unresolved_workflow_smoke_attempts().exists():
        return None
    if AssetGenerationJob.objects.filter(
        execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
    ).filter(
        Q(status__in=(
            AssetGenerationJob.Status.SUBMITTING,
            AssetGenerationJob.Status.SUBMITTED,
            AssetGenerationJob.Status.RUNNING,
            AssetGenerationJob.Status.CANCEL_REQUESTED,
            AssetGenerationJob.Status.UNCERTAIN,
        ))
        | Q(status=AssetGenerationJob.Status.QUEUED, claim_token__isnull=False),
    ).exists():
        return None
    unresolved_generation = GenerationTask.objects.exclude(pk__in=superseded_uncertain_task_ids()).exclude(
        submission_token="",
    ).exclude(
        submission_state__in=(
            GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
            GenerationTask.SubmissionState.TERMINAL_FAILED,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        ),
    ).filter(
        status__in=(
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        ),
    ).exists()
    if unresolved_generation or GenerationTask.objects.filter(
        status=GenerationTask.Status.RUNNING,
    ).exists():
        return None
    # The database uniqueness constraint prevents two active attempts for the
    # same version/deployment, but not two different GPU workflows. A claimed
    # attempt is therefore also part of the global generation mutex.
    if WorkflowSmokeAttempt.objects.filter(
        status__in=ACTIVE,
        claim_token__isnull=False,
    ).exists():
        return None
    now = timezone.now()
    in_flight = WorkflowSmokeAttempt.objects.filter(status__in=("submitting", "running", "cancel_requested"),
        submission_started_at__isnull=False)
    candidates = WorkflowSmokeAttempt.objects.filter(status__in=ACTIVE, claim_token__isnull=True)
    if in_flight.exists():
        # A polling worker releases its lease, not its remote GPU occupancy.
        # If its next poll is not due, do not start another queued workflow.
        candidates = candidates.filter(pk__in=in_flight.values("pk"))
    attempt = (
        candidates.select_for_update()
        .exclude(status=WorkflowSmokeAttempt.Status.PREPARING)
        .exclude(status="queued", error_code="execution_resource_confirmation_required")
        .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        .annotate(_priority=Case(
            When(status=WorkflowSmokeAttempt.Status.CANCEL_REQUESTED, then=Value(0)),
            When(status=WorkflowSmokeAttempt.Status.SUBMITTING, then=Value(1)),
            When(status=WorkflowSmokeAttempt.Status.RUNNING, then=Value(2)),
            default=Value(3), output_field=IntegerField(),
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
    attempt.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return attempt


def process_workflow_smoke(attempt: WorkflowSmokeAttempt):
    from studio.services.execution_workflow_smoke_worker import process_prepared_workflow_smoke
    return process_prepared_workflow_smoke(attempt)


def request_workflow_smoke_cancel(attempt: WorkflowSmokeAttempt, *, actor) -> WorkflowSmokeAttempt:
    from studio.services.execution_smoke_dispatch import request_prepared_smoke_cancel
    return request_prepared_smoke_cancel(attempt, actor)


@transaction.atomic
def recover_stale_workflow_smokes(minutes: float) -> int:
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or not math.isfinite(float(minutes))
        or float(minutes) <= 0
    ):
        raise ValueError("workflow smoke 陈旧任务回收阈值必须是正有限分钟数")
    cutoff = timezone.now() - timedelta(minutes=float(minutes))
    attempts = list(WorkflowSmokeAttempt.objects.select_for_update().filter(
        status__in=ACTIVE,
        claim_token__isnull=False,
        heartbeat_at__lt=cutoff,
    ))
    now = timezone.now()
    for attempt in attempts:
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
        attempt.heartbeat_at = now
        attempt.next_poll_at = now
        attempt.save(update_fields=(
            "claim_token", "worker_id", "claimed_at", "heartbeat_at",
            "next_poll_at", "updated_at",
        ))
    return len(attempts)
