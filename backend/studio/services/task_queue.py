from __future__ import annotations

from datetime import timedelta
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import Asset, GenerationTask, WorkerLease
from studio.services.comfyui import ComfyUIClient, ComfyUIError, ComfyUIExecutionError
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    ComfyUIContractUnavailable,
    preflight_comfyui_workflow,
)
from studio.services.reference_media import (
    H3_REFERENCE_FPS,
    ReferenceMediaNormalizationError,
    inspect_reference_video,
    normalize_paired_audio,
    normalize_reference_video,
)
from studio.services.provenance import ProvenanceError, persist_task_provenance, sha256_file
from studio.services.distribution import prepare_all_task_distributions
from studio.services.workflow import WorkflowBuildError, validate_comfy_graph


def claim_next_task() -> GenerationTask | None:
    """Claim one queued task safely. Works on SQLite and uses row locks on PostgreSQL."""
    with transaction.atomic():
        WorkerLease.objects.get_or_create(key="generation-claim")
        # A single durable mutex row serializes count + claim across every
        # PostgreSQL worker, so TASK_MAX_CONCURRENT=1 cannot be raced.
        WorkerLease.objects.select_for_update().get(key="generation-claim")
        # A failed/cancelled task with a persisted but non-terminal remote token
        # is a circuit breaker.  Claiming another task could duplicate GPU work
        # while the previous ComfyUI prompt is still alive but unobservable.
        unresolved_remote = GenerationTask.objects.exclude(submission_token="").exclude(
            submission_state__in=(
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
        if GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING).count() >= settings.TASK_MAX_CONCURRENT:
            return None
        # A queued task with a non-terminal token is a reattachment to an
        # already submitted remote prompt, not a fresh generation attempt. It
        # must be claimed ahead of every unsubmitted task regardless of normal
        # priority or max_attempts; otherwise another /prompt could be posted
        # while the recovered GPU job is still running.
        candidate = (
            GenerationTask.objects.select_for_update()
            .filter(status=GenerationTask.Status.QUEUED)
            .exclude(submission_token="")
            .exclude(submission_state__in=(
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            ))
            .order_by("queued_at")
            .first()
        )
        if candidate is None:
            candidate = (
                GenerationTask.objects.select_for_update()
                .filter(status=GenerationTask.Status.QUEUED, attempts__lt=models_f_max_attempts())
                .order_by("-priority", "queued_at")
                .first()
            )
        if not candidate:
            return None
        changed = GenerationTask.objects.filter(
            pk=candidate.pk, status=GenerationTask.Status.QUEUED
        ).update(
            status=GenerationTask.Status.RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            progress=1,
            progress_source=GenerationTask.ProgressSource.NONE,
            comfy_progress=None,
            progress_node_id="",
            progress_updated_at=None,
            progress_telemetry_state=GenerationTask.ProgressTelemetryState.UNKNOWN,
            progress_telemetry_error="",
            attempts=candidate.attempts + 1,
            error_message="",
            heartbeat_at=timezone.now(),
            stalled_at=None,
            remote_state=GenerationTask.RemoteState.CLAIMED,
            remote_error="",
        )
        if not changed:
            return None
        candidate.refresh_from_db()
        return candidate


def models_f_max_attempts():
    # Kept as a function so the expression remains migration-independent.
    from django.db.models import F
    return F("max_attempts")


def _replace_staged_tokens(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_staged_tokens(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_staged_tokens(item, mapping) for item in value]
    if isinstance(value, str) and value.startswith("__AUTOPROMPT_ASSET__:"):
        if value not in mapping:
            raise ComfyUIError(f"参考素材尚未完成安全上传: {value}")
        return mapping[value]
    return value


def _collect_staged_tokens(value) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_collect_staged_tokens(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_collect_staged_tokens(item) for item in value), set())
    if isinstance(value, str) and value.startswith("__AUTOPROMPT_ASSET__:"):
        return {value}
    return set()


class SubmissionUncertain(ComfyUIError):
    """Remote acceptance cannot be proven; never auto-submit a second time."""


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
    GenerationTask.objects.filter(pk=task.pk).update(**updates)
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
    GenerationTask.objects.filter(pk=task.pk).update(**updates)


def _best_effort_cancel(client: ComfyUIClient, prompt_id: str) -> tuple[bool, str]:
    cancel = getattr(client, "cancel", None)
    if not callable(cancel):
        return False, "ComfyUI client does not support cancellation"
    try:
        cancel(prompt_id)
        return True, ""
    except ComfyUIError as exc:
        return False, str(exc)


def _finalize_cancelled(
    task: GenerationTask,
    *,
    remote_confirmed: bool,
    remote_error: str = "",
) -> None:
    now = timezone.now()
    updates = {
        "status": GenerationTask.Status.CANCELLED,
        "progress": 0,
        "finished_at": now,
        "heartbeat_at": now,
        "remote_state": GenerationTask.RemoteState.CANCELLED,
        "remote_error": remote_error,
        "updated_at": now,
    }
    if remote_confirmed and task.submission_token:
        updates["submission_state"] = GenerationTask.SubmissionState.TERMINAL_CANCELLED
    GenerationTask.objects.filter(pk=task.pk).update(**updates)
    task.refresh_from_db()


def _wait_for_task_completion(
    task: GenerationTask,
    client: ComfyUIClient,
    prompt_id: str,
    *,
    timeout: float,
    progress_socket=None,
) -> dict:
    """Poll ComfyUI while persisting heartbeats and honoring durable cancellation.

    Production clients expose ``history``/``is_queued``/``health``.  The small
    compatibility fallback keeps third-party test clients working while the
    durable path is used for every real ComfyUI submission.
    """
    history_method = getattr(client, "history", None)
    queued_method = getattr(client, "is_queued", None)
    if not callable(history_method) or not callable(queued_method):
        _record_runtime_state(task, GenerationTask.RemoteState.RUNNING, remote_error="")
        return client.wait_for_completion(prompt_id, timeout=timeout)

    poll_seconds = max(0.05, float(getattr(settings, "TASK_REMOTE_POLL_SECONDS", 1)))
    heartbeat_seconds = max(0.05, float(getattr(settings, "TASK_HEARTBEAT_SECONDS", 5)))
    health_interval = max(0.0, float(getattr(settings, "TASK_HEALTH_CHECK_INTERVAL_SECONDS", 5)))
    health_failure_limit = max(1, int(getattr(settings, "TASK_HEALTH_FAILURE_LIMIT", 3)))
    missing_grace = max(0.0, float(getattr(settings, "TASK_REMOTE_MISSING_GRACE_SECONDS", 5)))
    cancel_grace = max(0.0, float(getattr(settings, "TASK_CANCEL_GRACE_SECONDS", 30)))
    deadline = time.monotonic() + max(0.0, timeout)
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
        event_reader = getattr(client, "read_progress_event", None)
        observation = None
        if callable(event_reader):
            try:
                observation = event_reader(
                    progress_socket, prompt_id, timeout=min(0.05, poll_seconds),
                )
            except Exception as exc:
                _record_progress_telemetry(
                    task,
                    GenerationTask.ProgressTelemetryState.UNAVAILABLE,
                    error=f"ComfyUI progress WebSocket read failed: {exc}",
                    observed_at=timezone.now(),
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
                    observed_at=observed_at,
                )
                _close_task_progress_socket(progress_socket)
                progress_socket = None
                return
            if fraction is None:
                invalidates_quantitative_progress = event_type in {
                    "executing", "progress", "progress_state",
                }
                _record_progress_telemetry(
                    task,
                    GenerationTask.ProgressTelemetryState.CONNECTED,
                    source=(
                        GenerationTask.ProgressSource.NONE
                        if invalidates_quantitative_progress else None
                    ),
                    clear_comfy_progress=invalidates_quantitative_progress,
                    node_id=node_id or None,
                    observed_at=observed_at,
                )
                return
        else:
            # Compatibility with small third-party/test clients that still
            # implement the original fraction-only method.
            reader = getattr(client, "read_progress_socket", None)
            if not callable(reader):
                return
            try:
                fraction = reader(progress_socket, prompt_id, timeout=min(0.05, poll_seconds))
            except Exception as exc:
                _record_progress_telemetry(
                    task,
                    GenerationTask.ProgressTelemetryState.UNAVAILABLE,
                    error=f"ComfyUI progress WebSocket read failed: {exc}",
                    observed_at=timezone.now(),
                )
                _close_task_progress_socket(progress_socket)
                progress_socket = None
                return
            if fraction is None:
                return
            node_id = ""
            observed_at = timezone.now()

        raw_percent = max(0, min(100, int(round(float(fraction) * 100))))
        _record_progress_telemetry(
            task,
            GenerationTask.ProgressTelemetryState.RECEIVING,
            source=GenerationTask.ProgressSource.COMFYUI,
            comfy_progress=raw_percent,
            node_id=node_id or None,
            observed_at=observed_at,
        )
        # 0-10% covers validation/upload/queue submission; keep 96-99% for
        # decode, mux, provenance and final output verification after sampling.
        value = min(95, max(10, 10 + int(round(float(fraction) * 85))))
        if value <= task.progress:
            return
        task.progress = value
        GenerationTask.objects.filter(
            pk=task.pk,
            status__in=(GenerationTask.Status.RUNNING, GenerationTask.Status.CANCEL_REQUESTED),
        ).update(progress=value, updated_at=timezone.now())

    def heartbeat(state: str, *, error: str | None = None, stalled: bool = False, force: bool = False):
        nonlocal next_heartbeat
        current = time.monotonic()
        if force or current >= next_heartbeat:
            _record_runtime_state(task, state, remote_error=error, stalled=stalled)
            next_heartbeat = current + heartbeat_seconds

    heartbeat(GenerationTask.RemoteState.RUNNING, error="", force=True)

    while time.monotonic() < deadline:
        task.refresh_from_db()
        current = time.monotonic()
        sampler_progress()

        if task.status in {
            GenerationTask.Status.CANCEL_REQUESTED,
            GenerationTask.Status.CANCELLED,
        }:
            cancel_since = cancel_since or current
            heartbeat(GenerationTask.RemoteState.CANCELLING, force=True)
            cancel_ok, cancel_error = (True, "") if cancel_sent else _best_effort_cancel(client, prompt_id)
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
                    cancel_ok, cancel_error = _best_effort_cancel(client, prompt_id)
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

    cancel_ok, cancel_error = _best_effort_cancel(client, prompt_id)
    terminal_confirmed = False
    if cancel_ok:
        try:
            terminal_confirmed = not bool(queued_method(prompt_id))
        except ComfyUIError as cancel_check_exc:
            cancel_error = cancel_error or str(cancel_check_exc)
    reason = f"等待 ComfyUI 任务 {prompt_id} 超时，已尝试终止远端执行"
    if cancel_error:
        reason = f"{reason}；终止失败: {cancel_error}"
    heartbeat(GenerationTask.RemoteState.DEGRADED, error=reason, stalled=True, force=True)
    raise RemoteExecutionStalled(
        reason,
        remote_status={
            "reason": "timeout",
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
            observed_at=timezone.now(),
        )
        return None
    _record_progress_telemetry(
        task,
        GenerationTask.ProgressTelemetryState.CONNECTED,
        error="",
        observed_at=timezone.now(),
    )
    return progress_socket


def _close_task_progress_socket(progress_socket) -> None:
    if progress_socket is None:
        return
    try:
        progress_socket.close()
    except Exception:
        pass


def _authorized_asset_source(task: GenerationTask, asset_id: str) -> tuple[Asset, Path]:
    try:
        asset = Asset.objects.get(pk=asset_id, is_active=True)
    except (Asset.DoesNotExist, ValueError) as exc:
        raise ComfyUIError(f"参考素材不存在或已停用: {asset_id}") from exc
    if asset.owner_id not in (None, task.owner_id):
        raise ComfyUIError(f"参考素材不属于当前任务租户: {asset_id}")

    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    if asset.owner_id is None:
        if not asset.local_path or asset.file:
            raise ComfyUIError(f"全局参考素材必须来自只读素材库: {asset_id}")
        path = Path(asset.local_path).resolve()
        allowed_root = asset_root
    elif asset.file:
        try:
            path = Path(asset.file.path).resolve()
        except (NotImplementedError, ValueError, OSError) as exc:
            raise ComfyUIError(f"用户参考素材不是可读取的本地上传: {asset_id}") from exc
        allowed_root = (media_root / "assets" / str(task.owner_id)).resolve()
    elif task.owner.is_staff and asset.local_path:
        path = Path(asset.local_path).resolve()
        if path.is_relative_to(asset_root):
            allowed_root = asset_root
        else:
            allowed_root = media_root
    else:
        raise ComfyUIError(f"普通用户参考素材必须由上传接口写入租户目录: {asset_id}")
    try:
        allowed = path.is_relative_to(allowed_root)
    except (OSError, ValueError):
        allowed = False
    if not allowed:
        raise ComfyUIError(f"参考素材路径越出租户授权目录: {asset_id}")
    if not path.is_file():
        raise ComfyUIError(f"参考素材文件不存在: {asset_id}")
    recorded_checksum = str(asset.checksum or "").casefold()
    actual_checksum = sha256_file(path)
    if (
        len(recorded_checksum) != 64
        or any(character not in "0123456789abcdef" for character in recorded_checksum)
        or actual_checksum != recorded_checksum
    ):
        raise ComfyUIError(
            f"参考素材内容哈希与已确认的素材记录不一致，已在提交 ComfyUI 前阻断: {asset_id}"
        )
    return asset, path


def _staged_record_is_safe(
    record: dict, reference: dict, asset_id: str, staged_records: list[dict],
) -> bool:
    """Reject legacy staged Ref2VA media that has no proven timing contract."""
    if not record.get("token") or not record.get("comfy_path"):
        return False
    # A retry may reuse a previously uploaded ComfyUI input only when both the
    # tenant source bytes and the exact submitted bytes were hashed.  Otherwise
    # the completed output could not expose a complete source-media chain.
    for key in ("source_sha256", "submitted_sha256"):
        digest = str(record.get(key) or "").casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return False
    if reference.get("category") != "video":
        return True
    if str(reference.get("asset_id")) == asset_id:
        return (
            record.get("media_role") == "reference_video"
            and record.get("effective_fps") == H3_REFERENCE_FPS
            and record.get("cfr_guaranteed") is True
            and record.get("original_asset_preserved") is True
        )
    if str(reference.get("audio_asset_id")) == asset_id:
        video_record = next((
            row for row in staged_records
            if str(row.get("asset_id")) == str(reference.get("asset_id"))
            and row.get("media_role") == "reference_video"
            and row.get("cfr_guaranteed") is True
        ), None)
        return (
            record.get("media_role") == "paired_reference_audio"
            and str(record.get("paired_video_asset_id")) == str(reference.get("asset_id"))
            and record.get("duration_aligned") is True
            and video_record is not None
            and record.get("effective_duration_ms") == video_record.get("effective_duration_ms")
        )
    return True


def _verify_reusable_staged_input(
    task: GenerationTask, client: ComfyUIClient, record: dict, asset_id: str,
) -> None:
    """Re-authorize the source before reusing a prior ComfyUI input mapping.

    A persisted ``staged_inputs`` row is only an execution cache.  It must not
    outlive asset deactivation, deletion, tenant ownership, or the exact source
    bytes that were authorized for this task.  Some local/test clients can also
    expose ``staged_input_sha256`` to verify the remote copy itself; the stock
    HTTP client currently has no cheap server-side digest endpoint, so source
    re-authorization remains mandatory even when that optional proof is absent.
    """

    asset, _path = _authorized_asset_source(task, asset_id)
    recorded_source_sha256 = str(record.get("source_sha256") or "").casefold()
    current_source_sha256 = str(asset.checksum or "").casefold()
    if recorded_source_sha256 != current_source_sha256:
        raise ComfyUIError(
            f"参考素材已在暂存后变更，不能复用旧的 ComfyUI 输入: {asset_id}"
        )

    # Optional protocol for a client backed by a locally readable input store
    # or a trusted digest endpoint.  Do not infer this capability from a mock's
    # dynamic attributes; it must be explicitly defined by the client class.
    checker = getattr(type(client), "staged_input_sha256", None)
    if not callable(checker):
        return
    try:
        staged_sha256 = str(checker(client, str(record["comfy_path"])) or "").casefold()
    except ComfyUIError:
        raise
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise ComfyUIError("无法校验已暂存的 ComfyUI 输入副本") from exc
    submitted_sha256 = str(record.get("submitted_sha256") or "").casefold()
    if staged_sha256 != submitted_sha256:
        raise ComfyUIError(
            f"已暂存的 ComfyUI 输入内容哈希不一致，不能安全复用: {asset_id}"
        )


def _safe_normalization_error(exc: Exception, *paths: Path) -> str:
    detail = str(exc)
    for path in paths:
        detail = detail.replace(str(path), path.name)
    return detail


def prepare_workflow(task: GenerationTask, client: ComfyUIClient) -> dict:
    workflow = deepcopy(task.generated_workflow.workflow_json)
    template = task.generated_workflow.template
    if not template.is_active or (
        not task.owner.is_staff and (template.owner_id is not None or not template.is_system)
    ):
        raise ComfyUIError("任务引用的 WorkflowTemplate 不再允许该用户执行")
    try:
        validate_comfy_graph(workflow)
    except WorkflowBuildError as exc:
        raise ComfyUIError(str(exc)) from exc
    required_tokens = _collect_staged_tokens(workflow)
    if not required_tokens:
        return workflow
    prompt_assets = {}
    reference_records = task.generated_workflow.prompt_artifact.structured_payload.get(
        "reference_bindings",
        task.generated_workflow.prompt_artifact.structured_payload.get("reference_assets", []),
    )
    for item in reference_records:
        if not isinstance(item, dict):
            continue
        for key in ("asset_id", "id", "audio_asset_id"):
            if item.get(key):
                prompt_assets[str(item[key])] = item
    mapping = {}
    staged_records = [
        row for row in (task.staged_inputs or []) if isinstance(row, dict)
    ]
    for staged in staged_records:
        token = staged.get("token")
        if not token or not staged.get("comfy_path") or token not in required_tokens:
            continue
        asset_id = token.split(":", 1)[1]
        reference = prompt_assets.get(asset_id)
        if reference and _staged_record_is_safe(
            staged, reference, asset_id, staged_records,
        ):
            _verify_reusable_staged_input(task, client, staged, asset_id)
            mapping[token] = staged["comfy_path"]
    video_plans = {}
    changed = False
    for token in sorted(required_tokens):
        if token in mapping:
            continue
        asset_id = token.split(":", 1)[1]
        item = prompt_assets.get(asset_id)
        if not item:
            raise ComfyUIError(f"参考素材 {asset_id} 不在提示词授权快照中")
        asset, path = _authorized_asset_source(task, asset_id)
        source_sha256 = sha256_file(path)
        staged_path = path
        staged_metadata = {
            "staging_version": 1,
            "media_role": item.get("category") or "image",
        }
        related_paths = [path]
        try:
            with tempfile.TemporaryDirectory(prefix="autoprompt-ref2va-") as temp_dir:
                temp_root = Path(temp_dir)
                if item.get("category") == "video" and str(item.get("asset_id")) == asset_id:
                    plan = video_plans.get(asset_id)
                    if plan is None:
                        plan = inspect_reference_video(path)
                        video_plans[asset_id] = plan
                    staged_path = temp_root / f"{asset_id}-cfr24.mp4"
                    staged_metadata = normalize_reference_video(
                        path,
                        staged_path,
                        include_embedded_audio=bool(item.get("use_embedded_audio")),
                        plan=plan,
                    )
                elif (
                    item.get("category") == "video"
                    and str(item.get("audio_asset_id")) == asset_id
                ):
                    video_asset_id = str(item.get("asset_id"))
                    _, video_path = _authorized_asset_source(task, video_asset_id)
                    related_paths.append(video_path)
                    plan = video_plans.get(video_asset_id)
                    if plan is None:
                        plan = inspect_reference_video(video_path)
                        video_plans[video_asset_id] = plan
                    staged_path = temp_root / f"{asset_id}-paired.wav"
                    staged_metadata = normalize_paired_audio(
                        path,
                        staged_path,
                        paired_video_asset_id=video_asset_id,
                        effective_duration_seconds=plan.effective_duration_seconds,
                    )
                upload = getattr(client, "upload_input_file", None) or getattr(client, "upload_image")
                submitted_sha256 = sha256_file(staged_path)
                response = upload(
                    staged_path,
                    subfolder=f"autoprompt/{task.owner_id}/{task.id}",
                    filename=f"{asset_id}{staged_path.suffix.lower() or '.bin'}",
                )
        except ReferenceMediaNormalizationError as exc:
            raise ComfyUIError(
                "Ref2VA reference media failed CFR24/duration normalization; "
                "the task was blocked before ComfyUI submission: "
                f"{_safe_normalization_error(exc, *related_paths)}"
            ) from exc
        comfy_path = "/".join(filter(None, (response.get("subfolder", ""), response["name"])))
        # Replace a legacy unnormalized mapping for the same token.  Retries
        # must never silently select the old ComfyUI input.
        task.staged_inputs = [
            row for row in staged_records if row.get("token") != token
        ]
        task.staged_inputs.append({
            "asset_id": asset_id, "token": token, "comfy_path": comfy_path,
            "checksum": source_sha256,
            "source_sha256": source_sha256,
            "submitted_sha256": submitted_sha256,
            **staged_metadata,
        })
        staged_records = task.staged_inputs
        mapping[token] = comfy_path
        changed = True
        # Persist each upload mapping immediately so a worker restart does not
        # upload the same tenant file repeatedly.
        task.save(update_fields=("staged_inputs", "updated_at"))
    if changed:
        task.save(update_fields=("staged_inputs", "updated_at"))
    return _replace_staged_tokens(workflow, mapping)


def process_task(task: GenerationTask, client: ComfyUIClient | None = None, wait_timeout: float = 900) -> GenerationTask:
    client = client or ComfyUIClient()
    task.refresh_from_db()
    if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
        prompt_id = task.submission_token or task.comfy_prompt_id
        cancel_ok, cancel_error = _best_effort_cancel(client, prompt_id) if prompt_id else (True, "")
        remote_confirmed = cancel_ok
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
                history = client.history(prompt_id)
            except ComfyUIError as exc:
                lookup_error = str(exc)
            if history is None and not lookup_error:
                try:
                    is_queued = client.is_queued(prompt_id)
                except ComfyUIError as exc:
                    lookup_error = str(exc)
            if history is None and (is_queued or lookup_error):
                task.submission_state = GenerationTask.SubmissionState.RECOVERED
                task.submission_submitted_at = task.submission_submitted_at or timezone.now()
                task.progress = max(task.progress, 10)
                if task.progress_source == GenerationTask.ProgressSource.NONE:
                    task.progress_source = GenerationTask.ProgressSource.SUBMISSION
                task.progress_updated_at = task.progress_updated_at or task.submission_submitted_at
                task.save(update_fields=(
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
                        task, client, prompt_id, timeout=wait_timeout,
                        progress_socket=progress_socket,
                    )
                finally:
                    _close_task_progress_socket(progress_socket)
            elif history is not None and task.submission_state != GenerationTask.SubmissionState.SUBMITTED:
                task.submission_state = GenerationTask.SubmissionState.RECOVERED
                task.submission_submitted_at = task.submission_submitted_at or timezone.now()
                task.save(update_fields=("submission_state", "submission_submitted_at", "updated_at"))
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
            contract_report = preflight_comfyui_workflow(
                client,
                executable_workflow,
                uploaded_paths={
                    item.get("comfy_path")
                    for item in task.staged_inputs
                    if isinstance(item, dict) and item.get("comfy_path")
                },
            )
            task.capability_snapshot = {
                **(task.capability_snapshot or {}),
                "comfyui_contract_preflight": {
                    **contract_report.as_dict(),
                    "validated_at": timezone.now().isoformat(),
                    "status": "passed",
                },
            }
            task.save(update_fields=("capability_snapshot", "updated_at"))
            prompt_id = str(uuid.uuid4())
            task.submission_token = prompt_id
            task.comfy_prompt_id = prompt_id
            task.client_id = task.client_id or f"{settings.COMFYUI_CLIENT_ID}-{task.id}"
            task.submission_state = GenerationTask.SubmissionState.PREPARED
            task.submission_prepared_at = timezone.now()
            task.save(update_fields=(
                "submission_token", "comfy_prompt_id", "client_id", "submission_state",
                "submission_prepared_at", "updated_at",
            ))
            progress_socket = _open_task_progress_socket(task, client)
            try:
                try:
                    queue_result = client.queue_prompt(
                        executable_workflow, task.client_id, prompt_id=prompt_id,
                    )
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
                task.submission_state = GenerationTask.SubmissionState.SUBMITTED
                task.submission_submitted_at = timezone.now()
                task.progress = 10
                task.progress_source = GenerationTask.ProgressSource.SUBMISSION
                task.comfy_progress = None
                task.progress_updated_at = task.submission_submitted_at
                task.save(update_fields=(
                    "submission_state", "submission_submitted_at", "progress", "progress_source",
                    "comfy_progress", "progress_updated_at", "updated_at",
                ))
                _record_runtime_state(task, GenerationTask.RemoteState.SUBMITTED, remote_error="")
                history = _wait_for_task_completion(
                    task, client, prompt_id, timeout=wait_timeout,
                    progress_socket=progress_socket,
                )
            finally:
                _close_task_progress_socket(progress_socket)
        remote_status = history.get("status", {}) if isinstance(history, dict) else {}
        if remote_status.get("status_str") == "error" or remote_status.get("completed") is False:
            raise ComfyUIExecutionError(
                f"ComfyUI 执行失败: {remote_status.get('messages', [])}",
                remote_status=remote_status,
            )
        task.refresh_from_db()
        if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
            cancel_ok, cancel_error = _best_effort_cancel(client, prompt_id)
            _finalize_cancelled(task, remote_confirmed=cancel_ok, remote_error=cancel_error)
            return task
        generated_at = timezone.now()
        try:
            task.outputs = persist_task_provenance(
                task, client.extract_outputs(history), generated_at=generated_at,
            )
        except (OSError, ProvenanceError, ValueError) as exc:
            raise ComfyUIError(
                "生成文件已返回，但 AI 来源清单无法安全封存；任务不会标记成功: "
                f"{exc}"
            ) from exc
        task.status = GenerationTask.Status.SUCCEEDED
        task.progress = 100
        task.progress_source = GenerationTask.ProgressSource.TERMINAL
        task.progress_updated_at = generated_at
        task.finished_at = generated_at
        task.heartbeat_at = task.finished_at
        task.remote_state = GenerationTask.RemoteState.SUCCEEDED
        task.remote_error = ""
        task.save(update_fields=(
            "outputs", "status", "progress", "progress_source",
            "progress_updated_at", "finished_at", "heartbeat_at",
            "remote_state", "remote_error", "updated_at",
        ))
        # Publication is intentionally a second, managed artifact. A valid
        # ComfyUI master remains a successful generation even when disclosure
        # rendering is unavailable; the per-output persisted state then makes
        # public download fail closed and exposes an explicit retry action.
        prepare_all_task_distributions(task)
        task.refresh_from_db(fields=("outputs",))
    except TaskCancellationFinalized:
        task.refresh_from_db()
    except ComfyUIContractUnavailable as exc:
        # No live contract means no compatibility claim. Because preflight
        # happens before token persistence and POST /prompt, an automatic
        # retry is safe and cannot duplicate GPU work.
        task.refresh_from_db()
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = (
            GenerationTask.Status.QUEUED
            if not task.submission_token and task.attempts < task.max_attempts
            else GenerationTask.Status.FAILED
        )
        task.remote_state = GenerationTask.RemoteState.DEGRADED
        task.remote_error = str(exc)
        task.save(update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    except ComfyUIContractError as exc:
        # A proven schema mismatch should not burn all automatic attempts. The
        # task remains explicitly retryable after ComfyUI/models/template are
        # repaired, but it has no ambiguous remote submission to reconcile.
        task.refresh_from_db()
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.DEGRADED
        task.remote_error = str(exc)
        task.save(update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    except SubmissionUncertain as exc:
        task.refresh_from_db()
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.status = GenerationTask.Status.FAILED
        task.remote_state = GenerationTask.RemoteState.MISSING
        task.remote_error = str(exc)
        task.save(update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "stalled_at", "updated_at",
        ))
    except ComfyUIExecutionError as exc:
        task.refresh_from_db()
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
        task.save(update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "submission_state",
            "status", "remote_state", "remote_error", "stalled_at", "updated_at",
        ))
    except ComfyUIError as exc:
        task.refresh_from_db()
        now = timezone.now()
        task.error_message = str(exc)
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        if task.status in {GenerationTask.Status.CANCEL_REQUESTED, GenerationTask.Status.CANCELLED}:
            task.status = GenerationTask.Status.CANCELLED
        elif task.submission_token:
            # A persisted token whose remote outcome is not definitive must
            # never be cleared or automatically posted a second time.
            task.status = GenerationTask.Status.FAILED
        else:
            task.status = (
                GenerationTask.Status.QUEUED
                if task.attempts < task.max_attempts
                else GenerationTask.Status.FAILED
            )
        task.remote_state = GenerationTask.RemoteState.FAILED
        task.remote_error = str(exc)
        task.save(update_fields=(
            "error_message", "finished_at", "heartbeat_at", "progress", "status",
            "remote_state", "remote_error", "updated_at",
        ))
    return task


def recover_stale_tasks(
    older_than_minutes: int = 30,
    client: ComfyUIClient | None = None,
) -> int:
    """Recover abandoned claims using heartbeat freshness and remote evidence.

    A task with a persisted submission token is only requeued when the same
    prompt still exists in ComfyUI queue/history.  It is never converted into a
    fresh POST, preventing duplicate GPU work after a worker crash.
    """
    current_time = timezone.now()
    threshold = current_time - timedelta(minutes=older_than_minutes)
    cancel_threshold = current_time - timedelta(
        seconds=max(0.0, float(getattr(settings, "TASK_CANCEL_GRACE_SECONDS", 30))),
    )
    client = client or ComfyUIClient()
    reconcilable_submission_states = (
        GenerationTask.SubmissionState.PREPARED,
        GenerationTask.SubmissionState.SUBMITTED,
        GenerationTask.SubmissionState.RECOVERED,
    )
    running_candidates = list(
        GenerationTask.objects.filter(status=GenerationTask.Status.RUNNING).filter(
            Q(heartbeat_at__lt=threshold)
            | Q(heartbeat_at__isnull=True, updated_at__lt=threshold)
        ).order_by("started_at")
    )
    # A submitted prompt may outlive a worker-side polling failure.  Reconcile
    # these circuit-breaker records on every recovery pass so the same remote
    # prompt can be reattached without creating a retry or a second POST.
    unresolved_failed_candidates = list(
        GenerationTask.objects.filter(
            status=GenerationTask.Status.FAILED,
            submission_state__in=reconcilable_submission_states,
        ).exclude(submission_token="").order_by("finished_at")
    )
    # Cancellation has its own short reconciliation clock.  It must not wait
    # for the much longer abandoned-worker threshold, otherwise the global
    # unresolved-token circuit breaker can unnecessarily block the queue.
    cancel_candidates = list(
        GenerationTask.objects.filter(
            status=GenerationTask.Status.CANCEL_REQUESTED,
            updated_at__lte=cancel_threshold,
        ).order_by("updated_at")
    )
    candidates = running_candidates + cancel_candidates + unresolved_failed_candidates
    recovered = 0
    for task in candidates:
        now = timezone.now()
        prompt_id = task.submission_token or task.comfy_prompt_id
        if task.status == GenerationTask.Status.CANCEL_REQUESTED:
            cancel_ok, cancel_error = _best_effort_cancel(client, prompt_id) if prompt_id else (True, "")
            remote_confirmed = not bool(prompt_id)
            remote_history = None
            history_checked = False
            queue_checked = False
            still_queued = True
            if prompt_id:
                try:
                    remote_history = client.history(prompt_id)
                    history_checked = True
                except ComfyUIError as exc:
                    cancel_error = cancel_error or str(exc)
                try:
                    still_queued = client.is_queued(prompt_id)
                    queue_checked = True
                except ComfyUIError as exc:
                    cancel_error = cancel_error or str(exc)
                remote_confirmed = (
                    (history_checked and remote_history is not None)
                    or (queue_checked and not still_queued)
                )
            if remote_confirmed:
                _finalize_cancelled(
                    task,
                    remote_confirmed=True,
                    remote_error=cancel_error,
                )
                recovered += 1
            else:
                # Keep both the task and submission token unresolved.  The
                # claim circuit breaker remains active until a later pass can
                # prove terminal history or absence from the remote queue.
                GenerationTask.objects.filter(
                    pk=task.pk,
                    status=GenerationTask.Status.CANCEL_REQUESTED,
                ).update(
                    heartbeat_at=now,
                    remote_state=GenerationTask.RemoteState.CANCELLING,
                    remote_error=(
                        cancel_error
                        or "Cancellation requested; ComfyUI still reports the prompt as running"
                    ),
                )
            continue

        if not prompt_id:
            changed = GenerationTask.objects.filter(
                pk=task.pk, status=GenerationTask.Status.RUNNING,
            ).update(
                status=GenerationTask.Status.QUEUED,
                progress=0,
                attempts=max(0, task.attempts - 1),
                finished_at=None,
                heartbeat_at=now,
                remote_state=GenerationTask.RemoteState.RECOVERED,
                remote_error="Recovered an abandoned pre-submission worker claim",
                error_message="Recovered stale worker task before remote submission",
                updated_at=now,
            )
            recovered += changed
            continue

        try:
            history = client.history(prompt_id)
            remote_present = history is not None or client.is_queued(prompt_id)
        except ComfyUIError as exc:
            changed = GenerationTask.objects.filter(
                pk=task.pk,
            ).filter(
                Q(status=GenerationTask.Status.RUNNING)
                | Q(
                    status=GenerationTask.Status.FAILED,
                    submission_state__in=reconcilable_submission_states,
                )
            ).update(
                status=GenerationTask.Status.FAILED,
                progress=0,
                finished_at=task.finished_at or now,
                heartbeat_at=now,
                stalled_at=task.stalled_at or now,
                remote_state=GenerationTask.RemoteState.DEGRADED,
                remote_error=str(exc),
                error_message=(
                    "Stale worker recovery could not verify the persisted ComfyUI submission; "
                    "automatic resubmission is blocked"
                ),
                updated_at=now,
            )
            recovered += changed
            continue

        if remote_present:
            changed = GenerationTask.objects.filter(
                pk=task.pk,
            ).filter(
                Q(status=GenerationTask.Status.RUNNING)
                | Q(
                    status=GenerationTask.Status.FAILED,
                    submission_state__in=reconcilable_submission_states,
                )
            ).update(
                status=GenerationTask.Status.QUEUED,
                progress=max(task.progress, 10),
                # claim_next_task increments attempts. Rewinding the abandoned
                # local claim keeps a same-token reattachment attempt-neutral.
                attempts=max(0, task.attempts - 1),
                finished_at=None,
                heartbeat_at=now,
                remote_state=GenerationTask.RemoteState.RECOVERED,
                remote_error="",
                error_message="Recovered stale worker and reattached to persisted ComfyUI prompt",
                updated_at=now,
            )
            recovered += changed
        elif task.status == GenerationTask.Status.FAILED:
            # A healthy ComfyUI response that reports the token in neither
            # queue nor history resolves the uncertainty.  Terminalizing the
            # missing token clears the global circuit breaker without ever
            # posting the same task again; a user may then create an explicit
            # retry using the already materialized workflow.
            changed = GenerationTask.objects.filter(
                pk=task.pk,
                status=GenerationTask.Status.FAILED,
                submission_state__in=reconcilable_submission_states,
            ).update(
                progress=0,
                finished_at=task.finished_at or now,
                heartbeat_at=now,
                stalled_at=task.stalled_at or now,
                submission_state=GenerationTask.SubmissionState.TERMINAL_CANCELLED,
                remote_state=GenerationTask.RemoteState.MISSING,
                remote_error="Persisted prompt is absent from ComfyUI queue and history",
                error_message=(
                    "Persisted ComfyUI submission is absent after successful recovery "
                    "reconciliation; the task can be retried safely"
                ),
                updated_at=now,
            )
            recovered += changed
        else:
            changed = GenerationTask.objects.filter(
                pk=task.pk, status=GenerationTask.Status.RUNNING,
            ).update(
                status=GenerationTask.Status.FAILED,
                progress=0,
                finished_at=now,
                heartbeat_at=now,
                stalled_at=task.stalled_at or now,
                remote_state=GenerationTask.RemoteState.MISSING,
                remote_error="Persisted prompt is absent from ComfyUI queue and history",
                error_message=(
                    "Persisted ComfyUI submission disappeared during stale recovery; "
                    "automatic resubmission is blocked"
                ),
                updated_at=now,
            )
            recovered += changed
    return recovered
