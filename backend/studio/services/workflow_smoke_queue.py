from __future__ import annotations

from datetime import timedelta
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from studio.models import (
    GenerationTask,
    ModelDeployment,
    WorkflowSmokeAttempt,
    WorkflowTemplateVersion,
    WorkerLease,
)
from studio.services.configuration import resolve_configuration
from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
)
from studio.services.workflow_versions import (
    _execution_target_sha256,
    smoke_test_workflow_version,
    verify_workflow_version_integrity,
)


ACTIVE = (
    WorkflowSmokeAttempt.Status.QUEUED,
    WorkflowSmokeAttempt.Status.SUBMITTING,
    WorkflowSmokeAttempt.Status.RUNNING,
    WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
)


@transaction.atomic
def enqueue_workflow_smoke(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
    created_by,
) -> tuple[WorkflowSmokeAttempt, bool]:
    version = WorkflowTemplateVersion.objects.select_for_update().select_related(
        "provider", "model_profile_version", "validated_deployment",
    ).get(pk=version.pk)
    deployment = ModelDeployment.objects.select_for_update().select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    existing = WorkflowSmokeAttempt.objects.filter(
        workflow_version=version,
        deployment=deployment,
        status__in=ACTIVE,
    ).order_by("created_at").first()
    if existing is not None:
        return existing, False
    if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
        raise ValidationError("必须先通过 live validate")
    if not version.provider_id or version.validated_deployment_id != deployment.id:
        raise ValidationError("smoke-test 必须使用通过 live validation 的同一 deployment")
    if deployment.profile_version_id != version.model_profile_version_id:
        raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
    verify_workflow_version_integrity(version)
    target_sha256 = _execution_target_sha256(version, deployment)
    if version.validation_evidence.get("execution_target_sha256") != target_sha256:
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    runtime = resolve_configuration(required_keys=(
        "workflow.smoke_idle_timeout_seconds",
        "workflow.smoke_hard_timeout_seconds",
    )).values
    now = timezone.now()
    deadlines = initial_deadlines(
        idle_seconds=runtime["workflow.smoke_idle_timeout_seconds"],
        hard_seconds=runtime["workflow.smoke_hard_timeout_seconds"],
        now=now,
        marker="phase:queued",
    )
    try:
        attempt = WorkflowSmokeAttempt.objects.create(
            workflow_version=version,
            deployment=deployment,
            created_by=created_by,
            graph_sha256=version.graph_sha256,
            execution_contract_sha256=version.execution_contract_sha256,
            validation_evidence_sha256=version.validation_evidence_sha256,
            target_sha256=target_sha256,
            deadline_started_at=deadlines.started_at,
            hard_deadline_at=deadlines.hard_deadline_at,
            idle_deadline_at=deadlines.idle_deadline_at,
            last_meaningful_progress_at=deadlines.last_meaningful_progress_at,
            progress_marker=deadlines.progress_marker,
        )
    except IntegrityError:
        attempt = WorkflowSmokeAttempt.objects.get(
            workflow_version=version,
            deployment=deployment,
            status__in=ACTIVE,
        )
        return attempt, False
    version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
    version.smoke_test_message = "真实 smoke-test 已进入持久队列。"
    version.smoke_test_evidence = {}
    version.smoke_test_evidence_sha256 = ""
    version.save(update_fields=(
        "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
        "smoke_test_evidence_sha256", "updated_at",
    ))
    return attempt, True


@transaction.atomic
def claim_next_workflow_smoke(worker_id: str) -> WorkflowSmokeAttempt | None:
    WorkerLease.objects.get_or_create(key="generation-claim")
    WorkerLease.objects.select_for_update().get(key="generation-claim")
    if GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING).exists():
        return None
    now = timezone.now()
    attempt = (
        WorkflowSmokeAttempt.objects.select_for_update()
        .filter(status__in=ACTIVE, claim_token__isnull=True)
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


def _update_submission(attempt_id, claim_token, *, submitted: bool, request_id: str = ""):
    with transaction.atomic():
        locked = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt_id)
        if locked.claim_token != claim_token:
            raise ValidationError("workflow smoke claim 已失效")
        now = timezone.now()
        if submitted:
            expected = str(locked.submission_token)
            if request_id != expected:
                raise ValidationError("workflow smoke 返回的 request id 与稳定 submission token 不一致")
            locked.status = WorkflowSmokeAttempt.Status.RUNNING
            locked.provider_request_id = request_id
            locked.submitted_at = now
            marker = "phase:submitted"
        else:
            locked.status = WorkflowSmokeAttempt.Status.SUBMITTING
            locked.submission_started_at = locked.submission_started_at or now
            marker = "phase:submitting"
        runtime = resolve_configuration(required_keys=(
            "workflow.smoke_idle_timeout_seconds",
        )).values
        updates = meaningful_deadline_updates(
            locked,
            marker=marker,
            idle_seconds=runtime["workflow.smoke_idle_timeout_seconds"],
            now=now,
        )
        locked.heartbeat_at = now
        for field, value in updates.items():
            setattr(locked, field, value)
        fields = ["status", "heartbeat_at", *updates.keys(), "updated_at"]
        fields.append("submitted_at" if submitted else "submission_started_at")
        if submitted:
            fields.append("provider_request_id")
        locked.save(update_fields=fields)


