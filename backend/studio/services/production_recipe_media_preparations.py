"""Recipe unit inputs feed the original execution preparation, never a queue."""
from copy import deepcopy
from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_catalog_readiness import output_kind
from studio.services.execution_preparation_owners import locked_preparation_owners
from studio.services.execution_preparations import ResolvedExecutionPreparation, create_execution_instance_preparation
from studio.services.media_generation_targets import resolve_media_generation_target
from studio.services.execution_field_review import production_field_review_contract
from studio.services.json_contracts import json_contract
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_parameter_sources import bind_recipe_parameter_sources, resolved_recipe_parameter_source
from studio.services.production_recipe_contracts import validate_recipe_execution_route, validate_recipe_execution_routing
from studio.services.production_recipe_preparations import _project_inputs, recipe_stage_context
from studio.services.production_recipe_material_units import frozen_recipe_material_unit
from studio.services.production_recipe_routing import (recipe_routing, initial_recipe_route_index,
    candidate_request_key, read_recipe_candidate_route, resolve_recipe_candidate_route,
    record_recipe_route_preparation, verify_recipe_preparation_route)
from studio.services.production_reference_mapping import expand_reviewed_reference_sources
from studio.services.production_reviewed_references import read_reviewed_shot_references
from studio.services.production_stage_catalog import STAGE_MEDIA_SCOPE_KINDS, validate_stage_execution_definition
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def recipe_media_stage_context(stage, expected_run_plan_sha256):
    plan, declaration = recipe_stage_context(stage, expected_run_plan_sha256)
    if stage.stage_key not in STAGE_MEDIA_SCOPE_KINDS or stage.mode != "existing_pipeline":
        raise ValidationError("此准备入口仅用于配方媒体生成职责")
    from studio.services.production_recipe_material_sources import recipe_material_source
    if stage.stage_key != "bible_image_generation" or recipe_material_source(declaration, "primary") == "generate":
        recipe_routing(declaration)
    return plan, declaration


def validate_media_candidate_source(stage, declaration, scope, candidate_number, generation_request, *, appearance_role="primary"):
    initial_count = recipe_routing(declaration, appearance_role=appearance_role)["variant_count"]
    if generation_request is None:
        if candidate_number > initial_count:
            raise ValidationError("候选编号超过冻结配方数量")
        return
    from studio.services.production_runs import _bible_regeneration_request_contract
    if appearance_role != "primary":
        raise ValidationError("原实体重新生成动作不能追加变体候选")
    if stage.stage_key != "bible_image_generation" or scope.get("kind") not in {"character", "scene", "prop"}:
        raise ValidationError("追加候选必须来自原Bible重新生成动作")
    if (not isinstance(generation_request, dict) or candidate_number <= initial_count
            or not isinstance(generation_request.get("targets"), list)):
        raise ValidationError("追加候选的请求或编号无效")
    request, digest = _bible_regeneration_request_contract(request_id=generation_request.get("request_id"),
        expected_source_snapshot_sha256=generation_request.get("expected_source_snapshot_sha256"),
        entities=generation_request.get("entities"))
    if (generation_request.get("request_sha256") != digest
            or {"entity_type": scope["kind"], "entity_id": scope["id"], "variant_number": candidate_number}
                not in generation_request.get("targets", [])
            or {"entity_type": scope["kind"], "entity_id": scope["id"]} not in request["entities"]
            or not stage.route_events.filter(unit_key=f'bible-regenerate:{request["request_id"]}',
                event_type="bible_regeneration_preparing", code=digest).exists()):
        raise ValidationError("追加候选缺少同次正式重新生成决定")
    if candidate_number > boundary_int("security.production.max_bible_candidates_per_entity"):
        raise ValidationError("追加候选超过当前每实体候选数量上限")


