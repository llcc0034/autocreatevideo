"""First-request parameter initialization, separate from read-only authoring.

The Preparation creator must call this only when allocating a new request and
persist the returned values in that same transaction. Existing preparations,
retries and form reads use their stored intent, never this initializer.
"""
from secrets import randbelow
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_parameters import (
    compile_execution_parameter_contract, execution_seed_domain, execution_seed_parameter_keys, resolve_execution_parameters,
)
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_media_parameters import validate_resolved_execution_media
from studio.services.execution_step_parameters import compose_execution_step_parameters
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


def _excluded_seed_indices(key, domain, values):
    if not isinstance(values, (list, tuple)) or any(type(value) is not int for value in values):
        raise ValidationError({f"parameters.{key}": "历史种子必须是明确整数列表"})
    if "values" in domain:
        excluded = set(values)
        indices = {i for i, value in enumerate(domain["values"]) if value in excluded}
    else:
        indices = {(value - domain["first"]) // domain["step"] for value in values
                   if (value - domain["first"]) % domain["step"] == 0
                   and 0 <= (value - domain["first"]) // domain["step"] < domain["count"]}
    if len(indices) == domain["count"]:
        raise ValidationError({f"parameters.{key}": "合法种子已被本实体候选用完，请调整参数合同后再生成"})
    return sorted(indices)


def initialize_execution_parameters(parameters, supplied, *, excluded_values=None):
    compiled = compile_execution_parameter_contract(parameters)
    domains = {
        key: execution_seed_domain(key, entry["rule"])
        for key, entry in compiled.items() if entry["rule"].get("seed_policy") == "generate_once"
    }
    # Validate the entire request before drawing any seed. These candidates
    # prove the admissible set; they are never returned as generated evidence.
    candidates = {key: row["values"][0] if "values" in row else row["first"] for key, row in domains.items()}
    resolve_execution_parameters(parameters, supplied, generated_values=candidates)
    exclusions = {} if excluded_values is None else excluded_values
    if not isinstance(exclusions, dict) or set(exclusions) - domains.keys():
        raise ValidationError({"parameters": "排除旧种子只能用于本次明确生成的种子参数"})
    excluded_indices = {key: _excluded_seed_indices(key, row, exclusions.get(key, [])) for key, row in domains.items()}
    generated = {}
    for key, row in domains.items():
        # Uniformly draw from the remaining indices without materializing a
        # uint64-sized domain or retrying indefinitely after collisions.
        index = randbelow(row["count"] - len(excluded_indices[key]))
        for excluded in excluded_indices[key]:
            if excluded > index:
                break
            index += 1
        generated[key] = row["values"][index] if "values" in row else row["first"] + index * row["step"]
    result = resolve_execution_parameters(parameters, supplied, generated_values=generated)
    result["generated_values"] = generated
    return result


def _definition_parameters(definition, policies, resolve_source):
    if not isinstance(definition, JsonContract):
        raise ValidationError({"parameters": "需要完整执行定义及摘要"})
    verify_json_contract(definition.value, schema_version=definition.schema_version, size_bytes=definition.size_bytes,
                         sha256=definition.sha256, max_bytes=execution_configuration_max_bytes())
    interface = definition.value.get("interface") if isinstance(definition.value, dict) else None
    if not isinstance(interface, dict) or not isinstance(interface.get("parameters"), dict):
        raise ValidationError({"parameters": "执行定义缺少参数合同"})
    from studio.services.execution_resource_parameters import has_system_output, validate_system_output_declaration
    validate_system_output_declaration(interface)
    if has_system_output(interface) and any(isinstance(row, dict) and row.get("key") == "max_tokens" for row in policies):
        raise ValidationError("步骤不能覆盖系统输出长度")
    parameters = interface["parameters"]
    if any(not isinstance(row, dict) or "layers" not in row for row in parameters.values()):
        raise ValidationError({"parameters": "冻结参数缺少来源层"})
    layers = {key: deepcopy(row["layers"]) for key, row in parameters.items()}
    if compile_execution_parameter_contract(layers) != parameters:
        raise ValidationError({"parameters": "参数摘要规则与冻结来源层不一致"})
    step = compose_execution_step_parameters(layers, policies, resolve_source=resolve_source)
    return interface, step, {key: row["layers"] for key, row in step["parameters"].items()}


def _validated_definition_result(interface, step, resolved):
    statics = interface.get("static_semantics", {})
    if not isinstance(statics, dict) or set(statics) & set(interface["parameters"]):
        raise ValidationError({"static_semantics": "静态事实不能与可调参数重名"})
    values = {**deepcopy(statics), **resolved["values"]}
    sources = {**{key: "workflow.static_semantics" for key in statics}, **resolved["sources"]}
    if "media_constraints" in interface:
        output = interface.get("output")
        if not isinstance(output, dict) or not isinstance(interface.get("media_semantic_keys"), dict):
            raise ValidationError({"parameters": "媒体定义缺少冻结输出合同"})
        # Each registered output adapter has one explicit shape; not a model
        # name fallback. Missing operands remain errors in the shared validator.
        if isinstance(output.get("adapter"), dict):
            contract = output.get("contract")
            primary = contract.get("primary") if isinstance(contract, dict) else None
            kind = primary.get("kind") if isinstance(primary, dict) else None
        else:
            kind = output.get("kind")
        if kind == "text":
            if interface["media_semantic_keys"] or interface["media_constraints"] != {"schema_version": 1, "resolution": {}, "fps": {"allowed": []}, "duration_ms": {}}:
                raise ValidationError({"parameters": "文本输出不能携带未消费的媒体约束"})
        else:
            constraints = interface["media_constraints"]
            if (interface.get("resolution_strategy") or {}).get("mode") == "source_image_derived":
                if {"image.width", "image.height"} & set(values):
                    raise ValidationError("来源推导尺寸不能被参数覆盖")
                # There is no size until the verified media-use facts reach
                # the shared graph compiler. That stage MUST check the full
                # frozen constraints before returning a prepared request.
                constraints = {**constraints, "resolution": {}}
            validate_resolved_execution_media(constraints=constraints,
                semantic_keys=interface["media_semantic_keys"], values=values, output_kind=kind)
    return json_contract({**resolved, "values": values, "sources": sources,
        "resolved_source_contracts": step["resolved_source_contracts"]}, max_bytes=execution_configuration_max_bytes()).value


@security_boundary_resolution_cache()
def resolve_execution_definition_parameters(definition, supplied, *, policies, resolve_source, generated_values):
    """Read/replay explicit step rules and persisted seeds through one resolver.

    No random draw, owner lookup or current definition selection. The source
    resolver must be the caller's authorized context/selection resolver.
    """
    interface, step, layers = _definition_parameters(definition, policies, resolve_source)
    result = resolve_execution_parameters(layers, supplied, generated_values=generated_values)
    result["generated_values"] = deepcopy(generated_values)
    return _validated_definition_result(interface, step, result)


@security_boundary_resolution_cache()
def initialize_execution_definition_parameters(definition, supplied, *, policies, resolve_source,
        new_candidate_seed_exclusions=None):
    """First preparation only: generate once, then persist with the same intent.

    Validate joint media constraints before drawing seeds, not just individual
    sliders. This helper alone is not idempotent; creation owns the owner/key
    transaction and must return stored preparations before calling it.
    """
    interface, step, layers = _definition_parameters(definition, policies, resolve_source)
    if new_candidate_seed_exclusions is not None:
        keys = execution_seed_parameter_keys(interface["parameters"],
            workflow=definition.value.get("target", {}).get("driver_type") == "comfyui")
        if not isinstance(new_candidate_seed_exclusions, dict) or set(new_candidate_seed_exclusions) != keys:
            raise ValidationError("新候选种子目录与执行合同不一致")
        for key in keys:
            if step["parameters"][key]["rule"].get("seed_policy") != "generate_once":
                layers[key].append(_new_candidate_seed_layer(key))
        # Existing fixed policies and const values remain hard errors.
        step["parameters"] = compile_execution_parameter_contract(layers)
    from studio.services.execution_resource_parameters import output_policy_receipt, with_system_output
    receipt = output_policy_receipt(interface)
    if receipt is not None:
        if "max_tokens" in supplied:
            raise ValidationError("输出长度统一从系统资源配置读取，不接收本次覆盖")
        interface = with_system_output(interface, receipt)
        layers["max_tokens"] = interface["parameters"]["max_tokens"]["layers"]
        step["parameters"]["max_tokens"] = interface["parameters"]["max_tokens"]
        step["resolved_source_contracts"]["max_tokens"] = receipt
    domains = {key: execution_seed_domain(key, row["rule"]) for key, row in step["parameters"].items()
               if row["rule"].get("seed_policy") == "generate_once"}
    candidates = {key: row["values"][0] if "values" in row else row["first"] for key, row in domains.items()}
    _validated_definition_result(interface, step, resolve_execution_parameters(layers, supplied, generated_values=candidates))
    return _validated_definition_result(interface, step, initialize_execution_parameters(layers, supplied,
        excluded_values=new_candidate_seed_exclusions))


def _new_candidate_seed_layer(key):
    return {"source": f"request.new_candidate_seed.{key}",
            "rule": {"type": "integer", "required": False, "seed_policy": "generate_once"}}


@security_boundary_resolution_cache()
def verify_prepared_execution_parameters(definition, prepared):
    """Recheck the persisted result without resolving a source or drawing again.

    The caller must first verify the Preparation detail digest. Step rules and
    selected-source identities here are that saved intent, not new user input.
    """
    interface, _, _ = _definition_parameters(definition, [], lambda _: None)
    expected = {"values", "sources", "contracts", "generated_values", "resolved_source_contracts"}
    if not isinstance(prepared, dict) or set(prepared) != expected or any(not isinstance(prepared[key], dict) for key in expected):
        raise ValidationError({"parameters": "准备参数缺少完整的值、来源、合同或实际种子"})
    receipt = prepared["resolved_source_contracts"].get("max_tokens")
    from studio.services.execution_resource_parameters import has_system_output
    if has_system_output(interface):
        from studio.services.execution_resource_parameters import with_system_output
        interface = with_system_output(interface, receipt)
    if set(prepared["contracts"]) != set(interface["parameters"]):
        raise ValidationError({"parameters": "准备参数目录与冻结实例不一致"})
    layers, source_keys = {}, set()
    if isinstance(receipt, dict) and receipt.get("kind") == "system_resource_output":
        source_keys.add("max_tokens")
    for key, entry in prepared["contracts"].items():
        base = interface["parameters"][key]["layers"]
        actual = entry.get("layers") if isinstance(entry, dict) else None
        if not isinstance(actual, list) or actual[:len(base)] != base or len(actual) > len(base) + 2:
            raise ValidationError({f"parameters.{key}": "准备参数不能删除、覆盖或伪造冻结来源层"})
        extras = actual[len(base):]
        if extras and extras[-1] == _new_candidate_seed_layer(key):
            seed_keys = execution_seed_parameter_keys(interface["parameters"],
                workflow=definition.value.get("target", {}).get("driver_type") == "comfyui")
            if key not in seed_keys:
                raise ValidationError({f"parameters.{key}": "新候选只能生成已声明的种子字段"})
            extras = extras[:-1]
        if len(extras) > 1:
            raise ValidationError({f"parameters.{key}": "准备包含重复或未知参数来源层"})
        if extras:
            extra = extras[0]
            rule = extra.get("rule") if isinstance(extra, dict) else None
            if (not isinstance(rule, dict) or set(extra) != {"source", "rule"}
                    or extra["source"] != f"step.parameter_policies.{key}"
                    or set(rule) not in ({"type", "required", "const"}, {"type", "required", "default"})
                    or rule["type"] != interface["parameters"][key]["rule"]["type"] or rule["required"] is not False):
                raise ValidationError({f"parameters.{key}": "准备中的步骤参数层不属于明确的固定值或默认值"})
            if "const" in rule:
                source_keys.add(key)
        layers[key] = actual
    if (not set(prepared["resolved_source_contracts"]) <= source_keys
            or any(not isinstance(value, dict) for value in prepared["resolved_source_contracts"].values())):
        raise ValidationError({"parameters": "准备中的固定来源身份与参数层不一致"})
    if set(prepared["values"]) != set(prepared["sources"]):
        raise ValidationError({"parameters": "准备参数值与来源目录不一致"})
    supplied = {key: value for key, value in prepared["values"].items() if prepared["sources"][key] == "request"}
    resolved = resolve_execution_parameters(layers, supplied, generated_values=prepared["generated_values"])
    resolved["generated_values"] = deepcopy(prepared["generated_values"])
    step = {"parameters": compile_execution_parameter_contract(layers), "resolved_source_contracts": prepared["resolved_source_contracts"]}
    verified = _validated_definition_result(interface, step, resolved)
    if verified != prepared:
        raise ValidationError({"parameters": "准备参数的值、来源、种子或摘要规则不一致"})
    return verified
