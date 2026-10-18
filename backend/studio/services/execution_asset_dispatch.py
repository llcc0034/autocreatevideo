"""Original AssetGenerationJob dispatch and pre-submit gate; no new queue.

The frozen payload is never rebuilt. Original workers own remote concurrency,
claim, submission, reconciliation and cancellation. This adapter rechecks the
same current admission and reserves only this call's cost under the root lock.
"""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import AssetGenerationJob, ExecutionPreparation
from studio.services.execution_asset_admission import collect_asset_execution_admission
from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_finalization import evaluate_current_prepared_admission
from studio.services.execution_dispatch import record_prepared_dispatch_gate
from studio.services.execution_request_storage import request_document
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.security_boundaries import security_boundary_resolution_cache


def _current_gate(job, preparation):
    detail, definition, _ = read_frozen_execution_request(preparation)
    current = verify_current_execution_definition(stored_execution_contract(preparation.execution_instance_version_id))
    if definition.sha256 != current.sha256 or job.execution_instance_version_id != preparation.execution_instance_version_id:
        raise ValidationError("待提交任务与冻结执行定义不一致")
    request = request_document(detail.prepared_request)
    policies, decision, root = evaluate_current_prepared_admission(preparation, definition, request, collect_asset_execution_admission)
    return detail, policies, decision, root




@security_boundary_resolution_cache()
def dispatch_asset_execution(*, preparation_id, expected_preparation_revision, actor):
    """Only materialize the original queue row; do not claim/invoke a provider."""
    if type(expected_preparation_revision) is not int or expected_preparation_revision < 1:
        raise ValidationError("必须提交明确的准备修订号")
    with registry_reference_write_guard():
        with _locked_preparations([preparation_id], actor) as rows:
            preparation = rows[str(preparation_id)]
            if not preparation.asset_job_id:
                raise ValidationError("素材派发必须使用真实素材根")
            job = AssetGenerationJob.objects.get(pk=preparation.asset_job_id)
            resuming = (preparation.reason_code == "execution_resource_confirmation_required"
                and job.status == "queued" and job.submission_started_at is None)
            if (preparation.status == "dispatched" and not resuming) or (preparation.status == "cancelled" and preparation.request_frozen_at is not None):
                # Stable replay cannot grant another reservation or restart a
                # terminal/uncertain task. Its original worker owns recovery.
                return job
            if preparation.revision != expected_preparation_revision or (preparation.status != "ready" and not resuming) or preparation.request_frozen_at is None:
                raise ValidationError("准备尚未就绪或修订已变化", code="execution_preparation_conflict")
            detail, policies, decision, root = _current_gate(job, preparation)
            record_prepared_dispatch_gate(job, preparation, detail, policies, decision, root, phase="dispatch")
            if decision["can_submit"]:
                job.status = "queued"
            job.save(update_fields=("status", "estimated_cost", "cost_currency", "error_code", "error_message", "next_poll_at", "updated_at"))
            return job


@security_boundary_resolution_cache()
def begin_asset_execution_submission(job):
    """Original claimed worker only; return (may_invoke, current_job).

    Reserve and persist SUBMITTING atomically before I/O. Repeated/stale worker
    calls cannot acquire another permit. No root/user-supplied skip flag.
    """
    return _check_claimed_asset_execution(job, phase="submission")


@security_boundary_resolution_cache()
def check_asset_execution_staging(job):
    """Check before media upload, without starting the main submit clock.

    Upload receipts are idempotent and cancellation-aware. After upload the
    original worker must still call begin_asset_execution_submission again.
    """
    return _check_claimed_asset_execution(job, phase="staging")


def _check_claimed_asset_execution(job, *, phase):
    if phase not in {"submission", "staging"}:
        raise ValidationError("未知的原任务执行阶段")
    if job.claim_token is None:
        raise ValidationError("提交需要原worker领取的有效任务")
    with registry_reference_write_guard():
        with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
            current = owners[("asset_job_id", str(job.pk))]
            if current.claim_token == job.claim_token and current.status == "cancel_requested" and current.submission_started_at is None:
                from studio.services.asset_generation import _finish_cancelled
                return False, _finish_cancelled(current)
            if current.claim_token != job.claim_token or current.status != "queued":
                return False, current
            preparation = ExecutionPreparation.objects.select_for_update().get(asset_job=current)
            if preparation.status not in {"ready", "dispatched"} or preparation.request_frozen_at is None:
                raise ValidationError("任务未完成请求冻结，不能开始远端提交")
            detail, policies, decision, root = _current_gate(current, preparation)
            record_prepared_dispatch_gate(current, preparation, detail, policies, decision, root, phase=phase)
            if decision["can_submit"] and phase == "submission":
                current.status = "submitting"
                current.submission_started_at = timezone.now()
                current.started_at = current.started_at or current.submission_started_at
                current.heartbeat_at = current.submission_started_at
            elif not decision["can_submit"]:
                # Keep the original queue owner and payload. Honor the same
                # configured recheck interval rather than spinning or failing
                # the task and making the user regenerate reviewed fields.
                current.claim_token, current.worker_id, current.claimed_at = None, "", None
            current.save(update_fields=("status", "estimated_cost", "cost_currency", "error_code", "error_message", "next_poll_at",
                "claim_token", "worker_id", "claimed_at", "submission_started_at", "started_at", "heartbeat_at", "updated_at"))
            return decision["can_submit"], current
