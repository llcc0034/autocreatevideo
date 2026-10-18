"""Preparation-backed inputs for the original GenerationTask worker.

Tasks are allocated by the original production dispatch transaction. Completing
this adapter alone does not open Comfy production admission.
No PromptArtifact, route or GeneratedWorkflow metadata supplies runtime inputs.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import json

from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned, ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask, HostCapabilitySnapshot, ProductionRun
from studio.services.configuration import resolve_configuration_from_snapshot
from studio.services.execution_definitions import execution_definition_version, stored_execution_contract, verify_current_execution_definition
from studio.services.execution_deadlines import expired_kind
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_production_media import read_prepared_production_media
from studio.services.execution_workflow_staging import stage_execution_workflow_media
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.runtime_platform import host_resolution_and_concurrency, verify_host_snapshot
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


@dataclass(frozen=True)
class PreparedGenerationTask:
    attempt: GenerationAttempt
    media: dict


class PreparedGenerationWaiting(Exception):
    """The original unsubmitted task waits for its Run, not a failed call."""


def _require_root_running(stage):
    if stage.status != "running" or stage.production_run.status != "running":
        temporary = {"running", "blocked", "awaiting_input", "awaiting_approval"}
        if stage.status in temporary and stage.production_run.status in temporary:
            raise PreparedGenerationWaiting("运行或阶段正在等待处理；本任务尚未提交，将在继续运行后恢复。")
        raise ValidationError("运行或阶段已结束，不能开始新的调用")


def defer_prepared_generation_task(task, reason):
    from studio.services.task_queue import _claimed_task_rows, TaskClaimLost
    attempt = _linked_task_attempt(task)
    with _locked_task_attempt(task, attempt) as (_stage, _preparation, current_attempt, current):
        if (not _claimed_task_rows(task, require_running=True).exists()
                or current.submission_token or current.comfy_prompt_id or current.submission_state != "unprepared"
                or current_attempt.submission_started_at is not None or current_attempt.ignore_late_result
                or current_attempt.cancel_requested_at is not None):
            raise TaskClaimLost("等待前领取或提交状态已变化")
        # No generation POST has started. Waiting time is not execution time;
        # staged upload receipts stay intact and are reused on the next claim.
        updates = {"status": "queued", "error_message": str(reason), "finished_at": None,
            "heartbeat_at": timezone.now(), "deadline_started_at": None, "hard_deadline_at": None,
            "idle_deadline_at": None, "last_meaningful_progress_at": None, "progress_marker": ""}
        if not update_prepared_generation_lifecycle(task, updates, condition=_claimed_task_rows(task, require_running=True)):
            raise TaskClaimLost("等待前领取已失效")
        return task


def allocate_prepared_generation_task(attempt):
    """One queue task per real primary Attempt, in the original dispatch lock."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Task allocation requires the original production dispatch transaction")
    if attempt.pk is None or attempt._state.adding or attempt.generation_task_id or attempt.status != "queued":
        raise ValidationError("仅为本次新建主调用分配任务，不补建、克隆或重置已有任务")
    media = read_prepared_production_media(attempt)
    if media["driver_type"] != "comfyui":
        raise ValidationError("非ComfyUI调用不能进入工作流任务队列")
    run = attempt.stage_run.production_run
    task = GenerationTask(owner_id=run.owner_id, project_id=run.project_id,
        episode_id=media["production_unit"]["episode_id"], kind=media["output_kind"])
    task.full_clean()
    task.save()
    attempt.generation_task = task
    attempt.full_clean()
    attempt.save(update_fields=("generation_task", "updated_at"))
    return task


def _linked_task_attempt(task):
    """Identity only: a corrupt request must still be recordable as a failure.

    This does not authorize submission or read a replacement request. Execution
    and artifact consumers must additionally verify the frozen media contract.
    """
    try:
        attempt = GenerationAttempt.objects.select_related(
            "stage_run__production_run__owner", "stage_run__production_run__project",
            "preparation",
        ).get(generation_task_id=task.pk)
    except (ObjectDoesNotExist, MultipleObjectsReturned) as exc:
        raise ValidationError("ComfyUI任务必须关联唯一的实际主调用与执行准备") from exc
    run = attempt.stage_run.production_run
    if (attempt.preparation_id is None or attempt.role != "primary" or attempt.parent_preparation_id is not None
            or attempt.preparation.stage_run_id != attempt.stage_run_id
            or attempt.execution_instance_version_id is None
            or attempt.execution_instance_version_id != attempt.preparation.execution_instance_version_id
            or attempt.unit_key != attempt.preparation.unit_key
            or task.owner_id != run.owner_id or task.project_id != run.project_id
            or run.project.owner_id != run.owner_id):
        raise ValidationError("ComfyUI任务的实际主调用、准备或所有者不一致")
    return attempt