def read_recipe_media_candidate(preparation, *, stage, plan, declaration):
    from studio.services.execution_preparations import read_execution_preparation_detail
    detail = read_execution_preparation_detail(preparation)
    verify_recipe_preparation_route(preparation, stage=stage, plan=plan, declaration=declaration)
    intent = detail.intent["request"]["requested"]
    scope, number = intent.get("scope"), intent.get("candidate_number")
    if (not isinstance(scope, dict) or set(scope) != {"kind", "id"} or type(number) is not int or number < 1
            or preparation.stage_run_id != stage.pk or intent.get("stage_key") != stage.stage_key
            or intent.get("run_plan_sha256") != plan.sha256):
        raise ValidationError("媒体候选准备与冻结制作单元或路线不一致")
    if stage.stage_key == "bible_image_generation":
        from studio.services.media_generation_appearances import normalize_media_appearance_request
        target = normalize_media_appearance_request(intent.get("appearance_target"))
        unit, _ = frozen_recipe_material_unit(stage, plan, scope, target)
        expected_key = unit["unit_key"]
    else:
        expected_key = f'{scope["kind"]}:{scope["id"]}'
    if preparation.unit_key != expected_key:
        raise ValidationError("媒体候选准备与冻结外观或镜头单元身份不一致")
    validate_media_candidate_source(stage, declaration, scope, number, intent.get("generation_request"),
        appearance_role=unit["appearance_role"] if stage.stage_key == "bible_image_generation" else "primary")
    return scope, number


def _material_requirement(stage, scope, *, requirements=None, appearance_target=None):
    """Select from the original M3 receipt, never infer a demand from an entity."""
    if requirements is None:
        from studio.services.bible_material_requirements import read_bible_material_requirements
        requirements = read_bible_material_requirements(run=stage.production_run,
            eligible_stage_ids=execution_source_stage_ids(stage), scopes=[scope]).value
    role = appearance_target["role"] if appearance_target is not None else "primary"
    setting_id = appearance_target["setting_revision_id"] if appearance_target is not None else None
    targets = [row for row in requirements["targets"]
        if row["scope"] == scope and row["appearance_role"] == role
        and (setting_id is None or row["setting"]["setting_revision_id"] == setting_id)]
    if len(targets) != 1:
        raise ValidationError("本次已审核素材需求没有此单元的唯一主图目标或指定变体，不能按实体存在创建候选")
    return {"schema_version": 1, "run_plan_sha256": requirements["run_plan_sha256"],
        "bible_review": deepcopy(requirements["bible_review"]), "target": deepcopy(targets[0])}


def require_current_recipe_material_requirement(preparation, *, stage, requirements=None):
    """Submission authority for a primary or its auxiliary calls; not history.

    Existing preparation replay stays immutable. A new call must still match
    the exact original content review; a route check alone cannot grant that.
    The scheduler can pass its already verified whole-stage receipt.
    """
    if stage.stage_key != "bible_image_generation":
        return
    from studio.services.execution_preparations import read_execution_preparation_detail
    from studio.services.media_generation_targets import read_media_generation_target
    plan, declaration = recipe_media_stage_context(stage, None)
    scope, _ = read_recipe_media_candidate(preparation, stage=stage, plan=plan, declaration=declaration)
    detail = read_execution_preparation_detail(preparation)
    expected = _material_requirement(stage, scope, requirements=requirements,
        appearance_target=detail.intent["request"]["requested"].get("appearance_target"))
    saved = detail.resolved_inputs.get("recipe_material_requirement")
    if (not isinstance(saved, dict)
            or json_contract(saved, max_bytes=execution_configuration_max_bytes()).sha256
                != json_contract(expected, max_bytes=execution_configuration_max_bytes()).sha256
            or read_media_generation_target(preparation).value["setting"] != expected["target"]["setting"]):
        raise ValidationError("候选准备缺少匹配的本轮素材需求依据，不能派发或采用其他审核/设定")


