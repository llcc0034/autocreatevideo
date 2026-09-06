"""Authoritative source registry for PromptProfile field generation.

The registry contains only facts that the production prompt-normalization
pipeline can actually resolve and freeze.  PromptProfile management, runtime
source collection, and the settings-page options API must all derive from this
module so a syntactically valid but unresolvable dotted path cannot be
published as a current contract.
"""
from __future__ import annotations

from dataclasses import dataclass


MODEL_GENERATION_MODES = ("model_generated", "translate_only")
MODEL_GENERATION_MODE_VALUES = frozenset(MODEL_GENERATION_MODES)
MODEL_GENERATION_OPERATIONS = frozenset({"image.generate", "video.generate"})


@dataclass(frozen=True, slots=True)
class PromptGenerationSourceSpec:
    value: str
    label: str
    scope_type: str
    group: str
    group_label: str
    description: str
    availability: str
    scope: str
    dialogue_exposure: str
    cross_shot_exposure: str
    cardinality: str

    def option(self) -> dict[str, str | list[str]]:
        return {
            "value": self.value,
            "label": self.label,
            "scope_type": self.scope_type,
            "group": self.group,
            "group_label": self.group_label,
            "description": self.description,
            "availability": self.availability,
            "scope": self.scope,
            "dialogue_exposure": self.dialogue_exposure,
            "cross_shot_exposure": self.cross_shot_exposure,
            "cardinality": self.cardinality,
            "status": "active",
            "generation_modes": list(MODEL_GENERATION_MODES),
        }


_SCOPE_METADATA = {
    "project": {
        "group": "project",
        "group_label": "项目事实 / Project facts",
        "subject": "当前项目",
        "scope": "项目级共享事实；可供项目内多个镜头使用；合同不主动注入独立对白正文，作者已写入该字段的文字仍会被读取。",
        "dialogue_exposure": "not_explicit",
        "cross_shot_exposure": "project_shared",
        "cardinality": "single",
    },
    "character": {
        "group": "character",
        "group_label": "人物设定 / Character Bible",
        "subject": "当前镜头已绑定的人物 Bible",
        "scope": "仅匹配当前镜头已绑定人物；不含对白正文；多个人物同时命中同一目标字段时会拒绝歧义。",
        "dialogue_exposure": "not_explicit",
        "cross_shot_exposure": "linked_bible_shared",
        "cardinality": "single_required",
    },
    "scene": {
        "group": "scene",
        "group_label": "场景设定 / Scene Bible",
        "subject": "当前镜头已绑定的场景 Bible",
        "scope": "仅匹配当前镜头的已绑定场景；不含对白正文，也不读取其他场景或镜头。",
        "dialogue_exposure": "not_explicit",
        "cross_shot_exposure": "linked_bible_shared",
        "cardinality": "single",
    },
    "prop": {
        "group": "prop",
        "group_label": "道具设定 / Prop Bible",
        "subject": "当前镜头已绑定的道具 Bible",
        "scope": "仅匹配当前镜头已绑定道具；不含对白正文；多个道具同时命中同一目标字段时会拒绝歧义。",
        "dialogue_exposure": "not_explicit",
        "cross_shot_exposure": "linked_bible_shared",
        "cardinality": "single_required",
    },
    "shot": {
        "group": "shot",
        "group_label": "当前镜头 / Target shot",
        "subject": "当前镜头",
        "scope": "仅当前镜头；不会自动读取独立对白字段、相邻镜头或全剧镜头；作者已写入所选自由文本字段的文字仍会被读取。",
        "dialogue_exposure": "not_explicit",
        "cross_shot_exposure": "target_shot_only",
        "cardinality": "single",
    },
}