@security_boundary_resolution_cache()
def read_prepared_generation_task(task):
    """Require one actual primary Attempt; never infer it from old JSON IDs."""
    attempt = _linked_task_attempt(task)
    media = read_prepared_production_media(attempt)
    run = attempt.stage_run.production_run
    if (media["driver_type"] != "comfyui" or task.owner_id != run.owner_id
            or task.project_id != run.project_id or run.project.owner_id != run.owner_id
            or task.kind != media["output_kind"]
            or (str(task.episode_id) if task.episode_id else None) != media["production_unit"]["episode_id"]):
        raise ValidationError("ComfyUI任务的执行协议、所有者、项目或产物与主准备不一致")
    return PreparedGenerationTask(attempt, media)


@security_boundary_resolution_cache()
def prepared_generation_concurrency(task):
    """Original Run configuration and current exact resource; never old route JSON.

    This limits allocation of NEW work. Reconciliation of a persisted remote
    request must not depend on today's quota or host-probe freshness.
    """
    context = read_prepared_generation_task(task)
    current = verify_current_execution_definition(stored_execution_contract(context.attempt.execution_instance_version_id))
    if current.sha256 != context.media["definition_sha256"]:
        raise ValidationError("媒体执行目标已变化，不能领取新的ComfyUI任务")
    run = context.attempt.stage_run.production_run
    base = verify_json_contract(run.config_snapshot, schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes, sha256=run.config_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if base.sha256 != context.media["execution_deadlines"]["configuration_sha256"]:
        raise ValidationError("任务时限与并发配置不属于本次冻结Run，不能读取新配置替代")
    version = execution_definition_version(context.attempt.execution_instance_version_id)
    host = version.deployment.runtime_host
    required = ("production.default_concurrency",) + (("recommendation.vram_tiers",) if host else ())
    resolved = resolve_configuration_from_snapshot(base.value, required_keys=required)
    limit = resolved.values["production.default_concurrency"]
    if type(limit) is not int or limit < 1:
        raise ValidationError("本次Run的并发配置必须为正整数")
    if host is not None:
        if host.status != "active":
            raise ValidationError("绑定运行主机不可用，不能领取新的ComfyUI任务")
        snapshot = HostCapabilitySnapshot.objects.filter(host=host).order_by("-captured_at", "-revision").first()
        if (snapshot is None or snapshot.probe_status not in {"succeeded", "partial"}
                or snapshot.expires_at is None or snapshot.expires_at <= timezone.now()):
            raise ValidationError("绑定主机的最新探测失败、缺失或过期，不能回退旧容量事实")
        verify_host_snapshot(snapshot)
        _, host_limit = host_resolution_and_concurrency(snapshot, resolved.values["recommendation.vram_tiers"])
        limit = min(limit, host_limit)
    return limit


@contextmanager
def locked_prepared_generation_task(task, context):
    """One lock order for staging and collection; no Task -> Attempt inversion."""
    with _locked_task_attempt(task, context.attempt) as rows:
        yield rows


@contextmanager
def _locked_task_attempt(task, attempt):
    with locked_preparation_owner_rows([("stage_run_id", attempt.stage_run_id)]) as owners:
        stage = owners[("stage_run_id", str(attempt.stage_run_id))]
        preparation = ExecutionPreparation.objects.select_for_update().get(pk=attempt.preparation_id, stage_run=stage)
        current_attempt = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk, preparation=preparation)
        current = GenerationTask.objects.select_for_update().get(pk=task.pk)
        if (current_attempt.generation_task_id != current.pk or current_attempt.role != "primary"
                or current_attempt.stage_run_id != stage.pk or preparation.stage_run_id != stage.pk
                or current.owner_id != task.owner_id or current.project_id != task.project_id):
            raise ValidationError("任务与实际主调用的关联已变化")
        yield stage, preparation, current_attempt, current