def _project_media_inputs(*, definition, route, stage, plan, scope, records, content_cache=None):
    """One projection for an explicit review write or an already approved read."""
    rules = route["input_sources"]
    content_cache = {} if content_cache is None else content_cache
    structured = {key: row for key, row in rules.items() if row["kind"] == "structured"}
    grouped = {key: row for key, row in rules.items() if row["kind"] == "reviewed_references"}
    scoped = {key: row for key, row in rules.items() if row["kind"] in {"context_field", "shot_field"}
        and row["scope"]["kind"] in {"shot", "request"}}
    inputs, _ = _project_inputs({key: row for key, row in rules.items() if key not in grouped and key not in scoped and key not in structured}, plan, stage, definition=definition)
    if scoped:
        from studio.services.production_reviewed_content import read_reviewed_storyboard, read_reviewed_bible, reviewed_shot_row, reviewed_entity_row
        if "unit" not in content_cache:
            content_cache["unit"] = resolve_production_unit(stage.production_run, scope)
        unit = content_cache["unit"]
        readers = {"shot": (read_reviewed_storyboard, reviewed_shot_row), "request": (read_reviewed_bible, reviewed_entity_row)}
        for kind, (reader, select_row) in readers.items():
            selected = {key: row for key, row in scoped.items() if row["scope"]["kind"] == kind}
            if not selected:
                continue
            if kind == "shot" and scope["kind"] != "shot":
                raise ValidationError("当前制作对象不是镜头，不能读取镜头上下文")
            if kind not in content_cache:
                content_cache[kind] = reader(run=stage.production_run, eligible_stage_ids=execution_source_stage_ids(stage))
            reviewed = content_cache[kind]
            select_row(reviewed, unit)
            from studio.services.execution_input_catalog import execution_context_field_options
            from studio.services.production_reviewed_content import read_context_dependencies
            options = {row["selector_key"]: row for row in execution_context_field_options()}
            for key, row in selected.items():
                dependencies = (read_context_dependencies(options[row["selector_key"]], run=stage.production_run,
                    eligible_stage_ids=execution_source_stage_ids(stage), cache=content_cache)
                    if row["kind"] == "context_field" else {})
                inputs[key] = {**deepcopy(row), "scope": dict(scope) if kind == "shot" else {"kind": "request"},
                    "content_revision_id": reviewed["review"]["decision_id"],
                    **({"related_revision_ids": {name: content["review"]["decision_id"] for name, content in dependencies.items()}}
                        if dependencies else {})}
    excluded = []
    if grouped:
        if scope["kind"] != "shot":
            raise ValidationError("审核参考映射只能用于当前镜头；Bible不能引用其尚未生成的参考图")
        mapped, excluded = expand_reviewed_reference_sources(specs=definition.value["configuration"]["inputs"],
            sources=grouped, records=records, shot_id=scope["id"])
        from studio.services.execution_media_source_policy import execution_media_source_policy
        for spec in definition.value["configuration"]["inputs"]:
            if spec["key"] in mapped and (policy := execution_media_source_policy(spec)) is not None:
                # Use the declared reader, never infer a new mode from returned
                # media or mix/fallback across origins. The original resolver
                # checks every item's actual source and preserves review scope.
                mapped[spec["key"]] = {"selection": {"source": policy["default_source"],
                    "items": mapped[spec["key"]]["items"]}}
        inputs.update(mapped)
    if structured:
        from studio.services.execution_structure_mapping import bind_structure
        from studio.services.execution_text_output_catalog import plan_text_output_options
        def bind_leaf(row):
            bound, _ = _project_media_inputs(definition=definition, route={"input_sources": {"leaf": row}},
                stage=stage, plan=plan, scope=scope, records=[], content_cache=content_cache)
            return bound["leaf"]
        inputs.update({key: {"kind": "structured", "template": bind_structure(row["template"], bind_leaf,
            source_schemas=plan.value["structure_source_schemas"], text_outputs=plan_text_output_options(plan))}
            for key, row in structured.items()})
    return inputs, excluded


def resolve_recipe_media_sources(*, definition, route, stage, plan, scope):
    """Same expansion for preview and creation. No model, seed or approval."""
    unit = resolve_production_unit(stage.production_run, scope)
    if scope["kind"] not in STAGE_MEDIA_SCOPE_KINDS[stage.stage_key]:
        raise ValidationError("制作对象范围与当前阶段职责不一致")
    source = None
    if any(rule["kind"] == "reviewed_references" for rule in route["input_sources"].values()):
        if scope["kind"] != "shot":
            raise ValidationError("审核参考映射只能用于当前镜头；Bible不能引用其尚未生成的参考图")
        source = read_reviewed_shot_references(stage=stage, shot_id=scope["id"])
    inputs, excluded = _project_media_inputs(definition=definition, route=route, stage=stage, plan=plan,
        scope=scope, records=source["records"] if source else [])
    return {"inputs": inputs, "review": source["review"] if source else None, "excluded_reference_keys": excluded, "unit": unit,
        "eligible_stage_ids": execution_source_stage_ids(stage)}


