"""Native business text protocols reused by the execution-instance path.

These are registered input/output adapters, not model-specific policies or an
alternate queue. Model limits and business prose still come from frozen config.
"""
from copy import deepcopy
import json

from django.core.exceptions import ValidationError

from studio.services.execution_request_storage import execution_request_max_bytes, request_part_contract
from studio.services.execution_input_catalog import execution_text_output_options
from studio.services.execution_parameters import compile_execution_parameter_contract
from studio.services.json_contracts import parse_json_object_text
from studio.services.script_generation_contract import validate_script_generation_model_profile
from studio.services.script_structure import validate_script_structure_model_profile, script_structure_planning_schema
from studio.services.security_boundaries import boundary_int
from studio.services.production_creation_inputs import CREATION_TEXT_FIELDS, CREATION_INTEGER_FIELDS
from studio.services.production_visual_rules import (validate_visual_rules_model_profile, build_visual_rules_plan,
    visual_rules_provider_payload, finalize_visual_rules)


NATIVE_TEXT_ADAPTERS = {"script.generate": "text.script_generate/v2", "script.structure": "text.script_structure/v3",
    "visual_rules.generate": "text.visual_rules/v2", "prompt.normalize": "text.prompt_normalize/v1"}
NATIVE_TEXT_OPERATIONS = tuple(NATIVE_TEXT_ADAPTERS)
_NATIVE_INPUTS = {
    "script.generate": {"idea": "text", "style_hint": "text", "episode_count": "json",
        "shots_per_episode": "json", "episode_duration_seconds": "json"},
    "script.structure": {"source_text": "text", "planning": "json"},
    "visual_rules.generate": {},
    "prompt.normalize": {"items": "json"},
}
_NATIVE_OPTIONAL_INPUTS = {"script.structure": frozenset({"planning"})}


def native_text_input_forms(adapter):
    """Presentation of the registered wire types; never edits a frozen definition."""
    if adapter != NATIVE_TEXT_ADAPTERS["script.generate"]:
        return {}
    forms = {key: {"label": label, "description": (
        "填写本次生成的要求；可先预览并引用项目内容，再按本轮需要修改。只提交你确认的内容。" if key in CREATION_TEXT_FIELDS else
        "填写正整数；单位见字段名称。实际集数、镜数及总量边界仍由服务端配置核验。"),
        "schema": {"type": "string"} if key in CREATION_TEXT_FIELDS else {"type": "integer", "minimum": 1}}
        for key, label in {**CREATION_TEXT_FIELDS, **CREATION_INTEGER_FIELDS}.items()}
    # Teaching examples are never runtime defaults or a model-specific route.
    forms["idea"].update(project_reference="project.logline")
    forms["idea"]["schema"]["examples"] = [
        "一名高中生发现同桌在放学后失踪，与好友沿着一封匿名信追查，逐渐发现失踪者主动隐瞒的秘密。请围绕人物目标、阻碍和关键转折展开，结尾留下悬念。"
    ]
    forms["style_hint"].update(project_reference="project.visual_style")
    forms["style_hint"]["schema"]["examples"] = [
        "校园悬疑，写实画面、自然光、克制的冷色调。叙事由平静逐步转为紧张，对白简短自然，线索通过可见动作呈现，避免夸张特效。"
    ]
    return forms


def _normalization_policy(profile):
    from studio.services.prompt_normalization import validate_prompt_normalization_model_profile
    return validate_prompt_normalization_model_profile(profile)


_NATIVE_POLICIES = {"script.generate": ("series_concept", validate_script_generation_model_profile),
    "script.structure": ("script_structure", validate_script_structure_model_profile),
    "visual_rules.generate": ("visual_rules", validate_visual_rules_model_profile),
    "prompt.normalize": ("prompt_normalization", _normalization_policy)}


