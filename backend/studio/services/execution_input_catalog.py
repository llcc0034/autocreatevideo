"""Schema-owned identity/scope choices shared by input authoring and readers.

These are platform fact kinds, not model purposes, inferred media roles or
permissions. A selected kind never authorizes a particular project or Asset.
Purpose keys must additionally come from the selected target/strategy catalog.
"""


from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import models
from studio.services.json_contracts import canonical_json_bytes


class ExecutionSubjectKind(models.TextChoices):
    CHARACTER = "character", "人物"
    SCENE = "scene", "场景"
    PROP = "prop", "道具"
    EXTERNAL = "external", "本次提供的主体"


class ExecutionScopeKind(models.TextChoices):
    REQUEST = "request", "本次请求"
    SHOT = "shot", "真实镜头"
    EPISODE = "episode", "真实剧集"
    PROJECT = "project", "真实项目"


class ExecutionSubjectCategory(models.TextChoices):
    CHARACTER = "character", "人物"
    SCENE = "scene", "场景"
    PROP = "prop", "道具"


def execution_subject_category_options():
    """Semantic categories, never project identities or rendering permission."""
    return [{"value": key, "label": label} for key, label in ExecutionSubjectCategory.choices]


EXECUTION_SUBJECT_KINDS = dict(ExecutionSubjectKind.choices)
EXECUTION_SCOPE_KINDS = dict(ExecutionScopeKind.choices)


def _reference_context_form(target):
    """Describe the existing per-unit protocol, without rewriting its contract."""
    if target.get("value_domain") != "unit_context":
        return {}
    from studio.models import PromptProfileFieldRule
    choices = dict(PromptProfileFieldRule.GenerationContextSelector.choices)
    selectors = list(dict.fromkeys(row["selector"] for row in target.get("fields", [])
        if row.get("usage") == "reference_generation_context"))
    if not selectors or any(key not in choices for key in selectors):
        return {}
    label = "、".join(choices[key] for key in selectors)
    return {"label": "逐单元辅助上下文 · " + label,
        "description": "以有序单元 reference_context.units 中每项 unit_key（本次单元标识）为属性名，"
            "逐项提供对应单元的" + label + "；属性名必须与单元一一对应，不能遗漏或多填。"
            "这些内容供策略的辅助字段生成使用，不是最终提示词。"
            "独立烟测填写的是本次测试事实，不代表项目已审核；生产中的真实上下文由已配置来源读取并冻结。"
            "仅填写此标识不会自动读取项目，也不会自动采用输出。"}


