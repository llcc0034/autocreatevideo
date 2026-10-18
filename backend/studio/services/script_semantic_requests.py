"""M2 request compiler and frozen reader over an exact output reference.

Neither path selects an instance, dispatches a call, or replaces a response
writer. Preparation reuses the Chat options validator; rule compilation and
frozen reads do not query current configuration. The original worker consumes
these checks and the original compiler/review path. Native instance selection
requires this output; model-policy authoring owns only transport settings.
No legacy script field contract participates in these messages.
"""
from copy import deepcopy
from hashlib import sha256
import math

from django.core.exceptions import ValidationError

from studio.services.business_outputs import verify_output_reference
from studio.services.data_structures import validate_data_structure_snapshot
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.native_prompt_payload import native_prompt_payload
from studio.services.execution_request_storage import request_part_contract
from studio.services.script_semantics import SEMANTICS_KEY, script_semantic_schema
from studio.services.script_semantic_relations import script_relation_rules
from studio.services.system_message_layout import assemble_system_message


SCRIPT_SEMANTIC_OPERATIONS = ("script.generate", "script.structure")
SCRIPT_SEMANTIC_SECTIONS = frozenset({"protocol", "output_structure", "semantic_mapping", "strategy"})


def script_semantic_output_rules(reference):
    """Compile meanings and paths from the same output used by M2 projection.

    The response schema is M1's schema, not the semantic consumer's schema.
    Keep their namespaces separate: a literal or lookup is a reader rule, not
    an instruction to add a new member to the model response. No inverse mapper
    or second schema compiler is needed, including for nested each/lookup.
    """
    reference = verify_output_reference(reference)
    if reference["parser"]["key"] != SEMANTICS_KEY:
        raise ValidationError({"output_version": "剧本规则需要确切的剧本语义映射输出版本"})
    return {
        "response_schema": validate_data_structure_snapshot(reference["data_structure"]),
        "consumer_semantics": {
            "schema": script_semantic_schema(),
            "projection": deepcopy(reference["parser"]["mapping"]),
            "relationships": script_relation_rules(),
        },
    }


def script_semantic_messages(*, operation, output_reference, inputs, prompt_profile_assembly,
                             max_bytes, system_layout=None):
    """Build unsent messages; the native caller owns input/transport validation.

    Inputs are already resolved business inputs, not another format to parse or
    infer. PromptProfile channels are the actual rendered channels, not field
    snapshots or a default strategy. The byte limit is the caller's existing
    request boundary; exceeding it never truncates user text or configured rules.
    """
    if operation not in SCRIPT_SEMANTIC_OPERATIONS or not isinstance(inputs, dict):
        raise ValidationError("需要明确的剧本能力和已解析输入")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValidationError("需要本次正式请求的字节边界")
    rules = script_semantic_output_rules(output_reference)
    profile_system, profile_payload = native_prompt_payload(prompt_profile_assembly, operation=operation)
    protocol = (
        "Return one JSON value matching RESPONSE_SCHEMA. Do not add Markdown fences or surrounding commentary. "
        "Property names, nesting, field meanings, types and configured use rules come from that schema. "
        "Its schema annotations are not additional response properties. "
        "CONSUMER_SEMANTICS describes how the platform reads your response, not a second output format. "
        "Its projection reads root from the response and item from the current each collection item. "
        "Literal values belong to the reader configuration; lookup uses the configured stable key. "
        "Do not emit the consumer schema or mapping envelope as extra response members. "
        "Fields not used by the projection still follow their configured response-schema meaning. "
        "No field name, entity name, translation or source passage creates an approval or a selected media file.\n"
    )
    parts = [
        ("protocol", protocol),
        ("output_structure", "RESPONSE_SCHEMA=" + canonical_json_bytes(rules["response_schema"]).decode("utf-8") + "\n"),
        ("semantic_mapping", "CONSUMER_SEMANTICS=" + canonical_json_bytes(rules["consumer_semantics"]).decode("utf-8") + "\n"),
    ]
    if profile_system:
        parts.append(("strategy", profile_system))
    # Build the receipt locally. A failed size/encoding check must not leave a
    # caller-visible success receipt for messages that were never returned.
    if system_layout is not None and (not isinstance(system_layout, dict) or system_layout):
        raise ValidationError("system分段记录必须为新的空对象")
    layout = {}
    user = {"operation": operation, "inputs": deepcopy(inputs), "prompt_profile": profile_payload}
    messages = [
        {"role": "system", "content": assemble_system_message(parts, layout=layout)},
        {"role": "user", "content": canonical_json_bytes(user).decode("utf-8")},
    ]
    messages = json_contract(messages, max_bytes=max_bytes).value
    if system_layout is not None:
        system_layout.update(layout)
    return messages


