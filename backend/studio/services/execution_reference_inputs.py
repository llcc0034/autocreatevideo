"""Explicit request-unit input for the existing reference-context primitive.

Only business unit facts are editable here. Mode, output kind, audio topology,
reference labels/ports and model contracts are supplied by the authorized
definition/material adapters, never accepted from the literal input object.
"""
from copy import deepcopy
from dataclasses import asdict

from studio.services.json_contracts import json_contract
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.dialogue import dialogue_language_schema
from studio.services.reference_context_facts import (
    DIALOGUE_REQUIRED_FACTS, ReferenceSubjectFacts, read_reference_context_facts, speaker_subject_key_schema,
)


REFERENCE_CONTEXT_INPUT_KEY = "reference_context.units"
_REQUEST_TEXT_FIELDS = {
    "color_script": "整体色彩要求", "extra_direction": "补充导演要求",
    "overall_soundscape": "整体声景", "non_diegetic_music": "背景音乐要求",
}


def reference_context_input_schema(*, input_structure=None):
    """Read-only form shape; no made-up units, identities or text defaults."""
    text = {"type": "string"}
    dialogue = {"speaker_id": {**text, "title": "说话人标识"}, "text": {**text, "title": "对白内容"},
        "language": dialogue_language_schema(),
        "delivery": {**text, "title": "语气与表达"}, "subject": {**text, "title": "说话人说明"},
        "voice_profile": {**text, "title": "声音说明"}, "voiceover": {"type": "boolean", "title": "画外音"},
        "start_ms": {"type": "integer", "minimum": 0, "title": "单元内开始时间（毫秒）"},
        "end_ms": {"type": "integer", "minimum": 1, "title": "单元内结束时间（毫秒）"},
        "speaker_subject_key": speaker_subject_key_schema()}
    unit = {
        "unit_key": {"type": "string", "minLength": 1, "title": "本次单元标识"},
        "duration_ms": {"type": "integer", "minimum": 1, "title": "单元时长（毫秒）"},
        "subject_keys": {"type": "array", "title": "本单元使用的主体", "uniqueItems": True,
            "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}}},
        "dialogue": {"type": "array", "title": "有序对白", "items": {"type": "object", "additionalProperties": False,
            "required": sorted(DIALOGUE_REQUIRED_FACTS), "properties": dialogue}},
        "voice_profiles": {"type": "array", "title": "声音特征", "items": deepcopy(text)},
        "continuity_notes": {**text, "title": "连续性说明"}, "ambient_sound": {**text, "title": "环境声音"},
        "requires_first_frame_continuity": {"type": "boolean", "title": "需要首帧连续"},
        "requires_last_frame_continuity": {"type": "boolean", "title": "需要尾帧连续"},
    }
    if input_structure is not None:
        from studio.services.data_structures import validate_data_structure_snapshot
        unit["structured_input"] = {**validate_data_structure_snapshot(input_structure), "title": "本单元结构化原值"}
    properties = {"schema_version": {"type": "integer", "const": 1},
        "ordered_units": {"type": "array", "minItems": 1, "title": "按执行顺序排列的单元", "items": {
            "type": "object", "additionalProperties": False, "required": list(unit), "properties": unit}},
        **{key: {**text, "title": title} for key, title in _REQUEST_TEXT_FIELDS.items()}}
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def project_reference_unit_input(value, *, prompt_type, mode, include_audio, language,
                                 subject_facts, reference_bindings, conditioning_mode_contract, frame_policy, max_bytes,
                                 unit_render_values):
    """Combine explicit unit values with service-derived resource/media facts.

    No implicit scope, graph example, current Project or latest approved field
    is read here. A literal source may describe a new asset unit; real project
    context must already have been authorized and resolved by its source owner.
    """
    required = {"schema_version", "ordered_units", *_REQUEST_TEXT_FIELDS}
    if not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PromptBuildError("参考单元输入必须完整，且不能覆盖模型、音频、素材标签或批准事实")
    json_contract(value, max_bytes=max_bytes)
    if not isinstance(value["ordered_units"], list) or not value["ordered_units"]:
        raise PromptBuildError("请明确提供至少一个有序单元")
    unit_keys = [row.get("unit_key") if isinstance(row, dict) else None for row in value["ordered_units"]]
    if any(not isinstance(key, str) or not key for key in unit_keys) or len(set(unit_keys)) != len(unit_keys):
        raise PromptBuildError("单元标识必须明确且唯一")
    if not isinstance(unit_render_values, dict) or set(unit_render_values) != set(unit_keys):
        raise PromptBuildError("渲染映射值必须与本次单元一致")
    for unit in value["ordered_units"]:
        if not isinstance(unit, dict) or "source_facts" in unit or "render_values" in unit:
            raise PromptBuildError("不能通过单元输入注入旧列名或已解析的渲染值")
    if not isinstance(subject_facts, (tuple, list)) or any(not isinstance(row, ReferenceSubjectFacts) for row in subject_facts):
        raise PromptBuildError("主体必须来自本次已解析的正式事实")
    if not isinstance(reference_bindings, (tuple, list)):
        raise PromptBuildError("参考素材必须来自本次已解析的有序绑定")
    context = {"prompt_type": prompt_type, "mode": mode, "include_audio": include_audio, "language": language,
        "ordered_units": [{**deepcopy(row), "render_values": deepcopy(unit_render_values[row["unit_key"]])}
                          for row in value["ordered_units"]],
        "subject_facts": [{**asdict(row), "description_parts": list(row.description_parts)} for row in subject_facts],
        "reference_bindings": deepcopy(list(reference_bindings)), "conditioning_mode_contract": deepcopy(conditioning_mode_contract),
        "frame_policy": deepcopy(frame_policy), **{key: value[key] for key in _REQUEST_TEXT_FIELDS}, "generation_contributions": {}}
    return read_reference_context_facts(json_contract({"schema_version": 1, "context": context}, max_bytes=max_bytes), max_bytes=max_bytes)