def execution_input_form_options(definition):
    """Read-only form metadata derived from exact targets and registered protocols.

    This is not a new permission, source resolver or a mutable contract overlay.
    Schema constraints from every mapped target are retained, not first-wins.
    """
    from studio.services.execution_native_text import native_text_input_forms
    interface = definition["interface"]
    native = native_text_input_forms(interface.get("output", {}).get("adapter"))
    from studio.services.execution_input_structures import input_structure_schemas
    input_schemas = input_structure_schemas(definition)
    result = {}
    for item in definition["configuration"]["inputs"]:
        labels, descriptions, schemas, references = [], [], [], []
        if item["key"] in input_schemas:
            logical_schema = input_schemas[item["key"]]
            schemas.append(deepcopy(logical_schema))
            labels.append(logical_schema["title"])
            descriptions.append(logical_schema["description"])
        literal_allowed = item["kind"] == "text"
        for mapping in sorted(item["targets"], key=lambda row: not row["is_primary"]):
            group = "profile_inputs" if mapping["target_kind"] == "profile_input" else "input_targets"
            target = interface.get(group, {}).get(mapping["target_key"], {})
            registered = native.get(mapping["target_key"], {}) if mapping["target_kind"] == "operation_input" else {}
            if mapping["target_kind"] == "profile_input":
                from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS
                registered = {
                    "request.theme": {"label": "一句话梗概", "project_reference": "project.logline",
                        "description": "按已配置策略使用本次内容。可预览并引用项目梗概，再明确确认；不会自动读取剧本。"},
                    "request.style": {"label": "视觉风格", "project_reference": "project.visual_style",
                        "description": "按已配置策略使用本次风格要求。引用项目风格时仅取视觉提示词，不附加负面词或人物库。"},
                    "request.subject": {"label": "本次生成主体描述",
                        "description": "Bible 参考图按本轮明确对象预填已审核描述，可人工修改；只保存本次副本，不改剧本或原设定。"},
                }.get(mapping["target_key"], {})
                if mapping["target_key"] == "request.subject":
                    from studio.services.execution_bible_suggestions import BIBLE_INPUT_REFERENCES
                    references.extend(key for key in BIBLE_INPUT_REFERENCES if key not in references)
                if mapping["target_key"] in VISUAL_RULE_SOURCE_FIELDS:
                    registered = {"label": VISUAL_RULE_SOURCE_FIELDS[mapping["target_key"]][1],
                        "description": "来自本轮人工审核或配方授权自动采用的影视规则；引用精确采用版本。未采用或已变化时不可引用，不自动复制整份剧本；自动采用不等于人工质量审核。"}
                if target.get("value_domain") == "unit_context":
                    registered = _reference_context_form(target)
            if "source_kinds" in target and "literal" not in target["source_kinds"]:
                literal_allowed = False
            if registered.get("project_reference") and registered["project_reference"] not in references:
                references.append(registered["project_reference"])
            schema = target.get("value_schema")
            if isinstance(schema, dict) and all(canonical_json_bytes(schema) != canonical_json_bytes(existing) for existing in schemas):
                schemas.append(deepcopy(schema))
            if registered.get("schema") and all(canonical_json_bytes(registered["schema"]) != canonical_json_bytes(existing) for existing in schemas):
                schemas.append(deepcopy(registered["schema"]))
            label = target.get("label") or (schema or {}).get("title") or registered.get("label")
            description = target.get("description") or (schema or {}).get("description") or registered.get("description")
            if label and label not in labels:
                labels.append(label)
            if description and description not in descriptions:
                descriptions.append(description)
        from studio.services.execution_input_suggestions import suggestion_options
        from studio.services.execution_project_sources import input_project_reader, PROJECT_SOURCE_READERS
        reader = input_project_reader(item, interface)
        if reader:
            source = PROJECT_SOURCE_READERS[reader["selector"]]
            result[item["key"]] = {"label": " / ".join(labels) if labels else source["label"], "schemas": schemas,
                "description": " ".join(descriptions) + f" 来自{source['location']}。选择项目并预览确认后冻结；不自动读取最新内容，不接受手工冒充项目事实。",
                "suggestions": [{"key": reader["selector"], "label": "预览并引用" + source["label"], "requires_project": True}]}
            continue
        result[item["key"]] = {"label": labels[0] if labels else item["key"],
            "description": " ".join(descriptions), "schemas": schemas,
            "suggestions": suggestion_options(references, schemas) if literal_allowed else []}
    return result


def execution_language_form_options(definition):
    """Explain the exact profile's language dependencies, without changing it.

    These are request-wide language contexts, not per-input translation or an
    output-language override. Only mapped policy fields use the declaration.
    """
    from studio.models import PromptProfileVersion
    from studio.services.prompt_profiles import prompt_language_options, verify_prompt_profile_contract

    result = {"choices": prompt_language_options(), "allow_custom": True,
        "source": {"required": False, "fields": []}, "target": {"required": False, "fields": []},
        "fixed_fields": []}
    prompt = definition["interface"]["prompt"]
    if prompt["mode"] != "profile":
        return result
    profile = PromptProfileVersion.objects.get(pk=prompt["profile_id"])
    contract = verify_prompt_profile_contract(profile)
    if profile.contract_sha256 != prompt["profile_sha256"]:
        raise ValidationError("语言说明与实例的精确策略不一致，请重新读取合同")
    forms = execution_input_form_options(definition)
    for rule in contract["rules"]:
        policy = rule["language_policy"]
        if policy == "any":
            continue
        mapped = []
        for item in definition["configuration"]["inputs"]:
            if any(rule["field_key"] in [field["field_key"] for field in definition["interface"].get(
                    "profile_inputs", {}).get(target["target_key"], {}).get("fields", [])
                    if field["usage"] in {"assembly_source", "generation_source", "reference_units"}]
                    for target in item["targets"] if target["target_kind"] == "profile_input"):
                mapped.append(forms[item["key"]]["label"])
        label = "、".join(dict.fromkeys(mapped)) or rule["field_definition"]["label"]
        if policy in {"preserve_source", "inherit"}:
            entry = result["source" if policy == "preserve_source" else "target"]
            if label not in entry["fields"]:
                entry["fields"].append(label)
            entry["required"] = entry["required"] or bool(rule["required"]
                or rule["generation_required"] or "non_empty" in rule["generation_validator_keys"]
                or rule["generation_mode"] in {"model_generated", "translate_only"})
        else:
            result["fixed_fields"].append({"label": label, "language": next(
                (item["label"] for item in result["choices"] if item["value"] == policy), policy)})
    return result


