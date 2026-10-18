"""Pure scalar-parameter contracts shared by instance authoring and preparation.

Adapters supply explicit, named layers in low-to-high default precedence. This
module never reads model names, chooses a resource, generates seeds or writes a
snapshot. Every layer remains in the resulting contract; alignment constraints
are intersected exactly rather than overwritten by the largest step.
"""
from copy import deepcopy
from fractions import Fraction
from math import gcd, lcm

from django.core.exceptions import ValidationError

from studio.services.execution_instances import (
    _valid_scalar, execution_configuration_max_bytes,
)
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


_TYPES = {"integer", "number", "string", "boolean"}
_FIELDS = {
    "type", "required", "minimum", "maximum", "enum", "alignments",
    "min_length", "max_length", "default", "const", "seed_policy",
}
_ABSENT = object()


def integer_parameter_editor_options():
    """Authoring metadata for the integer branch of this scalar contract."""
    return {
        "value_fields": [
            {"value": key, "label": label} for key, label in (
                ("minimum", "最小值"), ("maximum", "最大值"),
                ("default", "默认值（可选）"), ("const", "固定值（可选）"),
            )
        ],
        "seed_policies": [
            {"value": "fixed", "label": "固定／人工提供"},
            {"value": "generate_once", "label": "首次准备时生成一次"},
        ],
    }


def _error(path, message):
    raise ValidationError({path: message})


def _equal(left, right):
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return left == right


def _contains(options, value):
    return any(_equal(item, value) for item in options)


def _check_value(path, value, rule):
    if not _valid_scalar(value, rule["type"]):
        _error(path, f"必须符合 {rule['type']} 类型；布尔值不是数字")
    if "const" in rule and not _equal(value, rule["const"]):
        _error(path, "与固定值冲突")
    if "enum" in rule and not _contains(rule["enum"], value):
        _error(path, "不在枚举范围内")
    if rule["type"] in {"integer", "number"}:
        number = Fraction(str(value))
        if "minimum" in rule and number < Fraction(str(rule["minimum"])):
            _error(path, "小于最小值")
        if "maximum" in rule and number > Fraction(str(rule["maximum"])):
            _error(path, "大于最大值")
        for alignment in rule.get("alignments", []):
            if (number - Fraction(str(alignment["remainder"]))) % Fraction(str(alignment["modulus"])):
                _error(path, "不满足步长/余数对齐")
    if rule["type"] == "string":
        if "min_length" in rule and len(value) < rule["min_length"]:
            _error(path, "短于最小长度")
        if "max_length" in rule and len(value) > rule["max_length"]:
            _error(path, "超过最大长度")


def _check_rule(path, rule):
    if not isinstance(rule, dict) or set(rule) - _FIELDS or not isinstance(rule.get("type"), str) or rule["type"] not in _TYPES:
        _error(path, "参数规则必须明确类型且只使用受支持字段")
    if type(rule.get("required")) is not bool:
        _error(path, "必须明确声明 required")
    if "seed_policy" in rule:
        if rule["type"] != "integer" or rule["seed_policy"] not in ("fixed", "generate_once"):
            _error(path, "种子策略必须是整数参数的 fixed 或 generate_once")
        if rule["seed_policy"] == "generate_once" and ("default" in rule or "const" in rule):
            _error(path, "生成种子的参数层不能同时声明默认值或固定值")
    for key in ("minimum", "maximum"):
        if key in rule and (rule["type"] not in {"integer", "number"} or not _valid_scalar(rule[key], rule["type"])):
            _error(f"{path}.{key}", "数值边界与参数类型不符")
    if "minimum" in rule and "maximum" in rule and rule["minimum"] > rule["maximum"]:
        _error(path, "最小值大于最大值")
    for key in ("min_length", "max_length"):
        if key in rule and (rule["type"] != "string" or type(rule[key]) is not int or rule[key] < 0):
            _error(f"{path}.{key}", "长度必须是字符串参数的非负整数")
    if "min_length" in rule and "max_length" in rule and rule["min_length"] > rule["max_length"]:
        _error(path, "最小长度大于最大长度")
    alignments = rule.get("alignments", [])
    if not isinstance(alignments, list) or (alignments and rule["type"] not in {"integer", "number"}):
        _error(path, "只有数值参数可以声明对齐规则")
    for item in alignments:
        if (
            not isinstance(item, dict) or set(item) != {"modulus", "remainder"}
            or any(not _valid_scalar(item[key], rule["type"]) for key in item)
            or not 0 <= item["remainder"] < item["modulus"]
        ):
            _error(path, "对齐规则必须满足 0 <= remainder < modulus，且类型一致")
    if "enum" in rule:
        values = rule["enum"]
        if not isinstance(values, list) or not values:
            _error(path, "显式枚举不能为空；不限制枚举时省略 enum")
        for index, value in enumerate(values):
            if _contains(values[:index], value):
                _error(path, "枚举值重复")
            _check_value(path, value, {key: item for key, item in rule.items() if key not in {"enum", "const"}})
    for key in ("default", "const"):
        if key in rule:
            _check_value(f"{path}.{key}", rule[key], rule)


