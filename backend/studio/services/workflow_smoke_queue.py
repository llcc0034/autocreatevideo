from __future__ import annotations

from datetime import timedelta
import math
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from studio.models import (
    AssetGenerationJob,
    GenerationTask,
    ModelDeployment,
    WorkflowSmokeAssetBinding,
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
    _materialize_smoke_graph,
    smoke_test_workflow_version,
    verify_workflow_smoke_attempt_contract,
    verify_workflow_version_integrity,
)
from studio.services.workflow_smoke_fixtures import (
    FIXTURE_SCHEMA_VERSION,
    WorkflowSmokeFixtureConflict,
    finalize_workflow_smoke_fixture,
    normalize_workflow_smoke_fixture,
    verify_persisted_workflow_smoke_fixture,
)
from studio.services.workflow_smoke_resolution import (
    unresolved_workflow_smoke_attempts,
)


ACTIVE = (
    WorkflowSmokeAttempt.Status.QUEUED,
    WorkflowSmokeAttempt.Status.SUBMITTING,
    WorkflowSmokeAttempt.Status.RUNNING,
    WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
)


class WorkflowSmokeUnresolvedSubmission(WorkflowSmokeFixtureConflict):
    code = "unresolved_workflow_smoke_submission"


_DEFINITIVE_REMOTE_FAILURE_CODES = frozenset({
    "comfyui_resource_release_exclusive_worker_required",
    "comfyui_resource_release_rejected",
    "comfyui_resource_release_unconfirmed",
    "provider_smoke_failed",
    "provider_smoke_output_missing",
    "provider_smoke_output_invalid",
    "provider_smoke_output_unavailable",
    "workflow_smoke_contract_drifted",
    "workflow_smoke_superseded",
    "workflow_smoke_worker_superseded",
})