def recipe_reference_selection_validator(stage):
    """Called under the original review's Run/Shot locks, before approval.

    This validates the proposed facts; it is not a way to read unapproved
    references for execution. It writes no preparation, field or approval.
    """
    from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
    from studio.services.execution_input_resolution import resolve_execution_input_sources
    plan, declaration = recipe_media_stage_context(stage, None)
    stages = plan.value["recipe"]["definition"]["stages"]
    validate_recipe_execution_routing(declaration["routing"], stage_key=stage.stage_key,
        prior_stages=[row["key"] for row in stages[:stage.ordinal - 1]])
    routing = declaration["routing"]
    indexes = (range(len(routing["routes"])) if routing["mode"] == "ordered_fallback"
        else [initial_recipe_route_index(stage, declaration)])
    eligible = execution_source_stage_ids(stage)
    # This action approves reference selections, not prompt content. Validate
    # only real media inputs; the later preparation validates every required
    # text/context source against the newly completed original review.
    contracts = []
    for index in indexes:
        route = routing["routes"][index]
        definition = verify_current_execution_definition(stored_execution_contract(route["execution_instance_version_id"]))
        media_payload = deepcopy(definition.value)
        media_payload["configuration"]["inputs"] = [spec for spec in media_payload["configuration"]["inputs"]
            if spec["kind"] in {"image", "video", "audio"}]
        media_keys = {spec["key"] for spec in media_payload["configuration"]["inputs"]}
        # Keep input declarations and their frozen structures paired in this
        # temporary projection. The full contract above remains verified and
        # unchanged; text/JSON structures belong to the later preparation.
        if "input_structures" in media_payload["interface"]:
            media_payload["interface"]["input_structures"] = {
                key: snapshot for key, snapshot in media_payload["interface"]["input_structures"].items()
                if key in media_keys}
        media_definition = json_contract(media_payload, max_bytes=execution_configuration_max_bytes())
        media_route = {**route, "input_sources": {key: row for key, row in route["input_sources"].items() if key in media_keys}}
        contracts.append((definition, media_definition, media_route))

    def validate(*, shot, records):
        scope = {"kind": "shot", "id": str(shot.pk)}
        resolve_production_unit(stage.production_run, scope)
        # A reference decision is shared by candidates. Ordered fallback must
        # be safe for every declared route; it cannot silently re-approve media.
        for definition, media_definition, media_route in contracts:
            if output_kind(definition) != shot.media_type:
                raise ValidationError("实例产物类型与镜头规划不一致，请明确配置路线")
            inputs, _ = _project_media_inputs(definition=media_definition, route=media_route, stage=stage, plan=plan,
                scope=scope, records=records)
            resolve_execution_input_sources(media_definition, inputs, owner=stage.production_run.owner, subject_facts=[],
                max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                project_id=stage.production_run.project_id, production_run_id=str(stage.production_run_id),
                eligible_stage_ids=eligible)
    return validate


def _preview_media_sources(specs, sources):
    """Use the execution selector, including its explicit replacement policy."""
    from studio.services.execution_media_source_policy import select_execution_media_source
    return {spec["key"]: select_execution_media_source(spec, sources[spec["key"]])[0]["items"]
        for spec in specs if spec["key"] in sources}


