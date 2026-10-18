"""Read an asset worker request from its sole frozen Preparation.

This adapter never compiles, chooses fields, draws seeds or grants admission.
The returned driver arguments are an in-memory projection, not another saved
request snapshot. Submission and reconciliation use the same frozen identity.
"""
from copy import deepcopy
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from studio.models import ExecutionPreparation, ProviderProfile
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_task_links import validate_asset_preparation_link
from studio.services.json_contracts import JsonContract, json_contract


@dataclass(frozen=True)
class PreparedAssetExecution:
    preparation: ExecutionPreparation
    definition: JsonContract
    transport: JsonContract
    resolved_inputs: JsonContract
    runtime_budget: dict
    payload: dict
    output_kind: str
    output_contract: dict | None
    output_prefix: str | None
    field_request: JsonContract | None


def read_asset_prompt_hint(job):
    """Read the actually frozen positive channel for materialized metadata.

    No current strategy, candidate selection, or old Job input column is read.
    A no-prompt operation legitimately has no generated visual anchor.
    """
    validate_asset_preparation_link(job, job.execution_instance_version_id)
    preparation = job.execution_preparations.get()
    _, definition, transport = read_frozen_execution_request(preparation)
    if (str(job.execution_instance_version_id) != definition.value["version_id"]
            or transport.value["definition_sha256"] != definition.sha256):
        raise ValidationError("素材元数据与实际执行实例不一致")
    channels = transport.value.get("prompt_channels")
    if not isinstance(channels, dict) or not isinstance(channels.get("values"), dict):
        raise ValidationError("冻结请求缺少实际提示词通道，不能从当前配置补齐")
    values = channels["values"]
    if any(not isinstance(value, str) for value in values.values()):
        raise ValidationError("冻结提示词通道不是文本")
    if "positive_prompt" not in definition.value["interface"]["prompt"]["channels"]:
        if "positive_prompt" in values:
            raise ValidationError("冻结请求包含未声明的正向通道")
        return ""
    rule = definition.value["interface"]["prompt"]["channels"]["positive_prompt"]["rule"]
    if rule["required"] and "positive_prompt" not in values:
        raise ValidationError("冻结请求缺少必需的正向提示词，不能用空值替代")
    return values.get("positive_prompt", "")


def read_prepared_asset_execution(job, header):
    """Called after verifying the Job header; absent preparation is an error."""
    validate_asset_preparation_link(job, job.execution_instance_version_id)
    try:
        preparation = job.execution_preparations.get()
    except (ObjectDoesNotExist, ExecutionPreparation.MultipleObjectsReturned) as exc:
        raise ValidationError("素材任务必须且只能关联本次执行准备，不能恢复历史请求") from exc
    if preparation.execution_instance_version_id != job.execution_instance_version_id:
        raise ValidationError("素材任务与执行准备的实例版本不一致")
    detail, definition, transport = read_frozen_execution_request(preparation)
    if header["definition_sha256"] != definition.sha256:
        raise ValidationError("素材请求头、准备与执行定义不一致")
    value = transport.value
    inputs = detail.resolved_inputs.get("inputs")
    if not isinstance(inputs, dict):
        raise ValidationError("执行准备缺少明确的输入事实")
    resolved_inputs = json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes())
    field_request = None
    if job.parent_preparation_id:
        from studio.services.execution_auxiliary_requests import read_asset_field_batch
        field_request = read_asset_field_batch(job)
        if value["field_request_sha256"] != field_request.sha256:
            raise ValidationError("字段子调用与父准备的精确批次不一致")
    elif value["field_request_sha256"] is not None:
        raise ValidationError("主请求不能混入辅助字段批次")
    output_contract, output_prefix = None, None
    payload = deepcopy(value["payload"])
    if value["driver_type"] == ProviderProfile.DriverType.COMFYUI:
        # read_frozen_execution_request already verifies the complete compiled
        # contract shared with Smoke and production, not only the graph hash.
        compiled = payload["compiled_workflow"]
        output_contract = compiled["output_contract"]
        from studio.services.execution_workflow_outputs import frozen_comfy_output_prefix
        output_prefix = frozen_comfy_output_prefix(compiled, preparation_id=preparation.pk)
        output_kind = output_contract["primary"].get("kind")
        payload = {"workflow": deepcopy(compiled["graph"]), "submission_token": value["idempotency_key"]}
    else:
        output_kind = value["output"].get("kind")
    if output_kind != header["output_kind"]:
        raise ValidationError("冻结请求产物类型与素材任务不一致")
    return PreparedAssetExecution(preparation, definition, transport, resolved_inputs,
        deepcopy(header["runtime_budget"]), payload, output_kind, output_contract, output_prefix, field_request)