def native_text_interface(version):
    if version.workflow_version_id or version.operation not in NATIVE_TEXT_OPERATIONS:
        raise ValidationError("没有对应的原生文本执行协议", code="execution_interface_unavailable")
    profile = version.deployment.profile_version
    policy_key, policy_reader = _NATIVE_POLICIES[version.operation]
    policy = policy_reader(profile)
    if policy is None:
        raise ValidationError("模型未声明精确的原生业务能力")
    business_output = None
    if version.operation in {"script.generate", "script.structure"}:
        from studio.services.execution_output_contracts import execution_output_reference
        business_output = execution_output_reference(version, required=True)
    if version.operation == "visual_rules.generate":
        # Model snapshots remain historical facts; new business suggestions
        # have exactly one owner: the explicitly selected output version.
        policy.pop("quality_advisories", None)
        if version.output_version is not None:
            from studio.services.business_outputs import output_reference
            business_output = output_reference(version.output_version, require_published=True)
            if business_output["quality_advisories"]:
                policy["quality_advisories"] = deepcopy(business_output["quality_advisories"])
    from studio.services.chat_output_options import resolve_chat_output_options
    resolve_chat_output_options(policy["chat_request_options"], business_output)
    source = f"model.{profile.pk}.{policy_key}"
    # Native output length is supplied only by the system policy at preparation
    # creation. A model, instance or recipe cannot declare an allowance.
    parameters = {
        "max_tokens": [{"source": "system.execution.resources.review_policy", "rule": {
            "type": "integer", "required": True, "minimum": 1}}],
        "seed": [{"source": source, "rule": {"type": "integer", "required": True}}],
    }
    return {
        "operation": version.operation, "model_identifier": profile.model_identifier,
        "input_targets": {key: {"target_kind": "operation_input", "target_key": key, "kind": kind,
            "required": key not in _NATIVE_OPTIONAL_INPUTS.get(version.operation, ()),
            "min_items": 0 if key in _NATIVE_OPTIONAL_INPUTS.get(version.operation, ()) else 1,
            "max_items": 1, "source": source,
            **({"value_schema": script_structure_planning_schema()} if version.operation == "script.structure" and key == "planning" else {}),
            }
            for key, kind in _NATIVE_INPUTS[version.operation].items()},
        "prompt_channels": {key: {"semantic_key": key, "rule": {"type": "string", "required": required}, "source": source}
            for key, required in (("system_instruction", False), ("positive_prompt", True), ("negative_prompt", False))},
        "prompt_not_required": False,
        "parameters": compile_execution_parameter_contract(parameters),
        "native_policy": deepcopy(policy),
        **({"business_output": business_output} if business_output is not None else {}),
        "output": {"adapter": NATIVE_TEXT_ADAPTERS[version.operation],
            "kind": "script" if version.operation in {"script.generate", "script.structure"} else "text",
            "schema_version": {"script.generate": 2,
                "script.structure": 3, "visual_rules.generate": 2, "prompt.normalize": 1}[version.operation]},
    }


def validate_native_text_inputs(operation, values):
    if operation not in NATIVE_TEXT_OPERATIONS or not isinstance(values, dict):
        raise ValidationError("必须按原生能力提供完整的业务输入")
    required = set(_NATIVE_INPUTS[operation]) - _NATIVE_OPTIONAL_INPUTS.get(operation, frozenset())
    if not required <= set(values) or set(values) - set(_NATIVE_INPUTS[operation]):
        raise ValidationError("必须按原生能力提供完整的业务输入")
    if operation == "prompt.normalize":
        from studio.services.execution_normalization_text import normalization_request
        normalization_request(values["items"])
        return
    if operation == "script.structure":
        if not isinstance(values["source_text"], str):
            raise ValidationError({"source_text": "必须为完整剧本文本"})
        if "planning" in values:
            from studio.services.execution_structure_mapping import validate_structure_value
            validate_structure_value(values["planning"], script_structure_planning_schema(), "planning")
        return
    if operation == "visual_rules.generate":
        return
    for key in CREATION_TEXT_FIELDS:
        if not isinstance(values[key], str):
            raise ValidationError({key: "必须为明确文本"})
    for key in CREATION_INTEGER_FIELDS:
        if type(values[key]) is not int or values[key] < 1:
            raise ValidationError({key: "必须为正整数"})
    if (values["episode_count"] > boundary_int("security.production.max_episodes")
            or values["shots_per_episode"] > boundary_int("security.production.max_shots_per_episode")
            or values["episode_count"] * values["shots_per_episode"] > boundary_int("security.production.max_total_shots")):
        raise ValidationError("剧本集数或镜数超过当前生产边界")


