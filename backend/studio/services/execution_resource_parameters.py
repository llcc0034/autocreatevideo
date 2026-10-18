"""System-owned text output allowance, frozen once in preparation facts.

Model definitions declare the system source, never a local allowance. Other
model/workflow parameters and physical constraints are never relaxed here.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_parameters import compile_execution_parameter_contract
from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy, validate_resource_policy
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import json_contract


def has_system_output(interface):
    from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_ADAPTER
    adapter = interface.get("output", {}).get("adapter")
    return isinstance(adapter, str) and adapter in {*NATIVE_TEXT_ADAPTERS.values(), "text.prompt_fields/v1", "text.prompt_draft/v1", STRUCTURED_TEXT_ADAPTER}


def output_policy_receipt(interface):
    if not has_system_output(interface):
        return None
    policy = read_resource_policy()
    return {"kind": "system_resource_output", "policy": policy.value, "policy_sha256": policy.sha256}


def validate_system_output_declaration(interface):
    if not has_system_output(interface):
        return
    if "max_output_tokens" in interface.get("native_policy", {}):
        raise ValidationError("模型不能配置文本输出额度，请使用新的系统资源配置合同")
    expected = compile_execution_parameter_contract({"max_tokens": [{
        "source": "system.execution.resources.review_policy",
        "rule": {"type": "integer", "required": True, "minimum": 1}}]})["max_tokens"]
    if interface["parameters"].get("max_tokens") != expected:
        raise ValidationError("文本输出长度只能由系统资源配置提供")


def with_system_output(interface, receipt):
    validate_system_output_declaration(interface)
    if (not has_system_output(interface) or not isinstance(receipt, dict)
            or set(receipt) != {"kind", "policy", "policy_sha256"} or receipt["kind"] != "system_resource_output"):
        raise ValidationError("输出额度缺少明确的系统配置来源")
    policy = receipt["policy"]
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "values", "source"}
            or policy["schema_version"] != 1 or policy["source"].get("scope") != "system"
            or not policy["source"].get("revision_id")
            or json_contract(policy, max_bytes=execution_configuration_max_bytes()).sha256 != receipt["policy_sha256"]):
        raise ValidationError("输出额度的系统配置快照不完整")
    validate_resource_policy(policy["values"])
    result = deepcopy(interface)
    result["parameters"]["max_tokens"] = compile_execution_parameter_contract({"max_tokens": [{
        "source": f"system.{RESOURCE_POLICY}@{policy['source']['revision_id']}",
        "rule": {"type": "integer", "required": True, "const": policy["values"]["output_length"]}}]})["max_tokens"]
    return result
