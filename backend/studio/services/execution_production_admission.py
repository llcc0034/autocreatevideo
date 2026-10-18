"""ProductionRun admission for prepared text and registered media, without I/O.

The original Run is the budget/authorization root. Callers hold Run -> Stage
-> Preparation locks; this is not an alternate accounting or routing system.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from studio.models import GenerationAttempt, StageRun
from studio.services.execution_asset_budget import _money, estimate_execution_request
from studio.services.execution_capacity_observations import read_capacity_coverage
from studio.services.execution_capacity_signatures import describe_execution_capacity
from studio.services.execution_definitions import execution_definition_version
from studio.services.execution_finalization import ExecutionAdmissionFacts
from studio.services.execution_instances import execution_configuration_max_bytes, selectable_execution_versions
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
from studio.services.execution_auxiliary_requests import read_production_field_context
from studio.services.execution_resource_admission import read_execution_resource_state
from studio.services.execution_smoke_evidence import read_instance_smoke_facts
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.provider_registry import ProviderDriverError


def production_task_budget_state(run, attempt):
    from studio.services.execution_resource_policy import read_resource_policy
    limit = read_resource_policy().value["values"]["call_count"]
    used = run.generation_task_count
    if type(used) is not int or used < 0:
        raise ValidationError("运行任务计数无效，不能按零次继续")
    # A queued call already consumed its slot at allocation. Rechecking it
    # before submission must not reserve or count the same call twice.
    requested = 1 if attempt is None else 0
    blockers = []
    return {"limit": limit, "allocated": used, "requested": requested, "blockers": blockers}


def production_budget_state(run, deployment, transport, attempt):
    from studio.services.asset_generation import _is_external
    estimate = estimate_execution_request(deployment, transport)
    amount, currency = _money(estimate.value["amount"]), estimate.value["currency"]
    actual, committed = _money(run.actual_cost_total), _money(run.committed_cost_total)
    own = _money(attempt.committed_cost) if attempt is not None else Decimal("0")
    if actual is None or committed is None or own is None or own > committed:
        raise ValidationError("运行费用账目不完整或预留不一致，不能按零费用继续")
    blockers = []
    def block(code, message):
        blockers.append({"code": code, "message": message})
    external = _is_external(deployment)
    if external and not run.allow_external_calls:
        block("execution_external_authorization_required", "本次运行尚未授权外部模型调用")
    if amount is None:
        block("execution_estimate_unknown", "本次调用费用未知，请先配置正式价格")
    # A declared zero-cost local call has no monetary exposure and does not
    # replace the run's budget currency. Unknown/nonzero prices still fail closed.
    if amount != Decimal("0") and (((actual or committed) and run.cost_currency != currency)
            or (run.cost_currency and currency != run.cost_currency)):
        block("execution_budget_currency_conflict", "运行与本次调用的费用币种不同，不能自动换算")
    projected = actual + committed - own + amount if amount is not None else None
    # The estimate describes this call; the aggregate describes the Run's
    # ledger. A free local call must not relabel existing paid exposure.
    if amount == Decimal("0") and run.cost_currency:
        currency = run.cost_currency
    from studio.services.execution_resource_policy import read_resource_policy
    threshold = read_resource_policy().value["values"]["budget_by_currency"].get(currency)
    return {"schema_version": 1, "estimate": estimate.value, "is_external": external,
        "actual_total": str(actual), "committed_total": str(committed), "replaced_reservation": str(own),
        "projected_total": str(projected) if projected is not None else None,
        "budget_limit": threshold,
        "currency": currency, "blockers": blockers}


def collect_production_execution_admission(preparation, definition, request, *, retry_of=None,
                                          acknowledged_uncertain_attempt_id=None):
    if not preparation.stage_run_id or preparation.purpose != "production":
        raise ValidationError("生产准入必须使用真实阶段准备")
    stage = StageRun.objects.select_related("production_run__owner", "production_run__project").get(pk=preparation.stage_run_id)
    run = stage.production_run
    envelope = verify_json_contract(request.value, schema_version=request.schema_version, size_bytes=request.size_bytes,
        sha256=request.sha256, max_bytes=execution_request_max_bytes()).value
    if envelope.get("preparation_id") != str(preparation.pk) or not isinstance(envelope.get("transport"), dict):
        raise ValidationError("生产请求与准备不一致")
    transport = json_contract(envelope["transport"], max_bytes=execution_request_max_bytes())
    version = execution_definition_version(preparation.execution_instance_version_id)
    if (transport.value.get("execution_instance_version_id") != str(version.pk)
            or transport.value.get("definition_sha256") != definition.sha256
            or transport.value.get("target_fingerprint") != definition.value["target_fingerprint"]
            or transport.value.get("idempotency_key") != str(preparation.pk)):
        raise ValidationError("生产准入不能替换冻结的执行目标或请求")
    context = read_production_field_context(preparation)
    if run.plan_source == "recipe":
        from studio.services.production_recipe_routing import require_current_recipe_preparation
        require_current_recipe_preparation(context[0] if context is not None else preparation, stage=stage)
    from studio.services.async_media_contract import ASYNC_MEDIA_OPERATIONS
    from studio.services.production_stage_catalog import STAGE_MEDIA_OUTPUT_KINDS, validate_stage_execution_definition
    target = definition.value["target"]
    workflow_media = (target["driver_type"] == "comfyui" and target["workflow"] is not None
        and stage.stage_key in STAGE_MEDIA_OUTPUT_KINDS and context is None)
    media = workflow_media or (version.operation in ASYNC_MEDIA_OPERATIONS and target["driver_type"] == "async_rest"
        and stage.stage_key in STAGE_MEDIA_OUTPUT_KINDS)
    if ((version.operation not in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES} and not media)
            or (target["workflow"] is not None and not workflow_media)
            or (version.operation in FIELD_OPERATION_MODES) != (context is not None)):
        raise ValidationError("此生产队列尚未接通该执行协议，不能改用其他执行器", code="execution_interface_unavailable")
    if media:
        validate_stage_execution_definition(stage.stage_key, definition)
        from studio.services.execution_preparations import read_execution_preparation_detail
        from studio.services.execution_production_media import read_production_media_deadlines
        read_production_media_deadlines(read_execution_preparation_detail(preparation))
    blockers = []
    if (not selectable_execution_versions(run.owner).filter(pk=version.pk).exists()
            or run.project.owner_id != run.owner_id):
        blockers.append({"code": "execution_owner_unavailable", "message": "运行所有者已停用或无权执行此实例/项目"})
    if run.status != "running" or stage.status != "running":
        blockers.append({"code": "execution_root_not_submittable", "message": "运行或阶段已停止，不可开始新的调用"})
    if context is not None and (context[0].status == "cancelled" or context[0].request_frozen_at is not None):
        blockers.append({"code": "execution_parent_not_submittable", "message": "接收字段的主准备已取消或冻结，不能再调用辅助模型"})
    attempts = list(GenerationAttempt.objects.filter(preparation=preparation, retry_successor__isnull=True).only("role", "status", "ignore_late_result",
        "submission_started_at", "provider_request_id", "committed_cost").order_by("attempt_number")[:2])
    attempt = attempts[0] if attempts else None
    if retry_of is not None:
        from studio.services.execution_production_attempts import require_retryable_attempt
        if acknowledged_uncertain_attempt_id is not None:
            from studio.services.execution_media_uncertain_retry import require_uncertain_media_retry
            if retry_of.pk != acknowledged_uncertain_attempt_id:
                raise ValidationError("未决确认不属于本次重试主调用。")
            require_uncertain_media_retry(retry_of)
        else:
            require_retryable_attempt(retry_of)
        if len(attempts) != 1 or attempt.pk != retry_of.pk or context is not None:
            raise ValidationError("重试目标已变化，必须重新读取原主调用。")
        attempt, attempts = None, []
    if len(attempts) > 1 or (attempt is not None and (attempt.role != ("auxiliary" if context else "primary")
            or attempt.status not in {"queued", "running"} or attempt.ignore_late_result
            or attempt.submission_started_at is not None or attempt.provider_request_id)):
        blockers.append({"code": "execution_root_not_submittable", "message": "准备已提交、取消或远端状态未确定，不能再次调用"})
    unresolved = GenerationAttempt.objects.filter(stage_run__production_run=run,
        error_code="uncertain_remote_response", retry_successor__isnull=True)
    if retry_of is not None and acknowledged_uncertain_attempt_id == retry_of.pk:
        unresolved = unresolved.exclude(pk=retry_of.pk)
    if unresolved.exists():
        blockers.append({"code": "execution_submission_unresolved", "message": "本次运行仍有未核对的远端提交，请先处理原任务"})
    task_budget = production_task_budget_state(run, attempt)
    blockers.extend(task_budget["blockers"])
    resources = read_execution_resource_state(version)
    capacity = read_capacity_coverage(version.pk,
        describe_execution_capacity(definition, transport, runtime=resources.value["capacity_runtime"]))
    blockers.extend(resources.value["blockers"])
    budget = None
    try:
        budget = production_budget_state(run, version.deployment, transport, attempt)
        blockers.extend(budget["blockers"])
    except (ValidationError, ProviderDriverError):
        blockers.append({"code": "execution_budget_contract_invalid", "message": "运行费用账目或估算无效，请修正后继续"})
    root = json_contract({"schema_version": 1, "preparation_id": str(preparation.pk), "request_sha256": request.sha256,
        "production_run_id": str(run.pk), "stage_run_id": str(stage.pk), "owner_id": str(run.owner_id),
        "resources": resources.value, "capacity": capacity.value, "budget": budget, "task_budget": task_budget, "dispatch_required": True},
        max_bytes=execution_configuration_max_bytes())
    return ExecutionAdmissionFacts(read_instance_smoke_facts(version, definition), capacity.value["state"], blockers, root)
