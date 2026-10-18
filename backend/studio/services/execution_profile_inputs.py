"""Exact Profile input projection; no Binding or owner lookup.

Direct source selectors share the existing typed field assembler. Reference
projections need the structured unit/reference adapter and are not accepted as
arbitrary JSON at this boundary. Neither path may infer a source from a name.
"""
from copy import deepcopy
import math

from django.core.exceptions import ValidationError
from studio.models import PromptProfileFieldRule

from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.prompt_profiles import PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION, verify_prompt_profile_contract, validate_field_source_selector
from studio.services.execution_reference_inputs import REFERENCE_CONTEXT_INPUT_KEY, reference_context_input_schema
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR, read_profile_path
from studio.services.prompt_generation_contexts import structured_context_path, read_structured_context


_VALUE_KINDS = {"text": "text", "long_text": "text", "enum": "text", "boolean": "json", "integer": "json", "decimal": "json", "list": "json"}
_VALUE_DOMAINS = {"text": "string", "long_text": "string", "enum": "string", "boolean": "boolean", "integer": "number", "decimal": "number", "list": "string_list"}

# This is a platform-owned source family, not arbitrary prefix interpretation.
# Reference assets and reference-unit facts retain their explicit media wiring.
_REQUEST_VALUE_SELECTORS = frozenset(
    member.value for member in PromptProfileFieldRule.SourceSelector
    if member.name.startswith("REQUEST_") and member.name != "REQUEST_REFERENCE"
)


def derived_profile_inputs(profile):
    """Canonical value ports from one exact published strategy.

    Request values and explicitly registered project-reader ports are distinct.
    Derivation itself reads no business content and grants no object access.
    """
    targets = profile_input_targets(profile)
    from studio.services.execution_input_catalog import EXECUTION_SCOPE_KINDS
    # The structured root is composable from registered field sources. Scope
    # membership here permits wiring, never reads project facts by itself.
    return [{
        "key": key, "position": position, "kind": target["kind"],
        "required": target["required"], "min_items": target["min_items"], "max_items": 1,
        "semantic_contract": {"schema_version": 1, "allowed_purposes": [],
            "allowed_subject_kinds": [], "allowed_scope_kinds": (list(EXECUTION_SCOPE_KINDS) if key == STRUCTURED_INPUT_SELECTOR
                else [target["source_reader"]["scope_kind"] if target.get("source_reader") else "request"]),
            "paired_with_input_key": None, "allow_repeated_asset": False},
        "targets": [{"target_kind": "profile_input", "target_key": key, "is_primary": False}],
        **({"data_structure_version_id": target["input_structure"]["id"]} if target.get("input_structure") else {}),
    } for position, (key, target) in enumerate(sorted(targets.items()))
        if (key in _REQUEST_VALUE_SELECTORS or target.get("source_reader")) and target["value_domain"] in {*_VALUE_DOMAINS.values(), "json"}]


