"""Describe the complete scalar input partition of a verified Workflow.

This is a projection, not a replacement graph compiler or publication service.
Inputs are partitioned by registered semantics: user scalars, prompt channels,
media slots, model files, and server-owned output identity. Nothing is read from
the imported graph as an implicit input/default.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_inputs import workflow_input_targets
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_media_parameters import model_media_parameter_layers
from studio.services.execution_parameters import compile_execution_parameter_contract, workflow_parameter_rule
from studio.services.execution_prompts import WORKFLOW_PROMPT_CHANNELS, workflow_prompt_channels
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_compiler import _validated_static_semantics, WorkflowCompilerError


def _error(key, message):
    raise ValidationError({f"workflow.parameters.{key}": message})


@security_boundary_resolution_cache()
def workflow_execution_interface(version):
    """Project after resolve_execution_resources has verified graph/Projector."""
    workflow = version.workflow_version
    try:
        static_semantics = _validated_static_semantics(workflow.input_contract)
    except WorkflowCompilerError as exc:
        raise ValidationError({"static_semantics": str(exc)}) from exc
    if workflow is None:
        raise ValidationError({"workflow_version": "必须选择精确工作流"})
    definitions = workflow.input_contract.get("parameters")
    if not isinstance(definitions, dict):
        _error("contract", "缺少参数合同")
    bindings = {}
    for row in workflow.semantic_bindings.values("semantic_key", "value_type", "is_required"):
        bindings.setdefault(row["semantic_key"], []).append(row)
    if set(bindings) - definitions.keys() or definitions.keys() - bindings.keys():
        _error("contract", "参数定义与实际接线未精确对应，不能推断缺失的输入")
    prompts = workflow_prompt_channels(workflow)
    inputs = workflow_input_targets(workflow)
    parameters, constants, files, server_inputs = {}, {}, [], {}
    for key, definition in definitions.items():
        rows = bindings[key]
        if not isinstance(definition, dict) or any(row["value_type"] != definition.get("type") for row in rows):
            _error(key, "参数与接线类型不一致")
        declared = definition.get("required")
        if declared is not None and type(declared) is not bool:
            _error(key, "必填标志必须是布尔值")
        required = declared is True or any(row["is_required"] for row in rows)
        if key in WORKFLOW_PROMPT_CHANNELS.values() or definition["type"] in {"asset_image", "asset_video", "asset_audio"}:
            continue
        if definition["type"] == "model_artifact":
            files.append(key)
            continue
        rule = workflow_parameter_rule(key, definition, required=required)
        if key == "output.prefix":
            # A worker-generated storage identity is not a reusable user value.
            server_inputs[key] = {"type": "string", "required": required}
            continue
        marker = definition.get("asset_group_constant", False)
        if type(marker) is not bool:
            _error(key, "动态模板固定值标记必须是布尔值")
        if marker:
            if "default" not in definition:
                _error(key, "动态模板常量必须在冻结合同中明确提供值")
            if "const" in rule and rule["const"] != definition["default"]:
                _error(key, "动态模板常量与冻结固定值冲突")
            constants[key] = [{"source": f"workflow.{workflow.pk}.asset_group_constants", "rule": {**rule, "const": definition["default"]}}]
        else:
            parameters[key] = [{"source": f"workflow.{workflow.pk}.parameters", "rule": rule}]
    # The role mapping is the existing reviewed Workflow semantic vocabulary,
    # not matching by graph/model display name or widget-name substring.
    overlap = set(static_semantics) & set(workflow.input_contract["parameters"])
    if overlap:
        raise ValidationError({"static_semantics": "静态事实不能同时声明为节点参数：" + ", ".join(sorted(overlap))})
    media = model_media_parameter_layers(version.deployment.profile_version, {
        role: key for role, key in (
            ("width", "image.width"), ("height", "image.height"), ("fps", "video.fps"), ("duration_ms", "video.duration_ms"),
        ) if key in parameters or key in static_semantics
    })
    for key, layer in media["layers"].items():
        if key in parameters:
            parameters[key].insert(0, layer)
        else:
            # A frozen non-widget fact is still subject to model constraints;
            # validate it without turning it into an editable parameter.
            compile_execution_parameter_contract({key: [layer, {
                "source": f"workflow.{workflow.pk}.static_semantics",
                "rule": {"type": layer["rule"]["type"], "required": True, "const": static_semantics[key]},
            }]})
    return json_contract({
        "parameters": compile_execution_parameter_contract(parameters),
        "template_constants": compile_execution_parameter_contract(constants),
        "prompt_channels": prompts, "input_targets": inputs,
        "model_inputs": sorted(files), "server_inputs": server_inputs,
        "media_constraints": media["media_constraints"],
        "media_semantic_keys": media["semantic_keys"],
        "resolution_strategy": deepcopy(workflow.input_contract.get("resolution_strategy")),
        "frame_policy": deepcopy(workflow.input_contract.get("frame_policy")),
        "static_semantics": static_semantics,
    }, max_bytes=execution_configuration_max_bytes()).value
