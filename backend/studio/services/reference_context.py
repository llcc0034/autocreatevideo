"""Model-neutral reference facts rendered by an explicit, frozen template policy.

Templates can interpolate named scalar facts only: no expressions, attributes,
indexes, conversions, recursive expansion or executable template language.
"""
from __future__ import annotations

from copy import deepcopy
from string import Formatter
import json
import re
from django.core.exceptions import ValidationError

from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.workflow import aligned_frames, WorkflowBuildError
from studio.services.security_boundaries import boundary_int
from studio.services.reference_context_facts import ReferenceContextFacts, validate_reference_context_facts
from studio.services.reference_rendering_inputs import validate_unit_text_bindings


RENDERING_FIELDS = {
    "reference": ("参考素材声明", ("label", "subject", "category", "purpose", "description", "guidance", "parent_label", "source_kind")),
    "retention": ("参考保留关系", ("label", "subject", "purpose", "guidance", "shots")),
    "summary": ("任务摘要", ("mode", "shot_count", "duration_seconds", "reference_count", "include_audio")),
    "shot": ("逐镜描述", ("index", "start_seconds", "duration_seconds", "narrative", "description", "action", "camera", "dialogue", "references", "subjects", "environment", "lighting", "color_palette", "voice_profiles", "continuity", "subject_labels")),
    "dialogue": ("对白", ("speaker_id", "delivery", "text", "start_ms", "end_ms", "voiceover", "language")),
    "style": ("风格开场", ("visual_prompt", "color_script")),
    "sound": ("镜头声景", ("index", "description")),
    "music": ("镜头音乐", ("index", "description")),
    "direction": ("全局导演要求", ("description",)),
    "empty_references": ("无参考素材时的声明", ()),
    "empty_retention": ("无保留关系时的说明", ()),
    "silent_sound": ("不输出音频时的声景说明", ()),
    "silent_music": ("不输出音频时的音乐说明", ()),
}
RENDERING_FIELDS["shot"] = (RENDERING_FIELDS["shot"][0], (*RENDERING_FIELDS["shot"][1], "requires_first_frame_continuity", "requires_last_frame_continuity"))
RENDERING_FIELDS["reviewed_shot"] = ("已审核逐镜贡献", RENDERING_FIELDS["shot"][1])
RENDERING_FIELDS["dialogue"] = ("对白", (*RENDERING_FIELDS["dialogue"][1], "subject", "voice_profile",
    "speaker_reference_label", "speaker_index", "start_timecode", "end_timecode"))
RENDERING_FIELDS["voiceover_dialogue"] = ("画外音对白", RENDERING_FIELDS["dialogue"][1])
REFERENCE_FACTS = (*RENDERING_FIELDS["reference"][1], "relationship", "usage", "shots", "source_labels")
RENDERING_FIELDS["reference"] = ("参考素材声明", REFERENCE_FACTS)
RENDERING_FIELDS["entity_reference"] = ("参考主体声明", REFERENCE_FACTS)
RENDERING_FIELDS["retention"] = ("参考保留关系", REFERENCE_FACTS)
RENDERING_FIELDS["reference_usage"] = ("逐镜参考用途", REFERENCE_FACTS)
RENDERING_FIELDS["summary"] = ("任务摘要", (*RENDERING_FIELDS["summary"][1], "task_types", "reference_relationships"))
for _key in ("shot", "reviewed_shot"):
    RENDERING_FIELDS[_key] = (RENDERING_FIELDS[_key][0], (*RENDERING_FIELDS[_key][1], "shot_marker", "reference_usage", "timecode"))
RENDERING_FIELDS["shot_opening"] = ("首镜头标记", ("index", "timecode", "start_seconds"))
RENDERING_FIELDS["shot_transition"] = ("后续镜头标记", ("index", "timecode", "start_seconds"))


