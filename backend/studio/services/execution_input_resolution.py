"""Resolve explicit input uses, never implicit library picks or latest outputs.

This internal boundary consumes a service-verified definition and an authorized
owner/context. It neither creates preparation rows nor authorizes submission.
Selected outputs require exact decisions and a service-authorized Run/upstream
scope. Versioned context uses registered readers and confirmed source records;
caller-authored snapshots are not accepted as source evidence.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import CharacterBible, SceneBible, PropBible, SeriesProject, Episode, Shot, ScriptRevision
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_input_catalog import ExecutionSubjectCategory
from studio.services.execution_inputs import allocate_workflow_input_slots
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.reference_bindings import (
    ReferenceBindingError, validate_reference_input_counts, validate_reference_topology_facts,
)
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.execution_shot_field_mapping import shot_field_mapping_cache


_MEDIA = {"image", "video", "audio"}
_SUBJECT_MODELS = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}


def _error(key, message):
    raise ValidationError({f"inputs.{key}": message})


def _uuid(value, path):
    if not isinstance(value, str):
        _error(path, "必须使用正式对象 UUID")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValidationError({f"inputs.{path}": "必须使用正式对象 UUID"}) from exc


def _external_subjects(rows):
    if not isinstance(rows, list):
        _error("subject_facts", "本次主体事实必须是列表")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "name", "description", "category"}:
            _error("subject_facts", "本次主体必须明确声明 id、name、description、category；不推测主体类别")
        if not isinstance(row["category"], str) or row["category"] not in ExecutionSubjectCategory.values:
            _error("subject_facts", "主体语义类别必须从当前目录选择，不能用来源身份代替")
        key = _uuid(row["id"], "subject_facts")
        if key in result or any(not isinstance(row[field], str) for field in ("name", "description")) or not row["name"].strip():
            _error("subject_facts", "本次主体身份重复或名称/描述无效")
        result[key] = {"kind": "external", **deepcopy(row), "id": key}
    return result


def _scope(value, *, spec, owner, project_id):
    # An omitted optional scope means this request, not a guessed Shot/Project.
    if value is None:
        value = {"kind": "request"}
    if not isinstance(value, dict) or value.get("kind") not in spec["semantic_contract"]["allowed_scope_kinds"]:
        _error(spec["key"], "来源范围不在实例允许范围内")
    kind = value["kind"]
    if kind == "request":
        if set(value) != {"kind"}:
            _error(spec["key"], "本次请求范围不能冒充真实对象")
        return {"kind": kind}, None
    if set(value) != {"kind", "id"}:
        _error(spec["key"], "真实范围必须指定唯一对象")
    key = _uuid(value["id"], spec["key"])
    catalog = {"project": (SeriesProject, "id", "owner_id"),
               "episode": (Episode, "project_id", "project__owner_id"),
               "shot": (Shot, "episode__project_id", "episode__project__owner_id")}
    if kind not in catalog:
        _error(spec["key"], "未登记的来源范围")
    model, project_path, owner_path = catalog[kind]
    row = model.objects.filter(pk=key, **{owner_path: owner.pk}).values_list(project_path, flat=True).first()
    if row is None or (project_id is not None and str(row) != str(project_id)):
        _error(spec["key"], "来源范围不存在或不属于当前授权项目")
    return {"kind": kind, "id": key}, str(row)


def _subject(value, *, spec, owner, project_id, external, asset_id, scope, item_key, cache, shot_reference):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "id"}:
        _error(spec["key"], "主体引用必须为 kind 和正式 id")
    kind, key = value["kind"], _uuid(value["id"], spec["key"])
    if kind not in spec["semantic_contract"]["allowed_subject_kinds"]:
        _error(spec["key"], "主体类型不在实例允许范围内")
    if kind == "external":
        if key not in external:
            _error(spec["key"], "本次主体未声明，不能伪造 Bible 身份")
        return deepcopy(external[key])
    cache_key = (owner.pk, str(project_id), kind, key, asset_id, spec["key"], scope["kind"], scope.get("id"), item_key)
    if cache_key in cache:
        return deepcopy(cache[cache_key])
    entity_key = ("entity", owner.pk, str(project_id), kind, key)
    if entity_key not in cache:
        model = _SUBJECT_MODELS.get(kind)
        cache[entity_key] = model.objects.only("id", "project_id", "anchor_asset_id", "name", "description").filter(
            pk=key, project__owner_id=owner.pk).first() if model else None
    row = cache[entity_key]
    if row is None or (project_id is not None and str(row.project_id) != str(project_id)):
        _error(spec["key"], "主体不存在或不属于当前授权项目")
    if shot_reference is None and str(row.anchor_asset_id) != asset_id and not row.reference_assets.filter(pk=asset_id).exists():
        _error(spec["key"], "素材不是该主体已登记的参考素材")
    # A selected image already carries its appearance. This is a subject
    # association, not an implicit request for a current or old identity body.
    result = {"kind": kind, "id": key, "project_id": str(row.project_id), "name": row.name,
              "description": row.description}
    cache[cache_key] = result
    return deepcopy(result)


def _context_field(source, *, spec, owner, project_id, production_run_id, eligible_stage_ids, context_cache, production_scope, reviewed_smoke_context=None):
    key = spec["key"]
    if not {"kind", "selector_key", "scope", "content_revision_id"} <= set(source):
        _error(key, "上下文来源必须明确选择目录字段、范围和精确内容修订")
    from studio.services.execution_input_catalog import execution_context_field_options, bound_context_source_keys
    catalog = execution_context_field_options()
    choices = [row for row in catalog if row["selector_key"] == source["selector_key"]]
    if len(choices) != 1 or choices[0]["kind"] != spec["kind"]:
        _error(key, "上下文字段未登记或类型不匹配")
    dependency_keys = {row["key"] for row in choices[0].get("revision_dependencies", [])}
    if set(source) != bound_context_source_keys(choices[0]):
        _error(key, "上下文来源必须精确声明目录要求的全部审核版本")
    related_ids = source.get("related_revision_ids", {})
    if not isinstance(related_ids, dict) or set(related_ids) != dependency_keys:
        _error(key, "关联审核版本与目录声明不一致")
    related_ids = {name: _uuid(value, key) for name, value in related_ids.items()}
    from studio.services.json_contracts import canonical_json_bytes
    scope_cache_key = ("scope", spec["key"], canonical_json_bytes(source["scope"]))
    if scope_cache_key not in context_cache:
        context_cache[scope_cache_key] = _scope(source["scope"], spec=spec, owner=owner, project_id=project_id)
    scope, resolved_project_id = context_cache[scope_cache_key]
    if scope["kind"] != choices[0]["scope_kind"]:
        _error(key, "上下文范围与目录不匹配")
    revision_kind = choices[0]["revision_kind"]
    if revision_kind == "production_request":
        from studio.models import ProductionRun
        from studio.services.production_creation_inputs import verified_creation_inputs
        revision_id = _uuid(source["content_revision_id"], key)
        if revision_id != str(production_run_id):
            _error(key, "创建输入必须属于本轮明确授权的生产运行，不能指定其他运行")
        cache_key = ("creation_inputs", revision_id, str(resolved_project_id))
        if cache_key not in context_cache:
            run = ProductionRun.objects.filter(pk=production_run_id, project_id=resolved_project_id,
                owner=owner, project__owner=owner).first()
            if run is None:
                _error(key, "创建输入不存在或不属于当前项目及运行所有者")
            context_cache[cache_key] = verified_creation_inputs(run)
        values, receipt = context_cache[cache_key]
        field = source["selector_key"].removeprefix("run.")
        if field not in values or values[field] is None:
            _error(key, "本轮未提供所选创建输入，不能用配方示例值或项目默认值替代")
        return deepcopy(values[field]), {"kind": "context_field", "selector_key": source["selector_key"], "scope": scope,
            "content_revision_id": revision_id, "revision_kind": revision_kind, "creation_request": deepcopy(receipt)}
    if revision_kind == "visual_rules_approval":
        from studio.models import ProductionRun
        from studio.services.production_reviewed_visual_rules import read_reviewed_visual_rules
        revision_id = _uuid(source["content_revision_id"], key)
        if reviewed_smoke_context is not None:
            from studio.services.execution_smoke_sources import ReviewedSmokeContext
            if not isinstance(reviewed_smoke_context, ReviewedSmokeContext) or production_run_id is not None:
                _error(key, "烟测来源上下文必须由原任务校验，不能混入生产执行权限")
            if reviewed_smoke_context.project_id != resolved_project_id:
                _error(key, "审核来源不属于所选烟测上下文项目")
            production_run_id = reviewed_smoke_context.run_id
            eligible_stage_ids = reviewed_smoke_context.eligible_stage_ids
        run = ProductionRun.objects.filter(pk=production_run_id, project_id=resolved_project_id,
            owner=owner, project__owner=owner).first()
        if run is None:
            _error(key, "已审核影视规则需要本轮明确授权的运行和项目，独立请求不能冒充审核")
        cache_key = (str(run.pk), revision_kind, revision_id)
        if cache_key not in context_cache:
            context_cache[cache_key] = read_reviewed_visual_rules(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=revision_id)
        reviewed = context_cache[cache_key]
        fields = [row["key"] for row in reviewed["review_contract"]["fields"] if row["source_selector"] == source["selector_key"]]
        if len(fields) != 1:
            _error(key, "所选来源不属于原候选冻结的输出字段，不能使用当前目录替代")
        field = fields[0]
        return deepcopy(reviewed["canonical"][field]), {"kind": "context_field", "selector_key": source["selector_key"],
            "scope": scope, "content_revision_id": revision_id, "revision_kind": revision_kind,
            **({"smoke_source_context": {"run_id": reviewed_smoke_context.run_id,
                "stage_key": reviewed_smoke_context.stage_key, "run_plan_sha256": reviewed_smoke_context.run_plan_sha256}}
                if reviewed_smoke_context is not None else {}),
            "review": reviewed["review"], "output": {**reviewed["output"], "field": field,
                "business_output": reviewed["review_contract"]["output"]}}
    if revision_kind in {"storyboard_approval", "bible_approval"}:
        from studio.models import ProductionRun
        from studio.services.production_execution_units import resolve_production_unit
        from studio.services.production_reviewed_content import read_reviewed_storyboard, read_reviewed_bible, reviewed_shot_row, reviewed_entity_row
        run_cache_key = ("run", str(production_run_id), str(resolved_project_id if revision_kind == "storyboard_approval" else project_id))
        if run_cache_key not in context_cache:
            context_cache[run_cache_key] = ProductionRun.objects.filter(pk=production_run_id,
                project_id=resolved_project_id if revision_kind == "storyboard_approval" else project_id, owner=owner).first()
        run = context_cache[run_cache_key]
        if run is None:
            _error(key, "已审核内容需要本轮明确授权的制作运行")
        unit_scope = scope if revision_kind == "storyboard_approval" else production_scope
        unit_cache_key = ("unit", str(run.pk), canonical_json_bytes(unit_scope))
        if unit_cache_key not in context_cache:
            context_cache[unit_cache_key] = resolve_production_unit(run, unit_scope)
        unit = context_cache[unit_cache_key]
        revision_id = _uuid(source["content_revision_id"], key)
        cache_key = (str(run.pk), revision_kind, revision_id)
        if cache_key not in context_cache:
            reader = read_reviewed_storyboard if revision_kind == "storyboard_approval" else read_reviewed_bible
            context_cache[cache_key] = reader(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=revision_id)
        reviewed = context_cache[cache_key]
        row = reviewed_shot_row(reviewed, unit) if revision_kind == "storyboard_approval" else reviewed_entity_row(reviewed, unit)
        field = source["selector_key"].rsplit(".", 1)[-1]
        dependencies = {}
        setting_origin = {}
        if revision_kind == "bible_approval" and (field == "content"
                or source["selector_key"].startswith("production.entity.primary.")):
            from studio.services.production_reviewed_content import read_reviewed_entity_primary, reviewed_entity_content
            primary_cache_key = ("bible_primary_setting", str(run.pk), revision_id,
                unit["scope"]["kind"], unit["scope"]["id"])
            if primary_cache_key not in context_cache:
                context_cache[primary_cache_key] = read_reviewed_entity_primary(row, unit, allow_absent=True)
            fields, reference = context_cache[primary_cache_key]
            if field == "content":
                value = reviewed_entity_content(row, fields)
            elif fields is None:
                _error(key, "本轮已审核实体没有主图设定，不能从旧身份文字补齐所选字段")
            else:
                value = fields[field]
            setting_origin = {"setting": deepcopy(reference)}
        elif dependency_keys:
            from studio.services.production_reviewed_content import read_context_dependencies
            from studio.services.production_linked_content import reviewed_linked_shot_value
            dependencies = read_context_dependencies(choices[0], run=run, eligible_stage_ids=eligible_stage_ids,
                cache=context_cache, revision_ids=related_ids)
            value = reviewed_linked_shot_value(field, row, dependencies, run_id=run.pk,
                project_id=unit["project_id"], cache=context_cache)
        elif field in row:
            value = row[field]
        else:
            _error(key, "审核快照缺少所选字段，不能从当前对象补齐")
        return value, {"kind": "context_field", "selector_key": source["selector_key"], "scope": scope,
            "content_revision_id": revision_id, "revision_kind": revision_kind, "review": reviewed["review"], "unit": unit,
            **setting_origin,
            **({"related_reviews": {name: content["review"] for name, content in dependencies.items()}} if dependencies else {})}
    if source["selector_key"] not in {"script.content", "script.visual_context", "script.project_style"}:
        _error(key, "上下文读取适配尚未登记")
    revision_id = _uuid(source["content_revision_id"], key)
    revision = ScriptRevision.objects.filter(pk=revision_id, source__owner_id=owner.pk,
        source__project_id=resolved_project_id, source__project__owner_id=owner.pk,
        status=ScriptRevision.Status.CONFIRMED).values("id", "source_id", "revision", "content", "content_size_bytes",
            "content_sha256", "confirmed_by_id", "confirmed_at").first()
    if revision is None or revision["confirmed_by_id"] is None or revision["confirmed_at"] is None:
        _error(key, "剧本修订不存在、无权访问或尚未确认")
    import hashlib
    content = revision.pop("content")
    encoded = content.encode("utf-8")
    if len(encoded) != revision["content_size_bytes"] or hashlib.sha256(encoded).hexdigest() != revision["content_sha256"]:
        _error(key, "已确认剧本正文与摘要不一致")
    reference = {name: str(revision[name]) if name in {"id", "source_id"} else revision[name]
        for name in ("id", "source_id", "revision", "content_size_bytes", "content_sha256")}
    value = content
    if source["selector_key"] in {"script.visual_context", "script.project_style"}:
        from studio.services.production_visual_rules import visual_rules_project_context, visual_rules_project_style
        cache_key = (source["selector_key"], str(resolved_project_id))
        if cache_key not in context_cache:
            project = SeriesProject.objects.select_related("style__category").get(pk=resolved_project_id, owner_id=owner.pk)
            context_cache[cache_key] = (visual_rules_project_style(project) if source["selector_key"] == "script.project_style"
                else visual_rules_project_context(project))
        context = context_cache[cache_key]
        value = context if source["selector_key"] == "script.project_style" else {
            "source_text": content, "source_reference": reference, "project_context": context}
    return value, {"kind": "context_field", "selector_key": source["selector_key"], "scope": scope,
        "content_revision_id": revision_id, "source_reference": reference,
        "confirmed_by_id": str(revision["confirmed_by_id"]), "confirmed_at": revision["confirmed_at"].isoformat()}


@security_boundary_resolution_cache()
@shot_field_mapping_cache()
def resolve_execution_input_sources(definition, sources, *, owner, subject_facts, max_media_bytes, project_id=None,
        production_run_id=None, eligible_stage_ids=(), production_scope=None, reviewed_smoke_context=None, unit_key=None):
    """Resolve literal, versioned context, Asset and selected-output facts.

    max_media_bytes is the caller's resolved platform limit, never an HTTP
    override. owner is the preparation's business owner, not automatically a
    staff operator. A non-null project_id additionally restricts real subjects
    and scopes to that authorized preparation project.
    production_run_id/eligible_stage_ids come from the root's validated plan,
    not client input. A selected output never creates or impersonates an Asset.
    """
    if not isinstance(definition, JsonContract):
        _error("definition", "需要已验证的完整执行定义")
    verify_json_contract(definition.value, schema_version=definition.schema_version, size_bytes=definition.size_bytes,
                         sha256=definition.sha256, max_bytes=execution_configuration_max_bytes())
    if not getattr(owner, "is_active", False) or not getattr(owner, "pk", None):
        _error("owner", "需要有效的业务所有者")
    if type(max_media_bytes) is not int or max_media_bytes <= 0:
        _error("limits", "必须提供有效的服务端媒体大小边界")
    payload = definition.value
    if not isinstance(payload, dict) or not isinstance(payload.get("configuration"), dict) or not isinstance(payload.get("interface"), dict):
        _error("definition", "缺少完整定义")
    specs = payload["configuration"].get("inputs")
    if not isinstance(specs, list) or not isinstance(sources, dict) or any(not isinstance(key, str) or not key for key in sources):
        _error("sources", "逻辑来源必须是按输入 key 编排的对象")
    from studio.services.execution_input_structures import input_structure_schemas, resolve_input_structure
    input_structure_schemas(payload)
    from studio.services.execution_media_source_policy import execution_media_source_policy, select_execution_media_source
    for spec in specs:
        execution_media_source_policy(spec)
    json_contract([sources, subject_facts], max_bytes=execution_request_max_bytes())
    external = _external_subjects(subject_facts)
    if project_id is not None:
        project_id = _uuid(str(project_id), "project")
        if not SeriesProject.objects.filter(pk=project_id, owner_id=owner.pk).exists():
            _error("project", "当前项目不属于业务所有者")
    unknown = set(sources) - {row["key"] for row in specs}
    if unknown:
        _error("sources", "实例没有声明这些输入：" + ", ".join(sorted(unknown)))
    values, provenance, media, all_items, used_assets, context_cache = {}, [], {}, {}, {}, {}
    subject_cache = {}
    structured, video_audio = [], []
    for spec in specs:
        key = spec["key"]
        if key not in sources:
            if spec["required"] or spec["min_items"]:
                _error(key, "必需来源缺失")
            continue
        source = sources[key]
        if not isinstance(source, dict):
            _error(key, "必须提交明确的来源对象")
        if spec["kind"] not in _MEDIA:
            from studio.services.execution_project_sources import input_project_reader
            reader = input_project_reader(spec, payload["interface"])
            if reader and source.get("kind") != "project_snapshot":
                _error(key, "策略要求项目已保存事实，请预览并确认；不能手工冒充项目来源")
            for target in spec["targets"]:
                if target["target_kind"] not in {"operation_input", "profile_input"}:
                    continue
                group = "profile_inputs" if target["target_kind"] == "profile_input" else "input_targets"
                target_contract = payload["interface"][group][target["target_key"]]
                if "source_kinds" in target_contract and source.get("kind") not in target_contract["source_kinds"]:
                    _error(key, "此业务输入必须使用合同声明的真实上下文来源")
                if (source.get("kind") == "context_field" and "context_selectors" in target_contract
                        and source.get("selector_key") not in target_contract["context_selectors"]):
                    _error(key, "上下文字段与目标业务协议不匹配")
                if (source.get("kind") == "selected_output" and "selected_output_keys" in target_contract
                        and source.get("output_key") not in target_contract["selected_output_keys"]):
                    _error(key, "已采用输出与目标业务协议不匹配")
            if source.get("kind") == "structured":
                if set(source) != {"kind", "template"} or spec["kind"] != "json" or not spec["min_items"] <= 1 <= spec["max_items"]:
                    _error(key, "结构映射需要完整template和单值JSON输入")
                structured.append((spec, source))
                continue
            if source.get("kind") == "project_snapshot":
                if reader is None:
                    _error(key, "此输入没有声明项目快照读取合同")
                from studio.services.execution_input_suggestions import resolve_project_source_reference
                value, origin = resolve_project_source_reference(source, reader=reader, definition=definition,
                    input_key=key, owner=owner, project_id=project_id)
            elif source.get("kind") == "context_field":
                value, origin = _context_field(source, spec=spec, owner=owner, project_id=project_id,
                    production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids, context_cache=context_cache,
                    production_scope=production_scope, reviewed_smoke_context=reviewed_smoke_context)
            elif source.get("kind") == "selected_output":
                from studio.services.execution_selected_text import resolve_selected_execution_text
                value, origin = resolve_selected_execution_text(source, spec=spec, owner=owner,
                    project_id=project_id, production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids)
            elif set(source) in ({"kind", "value"}, {"kind", "value", "reference"}) and source["kind"] == "literal":
                value, origin = source["value"], {"kind": "literal"}
                if "reference" in source:
                    from studio.services.execution_input_suggestions import verify_input_suggestion_reference
                    if spec["kind"] != "text":
                        _error(key, "只有文字输入可以引用填写参考")
                    origin = verify_input_suggestion_reference(source["reference"], definition=definition,
                        input_key=key, value=value, owner=owner, project_id=project_id,
                        production_run_id=production_run_id, production_scope=production_scope,
                        eligible_stage_ids=eligible_stage_ids)
            else:
                _error(key, "文本/JSON仅接受字面值、精确上下文修订或已审核采用的目录输出")
            if spec["kind"] == "text" and not isinstance(value, str):
                _error(key, "文本来源必须为字符串")
            if spec["kind"] not in {"text", "json"} or not spec["min_items"] <= 1 <= spec["max_items"]:
                _error(key, "字面值类型或单值数量不匹配")
            values[key] = deepcopy(value)
            provenance.append({"input_key": key, **origin, "value_sha256": json_contract(value, max_bytes=execution_request_max_bytes()).sha256})
            continue
        source, source_choice = select_execution_media_source(spec, source)
        if set(source) != {"items"} or not isinstance(source["items"], list):
            _error(key, "媒体来源必须提供有序 items")
        if not spec["min_items"] <= len(source["items"]) <= spec["max_items"] or (spec["required"] and not source["items"]):
            _error(key, "展开后的素材数量超出实例合同")
        media[key] = []
        for position, item in enumerate(source["items"]):
            source_kind = item.get("kind") if isinstance(item, dict) else None
            identity_keys = ({"asset_id"} if source_kind == "asset" else
                {"paired_to"} if source_kind == "video_audio" else {"decision_id", "artifact_id"})
            required = {"kind", "item_key", "purpose", *identity_keys}
            if (source_kind not in {"asset", "selected_output", "video_audio"} or not required <= item.keys()
                    or set(item) - required - {"subject_ref", "scope_ref", "paired_to", "order"}):
                _error(key, "媒体必须使用正式 Asset、明确已采用产物或配对视频原音，不能传路径、临时文件或伪造来源")
            item_key = item["item_key"]
            if not isinstance(item_key, str) or not item_key or len(item_key) > 191 or item_key in all_items:
                _error(key, "素材使用标识必须在请求内唯一且不超过191字符")
            if "order" in item and (type(item["order"]) is not int or item["order"] != position):
                _error(key, "order 必须与数组位置一致，平台不会另行排序")
            allowed = spec["semantic_contract"]["allowed_purposes"]
            if (allowed and item["purpose"] not in allowed) or (not allowed and item["purpose"] is not None):
                _error(key, "用途不在实例声明范围内；无用途合同的槽位应显式为 null")
            scope, scope_project = _scope(item.get("scope_ref"), spec=spec, owner=owner, project_id=project_id)
            if source_kind == "video_audio":
                if (spec["kind"] != "audio" or spec["semantic_contract"]["paired_with_input_key"] is None
                        or item.get("subject_ref") is not None or not spec["targets"]
                        or any(target["target_kind"] != "workflow_slot" for target in spec["targets"])):
                    _error(key, "视频原音只能用于明确配对的音频输入，不能冒充独立素材或主体")
                record = {"input_key": key, "item_key": item_key, "position": position, "kind": "audio",
                    "asset_id": None, "artifact_id": None, "purpose": item["purpose"], "scope_ref": scope,
                    "subject": None, "paired_to": item["paired_to"], "asset_facts": None}
                media[key].append(record)
                all_items[item_key] = record
                video_audio.append((spec, record))
                continue  # Resolve its exact parent after all declared inputs, independent of input order.
            artifact_id = None
            binding_project_id = scope_project or project_id
            if source_kind == "asset":
                asset_id = _uuid(item["asset_id"], key)
                from studio.services.execution_media_sources import resolve_execution_asset_media
                asset_media = resolve_execution_asset_media(owner=owner, asset_id=asset_id,
                    expected_kind=spec["kind"], max_media_bytes=max_media_bytes)
                asset, facts = asset_media.asset, asset_media.media_facts
                if source_choice is not None and source_choice["source"] == "asset_upload":
                    from studio.services.asset_provenance import is_uploaded_asset
                    if not is_uploaded_asset(asset):
                        _error(key, "本地上传来源需要正式上传入库的素材；当前素材没有上传来源事实")
                origin = {"kind": "asset", "asset_id": asset_id}
            else:
                from studio.services.execution_selected_outputs import resolve_selected_execution_media, selected_output_item_key

                if item_key != selected_output_item_key(input_key=key, decision_id=item["decision_id"],
                        artifact_id=item["artifact_id"], purpose=item["purpose"]):
                    _error(key, "已采用产物使用标识必须对应明确决定、产物、输入及用途")
                selected = resolve_selected_execution_media(decision_id=item["decision_id"], artifact_id=item["artifact_id"],
                    owner=owner, production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids,
                    expected_kind=spec["kind"], max_media_bytes=max_media_bytes)
                if project_id is not None and str(selected.artifact.production_run.project_id) != str(project_id):
                    _error(key, "上游产物不属于本次准备项目")
                if scope_project is not None and str(selected.artifact.production_run.project_id) != scope_project:
                    _error(key, "上游产物与明确的来源范围不属于同一项目")
                asset, facts, origin = selected.artifact.asset, selected.media_facts, selected.selection
                binding_project_id = str(selected.artifact.production_run.project_id)
                asset_id = str(asset.pk) if asset is not None else None
                artifact_id = str(selected.artifact.pk)
            media_identity = ("asset", asset_id) if asset_id else ("artifact", artifact_id)
            previous = used_assets.get(media_identity, [])
            if previous and (not spec["semantic_contract"]["allow_repeated_asset"] or any(not entry for entry in previous)):
                _error(key, "合同不允许重复使用同一素材；不能静默去重")
            from studio.services.execution_shot_reference_facts import resolve_execution_shot_reference
            shot_reference = resolve_execution_shot_reference(item, spec=spec, owner=owner,
                project_id=binding_project_id, asset_id=asset_id, scope=scope, media_facts=facts, cache=subject_cache)
            subject = _subject(item.get("subject_ref"), spec=spec, owner=owner, project_id=binding_project_id,
                               external=external, asset_id=asset_id, scope=scope, item_key=item_key,
                               cache=subject_cache, shot_reference=shot_reference)
            record = {"input_key": key, "item_key": item_key, "position": position, "kind": spec["kind"], "asset_id": asset_id,
                      "artifact_id": artifact_id, "origin": origin,
                      "purpose": item["purpose"], "scope_ref": scope, "subject": subject, "paired_to": item.get("paired_to"),
                      "media_facts": facts, "asset_facts": {"name": asset.name, "description": asset.description, "prompt_hint": asset.prompt_hint} if asset is not None else None}
            if source_choice is not None:
                record["source_selection"] = {"source": source_choice["source"], "overridden": source_choice["overridden"]}
            if shot_reference is not None:
                record["shot_reference"] = shot_reference
            record["source_sha256"] = json_contract(record, max_bytes=execution_request_max_bytes()).sha256
            media[key].append(record)
            all_items[item_key] = record
            used_assets.setdefault(media_identity, []).append(spec["semantic_contract"]["allow_repeated_asset"])
        values[key] = media[key]
    for spec, record in video_audio:
        parent = all_items.get(record["paired_to"]) if isinstance(record["paired_to"], str) else None
        if (parent is None or parent["kind"] != "video" or parent["input_key"] != spec["semantic_contract"]["paired_with_input_key"]
                or parent["scope_ref"] != record["scope_ref"]):
            _error(spec["key"], "视频原音必须引用本次同范围、合同声明的具体父视频")
        if parent["media_facts"].get("observed_media", {}).get("has_audio") is not True:
            _error(spec["key"], "实际参考视频未检测到内嵌音轨，不能使用原音")
        record.update(origin={"kind": "video_audio", "parent_item_key": parent["item_key"],
            "parent_source_sha256": parent["source_sha256"]}, media_facts=deepcopy(parent["media_facts"]))
        record["source_sha256"] = json_contract(record, max_bytes=execution_request_max_bytes()).sha256
    paired_uses = set()
    for spec in specs:
        parent = spec["semantic_contract"]["paired_with_input_key"]
        for item in media.get(spec["key"], []):
            paired = item["paired_to"]
            if parent is None:
                if paired is not None:
                    _error(spec["key"], "当前输入没有声明配对关系")
            elif not isinstance(paired, str) or paired not in all_items or all_items[paired]["input_key"] != parent or paired in paired_uses:
                _error(spec["key"], "必须配对本次父输入的唯一素材使用，不能跨请求配对或争用")
            if paired is not None:
                paired_uses.add(paired)
    mode = payload["resources"]["conditioning_mode"]
    targets = payload["interface"]["input_targets"]
    model_counts = None
    if mode is not None:
        # Prompt-only context does not consume an execution slot. Count every
        # explicit use once through its primary mapping, never by Asset UUID.
        primary_targets = {spec["key"]: targets[mapping["target_key"]] for spec in specs for mapping in spec["targets"]
                           if mapping["target_kind"] != "profile_input" and mapping["is_primary"]}
        execution_items = {key: item for key, item in all_items.items() if item["input_key"] in primary_targets}
        images = [item for item in execution_items.values() if item["kind"] == "image"]
        image_counts = {"first_frame": 0, "last_frame": 0, "entity_images": 0}
        for item in images:
            role = primary_targets[item["input_key"]].get("reference_binding", {}).get("category")
            if role in {"first_frame", "last_frame"}:
                image_counts[role] += 1
            elif mode.get("input_kind") == "frame_slots":
                error = ValidationError({"inputs": "首尾帧来源缺少冻结的槽位角色，不能按顺序或名称猜测"})
                error.code = "execution_interface_unavailable"
                raise error
            else:
                image_counts["entity_images"] += 1
        videos = {key: {"purpose": item["purpose"], "embedded_audio": False, "external_audio": False, "audio_purpose": None}
                  for key, item in execution_items.items() if item["kind"] == "video"}
        audios = []
        for item in execution_items.values():
            if item["kind"] != "audio":
                continue
            if item["paired_to"] is None:
                audios.append({"purpose": item["purpose"]})
            elif item["paired_to"] in videos:
                videos[item["paired_to"]].update({"embedded_audio" if item["origin"]["kind"] == "video_audio" else "external_audio": True,
                    "audio_purpose": item["purpose"]})
            else:
                _error(item["input_key"], "音轨必须配对视频输入，不能推断其他媒体关系")
        try:
            model_counts = {"total_images": len(images), **image_counts,
                "reference_videos": len(videos), "paired_video_audios": sum(row["external_audio"] or row["embedded_audio"] for row in videos.values()),
                "reference_audios": len(audios)}
            validate_reference_input_counts(model_counts, mode)
            validate_reference_topology_facts(list(videos.values()), audios, mode)
        except ReferenceBindingError as exc:
            raise ValidationError({"inputs": str(exc)}) from exc
    if structured:
        from studio.services.execution_structure_mapping import (
            validate_structure, evaluate_structure, structure_source_option, source_schema_sha256, structure_leaves)
        from studio.services.production_execution_units import resolve_production_unit, production_unit_scope_members
        from studio.models import ProductionRun
        run = ProductionRun.objects.filter(pk=production_run_id, owner=owner, project_id=project_id).first()
        if run is None or run.plan_source != "recipe" or production_scope is None:
            _error("structured", "结构映射需要原生产准备明确的运行与制作单元")
        from studio.services.production_run_plans import get_run_execution_plan
        from studio.services.execution_text_output_catalog import plan_text_output_options
        plan = get_run_execution_plan(run)
        text_outputs = plan_text_output_options(plan)
        unit = resolve_production_unit(run, production_scope)
        if not isinstance(unit_key, str) or not unit_key.strip() or len(unit_key) > 191:
            _error("structured", "结构映射需要根准备的实际单元标识，不能从实体范围推断")
        members = production_unit_scope_members(unit, unit_key)
        from studio.services.json_contracts import JsonExpansionBudget
        shot_field_budget = JsonExpansionBudget(execution_resolved_inputs_max_bytes(), path="shot_field_mapping")
        primary_images = {spec["key"] for spec in specs if spec["kind"] == "image" and any(
            row["is_primary"] and row["target_kind"] != "profile_input" for row in spec["targets"])}
        subjects = []
        for item in all_items.values():
            subject, scope = item["subject"], item["scope_ref"]
            identity = [subject["kind"], subject["id"]] if subject else None
            if (item["input_key"] in primary_images and identity is not None and identity not in subjects
                    and (scope == {"kind": "request"} or (scope["kind"], scope.get("id")) in members)):
                subjects.append(identity)
        for spec, source in structured:
            key = spec["key"]
            def read_leaf(node):
                option = structure_source_option(node, key, text_outputs)
                if node["kind"] == "selected_output":
                    from studio.services.execution_selected_text import resolve_selected_execution_text
                    return resolve_selected_execution_text(node, spec={**spec, "kind": option["kind"]}, owner=owner,
                        project_id=project_id, production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids)
                if option["scope_kind"] == "shot" and node.get("scope") != production_scope:
                    _error(key, "结构字段不能读取其他镜头的上下文")
                if node["kind"] == "shot_field":
                    from studio.services.execution_shot_field_mapping import shot_field_schema_key
                    from studio.services.production_shot_field_sources import read_reviewed_shot_field_sources
                    cache_key = ("shot_fields", str(production_scope["id"]), node["content_revision_id"])
                    if cache_key not in context_cache:
                        requested = {shot_field_schema_key(leaf): leaf["field"]
                            for _, requested_source in structured for leaf in structure_leaves(requested_source["template"])
                            if leaf["kind"] == "shot_field" and leaf.get("scope") == production_scope
                            and leaf.get("content_revision_id") == node["content_revision_id"]}
                        facts = read_reviewed_shot_field_sources(run=run, scope=production_scope,
                            eligible_stage_ids=eligible_stage_ids, decision_id=node["content_revision_id"],
                            selections=list(requested.values()), max_bytes=execution_resolved_inputs_max_bytes()).value
                        context_cache[cache_key] = dict(zip(requested, facts, strict=True))
                    fact = context_cache[cache_key][shot_field_schema_key(node)]
                    from studio.services.json_contracts import canonical_json_bytes
                    from hashlib import sha256
                    if sha256(canonical_json_bytes(fact["origin"]["value_schema"])).hexdigest() != source_schema_sha256(node, key):
                        _error(key, "原绑定字段结构与配方冻结来源不一致，不能按当前结构重解释")
                    shot_field_budget.consume(fact)
                    return deepcopy(fact["value"]), {"kind": "shot_field", "scope": deepcopy(production_scope),
                        "selection": deepcopy(node["field"]), **deepcopy(fact["origin"])}
                return _context_field(node, spec={**spec, "kind": option["kind"]}, owner=owner, project_id=project_id,
                    production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids,
                    context_cache=context_cache, production_scope=production_scope, reviewed_smoke_context=reviewed_smoke_context)
            def validate_leaf(node, option, path):
                from studio.services.execution_input_catalog import bound_context_source_keys
                fields = ({"kind", "decision_id", "artifact_id", "output_key", "scope"}
                    if node["kind"] == "selected_output" else bound_context_source_keys(option))
                if node["kind"] == "shot_field":
                    fields = {"kind", "field", "scope", "content_revision_id"}
                if option.get("reader") == "structured_text":
                    fields.add("stage_key")
                if set(node) != fields | {"source_schema_sha256"}:
                    _error(path, "结构字段必须绑定精确已审核来源")
                if node["source_schema_sha256"] != source_schema_sha256(node, path, text_outputs):
                    _error(path, "来源结构目录已变化，请按当前合同重新准备，不重解释已冻结路径")
                if node["kind"] == "shot_field":
                    from studio.services.execution_shot_field_mapping import shot_field_schema_key
                    _uuid(node["content_revision_id"], path + ".content_revision_id")
                    if "shot" not in spec["semantic_contract"]["allowed_scope_kinds"]:
                        _error(path, "实例输入没有开放镜头范围")
                    if node.get("scope") != production_scope:
                        _error(path, "结构字段不能读取其他镜头的上下文")
                    if plan.value["structure_source_schemas"].get("shot_field", {}).get(
                            shot_field_schema_key(node)) != node["source_schema_sha256"]:
                        _error(path, "独立字段未在本轮RunPlan冻结，不能临时扩大来源")
            validate_structure(source["template"], leaf_validator=validate_leaf, path=key, text_outputs=text_outputs)
            value, leaves = evaluate_structure(source["template"], read_leaf=read_leaf, unit_key=unit_key,
                reference_subjects=subjects, path=key)
            values[key] = value
            provenance.append({"input_key": key, "kind": "structured", "unit": unit, "leaves": leaves,
                "value_sha256": json_contract(value, max_bytes=execution_request_max_bytes()).sha256})
    from studio.services.content_sources import validate_source_value
    from studio.services.execution_structure_mapping import structure_target_schemas, validate_structure_value
    origins = {row["input_key"]: row for row in provenance if "value_sha256" in row}
    for spec in specs:
        key = spec["key"]
        if spec["kind"] not in {"text", "json"} or key not in values:
            continue
        value, resolution = resolve_input_structure(payload, key, values[key])
        # Defaults are applied before target/Profile validation and port projection.
        for schema in structure_target_schemas(definition, spec):
            validate_structure_value(value, schema, key)
        for target in spec["targets"]:
            if target["target_kind"] == "profile_input":
                for reference in payload["interface"]["profile_inputs"][target["target_key"]].get("source_definitions", []):
                    validate_source_value(reference, value, path=f"inputs.{key}")
        values[key] = value
        if resolution is not None:
            origin = origins[key]
            origin["structure_resolution"] = {**resolution, "source_value_sha256": origin["value_sha256"]}
            origin["value_sha256"] = json_contract(value, max_bytes=execution_request_max_bytes()).sha256
    target_items, operation_values = {}, {}
    for spec in specs:
        for mapping in spec["targets"]:
            if mapping["target_kind"] == "workflow_slot":
                rows = []
                for item in media.get(spec["key"], []):
                    row = {"item_key": item["item_key"], "kind": item["kind"]}
                    if targets[mapping["target_key"]]["wiring"].get("index_source_target_key") is not None:
                        row["paired_to"] = item["paired_to"]
                    rows.append(row)
                target_items[mapping["target_key"]] = rows
            elif mapping["target_kind"] == "operation_input" and spec["key"] in values:
                operation_values[mapping["target_key"]] = deepcopy(values[spec["key"]])
    workflow_targets = {key: value for key, value in targets.items() if value["target_kind"] == "workflow_slot"}
    slots = allocate_workflow_input_slots(workflow_targets, target_items)
    resolved = {"schema_version": 1, "logical_values": values, "input_provenance": provenance,
        "workflow_slots": slots, "operation_inputs": operation_values, "model_input_counts": model_counts}
    if workflow_targets:
        from studio.services.execution_media_processing import freeze_execution_media_processing_profile
        resolved["media_processing_profile"] = freeze_execution_media_processing_profile(definition,
            json_contract(resolved, max_bytes=execution_resolved_inputs_max_bytes()),
            project=SeriesProject.objects.get(pk=project_id, owner_id=owner.pk) if project_id is not None else None)
    return json_contract(resolved, max_bytes=execution_resolved_inputs_max_bytes())