def update_prepared_generation_lifecycle(task, updates, *, condition, rejected=False, remote_present=False,
                                        failure_code=None):
    """Atomically record Task/Attempt/ledger facts behind the caller's own CAS.

    Worker claims and recovery observations have different CAS predicates but
    share this original root lock and ledger. No remote I/O occurs here.
    """
    attempt = _linked_task_attempt(task)
    with _locked_task_attempt(task, attempt) as (stage, preparation, attempt, current):
        if GenerationAttempt.objects.filter(retry_of=attempt).exists():
            return 0  # Explicit successor owns execution; keep the unresolved original unchanged.
        if not condition.exists():
            return 0
        status = updates.get("status", current.status)
        submission = updates.get("submission_state", current.submission_state)
        if status not in {"queued", "failed", "cancel_requested", "cancelled"}:
            raise ValidationError("此入口只记录任务恢复、失败或取消；成功必须经过真实产物归档")
        if attempt.status == "succeeded":
            raise ValidationError("已归档的主调用不能被失败或恢复观察覆盖")
        token = current.submission_token
        if token and (token != str(attempt.pk) or current.comfy_prompt_id != token
                or attempt.submission_started_at is None or attempt.provider_request_id not in {"", token}):
            raise ValidationError("任务提交标识与实际准备的提交事实不一致")
        if not token and (attempt.submission_started_at is not None or attempt.provider_request_id):
            raise ValidationError("已开始提交的主调用缺少原任务标识，不能按未提交释放费用")
        if rejected and (not token or attempt.provider_request_id or attempt.submitted_at is not None):
            raise ValidationError("已经确认接受的请求不能再按未提交拒绝处理")
        terminal_remote = submission in {"terminal_succeeded", "terminal_failed", "terminal_cancelled"}
        if status == "cancelled" and token and not terminal_remote:
            raise ValidationError("远端取消尚未确认，不能标记已取消")
        now = updates.get("heartbeat_at") or timezone.now()
        if remote_present:
            if not token or submission in {"terminal_failed", "terminal_cancelled"} or status != "queued":
                raise ValidationError("恢复必须指向尚未结束的原始远端提交")
            attempt.provider_request_id, attempt.submitted_at = token, attempt.submitted_at or now
            updates = {**updates, "submission_state": "terminal_succeeded" if submission == "terminal_succeeded" else "recovered",
                "submission_submitted_at": attempt.submitted_at}
        if status == "queued":
            if token and not remote_present:
                raise ValidationError("未确认原远端请求仍存在，不能重新入队")
            attempt.status = "submitted" if token else "queued"
            attempt.error_code = attempt.error_message = ""
            attempt.finished_at = None
        else:
            uncertain = bool(token) and not terminal_remote
            attempt.status = status
            attempt.error_code = ("uncertain_remote_response" if uncertain else
                "execution_cancelled" if status == "cancelled" else
                "execution_response_storage_failed" if failure_code == "execution_response_storage_failed" else
                "execution_output_invalid" if submission == "terminal_succeeded" else
                "execution_submission_rejected" if rejected else "execution_workflow_failed")
            # Raw remote errors may contain prompts or service paths. They stay
            # on the existing diagnostic task, not the public Attempt summary.
            attempt.error_message = ("远端结果尚未确认；保留原提交标识和预算，不会自动重发。" if uncertain else
                "本次调用未形成可采用候选，请查看原任务详情。")
            attempt.finished_at = None if status == "cancel_requested" else updates.get("finished_at", now)
            if status in {"cancel_requested", "cancelled"}:
                attempt.ignore_late_result = True
                attempt.cancel_requested_at = attempt.cancel_requested_at or now
            local_terminal_failure = (not attempt.is_external and status == "failed" and submission == "terminal_failed")
            if (not token or rejected or local_terminal_failure) and attempt.committed_cost is not None:
                run = ProductionRun.objects.get(pk=stage.production_run_id)
                if attempt.committed_cost < 0 or attempt.committed_cost > run.committed_cost_total:
                    raise ValidationError("任务释放预留与运行账目不一致")
                run.committed_cost_total -= attempt.committed_cost
                attempt.committed_cost = Decimal("0")
                run.save(update_fields=("committed_cost_total", "updated_at"))
        # No actual_cost is invented for ComfyUI. A definitive local failure
        # releases its reservation; external work still needs reconciliation.
        attempt.heartbeat_at, attempt.next_poll_at = now, None
        attempt.save(update_fields=("status", "error_code", "error_message", "finished_at", "heartbeat_at",
            "next_poll_at", "provider_request_id", "submitted_at", "ignore_late_result", "cancel_requested_at",
            "committed_cost", "updated_at"))
        values = {**updates, "updated_at": timezone.now()}
        if condition.update(**values) != 1:
            raise ValidationError("任务观察在同一事务内失效，不能提交部分状态")
        for key, value in values.items():
            setattr(task, key, value)
        if remote_present:
            from studio.services.production_execution_recovery import reconcile_recovered_media_stage
            reconcile_recovered_media_stage(stage)
        return 1