def shot_structure_fields():
    from studio.services.dialogue import DIALOGUE_ALLOWED_KEYS, dialogue_language_schema
    dialogue = {key: {"type": "integer" if key in {"start_ms", "end_ms"} else "boolean" if key == "voiceover" else "string"}
        for key in sorted(DIALOGUE_ALLOWED_KEYS)}
    dialogue["language"] = dialogue_language_schema()
    return {"duration_ms": ("镜头时长（毫秒）", {"type": "integer", "minimum": 1}),
        "requires_first_frame_continuity": ("首帧连续要求", {"type": "boolean"}),
        "requires_last_frame_continuity": ("尾帧连续要求", {"type": "boolean"}),
        "dialogue": ("有序对白", {"type": "array", "items": {"type": "object", "properties": dialogue}})}


def linked_shot_fields():
    from studio.services.reference_context_facts import speaker_subject_key_schema
    dialogue = deepcopy(shot_structure_fields()["dialogue"][1])
    dialogue["items"]["properties"].update(subject={"type": "string", "title": "说话人姓名"},
        voice_profile={"type": "string", "title": "说话人声音设定"},
        speaker_subject_key=speaker_subject_key_schema())
    return [("linked_bible", "镜头关联的已审核 Bible", {"type": "object"}),
        ("dialogue_with_speakers", "有序对白与已审核说话人设定", dialogue)]


def bound_context_source_keys(option):
    return {"kind", "selector_key", "scope", "content_revision_id"} | (
        {"related_revision_ids"} if option.get("revision_dependencies") else set())


def execution_context_field_options():
    """Only implemented, versioned context readers; never arbitrary JSONPath."""
    from studio.services.production_creation_inputs import creation_input_options
    from studio.services.production_reviewed_content import BIBLE_PRIMARY_FIELDS
    from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS, reviewed_visual_source_reader
    return [*creation_input_options(), *[{"selector_key": key, "label": label, "kind": "text", "scope_kind": "project",
        "revision_kind": "visual_rules_approval", "required_stage_key": "visual_rules_review",
        "source_reader": reviewed_visual_source_reader(key),
        "description": "从本轮已审核影视规则读取单项文字，冻结原任务、候选、审核决定与摘要；未审核或现值已变化时拒绝。"}
        for key, (_, label) in VISUAL_RULE_SOURCE_FIELDS.items()],
        {"selector_key": "script.content", "label": "已确认剧本全文", "kind": "text",
        "scope_kind": "project", "revision_kind": "script_revision",
        "description": "选择真实项目与已确认剧本修订；冻结正文及来源，不追随最新修订。"},
        {"selector_key": "script.visual_context", "label": "已确认剧本与项目影视上下文", "kind": "json",
        "scope_kind": "project", "revision_kind": "script_revision",
        "description": "冻结所选剧本全文，以及同一项目当前的风格、影视规则和已物化 Bible；不自动采用生成结果。"},
        {"selector_key": "script.project_style", "label": "所选剧本项目的当前风格", "kind": "json",
        "scope_kind": "project", "revision_kind": "script_revision",
        "description": "沿所选已确认剧本定位真实项目，只读取该项目当前关联的风格。准备时冻结；这不是风格审核证明，未关联风格时返回空值，不填默认风格。"},
        # Creative values are exact shot_field nodes in the shared structure
        # mapping. Never expose the old columns or a whole-Shot JSON shortcut.
        *[{"selector_key": f"shot.{key}", "label": f"当前已审核{label}", "kind": "json",
            "scope_kind": "shot", "revision_kind": "storyboard_approval", "value_schema": schema,
            "description": "从原分镜审核按当前镜头读取；字段缺失不会自动补值，对白保持原顺序。"}
            for key, (label, schema) in shot_structure_fields().items()],
        *[{"selector_key": f"shot.{key}", "label": label, "kind": "json", "scope_kind": "shot",
            "revision_kind": "storyboard_approval", "value_schema": schema,
            "revision_dependencies": [{"key": "bible", "revision_kind": "bible_approval", "stage_key": "bible_review"}],
            "description": "同时冻结本轮分镜与Bible审核；关联实体按明确出镜/发声关系读取并保留角色和位置，含原审核确切主图正文或未建立的null，不含旧人物池/素材绑定。对白按原行发言者身份读取姓名和声音，核对原speaker_id，不按名字重选人物或补写声音设定。"}
            for key, label, schema in linked_shot_fields()],
        *[{"selector_key": f"production.entity.{key}", "label": f"当前已审核实体{label}", "kind": kind,
            "scope_kind": "request", "revision_kind": "bible_approval",
            "description": "按本次明确目标读取本轮已审核Bible；整体内容含确切主图的共同身份/基础外观或未建立的null，不含旧视觉身份和素材绑定。"}
            for key, label, kind in (("content", "创作内容", "json"), ("description", "说明", "text"))],
        *[{"selector_key": f"production.entity.primary.{field}", "label": f"当前已审核{label}", "kind": "text",
            "scope_kind": "request", "revision_kind": "bible_approval",
            "description": "按本次实体和原Bible审核中的确切主图修订读取；不读取旧实体视觉字段，不自动改选主图或注入另一段锚点。"}
            for field, label in BIBLE_PRIMARY_FIELDS.items()]]


