"""Profile-owned input structure and declared field paths, shared with execution."""
from django.core.exceptions import ValidationError

from studio.services.data_structures import data_structure_snapshot, validate_data_structure_snapshot, resolve_structure_value
from studio.services.structure_paths import nonnull_schema, path_schema, read_structure_path


STRUCTURED_INPUT_SELECTOR = "request.structured_input"
_TYPES = {"text": "string", "long_text": "string", "enum": "string", "boolean": "boolean",
          "integer": "integer", "decimal": "number", "list": "array"}


def profile_input_structure(profile, *, require_published=False):
    if not profile.input_structure_version_id:
        return None
    structure = profile.input_structure_version
    if structure.status == "draft" or require_published and structure.status != "published":
        raise ValidationError({"input_structure_version": "请选择可用的确切结构版本"})
    return data_structure_snapshot(structure)


def validate_structured_rule(rule, *, snapshot=None, require_published=False):
    from studio.services.prompt_generation_contexts import validate_structured_contexts
    snapshot = snapshot if snapshot is not None else profile_input_structure(rule.profile_version, require_published=require_published)
    validate_structured_contexts(rule.generation_context_selectors, snapshot=snapshot,
        generation_source=rule.generation_source_selector, generation_mode=rule.generation_mode)
    if rule.source_selector != STRUCTURED_INPUT_SELECTOR and rule.generation_source_selector != STRUCTURED_INPUT_SELECTOR:
        if rule.source_path is not None:
            raise ValidationError({"source_path": "只有结构化输入来源可声明字段路径；其他来源须为null"})
        return
    if rule.source_version_id:
        raise ValidationError({"source_version": "结构化输入由策略的输入结构维护，不另绑定文字来源版本"})
    if snapshot is None:
        raise ValidationError({"input_structure_version": "请先为策略选择输入结构版本"})
    from studio.services.field_definitions import profile_rule_definition
    value_type = profile_rule_definition(rule).value_type
    validate_structured_path(snapshot, rule.source_path, value_type)
    validate_structured_generation(generation_mode=rule.generation_mode,
        generation_source=rule.generation_source_selector, output_semantics=rule.generation_output_semantics,
        projection=rule.profile_version.source_projection_key, value_type=value_type, source_selector=rule.source_selector)


def validate_structured_generation(*, generation_mode, generation_source, output_semantics, projection, value_type, source_selector):
    """One path selects the raw input for assembly or auxiliary generation."""
    if generation_source != STRUCTURED_INPUT_SELECTOR:
        raise ValidationError({"generation_source_selector": "结构化字段沿用同一输入路径，不能另选内容来源"})
    if source_selector != STRUCTURED_INPUT_SELECTOR and not (
            source_selector == "projection.output" and projection == "declarative_v1" and output_semantics in {"contribution", "final"}):
        raise ValidationError({"source_selector": "结构化生成只装配本字段或交给声明式原语，不另选最终来源"})
    if generation_mode in {"passthrough", "server_locked"}:
        return
    if generation_mode not in {"model_generated", "translate_only"}:
        raise ValidationError({"generation_mode": "未登记的结构化字段生成方式"})
    if (projection, output_semantics) not in {("direct_v1", "final"), ("declarative_v1", "final"), ("declarative_v1", "contribution")}:
        raise ValidationError({"generation_output_semantics": "结构化路径辅助生成须填入最终字段，或明确提供逐单元贡献"})
    if _TYPES.get(value_type) != "string":
        raise ValidationError({"source_path": "辅助生成来源必须为文本路径；不隐式转换数字、对象或列表"})


def validate_structured_path(snapshot, path, value_type):
    if snapshot is None:
        raise ValidationError({"input_structure_version": "请先为策略选择输入结构版本"})
    schema = nonnull_schema(path_schema(validate_data_structure_snapshot(snapshot), path, "source_path"))
    expected = _TYPES.get(value_type)
    actual = schema.get("type")
    if expected is None or actual != expected and not (expected == "number" and actual == "integer"):
        raise ValidationError({"source_path": "所选路径类型与规则字段不符；不隐式转换对象、数字或文字"})
    if expected == "array" and nonnull_schema(schema.get("items", {})).get("type") != "string":
        raise ValidationError({"source_path": "提示词列表字段需要文本元素；对象列表请明确选择条目字段"})


def resolve_profile_structure(snapshot, sources, *, max_bytes):
    """Resolve a copy; samples never become actual values or overwrite sources."""
    if snapshot is None or STRUCTURED_INPUT_SELECTOR not in sources:
        return sources, None
    from studio.services.json_contracts import JsonExpansionBudget
    result = resolve_structure_value(snapshot, sources[STRUCTURED_INPUT_SELECTOR], path=STRUCTURED_INPUT_SELECTOR,
                                     consume=JsonExpansionBudget(max_bytes, path=STRUCTURED_INPUT_SELECTOR).consume)
    if not result["valid"]:
        raise ValidationError(result["errors"])
    return {**sources, STRUCTURED_INPUT_SELECTOR: result["value"]}, {
        "structure_version_id": snapshot["id"], "defaults": result["defaults"],
    }


def read_profile_path(value, path, field_key):
    return read_structure_path(value, path, STRUCTURED_INPUT_SELECTOR,
                               "fields." + field_key, if_missing=None)