def _verify_plan_data(operation, plan, *, with_assembly=True):
    """Read only frozen facts. Model/config authoring is not rerun on reads."""
    if operation not in SCRIPT_SEMANTIC_OPERATIONS or not isinstance(plan, dict):
        raise ValidationError("剧本请求缺少确切能力和冻结计划")
    keys = {"schema_version", "business_output", "inputs", "parameters", "transport_policy",
        "source_kind", "source_revision"}
    if with_assembly:
        keys.add("prompt_profile_assembly")
    if (set(plan) != keys or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not isinstance(plan["inputs"], dict)):
        raise ValidationError("剧本请求计划版本或字段不一致")
    reference = verify_output_reference(plan["business_output"])
    if reference["parser"]["key"] != SEMANTICS_KEY:
        raise ValidationError("剧本请求必须使用确切的剧本语义映射输出版本")
    parameters, policy = plan["parameters"], plan["transport_policy"]
    if (not isinstance(parameters, dict) or set(parameters) != {"max_tokens", "seed"}
            or type(parameters["max_tokens"]) is not int or parameters["max_tokens"] < 1
            or type(parameters["seed"]) is not int):
        raise ValidationError("剧本请求需要已解析的正整数输出额度与整数种子")
    if (not isinstance(policy, dict) or set(policy) != {"timeout_seconds", "chat_request_options"}
            or type(policy["timeout_seconds"]) not in (int, float)
            or (type(policy["timeout_seconds"]) is float and not math.isfinite(policy["timeout_seconds"]))
            or policy["timeout_seconds"] <= 0
            or not isinstance(policy["chat_request_options"], dict)):
        raise ValidationError("剧本传输配置仅接受明确的超时和 Chat 选项，不接受旧业务字段合同")
    if with_assembly:
        native_prompt_payload(plan["prompt_profile_assembly"], operation=operation)
    if operation == "script.generate":
        if plan["source_kind"] is not None or plan["source_revision"] is not None:
            raise ValidationError("创意生成不能冒用已有剧本修订来源")
    else:
        from studio.services.script_sources import verify_script_text_source
        if plan["source_kind"] not in ("literal", "script_revision"):
            raise ValidationError("结构化请求必须明确原稿是直接输入还是已解析的剧本修订")
        verify_script_text_source(plan["inputs"].get("source_text"),
            source_reference=plan["source_revision"], source_kind=plan["source_kind"])
    return reference


def prepare_script_semantic_plan(*, operation, output_reference, inputs, parameters, transport_policy,
                                 plan_max_bytes, source_reference=None):
    """Validate resolved inputs before field preparation, without fake channels.

    Transport policy is a projection of the selected model's validated policy,
    not a new model configuration owner. Resolved input ports and output-token
    allowance are still validated by the original preparation caller. Model
    capabilities and candidate writers are cut over together, not by this helper.
    """
    from studio.services.model_profile_contracts import validate_chat_request_options

    if type(plan_max_bytes) is not int or plan_max_bytes < 1:
        raise ValidationError("需要明确的计划字节边界")
    source_kind, source = None, source_reference
    if operation == "script.structure":
        source_kind = "script_revision" if source_reference is not None else "literal"
        if source_reference is None:
            text = inputs.get("source_text") if isinstance(inputs, dict) else None
            if not isinstance(text, str):
                raise ValidationError("结构化请求必须提供完整原稿")
            try:
                encoded = text.encode("utf-8")
            except UnicodeError as exc:
                raise ValidationError("结构化原稿必须是有效 Unicode，不能替换后发送") from exc
            source = {"id": None, "source_id": None, "revision": None,
                "content_size_bytes": len(encoded), "content_sha256": sha256(encoded).hexdigest()}
    plan = {"schema_version": 1, "business_output": deepcopy(output_reference), "inputs": deepcopy(inputs),
        "parameters": deepcopy(parameters), "transport_policy": deepcopy(transport_policy),
        "source_kind": source_kind, "source_revision": deepcopy(source)}
    _verify_plan_data(operation, plan, with_assembly=False)
    # Validate provider-neutral options once at preparation, using the original
    # options owner. Frozen reads below never query today's configuration limits.
    plan["transport_policy"]["chat_request_options"] = validate_chat_request_options(
        plan["transport_policy"]["chat_request_options"])
    return request_part_contract(plan, max_bytes=plan_max_bytes, part="native_plan").value


