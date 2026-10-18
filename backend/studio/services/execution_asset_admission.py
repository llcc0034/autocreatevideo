"""Formal asset-root facts for finalization; not a remote submission permit.

Uses the original Job's owner, authorization and accounting facts. Claim and
remote concurrency remain in the original workers and must be checked again
before I/O; this collector neither reserves cost nor acquires a remote slot.
"""
from django.core.exceptions import ValidationError

from studio.models import AssetGenerationJob
from studio.services.execution_asset_budget import estimate_execution_request, read_asset_budget_state
from studio.services.execution_capacity_signatures import describe_execution_capacity
from studio.services.execution_capacity_observations import read_capacity_coverage
from studio.services.execution_definitions import execution_definition_version
from studio.services.execution_finalization import ExecutionAdmissionFacts
from studio.services.execution_instances import execution_configuration_max_bytes, selectable_execution_versions
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_resource_admission import read_execution_resource_state
from studio.services.execution_smoke_evidence import read_instance_smoke_facts
from studio.services.execution_task_links import validate_asset_preparation_link
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.provider_registry import ProviderDriverError


def collect_asset_execution_admission(preparation, definition, request):
    """Run under the ordered root/Preparation locks of finalization/dispatch."""
    if not preparation.asset_job_id:
        raise ValidationError("素材准入只能读取真实素材任务根")
    job = AssetGenerationJob.objects.select_related("owner", "model_deployment__profile_version__provider").get(pk=preparation.asset_job_id)
    validate_asset_preparation_link(job, preparation.execution_instance_version_id)
    if not isinstance(request, JsonContract) or not isinstance(definition, JsonContract):
        raise ValidationError("素材准入需要核对后的完整定义与本次请求")
    envelope = verify_json_contract(request.value, schema_version=request.schema_version, size_bytes=request.size_bytes,
        sha256=request.sha256, max_bytes=execution_request_max_bytes()).value
    if not isinstance(envelope, dict) or envelope.get("preparation_id") != str(preparation.pk) or not isinstance(envelope.get("transport"), dict):
        raise ValidationError("素材准入请求与准备不一致")
    transport = json_contract(envelope["transport"], max_bytes=execution_request_max_bytes())
    if (transport.value.get("execution_instance_version_id") != str(job.execution_instance_version_id)
            or transport.value.get("definition_sha256") != definition.sha256
            or transport.value.get("target_fingerprint") != definition.value["target_fingerprint"]
            or transport.value.get("idempotency_key") != str(preparation.pk)):
        raise ValidationError("素材准入不能替换本次请求或执行目标")
    version = execution_definition_version(job.execution_instance_version_id)
    blockers = []
    if not selectable_execution_versions(job.owner).filter(pk=version.pk).exists():
        blockers.append({"code": "execution_owner_unavailable", "message": "根任务所有者已停用或无权执行此实例"})
    if job.ignore_late_result or job.status not in {"preparing", "queued"} or job.submission_started_at is not None or job.provider_request_id or job.provider_request_id_sha256:
        blockers.append({"code": "execution_root_not_submittable", "message": "任务已取消、已提交或远端状态未确定，不能作为新请求提交"})
    if job.parent_preparation_id:
        parent = job.parent_preparation
        if parent.status in {"cancelled", "ready", "dispatched"} or parent.request_frozen_at is not None:
            blockers.append({"code": "execution_parent_not_preparing", "message": "父请求已取消或冻结，不再允许新的字段调用"})
        if parent.asset_job_id and (parent.asset_job.ignore_late_result or parent.asset_job.status != "preparing"):
            blockers.append({"code": "execution_parent_not_preparing", "message": "父素材任务不再准备中，不能继续辅助调用"})
        if parent.deployment_smoke_id or parent.workflow_smoke_id:
            from studio.services.execution_preparation_owners import nonproduction_preparation_root
            if nonproduction_preparation_root(parent).status != "preparing":
                blockers.append({"code": "execution_parent_not_preparing", "message": "父验收不再准备中，不能继续辅助调用"})
    resources = read_execution_resource_state(version)
    capacity = read_capacity_coverage(version.pk,
        describe_execution_capacity(definition, transport, runtime=resources.value["capacity_runtime"]))
    blockers.extend(resources.value["blockers"])
    budget = None
    try:
        estimate = estimate_execution_request(version.deployment, transport)
        budget = read_asset_budget_state(job, estimate)
        blockers.extend(budget.value["blockers"])
    except (ValidationError, ProviderDriverError):
        blockers.append({"code": "execution_budget_contract_invalid", "message": "费用估算或根预算合同无效，请修正配置后继续"})
    facts = read_instance_smoke_facts(version, definition)
    root = json_contract({"schema_version": 1, "preparation_id": str(preparation.pk), "request_sha256": request.sha256,
        "asset_job_id": str(job.pk), "owner_id": str(job.owner_id), "resources": resources.value, "budget": budget.value if budget is not None else None,
        "capacity": capacity.value,
        "dispatch_required": True}, max_bytes=execution_configuration_max_bytes())
    return ExecutionAdmissionFacts(facts, capacity.value["state"], blockers, root)