def native_text_source_reference(definition, resolved_inputs):
    """Use only provenance emitted by the shared authorized source resolver."""
    if definition["configuration"]["operation"] == "visual_rules.generate":
        from studio.services.visual_rules_inputs import validate_visual_rules_baseline, input_digest
        baseline = validate_visual_rules_baseline(resolved_inputs.get("visual_rules_baseline"))
        if (baseline["logical_inputs_sha256"] != input_digest(resolved_inputs.get("logical_values"))
                or baseline["input_provenance_sha256"] != input_digest(resolved_inputs.get("input_provenance"))):
            raise ValidationError("影视规则输入与冻结采用基线不一致")
        return baseline
    if definition["configuration"]["operation"] != "script.structure":
        return None
    keys = [row["key"] for row in definition["configuration"]["inputs"] if any(
        target["target_kind"] == "operation_input" and target["target_key"] == "source_text" for target in row["targets"])]
    provenance = resolved_inputs.get("input_provenance")
    if len(keys) != 1 or not isinstance(provenance, list):
        raise ValidationError("剧本结构化缺少唯一的输入来源证据")
    rows = [row for row in provenance if row["input_key"] == keys[0]]
    if len(rows) != 1:
        raise ValidationError("剧本结构化来源证据缺失或重复")
    if rows[0]["kind"] == "literal":
        return None
    row = rows[0]
    if row["kind"] == "selected_output":
        options = [option for option in execution_text_output_options() if option["output_key"] == row.get("output_key")
            and option["context_selector"] == "script.content"]
        if len(options) != 1 or not row.get("approval_id") or not row.get("decision_id"):
            raise ValidationError("已采用剧本缺少对应目录、审核或选择来源")
        row = row.get("context")
    if not isinstance(row, dict) or row.get("kind") != "context_field" or row.get("selector_key") != "script.content":
        raise ValidationError("该上下文不能冒充已确认剧本来源")
    return deepcopy(row["source_reference"])


def build_native_text_plan(interface, *, inputs, parameters, reference_max_bytes, source_reference=None):
    """Validate source facts early; the final request stores the same pure plan."""
    operation = interface["operation"]
    if interface["output"]["adapter"] != NATIVE_TEXT_ADAPTERS[operation]:
        raise ValidationError("此实例使用旧原生输入协议。请创建后继版本并重新配置输入，不会将旧来源自动转换。")
    validate_native_text_inputs(operation, inputs)
    policy = deepcopy(interface["native_policy"])
    if set(parameters) != {"max_tokens", "seed"} or type(parameters["max_tokens"]) is not int or parameters["max_tokens"] < 1:
        raise ValidationError("原生文本参数缺少已解析的正整数输出额度")
    # The caller verifies the mandatory system receipt before building this plan.
    if type(parameters["seed"]) is not int:
        raise ValidationError("原生文本调用需要已解析的整数种子")
    if operation in {"script.generate", "script.structure"}:
        from studio.services.script_semantic_requests import prepare_script_semantic_plan
        return prepare_script_semantic_plan(operation=operation, output_reference=interface.get("business_output"),
            inputs=inputs, parameters=parameters, transport_policy=policy,
            plan_max_bytes=reference_max_bytes, source_reference=source_reference)
    if operation == "prompt.normalize":
        if source_reference is not None:
            raise ValidationError("独立翻译不能冒用剧本修订来源")
        from studio.services.execution_normalization_text import normalization_request, normalization_payload
        plan = {"schema_version": 1, "request": normalization_request(inputs["items"]),
            "policy": deepcopy(policy), "output_length": parameters["max_tokens"], "seed": parameters["seed"]}
        # Validate capacity before allocating/dispatching the original task.
        normalization_payload(plan)
        return plan
    policy["max_output_tokens"] = parameters["max_tokens"]
    if operation == "visual_rules.generate":
        plan = build_visual_rules_plan(baseline=source_reference,
            model_identifier=interface["model_identifier"], policy=policy, seed=parameters["seed"])
        if "business_output" in interface:
            from studio.services.business_outputs import verify_output_reference
            plan["business_output"] = verify_output_reference(interface["business_output"])
        return plan
    raise ValidationError("没有对应的原生文本计划")


