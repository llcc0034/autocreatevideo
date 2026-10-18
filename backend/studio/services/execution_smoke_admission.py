"""Current original Smoke-root checks; only its own evidence gate is exempt."""
from django.core.exceptions import ValidationError

from studio.services.execution_asset_budget import estimate_execution_request, read_preparation_budget_state
from studio.services.execution_capacity_signatures import describe_execution_capacity
from studio.services.execution_capacity_observations import read_capacity_coverage
from studio.services.execution_definitions import execution_definition_version
from studio.services.execution_finalization import ExecutionAdmissionFacts
from studio.services.execution_instances import execution_configuration_max_bytes, selectable_execution_versions
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_preparation_owners import nonproduction_preparation_root
from studio.services.execution_resource_admission import read_execution_resource_state
from studio.services.execution_task_links import validate_smoke_preparation_link
from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError


def collect_smoke_execution_admission(preparation, definition, request):
    attempt = nonproduction_preparation_root(preparation)
    validate_smoke_preparation_link(attempt, preparation.execution_instance_version_id)
    resource = preparation.purpose == "resource_smoke"
    envelope = request.value
    transport = json_contract(envelope["transport"], max_bytes=execution_request_max_bytes())
    if (envelope.get("preparation_id") != str(preparation.pk)
            or transport.value["execution_instance_version_id"] != (None if resource else str(preparation.execution_instance_version_id))
            or transport.value["definition_sha256"] != definition.sha256
            or transport.value["target_fingerprint"] != attempt.target_sha256
            or transport.value["idempotency_key"] != str(preparation.pk)):
        raise ValidationError("验收准入必须对应本次冻结请求和实际执行目标")
    if resource:
        from studio.services.execution_resource_smoke_definition import load_resource_smoke_selection
        version = load_resource_smoke_selection(attempt, definition)
    else:
        version = execution_definition_version(preparation.execution_instance_version_id)
    actor = preparation.created_by
    blockers = []
    if not actor.is_active or not actor.is_staff or (not resource and not selectable_execution_versions(actor).filter(pk=version.pk).exists()):
        blockers.append({"code": "execution_owner_unavailable", "message": "本次验收操作人已停用或不再有验收权限"})
    if attempt.status not in {"preparing", "queued"} or attempt.submission_started_at is not None or attempt.provider_request_id:
        blockers.append({"code": "execution_root_not_submittable", "message": "验收已取消、已提交或远端状态未确定，不能重复提交"})
    resources = read_execution_resource_state(version)
    dimensions = describe_execution_capacity(definition, transport, runtime=resources.value["capacity_runtime"])
    capacity = (json_contract({**dimensions.value, "observation_id": None}, max_bytes=execution_configuration_max_bytes())
        if resource else read_capacity_coverage(version.pk, dimensions))
    adapter = definition.value["interface"]["output"].get("adapter")
    native = NATIVE_TEXT_ADAPTERS.get(version.operation)
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_ADAPTER
    if definition.value["target"]["driver_type"] not in {"async_rest", "comfyui"} and adapter not in {"text.prompt_fields/v1", "text.prompt_draft/v1", STRUCTURED_TEXT_ADAPTER} and not (native and adapter == native):
        blockers.append({"code": "execution_smoke_adapter_pending", "message": "此传输的组合验收worker尚未完成接线；已保留输入与字段，不会借用基础烟测代执行"})
    blockers.extend(resources.value["blockers"])
    budget = None
    try:
        estimate = estimate_execution_request(version.deployment, transport)
        budget = read_preparation_budget_state(preparation, estimate)
        blockers.extend(budget.value["blockers"])
    except (ValidationError, ProviderDriverError):
        blockers.append({"code": "execution_budget_contract_invalid", "message": "验收价格或根预算合同无效，请修正配置后继续"})
    return ExecutionAdmissionFacts((), capacity.value["state"], blockers, json_contract({"schema_version": 1,
        "preparation_id": str(preparation.pk), "request_sha256": request.sha256, "smoke_attempt_id": str(attempt.pk),
        "owner_id": str(actor.pk), "resources": resources.value, "budget": budget.value if budget is not None else None,
        "capacity": capacity.value, "dispatch_required": True},
        max_bytes=execution_configuration_max_bytes()))
