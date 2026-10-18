from __future__ import annotations

from datetime import timedelta
import logging
import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import (
    AssetGenerationJob,
    GenerationTask,
    WorkflowSmokeAttempt,
    WorkerLease,
)
from studio.services.comfyui import (
    ComfyUIClient,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUIHTTPError,
    ComfyUIPreSubmitResourceReleaseError,
)
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    ComfyUIContractUnavailable,
    preflight_comfyui_workflow,
)
from studio.services.provenance import ProvenanceError, persist_task_provenance
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.task_distribution_recovery import (
    claim_task_distribution, process_task_distribution, schedule_initial_distribution,
)
from studio.services.generated_artifacts import (
    inspect_task_output_media,
    record_generation_task_artifacts,
)
from studio.services.provider_registry import ProviderDriverError, restricted_comfyui_runtime_budget, validate_endpoint
from studio.services.execution_definitions import (
    execution_definition_version, stored_execution_contract, verify_current_execution_definition,
)
from studio.services.configuration import resolve_configuration
from studio.services.security_boundaries import boundary_int
from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
)
from studio.services.execution_monitoring import (
    idle_timeout_for_node,
)
from studio.services.production_run_auto_advance import schedule_generation_task_run_advance
from studio.services.execution_production_workflow_queue import (
    locked_prepared_generation_task, prepared_generation_concurrency, read_prepared_generation_task, stage_prepared_generation_task,
    begin_prepared_generation_submission, record_prepared_generation_submission,
    update_prepared_generation_lifecycle,
    claim_prepared_generation_task,
    read_prepared_generation_history, retain_prepared_generation_history,
    PreparedGenerationWaiting, defer_prepared_generation_task,
)
from studio.services.workflow_smoke_resolution import (
    unresolved_workflow_smoke_attempts,
)
from studio.services.execution_production_attempts import superseded_uncertain_task_ids


logger = logging.getLogger(__name__)


class TaskClaimLost(RuntimeError):
    """A late worker observation is not a failure of the current task claim."""


def _claimed_task_rows(task, *, require_running=False, allow_terminal=False):
    if type(task.attempts) is not int or task.attempts < 1 or task.started_at is None:
        raise TaskClaimLost("任务没有有效领取身份")
    rows = GenerationTask.objects.filter(pk=task.pk, attempts=task.attempts, started_at=task.started_at,
        owner_id=task.owner_id, project_id=task.project_id, kind=task.kind)
    if require_running:
        return rows.filter(status=GenerationTask.Status.RUNNING)
    if not allow_terminal:
        return rows.filter(status__in=(GenerationTask.Status.RUNNING, GenerationTask.Status.CANCEL_REQUESTED))
    return rows


def _refresh_claimed_task(task, *, require_running=False, allow_terminal=False, include_outputs=False):
    # Never refresh a stale object into a new worker's claim identity.
    deferred = ("staged_inputs",) + (() if include_outputs else ("outputs",))
    current = _claimed_task_rows(task, require_running=require_running, allow_terminal=allow_terminal).defer(*deferred).first()
    if current is None:
        raise TaskClaimLost("任务已由其他worker领取或已结束")
    for field in GenerationTask._meta.concrete_fields:
        if field.attname not in current.get_deferred_fields():
            setattr(task, field.attname, getattr(current, field.attname))
    task._state.fields_cache.clear()
    return task


def _update_claimed_task(task, updates, *, rejected=False, failure_code=None):
    # Cancel requests may receive telemetry or a cancellation result, but may
    # never be overwritten by a late success/failure/requeue or a new POST.
    require_running = (updates.get("status") in {"queued", "failed", "succeeded"}
        or "submission_token" in updates)
    values = {**updates, "updated_at": timezone.now()}
    condition = _claimed_task_rows(task, require_running=require_running)
    if updates.get("status") in {"failed", "cancel_requested", "cancelled"}:
        changed = update_prepared_generation_lifecycle(task, values, condition=condition, rejected=rejected, failure_code=failure_code)
    else:
        changed = condition.update(**values)
    if changed != 1:
        raise TaskClaimLost("领取已失效或取消已发生，忽略迟到的worker写入")
    for key, value in values.items():
        setattr(task, key, value)


def _save_claimed_task(task, *, update_fields, rejected=False, failure_code=None):
    _update_claimed_task(task, {key: getattr(task, key) for key in update_fields if key != "updated_at"},
        rejected=rejected, failure_code=failure_code)


def task_wait_timeout(task: GenerationTask, requested_timeout: float | None = None) -> float:
    """Return remaining persisted hard time; caller input cannot alter it."""
    runtime = _prepared_task_context(task).media["execution_deadlines"]
    if task.hard_deadline_at is None:
        return float(runtime["hard_timeout_seconds"])
    return max(0.0, (task.hard_deadline_at - timezone.now()).total_seconds())


def _prepared_task_context(task):
    try:
        return read_prepared_generation_task(task)
    except ValidationError as exc:
        raise ComfyUIError("; ".join(exc.messages)) from exc


def _task_node_type(task: GenerationTask, node_id: str) -> str:
    graph = _prepared_task_context(task).media["payload"]["compiled_workflow"]["graph"]
    node = graph.get(str(node_id))
    return str(node["class_type"]) if isinstance(node, dict) else ""


def _task_idle_seconds(task: GenerationTask, *, node_id: str = "") -> float:
    context = _prepared_task_context(task)
    graph = context.media["payload"]["compiled_workflow"]["graph"]
    node = graph.get(str(node_id)) if node_id else None
    return idle_timeout_for_node(
        context.media["execution_monitoring"], node_id=node_id,
        node_type=str(node["class_type"]) if isinstance(node, dict) else "",
        frozen_runtime_idle_timeout_seconds=context.media["execution_deadlines"]["idle_timeout_seconds"],
    )


def _mark_task_meaningful_progress(
    task: GenerationTask,
    marker: str,
    *,
    now=None,
    node_id: str = "",
) -> bool:
    observed = now or timezone.now()
    updates = meaningful_deadline_updates(
        task,
        marker=marker,
        idle_seconds=_task_idle_seconds(task, node_id=node_id),
        now=observed,
    )
    if not updates:
        return False
    changed = GenerationTask.objects.filter(pk=task.pk, attempts=task.attempts, started_at=task.started_at,
        status__in=(GenerationTask.Status.RUNNING, GenerationTask.Status.CANCEL_REQUESTED),
        progress_marker=task.progress_marker, hard_deadline_at=task.hard_deadline_at,
    ).update(**updates, updated_at=observed)
    if changed != 1:
        return False
    for field, value in updates.items():
        setattr(task, field, value)
    return True


