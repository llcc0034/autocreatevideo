"""Explicit structure paths for renderer positions, never business column readers."""
from django.core.exceptions import ValidationError

from studio.services.prompt_profile_structures import validate_structured_path, read_profile_path, resolve_profile_structure, STRUCTURED_INPUT_SELECTOR


UNIT_RENDER_SLOTS = {
    "visual_prompt": "风格开场", "narrative": "镜头叙述", "description": "镜头描述",
    "action": "动作", "camera": "镜头运动", "sound_effects": "镜头声景", "music": "镜头音乐",
    "subjects": "主体描述", "environment": "环境", "lighting": "光照", "color_palette": "色彩",
}


def validate_unit_text_bindings(bindings, *, structure=None, check_structure=False):
    if not isinstance(bindings, dict) or set(bindings) - set(UNIT_RENDER_SLOTS):
        raise ValidationError("单元原文映射必须使用已登记的渲染位置")
    for slot, path in bindings.items():
        if not isinstance(path, list) or any(not isinstance(token, str) and (type(token) is not int or token < 0) for token in path):
            raise ValidationError("渲染来源须为属性名/非负整数下标数组，不能使用点路径或脚本")
        if check_structure:
            validate_structured_path(structure, path, "text")
    return bindings


def resolve_unit_render_values(*, bindings, structure, structured_inputs, unit_keys, max_bytes):
    validate_unit_text_bindings(bindings, structure=structure, check_structure=True)
    result = {}
    for unit_key in unit_keys:
        # Unmapped positions are explicitly unused, not missing source fallbacks.
        values = dict.fromkeys(UNIT_RENDER_SLOTS, "")
        if bindings:
            if not isinstance(structured_inputs, dict) or unit_key not in structured_inputs:
                raise ValidationError("渲染缺少本单元的结构化原值")
            sources, _ = resolve_profile_structure(structure,
                {STRUCTURED_INPUT_SELECTOR: structured_inputs[unit_key]}, max_bytes=max_bytes)
            for slot, path in bindings.items():
                value, _, present = read_profile_path(sources[STRUCTURED_INPUT_SELECTOR], path, slot)
                if not present or not isinstance(value, str):
                    raise ValidationError({f"unit_text_bindings.{slot}": "所选原文路径缺失或不是文本"})
                values[slot] = value
        result[unit_key] = values
    return result