def reconcile_profile_inputs(version, rows, *, cloning=False):
    """Single authoring writer for deterministic ports and physical choices.

    Mixed physical/profile mappings are genuine execution-combination choices;
    their source scope stays explicitly authored. Pure request ports are not a
    second editable source declaration. Cloning keeps history intact while
    deriving fresh ports for the successor instead of copying obsolete aliases.
    """
    if not isinstance(rows, list):
        raise ValidationError({"inputs": "输入配置必须是数组"})
    max_bytes = execution_configuration_max_bytes()
    json_contract(rows, max_bytes=max_bytes)
    generated = derived_profile_inputs(version.prompt_profile_version) if version.prompt_mode == "profile" else []
    by_key = {row["key"]: row for row in generated}
    physical, seen = set(), set()
    authored = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("targets"), list) or type(row.get("position")) is not int:
            raise ValidationError({"inputs": "输入必须声明完整目标列表"})
        mappings = row["targets"]
        if any(not isinstance(target, dict) or not isinstance(target.get("target_key"), str) for target in mappings):
            raise ValidationError({"inputs": "输入目标必须是对象，并声明字符串标识"})
        request_keys = {target.get("target_key") for target in mappings
            if target.get("target_kind") == "profile_input" and target.get("target_key") in by_key}
        if request_keys and not any(target.get("target_kind") != "profile_input" for target in mappings):
            if len(request_keys) != 1 or len(mappings) != 1:
                raise ValidationError({"inputs": "本次输入由策略逐来源派生，不能在实例合并多个来源"})
            key = next(iter(request_keys))
            expected = by_key[key]
            if row.get("data_structure_version_id") != expected.get("data_structure_version_id"):
                raise ValidationError({"inputs": "策略派生输入的结构由策略管理，不能在实例重复配置"})
            if key in seen or (not cloning and json_contract(
                    {k: v for k, v in row.items() if k not in {"position", "data_structure_version_id"}}, max_bytes=max_bytes).sha256 !=
                    json_contract({k: v for k, v in expected.items() if k not in {"position", "data_structure_version_id"}}, max_bytes=max_bytes).sha256):
                raise ValidationError({"inputs": f"{key} 由策略自动派生，不能在实例改写；请修改策略或删除重复声明"})
            seen.add(key)
            continue
        physical.update(request_keys)
        owned_structures = {by_key[key]["data_structure_version_id"] for key in request_keys
            if by_key[key].get("data_structure_version_id")}
        if len(owned_structures) > 1:
            raise ValidationError({"inputs": "同一输入不能连接结构版本不同的策略来源"})
        authored_row = deepcopy(row)
        if owned_structures:
            structure_id = next(iter(owned_structures))
            if row.get("data_structure_version_id") not in (None, structure_id):
                raise ValidationError({"inputs": "混合接线的输入结构由精确策略管理，不能另选结构版本"})
            authored_row["data_structure_version_id"] = structure_id
        authored.append(authored_row)
    # Existing physical port positions are user choices. Derived positions are
    # server-owned and allocated after them, never colliding with saved ports.
    if any(type(row.get("position")) is not int for row in authored):
        raise ValidationError({"inputs": "输入顺序必须是整数"})
    position = max((row["position"] for row in authored), default=-1) + 1
    for row in generated:
        if row["key"] in physical:
            continue
        if any(item.get("key") == row["key"] for item in authored):
            raise ValidationError({"inputs": f"输入标识 {row['key']} 与策略派生输入冲突，请明确调整物理槽位输入"})
        authored.append({**row, "position": position})
        position += 1
    return authored


def _add_source(targets, profile, row, *, key, kind, domain, required, default, usage, selector, enum=None):
    target = targets.setdefault(key, {
        "target_kind": "profile_input", "target_key": key, "kind": kind, "value_domain": domain,
        "required": False, "min_items": 0, "max_items": 1, "source": f"profile.{profile.pk}.rules", "fields": [], "enum": None,
    })
    if target["value_domain"] != domain:
        raise ValidationError({f"profile_inputs.{key}": "同一来源的字段声明了相互冲突的类型"})
    if enum is not None:
        target["enum"] = list(enum) if target["enum"] is None else [value for value in target["enum"] if value in enum]
        if not target["enum"]:
            raise ValidationError({f"profile_inputs.{key}": "同一来源的字段枚举没有交集"})
    target["required"] = target["required"] or required
    target["min_items"] = int(target["required"])
    target["fields"].append({"field_key": row["field_key"], "default": deepcopy(default),
        "required": row["required"] if usage == "assembly_source" else required,
        "usage": usage, "selector": selector})
    if row.get("source_definition") and usage in {"assembly_source", "generation_source"}:
        definitions = target.setdefault("source_definitions", [])
        if row["source_definition"] not in definitions:
            definitions.append(deepcopy(row["source_definition"]))


def _add_structure_source(targets, profile, row, contract, *, usage, selector, path):
    from studio.services.data_structures import validate_data_structure_snapshot
    structure = contract.get("input_structure")
    _add_source(targets, profile, row, key=STRUCTURED_INPUT_SELECTOR, kind="json", domain="json",
        required=True, default=None, usage=usage, selector=selector)
    target = targets[STRUCTURED_INPUT_SELECTOR]
    target["input_structure"] = structure
    target["value_schema"] = validate_data_structure_snapshot(structure)
    target["fields"][-1]["source_path"] = deepcopy(path)


def _add_final_generation_sources(targets, profile, row, contract):
    """A field's source and extra context are independent declared inputs."""
    key = row["generation_source_selector"]
    if key == STRUCTURED_INPUT_SELECTOR:
        _add_structure_source(targets, profile, row, contract, usage="generation_source", selector=key, path=row["source_path"])
    elif key == "projection.output":
        if row["source_selector"] != "projection.output" or contract["source_projection_key"] != "declarative_v1":
            raise ValidationError({"generation_source_selector": "装配原文必须来自本字段的声明式投影"})
    else:
        from studio.services.execution_source_readers import implemented_source_reader
        implemented_source_reader(key)
        _add_source(targets, profile, row, key=key, kind="text", domain="string", required=True,
            default=None, usage="generation_source", selector=key)
    for selector in row["generation_context_selectors"]:
        path = structured_context_path(selector)
        if path is not None:
            _add_structure_source(targets, profile, row, contract, usage="generation_context", selector=selector, path=path)
        else:
            _add_source(targets, profile, row, key=f"context.{selector}", kind="json", domain="json",
                required=True, default=None, usage="generation_context", selector=selector)