def _ensure_task_deadlines(task: GenerationTask) -> GenerationTask:
    if task.deadline_started_at is not None:
        return task
    runtime = _prepared_task_context(task).media["execution_deadlines"]
    observed = timezone.now()
    hard_seconds = float(runtime["hard_timeout_seconds"])
    deadlines = initial_deadlines(
        # A monitoring window is an upper bound on silence, never authority to
        # enlarge the immutable task hard limit.  Initial creation mirrors the
        # already-capped node-transition renewal path.
        idle_seconds=min(_task_idle_seconds(task), hard_seconds),
        hard_seconds=hard_seconds,
        now=observed,
        marker="phase:claimed",
    )
    _claimed_task_rows(task, require_running=True).filter(deadline_started_at__isnull=True).update(
        deadline_started_at=deadlines.started_at,
        hard_deadline_at=deadlines.hard_deadline_at,
        idle_deadline_at=deadlines.idle_deadline_at,
        last_meaningful_progress_at=deadlines.last_meaningful_progress_at,
        progress_marker=deadlines.progress_marker,
        updated_at=observed,
    )
    _refresh_claimed_task(task)
    return task


def comfy_client_for_task(task: GenerationTask) -> ComfyUIClient:
    """Same frozen target for submit/reconciliation; activation is an admission fact."""
    context = _prepared_task_context(task)
    try:
        current = verify_current_execution_definition(stored_execution_contract(context.attempt.execution_instance_version_id))
        if current.sha256 != context.media["definition_sha256"]:
            raise ValidationError("ComfyUI任务目标已变化，不能用新连接处理原请求")
        version = execution_definition_version(context.attempt.execution_instance_version_id)
        deployment = version.deployment
        config = {**deployment.provider.driver_config, **deployment.runtime_config}
        budget = restricted_comfyui_runtime_budget(config)
        endpoint = validate_endpoint(config["base_url"], resolve_dns=True)
    except ValidationError as exc:
        raise ComfyUIError("; ".join(exc.messages)) from exc
    return ComfyUIClient(base_url=endpoint, timeout=budget["timeout_seconds"], endpoint_prevalidated=True,
        pre_submit_resource_release_policy=config.get("pre_submit_resource_release"))


def _persist_task_observation(task, **values):
    from studio.services.execution_task_observations import record_task_observation
    if not record_task_observation(task, **values):
        raise TaskClaimLost("任务检查返回时原领取已失效，不记录到新领取")


def _release_resources_for_new_submission(
    task: GenerationTask,
    client: ComfyUIClient,
) -> None:
    release = getattr(client, "release_resources_before_submit", None)
    if not callable(release):
        return
    _refresh_claimed_task(task, require_running=True)
    try:
        audit = release()
    except ComfyUIPreSubmitResourceReleaseError as exc:
        _persist_task_observation(task, kind="resource_release", report=exc.audit)
        raise
    if audit is not None:
        _persist_task_observation(task, kind="resource_release", report=audit)


def claim_next_task() -> GenerationTask | None:
    """Claim one queued task safely. Works on SQLite and uses row locks on PostgreSQL."""
    with transaction.atomic():
        WorkerLease.objects.get_or_create(key="generation-claim")
        # A single durable mutex row serializes count + claim across every
        # PostgreSQL worker, so TASK_MAX_CONCURRENT=1 cannot be raced.
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
        if WorkflowSmokeAttempt.objects.filter(
            status__in=(
                WorkflowSmokeAttempt.Status.SUBMITTING,
                WorkflowSmokeAttempt.Status.RUNNING,
                WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
            ),
        ).exists():
            return None
        # A failed/cancelled task with a persisted but non-terminal remote token
        # is a circuit breaker.  Claiming another task could duplicate GPU work
        # while the previous ComfyUI prompt is still alive but unobservable.
        unresolved_remote = GenerationTask.objects.exclude(pk__in=superseded_uncertain_task_ids()).exclude(submission_token="").exclude(
            submission_state__in=(
                GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            ),
        ).filter(
            status__in=(
                GenerationTask.Status.FAILED,
                GenerationTask.Status.CANCELLED,
                GenerationTask.Status.CANCEL_REQUESTED,
            ),
        ).exists()
        if unresolved_remote:
            return None
        # A queued task with a non-terminal token is a reattachment to an
        # already submitted remote prompt, not a fresh generation attempt. It
        # must be claimed ahead of every unsubmitted task regardless of normal
        # priority or max_attempts; otherwise another /prompt could be posted
        # while the recovered GPU job is still running.
        candidate = (
            GenerationTask.objects.all()
            .filter(status=GenerationTask.Status.QUEUED)
            .exclude(submission_token="")
            .exclude(submission_state__in=(
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            ))
            .order_by("queued_at")
            .first()
        )
        if candidate is not None:
            try:
                _prepared_task_context(candidate)
                if any(getattr(candidate, key) is None for key in (
                        "deadline_started_at", "hard_deadline_at", "idle_deadline_at")):
                    raise ComfyUIError("已提交任务缺少原始时限，不能在恢复领取时重新计时")
            except ComfyUIError as exc:
                # A recovered token is never converted into a fresh POST. Mark
                # the corrupt frozen execution fact failed while preserving its
                # remote identity for explicit reconciliation.
                now = timezone.now()
                changed = update_prepared_generation_lifecycle(candidate, {
                    "status": GenerationTask.Status.FAILED, "finished_at": now, "heartbeat_at": now,
                    "error_message": str(exc), "remote_state": GenerationTask.RemoteState.FAILED,
                    "remote_error": str(exc),
                }, condition=GenerationTask.objects.filter(pk=candidate.pk, status="queued"))
                if changed:
                    schedule_generation_task_run_advance(candidate.pk)
                return None
        if candidate is None:
            # A merely queued Smoke may take priority over NEW work, but not
            # over reconciliation of a persisted prompt. Otherwise each queue
            # waits for the other and the existing remote job is never polled.
            if WorkflowSmokeAttempt.objects.filter(status=WorkflowSmokeAttempt.Status.QUEUED).exists():
                return None
            running_tasks = list(
                GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING)
                .only("id", "owner_id", "project_id", "kind", "created_at")
            )
            running_count = len(running_tasks)
            hard_ceiling = min(
                int(settings.TASK_MAX_CONCURRENT),
                boundary_int("security.production.max_concurrency"),
            )
            if running_count >= hard_ceiling:
                return None
            running_limits = [hard_ceiling]
            for running_task in running_tasks:
                try:
                    running_limits.append(prepared_generation_concurrency(running_task))
                except ValidationError:
                    # Do not enlarge a pool while an active platform task has
                    # lost its authoritative capacity evidence.
                    logger.error(
                        "Running task %s has invalid preparation-backed concurrency facts",
                        running_task.id,
                    )
                    return None
            active_pool_limit = min(running_limits)
            candidates = list(
                GenerationTask.objects.all()
                .filter(status=GenerationTask.Status.QUEUED, submission_token="")
                .filter(platform_attempts__role="primary", platform_attempts__ignore_late_result=False,
                    platform_attempts__status__in=("queued", "running"),
                    platform_attempts__preparation__status="dispatched",
                    platform_attempts__stage_run__status="running",
                    platform_attempts__stage_run__production_run__status="running")
                .exclude(platform_attempts__error_code="execution_resource_confirmation_required")
                .filter(Q(platform_attempts__next_poll_at__isnull=True) | Q(platform_attempts__next_poll_at__lte=timezone.now()))
                .order_by("-priority", "queued_at")[:256]
            )
            for queued in candidates:
                try:
                    # This reads the actual Preparation and its original Run.
                    # Missing old rows are not inferred from task metadata.
                    candidate_limit = min(
                        hard_ceiling,
                        active_pool_limit,
                        prepared_generation_concurrency(queued),
                    )
                except ValidationError as exc:
                    now = timezone.now()
                    changed = update_prepared_generation_lifecycle(queued, {
                        "status": GenerationTask.Status.FAILED, "finished_at": now, "heartbeat_at": now,
                        "error_message": str(exc), "remote_state": GenerationTask.RemoteState.FAILED,
                    }, condition=GenerationTask.objects.filter(pk=queued.pk, status="queued", submission_token=""))
                    if changed:
                        schedule_generation_task_run_advance(queued.pk)
                    continue
                if running_count < candidate_limit:
                    candidate = queued
                    break
        if not candidate:
            return None
        claim_time = timezone.now()
        deadline_updates = {}
        if candidate.deadline_started_at is None:
            runtime = _prepared_task_context(candidate).media["execution_deadlines"]
            hard_seconds = float(runtime["hard_timeout_seconds"])
            deadlines = initial_deadlines(
                idle_seconds=min(_task_idle_seconds(candidate), hard_seconds),
                hard_seconds=hard_seconds,
                now=claim_time,
                marker="phase:claimed",
            )
            deadline_updates = {
                "deadline_started_at": deadlines.started_at,
                "hard_deadline_at": deadlines.hard_deadline_at,
                "idle_deadline_at": deadlines.idle_deadline_at,
                "last_meaningful_progress_at": deadlines.last_meaningful_progress_at,
                "progress_marker": deadlines.progress_marker,
            }
        return claim_prepared_generation_task(candidate, {
            "status": GenerationTask.Status.RUNNING, "started_at": claim_time, "finished_at": None,
            "progress": 1, "progress_source": GenerationTask.ProgressSource.NONE, "comfy_progress": None,
            "progress_node_id": "", "progress_updated_at": None,
            "progress_telemetry_state": GenerationTask.ProgressTelemetryState.UNKNOWN, "progress_telemetry_error": "",
            "attempts": candidate.attempts + 1, "error_message": "", "heartbeat_at": claim_time,
            "stalled_at": None, "remote_state": GenerationTask.RemoteState.CLAIMED, "remote_error": "",
            **deadline_updates,
            "updated_at": claim_time,
        })


