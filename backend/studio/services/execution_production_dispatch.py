"""Prepared production calls use original GenerationAttempt, never an AssetJob."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRun
from studio.services.execution_definitions import execution_definition_version, stored_execution_contract, verify_current_execution_definition
from studio.services.execution_auxiliary_requests import read_production_field_context
from studio.services.execution_dispatch import record_prepared_dispatch_gate
from studio.services.execution_finalization import evaluate_current_prepared_admission
from studio.services.execution_request_storage import request_document, reference_prepared_request
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_production_admission import collect_production_execution_admission
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.security_boundaries import security_boundary_resolution_cache


def current_production_gate(preparation, *, retry_of=None, acknowledged_uncertain_attempt_id=None):
    from studio.services.execution_production_attempts import require_unfulfilled_material_unit
    require_unfulfilled_material_unit(stage_id=preparation.stage_run_id, unit_key=preparation.unit_key)
    detail, definition, _ = read_frozen_execution_request(preparation)
    current = verify_current_execution_definition(stored_execution_contract(preparation.execution_instance_version_id))
    if definition.sha256 != current.sha256:
        raise ValidationError("待提交请求的执行定义已变化")
    request = request_document(detail.prepared_request)
    def collect(preparation, definition, request):
        extra = ({"acknowledged_uncertain_attempt_id": acknowledged_uncertain_attempt_id}
            if acknowledged_uncertain_attempt_id is not None else {})
        return collect_production_execution_admission(preparation, definition, request, retry_of=retry_of, **extra)
    policies, decision, root = evaluate_current_prepared_admission(preparation, definition, request,
        collect if retry_of is not None else collect_production_execution_admission)
    return detail, policies, decision, root


def _reserve(run, attempt):
    previous = attempt.committed_cost if attempt.committed_cost is not None else Decimal("0")
    delta = attempt.estimated_cost - previous
    run.committed_cost_total += delta
    run.estimated_cost_total += delta
    if attempt.estimated_cost:
        run.cost_currency = attempt.cost_currency
    attempt.committed_cost = attempt.estimated_cost
    run.save(update_fields=("committed_cost_total", "estimated_cost_total", "cost_currency", "updated_at"))


@security_boundary_resolution_cache()
def dispatch_production_execution(*, preparation_id, expected_preparation_revision, actor, retry_of_id=None, automatic=False,
                                  acknowledged_uncertain_attempt_id=None):
    if acknowledged_uncertain_attempt_id is not None and (automatic or acknowledged_uncertain_attempt_id != retry_of_id):
        raise ValidationError("未决主调用只能经显式确认新增尝试，不能自动重试。")
    if type(expected_preparation_revision) is not int or expected_preparation_revision < 1:
        raise ValidationError("必须提交明确的准备修订号")
    with registry_reference_write_guard():
        with _locked_preparations([preparation_id], actor) as rows:
            preparation = rows[str(preparation_id)]
            if not preparation.stage_run_id:
                raise ValidationError("生产派发必须使用阶段准备")
            retry_of = None
            if retry_of_id is not None:
                retry_of = GenerationAttempt.objects.select_related("execution_instance_version").get(
                    pk=retry_of_id, preparation=preparation)
                existing = GenerationAttempt.objects.filter(retry_of=retry_of).first()
                if existing is None:
                    from studio.services.execution_production_attempts import require_retryable_attempt
                    if acknowledged_uncertain_attempt_id is not None:
                        from studio.services.execution_media_uncertain_retry import require_uncertain_media_retry
                        require_uncertain_media_retry(retry_of)
                    else:
                        require_retryable_attempt(retry_of, automatic=automatic)
                    if automatic:
                        from studio.services.production_execution_recovery import production_retry_decision, retry_due
                        if not retry_due(production_retry_decision(retry_of)):
                            return None
            else:
                current = list(GenerationAttempt.objects.filter(preparation=preparation, retry_successor__isnull=True)[:2])
                if len(current) > 1:
                    raise ValidationError("同一准备存在未关联的多次调用，不能自动选择结果。")
                existing = current[0] if current else None
            if existing is not None:
                from studio.services.execution_resource_policy import REVIEW_REQUIRED
                if (preparation.reason_code == REVIEW_REQUIRED and existing.status == "queued"
                        and existing.submission_started_at is None):
                    if preparation.revision != expected_preparation_revision:
                        raise ValidationError("准备已变化，请重新读取", code="execution_preparation_conflict")
                    detail, policies, decision, root = current_production_gate(preparation)
                    record_prepared_dispatch_gate(existing, preparation, detail, policies, decision, root, phase="dispatch")
                    existing.save()
                # Never reset terminal/uncertain state or allocate another
                # attempt in response to a replay of the same Preparation.
                if existing.role == "primary" and existing.execution_instance_version.workflow_version_id:
                    if existing.generation_task_id is None:
                        raise ValidationError("既有工作流调用缺少原任务，不能在重复派发时补建")
                    from studio.services.execution_production_workflow_queue import read_prepared_generation_task
                    read_prepared_generation_task(existing.generation_task)
                return existing
            allowed = {"ready", "dispatched", "blocked"} if retry_of else {"ready"}
            if preparation.revision != expected_preparation_revision or preparation.status not in allowed:
                raise ValidationError("准备未就绪或已变化", code="execution_preparation_conflict")
            detail, policies, decision, root = current_production_gate(preparation, retry_of=retry_of,
                acknowledged_uncertain_attempt_id=acknowledged_uncertain_attempt_id)
            auxiliary = read_production_field_context(preparation, detail)
            version = execution_definition_version(preparation.execution_instance_version_id)
            stage = preparation.stage_run
            run = ProductionRun.objects.get(pk=stage.production_run_id)
            route_index = detail.intent["request"]["requested"].get("route_index")
            if route_index is not None and (type(route_index) is not int or not 0 <= route_index <= 32767):
                raise ValidationError("准备中的路线编号无效")
            ordinal = GenerationAttempt.objects.filter(stage_run=stage, unit_key=preparation.unit_key).aggregate(n=Max("attempt_number"))["n"]
            attempt = GenerationAttempt(stage_run=stage, preparation=preparation, execution_instance_version=version,
                retry_of=retry_of,
                retry_requested_by=actor if retry_of else None,
                role="auxiliary" if auxiliary else "primary", unit_key=preparation.unit_key, route_index=route_index,
                parent_preparation=auxiliary[0] if auxiliary else None,
                auxiliary_purpose=auxiliary[2]["purpose"] if auxiliary else "", field_batch_key=auxiliary[1].sha256 if auxiliary else "",
                attempt_number=auxiliary[2]["attempt_number"] if auxiliary else (ordinal or 0) + 1,
                model_deployment=version.deployment, prompt_profile_version=version.prompt_profile_version,
                prompt_profile_contract_sha256=version.prompt_profile_version.contract_sha256 if version.prompt_profile_version_id else "",
                operation=version.operation, request_sha256=preparation.request_sha256)
            record_prepared_dispatch_gate(attempt, preparation, detail, policies, decision, root, phase="dispatch")
            if not decision["can_submit"]:
                return None  # Persisted Preparation blocker, no queued job.
            attempt.is_external = root.value["budget"]["is_external"]
            reference_prepared_request(attempt, detail)
            _reserve(run, attempt)
            attempt.full_clean()
            attempt.save()
            if not auxiliary and version.workflow_version_id:
                from studio.services.execution_production_workflow_queue import allocate_prepared_generation_task
                allocate_prepared_generation_task(attempt)
            stage.attempt_count += 1
            stage.save(update_fields=("attempt_count", "updated_at"))
            run.generation_task_count += 1
            run.save(update_fields=("generation_task_count", "updated_at"))
            return attempt


@security_boundary_resolution_cache()
def begin_production_execution_submission(attempt):
    if attempt.claim_token is None:
        raise ValidationError("提交需要原worker的有效领取凭据")
    with registry_reference_write_guard():
        with locked_preparation_owner_rows([("stage_run_id", attempt.stage_run_id)]) as owners:
            stage = owners[("stage_run_id", str(attempt.stage_run_id))]
            preparation = ExecutionPreparation.objects.select_for_update().get(pk=attempt.preparation_id, stage_run=stage)
            current = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk, preparation=preparation)
            if current.claim_token != attempt.claim_token or current.status not in {"queued", "running"} or current.submission_started_at is not None:
                return False, current
            detail, policies, decision, root = current_production_gate(preparation)
            if current.request_sha256 != preparation.request_sha256 or current.request_snapshot_sha256 != preparation.request_sha256:
                raise ValidationError("原任务请求与冻结准备不一致")
            record_prepared_dispatch_gate(current, preparation, detail, policies, decision, root, phase="submission")
            if decision["can_submit"]:
                _reserve(ProductionRun.objects.get(pk=stage.production_run_id), current)
                current.status = "submitting"
                current.submission_started_at = timezone.now()
                current.heartbeat_at = current.submission_started_at
            else:
                current.status = "queued"
                current.claim_token, current.worker_id, current.claimed_at = None, "", None
            current.save()
            return decision["can_submit"], current