@registry_reference_writer
@security_boundary_resolution_cache()
def preview_recipe_media_mapping(*, stage_run_id, expected_run_plan_sha256, shot_id, candidate_number, actor):
    from studio.models import Asset
    from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
    from studio.services.execution_input_resolution import resolve_execution_input_sources
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        plan, declaration = recipe_media_stage_context(stage, expected_run_plan_sha256)
        stages = plan.value["recipe"]["definition"]["stages"]
        routing = declaration["routing"]
        if type(candidate_number) is not int or not 1 <= candidate_number <= routing["variant_count"]:
            raise ValidationError("请从本阶段冻结的候选数量中选择编号")
        choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=f"shot:{shot_id}", candidate_number=candidate_number)
        route = routing["routes"][choice.index]
        if choice.preparation is not None:
            from studio.services.execution_preparations import read_execution_preparation_detail
            saved = read_execution_preparation_detail(choice.preparation).intent["request"]
            route = {**route, "execution_instance_version_id": str(choice.preparation.execution_instance_version_id),
                "expected_definition_sha256": saved["definition_sha256"]}
        from studio.services.execution_text_output_catalog import plan_text_output_options
        validate_recipe_execution_route(route, stage_key=stage.stage_key,
            prior_stages=[row["key"] for row in stages[:stage.ordinal - 1]], text_outputs=plan_text_output_options(plan))
        definition = verify_current_execution_definition(stored_execution_contract(route["execution_instance_version_id"]))
        sources = resolve_recipe_media_sources(definition=definition, route=route, stage=stage, plan=plan,
            scope={"kind": "shot", "id": shot_id})
        # The original normalizer validates bytes, ownership, cardinality,
        # declared purposes and duplicate uses. No parameter/seed allocation.
        resolved = resolve_execution_input_sources(definition, sources["inputs"], owner=stage.production_run.owner,
            subject_facts=[], max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
            project_id=stage.production_run.project_id, production_run_id=str(stage.production_run_id),
            eligible_stage_ids=sources["eligible_stage_ids"], production_scope={"kind": "shot", "id": shot_id},
            unit_key=f"shot:{shot_id}").value
        specs = definition.value["configuration"]["inputs"]
        media = [spec for spec in specs if spec["kind"] in {"image", "video", "audio"}]
        media_sources = _preview_media_sources(media, sources["inputs"])
        asset_ids = {item["asset_id"] for spec in media for item in media_sources.get(spec["key"], [])
            if item["kind"] == "asset"}
        names = {str(pk): name for pk, name in Asset.objects.filter(pk__in=asset_ids).values_list("id", "name")}
        from studio.services.production_stage_input_amendments import stage_input_amendment_identity
        result = {"schema_version": 1, "status": "inputs_validated", "run_plan_sha256": plan.sha256,
            "input_amendment": stage_input_amendment_identity(stage), "effective_input_sources": route["input_sources"],
            "definition_sha256": definition.sha256, "stage_run_id": str(stage.pk), "shot_id": shot_id,
            "candidate_number": candidate_number, "route_index": choice.index,
            "execution_instance_version_id": route["execution_instance_version_id"],
            "review": sources["review"], "excluded_reference_keys": sources["excluded_reference_keys"],
            "value_inputs": [{"key": spec["key"], "kind": spec["kind"], "value": resolved["logical_values"][spec["key"]],
                "provenance": next(row for row in resolved["input_provenance"] if row["input_key"] == spec["key"])}
                for spec in specs if spec["kind"] in {"text", "json"} and spec["key"] in resolved["logical_values"]],
            "inputs": [{"key": spec["key"], "kind": spec["kind"], "minimum": spec["min_items"], "maximum": spec["max_items"],
                "items": [{**item, "name": names[item["asset_id"]] if item["kind"] == "asset" else
                    "配对视频原音" if item["kind"] == "video_audio" else item["artifact_id"]}
                    for item in media_sources.get(spec["key"], [])]} for spec in media],
            "notice": "按当前已审核内容核对输入映射与素材完整性；未生成提示词、分配种子、批准费用、创建任务或调用模型。"}
        if choice.preparation is not None:
            result["notice"] += f" 已有准备 {choice.preparation.pk} 的输入保持冻结，本预览不会更新它；实际执行输入请查看该准备详情。"
        return json_contract(result, max_bytes=execution_resolved_inputs_max_bytes()).value