def fail_claimed_task_preflight(
    task: GenerationTask,
    exc: Exception,
) -> GenerationTask:
    """Persist a claim-time validation failure before any remote submission."""

    message = str(exc).strip() or "媒体任务执行前校验失败"
    now = timezone.now()
    changed = update_prepared_generation_lifecycle(task, {
        "status": GenerationTask.Status.FAILED, "finished_at": now, "heartbeat_at": now,
        "progress": 0, "error_message": message, "remote_state": GenerationTask.RemoteState.FAILED,
        "remote_error": message,
    }, condition=_claimed_task_rows(task, require_running=True))
    if changed:
        schedule_generation_task_run_advance(task.pk)
    return GenerationTask.objects.get(pk=task.pk)




class SubmissionUncertain(ComfyUIError):
    """Remote acceptance cannot be proven; never auto-submit a second time."""


class SubmissionRejected(ComfyUIError):
    """ComfyUI returned an HTTP response proving the prompt was rejected."""


class RemoteExecutionStalled(ComfyUIExecutionError):
    """The remote executor is reachable enough to diagnose, but cannot make progress."""


class TaskCancellationFinalized(RuntimeError):
    """Internal control-flow signal used after a durable cancellation is written."""


def _record_runtime_state(
    task: GenerationTask,
    remote_state: str,
    *,
    remote_error: str | None = None,
    stalled: bool = False,
) -> None:
    """Persist a lightweight worker heartbeat without rewriting lifecycle timestamps."""
    now = timezone.now()
    updates = {"heartbeat_at": now, "remote_state": remote_state}
    if remote_error is not None:
        updates["remote_error"] = remote_error
    if stalled and task.stalled_at is None:
        updates["stalled_at"] = now
        task.stalled_at = now
    _update_claimed_task(task, updates)
    task.heartbeat_at = now
    task.remote_state = remote_state
    if remote_error is not None:
        task.remote_error = remote_error


def _record_progress_telemetry(
    task: GenerationTask,
    telemetry_state: str,
    *,
    error: str = "",
    source: str | None = None,
    comfy_progress: int | None = None,
    clear_comfy_progress: bool = False,
    node_id: str | None = None,
    observed_at=None,
) -> None:
    """Persist advisory progress telemetry independently of task liveness."""
    updates = {
        "progress_telemetry_state": telemetry_state,
        "progress_telemetry_error": error,
    }
    task.progress_telemetry_state = telemetry_state
    task.progress_telemetry_error = error
    if source is not None:
        updates["progress_source"] = source
        task.progress_source = source
    if clear_comfy_progress:
        updates["comfy_progress"] = None
        task.comfy_progress = None
    elif comfy_progress is not None:
        updates["comfy_progress"] = max(0, min(100, int(comfy_progress)))
        task.comfy_progress = updates["comfy_progress"]
    if node_id is not None:
        updates["progress_node_id"] = str(node_id)
        task.progress_node_id = str(node_id)
    if observed_at is not None:
        updates["progress_updated_at"] = observed_at
        task.progress_updated_at = observed_at
    _update_claimed_task(task, updates)


def _best_effort_cancel(client: ComfyUIClient, prompt_id: str) -> tuple[bool, str]:
    cancel = getattr(client, "cancel", None)
    if not callable(cancel):
        return False, "ComfyUI client does not support cancellation"
    try:
        cancel(prompt_id)
        return True, ""
    except ComfyUIError as exc:
        return False, str(exc)


def _cancel_claimed_task(task, client, prompt_id):
    _refresh_claimed_task(task)
    return _best_effort_cancel(client, prompt_id)


def _cancelled_task_updates(
    task: GenerationTask,
    *,
    remote_confirmed: bool,
    remote_error: str = "",
) -> dict:
    now = timezone.now()
    confirmed = remote_confirmed or not task.submission_token
    updates = {
        "status": GenerationTask.Status.CANCELLED if confirmed else GenerationTask.Status.CANCEL_REQUESTED,
        "progress": 0,
        "finished_at": now if confirmed else None,
        "heartbeat_at": now,
        "remote_state": GenerationTask.RemoteState.CANCELLED if confirmed else GenerationTask.RemoteState.CANCELLING,
        "remote_error": remote_error,
        "updated_at": now,
    }
    if remote_confirmed and task.submission_token:
        updates["submission_state"] = GenerationTask.SubmissionState.TERMINAL_CANCELLED
    return updates


