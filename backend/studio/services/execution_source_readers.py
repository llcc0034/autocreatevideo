"""Implemented source protocols frozen into exact execution definitions."""
from copy import deepcopy

from django.core.exceptions import ValidationError


from studio.services.visual_rules_output_contract import VISUAL_RULE_SOURCE_FIELDS, VISUAL_RULES_REVIEW_SCHEMA_VERSION


# Explicitly supported request value slots. New SourceSelector members do not
# automatically gain a reader. Values here are text, never paths or expressions.
REQUEST_TEXT_SOURCE_SELECTORS = (
    "request.theme", "request.lighting", "request.environment", "request.action",
    "request.camera", "request.style", "request.language", "request.text",
    "request.bgm_style", "request.dialogue_delivery", "request.negative_constraints",
    "request.subject", "request.composition", "request.color_palette", "request.motion",
    "request.duration", "request.aspect_ratio", "request.sound_effects", "request.voiceover",
)


def implemented_source_reader(selector):
    """Stable protocol semantics, independent of the current authoring catalog.

    A changed read/scope/approval rule requires a new protocol, never an edit to
    these v1 meanings. This does not evaluate arbitrary selectors or paths.
    """
    if selector in ("project.logline", "style.visual_prompt"):
        return {"protocol": "project_snapshot/v1", "selector": selector, "scope_kind": "project",
            "value_kind": "text", "snapshot_policy": "confirmed_preview"}
    if selector in REQUEST_TEXT_SOURCE_SELECTORS:
        return {"protocol": "request_text/v1", "selector": selector, "scope_kind": "request",
            "value_kind": "text", "snapshot_policy": "confirmed_request"}
    output_fields = {
        "reviewed_visual_rules.continuity_summary": "continuity_summary",
        "reviewed_visual_rules.color_script": "color_script",
        "reviewed_visual_rules.negative_prompt": "negative_prompt",
        "reviewed_visual_rules.camera_guidance": "camera_guidance",
    }
    if isinstance(selector, str) and selector in output_fields:
        return {"protocol": "reviewed_visual_rules/v1", "selector": selector, "scope_kind": "project",
            "value_kind": "text", "snapshot_policy": "exact_current_approval",
            "stage_key": "visual_rules_review", "output_schema_version": 3,
            "output_field": output_fields[selector]}
    raise ValidationError("来源读取协议尚未支持此字段")


def validate_source_reader(reader):
    if not isinstance(reader, dict):
        raise ValidationError("来源读取合同缺失，不会回退到当前目录")
    expected = implemented_source_reader(reader.get("selector"))
    # JSON types are significant (True and 1 must not compare equal).
    if set(reader) != set(expected) or any(type(reader[key]) is not type(value) or reader[key] != value
            for key, value in expected.items()):
        raise ValidationError("来源读取合同与已实现协议不一致")
    return deepcopy(reader)


def reviewed_visual_source_reader(selector):
    if not isinstance(selector, str) or selector not in VISUAL_RULE_SOURCE_FIELDS:
        raise ValidationError("未登记的影视规则输出字段")
    reader = implemented_source_reader(selector)
    if (reader["output_schema_version"] != VISUAL_RULES_REVIEW_SCHEMA_VERSION
            or reader["output_field"] != VISUAL_RULE_SOURCE_FIELDS[selector][0]):
        raise ValidationError("当前输出目录需要新读取协议，不能改写既有 v1 合同")
    return reader


def input_source_reader(spec, interface):
    from studio.services.content_sources import verify_source_reference
    for target in spec["targets"]:
        if target["target_kind"] != "profile_input":
            continue
        source = interface.get("profile_inputs", {}).get(target["target_key"], {})
        for reference in source.get("source_definitions", []):
            verified = verify_source_reference(reference)
            if verified["reader"] != source.get("source_reader"):
                raise ValidationError({spec["key"]: "来源快照与读取协议不一致"})
    readers = [interface.get("profile_inputs", {}).get(target["target_key"], {}).get("source_reader")
        for target in spec["targets"] if target["target_kind"] == "profile_input"]
    readers = [row for row in readers if row is not None]
    if not readers:
        return None
    reader = validate_source_reader(readers[0])
    for row in readers[1:]:
        validate_source_reader(row)
    if (any(target["target_key"] != reader["selector"] for target in spec["targets"]
            if target["target_kind"] == "profile_input")
            or any(row != reader for row in readers)
            or spec["kind"] != "text" or reader["scope_kind"] not in spec["semantic_contract"]["allowed_scope_kinds"]):
        raise ValidationError({spec["key"]: "来源读取合同、类型或范围不一致"})
    return deepcopy(reader)