def _numeric_lattice(path, rule):
    """Return the exact shared lattice in scaled integer coordinates."""
    alignments = list(rule.get("alignments", []))
    if rule["type"] == "integer":
        alignments.append({"modulus": 1, "remainder": 0})
    if not alignments:
        return None
    pairs = [(Fraction(str(row["modulus"])), Fraction(str(row["remainder"]))) for row in alignments]
    scale = lcm(*(number.denominator for pair in pairs for number in pair))
    modulus, remainder = 1, 0
    for step, offset in pairs:
        next_modulus, next_remainder = int(step * scale), int(offset * scale)
        common = gcd(modulus, next_modulus)
        delta = next_remainder - remainder
        if delta % common:
            _error(path, "多组步长/余数没有共同解")
        reduced = next_modulus // common
        factor = (delta // common * pow(modulus // common, -1, reduced)) % reduced if reduced > 1 else 0
        remainder += modulus * factor
        modulus = lcm(modulus, next_modulus)
        remainder %= modulus
    return scale, modulus, remainder


def _numeric_intersection(path, rule):
    """Prove a non-empty rational/integer domain using generalized CRT."""
    lattice = _numeric_lattice(path, rule)
    if lattice is None:
        return
    scale, modulus, remainder = lattice
    if "minimum" in rule and "maximum" in rule:
        lower = Fraction(str(rule["minimum"])) * scale
        upper = Fraction(str(rule["maximum"])) * scale
        delta = (lower - remainder) / modulus
        first = remainder + (-(-delta.numerator // delta.denominator)) * modulus
        if first > upper:
            _error(path, "当前数值范围内没有满足全部对齐规则的值")


@security_boundary_resolution_cache()
def compile_execution_parameter_contract(parameters):
    """Validate explicit scalar layers and retain source/precedence information.

    Input: {key: [{source: exact_path, rule: {...}}, ...]}. No implicit layer
    order is guessed. All rules are hard restrictions; only defaults use order.
    """
    if not isinstance(parameters, dict):
        _error("parameters", "参数合同必须是对象")
    json_contract(parameters, max_bytes=execution_configuration_max_bytes())
    result = {}
    for key, layers in parameters.items():
        if not isinstance(key, str) or not key or not isinstance(layers, list) or not layers:
            _error("parameters", "每个参数必须有名称和至少一个明确来源")
        sources, rules, constants = set(), [], []
        for layer in layers:
            if not isinstance(layer, dict) or set(layer) != {"source", "rule"}:
                _error(key, "参数层必须包含且仅包含 source、rule")
            source = layer["source"]
            if not isinstance(source, str) or not source.strip() or source in sources:
                _error(key, "参数来源必须非空且不可重复")
            sources.add(source)
            rule = layer["rule"]
            _check_rule(f"{source}.{key}", rule)
            rules.append(rule)
            if "const" in rule:
                constants.append((source, rule["const"]))
        kinds = {row["type"] for row in rules}
        if len(kinds) != 1 and kinds != {"integer", "number"}:
            _error(key, "各层参数类型没有交集")
        kind = "integer" if "integer" in kinds else rules[0]["type"]
        merged = {"type": kind, "required": any(row["required"] for row in rules)}
        for name, reducer in (("minimum", max), ("maximum", min), ("min_length", max), ("max_length", min)):
            values = [row[name] for row in rules if name in row]
            if values:
                merged[name] = reducer(values)
        if kind == "integer":
            for name in ("minimum", "maximum"):
                if name in merged:
                    number = Fraction(str(merged[name]))
                    merged[name] = -(-number.numerator // number.denominator) if name == "minimum" else number.numerator // number.denominator
        for lower, upper in (("minimum", "maximum"), ("min_length", "max_length")):
            if lower in merged and upper in merged and merged[lower] > merged[upper]:
                _error(key, "各层参数范围没有交集")
        alignments = [deepcopy(item) for row in rules for item in row.get("alignments", [])]
        if alignments:
            merged["alignments"] = alignments
        enums = [row["enum"] for row in rules if "enum" in row]
        if enums:
            options = []
            for item in enums[0]:
                if not all(_contains(values, item) for values in enums[1:]):
                    continue
                try:
                    _check_value(key, item, merged)
                except ValidationError:
                    continue
                options.append(item)
            if not options:
                _error(key, "枚举与其他约束没有交集")
            merged["enum"] = options
        elif kind in {"integer", "number"}:
            _numeric_intersection(key, merged)
        seed_policies = {rule["seed_policy"] for rule in rules if "seed_policy" in rule}
        if len(seed_policies) > 1:
            _error(key, "种子策略冲突，不能覆盖已有策略")
        seed_policy = next(iter(seed_policies), None)
        if seed_policy is not None:
            merged["seed_policy"] = seed_policy
        if seed_policy == "generate_once":
            generation_at = next(index for index, rule in enumerate(rules) if rule.get("seed_policy") == "generate_once")
            if any("default" in rule for rule in rules[generation_at + 1:]):
                _error(key, "自动种子不能同时设置后续步骤默认值")
        if constants:
            if seed_policy == "generate_once":
                _error(key, "底层固定种子不能改为每次生成")
            source, value = constants[0]
            if any(not _equal(value, other) for _, other in constants[1:]):
                _error(key, "不同来源的固定值冲突，不允许覆盖")
            _check_value(key, value, merged)
            merged["const"] = value
            origin = source
        else:
            defaults = [(layer["source"], layer["rule"]["default"]) for layer in layers if "default" in layer["rule"]]
            origin = None
            if defaults and seed_policy != "generate_once":
                origin, value = defaults[-1]
                _check_value(key, value, merged)
                merged["default"] = value
        if seed_policy == "generate_once":
            # Generation replaces lower-layer defaults, never hard rules.
            execution_seed_domain(key, merged)
        result[key] = {"rule": merged, "value_source": origin, "layers": deepcopy(layers)}
    json_contract(result, max_bytes=execution_configuration_max_bytes())
    return result


def execution_seed_parameter_keys(parameters, *, workflow):
    """Registered protocol seed role plus explicitly authored seed policies."""
    canonical = "sampling.seed" if workflow else "seed"
    return {key for key, row in parameters.items()
            if key == canonical or row["rule"].get("seed_policy") in {"fixed", "generate_once"}}


def execution_seed_domain(key, rule):
    """Describe a finite admissible integer set without drawing randomness."""
    if rule["type"] != "integer":
        _error(key, "自动种子必须是整数")
    if "enum" in rule:
        return {"values": deepcopy(rule["enum"]), "count": len(rule["enum"])}
    if "minimum" not in rule or "maximum" not in rule:
        _error(key, "生成种子必须明确有限范围或枚举，不注入默认上限")
    scale, modulus, remainder = _numeric_lattice(key, rule)
    lower, upper = rule["minimum"] * scale, rule["maximum"] * scale
    first = remainder + (-(-(lower - remainder) // modulus)) * modulus
    if first > upper:
        _error(key, "种子范围内没有满足全部对齐的整数")
    return {"first": first // scale, "step": modulus // scale, "count": (upper - first) // modulus + 1}


def resolve_execution_parameters(parameters, supplied, *, generated_values=None):
    """Resolve declared values and previously drawn seeds; never draw again."""
    if not isinstance(supplied, dict):
        _error("parameters", "本次参数必须是对象")
    if any(not isinstance(key, str) for key in supplied):
        _error("parameters", "参数名称必须是字符串")
    compiled = compile_execution_parameter_contract(parameters)
    unknown = set(supplied) - set(compiled)
    if unknown:
        _error("parameters", "含未声明参数: " + ", ".join(sorted(unknown)))
    generated_keys = {key for key, row in compiled.items() if row["rule"].get("seed_policy") == "generate_once"}
    if generated_values is None:
        generated_values = {}
    if not isinstance(generated_values, dict) or set(generated_values) != generated_keys:
        _error("parameters", "必须提供首次准备时生成的全部种子；读取或重试不能重新生成")
    if generated_keys & supplied.keys():
        _error("parameters", "自动种子不能同时提交手动值；需要手动指定时请选择 fixed 策略")
    values, origins = {}, {}
    for key, entry in compiled.items():
        rule = entry["rule"]
        value = supplied.get(key, _ABSENT)
        origin = "request"
        if key in generated_keys:
            value = generated_values[key]
            origin = "preparation.generated"
        if value is _ABSENT:
            value = rule.get("const", rule.get("default", _ABSENT))
            origin = entry["value_source"]
        if value is _ABSENT:
            if rule["required"]:
                _error(key, "缺少必需参数，未声明默认值或固定值")
            continue
        _check_value(key, value, rule)
        values[key], origins[key] = deepcopy(value), origin
    json_contract(values, max_bytes=execution_configuration_max_bytes())
    return {"values": values, "sources": origins, "contracts": compiled}


def instance_parameter_rule(spec):
    """Translate the one authoritative authoring row, not a second saved schema."""
    from studio.services.execution_instances import validate_execution_parameter_spec

    validate_execution_parameter_spec(spec)
    rule = {"type": spec.value_type, "required": False}
    if spec.seed_policy is not None:
        rule["seed_policy"] = spec.seed_policy
    for name in ("minimum", "maximum"):
        if getattr(spec, name) is not None:
            rule[name] = getattr(spec, name)
    if spec.enum_values:
        rule["enum"] = deepcopy(spec.enum_values)
    if spec.step is not None:
        rule["alignments"] = [{"modulus": spec.step, "remainder": spec.remainder if spec.remainder is not None else 0}]
    if spec.default_value is not None:
        rule["default"] = spec.default_value
    if spec.policy == "locked":
        rule["const"] = spec.locked_value
    return rule


def workflow_parameter_rule(key, definition, *, required):
    """Adapt existing frozen scalar semantics; never expose assets/model files."""
    if not isinstance(definition, dict):
        _error(key, "工作流未声明此参数")
    kind = definition.get("type")
    if kind == "enum":
        options = definition.get("options")
        if not isinstance(options, list) or not options:
            _error(key, "工作流枚举缺少选项")
        kinds = {next((name for name in ("boolean", "integer", "number", "string") if _valid_scalar(value, name)), None) for value in options}
        if None in kinds:
            _error(key, "工作流枚举只能包含有限标量选项")
        if len(kinds) > 1 and not kinds <= {"integer", "number"} and "const" in definition:
            # A declared constant narrows the domain, not merely the default.
            # Keep its JSON type and reject a constant absent from the source enum.
            value = definition["const"]
            if not _contains(options, value):
                _error(key, "工作流固定值不在原始枚举范围内")
            definition = {**definition, "options": [deepcopy(value)]}
            kinds = {next(name for name in ("boolean", "integer", "number", "string") if _valid_scalar(value, name))}
        if kinds <= {"integer", "number"}:
            kind = "number" if "number" in kinds else "integer"
        elif len(kinds) == 1:
            kind = next(iter(kinds))
        else:
            _error(key, "混合类型的工作流枚举不能作为有类型的实例参数")
    if not isinstance(kind, str) or kind not in _TYPES:
        _error(key, "此工作流输入不是可配置的标量参数；模型文件和素材应走各自合同")
    rule = {"type": kind, "required": required}
    for source, target in (("min", "minimum"), ("max", "maximum"), ("options", "enum"),
                           ("min_length", "min_length"), ("max_length", "max_length"),
                           ("default", "default"), ("const", "const")):
        if source in definition:
            # The current Workflow contract represents an absent bound as null.
            # This is not a numeric zero, and it must not become a constraint.
            if source in {"min", "max", "options", "min_length", "max_length"} and definition[source] is None:
                continue
            rule[target] = deepcopy(definition[source])
    alignments = []
    if "multiple_of" in definition:
        alignments.append({"modulus": definition["multiple_of"], "remainder": 0})
    if "alignment_modulus" in definition or "alignment_remainder" in definition:
        alignments.append({"modulus": definition.get("alignment_modulus"), "remainder": definition.get("alignment_remainder")})
    if alignments:
        rule["alignments"] = alignments
    _check_rule(key, rule)
    return rule


def validate_workflow_execution_parameters(version):
    """Check all exact Workflow/model scalar layers, then instance restrictions."""
    from studio.services.execution_workflow_contracts import workflow_execution_interface

    interface = workflow_execution_interface(version)
    return restrict_execution_parameters(
        interface["parameters"], version.parameters.all(), source=f"instance.{version.pk}.parameters",
    )


@security_boundary_resolution_cache()
def restrict_execution_parameters(base_parameters, specs, *, source):
    """One restriction path for Workflow and API, with unchanged base entries."""
    layers = {key: deepcopy(row["layers"]) for key, row in base_parameters.items()}
    seen = set()
    for spec in specs:
        if spec.key not in layers:
            _error(f"parameters.{spec.key}", "不是该执行目标的可配置参数；模型、素材、提示词及服务端输入不能作为普通参数覆盖")
        if spec.key in seen:
            _error(f"parameters.{spec.key}", "实例参数重复")
        seen.add(spec.key)
        rule = instance_parameter_rule(spec)
        base = base_parameters[spec.key]["rule"]
        if "minimum" in base and "minimum" in rule and rule["minimum"] < base["minimum"]:
            _error(f"parameters.{spec.key}", "实例最小值不能放宽执行目标边界")
        if "maximum" in base and "maximum" in rule and rule["maximum"] > base["maximum"]:
            _error(f"parameters.{spec.key}", "实例最大值不能放宽执行目标边界")
        if "enum" in base and "enum" in rule and any(not _contains(base["enum"], value) for value in rule["enum"]):
            _error(f"parameters.{spec.key}", "实例枚举不能增加执行目标未支持的选项")
        layers[spec.key].append({"source": source, "rule": rule})
    return compile_execution_parameter_contract(layers)
