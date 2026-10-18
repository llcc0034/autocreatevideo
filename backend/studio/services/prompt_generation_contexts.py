"""Explicit auxiliary context paths in the Profile's existing input structure.

Selectors encode the existing token-array path, not an expression language.
They travel in the original versioned selector list and context_facts contract.
"""
import json

from django.core.exceptions import ValidationError

from studio.services.data_structures import validate_data_structure_snapshot
from studio.services.structure_paths import path_schema, read_structure_path

STRUCTURED_CONTEXT_PREFIX = "request.structured_input:"


def structured_context_selector(path):
    return STRUCTURED_CONTEXT_PREFIX + json.dumps(path, ensure_ascii=False, separators=(",", ":"))


def structured_context_path(selector):
    if not isinstance(selector, str) or not selector.startswith(STRUCTURED_CONTEXT_PREFIX):
        return None
    try:
        path = json.loads(selector[len(STRUCTURED_CONTEXT_PREFIX):])
    except (ValueError, RecursionError) as exc:
        raise ValidationError({"generation_context_selectors": "附加字段路径格式无效"}) from exc
    if (not isinstance(path, list)
            or any(not (type(token) is str or type(token) is int and token >= 0) for token in path)
            or selector != structured_context_selector(path)):
        raise ValidationError({"generation_context_selectors": "附加字段必须使用规范属性名/非负下标路径"})
    return path


def validate_structured_contexts(selectors, *, snapshot, generation_source, generation_mode):
    if not isinstance(selectors, list):
        return  # the common rule validator reports the malformed selector list
    paths = [structured_context_path(selector) for selector in selectors]
    paths = [path for path in paths if path is not None]
    if not paths:
        return
    if generation_mode not in {"model_generated", "translate_only"}:
        raise ValidationError({"generation_context_selectors": "结构字段参考须选择生成或翻译"})
    if snapshot is None:
        raise ValidationError({"input_structure_version": "附加字段需要绑定确切输入结构"})
    schema = validate_data_structure_snapshot(snapshot)
    for path in paths:
        path_schema(schema, path, "generation_context_selectors")


def read_structured_context(value, selector, field_key):
    path = structured_context_path(selector)
    if path is None:
        raise ValidationError({"generation_context_selectors": "不是结构化参考来源"})
    # Missing is an error; explicit null/false/zero/empty values retain their
    # schema-validated meaning. Never fall back to another field or unit.
    result, _, _ = read_structure_path(value, path, "request.structured_input", f"fields.{field_key}.context_facts")
    return result
