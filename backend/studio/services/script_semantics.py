"""M2 script input semantics over M1's configured structures and mapping engine.

This is a pure configuration/projection service, not a script writer or another
parser. Source keys, nesting, use rules and creative fields belong to the exact
DataStructure snapshot. The finite target below describes business inputs only.
It does not create Bible/shot/media records or infer references from names.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.data_structures import _value_errors, validate_data_structure_snapshot
from studio.services.json_contracts import JsonExpansionBudget, json_contract
from studio.services.structure_mapping import (
    evaluate_mapping, mapping_authoring_options, validate_mapping,
)
from studio.services.structured_output_parsing import parse_structure_text
from studio.services.script_semantic_relations import VALIDATION_SCOPE, script_relation_errors
from studio.services.dialogue import dialogue_language_schema


SEMANTICS_KEY = "script_semantics.v1"


def _text(title, *, identity=False):
    return {"type": "string", "title": title, **({"minLength": 1} if identity else {})}


def _object(title, properties, required=()):
    return {"type": "object", "title": title, "properties": properties,
        "required": list(required), "additionalProperties": False}


def _list(title, items):
    return {"type": "array", "title": title, "items": items}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}], "title": schema["title"]}


def script_semantic_schema():
    """Engine input catalog, not provider response property names or a template.

    Optional creative content has no built-in prose, default value or length
    advice. Cross-record references are checked by script_semantic_relations;
    projection preview alone does not mean a candidate is ready to materialize.
    """
    key = _text("稳定声明key（不是显示名或译名）", identity=True)
    primary = _object("主图设定", {
        "reference_name": _text("主图素材引用名（语言不限）", identity=True),
        "anchor": _text("主图外观设定"),
    })
    variant = _object("相对主图的变体设定", {
        "key": key, "name": _text("变体名称", identity=True),
        "reference_name": _text("变体素材引用名（语言不限）", identity=True),
        "anchor": _text("相对主图的差异设定"),
    }, ("key", "name"))
    entity = _object("实体声明", {
        "key": key, "kind": {"type": "string", "title": "实体类型", "enum": ["character", "scene", "prop"]},
        "name": _text("实体名称（语言不限）", identity=True),
        "description": _text("实体背景描述"), "identity_anchor": _text("共同身份设定"),
        "primary": primary, "variants": _list("变体集合（可为空）", variant),
    }, ("key", "kind", "name"))
    reference = _object("镜头素材需求（不是已选图片）", {
        "use_key": key, "entity_key": key, "variant_key": _nullable(key),
        "media_kind": {"type": "string", "title": "媒体类型", "enum": ["image", "video", "audio"]},
        "purpose": _text("创作用途描述（可选，不是模型输入用途）"),
    }, ("use_key", "entity_key", "variant_key", "media_kind"))
    dialogue = _object("对白（保留原语言）", {
        "speaker_entity_key": _nullable(key), "text": _text("对白原文"),
        "language": dialogue_language_schema(),
        "delivery": _text("演绎方式"), "is_voiceover": {"type": "boolean", "title": "是否画外音"},
        "start_ms": {"type": "integer", "minimum": 0, "title": "对白开始（毫秒）"},
        "end_ms": {"type": "integer", "minimum": 0, "title": "对白结束（毫秒）"},
    }, ("text",))
    shot = _object("镜头声明", {
        "shot": {"type": "integer", "minimum": 1, "title": "镜序号"},
        "setting": _text("场景描述"), "action": _text("动作"), "camera": _text("机位与运镜"),
        "duration_ms": {"type": "integer", "minimum": 1, "title": "镜头时长（毫秒）"},
        "episode_start_ms": {"type": "integer", "minimum": 0, "title": "集内开始（毫秒）"},
        "episode_end_ms": {"type": "integer", "minimum": 0, "title": "集内结束（毫秒）"},
        "scene_key": _nullable(key), "character_keys": _list("可见人物key", key),
        "prop_keys": _list("可见道具key", key), "dialogue": dialogue,
        "references": _list("本镜素材需求", reference),
    }, ("shot",))
    episode = _object("分集声明", {
        "episode": {"type": "integer", "minimum": 1, "title": "集序号"},
        "title": _text("分集标题"), "shots": _list("有序镜头声明", shot),
    }, ("episode", "shots"))
    return _object("剧本业务输入（不是外部JSON格式）", {
        "entities": _list("实体声明", entity), "episodes": _list("分集声明", episode),
    }, ("entities", "episodes"))


def script_semantic_options():
    return {"key": SEMANTICS_KEY, "json_schema": script_semantic_schema(),
        "mapping": mapping_authoring_options()}


def freeze_script_semantics(*, source_contract, mapping, max_bytes):
    """Freeze explicit configuration for the existing output contract owner.

    No DB writes, identity allocation, current-version lookup or config fallback.
    The caller must persist the result through the output version authoring path.
    """
    schema = validate_data_structure_snapshot(source_contract)
    validate_mapping(mapping, schema, script_semantic_schema())
    body = {"key": SEMANTICS_KEY, "source_structure": deepcopy(source_contract), "mapping": deepcopy(mapping)}
    contract = json_contract(body, max_bytes=max_bytes)
    return {**contract.value, "sha256": contract.sha256}


def verify_script_semantics(configuration, *, max_bytes):
    if (not isinstance(configuration, dict) or set(configuration) != {"key", "source_structure", "mapping", "sha256"}
            or configuration["key"] != SEMANTICS_KEY):
        raise ValidationError({"script_semantics": "需要确切的剧本语义映射及来源结构版本"})
    expected = freeze_script_semantics(source_contract=configuration["source_structure"],
        mapping=configuration["mapping"], max_bytes=max_bytes)
    if expected["sha256"] != configuration["sha256"]:
        raise ValidationError({"script_semantics": "剧本语义映射或来源结构已变化，请重新读取版本"})
    return expected


def _parse_script_semantics(*, configuration, response_text, max_bytes, check_relations=False):
    """Common projection after M1 parsing; no candidate writes or relation repair."""
    configuration = verify_script_semantics(configuration, max_bytes=max_bytes)
    source = configuration["source_structure"]
    budget = JsonExpansionBudget(max_bytes, path="script_semantics.parse_budget")
    parsed = parse_structure_text(structure=source, response_text=response_text,
        max_bytes=max_bytes, path="source", budget=budget)
    result = {"validation_scope": "structure_and_mapping",
        "configuration_sha256": configuration["sha256"], "source_structure_version_id": source["id"],
        "source_fields": parsed["fields"], "parsed": parsed["parsed"], "valid": False,
        "raw_value": parsed["raw_value"], "source_value": parsed["value"], "value": None,
        "defaults": parsed["defaults"], "provenance": [], "warnings": parsed["warnings"], "errors": parsed["errors"]}
    if not parsed["valid"]:
        if not parsed["parsed"] and "source" in result["errors"]:
            result["errors"]["response_text"] = result["errors"].pop("source")
        return result
    try:
        value, origins = evaluate_mapping(configuration["mapping"], parsed["value"],
            source_schema=parsed["json_schema"], target_schema=script_semantic_schema(), consume=budget.consume)
        result["value"] = value
        for origin in origins:
            record = {**origin, "source_structure_version_id": source["id"],
                "semantic_contract": SEMANTICS_KEY, "configuration_sha256": configuration["sha256"]}
            budget.consume(record)
            result["provenance"].append(record)
        result["errors"] = _value_errors(script_semantic_schema(), value, path="value")
        result["valid"] = not result["errors"]
        if result["valid"] and check_relations:
            relations = script_relation_errors(value, provenance=result["provenance"], consume=budget.consume)
            result.update(validation_scope=VALIDATION_SCOPE, relation_errors=relations, valid=not relations)
            for error in relations:
                result["errors"].setdefault(error["path"], []).append(error["message"])
    except ValidationError as exc:
        result["valid"] = False
        result["errors"] = getattr(exc, "message_dict", None) or {
            "response_text" if not result["parsed"] else "mapping": exc.messages}
    except RecursionError:
        result["valid"] = False
        result["errors"] = {"mapping": ["当前嵌套无法解析，请检查结构与样例"]}
    return result


def preview_script_semantics(*, configuration, response_text, max_bytes):
    """Shape/projection preview is not relation validation or an accepted script."""
    return {"preview_only": True, **_parse_script_semantics(
        configuration=configuration, response_text=response_text, max_bytes=max_bytes)}


class ScriptSemanticValidationError(ValidationError):
    """Keep parser-owned paths/relations without copying returned script content.

    Preview returns the complete report; writers and execution adapters raise
    this error so the original response-review owner can retain diagnostics.
    """
    def __init__(self, report):
        if report["valid"] or not report["errors"]:
            raise ValueError("Only an invalid semantic report can become a validation error")
        self.structure_errors = deepcopy(report["errors"])
        self.relation_errors = deepcopy(report.get("relation_errors", []))
        self.validation_scope = report["validation_scope"]
        # The regular HTTP field-error envelope should also identify the actual
        # source for mapped relations, not just the consumer's semantic path.
        for issue in self.relation_errors:
            source = issue.get("source_path")
            if source and source != issue["path"]:
                messages = self.structure_errors.setdefault(source, [])
                if issue["message"] not in messages:
                    messages.append(issue["message"])
        super().__init__(self.structure_errors, code="script_semantic_result_invalid")


def parse_script_semantic_output(*, reference, response_text, max_bytes):
    """Explicit JSON input over an exact output version, not auto-detected text.

    Used by import/response adapters; malformed JSON stays a parse failure and
    never falls back to AI or the old marker parser. Relationships are checked
    after projection; success still does not create or approve a candidate.
    """
    from studio.services.business_outputs import verify_output_reference

    reference = verify_output_reference(reference)
    if reference["parser"]["key"] != SEMANTICS_KEY:
        raise ValidationError("剧本解析需要明确的剧本语义映射输出版本")
    configuration = freeze_script_semantics(source_contract=reference["data_structure"],
        mapping=reference["parser"]["mapping"], max_bytes=max_bytes)
    return {"output_version_id": reference["id"], "output_contract_sha256": reference["sha256"],
        **_parse_script_semantics(configuration=configuration, response_text=response_text, max_bytes=max_bytes,
            check_relations=True)}