def execution_text_output_options(stages=()):
    """Business projections plus exact configured step outputs; never latest."""
    from studio.services.execution_text_output_catalog import structured_text_output_options
    return [*[{"output_key": f"{role}.{selector}", "stage_key": stage, "artifact_role": role,
        "kind": kind, "scope_kind": "project", "context_selector": selector, "label": label}
        for stage, role in (("script_generate", "generated_script"), ("script_structure", "structured_script"))
        for selector, kind, label in (("script.content", "text", "已采用剧本全文"),
            ("script.visual_context", "json", "已采用剧本与项目影视上下文"),
            ("script.project_style", "json", "已采用剧本项目的当前风格"))],
        *structured_text_output_options(stages)]


def execution_input_identity_options():
    return {
        "subject_kinds": [{"value": key, "label": label} for key, label in EXECUTION_SUBJECT_KINDS.items()],
        "scope_kinds": [{"value": key, "label": label} for key, label in EXECUTION_SCOPE_KINDS.items()],
    }


def execution_input_purpose_options(resources, input_targets):
    """Project media purposes from the exact model-mode contract, not names.

    This covers the existing model topology's video/audio declarations. Other
    kinds do not acquire invented purpose keys. Reference-profile categories
    still require their own renderer/source adapter before full publication.
    """
    mode = resources["conditioning_mode"]
    topology = mode.get("dynamic_reference_topology") if mode else None
    if not topology:
        return {}
    kinds = {row["kind"] for row in input_targets.values()}
    source = f"model.{resources['model_profile_version_id']}.conditioning_contract"
    result = {}
    for kind, group_key in (("video", "videos"), ("audio", "audios")):
        if kind not in kinds:
            continue
        result[kind] = [{
            "value": row["key"], "label": row["label"], "description": row["description"],
            "minimum_items": row["minimum_items"], "maximum_items": row["maximum_items"],
            "source": f"{source}.modes.{mode['key']}.dynamic_reference_topology.{group_key}",
        } for row in topology[group_key]["purposes"] if row["maximum_items"] > 0]
    return deepcopy(result)


def validate_execution_input_purposes(inputs, options):
    for spec in inputs:
        allowed = {row["value"] for row in options.get(spec.kind, [])}
        unknown = [value for value in spec.semantic_contract["allowed_purposes"] if value not in allowed]
        if unknown:
            raise ValidationError({f"inputs.{spec.key}.allowed_purposes": "所选资源未声明此类型的用途：" + ", ".join(unknown)})