def _poll_control(attempt_id, claim_token, remote_status: str) -> str:
    with transaction.atomic():
        locked = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt_id)
        if locked.claim_token != claim_token:
            return "claim_lost"
        now = timezone.now()
        locked.heartbeat_at = now
        locked.last_polled_at = now
        locked.poll_count += 1
        normalized = str(remote_status or "")[:120]
        if normalized and normalized != locked.remote_status:
            runtime = resolve_configuration(required_keys=(
                "workflow.smoke_idle_timeout_seconds",
            )).values
            updates = meaningful_deadline_updates(
                locked,
                marker=f"status:{normalized}",
                idle_seconds=runtime["workflow.smoke_idle_timeout_seconds"],
                now=now,
            )
            for field, value in updates.items():
                setattr(locked, field, value)
        else:
            updates = {}
        locked.remote_status = normalized
        locked.save(update_fields=(
            "heartbeat_at", "last_polled_at", "poll_count", "remote_status",
            *updates.keys(), "updated_at",
        ))
        if locked.status == WorkflowSmokeAttempt.Status.CANCEL_REQUESTED:
            return "cancelled"
        kind = expired_kind(locked, now=now)
        return f"{kind}_timeout" if kind else ""


@transaction.atomic
def _finish(attempt_id, claim_token, *, status: str, result=None, code="", message=""):
    attempt = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.claim_token != claim_token:
        return attempt
    now = timezone.now()
    attempt.status = status
    attempt.result = result or {}
    attempt.error_code = str(code)[:100]
    attempt.error_message = str(message)[:2000]
    attempt.finished_at = now
    attempt.heartbeat_at = now
    attempt.claim_token = None
    attempt.worker_id = ""
    attempt.claimed_at = None
    attempt.next_poll_at = None
    attempt.save(update_fields=(
        "status", "result", "error_code", "error_message", "finished_at",
        "heartbeat_at", "claim_token", "worker_id", "claimed_at", "next_poll_at",
        "updated_at",
    ))
    return attempt


def process_workflow_smoke(attempt: WorkflowSmokeAttempt) -> WorkflowSmokeAttempt:
    claim_token = attempt.claim_token
    if claim_token is None:
        raise ValidationError("workflow smoke 未持有 claim")
    if attempt.status == WorkflowSmokeAttempt.Status.CANCEL_REQUESTED and not attempt.submission_started_at:
        return _finish(
            attempt.pk, claim_token,
            status=WorkflowSmokeAttempt.Status.CANCELLED,
            code="cancelled_before_submission",
            message="烟测在远端提交前取消。",
        )
    if expired_kind(attempt) and not attempt.submission_started_at:
        kind = expired_kind(attempt)
        return _finish(
            attempt.pk, claim_token,
            status=WorkflowSmokeAttempt.Status.FAILED,
            code=f"{kind}_timeout",
            message=f"烟测在远端提交前达到持久化 {kind} deadline。",
        )
    submitted_before = bool(attempt.submission_started_at)
    try:
        _version, report = smoke_test_workflow_version(
            attempt.workflow_version,
            deployment=attempt.deployment,
            submission_token=str(attempt.submission_token),
            submitted_before=submitted_before,
            on_submission_started=lambda: _update_submission(
                attempt.pk, claim_token, submitted=False,
            ),
            on_submitted=lambda request_id: _update_submission(
                attempt.pk, claim_token, submitted=True, request_id=request_id,
            ),
            poll_control=lambda remote_status: _poll_control(
                attempt.pk, claim_token, remote_status,
            ),
        )
    except Exception as exc:
        current = WorkflowSmokeAttempt.objects.get(pk=attempt.pk)
        if current.status == WorkflowSmokeAttempt.Status.CANCEL_REQUESTED:
            status = WorkflowSmokeAttempt.Status.CANCELLED
            code = "cancelled"
        elif current.status == WorkflowSmokeAttempt.Status.SUBMITTING and not current.provider_request_id:
            status = WorkflowSmokeAttempt.Status.UNCERTAIN
            code = "submission_uncertain"
        else:
            status = WorkflowSmokeAttempt.Status.FAILED
            code = getattr(exc, "code", "workflow_smoke_failed")
        return _finish(
            attempt.pk, claim_token,
            status=status, code=code, message=str(exc),
        )
    return _finish(
        attempt.pk, claim_token,
        status=WorkflowSmokeAttempt.Status.SUCCEEDED,
        result=report,
    )


@transaction.atomic
def request_workflow_smoke_cancel(attempt: WorkflowSmokeAttempt) -> WorkflowSmokeAttempt:
    attempt = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.status not in ACTIVE:
        raise ValidationError("workflow smoke 已是终态")
    now = timezone.now()
    if not attempt.submission_started_at:
        attempt.status = WorkflowSmokeAttempt.Status.CANCELLED
        attempt.finished_at = now
        attempt.error_code = "cancelled_before_submission"
        attempt.error_message = "烟测在远端提交前取消。"
    else:
        attempt.status = WorkflowSmokeAttempt.Status.CANCEL_REQUESTED
        attempt.cancel_requested_at = attempt.cancel_requested_at or now
        attempt.next_poll_at = now
    attempt.save(update_fields=(
        "status", "finished_at", "error_code", "error_message",
        "cancel_requested_at", "next_poll_at", "updated_at",
    ))
    return attempt


@transaction.atomic
def recover_stale_workflow_smokes(minutes: int = 10) -> int:
    cutoff = timezone.now() - timedelta(seconds=max(60, int(minutes) * 60))
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