def _finalize_cancelled(task, *, remote_confirmed, remote_error="", remote_succeeded=False):
    updates = _cancelled_task_updates(task, remote_confirmed=remote_confirmed, remote_error=remote_error)
    if remote_succeeded:
        # Cancel local adoption, not the already completed remote execution.
        updates["submission_state"] = GenerationTask.SubmissionState.TERMINAL_SUCCEEDED
        updates["remote_state"] = GenerationTask.RemoteState.SUCCEEDED
    _update_claimed_task(task, updates)
    schedule_generation_task_run_advance(task.pk)


def _wait_for_task_completion(
    task: GenerationTask,
    client: ComfyUIClient,
    prompt_id: str,
    *,
    progress_socket=None,
) -> dict:
    """Poll ComfyUI while persisting heartbeats and honoring durable cancellation.

    Every production client must expose the durable history/queue contract.
    Missing methods are a programming/configuration error, not authority to
    enter a second polling path with different defaults.
    """
    history_method = getattr(client, "history", None)
    queued_method = getattr(client, "is_queued", None)
    if not callable(history_method) or not callable(queued_method):
        raise ComfyUIError("ComfyUI client 缺少 durable history/is_queued 合同")
    event_reader = getattr(client, "read_progress_event", None)
    if progress_socket is not None and not callable(event_reader):
        raise ComfyUIError("ComfyUI client 缺少 read_progress_event 进度合同")

    runtime = resolve_configuration(required_keys=(
        "runtime.task_remote_poll_seconds",
        "runtime.task_heartbeat_seconds",
        "runtime.task_health_check_interval_seconds",
        "runtime.task_health_failure_limit",
        "runtime.task_remote_missing_grace_seconds",
        "runtime.task_cancel_grace_seconds",
    )).values
    poll_seconds = float(runtime["runtime.task_remote_poll_seconds"])
    heartbeat_seconds = float(runtime["runtime.task_heartbeat_seconds"])
    health_interval = float(runtime["runtime.task_health_check_interval_seconds"])
    health_failure_limit = int(runtime["runtime.task_health_failure_limit"])
    missing_grace = float(runtime["runtime.task_remote_missing_grace_seconds"])
    cancel_grace = float(runtime["runtime.task_cancel_grace_seconds"])
    next_heartbeat = 0.0
    next_health_check = 0.0
    consecutive_health_failures = 0
    transient_poll_error = ""
    missing_since: float | None = None
    cancel_since: float | None = None
    cancel_sent = False

    def sampler_progress():
        nonlocal progress_socket
        if progress_socket is None:
            return
        try:
            observation = event_reader(
                progress_socket, prompt_id, timeout=min(0.05, poll_seconds),
            )
        except Exception as exc:
            _record_progress_telemetry(
                task,
                GenerationTask.ProgressTelemetryState.UNAVAILABLE,
                error=f"ComfyUI progress WebSocket read failed: {exc}",
            )
            _close_task_progress_socket(progress_socket)
            progress_socket = None
            return
        if observation is None:
            return
        telemetry_state = str(getattr(observation, "telemetry_state", "connected") or "connected")
        telemetry_error = str(getattr(observation, "error", "") or "")
        event_type = str(getattr(observation, "event_type", "") or "")
        node_id = str(getattr(observation, "node_id", "") or "")
        fraction = getattr(observation, "fraction", None)
        observed_at = timezone.now()
        if telemetry_state == GenerationTask.ProgressTelemetryState.UNAVAILABLE:
            _record_progress_telemetry(
                task,
                GenerationTask.ProgressTelemetryState.UNAVAILABLE,
                error=telemetry_error or "ComfyUI progress WebSocket became unavailable",
                node_id=node_id or None,
            )
            _close_task_progress_socket(progress_socket)
            progress_socket = None
            return
        if fraction is None:
            invalidates_quantitative_progress = event_type in {
                "executing", "progress", "progress_state",
            }
            meaningful = bool(node_id and node_id != task.progress_node_id)
            _record_progress_telemetry(
                task,
                GenerationTask.ProgressTelemetryState.CONNECTED,
                source=(
                    GenerationTask.ProgressSource.NONE
                    if invalidates_quantitative_progress else None
                ),
                clear_comfy_progress=invalidates_quantitative_progress,
                node_id=node_id or None,
                observed_at=observed_at if meaningful else None,
            )
            if meaningful:
                _mark_task_meaningful_progress(
                    task,
                    f"node:{node_id}",
                    now=observed_at,
                    node_id=node_id,
                )
            return

        raw_percent = max(0, min(100, int(round(float(fraction) * 100))))
        previous_percent = task.comfy_progress
        previous_node = task.progress_node_id
        node_changed = bool(node_id and node_id != previous_node)
        telemetry_advanced = (
            node_changed
            or previous_percent is None
            or raw_percent > previous_percent
        )
        _record_progress_telemetry(
            task,
            GenerationTask.ProgressTelemetryState.RECEIVING,
            source=GenerationTask.ProgressSource.COMFYUI,
            comfy_progress=raw_percent,
            node_id=node_id or None,
            observed_at=observed_at if telemetry_advanced else None,
        )
        # Quantitative sampler updates remain visible telemetry, but do not
        # create an unbounded sliding deadline. A configured node receives its
        # idle window exactly once when execution enters that node.
        if node_changed:
            _mark_task_meaningful_progress(
                task,
                f"node:{node_id}",
                now=observed_at,
                node_id=node_id,
            )
        # 0-10% covers validation/upload/queue submission; keep 96-99% for
        # decode, mux, provenance and final output verification after sampling.
        value = min(95, max(10, 10 + int(round(float(fraction) * 85))))
        if value <= task.progress:
            return
        task.progress = value
        _update_claimed_task(task, {"progress": value})

    def heartbeat(state: str, *, error: str | None = None, stalled: bool = False, force: bool = False):
        nonlocal next_heartbeat
        current = time.monotonic()
        if force or current >= next_heartbeat:
            _record_runtime_state(task, state, remote_error=error, stalled=stalled)
            next_heartbeat = current + heartbeat_seconds

    heartbeat(GenerationTask.RemoteState.RUNNING, error="", force=True)

    deadline_kind = "hard"
    while True:
        _refresh_claimed_task(task)
        current = time.monotonic()
        # Consume a pending node-transition event before deciding that idle
        # expired. Repeated events for the same node cannot renew the window.
        sampler_progress()
        deadline_kind = expired_kind(task) or ""
        if deadline_kind:
            break

        if task.status in {
            GenerationTask.Status.CANCEL_REQUESTED,
            GenerationTask.Status.CANCELLED,
        }:
            cancel_since = cancel_since or current
            heartbeat(GenerationTask.RemoteState.CANCELLING, force=True)
            cancel_ok, cancel_error = (True, "") if cancel_sent else _cancel_claimed_task(task, client, prompt_id)
            cancel_sent = cancel_sent or cancel_ok
            try:
                still_queued = bool(queued_method(prompt_id))
            except ComfyUIError as exc:
                still_queued = True
                cancel_error = cancel_error or str(exc)
            if cancel_ok and not still_queued:
                _finalize_cancelled(task, remote_confirmed=True)
                raise TaskCancellationFinalized()
            if current - cancel_since >= cancel_grace:
                reason = "ComfyUI did not confirm cancellation before the cancellation grace period expired"
                if cancel_error:
                    reason = f"{reason}: {cancel_error}"
                heartbeat(GenerationTask.RemoteState.DEGRADED, error=reason, stalled=True, force=True)
                raise RemoteExecutionStalled(
                    reason,
                    remote_status={
                        "reason": "cancel_timeout",
                        "cancel_error": cancel_error,
                        "terminal_confirmed": False,
                    },
                )

        poll_error = ""
        try:
            history = history_method(prompt_id)
        except ComfyUIError as exc:
            history = None
            poll_error = str(exc)
        if history is not None:
            heartbeat(GenerationTask.RemoteState.SUCCEEDED, error="", force=True)
            return history

        try:
            is_queued = bool(queued_method(prompt_id))
        except ComfyUIError as exc:
            is_queued = None
            queue_error = str(exc)
            poll_error = f"{poll_error}; {queue_error}" if poll_error else queue_error

        if poll_error:
            transient_poll_error = poll_error
            if is_queued:
                missing_since = None
            heartbeat(
                GenerationTask.RemoteState.DEGRADED,
                error=transient_poll_error,
                force=True,
            )
        elif is_queued:
            missing_since = None
            heartbeat(GenerationTask.RemoteState.RUNNING, error="")
            transient_poll_error = ""
        else:
            missing_since = missing_since or current
            heartbeat(GenerationTask.RemoteState.MISSING, error="Remote prompt is absent from queue and history")
            if current - missing_since >= missing_grace:
                heartbeat(
                    GenerationTask.RemoteState.MISSING,
                    error="Remote prompt is absent from queue and history",
                    stalled=True,
                    force=True,
                )
                raise SubmissionUncertain(
                    "任务已有持久化 submission token，但 ComfyUI 队列和历史均未找到该任务；"
                    "系统不会自动重复提交，请确认远端状态后重试"
                )

        if current >= next_health_check and callable(getattr(client, "health", None)):
            next_health_check = current + health_interval
            try:
                client.health()
            except ComfyUIError as exc:
                consecutive_health_failures += 1
                heartbeat(
                    GenerationTask.RemoteState.DEGRADED,
                    error=str(exc),
                    force=True,
                )
                if consecutive_health_failures >= health_failure_limit:
                    cancel_ok, cancel_error = _cancel_claimed_task(task, client, prompt_id)
                    terminal_confirmed = False
                    if cancel_ok:
                        try:
                            terminal_confirmed = not bool(queued_method(prompt_id))
                        except ComfyUIError as cancel_check_exc:
                            cancel_error = cancel_error or str(cancel_check_exc)
                    reason = (
                        f"ComfyUI health check failed {consecutive_health_failures} consecutive times: {exc}"
                    )
                    if cancel_error:
                        reason = f"{reason}; cancellation also failed: {cancel_error}"
                    heartbeat(
                        GenerationTask.RemoteState.DEGRADED,
                        error=reason,
                        stalled=True,
                        force=True,
                    )
                    raise RemoteExecutionStalled(
                        reason,
                        remote_status={
                            "reason": "health_check_failed",
                            "consecutive_failures": consecutive_health_failures,
                            "cancel_requested": cancel_ok,
                            "terminal_confirmed": terminal_confirmed,
                        },
                    )
            else:
                if consecutive_health_failures:
                    heartbeat(GenerationTask.RemoteState.RUNNING, error="", force=True)
                consecutive_health_failures = 0

        time.sleep(poll_seconds)

    cancel_ok, cancel_error = _cancel_claimed_task(task, client, prompt_id)
    terminal_confirmed = False
    if cancel_ok:
        try:
            terminal_confirmed = not bool(queued_method(prompt_id))
        except ComfyUIError as cancel_check_exc:
            cancel_error = cancel_error or str(cancel_check_exc)
    reason = (
        f"等待 ComfyUI 任务 {prompt_id} 达到 {deadline_kind or 'hard'} deadline，"
        "已尝试终止远端执行"
    )
    if cancel_error:
        reason = f"{reason}；终止失败: {cancel_error}"
    heartbeat(GenerationTask.RemoteState.DEGRADED, error=reason, stalled=True, force=True)
    raise RemoteExecutionStalled(
        reason,
        remote_status={
            "reason": f"{deadline_kind or 'hard'}_timeout",
            "cancel_requested": cancel_ok,
            "terminal_confirmed": terminal_confirmed,
        },
    )