@registry_reference_writer
@security_boundary_resolution_cache()
def prepare_recipe_media_unit(*, stage_run_id, expected_run_plan_sha256, scope, candidate_number, actor, generation_request=None,
        appearance_target=None, material_unit_key=None, supersedes=None):
    from studio.services.media_generation_appearances import normalize_media_appearance_request
    if material_unit_key is not None and appearance_target is not None:
        raise ValidationError("本轮素材单元由冻结需求确定外观目标，不接受另选主图或差异声明")
    appearance_target = normalize_media_appearance_request(appearance_target)
    if type(candidate_number) is not int or candidate_number < 1:
        raise ValidationError("需要从1开始的明确候选编号")
    owner_ref = ("stage_run_id", stage_run_id)
    with locked_preparation_owners([owner_ref], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        plan, declaration = recipe_media_stage_context(stage, expected_run_plan_sha256)
        if material_unit_key is not None:
            from studio.services.production_recipe_material_units import frozen_recipe_material_units
            if stage.stage_key != "bible_image_generation":
                raise ValidationError("素材单元只能用于本轮Bible参考图阶段")
            if not stage.input_snapshot_sha256:
                from studio.services.production_runs import _freeze_stage_input
                _freeze_stage_input(stage.production_run, stage)
            units, requirements = frozen_recipe_material_units(stage, plan)
            matches = [row for row in units if row["unit_key"] == material_unit_key and row["scope"] == scope]
            if len(matches) != 1 or matches[0]["appearance_role"] != "variant":
                raise ValidationError("必须选择本轮同一实体的确切变体单元")
            target = next(row for row in requirements["targets"] if row["requirement_key"] == matches[0]["requirement_key"])
            appearance_target = {"role": "variant", "setting_revision_id": target["setting"]["setting_revision_id"]}
        role = appearance_target["role"] if appearance_target is not None else "primary"
        from studio.services.production_recipe_material_sources import recipe_material_source
        if stage.stage_key == "bible_image_generation" and recipe_material_source(declaration, role) != "generate":
            raise ValidationError("此角色配置为已有/上传成图，不能绕过待来源状态创建生成准备")
        routing = recipe_routing(declaration, appearance_role=role)
        unit = resolve_production_unit(stage.production_run, scope)
        validate_media_candidate_source(stage, declaration, scope, candidate_number, generation_request, appearance_role=role)
        unit_key = f'{unit["scope"]["kind"]}:{unit["scope"]["id"]}'
        frozen_requirement = None
        if stage.stage_key == "bible_image_generation":
            # Freeze the original stage before its first preparation, under
            # the same owner transaction. Replays read only that snapshot.
            if not stage.input_snapshot_sha256:
                from studio.services.production_runs import _freeze_stage_input
                _freeze_stage_input(stage.production_run, stage)
            material_unit, requirements = frozen_recipe_material_unit(stage, plan, scope, appearance_target)
            unit_key = material_unit["unit_key"]
            frozen_requirement = _material_requirement(stage, scope, requirements=requirements, appearance_target=appearance_target)
        choice = resolve_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=unit_key, candidate_number=candidate_number)
        if supersedes is not None:
            from studio.services.production_recipe_routing import RecipeRouteChoice
            if (material_unit_key is None or role != "variant" or choice.preparation is None
                    or choice.preparation.pk != supersedes.pk or supersedes.stage_run_id != stage.pk):
                raise ValidationError("只能替代本轮当前且未执行的确切变体准备")
            choice = RecipeRouteChoice(choice.index)
        elif choice.preparation is not None:
            supersedes = choice.preparation.supersedes
        route = routing["routes"][choice.index]
        # Replay or later reference reselection keeps the explicitly recorded
        # policy successor; the frozen plan still owns all other route choices.
        prior = choice.preparation or supersedes
        configuration_replacement = None
        if prior is not None:
            from studio.services.execution_preparations import read_execution_preparation_detail
            prior_request = read_execution_preparation_detail(prior).intent["request"]
            route = {**route, "execution_instance_version_id": str(prior.execution_instance_version_id),
                "expected_definition_sha256": prior_request["definition_sha256"]}
            if choice.preparation is not None:
                configuration_replacement = prior_request["requested"].get("configuration_replacement")
        dependency = None
        if material_unit_key is not None:
            if choice.preparation is not None:
                from studio.services.execution_preparations import read_execution_preparation_detail
                appearance_target = deepcopy(read_execution_preparation_detail(choice.preparation).intent["request"]["requested"]["appearance_target"])
            else:
                from studio.services.production_recipe_contracts import recipe_execution_contract
                from studio.services.execution_definitions import verify_stored_execution_definition
                from studio.services.production_recipe_material_inputs import recipe_material_binding
                from studio.services.production_recipe_material_registration import read_registered_recipe_primary_dependency
                from rest_framework.exceptions import ValidationError as APIValidationError
                binding = recipe_material_binding(verify_stored_execution_definition(recipe_execution_contract(route)),
                    route["input_sources"], unit_key=unit_key)
                if binding is None:
                    raise ValidationError("变体路线尚未配置本轮主图及当前设定差异来源")
                try:
                    dependency = read_registered_recipe_primary_dependency(stage=stage, unit_key=unit_key)
                except APIValidationError as exc:
                    raise ValidationError(str(exc.detail)) from exc
                if dependency is None:
                    raise ValidationError("本轮确切主图尚未选择或登记，不能改用素材库其他版本")
                appearance_target = normalize_media_appearance_request({**appearance_target, **binding,
                    "primary_version_id": str(dependency["version"].pk)})
        from studio.services.production_stage_input_amendments import stage_input_amendment_identity
        amendment = stage_input_amendment_identity(stage)
        appearance_source = "bible_primary" if stage.stage_key == "bible_image_generation" and role == "primary" else None
        intent = json_contract({"schema_version": 1, "run_plan_sha256": plan.sha256,
            **({"configuration_replacement": configuration_replacement} if configuration_replacement else {}),
            **({"supersedes_preparation_id": str(supersedes.pk)} if supersedes else {}),
            **({"input_amendment": amendment} if amendment else {}),
            "stage_key": stage.stage_key, "scope": scope, "route_index": choice.index, "candidate_number": candidate_number,
            "parameters": {}, "manual_channels": {},
            **({"appearance_target_source": appearance_source} if appearance_source is not None else {}),
            **({"appearance_target": appearance_target} if appearance_target is not None else {}),
            **({"generation_request": generation_request} if generation_request is not None else {})},
            max_bytes=execution_resolved_inputs_max_bytes())

        def resolve(definition, locked_stage):
            run = locked_stage.production_run
            if run.status != "running" or locked_stage.status != "running":
                raise ValidationError("只有正在执行的配方阶段可以新建单元准备")
            stages = plan.value["recipe"]["definition"]["stages"]
            from studio.services.execution_text_output_catalog import plan_text_output_options
            validate_recipe_execution_route(route, stage_key=stage.stage_key,
                prior_stages=[row["key"] for row in stages[:stage.ordinal - 1]], text_outputs=plan_text_output_options(plan))
            validate_stage_execution_definition(stage.stage_key, definition)
            if scope["kind"] == "shot":
                from studio.models import Shot
                if Shot.objects.get(pk=scope["id"]).media_type != output_kind(definition):
                    raise ValidationError("实例产物类型与此镜头规划不一致，请明确配置路线，不自动换用其他模型")
            if definition.value["configuration"]["prompt_mode"] == "manual":
                raise ValidationError("自动制作不能替代人工输入完整提示词，请选择策略组装或无需提示词的实例")
            input_route, material_binding = route, None
            from studio.services.production_recipe_material_inputs import (
                recipe_material_binding, bind_material_sources, recipe_material_primary_reference,
            )
            if recipe_material_binding(definition, route["input_sources"]) is not None:
                if dependency is None:
                    raise ValidationError("动态素材来源必须由本轮确切变体单元准备，不接受手填主图版本")
                from studio.services.bible_setting_evidence import read_setting_revisions
                setting_id = appearance_target["setting_revision_id"]
                setting = read_setting_revisions([setting_id], project_id=run.project_id)[setting_id]
                input_route = {**route, "input_sources": bind_material_sources(route["input_sources"],
                    version=dependency["version"], difference=setting.appearance_anchor)}
                material_binding = {"primary": recipe_material_primary_reference(dependency),
                    "difference": {"setting_revision_id": setting_id,
                    "value_sha256": json_contract(setting.appearance_anchor, max_bytes=execution_configuration_max_bytes()).sha256}}
                if supersedes is not None:
                    from studio.services.production_recipe_material_inputs import read_recipe_material_binding
                    if read_recipe_material_binding(supersedes)["primary"] == material_binding["primary"]:
                        raise ValidationError("主图依据未改变，不得重复分配变体准备")
            sources = resolve_recipe_media_sources(definition=definition, route=input_route, stage=locked_stage,
                plan=plan, scope=scope)
            language = run.project.language
            if not isinstance(language, str) or not language.strip() or len(language) > 40:
                raise ValidationError("项目缺少明确语言，不默认中文或英文")
            seed_basis = None
            if generation_request is not None:
                seed_basis = _new_candidate_seed_basis(definition, locked_stage, unit_key)
            resolved = resolve_media_generation_target(definition, {"inputs": sources["inputs"], "parameters": {},
                "manual_channels": {}, "subject_facts": [], "source_language": language, "target_language": language},
                candidate_count=routing["variant_count"], candidate_evaluator=routing["evaluator"], appearance_target=appearance_target,
                appearance_target_source=appearance_source,
                owner=run.owner, unit_key=unit_key, max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                review_contract=production_field_review_contract(locked_stage),
                parameter_policies=bind_recipe_parameter_sources(route["parameter_policies"], run=run,
                    definition=definition, scope=scope, eligible_stage_ids=sources["eligible_stage_ids"], stage_key=locked_stage.stage_key),
                resolve_parameter_source=resolved_recipe_parameter_source, project_id=run.project_id,
                production_run_id=str(run.pk), eligible_stage_ids=sources["eligible_stage_ids"], production_scope=scope,
                new_candidate_seed_exclusions=seed_basis["excluded_values"] if seed_basis is not None else None)
            # Provenance travels with the same immutable detail and intent hash;
            # replay never rereads/replaces this original review selection.
            facts = {**resolved.resolved_inputs.value, "recipe_input_review": sources["review"]}
            if material_binding is not None:
                facts["recipe_material_binding"] = material_binding
            if frozen_requirement is not None:
                requirement = _material_requirement(locked_stage, scope, appearance_target=appearance_target)
                if (requirement != frozen_requirement
                        or facts["media_generation_target"]["setting"] != requirement["target"]["setting"]):
                    raise ValidationError("候选设定与本轮已审核素材需求不一致，不能创建其他修订的准备")
                if role == "variant":
                    from studio.services.bible_setting_evidence import read_setting_revisions, primary_setting_content_reference
                    setting_id = requirement["target"]["setting"]["setting_revision_id"]
                    setting = read_setting_revisions([setting_id], project_id=run.project_id)[setting_id]
                    appearance = facts["media_generation_target"]["appearance"]
                    if (primary_setting_content_reference(setting.primary_setting_revision) != requirement["target"]["primary_dependency"]
                            or appearance["difference"]["value_sha256"] != json_contract(setting.appearance_anchor,
                                max_bytes=execution_configuration_max_bytes()).sha256):
                        raise ValidationError("变体实际差异或主图依赖与本轮已审核设定不一致，不能用无关正文替代")
                facts["recipe_material_requirement"] = requirement
            if seed_basis is not None:
                facts["new_candidate_seed_basis"] = seed_basis
            return ResolvedExecutionPreparation(json_contract(facts, max_bytes=execution_resolved_inputs_max_bytes()), resolved.review_contract)

        preparation = create_execution_instance_preparation(owner_ref=owner_ref,
            version_id=route["execution_instance_version_id"], expected_definition_sha256=route["expected_definition_sha256"],
            unit_key=unit_key, request_key=candidate_request_key(choice.index, candidate_number,
                supersedes_id=supersedes.pk if supersedes else (choice.preparation.supersedes_id if choice.preparation else None)),
            intent=intent, resolve_facts=resolve, actor=actor,
            supersedes=supersedes or (choice.preparation.supersedes if choice.preparation and choice.preparation.supersedes_id else None))
        record_recipe_route_preparation(stage=stage, preparation=preparation, choice=choice, candidate_number=candidate_number)
        return preparation