def _source(scope_type: str, value: str, label: str) -> PromptGenerationSourceSpec:
    metadata = _SCOPE_METADATA[scope_type]
    return PromptGenerationSourceSpec(
        value=value,
        label=label,
        scope_type=scope_type,
        group=str(metadata["group"]),
        group_label=str(metadata["group_label"]),
        description=(
            f"读取{metadata['subject']}中已确认的“{label}”，作为生成当前目标字段的主要内容来源。"
        ),
        availability=(
            "适用于图像或视频生产策略的“模型生成”和“仅翻译”；服务端会从本次生产运行冻结的事实中精确解析，缺失时不会猜测或回退。"
        ),
        scope=str(metadata["scope"]),
        dialogue_exposure=str(metadata["dialogue_exposure"]),
        cross_shot_exposure=str(metadata["cross_shot_exposure"]),
        cardinality=str(metadata["cardinality"]),
    )


PROMPT_GENERATION_SOURCE_REGISTRY = (
    _source("project", "style.visual_prompt", "项目视觉风格正向提示"),
    _source("project", "style.negative_prompt", "项目视觉风格负面提示"),
    _source("project", "project.production_settings.color_script", "项目色彩脚本"),
    _source("project", "project.production_settings.negative_prompt", "项目制作负面约束"),
    _source("project", "project.continuity_summary", "项目连续性摘要"),
    _source("character", "character.name", "人物名称"),
    _source("character", "character.age_appearance", "人物年龄与外观"),
    _source("character", "character.visual_identity", "人物视觉身份"),
    _source("character", "character.face_identity", "人物面部身份"),
    _source("character", "character.wardrobe", "人物服装"),
    _source("character", "character.voice_profile", "人物声音设定"),
    _source("character", "character.personality", "人物性格"),
    _source("character", "character.negative_constraints", "人物负面约束"),
    _source("scene", "scene.name", "场景名称"),
    _source("scene", "scene.visual_identity", "场景视觉身份"),
    _source("scene", "scene.location", "场景地点"),
    _source("scene", "scene.time_of_day", "场景时间"),
    _source("scene", "scene.lighting", "场景灯光"),
    _source("scene", "scene.color_palette", "场景色彩"),
    _source("scene", "scene.ambient_sound", "场景环境声音"),
    _source("scene", "scene.negative_constraints", "场景负面约束"),
    _source("prop", "prop.name", "道具名称"),
    _source("prop", "prop.visual_identity", "道具视觉身份"),
    _source("prop", "prop.material", "道具材质"),
    _source("prop", "prop.distinctive_marks", "道具识别特征"),
    _source("prop", "prop.continuity_state", "道具连续性状态"),
    _source("prop", "prop.negative_constraints", "道具负面约束"),
    _source("shot", "shot.narrative", "当前镜头叙事"),
    _source("shot", "shot.detailed_description", "当前镜头详细描述"),
    _source("shot", "shot.action", "当前镜头动作"),
    _source("shot", "shot.camera", "当前镜头语言"),
    _source("shot", "shot.continuity_notes", "当前镜头连续性备注"),
    _source("shot", "shot.sound_effects", "当前镜头音效"),
    _source("shot", "shot.music", "当前镜头音乐"),
    _source("shot", "shot.negative_prompt", "当前镜头负面提示"),
)


PROMPT_GENERATION_SOURCE_VALUES = frozenset(
    item.value for item in PROMPT_GENERATION_SOURCE_REGISTRY
)
if len(PROMPT_GENERATION_SOURCE_VALUES) != len(PROMPT_GENERATION_SOURCE_REGISTRY):
    raise RuntimeError("Prompt generation source registry contains duplicate values")


PROMPT_GENERATION_SOURCE_KEYS_BY_SCOPE = {
    scope_type: frozenset(
        item.value
        for item in PROMPT_GENERATION_SOURCE_REGISTRY
        if item.scope_type == scope_type
    )
    for scope_type in _SCOPE_METADATA
}


def prompt_generation_source_options() -> list[dict[str, str | list[str]]]:
    return [item.option() for item in PROMPT_GENERATION_SOURCE_REGISTRY]