def claim_prepared_generation_task(task, updates):
    """Attach the original worker claim under the same root lock as settlement."""
    attempt = _linked_task_attempt(task)
    with _locked_task_attempt(task, attempt) as (stage, preparation, attempt, current):
        if (current.status != "queued" or current.attempts != task.attempts or current.started_at != task.started_at
                or attempt.ignore_late_result or attempt.status not in {"queued", "running", "submitting", "submitted"}):
            return None
        # Resuming remote observation is not new admission. A stopped root must
        # still reconcile its original submission, but cannot start a new one.
        if not current.submission_token and (stage.status != "running" or stage.production_run.status != "running"
                or preparation.status != "dispatched"):
            return None
        if current.submission_token:
            if current.submission_token != str(attempt.pk) or attempt.submission_started_at is None:
                raise ValidationError("恢复领取缺少本次准备的原始提交事实")
        else:
            if attempt.submission_started_at is not None:
                raise ValidationError("已开始提交的主调用不能被当成新任务领取")
            attempt.status = "running"
        attempt.started_at = attempt.started_at or updates["started_at"]
        attempt.heartbeat_at = updates["heartbeat_at"]
        attempt.save(update_fields=("status", "started_at", "heartbeat_at", "updated_at"))
        for key, value in updates.items():
            setattr(current, key, value)
        current.save(update_fields=tuple(updates))
        return current


@contextmanager
def _active_task(task, context):
    """Original claim identity, under Run -> Stage -> Preparation -> Attempt -> Task."""
    attempt = context.attempt
    with locked_prepared_generation_task(task, context) as (stage, preparation, current_attempt, current):
        if (type(task.attempts) is not int or task.attempts < 1 or task.started_at is None
                or current.attempts != task.attempts or current.started_at != task.started_at
                or current.status != "running" or current.submission_state != "unprepared" or current.submission_token
                or current.deadline_started_at is None or current.hard_deadline_at is None or current.idle_deadline_at is None
                or current.owner_id != task.owner_id or current.project_id != task.project_id or current.kind != task.kind
                or current_attempt.generation_task_id != current.pk or current_attempt.status not in {"queued", "running"}
                or any(getattr(current_attempt, key) != getattr(attempt, key) for key in (
                    "execution_instance_version_id", "model_deployment_id", "unit_key", "role", "operation"))
                or preparation.execution_instance_version_id != attempt.execution_instance_version_id
                or current_attempt.submission_started_at is not None
                or current_attempt.request_sha256 != preparation.request_sha256
                or current_attempt.request_snapshot_sha256 != preparation.request_sha256
                or preparation.request_sha256 != context.attempt.request_snapshot_sha256
                or preparation.status != "dispatched"):
            raise ValidationError("ComfyUI任务领取已失效、已停止、已提交或已超时，不能继续上传")
        _require_root_running(stage)
        if expired_kind(current):
            raise ValidationError("ComfyUI任务已超时，不能继续上传")
        # Liveness is not meaningful node progress and cannot renew idle/hard.
        current.heartbeat_at = timezone.now()
        current.save(update_fields=("heartbeat_at", "updated_at"))
        yield current


