"""Read the compiled Comfy request shared by every prepared execution root.

This validates saved facts, never recompiles a graph or consults a current
route, PromptArtifact, model file inventory or default parameter source.
"""
from copy import deepcopy
from dataclasses import fields

from django.core.exceptions import ValidationError

from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_workflow_outputs import frozen_comfy_output_prefix
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_compiler import CompiledWorkflow
from studio.services.workflow import validate_comfy_graph, WorkflowBuildError


@security_boundary_resolution_cache()
def verify_execution_workflow_transport(definition, transport):
    """Return an isolated projection of the one immutable compiled request."""
    definition = verify_execution_definition_payload(definition)
    if not isinstance(transport, JsonContract):
        raise ValidationError("工作流读取需要完整冻结请求及摘要")
    value = verify_json_contract(transport.value, schema_version=transport.schema_version,
        size_bytes=transport.size_bytes, sha256=transport.sha256,
        max_bytes=execution_request_max_bytes()).value
    target, interface = definition.value["target"], definition.value["interface"]
    workflow = target.get("workflow")
    if (not isinstance(value, dict) or not isinstance(workflow, dict)
            or value.get("driver_type") != "comfyui" or target.get("driver_type") != "comfyui"
            or value.get("execution_instance_version_id") != definition.value["version_id"]
            or value.get("definition_sha256") != definition.sha256
            or value.get("target_fingerprint") != definition.value["target_fingerprint"]
            or value.get("operation") != definition.value["configuration"]["operation"]
            or value.get("output") != interface["output"] or value.get("field_request_sha256") is not None
            or not isinstance(value.get("idempotency_key"), str) or not value["idempotency_key"]):
        raise ValidationError("工作流提交请求与冻结执行实例身份不一致")
    payload = value.get("payload")
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "compiled_workflow", "media_preparation"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
        raise ValidationError("冻结工作流请求结构不完整")
    compiled = payload["compiled_workflow"]
    if (not isinstance(compiled, dict) or set(compiled) != {field.name for field in fields(CompiledWorkflow)}
            or compiled["compiler_id"] != workflow["compiler_id"]
            or type(compiled["compiler_version"]) is not int or compiled["compiler_version"] != workflow["compiler_version"]
            or compiled["workflow_contract_sha256"] != workflow["execution_contract_sha256"]):
        raise ValidationError("冻结提交图与原精确工作流编译合同不一致")
    for name, expected_type in (("graph", dict), ("resolved_semantics", dict), ("asset_bindings", list), ("model_requirements", list)):
        if (not isinstance(compiled[name], expected_type)
                or json_contract(compiled[name], max_bytes=execution_configuration_max_bytes()).sha256 != compiled[f"{name}_sha256"]):
            raise ValidationError({name: "冻结工作流内容与摘要不一致"})
    try:
        validate_comfy_graph(compiled["graph"])
    except WorkflowBuildError as exc:
        raise ValidationError({"graph": str(exc)}) from exc
    output = compiled["output_contract"]
    expected = deepcopy(interface["output"]["contract"])
    # Recheck derived dimensions against saved per-use source observations,
    # without loading a different Asset, decoding it again or rebuilding a graph.
    strategy = interface.get("resolution_strategy") or {}
    if strategy.get("mode") == "source_image_derived":
        from studio.services.workflow_resolution import resolve_prepared_image_resolution, WorkflowResolutionError
        try:
            resolution = resolve_prepared_image_resolution(strategy, compiled["asset_bindings"])
        except WorkflowResolutionError as exc:
            raise ValidationError({"resolution": str(exc)}) from exc
        expected = {**expected, "schema_version": 2, "resolution": resolution}
        if any(compiled["resolved_semantics"].get(f"image.{key}") != resolution[key] for key in ("width", "height")):
            raise ValidationError("冻结输出尺寸与本次素材使用的解析尺寸不一致")
    if output != expected or not isinstance(output, dict) or not isinstance(output.get("primary"), dict):
        raise ValidationError("提交图产出合同与执行实例冻结声明不一致")
    primary = output["primary"]
    if str(primary.get("node_id", "")) not in compiled["graph"]:
        raise ValidationError("冻结主输出节点不存在于本次提交图")
    frozen_comfy_output_prefix(compiled, preparation_id=value["idempotency_key"])
    return deepcopy(compiled)