def profile_input_targets(profile):
    """Project declared sources, keeping renderer outputs server-owned."""
    contract = verify_prompt_profile_contract(profile)
    if contract["schema_version"] != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION:
        raise ValidationError({"prompt_profile_version": "执行实例只接受当前完整策略合同，不转换旧规则"})
    reference = contract["source_projection_key"] == "declarative_v1"
    if not reference and (contract["source_projection_key"] != "direct_v1" or contract.get("source_projection_config")):
        error = ValidationError({"prompt_profile_version": "该策略需要结构化参考事实适配，不能把投影输出当成可手填输入"})
        error.code = "execution_interface_unavailable"
        raise error
    targets = {}
    for row in contract["rules"]:
        validate_field_source_selector(row["source_selector"])
        generated = row["generation_mode"] in {"model_generated", "translate_only"}
        if generated and row["generation_output_semantics"] == "final":
            _add_final_generation_sources(targets, profile, row, contract)
            if row["source_selector"] != "projection.output":
                continue
        if row["source_selector"] == STRUCTURED_INPUT_SELECTOR:
            _add_structure_source(targets, profile, row, contract, usage="assembly_source",
                selector=STRUCTURED_INPUT_SELECTOR, path=row["source_path"])
            continue
        if reference and row["source_selector"] == "projection.output":
            from studio.services.source_projection_primitives import reference_profile_requires_units
            if not reference_profile_requires_units(contract):
                continue
            _add_source(targets, profile, row, key=REFERENCE_CONTEXT_INPUT_KEY, kind="json", domain="json",
                required=True, default=None, usage="reference_units", selector=REFERENCE_CONTEXT_INPUT_KEY)
            structured = any(item["generation_source_selector"] == STRUCTURED_INPUT_SELECTOR and item["generation_output_semantics"] == "contribution"
                             for item in contract["rules"])
            from studio.services.reference_rendering_inputs import validate_unit_text_bindings
            bindings = contract["source_projection_config"]["primitive_options"]["reference_context_ir_v1"]["rendering"]["unit_text_bindings"]
            validate_unit_text_bindings(bindings, structure=contract.get("input_structure"), check_structure=True)
            structured = structured or bool(bindings)
            targets[REFERENCE_CONTEXT_INPUT_KEY]["value_schema"] = reference_context_input_schema(
                input_structure=contract.get("input_structure") if structured else None)
            if row["generation_output_semantics"] != "contribution":
                continue
            if row["generation_source_selector"] == STRUCTURED_INPUT_SELECTOR:
                targets[REFERENCE_CONTEXT_INPUT_KEY]["fields"][-1]["source_path"] = deepcopy(row["source_path"])
            for selector in row["generation_context_selectors"]:
                if structured_context_path(selector) is not None:
                    continue  # resolved from this exact unit's structured_input
                _add_source(targets, profile, row, key=f"context.{selector}", kind="json", domain="unit_context",
                    required=True, default=None, usage="reference_generation_context", selector=selector)
            continue
        if row["generation_mode"] in {"model_generated", "translate_only"}:
            raise ValidationError({"prompt_profile_version": "贡献型字段需要声明式原语，不能当作直接最终字段"})
        if row["generation_mode"] not in {"server_locked", "passthrough"}:
            raise ValidationError({"prompt_profile_version": "未登记的字段生成方式"})
        key = row["source_selector"]
        if key == "fixed.value":
            continue
        field_type = row["field_value_type"]
        if field_type not in _VALUE_KINDS or key == "projection.output":
            raise ValidationError({"prompt_profile_version": "策略来源类型不能投影为直接输入"})
        required = row["required"] and not row["has_default"]
        _add_source(targets, profile, row, key=key, kind=_VALUE_KINDS[field_type], domain=_VALUE_DOMAINS[field_type],
                    required=required, default=row["default_value"] if row["has_default"] else None, usage="assembly_source", selector=key,
                    enum=row["field_value_options"] if field_type == "enum" else None)
    from studio.services.execution_project_sources import PROJECT_SOURCE_READERS, project_source_reader
    from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS, reviewed_visual_source_reader, validate_source_reader
    for key, target in targets.items():
        reader = None
        if target.get("source_definitions"):
            from studio.services.content_sources import verify_source_reference
            for reference in target["source_definitions"]:
                verify_source_reference(reference)
            target["label"] = " / ".join(dict.fromkeys(row["name"] for row in target["source_definitions"]))
            target["description"] = "；".join(f"{row['name']} v{row['version']}：{row['description']}" for row in target["source_definitions"])
            readers = [validate_source_reader(row.get("reader")) for row in target["source_definitions"]]
            reader = readers[0]
            if reader["selector"] != key or any(row != reader for row in readers):
                raise ValidationError({f"profile_inputs.{key}": "精确来源版本读取合同存在冲突"})
        elif key in PROJECT_SOURCE_READERS:
            reader = project_source_reader(key)
        elif key in VISUAL_RULE_SOURCE_FIELDS:
            reader = reviewed_visual_source_reader(key)
        if reader:
            if target["value_domain"] != "string":
                raise ValidationError({f"profile_inputs.{key}": "受控来源读取器只提供文字，策略字段类型不匹配"})
            target["source_reader"] = reader
            target["source_kinds"] = [{"project_snapshot/v1": "project_snapshot",
                "reviewed_visual_rules/v1": "context_field", "request_text/v1": "literal"}[reader["protocol"]]]
            if reader["protocol"] == "reviewed_visual_rules/v1":
                target["context_selectors"] = [key]
    return json_contract(targets, max_bytes=execution_configuration_max_bytes()).value


