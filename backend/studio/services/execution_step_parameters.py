"""Apply the plan's explicit step policies to the same scalar contract.

Source resolution belongs to the authorized business-input resolver supplied
by the caller. This module neither traverses JSONPath nor reads a latest output
or a project. It does not initialize seeds or persist a second parameter state.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_parameters import compile_execution_parameter_contract
from studio.services.json_contracts import json_contract


def validate_execution_step_parameter_policies(parameters, policies):
    """Validate authored policies without resolving a future source or seed."""
    compiled = compile_execution_parameter_contract(parameters)
    if not isinstance(policies, list):
        raise ValidationError({"parameter_policies": "步骤参数策略必须是数组"})
    json_contract(policies, max_bytes=execution_configuration_max_bytes())
    seen = set()
    for row in policies:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] not in compiled:
            raise ValidationError({"parameter_policies": "步骤只能配置实例已声明的参数"})
        key, policy = row["key"], row.get("policy")
        if key in seen:
            raise ValidationError({"parameter_policies": f"步骤参数 {key} 重复"})
        seen.add(key)
        if policy not in ("default", "locked_value", "locked_source"):
            raise ValidationError({f"parameter_policies.{key}": "必须明确选择默认值、固定值或固定来源"})
        if compiled[key]["rule"].get("seed_policy") == "generate_once":
            raise ValidationError({f"parameter_policies.{key}": "自动种子不能再配置步骤默认值或固定来源"})
        expected = {"key", "policy", "source" if policy == "locked_source" else "value"}
        if set(row) != expected:
            raise ValidationError({f"parameter_policies.{key}": "值与来源必须互斥且完整，不接受额外字段"})
        if policy == "locked_source" and not isinstance(row["source"], dict):
            raise ValidationError({f"parameter_policies.{key}": "固定来源必须是明确的来源合同，不接受路径字符串"})
    # Static values must already obey all layers at authoring time. A future
    # source is checked against these same layers when its real value exists.
    static_layers = deepcopy(parameters)
    for row in policies:
        if row["policy"] != "locked_source":
            key = row["key"]
            static_layers[key].append({"source": f"step.parameter_policies.{key}", "rule": {
                "type": compiled[key]["rule"]["type"], "required": False,
                "default" if row["policy"] == "default" else "const": deepcopy(row["value"]),
            }})
    compile_execution_parameter_contract(static_layers)
    return compiled


def compose_execution_step_parameters(parameters, policies, *, resolve_source):
    compiled = validate_execution_step_parameter_policies(parameters, policies)
    layers = deepcopy(parameters)
    sources = {}
    for row in policies:
        key, policy = row["key"], row["policy"]
        if policy == "locked_source":
            # The one business resolver must validate ownership, exact source
            # revisions, selector registration and scalar cardinality first.
            value = resolve_source(deepcopy(row["source"]))
            sources[key] = deepcopy(row["source"])
        else:
            value = row["value"]
        layers[key].append({"source": f"step.parameter_policies.{key}", "rule": {
            "type": compiled[key]["rule"]["type"], "required": False,
            "default" if policy == "default" else "const": deepcopy(value),
        }})
    return {"parameters": compile_execution_parameter_contract(layers), "resolved_source_contracts": sources}