def _exception_code(exc: BaseException) -> str:
    """Recover a stable provider code through Django's ValidationError wrapper."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = str(getattr(current, "code", "") or "").strip()
        if code:
            return code
        current = current.__cause__ or current.__context__
    return ""


def _exception_message(exc: BaseException) -> str:
    """Prefer the coded domain cause over a Django ValidationError wrapper."""

    current: BaseException | None = exc
    seen: set[int] = set()
    fallback = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        fallback = fallback or message
        if str(getattr(current, "code", "") or "").strip() and message:
            return message[:1500]
        current = current.__cause__ or current.__context__
    return fallback[:1500]


def _failure_outcome(
    attempt: WorkflowSmokeAttempt,
    exc: BaseException,
) -> tuple[str, str, str]:
    """Classify terminal facts without claiming remote failure after submit."""

    provider_code = _exception_code(exc)
    failure_message = _exception_message(exc) or "workflow smoke 执行失败"
    crossed_submission_boundary = bool(
        attempt.submission_started_at
        or attempt.submitted_at
        or attempt.provider_request_id
    )
    if provider_code == "workflow_smoke_cancelled_result_discarded":
        return (
            WorkflowSmokeAttempt.Status.CANCELLED,
            provider_code,
            "取消请求已生效；远端返回的结果未被采纳为 smoke 证据。",
        )
    if (
        attempt.status == WorkflowSmokeAttempt.Status.CANCEL_REQUESTED
        and provider_code == "provider_smoke_cancelled"
    ):
        return (
            WorkflowSmokeAttempt.Status.UNCERTAIN,
            "remote_cancel_state_uncertain",
            "已向远端请求取消，但无法确认远端最终状态；请先核对 ComfyUI 历史。",
        )
    definitive_submit_rejection = bool(
        provider_code == "provider_request_rejected"
        and attempt.submission_started_at
        and not attempt.submitted_at
        and not attempt.provider_request_id
    )
    if (
        crossed_submission_boundary
        and provider_code not in _DEFINITIVE_REMOTE_FAILURE_CODES
        and not definitive_submit_rejection
    ):
        code = (
            "remote_state_uncertain"
            if attempt.provider_request_id or attempt.submitted_at
            else "submission_uncertain"
        )
        return (
            WorkflowSmokeAttempt.Status.UNCERTAIN,
            code,
            (
                "真实 smoke-test 已越过远端提交边界，但无法确认最终状态；"
                "请先核对 ComfyUI 历史后再重试。原因："
                + failure_message
            ),
        )
    return (
        WorkflowSmokeAttempt.Status.FAILED,
        provider_code or "workflow_smoke_failed",
        failure_message,
    )


@transaction.atomic
def enqueue_workflow_smoke(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
    created_by,
    fixture,
) -> tuple[WorkflowSmokeAttempt, bool]:
    deployment = ModelDeployment.objects.select_for_update(of=("self",)).select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
        "provider", "model_profile_version", "validated_deployment",
    ).get(pk=version.pk)
    if version.status != WorkflowTemplateVersion.Status.VALIDATED:
        raise ValidationError(
            "只有 validated 工作流版本可以创建 smoke-test；请先完成 live validate",
        )
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
    unresolved = WorkflowSmokeAttempt.objects.select_for_update(of=("self",)).filter(
        workflow_version=version,
        deployment=deployment,
        status=WorkflowSmokeAttempt.Status.UNCERTAIN,
        resolutions__isnull=True,
    ).order_by("created_at").first()
    if unresolved is not None:
        raise WorkflowSmokeUnresolvedSubmission(
            "已有远端状态不确定的 workflow smoke；必须先人工消歧，不能重复提交",
        )
    frozen_fixture = normalize_workflow_smoke_fixture(version, fixture)
    # Compile before creating durable work. Invalid parameter/slot choices are
    # request errors, not failed GPU attempts.
    materialized = _materialize_smoke_graph(
        version,
        fixture_values=frozen_fixture.values,
        fixture_reference_bindings=frozen_fixture.reference_bindings,
        fixture_asset_facts=frozen_fixture.asset_facts,
    )
    frozen_fixture = finalize_workflow_smoke_fixture(
        frozen_fixture,
        materialized_graph_sha256=materialized.graph_sha256,
    )
    existing = WorkflowSmokeAttempt.objects.filter(
        workflow_version=version,
        deployment=deployment,
        status__in=ACTIVE,
    ).order_by("created_at").first()
    if existing is not None:
        try:
            verify_workflow_smoke_attempt_contract(
                existing,
                version=version,
                deployment=deployment,
            )
        except ValidationError as exc:
            raise WorkflowSmokeFixtureConflict(
                "已有 active smoke-test 属于旧 graph/validation/deployment 合同",
            ) from exc
        if existing.fixture_sha256 == frozen_fixture.fixture_sha256:
            return existing, False
        raise WorkflowSmokeFixtureConflict(
            "同一 workflow/deployment 已有使用另一组素材或参数的 active smoke-test",
        )
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
        with transaction.atomic():
            attempt = WorkflowSmokeAttempt.objects.create(
                workflow_version=version,
                deployment=deployment,
                created_by=created_by,
                graph_sha256=version.graph_sha256,
                execution_contract_sha256=version.execution_contract_sha256,
                validation_evidence_sha256=version.validation_evidence_sha256,
                target_sha256=target_sha256,
                fixture_values=frozen_fixture.persisted_values,
                fixture_values_schema_version=FIXTURE_SCHEMA_VERSION,
                fixture_values_size_bytes=frozen_fixture.values_size_bytes,
                fixture_values_sha256=frozen_fixture.values_sha256,
                fixture_sha256=frozen_fixture.fixture_sha256,
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
        try:
            verify_workflow_smoke_attempt_contract(
                attempt,
                version=version,
                deployment=deployment,
            )
        except ValidationError as exc:
            raise WorkflowSmokeFixtureConflict(
                "并发 active smoke-test 属于旧 graph/validation/deployment 合同",
            ) from exc
        if attempt.fixture_sha256 == frozen_fixture.fixture_sha256:
            return attempt, False
        raise WorkflowSmokeFixtureConflict(
            "同一 workflow/deployment 已有使用另一组素材或参数的 active smoke-test",
        )
    WorkflowSmokeAssetBinding.objects.bulk_create([
        WorkflowSmokeAssetBinding(
            attempt=attempt,
            asset_id=row["asset_id"],
            semantic_key=row["semantic_key"],
            input_slot=row["input_slot"],
            media_kind=row["media_kind"],
            category=row["category"],
            checksum=row["checksum"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
        )
        for row in frozen_fixture.asset_facts
    ])
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
    unresolved_generation = GenerationTask.objects.exclude(
        submission_token="",
    ).exclude(
        submission_state__in=(
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
            if locked.status != WorkflowSmokeAttempt.Status.CANCEL_REQUESTED:
                locked.status = WorkflowSmokeAttempt.Status.RUNNING
            locked.provider_request_id = request_id
            locked.submitted_at = now
            marker = "phase:submitted"
        else:
            if locked.status in {
                WorkflowSmokeAttempt.Status.CANCELLED,
                WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
            }:
                raise ValidationError(
                    "workflow smoke 已取消，禁止开始远端提交",
                    code="workflow_smoke_cancelled_before_submission",
                )
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
    if (
        status == WorkflowSmokeAttempt.Status.SUCCEEDED
        and attempt.status in {
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
            WorkflowSmokeAttempt.Status.CANCELLED,
        }
    ):
        status = WorkflowSmokeAttempt.Status.CANCELLED
        result = {}
        code = "workflow_smoke_cancelled_result_discarded"
        message = "取消请求已生效；远端返回的结果未被采纳为 smoke 证据。"
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


@transaction.atomic
def _mark_version_smoke_state(
    attempt_id,
    *,
    expected_claim_token,
    state: str,
    message: str,
) -> bool:
    """Update only the validation generation that created this attempt."""

    seed = WorkflowSmokeAttempt.objects.only(
        "workflow_version_id", "deployment_id",
    ).get(pk=attempt_id)
    deployment = ModelDeployment.objects.select_for_update(of=("self",)).get(
        pk=seed.deployment_id,
    )
    version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).get(
        pk=seed.workflow_version_id,
    )
    attempt = WorkflowSmokeAttempt.objects.select_for_update(of=("self",)).get(
        pk=attempt_id,
    )
    if attempt.claim_token != expected_claim_token:
        return False
    try:
        verify_workflow_smoke_attempt_contract(
            attempt,
            version=version,
            deployment=deployment,
        )
    except ValidationError:
        return False
    version.smoke_test_state = state
    version.smoke_test_message = str(message)[:2000]
    version.smoke_test_evidence = {}
    version.smoke_test_evidence_sha256 = ""
    version.save(update_fields=(
        "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
        "smoke_test_evidence_sha256", "updated_at",
    ))
    return True


def _mark_version_smoke_failed(attempt_id, claim_token, message: str) -> bool:
    """Record an attempt failure without invalidating newer validation."""

    return _mark_version_smoke_state(
        attempt_id,
        expected_claim_token=claim_token,
        state=WorkflowTemplateVersion.ValidationState.FAILED,
        message=message,
    )


def _mark_version_smoke_not_run(attempt_id, claim_token, message: str) -> bool:
    return _mark_version_smoke_state(
        attempt_id,
        expected_claim_token=claim_token,
        state=WorkflowTemplateVersion.ValidationState.NOT_RUN,
        message=message,
    )


def process_workflow_smoke(attempt: WorkflowSmokeAttempt) -> WorkflowSmokeAttempt:
    claim_token = attempt.claim_token
    if claim_token is None:
        raise ValidationError("workflow smoke 未持有 claim")
    if attempt.status == WorkflowSmokeAttempt.Status.CANCEL_REQUESTED and not attempt.submission_started_at:
        _mark_version_smoke_not_run(
            attempt.pk,
            claim_token,
            "真实 smoke-test 已在远端提交前取消。",
        )
        return _finish(
            attempt.pk, claim_token,
            status=WorkflowSmokeAttempt.Status.CANCELLED,
            code="cancelled_before_submission",
            message="烟测在远端提交前取消。",
        )
    if expired_kind(attempt) and not attempt.submission_started_at:
        kind = expired_kind(attempt)
        _mark_version_smoke_failed(
            attempt.pk,
            claim_token,
            f"真实 smoke-test 在远端提交前达到持久化 {kind} deadline。",
        )
        return _finish(
            attempt.pk, claim_token,
            status=WorkflowSmokeAttempt.Status.FAILED,
            code=f"{kind}_timeout",
            message=f"烟测在远端提交前达到持久化 {kind} deadline。",
        )
    submitted_before = bool(attempt.submission_started_at)
    try:
        verify_workflow_smoke_attempt_contract(
            attempt,
            version=attempt.workflow_version,
            deployment=attempt.deployment,
        )
        fixture = verify_persisted_workflow_smoke_fixture(attempt)
        _version, report = smoke_test_workflow_version(
            attempt.workflow_version,
            deployment=attempt.deployment,
            fixture_values=fixture.values,
            fixture_reference_bindings=fixture.reference_bindings,
            fixture_asset_facts=fixture.asset_facts,
            materialized_graph_sha256=fixture.materialized_graph_sha256,
            fixture_sha256=fixture.fixture_sha256,
            smoke_attempt_id=str(attempt.id),
            expected_claim_token=claim_token,
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
        status, code, message = _failure_outcome(current, exc)
        if (
            code != "workflow_smoke_superseded"
            and current.status not in {
                WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
                WorkflowSmokeAttempt.Status.CANCELLED,
            }
        ):
            _mark_version_smoke_failed(attempt.pk, claim_token, message)
        return _finish(
            attempt.pk, claim_token,
            status=status, code=code, message=message,
        )
    return _finish(
        attempt.pk, claim_token,
        status=WorkflowSmokeAttempt.Status.SUCCEEDED,
        result=report,
    )


@transaction.atomic
def request_workflow_smoke_cancel(attempt: WorkflowSmokeAttempt) -> WorkflowSmokeAttempt:
    seed = WorkflowSmokeAttempt.objects.only(
        "workflow_version_id", "deployment_id",
    ).get(pk=attempt.pk)
    deployment = ModelDeployment.objects.select_for_update(of=("self",)).get(
        pk=seed.deployment_id,
    )
    version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).get(
        pk=seed.workflow_version_id,
    )
    attempt = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.status not in ACTIVE:
        raise ValidationError("workflow smoke 已是终态")
    now = timezone.now()
    if not attempt.submission_started_at:
        attempt.status = WorkflowSmokeAttempt.Status.CANCELLED
        attempt.finished_at = now
        attempt.error_code = "cancelled_before_submission"
        attempt.error_message = "烟测在远端提交前取消。"
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
        attempt.next_poll_at = None
        version_message = "真实 smoke-test 已在远端提交前取消。"
    else:
        attempt.status = WorkflowSmokeAttempt.Status.CANCEL_REQUESTED
        attempt.cancel_requested_at = attempt.cancel_requested_at or now
        attempt.next_poll_at = now
        version_message = "真实 smoke-test 已请求取消，等待远端终态。"
    try:
        verify_workflow_smoke_attempt_contract(
            attempt,
            version=version,
            deployment=deployment,
        )
        owns_validation_generation = True
    except ValidationError:
        owns_validation_generation = False
    attempt.save(update_fields=(
        "status", "finished_at", "error_code", "error_message",
        "cancel_requested_at", "next_poll_at", "claim_token", "worker_id",
        "claimed_at", "updated_at",
    ))
    if owns_validation_generation:
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.smoke_test_message = version_message
        version.smoke_test_evidence = {}
        version.smoke_test_evidence_sha256 = ""
        version.save(update_fields=(
            "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
            "smoke_test_evidence_sha256", "updated_at",
        ))
    return attempt


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