def validate_profile_input_targets(version, targets, *, require_complete):
    assigned = {}
    for spec in version.inputs.prefetch_related("targets"):
        for mapping in spec.targets.all():
            if mapping.target_kind != "profile_input":
                continue
            key = mapping.target_key
            target = targets.get(key)
            if target is None:
                raise ValidationError({f"inputs.{spec.key}": "所选策略没有声明此输入"})
            if key in assigned:
                raise ValidationError({f"inputs.{spec.key}": "多个逻辑输入不能争写同一个提示词来源"})
            if spec.kind != target["kind"] or spec.max_items != 1 or spec.min_items < target["min_items"] or (target["required"] and not spec.required):
                raise ValidationError({f"inputs.{spec.key}": "类型、单值数量或必填性与精确策略不一致"})
            if target.get("input_structure") and str(spec.data_structure_version_id) != target["input_structure"]["id"]:
                raise ValidationError({f"inputs.{spec.key}": "结构必须引用策略声明的确切版本"})
            if target.get("source_reader") and target["source_reader"]["scope_kind"] not in spec.semantic_contract["allowed_scope_kinds"]:
                raise ValidationError({f"inputs.{spec.key}": "输入范围必须与精确来源读取合同一致"})
            assigned[key] = spec.key
    if require_complete:
        missing = sorted(key for key, row in targets.items() if row["required"] and key not in assigned)
        if missing:
            raise ValidationError({"profile_inputs": "策略必需来源尚未接线：" + ", ".join(missing)})
    return targets


def _validate_source_value(target, value):
    if target.get("input_structure"):
        from studio.services.data_structures import validate_structure_value
        validate_structure_value(target["input_structure"], value, path=f"profile_inputs.{target['target_key']}")
    from studio.services.content_sources import validate_source_value
    for reference in target.get("source_definitions", []):
        validate_source_value(reference, value, path=f"profile_inputs.{target['target_key']}")
    domain = target["value_domain"]
    valid = {
        "string": lambda: isinstance(value, str),
        "boolean": lambda: type(value) is bool,
        "number": lambda: type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
        "string_list": lambda: isinstance(value, list) and all(isinstance(item, str) for item in value),
        "json": lambda: True,  # bounded canonical JSON validation follows
        "unit_context": lambda: isinstance(value, dict) and all(isinstance(key, str) and bool(key) for key in value),
    }
    if domain not in valid or not valid[domain]():
        raise ValidationError({f"profile_inputs.{target['target_key']}": "已解析来源类型与精确策略不一致"})
    json_contract(value, max_bytes=execution_request_max_bytes())
    if target["enum"] is not None and value not in target["enum"]:
        raise ValidationError({f"profile_inputs.{target['target_key']}": "已解析来源不在策略共同枚举内"})


