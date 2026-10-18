"""Pure transport request assembly from a verified definition and saved facts.

No admission verdict, field approval, persistence, model invocation or dispatch.
The enclosing preparation finalizer owns locking, source freshness and budget.
"""
from copy import deepcopy
from dataclasses import asdict
import json

from django.core.exceptions import ValidationError

from studio.models import ProviderProfile
from studio.services.async_media_contract import validate_standard_payload
from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION
from studio.services.execution_request_storage import execution_request_max_bytes, request_part_contract
from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
from studio.services.execution_parameters import resolve_execution_parameters
from studio.services.execution_prompts import normalize_compiled_profile_channels, normalize_manual_execution_channels
from studio.services.execution_text_contracts import TEXT_EXECUTION_OPERATIONS, build_execution_text_payload
from studio.services.execution_media_parameters import validate_resolved_execution_media
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


@security_boundary_resolution_cache()
def compile_execution_transport_request(definition, *, prepared_parameters, manual_channels,
        idempotency_key, profile_compilation=None, field_request=None, workflow_snapshot=None,
        total_duration_ms=None, workflow_inputs=None, operation_inputs=None, operation_source_reference=None):
    """Compile registered transports with explicit, already-authorized facts.

    ``profile_compilation`` and ``field_request`` must come from the parent's
    verified field pipeline; they are not public request JSON. ComfyUI uses
    the shared graph compiler. Workflow inputs and reference labels come from
    the frozen preparation, never public token/path/label overrides. Media
    placeholders still require the original worker's managed upload adapter.
    """
    definition = verify_execution_definition_payload(definition)
    payload = definition.value
    interface, target = payload["interface"], payload["target"]
    if not isinstance(idempotency_key, str) or not idempotency_key or idempotency_key.strip() != idempotency_key or len(idempotency_key) > 191:
        raise ValidationError({"idempotency_key": "需要本次持久化且不超过191字符的明确请求标识"})
    driver, operation = target["driver_type"], payload["configuration"]["operation"]
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS, build_native_text_request
    native = operation in NATIVE_TEXT_OPERATIONS and driver != ProviderProfile.DriverType.COMFYUI
    operation_plan = None
    field_system_layout = None
    if not native and (operation_inputs is not None or operation_source_reference is not None):
        raise ValidationError("此操作不能接收未消费的原生业务输入")
    allowed_targets = {"field_request"} if interface.get("operation") in FIELD_OPERATION_MODES else set()
    if native:
        allowed_targets = set(interface["input_targets"])
    media_inputs = any(spec["kind"] in {"image", "video", "audio"} for spec in payload["configuration"]["inputs"])
    if driver != ProviderProfile.DriverType.COMFYUI and (set(interface["input_targets"]) - allowed_targets or media_inputs):
        raise ValidationError("此接口的素材提交适配尚未接通，不能丢弃已声明素材", code="execution_interface_unavailable")
    if driver != ProviderProfile.DriverType.COMFYUI and (workflow_snapshot is not None or total_duration_ms is not None
            or workflow_inputs is not None):
        raise ValidationError("API请求不能混入未消费的工作流事实")
    labels = []
    media = None
    if driver == ProviderProfile.DriverType.COMFYUI:
        if media_inputs and workflow_inputs is None:
            raise ValidationError("已声明素材必须提供原准备中的实际使用与端口，不能读取示例文件")
        if workflow_inputs is not None:
            from studio.services.execution_workflow_media import prepare_execution_workflow_media
            media = prepare_execution_workflow_media(definition, workflow_inputs).value
            labels = media["declared_reference_labels"]
    parameters = verify_prepared_execution_parameters(definition, prepared_parameters)
    if interface["prompt"]["mode"] == "profile":
        if manual_channels:
            raise ValidationError("策略组装不能同时覆盖人工提示词通道")
        channels = normalize_compiled_profile_channels(interface["prompt"], profile_compilation, declared_reference_labels=labels)
    else:
        if profile_compilation is not None:
            raise ValidationError("人工或无提示词方式不能混入策略编译结果")
        channels = normalize_manual_execution_channels(interface["prompt"], manual_channels, declared_reference_labels=labels)
    if driver == ProviderProfile.DriverType.COMFYUI:
        if field_request is not None:
            raise ValidationError("工作流主请求不能接收字段批次")
        body = _compile_workflow_body(payload, parameters, channels, workflow_snapshot,
            idempotency_key=idempotency_key, total_duration_ms=total_duration_ms, media=media)
    elif driver == ProviderProfile.DriverType.ASYNC_REST:
        if field_request is not None:
            raise ValidationError("异步媒体主请求不能接收字段批次")
        schema = interface.get("request_schema")
        if not isinstance(schema, dict):
            raise ValidationError("冻结执行定义缺少原API请求Schema，不能重读当前模型补齐")
        body = {"schema_version": 1, "idempotency_key": idempotency_key,
            "model": interface["model_identifier"], **parameters["values"]}
        for name, value in channels["values"].items():
            key = interface["prompt"]["channels"][name]["semantic_key"]
            if key in body:
                raise ValidationError("提示词通道与参数争写同一API字段")
            body[key] = value
        validate_standard_payload({"request_schema": schema}, body)
    elif native:
        if field_request is not None:
            raise ValidationError("原生业务文本不能接收字段批次")
        body, operation_plan = build_native_text_request(interface, inputs=operation_inputs,
            parameters=parameters["values"], channels=channels["values"], profile_compilation=profile_compilation,
            source_reference=operation_source_reference)
    elif operation in TEXT_EXECUTION_OPERATIONS and driver in {
        ProviderProfile.DriverType.DEEPSEEK_API, ProviderProfile.DriverType.OPENAI_COMPATIBLE,
        ProviderProfile.DriverType.LOCAL_QWEN,
    }:
        # Generated seeds have a separate provenance channel in the original
        # text protocol; they cannot also masquerade as caller-supplied values.
        generated = parameters["generated_values"]
        values = {key: value for key, value in parameters["values"].items() if key not in generated}
        field_system_layout = {} if operation in (*FIELD_OPERATION_MODES, STRUCTURED_TEXT_OPERATION) else None
        if operation == STRUCTURED_TEXT_OPERATION:
            from studio.services.execution_structured_text import structured_text_plan
            operation_plan = structured_text_plan(interface["business_output"])
        body = build_execution_text_payload(interface, parameters=values, generated_values=generated,
            channels=manual_channels, profile_compilation=profile_compilation, field_request=field_request,
            system_layout=field_system_layout)
    else:
        raise ValidationError("执行目标尚无已接通的逐次请求适配器", code="execution_interface_unavailable")
    chat_layout = None
    from studio.services.chat_body import CHAT_DRIVERS, assemble_chat_body
    if driver in CHAT_DRIVERS:
        _body, chat_layout = assemble_chat_body(interface["model_identifier"], body)
    return request_part_contract({"schema_version": 1, "execution_instance_version_id": payload["version_id"],
        "definition_sha256": definition.sha256, "target_fingerprint": payload["target_fingerprint"],
        "operation": operation, "driver_type": driver, "idempotency_key": idempotency_key,
        "parameters": parameters, "prompt_channels": channels,
        # Compiler tuples become JSON arrays at this persistence/wire boundary
        # so the first response and a database reload have identical types.
        "profile_compilation": json.loads(canonical_json_bytes(asdict(profile_compilation))) if profile_compilation is not None else None,
        "field_request_sha256": field_request.sha256 if field_request is not None else None,
        "payload": body, "output": deepcopy(interface["output"]),
        **({"chat_body_layout": chat_layout} if chat_layout is not None else {}),
        **({"system_message_layout": field_system_layout} if field_system_layout is not None else {}),
        **({"operation_plan": operation_plan} if operation_plan is not None else {})}, max_bytes=execution_request_max_bytes(), part="transport")


