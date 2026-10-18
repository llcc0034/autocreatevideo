"""Generation choices derived from the implemented execution source readers.

Structured input is validated separately against the strategy's exact structure
and source_path. Fixed Bible/Shot column names are not executable input readers.
"""
from __future__ import annotations

MODEL_GENERATION_MODES = ("model_generated", "translate_only")
MODEL_GENERATION_MODE_VALUES = frozenset(MODEL_GENERATION_MODES)
# Field processing belongs to the shared preparation pipeline, not a media
# operation allow-list. Auxiliary operations themselves must stay leaves.
FIELD_AUXILIARY_OPERATIONS = frozenset({"prompt.fields.generate", "prompt.fields.translate"})


def prompt_generation_source_options():
    # Lazy import: the normalization contract also imports the mode constants.
    from studio.services.content_sources import source_reader_options

    result = []
    for item in source_reader_options():
        reader = item["reader"]
        result.append({
            "value": item["value"], "label": item["label"],
            "scope_type": reader["scope_kind"], "group": reader["protocol"],
            "group_label": item["group_label"], "description": item["description"],
            "availability": "适用于使用策略的执行输入字段生成及翻译；须沿所选输入合同提供并冻结原值。维护入口：" + item["record_location"],
            "scope": item["execution_usage"],
            "dialogue_exposure": "explicit_input_only",
            "cross_shot_exposure": "explicit_input_only",
            "cardinality": "single", "status": "active",
            "generation_modes": list(MODEL_GENERATION_MODES),
        })
    result.append({
        "value": "projection.output", "label": "本字段的确定性装配原文",
        "scope_type": "request", "group": "projection", "group_label": "本次装配事实",
        "description": "先按本字段的投影配置读取本次事实，作为辅助生成原文；采用结果填入本字段，不另登记接收槽。",
        "availability": "声明式策略、最终字段值；原文来自本次实际素材和单元，不能手填投影结果。",
        "scope": "本次请求", "dialogue_exposure": "explicit_input_only", "cross_shot_exposure": "explicit_input_only",
        "cardinality": "single", "status": "active", "generation_modes": list(MODEL_GENERATION_MODES),
    })
    return result


def prompt_generation_source_values():
    return frozenset(item["value"] for item in prompt_generation_source_options())
