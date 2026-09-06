"""Model-neutral reference facts rendered by an explicit, frozen template policy.

Templates can interpolate named scalar facts only: no expressions, attributes,
indexes, conversions, recursive expansion or executable template language.
"""
from __future__ import annotations

from copy import deepcopy
from string import Formatter
import json
import re

from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.workflow import aligned_frames, WorkflowBuildError
from studio.services.security_boundaries import boundary_int


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
RENDERING_FIELDS["dialogue"] = ("对白", (*RENDERING_FIELDS["dialogue"][1], "subject", "voice_profile"))
RENDERING_FIELDS["voiceover_dialogue"] = ("画外音对白", RENDERING_FIELDS["dialogue"][1])
REFERENCE_FACTS = (*RENDERING_FIELDS["reference"][1], "relationship", "usage", "shots")
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
    if not isinstance(value, dict) or set(value) != {"templates", "separator", "list_prefix", "prompt_template_key", "reference_rules", "task_separator", "unreferenced_task"}:
        raise PromptBuildError("参考上下文缺少完整渲染配置；请创建后继草稿并配置模板、分隔符和模板标识")
    templates = value["templates"]
    if not isinstance(templates, dict) or set(templates) != set(RENDERING_FIELDS):
        raise PromptBuildError("参考上下文模板字段必须与服务端目录一致")
    limit = boundary_int("security.prompt_profile.field_text_max_characters")
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
    return deepcopy(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def render_reference_template(template: str, names, facts: dict) -> str:
    """Shared scalar replacement after policy validation; no recursive expansion."""
    return template.format_map({name: _text(facts.get(name)) for name in names}).strip()


def _timecode(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def compile_reference_context(context, rendering: dict) -> dict:
    """Use canonical, reviewed facts. Templates never execute code or invent assets."""
    from studio.services.prompt_profiles import media_profile_sources, _translated_media_value, resolved_media_override
    from studio.services.dialogue import validate_dialogue_timing
    from studio.services.visual_reference_policy import character_requires_visual_reference

    policy = validate_rendering_policy(rendering)
    separator = policy["separator"]

    def render(key: str, **facts) -> str:
        return render_reference_template(policy["templates"][key], RENDERING_FIELDS[key][1], facts)

    def translated(scope, entity, attribute):
        return _translated_media_value(context.project, scope_type=scope, scope_id=entity.id,
            field_key=f"{scope}.{attribute}", source=getattr(entity, attribute, ""))

    references = deepcopy(list(context.reference_bindings))
    mode_contract = context.conditioning_mode_contract
    if not isinstance(mode_contract, dict) or mode_contract.get("key") != context.mode:
        raise PromptBuildError("参考上下文必须使用与请求模式一致的冻结数量合同")
    if any(shot.episode.project_id != context.project.id for shot in context.shots):
        raise PromptBuildError("参考上下文的分镜必须属于当前项目")
    durations = []
    for shot in context.shots:
        duration = context.duration_overrides.get(str(shot.id), shot.duration_ms)
        if type(duration) is not int or duration <= 0:
            raise PromptBuildError("参考上下文的每个镜头必须有明确的正整数时长")
        durations.append(duration)
    if not durations:
        raise PromptBuildError("参考上下文至少需要一个镜头")
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
    usage, entities = {}, {}
    for index, shot in enumerate(context.shots, 1):
        for category, rows in (("character", shot.characters.all()), ("prop", shot.props.all()),
                               ("scene", [shot.scene] if shot.scene_id else [])):
            for entity in rows:
                key = (category, str(entity.id))
                entities[key] = entity
                usage.setdefault(key, []).append(index)

    labels = set()
    reference_facts = []
    rule_index = {(rule["category"], rule["purpose"]): rule for rule in policy["reference_rules"]}
    def used_in(item, index):
        return not item.get("entity_id") or index in usage.get((item.get("category"), str(item.get("entity_id"))), [])

    def record_reference(facts, item):
        identity = (facts["category"], facts["purpose"])
        if identity not in rule_index:
            raise PromptBuildError(f"参考用途未配置：{identity[0]} / {identity[1] or '未声明用途'}；不会猜测保留方式")
        rule = rule_index[identity]
        facts.update({key: rule[key] for key in ("task", "relationship", "usage")})
        reference_facts.append((item, facts))
        fields["subject_definitions"].append(render("entity_reference" if facts["subject"] else "reference", **facts))
        fields["retention_analysis"].append(render("retention", **facts))

    for item in references:
        label = item.get("input_label")
        if not isinstance(label, str) or not label or label in labels:
            raise PromptBuildError("参考上下文素材标签缺失或重复")
        labels.add(label)
        category = item.get("category", "")
        key = (category, str(item.get("entity_id", "")))
        entity = entities.get(key)
        attributes = {"character": ("visual_identity", "wardrobe", "body_language"),
                      "scene": ("visual_identity", "location", "time_of_day", "lighting", "color_palette"),
                      "prop": ("visual_identity", "material", "distinctive_marks", "continuity_state")}.get(category, ())
        description = separator.join(filter(None, (translated(category, entity, attr) for attr in attributes))) if entity else item.get("description", "")
        facts = {"label": label, "subject": item.get("entity_label", ""), "category": category,
                 "purpose": item.get("purpose", ""), "description": description,
                 "guidance": item.get("prompt_hint", ""), "source_kind": "asset", "parent_label": "",
                 "shots": separator.join(str(index) for index in range(1, len(durations) + 1) if used_in(item, index))}
        record_reference(facts, item)
        paired_label = item.get("audio_input_label")
        if paired_label:
            if not isinstance(paired_label, str) or paired_label in labels:
                raise PromptBuildError("配套音频标签无效或重复")
            labels.add(paired_label)
            record_reference({
                **facts, "label": paired_label, "subject": "", "category": "audio", "purpose": item.get("audio_purpose", ""),
                "parent_label": label, "source_kind": "embedded_audio" if item.get("use_embedded_audio") else "paired_audio",
                "description": item.get("audio_description", ""), "guidance": item.get("audio_prompt_hint", ""),
            }, item)
    for group, template in (("subject_definitions", "empty_references"), ("retention_analysis", "empty_retention")):
        if not fields[group]:
            empty = render(template)
            fields[group] = [empty] if empty else []

    consumed = set()
    sounds, music = [], []
    elapsed = 0
    for index, (shot, duration) in enumerate(zip(context.shots, durations, strict=True), 1):
        canonical = media_profile_sources(project=context.project, shots=[shot], include_audio=context.include_audio,
            effective_durations_ms={str(shot.id): duration}, reference_bindings=references)
        if index == 1:
            color_script = _translated_media_value(context.project, scope_type="project", scope_id=context.project.id,
                field_key="project.production_settings.color_script",
                source=(context.project.production_settings or {}).get("color_script", ""))
            style = render("style", visual_prompt=canonical["style.visual_prompt"], color_script=color_script)
            if style:
                fields["detailed_description"].append(style)

        def contribution(selector, template, placeholder):
            key = (str(shot.id), selector)
            if key not in context.generation_contributions:
                return canonical[selector]
            if selector in {"shot.sound_effects", "shot.music"} and not context.include_audio:
                raise PromptBuildError(f"当前合同不输出音频，不能丢弃已审核的 {selector} 贡献")
            placeholders = {name for _, name, _, _ in Formatter().parse(policy["templates"][template])}
            if placeholder not in placeholders:
                raise PromptBuildError(f"模板 {template} 未引用 {{{placeholder}}}，不能丢弃已审核的 {selector} 贡献")
            consumed.add(key)
            return context.generation_contributions[key]

        shot_template = "reviewed_shot" if (str(shot.id), "shot.detailed_description") in context.generation_contributions else "shot"
        description = contribution("shot.detailed_description", shot_template, "description")
        sound = contribution("shot.sound_effects", "sound", "description")
        score = contribution("shot.music", "music", "description")
        dialogue_text = []
        if context.include_audio:
            if shot.dialogue and "dialogue" not in {name for _, name, _, _ in Formatter().parse(policy["templates"][shot_template])}:
                raise PromptBuildError("当前逐镜模板未引用 {dialogue}，不能丢弃镜头对白")
            for dialogue_index, entry in enumerate(validate_dialogue_timing(shot.dialogue or [], duration_ms=duration)):
                speaker = next((row for row in shot.characters.all() if row.stable_speaker_id == entry["speaker_id"]), None)
                is_voiceover = entry.get("voiceover", False) or (speaker is not None and not character_requires_visual_reference(speaker))
                template = "voiceover_dialogue" if is_voiceover else "dialogue"
                if not policy["templates"][template].strip():
                    raise PromptBuildError("当前镜头包含对白，不能使用空的对白模板")
                delivery = _translated_media_value(context.project, scope_type="shot", scope_id=shot.id,
                    field_key=f"shot.dialogue.{dialogue_index}.delivery", source=entry.get("delivery", ""))
                subject = next((item.get("entity_label", "") for item in references if speaker and item.get("category") == "character" and item.get("entity_id") == str(speaker.id)), "")
                dialogue_text.append(render(template, **{**entry, "delivery": delivery, "language": context.project.language,
                    "subject": subject, "voice_profile": translated("character", speaker, "voice_profile") if speaker else "",
                    "start_ms": entry.get("start_ms", 0), "end_ms": entry.get("end_ms", duration), "voiceover": is_voiceover}))
        used_facts = [facts for item, facts in reference_facts if used_in(item, index)]
        used_labels = [facts["label"] for facts in used_facts]
        voices = separator.join(filter(None, (translated("character", row, "voice_profile")
                      for row in shot.characters.all()))) if context.include_audio else ""
        marker = render("shot_opening" if index == 1 else "shot_transition", index=index,
                        timecode=_timecode(elapsed), start_seconds=elapsed / 1000)
        fields["detailed_description"].append(render(shot_template, index=index, start_seconds=elapsed / 1000,
            timecode=_timecode(elapsed), shot_marker=marker,
            reference_usage=separator.join(render("reference_usage", **facts) for facts in used_facts),
            duration_seconds=duration / 1000, narrative=canonical["shot.narrative"], description=description,
            action=canonical["shot.action"], camera=canonical["shot.camera"],
            dialogue=separator.join(dialogue_text), references=separator.join(used_labels),
            subjects=canonical["request.subject"], environment=canonical["request.environment"],
            lighting=canonical["request.lighting"], color_palette=canonical["request.color_palette"],
            voice_profiles=voices, continuity=translated("shot", shot, "continuity_notes"),
            requires_first_frame_continuity=shot.requires_first_frame_continuity,
            requires_last_frame_continuity=shot.requires_last_frame_continuity,
            subject_labels=separator.join(dict.fromkeys(facts["subject"] for facts in used_facts if facts["subject"]))))
        if sound:
            sounds.append(render("sound", index=index, description=sound))
        ambient = translated("scene", shot.scene, "ambient_sound") if shot.scene_id else ""
        if ambient and ambient != sound:
            sounds.append(render("sound", index=index, description=ambient))
        if score:
            music.append(render("music", index=index, description=score))
        elapsed += duration
    if context.extra_direction:
        direction = render("direction", description=context.extra_direction)
        if not direction:
            raise PromptBuildError("存在全局导演要求，但未配置对应渲染模板")
        fields["detailed_description"].append(direction)
    overall = resolved_media_override(context.project, context.shots, "sound_effects", context.overall_soundscape)
    background = resolved_media_override(context.project, context.shots, "music", context.non_diegetic_music)
    fields["overall_soundscape"] = (separator.join(dict.fromkeys(filter(None, [overall, *sounds])))
                                   if context.include_audio else render("silent_sound"))
    fields["non_diegetic_music"] = (separator.join(dict.fromkeys(filter(None, [background, *music])))
                                  if context.include_audio else render("silent_music"))
    fields["summary"] = render("summary", mode=context.mode, shot_count=len(durations),
        duration_seconds=f"{duration_seconds:.3f}".rstrip("0").rstrip("."), reference_count=len(labels), include_audio=context.include_audio,
        task_types=policy["task_separator"].join(dict.fromkeys(facts["task"] for _, facts in reference_facts)) if reference_facts else policy["unreferenced_task"],
        reference_relationships=separator.join(render("reference_usage", **facts) for _, facts in reference_facts))
    return {"fields": fields, "consumed_contributions": frozenset(consumed),
            "execution_facts": {"effective_conditioning_mode": context.mode,
                                "prompt_template_key": policy["prompt_template_key"]}}


def reference_context_advisories(context, assembly: dict, config: dict) -> list[str]:
    """Diagnostics only; absence disables a hint, never an integrity check."""
    warnings = []
    density = config.get("short_shot_action_density")
    if density:
        for shot in context.shots:
            duration = context.duration_overrides.get(str(shot.id), shot.duration_ms)
            action = " ".join(dict.fromkeys(value for value in (shot.narrative, shot.action) if value))
            if duration <= density["maximum_duration_ms"] and (
                len(action) > density["maximum_action_characters"]
                or len(re.findall(r"[,，;；]", action)) >= density["minimum_clause_separators"]
            ):
                warnings.append(f"镜头 {shot.number} 的动作密度超过本策略建议值，可拆分镜头或减少同时动作。")
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