def _open_task_progress_socket(task: GenerationTask, client: ComfyUIClient):
    if not bool(getattr(settings, "TASK_PROGRESS_WEBSOCKET_ENABLED", True)):
        return None
    opener = getattr(client, "open_progress_socket", None)
    if not callable(opener) or not task.client_id:
        return None
    try:
        progress_socket = opener(task.client_id, timeout=0.1)
    except Exception as exc:
        # Progress telemetry is advisory. HTTP history/queue polling remains
        # authoritative and must never fail a valid generation.
        _record_progress_telemetry(
            task,
            GenerationTask.ProgressTelemetryState.UNAVAILABLE,
            error=str(exc),
        )
        return None
    try:
        _record_progress_telemetry(task, GenerationTask.ProgressTelemetryState.CONNECTED, error="")
    except TaskClaimLost:
        _close_task_progress_socket(progress_socket)
        raise
    return progress_socket


def _close_task_progress_socket(progress_socket) -> None:
    if progress_socket is None:
        return
    try:
        progress_socket.close()
    except Exception:
        pass


def prepare_workflow(task: GenerationTask, client: ComfyUIClient) -> dict:
    try:
        return stage_prepared_generation_task(task, client)
    except ValidationError as exc:
        raise ComfyUIError("; ".join(exc.messages)) from exc


def process_task(task: GenerationTask, client: ComfyUIClient | None = None) -> GenerationTask:
    try:
        _refresh_claimed_task(task)
        # Confirmed terminal receipts need local validation, not current DNS.
        if client is None and task.submission_state != GenerationTask.SubmissionState.TERMINAL_SUCCEEDED:
            client = comfy_client_for_task(task)
        return _process_claimed_task(task, client)
    except TaskClaimLost:
        # This is ownership loss, not a new remote failure or retry.
        return GenerationTask.objects.get(pk=task.pk)


