"""Queue and cancel the original Smoke owner after shared request preparation."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
from studio.services.execution_dispatch import record_prepared_dispatch_gate
from studio.services.execution_finalization import evaluate_current_prepared_admission
from studio.services.execution_request_storage import request_document
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparation_owners import nonproduction_preparation_root, preparation_owner_ref, locked_preparation_owner_rows
from studio.services.execution_preparations import read_frozen_execution_request, preparation_execution_definition
from studio.services.execution_smoke_admission import collect_smoke_execution_admission
from studio.services.registry_reference_lock import registry_reference_write_guard, registry_reference_writer
from studio.services.security_boundaries import security_boundary_resolution_cache


def _check(preparation, attempt, phase):
    detail, definition, _ = read_frozen_execution_request(preparation)
    current = preparation_execution_definition(preparation, detail=detail, current=True)
    if current.sha256 != definition.sha256:
        raise ValidationError("验收冻结定义与当前精确目标不一致")
    policies, decision, root = evaluate_current_prepared_admission(preparation, definition,
        request_document(detail.prepared_request), collect_smoke_execution_admission)
    record_prepared_dispatch_gate(attempt, preparation, detail, policies, decision, root, phase=phase)
    return decision["can_submit"]


def _initialize_deadlines(attempt):
    from studio.services.execution_deadlines import initial_deadlines
    from studio.services.provider_registry import deployment_execution_config_contract
    from studio.models import ProviderProfile
    config = deployment_execution_config_contract(attempt.deployment).value
    if attempt.deployment.provider.driver_type == ProviderProfile.DriverType.COMFYUI:
        from studio.services.configuration import resolve_configuration
        values = resolve_configuration(required_keys=("workflow.smoke_idle_timeout_seconds", "workflow.smoke_hard_timeout_seconds")).values
        idle, hard = values["workflow.smoke_idle_timeout_seconds"], values["workflow.smoke_hard_timeout_seconds"]
    elif attempt.deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        from studio.services.deployment_smoke_queue import async_smoke_deadline_seconds
        from studio.services.configuration import resolve_configuration
        hard = async_smoke_deadline_seconds(attempt.deployment)
        idle = min(hard, float(resolve_configuration(required_keys=("runtime.task_idle_timeout_seconds",)).values["runtime.task_idle_timeout_seconds"]))
    else:
        hard = idle = config["timeout_seconds"]
    times = initial_deadlines(idle_seconds=idle, hard_seconds=hard, now=timezone.now(), marker="phase:queued")
    attempt.deadline_started_at, attempt.hard_deadline_at = times.started_at, times.hard_deadline_at
    attempt.idle_deadline_at, attempt.last_meaningful_progress_at = times.idle_deadline_at, times.last_meaningful_progress_at
    attempt.progress_marker = times.progress_marker


@security_boundary_resolution_cache()
def dispatch_smoke_execution(*, preparation_id, expected_preparation_revision, actor):
    with registry_reference_write_guard():
        with _locked_preparations([preparation_id], actor) as rows:
            prep = rows[str(preparation_id)]
            attempt = nonproduction_preparation_root(prep)
            resuming = (prep.reason_code == "execution_resource_confirmation_required"
                and attempt.status == "queued" and attempt.submission_started_at is None)
            if (prep.status == "dispatched" and not resuming) or (prep.status == "cancelled" and prep.request_frozen_at is not None):
                return attempt
            if type(expected_preparation_revision) is not int or prep.revision != expected_preparation_revision or (prep.status != "ready" and not resuming):
                raise ValidationError("验收准备尚未就绪或已变化", code="execution_preparation_conflict")
            if _check(prep, attempt, "dispatch"):
                attempt.status = "queued"
                if isinstance(attempt, ModelDeploymentSmokeAttempt):
                    attempt.request_sha256 = prep.request_sha256
                # Human waiting is not execution time. No submitted request
                # enters this path; the same queue and accounting are reused.
                _initialize_deadlines(attempt)
            attempt.save()
            return attempt


@security_boundary_resolution_cache()
def begin_smoke_execution_submission(attempt):
    return _check_claimed_smoke(attempt, submitting=True)


@security_boundary_resolution_cache()
def check_smoke_execution_staging(attempt):
    return _check_claimed_smoke(attempt, submitting=False)


def _check_claimed_smoke(attempt, *, submitting):
    if attempt.claim_token is None:
        raise ValidationError("验收提交必须持有原worker租约")
    owner_key = "deployment_smoke_id" if isinstance(attempt, ModelDeploymentSmokeAttempt) else "workflow_smoke_id"
    with registry_reference_write_guard():
        with locked_preparation_owner_rows([(owner_key, attempt.pk)]) as roots:
            current = roots[(owner_key, str(attempt.pk))]
            if current.claim_token != attempt.claim_token or current.status != "queued":
                return False, current
            prep = ExecutionPreparation.objects.select_for_update().get(**{owner_key: current.pk})
            allowed = _check(prep, current, "submission" if submitting else "staging")
            if allowed and submitting:
                current.status, current.submission_started_at = "submitting", timezone.now()
            elif not allowed:
                current.claim_token, current.worker_id, current.claimed_at = None, "", None
            current.save()
            return allowed, current


@registry_reference_writer
def request_prepared_smoke_cancel(attempt, actor):
    """Use the same original root/preparation before and after submission."""
    from studio.services.execution_preparation_owners import locked_preparation_owners
    from studio.services.execution_smoke_evidence import read_prepared_smoke_context
    from studio.services.provider_registry import driver_for
    if isinstance(attempt, ModelDeploymentSmokeAttempt):
        owner = ("deployment_smoke_id", attempt.pk)
    elif isinstance(attempt, WorkflowSmokeAttempt):
        owner = ("workflow_smoke_id", attempt.pk)
    else:
        raise ValidationError("取消烟测需要正式烟测任务")
    with locked_preparation_owners([owner], actor) as roots:
        root = roots[(owner[0], str(owner[1]))]
        try:
            prep = ExecutionPreparation.objects.select_for_update().get(**{owner[0]: root.pk})
        except ExecutionPreparation.DoesNotExist as exc:
            raise ValidationError("烟测缺少原始执行准备，不能取消") from exc
        if root.submission_started_at is None:
            return cancel_prepared_smoke(prep, actor)
        read_prepared_smoke_context(root, prep)
        if root.status not in {"submitting", "submitted", "running", "cancel_requested"}:
            raise ValidationError("烟测已是终态或提交结果待消歧")
        if isinstance(root, ModelDeploymentSmokeAttempt) and "chat_completion" in driver_for(root.deployment.provider).capabilities().get("operations", []):
            raise ValidationError("文本烟测已进入单次提交边界，不能安全撤回；请等待终态或人工消歧")
        root.status = "cancel_requested"
        root.cancel_requested_at = root.cancel_requested_at or timezone.now()
        root.next_poll_at = timezone.now()
        root.save(update_fields=("status", "cancel_requested_at", "next_poll_at", "updated_at"))
        return root


@registry_reference_writer
def cancel_prepared_smoke(preparation, actor):
    """Cancel unsubmitted parent and original children, preserving uncertain cost."""
    from studio.services.asset_generation import request_asset_generation_cancel
    parent_ref = preparation_owner_ref(preparation)
    children = list(preparation.auxiliary_asset_jobs.all())
    from studio.services.execution_preparation_owners import locked_preparation_owners
    with locked_preparation_owners([parent_ref, *[("asset_job_id", child.pk) for child in children]], actor) as roots:
        root = roots[(parent_ref[0], str(parent_ref[1]))]
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation.pk)
        from studio.services.execution_task_links import validate_smoke_preparation_link
        validate_smoke_preparation_link(root, prep.execution_instance_version_id)
        if root.status == "cancelled":
            return root
        if root.status not in {"preparing", "queued", "cancel_requested"}:
            raise ValidationError("验收已进入提交边界或终态，不能作为未提交请求取消")
        if root.submission_started_at is not None:
            raise ValidationError("主验收已进入单次提交边界，需由原远端取消或消歧处理")
        for child in children:
            child.refresh_from_db()
            if child.status in {"preparing", "queued", "submitting", "submitted", "running", "cancel_requested"}:
                request_asset_generation_cancel(child)
        pending = prep.auxiliary_asset_jobs.exclude(status__in=("succeeded", "failed", "cancelled")).exists()
        root.status = "cancel_requested" if pending else "cancelled"
        root.cancel_requested_at = root.cancel_requested_at or timezone.now()
        root.finished_at = None if pending else timezone.now()
        root.next_poll_at, root.claim_token, root.worker_id, root.claimed_at = None, None, "", None
        root.save()
        prep.status, prep.reason_code, prep.reason = "cancelled", "cancelled_before_submission", "验收在主调用提交前取消"
        prep.revision += 1
        prep.next_check_at = timezone.now() if pending else None
        prep.save()
        if parent_ref[0] == "workflow_smoke_id" and root.scope == "resource":
            from studio.models import WorkflowTemplateVersion
            from studio.services.execution_workflow_resource_evidence import same_workflow_validation
            workflow = WorkflowTemplateVersion.objects.get(pk=root.workflow_version_id)
            if same_workflow_validation(root, workflow):
                workflow.smoke_test_state = "not_run"
                workflow.smoke_test_message = "基础烟测在主调用提交前取消，未产生验收通过证据。"
                workflow.smoke_test_evidence, workflow.smoke_test_evidence_sha256 = {}, ""
                workflow.save(update_fields=("smoke_test_state", "smoke_test_message", "smoke_test_evidence",
                    "smoke_test_evidence_sha256", "updated_at"))
        return root
