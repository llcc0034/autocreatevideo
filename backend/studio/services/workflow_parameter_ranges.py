"""Projector-authored numeric constraints, narrowed against the projected node contract."""
from copy import deepcopy
from decimal import Decimal, localcontext
import math


class ParameterRangeError(ValueError):
    pass


RANGE_FIELDS = {"min", "max", "multiple_of", "default", "alignment_modulus", "alignment_remainder"}


def is_multiple(value, step):
    left, right = Decimal(str(value)), Decimal(str(step))
    with localcontext() as context:
        context.prec = max(28, len(left.as_tuple().digits) + abs(left.adjusted() - right.adjusted()) + 4)
        return left % right == 0


def validate_parameter_range(value):
    if not isinstance(value, dict) or set(value) - RANGE_FIELDS:
        raise ParameterRangeError("参数范围字段无效")
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or abs(number) > 9007199254740991 or not math.isfinite(number):
            raise ParameterRangeError(f"参数范围 {key} 必须是 JSON 安全范围内的有限数值")
    if "min" in value and "max" in value and value["min"] > value["max"]:
        raise ParameterRangeError("参数范围最小值不能大于最大值")
    if "multiple_of" in value and value["multiple_of"] <= 0:
        raise ParameterRangeError("参数步长必须大于 0")
    if "alignment_modulus" in value or "alignment_remainder" in value:
        modulus, remainder = value.get("alignment_modulus"), value.get("alignment_remainder")
        if type(modulus) is not int or type(remainder) is not int or not 0 <= remainder < modulus:
            raise ParameterRangeError("整数对齐必须满足 0 <= 余数 < 模数")
    return value


def narrow_parameter_range(key, definition, constraints):
    from studio.services.workflow_semantics import _validate_value, WorkflowSemanticError

    validate_parameter_range(constraints)
    if not constraints:
        return deepcopy(definition)
    value_type = definition.get("type")
    if value_type not in {"integer", "number"}:
        raise ParameterRangeError(f"{key} 不是数值参数，不能配置数值范围")
    if value_type == "integer" and any(type(value) is not int for value in constraints.values()):
        raise ParameterRangeError(f"{key} 的范围、默认值和步长必须是整数")
    result = deepcopy(definition)
    for bound in ("min", "max"):
        if bound not in constraints:
            continue
        current, requested = definition.get(bound), constraints[bound]
        if current is not None and ((bound == "min" and requested < current) or (bound == "max" and requested > current)):
            raise ParameterRangeError(f"{key}.{bound} 不能放宽节点或引擎合同边界 {current}")
    if "multiple_of" in constraints and definition.get("multiple_of"):
        if not is_multiple(constraints["multiple_of"], definition["multiple_of"]):
            raise ParameterRangeError(f"{key} 的步长必须是节点步长的整数倍")
    if "alignment_modulus" in constraints and definition.get("alignment_modulus"):
        modulus = definition["alignment_modulus"]
        if constraints["alignment_modulus"] % modulus or constraints["alignment_remainder"] % modulus != definition["alignment_remainder"]:
            raise ParameterRangeError(f"{key} 不能放宽引擎整数对齐规则")
    result.update(constraints)
    if result.get("min") is not None and result.get("max") is not None and result["min"] > result["max"]:
        raise ParameterRangeError(f"{key} 与节点合同没有可用的数值交集")
    if "default" not in result:
        raise ParameterRangeError(f"{key} 请显式配置一个范围内的默认值")
    try:
        _validate_value(key, result["default"], result, value_type)
    except WorkflowSemanticError as exc:
        raise ParameterRangeError(f"{key} 默认值不满足范围，请调整默认值：{exc}") from exc
    return result


def apply_projector_parameter_ranges(contract, policies):
    """Apply atomically. Never leave a partially constrained contract on failure."""
    result = deepcopy(contract)
    for policy in policies:
        if not policy.get("constraints"):
            continue
        key = policy["semantic_key"]
        definition = result.get("parameters", {}).get(key)
        if not isinstance(definition, dict):
            raise ParameterRangeError(f"{key} 未投影为可配置参数")
        result["parameters"][key] = narrow_parameter_range(key, definition, policy["constraints"])
    return result


def parameter_range_preview(rows, contract, policies):
    """Keep diagnostics on the same projected contract as execution, retaining source evidence."""
    configured = {row["semantic_key"] for row in policies if row.get("constraints")}
    result = deepcopy(rows)
    for row in result:
        key = row.get("semantic_key")
        if key not in configured:
            continue
        definition = contract["parameters"][key]
        for field in RANGE_FIELDS:
            row[field] = definition.get(field)
        row["recommended_safe_value"] = definition["default"]
    return result