def _process_claimed_task(task, client):
    task = _ensure_task_deadlines(task)
    if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
        if task.submission_state == GenerationTask.SubmissionState.TERMINAL_SUCCEEDED:
            _finalize_cancelled(task, remote_confirmed=True, remote_succeeded=True)
            return task
        prompt_id = task.submission_token or task.comfy_prompt_id
        cancel_ok, cancel_error = _cancel_claimed_task(task, client, prompt_id) if prompt_id else (True, "")
        remote_confirmed = not bool(prompt_id)
        if prompt_id and cancel_ok and callable(getattr(client, "is_queued", None)):
            try:
                remote_confirmed = not client.is_queued(prompt_id)
            except ComfyUIError as exc:
                remote_confirmed = False
                cancel_error = str(exc)
        _finalize_cancelled(task, remote_confirmed=remote_confirmed, remote_error=cancel_error)
        return task

    try:
        history = None
        prompt_id = task.submission_token or task.comfy_prompt_id
        had_persisted_submission = bool(prompt_id)
        if prompt_id:
            lookup_error = ""
            is_queued = False
            try:
                history = read_prepared_generation_history(task)
                if history is None and task.submission_state == GenerationTask.SubmissionState.TERMINAL_SUCCEEDED:
                    raise ValidationError("原成功回执缺失，不能重新收集或提交；请核对原任务存储。")
                if history is None:
                    history = client.history(prompt_id)
            except ComfyUIError as exc:
                lookup_error = str(exc)
            if history is None and not lookup_error:
                try:
                    is_queued = client.is_queued(prompt_id)
                except ComfyUIError as exc:
                    lookup_error = str(exc)
            if history is None and (is_queued or lookup_error):
                if is_queued:
                    record_prepared_generation_submission(task, recovered=True)
                task.progress = max(task.progress, 10)
                if task.progress_source == GenerationTask.ProgressSource.NONE:
                    task.progress_source = GenerationTask.ProgressSource.SUBMISSION
                task.progress_updated_at = task.progress_updated_at or task.submission_submitted_at
                _save_claimed_task(task, update_fields=(
                    "submission_state", "submission_submitted_at", "progress",
                    "progress_source", "progress_updated_at", "updated_at",
                ))
                _record_runtime_state(
                    task,
                    (
                        GenerationTask.RemoteState.RECOVERED
                        if is_queued else GenerationTask.RemoteState.DEGRADED
                    ),
                    remote_error=lookup_error,
                )
                progress_socket = _open_task_progress_socket(task, client)
                try:
                    history = _wait_for_task_completion(
                        task, client, prompt_id, progress_socket=progress_socket,
                    )
                finally:
                    _close_task_progress_socket(progress_socket)
            elif history is not None and task.submission_state != GenerationTask.SubmissionState.SUBMITTED:
                record_prepared_generation_submission(task, recovered=True)
            elif history is None and had_persisted_submission:
                raise SubmissionUncertain(
                    "任务已有持久化 submission token，但 ComfyUI 队列和历史均未找到；"
                    "为避免重复提交，系统不会自动再次 POST，已停止任务并要求人工核验"
                )
        if history is None:
            _record_runtime_state(task, GenerationTask.RemoteState.PREPARING, remote_error="")
            executable_workflow = prepare_workflow(task, client)
            # Validate against the connected server after staged uploads make
            # loader choices visible, but before persisting a submission token
            # or calling POST /prompt. A failed preflight is therefore proven
            # not to have consumed GPU work and is safe from duplicate posts.
            preflight_started_at = timezone.now()
            try:
                contract_report = preflight_comfyui_workflow(
                    client,
                    executable_workflow,
                    uploaded_paths={
                        item.get("comfy_path")
                        for item in task.staged_inputs
                        if isinstance(item, dict) and item.get("comfy_path")
                    },
                )
            except ComfyUIError as exc:
                _persist_task_observation(task, kind="preflight", started_at=preflight_started_at,
                    error_code="contract_unavailable" if isinstance(exc, ComfyUIContractUnavailable) else "contract_invalid")
                raise
            _persist_task_observation(task, kind="preflight", started_at=preflight_started_at,
                report=contract_report.as_dict())
            # This is process-global ComfyUI state and therefore runs only for
            # a genuinely new submission: after uploads/live preflight, but
            # before any prompt token is persisted or POST /prompt is called.
            # Recovery paths with an existing token never enter this branch.
            _release_resources_for_new_submission(task, client)
            _refresh_claimed_task(task, require_running=True)
            if not begin_prepared_generation_submission(task):
                return task
            prompt_id = task.submission_token
            progress_socket = _open_task_progress_socket(task, client)
            try:
                _refresh_claimed_task(task, require_running=True)
                try:
                    queue_result = client.queue_prompt(
                        executable_workflow, task.client_id, prompt_id=prompt_id,
                    )
                except ComfyUIHTTPError as exc:
                    # An HTTP response is definitive evidence that /prompt
                    # rejected this request.  Keep the frozen token for audit,
                    # but do not send it through the uncertain-submission path:
                    # no remote reconciliation is needed before a safe retry.
                    if 400 <= exc.status_code < 500:
                        raise SubmissionRejected(str(exc)) from exc
                    raise SubmissionUncertain("ComfyUI提交返回服务端错误，是否已接受未知；不会自动重发") from exc
                except ComfyUIError as exc:
                    raise SubmissionUncertain(
                        "ComfyUI /prompt 返回前连接中断，远端是否已接受未知；"
                        "为避免重复提交，本任务需人工核验"
                    ) from exc
                if queue_result.node_errors:
                    raise ComfyUIExecutionError(
                        f"ComfyUI 节点校验失败: {queue_result.node_errors}",
                        remote_status={"node_errors": queue_result.node_errors},
                    )
                if queue_result.prompt_id != prompt_id:
                    raise SubmissionUncertain("ComfyUI 返回的 prompt_id 与预提交 token 不一致")
                record_prepared_generation_submission(task)
                task.progress = 10
                task.progress_source = GenerationTask.ProgressSource.SUBMISSION
                task.comfy_progress = None
                task.progress_updated_at = task.submission_submitted_at
                _save_claimed_task(task, update_fields=(
                    "submission_state", "submission_submitted_at", "progress", "progress_source",
                    "comfy_progress", "progress_updated_at", "updated_at",
                ))
                _mark_task_meaningful_progress(
                    task, "phase:submitted", now=task.submission_submitted_at,
                )
                _record_runtime_state(task, GenerationTask.RemoteState.SUBMITTED, remote_error="")
                history = _wait_for_task_completion(
                    task, client, prompt_id, progress_socket=progress_socket,
                )
            finally:
                _close_task_progress_socket(progress_socket)
        remote_status = history.get("status", {}) if isinstance(history, dict) else {}
        if not isinstance(remote_status, dict):
            raise SubmissionUncertain("远端 history 缺少有效状态，不能按完成验收")
        if remote_status.get("status_str") == "error":
            record_prepared_generation_submission(task, recovered=had_persisted_submission)
            _update_claimed_task(task, {"submission_state": GenerationTask.SubmissionState.TERMINAL_FAILED,
                "remote_state": GenerationTask.RemoteState.FAILED})
            retain_prepared_generation_history(task, history)
            raise ComfyUIExecutionError(
                f"ComfyUI 执行失败: {remote_status.get('messages', [])}",
                remote_status=remote_status,
            )
        if remote_status.get("status_str") != "success" or remote_status.get("completed") is not True:
            raise SubmissionUncertain("远端历史尚未提供明确的完成状态，不能按生成成功验收")
        _refresh_claimed_task(task)
        if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
            # The returned terminal history already proves remote completion.
            _finalize_cancelled(task, remote_confirmed=True, remote_succeeded=True)
            return task
        record_prepared_generation_submission(task, recovered=had_persisted_submission)
        _update_claimed_task(task, {"submission_state": GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
            "remote_state": GenerationTask.RemoteState.SUCCEEDED})
        retained = retain_prepared_generation_history(task, history)
        if retained is None:
            _refresh_claimed_task(task)
            _finalize_cancelled(task, remote_confirmed=True, remote_succeeded=True)
            return task
        generated_at = timezone.now()
        try:
            observed_outputs = inspect_task_output_media(
                task, (client or ComfyUIClient).extract_outputs(history),
            )
        except (OSError, ProvenanceError, ValueError, ValidationError, ProviderDriverError) as exc:
            logger.exception(
                "Generated output inspection/provenance sealing failed for task %s",
                task.pk,
            )
            raise ComfyUIError(
                "生成文件已返回，但正式 GeneratedArtifact manifest 的媒体探测或 "
                "AI 来源清单无法安全封存；任务不会标记成功: "
                f"{exc}"
            ) from exc
        cancellation_observed = False
        try:
            # The terminal task state and its required managed artifact
            # manifests are one database fact.  Holding the task row lock until
            # both are ready prevents readers from observing a successful task
            # whose required artifacts have not been archived.
            context = _prepared_task_context(task)
            with locked_prepared_generation_task(task, context) as (_stage, _preparation, attempt, locked_task):
                if attempt.generation_task_id != task.pk or not _claimed_task_rows(task).exists():
                    raise TaskClaimLost("产物返回前领取已失效，不得归档或推进阶段")
                if locked_task.status in {
                    GenerationTask.Status.CANCEL_REQUESTED,
                    GenerationTask.Status.CANCELLED,
                }:
                    cancellation_observed = True
                else:
                    # Sidecar sealing writes files AND task.outputs. Keep it
                    # behind the same claim lock as manifest publication;
                    # a late worker must not overwrite the new owner's files.
                    locked_task.outputs = persist_task_provenance(
                        locked_task, observed_outputs, generated_at=generated_at,
                    )
                    locked_task.status = GenerationTask.Status.SUCCEEDED
                    locked_task.progress = 100
                    locked_task.progress_source = GenerationTask.ProgressSource.TERMINAL
                    locked_task.progress_updated_at = generated_at
                    locked_task.finished_at = generated_at
                    locked_task.heartbeat_at = generated_at
                    locked_task.remote_state = GenerationTask.RemoteState.SUCCEEDED
                    locked_task.remote_error = ""
                    locked_task.save(update_fields=(
                        "outputs", "status", "progress", "progress_source",
                        "progress_updated_at", "finished_at", "heartbeat_at",
                        "remote_state", "remote_error", "updated_at",
                    ))
                    record_generation_task_artifacts(locked_task)
                    schedule_initial_distribution(locked_task)
                    task = locked_task
        except TaskClaimLost:
            raise
        except Exception as exc:
            logger.exception("GeneratedArtifact archival failed for task %s", task.pk)
            raise ComfyUIError(
                "生成文件已完成，但正式 GeneratedArtifact manifest 写入失败；"
                "任务不会保留成功状态"
            ) from exc
        if cancellation_observed:
            _refresh_claimed_task(task)
            _finalize_cancelled(
                task,
                remote_confirmed=True,
                remote_succeeded=True,
            )
            return task

        # Disclosure publication is intentionally secondary to the immutable
        # managed master.  It runs only after the success+archive transaction
        # commits and can never downgrade a valid generation task.
        try:
            publication = claim_task_distribution(task.pk)
            if publication is not None:
                process_task_distribution(publication, advance=False)
        except Exception:
            logger.exception("Managed distribution preparation failed for task %s", task.pk)
        # Auto-delivery must observe the worker's publication result, not race
        # its post-commit encoding. Missing copies remain an explicit blocker.
        schedule_generation_task_run_advance(task.pk)
        _refresh_claimed_task(task, allow_terminal=True, include_outputs=True)
    except PreparedGenerationWaiting as exc:
        return defer_prepared_generation_task(task, exc)
    except TaskCancellationFinalized:
        _refresh_claimed_task(task, allow_terminal=True)
    except ComfyUIContractUnavailable as exc:
        # A failed preflight is not a new call. Retrying business execution
        # requires an explicit action; claim count is not a paid retry policy.
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.DEGRADED
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    except ComfyUIContractError as exc:
        # A proven schema mismatch should not burn all automatic attempts. The
        # task remains explicitly retryable after ComfyUI/models/template are
        # repaired, but it has no ambiguous remote submission to reconcile.
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.DEGRADED
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    except SubmissionRejected as exc:
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.FAILED
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress",
            "submission_state", "status", "remote_state", "remote_error", "updated_at",
        ), rejected=True)
    except SubmissionUncertain as exc:
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.MISSING
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "stalled_at", "updated_at",
        ))
    except ComfyUIExecutionError as exc:
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        terminal_confirmed = (
            not isinstance(exc, RemoteExecutionStalled)
            or bool(exc.remote_status.get("terminal_confirmed"))
        )
        if terminal_confirmed:
            task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        task.status = (
            GenerationTask.Status.CANCELLED
            if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}
            and not isinstance(exc, RemoteExecutionStalled)
            else GenerationTask.Status.FAILED
        )
        task.remote_state = (
            GenerationTask.RemoteState.FAILED
            if terminal_confirmed
            else GenerationTask.RemoteState.DEGRADED
        )
        task.remote_error = str(exc)
        if isinstance(exc, RemoteExecutionStalled) and task.stalled_at is None:
            task.stalled_at = now
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "submission_state",
            "status", "remote_state", "remote_error", "stalled_at", "updated_at",
        ))
    except ComfyUIPreSubmitResourceReleaseError as exc:
        # The release endpoint is separate from /prompt.  Its failure is a
        # definitive fail-closed pre-submit fact, never a missing/uncertain
        # remote generation and never an automatic retry.
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.DEGRADED
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    except (ComfyUIError, ValidationError, ProviderDriverError) as exc:
        _refresh_claimed_task(task)
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
            task.status = (GenerationTask.Status.CANCEL_REQUESTED
                if task.submission_token and task.submission_state not in {"terminal_succeeded", "terminal_failed", "terminal_cancelled"}
                else GenerationTask.Status.CANCELLED)
            if task.status == GenerationTask.Status.CANCEL_REQUESTED:
                task.finished_at = None
        elif task.submission_token or task.comfy_prompt_id:
            # A persisted token whose remote outcome is not definitive must
            # never be cleared or automatically posted a second time.
            task.status = GenerationTask.Status.FAILED
        else:
            task.status = GenerationTask.Status.FAILED
        task.remote_state = (GenerationTask.RemoteState.SUCCEEDED
            if task.submission_state == GenerationTask.SubmissionState.TERMINAL_SUCCEEDED
            else GenerationTask.RemoteState.FAILED)
        task.remote_error = str(exc)
        _save_claimed_task(task, update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ), failure_code="execution_response_storage_failed" if isinstance(exc, ExecutionResponseStorageError) else None)
    if task.status in {
        GenerationTask.Status.FAILED,
        GenerationTask.Status.CANCELLED,
    }:
        schedule_generation_task_run_advance(task.pk)
    return task