def _new_candidate_seed_basis(definition, stage, unit_key):
    """Read original, hash-verified preparations under the same owner lock.

    Called only by first resolution; duplicate actions/retries never enter it.
    No current project seed, task output, or cross-entity seed is substituted.
    """
    from studio.models import ExecutionPreparation
    from studio.services.execution_parameters import execution_seed_parameter_keys
    from studio.services.execution_preparations import read_execution_preparation_detail

    interface = definition.value["interface"]
    rules = {**interface.get("template_constants", {}), **interface["parameters"]}
    keys = execution_seed_parameter_keys(rules, workflow=definition.value["target"]["driver_type"] == "comfyui")
    if keys - interface["parameters"].keys():
        raise ValidationError("种子已被执行合同锁定，不能生成新种子；请调整版本后再生成候选")
    excluded = {key: set() for key in sorted(keys)}
    previous = []
    preparations = ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit_key).select_related("contract").order_by("created_at", "id")
    for preparation in preparations:
        detail = read_execution_preparation_detail(preparation, detail=getattr(preparation, "contract", None))
        values = detail.resolved_inputs["parameters"]["values"]
        for key in keys & values.keys():
            if type(values[key]) is not int:
                raise ValidationError("原候选种子不是明确整数，不能静默忽略")
            excluded[key].add(values[key])
        previous.append({"preparation_id": str(preparation.pk), "resolved_inputs_sha256": detail.resolved_inputs_sha256})
    return {"policy": "new_candidate", "prior_preparations": previous,
            "excluded_values": {key: sorted(values) for key, values in excluded.items()}}
