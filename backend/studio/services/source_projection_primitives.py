"""Allow-listed deterministic source-projection primitives.

Primitives are implementation capabilities.  PromptProfile field rules decide
which primitives are used, which business field receives each result, and how
the final prompt is rendered.  Keeping those choices in the versioned database
contract lets another model reuse a primitive without adding a model branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

from django.core.exceptions import ValidationError


REFERENCE_CONTEXT_ENGINE_KEY = "reference_context_ir_v1"


def contribution_target(primitive_key):
    """A primitive owns the render destination, independently of input paths."""
    primitive = SOURCE_PROJECTION_PRIMITIVES.get(primitive_key)
    if primitive is None or len(primitive.contribution_source_selectors) != 1:
        raise ValidationError({"projection_primitive": "所选原语没有唯一的贡献接收项"})
    return next(iter(primitive.contribution_source_selectors))


@dataclass(frozen=True)
class SourceProjectionPrimitive:
    key: str
    label: str
    description: str
    engine_key: str
    result_field: str
    rendering_template_keys: tuple[str, ...]
    contribution_source_selectors: frozenset[str] = frozenset()


def _primitive(
    key: str,
    label: str,
    description: str,
    result_field: str,
    *,
    rendering_template_keys: tuple[str, ...],
    contribution_source_selectors: tuple[str, ...] = (),
) -> SourceProjectionPrimitive:
    return SourceProjectionPrimitive(
        key=key,
        label=label,
        description=description,
        engine_key=REFERENCE_CONTEXT_ENGINE_KEY,
        result_field=result_field,
        rendering_template_keys=rendering_template_keys,
        contribution_source_selectors=frozenset(contribution_source_selectors),
    )


SOURCE_PROJECTION_PRIMITIVES = {
    item.key: item
    for item in (
        _primitive(
            "reference_context.subject_definitions",
            "参考主体定义 / Subject definitions",
            "把冻结参考绑定整理为稳定 Subject 编号与主体定义。",
            "subject_definitions",
            rendering_template_keys=("reference", "entity_reference", "empty_references"),
        ),
        _primitive(
            "reference_context.summary",
            "任务摘要 / Task summary",
            "根据目标镜头、时长、模式和音频合同生成确定性任务摘要。",
            "summary",
            rendering_template_keys=("summary", "reference_usage"),
        ),
        _primitive(
            "reference_context.retention_analysis",
            "参考保留关系 / Retention analysis",
            "按镜头说明参考素材需要保留的身份、外观与运动关系。",
            "retention_analysis",
            rendering_template_keys=("retention", "empty_retention"),
        ),
        _primitive(
            "reference_context.detailed_description",
            "导演时间线 / Detailed direction",
            "组合风格开场、逐镜头时间线、动作、对白和参考主体首次出现。",
            "detailed_description",
            rendering_template_keys=("style", "shot", "reviewed_shot", "shot_opening", "shot_transition", "reference_usage", "dialogue", "voiceover_dialogue", "direction"),
            contribution_source_selectors=("shot.detailed_description",),
        ),
        _primitive(
            "reference_context.overall_soundscape",
            "总体声景 / Overall soundscape",
            "组合逐镜头音效与运行级总体声景。",
            "overall_soundscape",
            rendering_template_keys=("sound", "silent_sound"),
            contribution_source_selectors=("shot.sound_effects",),
        ),
        _primitive(
            "reference_context.non_diegetic_music",
            "非画内音乐 / Non-diegetic music",
            "组合逐镜头音乐贡献与运行级非画内音乐。",
            "non_diegetic_music",
            rendering_template_keys=("music", "silent_music"),
            contribution_source_selectors=("shot.music",),
        ),
    )
}


def reference_result_fields(rules):
    """Only the primitive outputs selected by this exact strategy."""
    return frozenset(SOURCE_PROJECTION_PRIMITIVES[row["projection_primitive"]].result_field
        for row in rules if row["source_selector"] == "projection.output")


def reference_requires_units(*, prompt_type, result_fields, rendering):
    """Reference declarations need units only when their templates consume them.

    Temporal/direction primitives and video keep their explicit unit contract.
    Unused templates must not introduce inputs into a reference-only image.
    """
    if (prompt_type != "image" or not result_fields
            or set(result_fields) - {"subject_definitions", "retention_analysis"}
            or rendering["unit_text_bindings"]):
        return True
    templates = {key for primitive in SOURCE_PROJECTION_PRIMITIVES.values()
        if primitive.result_field in result_fields for key in primitive.rendering_template_keys}
    return any(name == "shots" for key in templates
        for _, name, _, _ in Formatter().parse(rendering["templates"][key]))


def reference_profile_requires_units(contract):
    rendering = contract["source_projection_config"]["primitive_options"][REFERENCE_CONTEXT_ENGINE_KEY]["rendering"]
    return (any(row["source_selector"] == "projection.output" and (
            row["generation_mode"] in {"model_generated", "translate_only"} or row["generation_context_selectors"])
            for row in contract["rules"])
        or reference_requires_units(prompt_type="image" if contract["operation"] == "image.generate" else "video",
            result_fields=reference_result_fields(contract["rules"]), rendering=rendering))


def source_projection_primitive_options() -> list[dict[str, object]]:
    return [
        {
            "value": primitive.key,
            "label": primitive.label,
            "description": primitive.description,
            "engine_key": primitive.engine_key,
            # Read-only dependency metadata, not saved defaults or an execution trace.
            "rendering_template_keys": list(primitive.rendering_template_keys),
            "contribution_source_selectors": sorted(
                primitive.contribution_source_selectors,
            ),
        }
        for primitive in SOURCE_PROJECTION_PRIMITIVES.values()
    ]