@security_boundary_resolution_cache()
def project_execution_profile_sources(definition, resolved_inputs):
    """Pure projection of owner-authorized values through frozen logical wiring.

    resolved_inputs is {logical_input_key: value}, not a selector expression or
    a caller-authored rule. Asset/context/selected-output permission and source
    revisions belong to the preparation resolver. This projection never reads
    owners, applies a field default, selects text, or acquires approval.
    """
    if not isinstance(definition, JsonContract):
        raise ValidationError("需要完整执行定义与摘要")
    verify_json_contract(definition.value, schema_version=definition.schema_version, size_bytes=definition.size_bytes,
                         sha256=definition.sha256, max_bytes=execution_configuration_max_bytes())
    if not isinstance(resolved_inputs, dict) or any(not isinstance(key, str) or not key for key in resolved_inputs):
        raise ValidationError({"inputs": "已解析逻辑输入必须为对象"})
    json_contract(resolved_inputs, max_bytes=execution_resolved_inputs_max_bytes())
    payload = definition.value
    if not isinstance(payload, dict) or not isinstance(payload.get("configuration"), dict) or not isinstance(payload.get("interface"), dict):
        raise ValidationError("缺少完整执行定义")
    specs = payload["configuration"].get("inputs")
    targets = payload["interface"].get("profile_inputs", {})
    if not isinstance(specs, list) or not isinstance(targets, dict):
        raise ValidationError("执行定义缺少逻辑输入及策略接线")
    known_keys = {spec["key"] for spec in specs}
    unknown = set(resolved_inputs) - known_keys
    if unknown:
        raise ValidationError({"inputs": "当前实例未声明逻辑输入：" + ", ".join(sorted(unknown))})
    assigned, supplied = {}, {}
    for spec in specs:
        for mapping in spec["targets"]:
            if mapping["target_kind"] != "profile_input":
                continue
            key = mapping["target_key"]
            if key not in targets or key in assigned:
                raise ValidationError({"inputs": "策略目标缺失或被多个输入争写"})
            assigned[key] = spec["key"]
            if spec["key"] in resolved_inputs:
                _validate_source_value(targets[key], resolved_inputs[spec["key"]])
                supplied[key] = resolved_inputs[spec["key"]]
    missing = [key for key, target in targets.items() if target["required"] and key not in supplied]
    if missing:
        raise ValidationError({"inputs": "缺少已解析的策略来源：" + ", ".join(missing)})
    assembly_sources, generation_sources, generation_contexts, provenance = {}, {}, {}, []
    reference_values = {}
    for key, value in supplied.items():
        target = targets[key]
        uses = []
        for field in target["fields"]:
            selector, usage = field["selector"], field["usage"]
            if selector == "projection.output":
                raise ValidationError("不能从请求注入平台投影结果")
            if usage == "assembly_source":
                destination, destination_key = assembly_sources, selector
            elif usage == "generation_source":
                destination, destination_key = generation_sources, field["field_key"]
            elif usage == "generation_context":
                destination = generation_contexts.setdefault(field["field_key"], {})
                destination_key = selector
            elif usage == "reference_units":
                destination, destination_key = reference_values, "unit_input"
            elif usage == "reference_generation_context":
                destination = reference_values.setdefault("generation_contexts", {})
                destination_key = selector
            else:
                raise ValidationError("未登记的策略来源用途")
            selected_value = value
            if usage == "generation_context":
                if structured_context_path(selector) is not None:
                    selected_value = read_structured_context(value, selector, field["field_key"])
            if usage == "generation_source" and selector == STRUCTURED_INPUT_SELECTOR:
                selected_value, _, present = read_profile_path(value, field["source_path"], field["field_key"])
                if not present or not isinstance(selected_value, str):
                    raise ValidationError({f"fields.{field['field_key']}": "字段生成路径缺失或不是明确文本，不能使用规则默认值代替"})
            if destination_key in destination and destination[destination_key] != selected_value:
                raise ValidationError("不同逻辑输入向同一策略事实提供了冲突值")
            destination[destination_key] = deepcopy(selected_value)
            uses.append({"field_key": field["field_key"], "usage": usage, "selector": selector,
                         **({"source_path": deepcopy(field["source_path"])} if "source_path" in field else {})})
        provenance.append({"input_key": assigned[key], "target_key": key, "uses": uses,
                           "value_sha256": json_contract(value, max_bytes=execution_request_max_bytes()).sha256})
    return json_contract({
        "assembly_sources": assembly_sources, "generation_sources": generation_sources,
        "generation_contexts": generation_contexts, "provenance": provenance,
        "omitted_targets": [key for key in targets if key not in supplied],
        **({"reference": reference_values} if reference_values else {}),
    }, max_bytes=execution_request_max_bytes()).value