def build_native_text_request(interface, *, inputs, parameters, channels, profile_compilation, source_reference=None):
    maximum = execution_request_max_bytes()
    plan = build_native_text_plan(interface, inputs=inputs, parameters=parameters,
        reference_max_bytes=maximum, source_reference=source_reference)
    # Shared builder handles the strict wire schema. This is the actual manual
    # channel set or reviewed compilation, never an implicitly selected Binding.
    plan["prompt_profile_assembly"] = {
        "channels": deepcopy(channels),
        "structured_fields": deepcopy(profile_compilation.assembly["structured_fields"]) if profile_compilation else {},
    }
    plan = request_part_contract(plan, max_bytes=maximum, part="native_plan").value
    if interface["operation"] in {"script.generate", "script.structure"}:
        from studio.services.script_semantic_requests import compile_script_semantic_plan
        return compile_script_semantic_plan(operation=interface["operation"], plan=plan,
            plan_max_bytes=maximum, request_max_bytes=execution_request_max_bytes())
    from studio.services.execution_normalization_text import normalization_payload
    builder = {"visual_rules.generate": visual_rules_provider_payload,
        "prompt.normalize": normalization_payload}[interface["operation"]]
    system_layout = {}
    payload = builder(plan, system_layout=system_layout)
    plan["system_message_layout"] = system_layout
    return payload, {"operation": interface["operation"],
        "plan": request_part_contract(plan, max_bytes=maximum, part="native_plan").value}


def parse_native_text_response(operation, text, operation_plan, *, request_payload=None, expected_output=None):
    if (operation not in NATIVE_TEXT_OPERATIONS or not isinstance(operation_plan, dict)
            or set(operation_plan) != {"operation", "plan"} or operation_plan["operation"] != operation
            or not isinstance(operation_plan["plan"], dict)):
        raise ValidationError("原生文本结果缺少本次冻结业务合同")

    if operation in {"script.generate", "script.structure"}:
        if expected_output is None:
            raise ValidationError("剧本响应缺少原实例确切输出版本")
        from studio.services.execution_request_storage import execution_request_max_bytes
        from studio.services.script_semantic_requests import parse_script_semantic_response
        from studio.services.script_semantics import ScriptSemanticValidationError
        from studio.services.execution_structured_text import structured_text_warning_messages

        report = parse_script_semantic_response(response_text=text, operation_plan=operation_plan,
            request_payload=request_payload, request_max_bytes=execution_request_max_bytes(),
            response_max_bytes=boundary_int("security.provider.max_response_bytes"), expected_output=expected_output)
        if not report["valid"]:
            raise ScriptSemanticValidationError(report)
        # The original response/revision own the text. This in-memory marker
        # selects the exact-output writer, which rechecks the actual saved call.
        return {"result": report, "warnings": structured_text_warning_messages(report["warnings"]),
            "text": text, "output_contract": deepcopy(expected_output)}

    payload = parse_json_object_text(text, max_bytes=boundary_int("security.provider.max_response_bytes"))
    if operation == "prompt.normalize":
        from studio.services.execution_normalization_text import normalization_result
        return normalization_result(operation_plan["plan"], text)
    if operation == "visual_rules.generate":
        result = finalize_visual_rules(plan=operation_plan["plan"], payload=payload)
        return {"result": result, "warnings": result["warnings"],
            "text": json.dumps(result["candidate"], ensure_ascii=False, sort_keys=True)}
    raise ValidationError("没有对应的原生文本响应协议")