@security_boundary_resolution_cache()
def begin_prepared_generation_submission(task):
    """Final admission and reservation use the Task claim, not a forged Attempt lease."""
    from studio.services.execution_dispatch import record_prepared_dispatch_gate
    from studio.services.execution_production_dispatch import current_production_gate, _reserve
    from studio.services.registry_reference_lock import registry_reference_write_guard
    from studio.models import ProductionRun
    context = read_prepared_generation_task(task)
    with registry_reference_write_guard(), _active_task(task, context) as current:
        attempt = GenerationAttempt.objects.get(pk=context.attempt.pk)
        preparation = ExecutionPreparation.objects.get(pk=attempt.preparation_id)
        detail, policies, decision, root = current_production_gate(preparation)
        record_prepared_dispatch_gate(attempt, preparation, detail, policies, decision, root, phase="submission")
        now = timezone.now()
        if decision["can_submit"]:
            _reserve(ProductionRun.objects.get(pk=attempt.stage_run.production_run_id), attempt)
            attempt.status, attempt.submission_started_at, attempt.heartbeat_at = "submitting", now, now
            # An explicit retry creates another Attempt for the same frozen
            # Preparation. Its delivery identity must be new; recovery of
            # this Attempt keeps the same UUID and never creates another POST.
            current.submission_token = current.comfy_prompt_id = str(attempt.pk)
            current.client_id = f"autoprompt-studio-{current.pk}"
            current.submission_state, current.submission_prepared_at = "prepared", now
        else:
            # Keep the original request and deadlines. A later admission check
            # is not another paid attempt and must not exhaust retry count.
            current.status = "queued"
            current.error_message = preparation.reason
        attempt.save()
        current.save(update_fields=("status", "error_message", "submission_token", "comfy_prompt_id",
            "client_id", "submission_state", "submission_prepared_at", "updated_at"))
        for key in ("status", "error_message", "submission_token", "comfy_prompt_id", "client_id", "submission_state", "submission_prepared_at"):
            setattr(task, key, getattr(current, key))
        return decision["can_submit"]


@security_boundary_resolution_cache()
def record_prepared_generation_submission(task, *, recovered=False):
    """An accepted or found remote ID is a fact on both original task records."""
    from studio.services.task_queue import _claimed_task_rows, TaskClaimLost
    context = read_prepared_generation_task(task)
    with locked_prepared_generation_task(task, context) as (_stage, preparation, attempt, current):
        if not _claimed_task_rows(task).exists():
            raise TaskClaimLost("远端返回前任务领取已失效")
        token = str(attempt.pk)
        if (current.submission_token != token or current.comfy_prompt_id != token
                or attempt.generation_task_id != current.pk or attempt.submission_started_at is None
                or attempt.status not in {"submitting", "submitted", "cancel_requested"}
                or attempt.provider_request_id not in {"", token}):
            raise ValidationError("远端回执没有对应本次准备的原始提交事实")
        now = timezone.now()
        attempt.provider_request_id = token
        attempt.submitted_at = attempt.submitted_at or now
        attempt.heartbeat_at = now
        if attempt.status != "cancel_requested":
            attempt.status = "submitted"
        attempt.save(update_fields=("provider_request_id", "submitted_at", "heartbeat_at", "status", "updated_at"))
        if current.submission_state != "terminal_succeeded":
            current.submission_state = "recovered" if recovered else "submitted"
        current.submission_submitted_at = attempt.submitted_at
        current.save(update_fields=("submission_state", "submission_submitted_at", "updated_at"))
        task.submission_state, task.submission_submitted_at = current.submission_state, current.submission_submitted_at


def read_prepared_generation_history(task):
    """Original retained terminal observation, not a remote lookup or candidate."""
    from studio.models import ExecutionTextResponseReview
    from studio.services.execution_text_response_review import read_retained_text_response, read_collect_receipt
    attempt = _linked_task_attempt(task)
    row = ExecutionTextResponseReview.objects.filter(attempt=attempt).first()
    if row is None:
        return None
    original, _, _, _ = read_retained_text_response(row)
    return read_collect_receipt(original, request_id=task.submission_token)["history"]