def rendering_options() -> list[dict]:
    from studio.services.reference_rendering_help import FIELD_HELP, placeholder_help

    rows = []
    for key, (label, names) in RENDERING_FIELDS.items():
        description, assembly, requirement, example_template = FIELD_HELP[key]
        help_rows = placeholder_help(key, names)
        examples = {name: item["example"] for name, item in help_rows.items()}
        rows.append({"key": key, "label": label, "placeholders": list(names),
                     "description": description, "assembly": assembly, "requirement": requirement,
                     "placeholder_help": help_rows,
                     "example": {"template": example_template,
                                 "result": render_reference_template(example_template, names, examples)}})
    return rows


def validate_rendering_policy(value: object) -> dict:
    required = {"templates", "separator", "list_prefix", "prompt_template_key", "reference_rules", "task_separator", "unreferenced_task", "unit_text_bindings"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"reference_layout"}:
        raise PromptBuildError("参考上下文缺少完整渲染配置；请创建后继草稿并配置模板、分隔符、模板标识及明确的原文映射（可为空）")
    try:
        validate_unit_text_bindings(value["unit_text_bindings"])
    except ValidationError as exc:
        raise PromptBuildError(str(exc)) from exc
    templates = value["templates"]
    if not isinstance(templates, dict) or set(templates) != set(RENDERING_FIELDS):
        raise PromptBuildError("参考上下文模板字段必须与服务端目录一致")
    limit = boundary_int("security.prompt_profile.field_text_max_characters")
    layout = value.get("reference_layout")
    if "reference_layout" in value:
        if not isinstance(layout, dict) or set(layout) != {"subject_label_template", "source_separator", "summary_reference_template", "shot_reference_template"}:
            raise PromptBuildError("分段参考布局必须完整声明主体编号、来源分隔符及摘要/逐镜参考模板")
        if any(not isinstance(text, str) or len(text) > limit for text in layout.values()):
            raise PromptBuildError("分段参考布局必须为有界文本")
        if not re.fullmatch(r"<[A-Za-z][A-Za-z_ -]* \{index\}>", layout["subject_label_template"]):
            raise PromptBuildError("主体编号模板须为 <名称 {index}>；编号由本次实际主体分配")
        if not layout["source_separator"]:
            raise PromptBuildError("主体来源分隔符不能为空")
        for key in ("summary_reference_template", "shot_reference_template"):
            try:
                parsed = list(Formatter().parse(layout[key]))
            except ValueError as exc:
                raise PromptBuildError(f"{key} 花括号格式无效") from exc
            if any(name is not None and (name not in REFERENCE_FACTS or spec or conversion)
                   for _, name, spec, conversion in parsed):
                raise PromptBuildError(f"{key} 仅允许参考事实目录中的简单占位符")
            if layout[key].strip() and "label" not in {name for _, name, _, _ in parsed}:
                raise PromptBuildError(f"{key} 非空时必须保留 {{label}}")
    rules = value["reference_rules"]
    if not isinstance(rules, list) or len(rules) > boundary_int("security.contract_complexity.prompt_profile.value_options_max_items"):
        raise PromptBuildError("参考用途规则必须是有界列表")
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"category", "purpose", "task", "relationship", "usage"}:
            raise PromptBuildError("参考用途规则必须包含类别、用途、任务名称、保留方式和使用说明")
        if any(not isinstance(v, str) or len(v) > limit or (k != "purpose" and not v.strip()) for k, v in rule.items()):
            raise PromptBuildError("参考用途规则必须为长度受限的文本；只有用途允许留空，表示精确匹配未声明用途")
        identity = (rule["category"], rule["purpose"])
        if identity in seen:
            raise PromptBuildError("参考用途规则的类别和用途不能重复")
        seen.add(identity)
    if not isinstance(value["task_separator"], str) or not value["task_separator"] or len(value["task_separator"]) > limit:
        raise PromptBuildError("任务名称分隔符必须为非空的有界文本")
    if not isinstance(value["unreferenced_task"], str) or not value["unreferenced_task"].strip() or len(value["unreferenced_task"]) > limit:
        raise PromptBuildError("无参考素材时的任务名称必须明确配置")
    for key, (_, allowed) in RENDERING_FIELDS.items():
        template = templates[key]
        if not isinstance(template, str) or len(template) > limit:
            raise PromptBuildError(f"渲染模板 {key} 必须为不超过 {limit} 字符的文本")
        try:
            fields = list(Formatter().parse(template))
        except ValueError as exc:
            raise PromptBuildError(f"渲染模板 {key} 的花括号格式无效") from exc
        for _, name, spec, conversion in fields:
            if name is not None and (name not in allowed or spec or conversion):
                raise PromptBuildError(f"渲染模板 {key} 仅允许目录中的简单占位符；不支持表达式或格式指令")
        if key == "entity_reference" and layout is not None and "source_labels" not in {name for _, name, _, _ in fields}:
            raise PromptBuildError("启用主体编号时，主体声明必须包含 {source_labels} 以说明真实素材来源")
        if key in {"reference", "entity_reference", "retention", "reference_usage", "shot", "reviewed_shot", "summary", "shot_opening", "shot_transition"} and not template.strip():
            raise PromptBuildError(f"必需的渲染模板 {key} 不能为空")
        if key in {"reference", "entity_reference", "retention", "reference_usage"} and not any(name == "label" for _, name, _, _ in fields):
            raise PromptBuildError("参考素材声明必须包含 {label}，以保留精确素材引用")
        required_facts = {"entity_reference": {"subject"}, "retention": {"relationship"},
                          "reference_usage": {"usage"}, "summary": {"task_types", "reference_relationships"},
                          "shot": {"shot_marker", "reference_usage"}, "reviewed_shot": {"shot_marker", "reference_usage"},
                          "shot_opening": {"index"}, "shot_transition": {"index", "timecode"}}
        missing = required_facts.get(key, set()) - {name for _, name, _, _ in fields}
        if missing:
            raise PromptBuildError(f"模板 {key} 必须引用 " + ", ".join("{" + name + "}" for name in sorted(missing)))
        if key in {"dialogue", "voiceover_dialogue"} and template and not any(name == "text" for _, name, _, _ in fields):
            raise PromptBuildError("对白模板必须包含 {text}")
    if not isinstance(value["separator"], str) or not value["separator"] or len(value["separator"]) > limit:
        raise PromptBuildError("渲染分隔符必须为非空且不超过字段上限的文本")
    if not isinstance(value["list_prefix"], str) or len(value["list_prefix"]) > limit:
        raise PromptBuildError("列表前缀必须为文本且不超过字段上限")
    if not isinstance(value["prompt_template_key"], str) or not value["prompt_template_key"].strip():
        raise PromptBuildError("必须明确填写本策略的提示词模板标识")
    if len(value["prompt_template_key"]) > limit:
        raise PromptBuildError("提示词模板标识超过字段上限")
    from studio.services.prompt_normalization_contract import PromptNormalizationContractError, validate_director_text_characters
    authored_texts = [*templates.values(), *(value[key] for key in (
        "separator", "list_prefix", "prompt_template_key", "task_separator", "unreferenced_task"))]
    authored_texts.extend(text for rule in rules for text in rule.values())
    if layout is not None:
        authored_texts.extend(layout.values())
    try:
        for text in authored_texts:
            validate_director_text_characters(text)
    except PromptNormalizationContractError as exc:
        raise PromptBuildError("参考渲染配置包含禁止的控制或 Unicode 格式字符") from exc
    return deepcopy(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _join_reference_text(separator, values):
    return separator.join(values)


def render_reference_template(template: str, names, facts: dict) -> str:
    """Render declared values verbatim; no language-specific shadow copy."""
    return template.format_map({name: _text(facts.get(name)) for name in names}).strip()


def _timecode(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def compile_reference_context(context: ReferenceContextFacts, rendering: dict, *, result_fields=None) -> dict:
    """Use canonical, reviewed facts. Templates never execute code or invent assets."""
    validate_reference_context_facts(context)
    policy = validate_rendering_policy(rendering)
    from studio.services.source_projection_primitives import SOURCE_PROJECTION_PRIMITIVES, reference_requires_units
    available = {primitive.result_field for primitive in SOURCE_PROJECTION_PRIMITIVES.values()}
    selected = available if result_fields is None else set(result_fields)
    if not selected or selected - available:
        raise PromptBuildError("参考组装必须选择已声明的原语输出")
    needs_units = reference_requires_units(prompt_type=context.prompt_type, result_fields=selected, rendering=policy)
    if needs_units and not context.ordered_units:
        raise PromptBuildError("所选参考输出需要明确的有序单元")
    separator = policy["separator"]
    from studio.services.reference_subject_labels import reference_subject_labels
    subject_labels = reference_subject_labels(context, policy)
    layout = policy.get("reference_layout")
    # Clip-local voice order is independent of project IDs and reference slots.
    # Same-time speech follows the explicit dialogue-list order.
    speaker_indices = {}
    for unit in context.ordered_units:
        for entry in sorted(unit.dialogue, key=lambda row: row["start_ms"]):
            if entry["speaker_id"]:
                speaker_indices.setdefault(entry["speaker_id"], len(speaker_indices) + 1)

    def render(key: str, **facts) -> str:
        return render_reference_template(policy["templates"][key], RENDERING_FIELDS[key][1], facts)

    references = deepcopy(list(context.reference_bindings))
    mode_contract = context.conditioning_mode_contract
    # An image Workflow explicitly has no conditioning mode; its frozen input
    # ports own reference quantities. Do not fabricate a video mode to render it.
    image_without_mode = context.prompt_type == "image" and context.mode == "" and mode_contract is None
    if not image_without_mode and (not isinstance(mode_contract, dict) or mode_contract.get("key") != context.mode):
        raise PromptBuildError("参考上下文必须使用与请求模式一致的冻结数量合同")
    durations = []
    for unit in context.ordered_units:
        duration = unit.duration_ms
        if type(duration) is not int or duration <= 0:
            raise PromptBuildError("参考上下文的每个镜头必须有明确的正整数时长")
        durations.append(duration)
    duration_seconds = sum(durations) / 1000
    if context.prompt_type == "video":
        if not isinstance(context.frame_policy, dict):
            raise PromptBuildError("视频参考上下文缺少冻结 frame_policy；不会使用模型专用默认帧数")
        try:
            duration_seconds = aligned_frames(sum(durations), context.frame_policy) / float(context.frame_policy["fps"])
        except (WorkflowBuildError, TypeError, ValueError, KeyError) as exc:
            raise PromptBuildError("视频参考上下文的冻结 frame_policy 无效") from exc

    fields = {"subject_definitions": [], "retention_analysis": [], "detailed_description": [],
              "overall_soundscape": "", "non_diegetic_music": ""}
    usage = {}
    entities = {(subject.category, subject.identity): subject for subject in context.subject_facts}
    for index, unit in enumerate(context.ordered_units, 1):
        for key in unit.subject_keys:
            usage.setdefault(key, []).append(index)

    labels = set()
    reference_facts = []
    declared_subjects = {}
    rule_index = {(rule["category"], rule["purpose"]): rule for rule in policy["reference_rules"]}
    def used_in(item, index):
        if "unit_keys" in item and context.ordered_units[index - 1].unit_key not in item["unit_keys"]:
            return False
        subject_category = item.get("entity_category", item.get("category"))
        return not item.get("entity_id") or index in usage.get((subject_category, str(item.get("entity_id"))), [])

    def record_reference(facts, item):
        identity = (facts["category"], facts["purpose"])
        if identity not in rule_index:
            raise PromptBuildError(f"参考用途未配置：{identity[0]} / {identity[1] or '未声明用途'}；不会猜测保留方式")
        rule = rule_index[identity]
        facts.update({key: rule[key] for key in ("task", "relationship", "usage")})
        reference_facts.append((item, facts))
        # Several physical sources may define one exact subject. Do not merge
        # contradictory roles or guess that two equal names are one identity.
        previous = declared_subjects.get(facts["label"])
        if previous is not None:
            if any(previous[key] != facts[key] for key in ("category", "purpose", "relationship", "usage", "guidance")):
                raise PromptBuildError("同一主体的参考用途不一致；请明确各来源职责后再组装")
            if previous["subject"] != facts["subject"]:
                raise PromptBuildError("同一主体的参考名称不一致；请核对来源投影的主体名称")
        else:
            declared_subjects[facts["label"]] = facts
            if "subject_definitions" in selected:
                fields["subject_definitions"].append(render("entity_reference" if facts["subject"] else "reference", **facts))
            if "retention_analysis" in selected:
                fields["retention_analysis"].append(render("retention", **facts))

    for item in references:
        label = item.get("input_label")
        if not isinstance(label, str) or not label or label in labels:
            raise PromptBuildError("参考上下文素材标签缺失或重复")
        labels.add(label)
        category = item.get("category", "")
        key = (item.get("entity_category", category), str(item.get("entity_id", "")))
        entity = entities.get(key)
        logical = subject_labels.get(key)
        description = separator.join(filter(None, entity.description_parts)) if entity else item.get("description", "")
        facts = {"label": logical["label"] if logical else label, "subject": item.get("entity_label", ""), "category": category,
                 "source_labels": layout["source_separator"].join(logical["source_labels"]) if logical else label,
                 "purpose": item.get("purpose", ""), "description": description,
                 "guidance": item.get("prompt_hint", ""),
                 "source_kind": "generated_artifact" if item.get("artifact_id") else "asset", "parent_label": "",
                 "shots": separator.join(str(index) for index in range(1, len(durations) + 1)
                    if (context.ordered_units[index - 1].unit_key in logical["unit_keys"] if logical else used_in(item, index)))}
        record_reference(facts, item)
        paired_label = item.get("audio_input_label")
        if paired_label:
            if not isinstance(paired_label, str) or paired_label in labels:
                raise PromptBuildError("配套音频标签无效或重复")
            labels.add(paired_label)
            record_reference({
                **facts, "label": paired_label, "subject": item.get("audio_use", {}).get("entity_label", ""),
                "source_labels": paired_label,
                "category": "audio", "purpose": item.get("audio_purpose", ""),
                "parent_label": label, "source_kind": "embedded_audio" if item.get("use_embedded_audio") else "paired_audio",
                "description": item.get("audio_description", ""), "guidance": item.get("audio_prompt_hint", ""),
            }, item)
    for group, template in (("subject_definitions", "empty_references"), ("retention_analysis", "empty_retention")):
        if group in selected and not fields[group]:
            empty = render(template)
            fields[group] = [empty] if empty else []

    consumed = set()
    def render_usage(facts, section):
        if layout is None:
            return render("reference_usage", **facts)
        return render_reference_template(layout[f"{section}_reference_template"], REFERENCE_FACTS, facts)

    def unique_reference_facts(rows):
        return list({row["label"]: row for row in rows}.values())

    sounds, music = [], []
    elapsed = 0
    for index, (unit, duration) in enumerate(zip(context.ordered_units, durations, strict=True), 1):
        if not selected.intersection({"detailed_description", "overall_soundscape", "non_diegetic_music"}):
            break
        canonical = unit.render_values
        if index == 1 and "detailed_description" in selected:
            style = render("style", visual_prompt=canonical["visual_prompt"], color_script=context.color_script)
            if style:
                fields["detailed_description"].append(style)

        def contribution(selector, template, placeholder, raw_value):
            key = (unit.unit_key, selector)
            if key not in context.generation_contributions:
                return raw_value
            if selector in {"shot.sound_effects", "shot.music"} and not context.include_audio:
                raise PromptBuildError(f"当前合同不输出音频，不能丢弃已审核的 {selector} 贡献")
            placeholders = {name for _, name, _, _ in Formatter().parse(policy["templates"][template])}
            if placeholder not in placeholders:
                raise PromptBuildError(f"模板 {template} 未引用 {{{placeholder}}}，不能丢弃已审核的 {selector} 贡献")
            consumed.add(key)
            return context.generation_contributions[key]

        if "detailed_description" in selected:
            shot_template = "reviewed_shot" if (unit.unit_key, "shot.detailed_description") in context.generation_contributions else "shot"
            description = contribution("shot.detailed_description", shot_template, "description", canonical["description"])
            dialogue_text = []
            if context.include_audio:
                if unit.dialogue and "dialogue" not in {name for _, name, _, _ in Formatter().parse(policy["templates"][shot_template])}:
                    raise PromptBuildError("当前逐镜模板未引用 {dialogue}，不能丢弃镜头对白")
                for dialogue_index, entry in enumerate(unit.dialogue):
                    template = "voiceover_dialogue" if entry["voiceover"] else "dialogue"
                    if not policy["templates"][template].strip():
                        raise PromptBuildError("当前镜头包含对白，不能使用空的对白模板")
                    required = {name for _, name, _, _ in Formatter().parse(policy["templates"][template])}
                    language = entry.get("language", "")
                    if "language" in required and not language.strip():
                        raise PromptBuildError(f"单元 {unit.unit_key} 的 dialogue[{dialogue_index}].language 未声明；"
                            "当前对白模板需要本句语言，不能用项目语言或导演输出语言代替")
                    speaker_label = ""
                    if "speaker_reference_label" in required:
                        key = tuple(entry.get("speaker_subject_key", ()))
                        binding = subject_labels.get(key)
                        if binding is None or unit.unit_key not in binding["unit_keys"]:
                            raise PromptBuildError("对白模板需要发言人参考标签，但缺少本单元确切主体与参考素材的绑定")
                        speaker_label = binding["label"]
                    if "speaker_index" in required and not entry["speaker_id"].strip():
                        raise PromptBuildError("对白模板需要本次发言序号，但未提供稳定说话人标识")
                    dialogue_text.append(render(template, **{**entry, "language": language,
                        "speaker_reference_label": speaker_label, "speaker_index": speaker_indices.get(entry["speaker_id"]),
                        "start_timecode": _timecode(elapsed + entry["start_ms"]),
                        "end_timecode": _timecode(elapsed + entry["end_ms"])}))
            used_facts = unique_reference_facts(facts for item, facts in reference_facts if used_in(item, index))
            used_labels = [facts["label"] for facts in used_facts]
            voices = separator.join(filter(None, unit.voice_profiles)) if context.include_audio else ""
            marker = render("shot_opening" if index == 1 else "shot_transition", index=index,
                            timecode=_timecode(elapsed), start_seconds=elapsed / 1000)
            fields["detailed_description"].append(render(shot_template, index=index, start_seconds=elapsed / 1000,
                timecode=_timecode(elapsed), shot_marker=marker,
                reference_usage=_join_reference_text(separator, filter(None, (render_usage(facts, "shot") for facts in used_facts))),
                duration_seconds=duration / 1000, narrative=canonical["narrative"], description=description,
                action=canonical["action"], camera=canonical["camera"],
                dialogue=_join_reference_text(separator, dialogue_text), references=separator.join(used_labels),
                subjects=canonical["subjects"], environment=canonical["environment"],
                lighting=canonical["lighting"], color_palette=canonical["color_palette"],
                voice_profiles=voices, continuity=unit.continuity_notes,
                requires_first_frame_continuity=unit.requires_first_frame_continuity,
                requires_last_frame_continuity=unit.requires_last_frame_continuity,
                subject_labels=separator.join(dict.fromkeys((facts["label"] if layout else facts["subject"]) for facts in used_facts if facts["subject"]))))
        if "overall_soundscape" in selected:
            sound = contribution("shot.sound_effects", "sound", "description", canonical["sound_effects"])
            if sound:
                sounds.append(render("sound", index=index, description=sound))
            ambient = unit.ambient_sound
            if ambient and ambient != sound:
                sounds.append(render("sound", index=index, description=ambient))
        if "non_diegetic_music" in selected:
            score = contribution("shot.music", "music", "description", canonical["music"])
            if score:
                music.append(render("music", index=index, description=score))
        elapsed += duration
    if "detailed_description" in selected and context.extra_direction:
        direction = render("direction", description=context.extra_direction)
        if not direction:
            raise PromptBuildError("存在全局导演要求，但未配置对应渲染模板")
        fields["detailed_description"].append(direction)
    overall = context.overall_soundscape
    background = context.non_diegetic_music
    if "overall_soundscape" in selected:
        fields["overall_soundscape"] = (_join_reference_text(separator, dict.fromkeys(filter(None, [overall, *sounds])))
                                       if context.include_audio else render("silent_sound"))
    if "non_diegetic_music" in selected:
        fields["non_diegetic_music"] = (_join_reference_text(separator, dict.fromkeys(filter(None, [background, *music])))
                                      if context.include_audio else render("silent_music"))
    if "summary" in selected:
        fields["summary"] = render("summary", mode=context.mode, shot_count=len(durations),
            duration_seconds=f"{duration_seconds:.3f}".rstrip("0").rstrip("."), reference_count=len(labels), include_audio=context.include_audio,
            task_types=policy["task_separator"].join(dict.fromkeys(facts["task"] for _, facts in reference_facts)) if reference_facts else policy["unreferenced_task"],
            reference_relationships=_join_reference_text(separator, filter(None, (render_usage(facts, "summary")
                for facts in unique_reference_facts(facts for _, facts in reference_facts)))))
    fields = {key: value for key, value in fields.items() if key in selected}
    fields = {key: [str(item) for item in value] if isinstance(value, list) else str(value)
        for key, value in fields.items()}
    return {"fields": fields, "consumed_contributions": frozenset(consumed),
            "execution_facts": {"effective_conditioning_mode": context.mode,
                                "prompt_template_key": policy["prompt_template_key"],
                                **({"reference_subject_bindings": list(subject_labels.values())} if subject_labels else {})}}


def reference_context_advisories(context, assembly: dict, config: dict) -> list[str]:
    """Diagnostics only; absence disables a hint, never an integrity check."""
    if config.get("short_shot_action_density") or config.get("motion_camera_reference_guidance"):
        if context is None:
            raise PromptBuildError("配置的质量建议需要明确的参考单元事实")
        validate_reference_context_facts(context)
    warnings = []
    density = config.get("short_shot_action_density")
    if density:
        for unit in context.ordered_units:
            duration = unit.duration_ms
            action = " ".join(dict.fromkeys(value for value in (unit.render_values["narrative"], unit.render_values["action"]) if value))
            if duration <= density["maximum_duration_ms"] and (
                len(action) > density["maximum_action_characters"]
                or len(re.findall(r"[,，;；]", action)) >= density["minimum_clause_separators"]
            ):
                warnings.append(f"单元 {unit.unit_key} 的动作密度超过本策略建议值，可拆分单元或减少同时动作。")
    overlap = config.get("positive_negative_token_overlap")
    if overlap:
        channels = assembly.get("channels") or {}
        tokens = lambda value: set(re.findall(r"[a-z]+(?:[-'][a-z]+)?", value.lower()))
        positive = tokens(channels.get("positive_prompt", ""))
        negative = tokens(channels.get("negative_prompt", ""))
        shared = positive & negative
        if len(shared) >= overlap["minimum_shared_tokens"] and len(shared) / max(1, len(negative)) >= overlap["minimum_overlap_ratio"]:
            warnings.append("正负提示词重叠超过本策略建议值，请检查是否把正向剧情复制到了负面约束。")
    motion = config.get("motion_camera_reference_guidance")
    if motion:
        generic = {value.strip(" .").casefold() for value in motion["generic_values"]}
        for item in context.reference_bindings:
            if item.get("category") != "video" or item.get("purpose") != "motion_camera_reference":
                continue
            hint = str(item.get("prompt_hint") or item.get("description") or "").strip(" .").casefold()
            if hint in generic or len(re.findall(r"[a-z]+(?:[-'][a-z]+)?", hint)) < motion["minimum_english_words"]:
                warnings.append(f"{item.get('input_label', '')} 的运镜参考说明未达到本策略建议值，请补充实际借用的运动或节奏。")
    return warnings