def compile_script_semantic_plan(*, operation, plan, plan_max_bytes, request_max_bytes):
    """Compile the prepared plan after real manual/Profile channels are ready."""
    for limit in (plan_max_bytes, request_max_bytes):
        if type(limit) is not int or limit < 1:
            raise ValidationError("需要明确的计划和最终请求字节边界")
    plan = deepcopy(plan)
    _verify_plan_data(operation, plan)
    layout = {}
    messages = script_semantic_messages(operation=operation, output_reference=plan["business_output"],
        inputs=plan["inputs"], prompt_profile_assembly=plan["prompt_profile_assembly"],
        max_bytes=request_max_bytes, system_layout=layout)
    from studio.services.chat_output_options import resolve_chat_output_options
    payload = request_part_contract({"messages": messages, **deepcopy(plan["parameters"]), **deepcopy(plan["transport_policy"]),
        "chat_request_options": resolve_chat_output_options(plan["transport_policy"]["chat_request_options"], plan["business_output"])},
        max_bytes=request_max_bytes, part="provider_payload").value
    plan["system_message_layout"] = layout
    envelope = request_part_contract({"operation": operation, "plan": plan}, max_bytes=plan_max_bytes, part="native_plan").value
    return payload, envelope


def build_script_semantic_request(*, operation, output_reference, inputs, parameters, transport_policy,
                                  prompt_profile_assembly, plan_max_bytes, request_max_bytes,
                                  source_reference=None):
    """Unsent builder sharing preparation and compilation with native requests."""
    plan = prepare_script_semantic_plan(operation=operation, output_reference=output_reference,
        inputs=inputs, parameters=parameters, transport_policy=transport_policy,
        plan_max_bytes=plan_max_bytes, source_reference=source_reference)
    plan["prompt_profile_assembly"] = deepcopy(prompt_profile_assembly)
    return compile_script_semantic_plan(operation=operation, plan=plan,
        plan_max_bytes=plan_max_bytes, request_max_bytes=request_max_bytes)


def verify_script_semantic_request(envelope, payload, *, max_bytes, expected_output=None):
    """Verify saved request facts without rerendering prose or reading a registry.

    The enclosing request/definition readers verify their storage hashes and
    supply the expected output reference. This checks its native plan and wire
    projection, not source permission, candidate validity or provider receipt.
    """
    from studio.services.system_message_layout import inspect_system_message_layout

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValidationError("需要本次冻结请求的字节边界")
    if (not isinstance(envelope, dict) or set(envelope) != {"operation", "plan"}
            or not isinstance(envelope["plan"], dict) or "system_message_layout" not in envelope["plan"]):
        raise ValidationError("剧本请求缺少本次冻结的输出计划与组装记录")
    plan = deepcopy(envelope["plan"])
    layout = plan.pop("system_message_layout")
    reference = _verify_plan_data(envelope["operation"], plan)
    if expected_output is not None and reference != expected_output:
        raise ValidationError("剧本请求的输出计划与冻结实例定义不一致")
    from studio.services.chat_output_options import project_chat_output_options
    expected_transport = {**plan["transport_policy"], "chat_request_options": project_chat_output_options(
        plan["transport_policy"]["chat_request_options"], reference)}
    if (not isinstance(payload, dict) or set(payload) != {"messages", *plan["parameters"], *plan["transport_policy"]}
            or any(canonical_json_bytes(payload[key]) != canonical_json_bytes(value)
                for key, value in {**plan["parameters"], **expected_transport}.items())):
        raise ValidationError("剧本请求的传输参数与冻结计划不一致")
    json_contract(payload, max_bytes=max_bytes)
    try:
        trace = inspect_system_message_layout(layout, payload["messages"],
            allowed_sections=SCRIPT_SEMANTIC_SECTIONS,
            required_sections={"protocol", "output_structure", "semantic_mapping"})
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationError("剧本请求的 system 分段记录无效") from exc
    if trace["state"] != "matched":
        raise ValidationError("剧本请求正文与组装记录不一致")
    system, profile = native_prompt_payload(plan["prompt_profile_assembly"], operation=envelope["operation"])
    strategies = [row for row in trace["rows"] if row["key"] == "strategy"]
    if (bool(strategies) != bool(system) or (strategies and
            payload["messages"][0]["content"][strategies[0]["start"]:strategies[0]["end"]] != system)):
        raise ValidationError("剧本请求的系统策略与冻结通道不一致")
    expected_user = {"operation": envelope["operation"], "inputs": plan["inputs"], "prompt_profile": profile}
    if payload["messages"][1]["content"] != canonical_json_bytes(expected_user).decode("utf-8"):
        raise ValidationError("剧本请求的原稿、生成输入或策略通道与冻结计划不一致")
    return deepcopy(reference)