def _recovery_task_rows(task):
    """Remote observation may update only the unchanged recovery candidate.

    A heartbeat during network I/O invalidates recovery, even without a new
    claim. Both workers and recovery use the same task row, not a second lease.
    """
    return GenerationTask.objects.filter(pk=task.pk, attempts=task.attempts, started_at=task.started_at,
        status=task.status, heartbeat_at=task.heartbeat_at, updated_at=task.updated_at,
        submission_token=task.submission_token, submission_state=task.submission_state,
        owner_id=task.owner_id, project_id=task.project_id, kind=task.kind)


def recover_stale_tasks(
    older_than_minutes: float | None = None,
) -> int:
    """Reconcile original tokens, atomically with the actual Attempt and ledger.

    Missing remote history is not proof of rejection. Recovery never posts,
    allocates another attempt, rewinds a claim count or renews a deadline.
    """
    if older_than_minutes is None:
        older_than_minutes = resolve_configuration(required_keys=(
            "runtime.worker_stale_recovery_minutes",
        )).values["runtime.worker_stale_recovery_minutes"]
    if (isinstance(older_than_minutes, bool)
            or not isinstance(older_than_minutes, (int, float)) or older_than_minutes <= 0):
        raise ValidationError("陈旧任务回收阈值必须是正分钟数")
    threshold = timezone.now() - timedelta(minutes=older_than_minutes)
    cancel_grace = float(resolve_configuration(
        required_keys=("runtime.task_cancel_grace_seconds",),
    ).values["runtime.task_cancel_grace_seconds"])
    cancel_threshold = timezone.now() - timedelta(seconds=max(0.0, cancel_grace))
    reconcilable = ("prepared", "submitted", "recovered")
    query = GenerationTask.objects.defer("staged_inputs", "outputs").exclude(platform_attempts__retry_successor__isnull=False)
    candidates = list(query.filter(
        Q(status="running") & (Q(heartbeat_at__lt=threshold) | Q(heartbeat_at__isnull=True, updated_at__lt=threshold))
        | Q(status="cancel_requested", updated_at__lte=cancel_threshold)
        | Q(status="failed", submission_state__in=reconcilable) & ~Q(submission_token="")
    ).order_by("updated_at"))
    recovered = 0
    for task in candidates:
        def persist(updates, *, remote_present=False, failure_code=None):
            return update_prepared_generation_lifecycle(task, updates,
                condition=_recovery_task_rows(task), remote_present=remote_present, failure_code=failure_code)

        def unresolved(message):
            now = timezone.now()
            cancelling = task.status == "cancel_requested"
            changed = persist({"status": "cancel_requested" if cancelling else "failed",
                "progress": 0, "finished_at": None if cancelling else task.finished_at or now,
                "heartbeat_at": now, "stalled_at": task.stalled_at or now,
                "remote_state": "cancelling" if cancelling else "degraded",
                "remote_error": message,
                "error_message": "原提交结果尚未确认；保留提交标识与预算，不会自动重发。"})
            if changed:
                schedule_generation_task_run_advance(task.pk)
            return changed

        if not _recovery_task_rows(task).exists():
            continue
        # Only stale running claims can resume local archival. Terminal local
        # failures are not turned into automatic retries by this receipt.
        if task.status == "running" and task.submission_token:
            try:
                retained_history = read_prepared_generation_history(task)
            except ValidationError as exc:
                changed = persist({"status": "failed", "progress": 0, "finished_at": timezone.now(),
                    "heartbeat_at": timezone.now(), "remote_error": str(exc),
                    "error_message": "原回执无法校验；不会重新收集或提交。"},
                    failure_code="execution_response_storage_failed" if isinstance(exc, ExecutionResponseStorageError) else None)
                recovered += changed
                if changed:
                    schedule_generation_task_run_advance(task.pk)
                continue
            if retained_history is not None:
                succeeded = retained_history["status"]["status_str"] == "success"
                updates = {"status": "queued" if succeeded else "failed",
                    "submission_state": "terminal_succeeded" if succeeded else "terminal_failed",
                    "progress": max(task.progress, 10) if succeeded else 0,
                    "finished_at": None if succeeded else timezone.now(), "heartbeat_at": timezone.now(),
                    "remote_state": "succeeded" if succeeded else "failed", "remote_error": "",
                    "error_message": "已读取原终态回执；不重新收集或生成。"}
                changed = persist(updates, remote_present=succeeded)
                recovered += changed
                if changed and not succeeded:
                    schedule_generation_task_run_advance(task.pk)
                continue
        try:
            task_client = comfy_client_for_task(task)
        except ComfyUIError as exc:
            recovered += unresolved(str(exc))
            continue
        prompt_id = task.submission_token or task.comfy_prompt_id
        if not _recovery_task_rows(task).exists():
            continue
        if task.status == "cancel_requested":
            cancel_error = ""
            remote_confirmed = not bool(prompt_id)
            if prompt_id:
                _cancel_ok, cancel_error = _best_effort_cancel(task_client, prompt_id)
                try:
                    history = task_client.history(prompt_id)
                    if history is not None:
                        remote_confirmed = True
                    else:
                        remote_confirmed = not task_client.is_queued(prompt_id)
                except ComfyUIError as exc:
                    cancel_error = cancel_error or str(exc)
            changed = persist(_cancelled_task_updates(task,
                remote_confirmed=remote_confirmed, remote_error=cancel_error))
            recovered += changed
            if changed:
                schedule_generation_task_run_advance(task.pk)
            continue
        now = timezone.now()
        if not prompt_id:
            recovered += persist({"status": "queued", "progress": 0, "finished_at": None,
                "heartbeat_at": now, "remote_state": "recovered", "remote_error": "",
                "error_message": "已回收提交前中断的原任务领取；继续使用同一执行准备。"})
            continue
        try:
            history = task_client.history(prompt_id)
            remote_present = history is not None or task_client.is_queued(prompt_id)
        except ComfyUIError as exc:
            recovered += unresolved(str(exc))
            continue
        if remote_present:
            recovered += persist({"status": "queued", "progress": max(task.progress, 10),
                "finished_at": None, "heartbeat_at": now, "remote_state": "recovered",
                "remote_error": "", "error_message": "已找回原远端提交；只恢复结果跟踪，不重新生成。"},
                remote_present=True)
        else:
            recovered += unresolved("远端队列和历史均未找到原提交；历史可能已被清理，不能据此判定未执行。")
    return recovered