def _compile_workflow_body(definition, parameters, channels, snapshot, *, idempotency_key, total_duration_ms, media):
    from studio.services.workflow_compiler import compile_frozen_workflow_values, WorkflowCompilerError

    target, interface = definition["target"], definition["interface"]
    workflow = target["workflow"]
    if (not isinstance(snapshot, dict) or not isinstance(workflow, dict)
            or snapshot.get("workflow_version_id") != workflow["id"]
            or snapshot.get("model_identifier") != target["model"]["identifier"]
            or any(snapshot.get(key) != workflow[key] for key in ("compiler_id", "compiler_version", "execution_contract_sha256"))):
        raise ValidationError("请求必须读取执行定义中原精确工作流合同，不选择当前其他版本")
    if set(interface["server_inputs"]) - {"output.prefix"}:
        raise ValidationError("工作流含尚未接入的服务端输入", code="execution_interface_unavailable")
    values = {key: value for key, value in parameters["values"].items() if key not in interface["static_semantics"]}
    constants = resolve_execution_parameters(interface["template_constants"], {})["values"] if interface["template_constants"] else {}
    if set(constants) & set(values):
        raise ValidationError("工作流模板常量与本次参数争写")
    values.update(constants)
    for name, value in channels["values"].items():
        key = interface["prompt"]["channels"][name]["semantic_key"]
        if key in values:
            raise ValidationError("工作流提示词通道与参数争写")
        values[key] = value
    if "output.prefix" in interface["server_inputs"]:
        values["output.prefix"] = f"autoprompt/execution/{idempotency_key}"
    semantic_media = media["semantic_inputs"] if media is not None else {}
    bindings = {row["item_key"]: row for row in media["bindings"]} if media is not None else {}
    consumed = set()
    def resolve_media(key, _rule):
        source = semantic_media.get(key)
        if source is None:
            # The common compiler validates required versus optional slots.
            return None, None
        consumed.add(key)
        return source["token"], {**deepcopy(bindings[source["item_key"]]), "semantic_key": key}
    groups = media["classified_groups"] if media is not None else None
    if workflow["compiler_version"] == 3 and groups is None:
        groups = {"images": [], "videos": [], "audios": []}
    classified_bindings = [deepcopy(row) for row in bindings.values()
        if any(port["port"]["kind"] == "node_input" for port in row["ports"])]
    try:
        compiled = compile_frozen_workflow_values(workflow_snapshot=snapshot, scalar_values=values,
            resolve_asset=resolve_media, classified_groups=groups, classified_bindings=classified_bindings,
            output_kind=interface["output"]["contract"]["primary"]["kind"], total_duration_ms=total_duration_ms)
    except WorkflowCompilerError as exc:
        raise ValidationError({"workflow_request": str(exc)}) from exc
    if consumed != set(semantic_media):
        raise ValidationError("存在未被工作流消费的素材语义槽，不能静默丢弃输入")
    if (interface.get("resolution_strategy") or {}).get("mode") == "source_image_derived":
        validate_resolved_execution_media(constraints=interface["media_constraints"],
            semantic_keys={**interface["media_semantic_keys"], "width": "image.width", "height": "image.height"},
            values=compiled.resolved_semantics, output_kind=compiled.output_contract["primary"]["kind"])
    expected_output = deepcopy(interface["output"]["contract"])
    if "resolution" in compiled.output_contract:
        expected_output = {**expected_output, "schema_version": 2, "resolution": compiled.output_contract["resolution"]}
    if compiled.output_contract != expected_output:
        raise ValidationError("工作流请求的产出合同与冻结执行定义不一致")
    return {"schema_version": 1, "compiled_workflow": asdict(compiled), "media_preparation": media}