def read_script_semantic_attempt_output(attempt):
    """Bind a script result to the actual saved call, not a caller-supplied output.

    This reader does not admit the protocol or perform a provider call. It uses
    the same request/file/instance verification as the production text worker.
    """
    from studio.services.execution_preparations import read_production_attempt_request
    if attempt.operation not in SCRIPT_SEMANTIC_OPERATIONS or attempt.role != "primary":
        raise ValidationError("结构化剧本候选需要原剧本主调用")
    # The common frozen reader already checks the plan, body and preparation
    # together. Do not repeat that check or accept caller-supplied output facts.
    _, definition, _ = read_production_attempt_request(attempt)
    output = definition.value["interface"].get("business_output")
    if output is None:
        raise ValidationError("剧本原调用缺少确切结构化输出及原生请求计划")
    return deepcopy(output)


def verify_script_semantic_execution(definition, resolved_inputs, value, *, max_bytes):
    """One binding check for frozen production, asset and Smoke request readers."""
    from studio.services.chat_body import CHAT_DRIVERS

    operation, interface = definition["configuration"]["operation"], definition["interface"]
    envelope = value.get("operation_plan")
    if (operation not in SCRIPT_SEMANTIC_OPERATIONS
            or definition["target"]["workflow"] is not None or value["driver_type"] not in CHAT_DRIVERS
            or interface.get("business_output") is None or not isinstance(envelope, dict)
            or envelope.get("operation") != operation):
        raise ValidationError("剧本原调用缺少确切结构化输出及原生请求计划")
    output = verify_script_semantic_request(envelope, value["payload"],
        max_bytes=max_bytes, expected_output=interface["business_output"])
    plan = envelope["plan"]
    inputs = resolved_inputs.get("inputs", {})
    compilation = value["profile_compilation"]
    assembly = compilation.get("assembly") if isinstance(compilation, dict) else None
    if ((compilation is not None and not isinstance(assembly, dict))
            or (definition["configuration"]["prompt_mode"] == "profile") != (compilation is not None)):
        raise ValidationError("剧本实际提示词模式与冻结策略编译记录不一致")
    structured_fields = assembly.get("structured_fields") if isinstance(assembly, dict) else {}
    if (not isinstance(value["parameters"], dict) or not isinstance(value["prompt_channels"], dict)
            or not isinstance(inputs, dict)
            or plan["parameters"] != value["parameters"].get("values")
            or plan["inputs"] != inputs.get("operation_inputs")
            or canonical_json_bytes(plan["transport_policy"]) != canonical_json_bytes(interface.get("native_policy"))
            or plan["prompt_profile_assembly"] != {"channels": value["prompt_channels"].get("values"),
                "structured_fields": structured_fields}):
        raise ValidationError("剧本计划与原准备的输入、参数或实际策略通道不一致")
    if operation == "script.structure":
        from studio.services.execution_native_text import native_text_source_reference
        source = native_text_source_reference(definition, inputs)
        if ((source is None and plan["source_kind"] != "literal")
                or (source is not None and (plan["source_kind"] != "script_revision" or plan["source_revision"] != source))):
            raise ValidationError("结构化计划的原稿修订与实际授权输入来源不一致")
    return output


def parse_script_semantic_response(*, response_text, operation_plan, request_payload,
                                   request_max_bytes, response_max_bytes, expected_output):
    """Consume the same exact output used for the unsent/frozen request rules.

    Native parser dispatch converts this report to the original worker envelope;
    the request-bound candidate writer owns persistence. Protocol authoring and
    publication remain separate from response handling. Invalid responses retain
    their structure/relation diagnostics.
    """
    from studio.services.script_semantics import parse_script_semantic_output

    if expected_output is None:
        raise ValidationError("响应解析必须绑定冻结实例的确切输出版本")
    reference = verify_script_semantic_request(operation_plan, request_payload,
        max_bytes=request_max_bytes, expected_output=expected_output)
    return parse_script_semantic_output(reference=reference, response_text=response_text, max_bytes=response_max_bytes)