def retain_prepared_generation_history(task, history):
    """Commit the actual terminal history before probe/archive can fail.

    Uses the original root locks and Task claim. No response permits a new
    submission, creates a candidate, or changes an existing response object.
    """
    from django.core.files.storage import default_storage
    from studio.models import ExecutionTextResponseReview
    from studio.services.execution_response_storage import store_response, ExecutionResponseStorageError
    from studio.services.execution_text_response_review import COLLECT_RECEIPT_KIND, read_retained_text_response
    from studio.services.json_contracts import assign_json_contract
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.task_queue import _claimed_task_rows, TaskClaimLost
    context = read_prepared_generation_task(task)
    status = history.get("status") if isinstance(history, dict) else None
    if not isinstance(status, dict) or not (status.get("status_str") == "error" or (
            status.get("status_str") == "success" and status.get("completed") is True)):
        raise ValidationError("只有实际终态 history 可以保存为本次媒体回执")
    with locked_prepared_generation_task(task, context) as (stage, preparation, attempt, current):
        if not _claimed_task_rows(task).exists():
            raise TaskClaimLost("回执返回前任务领取失效")
        if (current.status == "cancel_requested" or attempt.ignore_late_result or attempt.cancel_requested_at is not None
                or stage.status == "cancelled" or stage.production_run.status == "cancelled"):
            return None  # Same claim cancellation still needs the original terminal settlement.
        # Receiving an already submitted result is not new admission. Other
        # units can put the root in awaiting_input/blocked without losing this
        # Task's claim; only its cancellation/claim facts suppress the result.
        token = str(attempt.pk)
        if (attempt.status != "submitted" or attempt.submission_started_at is None or attempt.submitted_at is None
                or current.submission_token != token or current.comfy_prompt_id != token
                or attempt.provider_request_id != token
                or attempt.request_sha256 != preparation.request_sha256
                or attempt.request_snapshot_sha256 != preparation.request_sha256):
            raise ValidationError("媒体回执缺少原调用的确切请求与提交事实")
        # Verify the original request, never today's definition or current peer.
        from studio.services.execution_preparations import read_production_attempt_request
        read_production_attempt_request(attempt)
        try:
            text = json.dumps({"schema_version": 1, "request_id": token, "history": history},
                ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValidationError("实际 history 不是可保存的 JSON 回执") from exc
        existing = ExecutionTextResponseReview.objects.filter(attempt=attempt).first()
        if existing is not None:
            original, _, _, _ = read_retained_text_response(existing)
            if original != text:
                raise ValidationError("本次调用已保留不同的原回执，不能覆盖")
            return existing
        row = ExecutionTextResponseReview(attempt=attempt)
        assign_json_contract(row, "diagnostics", {"schema_version": 1, "body_kind": COLLECT_RECEIPT_KIND,
            "response_source": "comfyui_history", "provider_request_id": token,
            "preparation_id": str(preparation.pk), "request_sha256": preparation.request_sha256,
            "output_node_id": str(context.media["output_contract"]["primary"]["node_id"])},
            max_bytes=execution_configuration_max_bytes())
        key, row.original_size, row.original_sha256 = store_response(text)
        row.original_file = key
        try:
            row.save()
        except Exception as exc:
            default_storage.delete(key)  # Only this failed write's newly created object.
            raise ExecutionResponseStorageError("实际媒体回执登记失败；不会伪装已保留或自动重跑") from exc
        return row


@security_boundary_resolution_cache()
def stage_prepared_generation_task(task, client):
    """Per-use uploads and durable receipts, not graph recompilation or /prompt."""
    context = read_prepared_generation_task(task)
    with _active_task(task, context):
        pass
    # Reuse the normal production gate before any upload, including calls
    # made directly to the task queue service.
    from studio.services.execution_production_dispatch import current_production_gate
    _, _, decision, _ = current_production_gate(context.attempt.preparation)
    if not decision["can_submit"]:
        # Re-read the original root in case another unit paused it after claim.
        stage = context.attempt.stage_run
        stage.refresh_from_db()
        stage.production_run.refresh_from_db()
        _require_root_running(stage)
        raise ValidationError("本次执行准备尚未通过当前生产准入，不能上传素材")
    with _active_task(task, context) as current:
        receipts = current.staged_inputs
    detail, definition, transport = read_frozen_execution_request(context.attempt.preparation)
    inputs = json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes())
    stage = context.attempt.stage_run
    from studio.services.production_execution_sources import execution_source_stage_ids
    eligible = execution_source_stage_ids(stage)

    def check_active():
        with _active_task(task, context):
            pass

    def persist(rows):
        with _active_task(task, context) as current:
            current.staged_inputs = rows
            current.save(update_fields=("staged_inputs", "updated_at"))
            task.staged_inputs = rows

    staged = stage_execution_workflow_media(definition=definition, resolved_inputs=inputs,
        transport=transport, owner=stage.production_run.owner, root_id=task.pk, client=client,
        staged_inputs=receipts, persist_staged_inputs=persist, check_active=check_active,
        max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
        production_run_id=str(stage.production_run_id), eligible_stage_ids=eligible)
    return staged.graph
