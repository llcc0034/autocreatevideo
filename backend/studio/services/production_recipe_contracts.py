"""Current recipe authoring contracts, not production admission or dispatch.

Recipes reference exact execution versions. Source rules may refer to a future
reviewed selection; this validator never substitutes current media, resolves a
seed, calls a model, or manufactures a successful production result.
"""
from copy import deepcopy
from contextlib import contextmanager
import re

from django.core.exceptions import ValidationError

from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_input_catalog import execution_context_field_options, execution_text_output_options
from studio.services.execution_shot_field_mapping import shot_field_mapping_cache
from studio.services.execution_step_parameters import validate_execution_step_parameter_policies
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_recipe_routing_contract import recipe_route_reference, validate_recipe_routing_shape
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _error(path, message):
    raise ValidationError({path: message})


@contextmanager
def recipe_validation_context(**context):
    """Attach known authoring identities without replacing the original error."""
    try:
        yield
    except ValidationError as exc:
        # Inner route identity is more precise than its enclosing stage. No
        # values, configuration body or arbitrary exception text is attached.
        exc.recipe_validation_context = {**context, **getattr(exc, "recipe_validation_context", {})}
        raise


def recipe_validation_errors(exc, *, phase, stages=()):
    """Project only a contract ValidationError for the authorized admin reader."""
    context = getattr(exc, "recipe_validation_context", {})
    groups = exc.error_dict if hasattr(exc, "error_dict") else {"__all__": exc.error_list}
    rows = []
    for path, errors in groups.items():
        identity = dict(context)
        # A shape validator's explicit indexed path is evidence. Whole-recipe
        # errors must not be attributed to a guessed first/last stage.
        indexed = re.match(r"^definition\.stages\[(\d+)\](?:\.|$)", path)
        if indexed and "stage_index" not in identity:
            index = int(indexed.group(1))
            if isinstance(stages, list) and index < len(stages) and isinstance(stages[index], dict):
                stage = stages[index]
                identity.update(stage_index=index, stage_key=stage.get("key"), stage_kind=stage.get("kind", stage.get("key")))
        for error in errors:
            rows.append({"phase": phase, "field_path": path, "message": error.messages[0], "code": error.code,
                **{key: identity.get(key) for key in ("stage_index", "stage_key", "stage_kind", "routing_key", "route_index")}})
    return rows


def _object(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        _error(path, "必须明确提供全部合同字段，不接受旧资源组合或额外字段")


def recipe_stage_kind(stage):
    """Read a declaration's duty, not its unique step key or display name.

    Singleton duties use their registered key. Repeatable duties must declare
    a kind explicitly; only a catalog registering that kind can accept them.
    """
    return stage["kind"] if "kind" in stage else stage["key"]


def recipe_stage_routings(stage):
    """Explicit route groups; a missing variant group never inherits primary."""
    if "variant_routing" in stage and (recipe_stage_kind(stage) != "bible_image_generation"
            or stage.get("mode") != "existing_pipeline"):
        _error("variant_routing", "只有素材主图/变体生成阶段可声明变体路线")
    return [(key, stage[key]) for key in ("routing", "variant_routing") if key in stage]


def recipe_stage_declarations(definition, catalog=None):
    """Pure shape/order check shared by authoring and immutable RunPlan reads."""
    json_contract(definition, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if not isinstance(definition, dict) or type(definition.get("schema_version")) is not int or definition["schema_version"] != 3:
        _error("definition.schema_version", "仅接受 schema3 配方，不转换历史合同")
    _object(definition, {"schema_version", "interaction_mode", "stages"}, "definition")
    if definition["interaction_mode"] not in ("assisted", "unattended"):
        _error("definition.interaction_mode", "请选择辅助制作或无人值守")
    catalog = production_stage_catalog().value if catalog is None else catalog
    entries = {row["key"]: row for row in catalog["stages"]}
    order = [row["key"] for row in catalog["stages"] if not row.get("repeatable", False)]
    modes = {(row["key"], mode) for row in catalog["stages"] for mode in row["modes"]}
    stages = definition["stages"]
    if not isinstance(stages, list) or not stages:
        _error("definition.stages", "需要受控目录内的非空阶段集合")
    seen, singleton_order = set(), []
    for index, stage in enumerate(stages):
        path = f"definition.stages[{index}]"
        if not isinstance(stage, dict) or not {"key", "mode", "approval"} <= set(stage) or set(stage) - {
                "key", "kind", "name", "mode", "approval", "decision_policy", "routing", "variant_routing", "field_review", "material_sources", "material_selection", "shot_reference_selection", "assembly_output", "assembly_timing", "assembly_media", "delivery_metadata"}:
            _error(path, "阶段必须明确声明职责、执行方式和审核方式")
        key, kind, mode = stage["key"], recipe_stage_kind(stage), stage["mode"]
        recipe_stage_routings(stage)
        from studio.services.production_recipe_material_sources import validate_material_sources
        validate_material_sources(stage)
        from studio.services.production_recipe_shot_references import validate_shot_reference_selection
        validate_shot_reference_selection(stage, stages[:len(seen)])
        from studio.services.production_recipe_assembly_output import PRODUCTION_AUTHOR_KEYS, validate_recipe_assembly_output_shape
        for author_key in PRODUCTION_AUTHOR_KEYS:
            validate_recipe_assembly_output_shape(stage, key=author_key)
        if not isinstance(key, str) or not key.strip() or key != key.strip() or len(key) > 100:
            _error(path + ".key", "步骤身份必须为1至100字符的非空标识")
        if (not isinstance(kind, str) or not isinstance(mode, str)
                or (kind, mode) not in modes or key in seen):
            _error(path, "阶段重复或执行方式不在受控目录内")
        repeatable = entries[kind].get("repeatable", False)
        if repeatable:
            if "kind" not in stage or key in entries:
                _error(path + ".key", "可重复职责需要独立步骤身份，不能占用目录职责标识")
            name = stage.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 160:
                _error(path + ".name", "请为步骤填写1至160字符的显示名称；名称不作为身份")
        else:
            if "kind" in stage or "name" in stage:
                _error(path, "单次业务职责使用原目录身份，不接受另一套职责或名称别名")
            singleton_order.append(key)
        if stage["approval"] not in ("auto", "required", "on_warning", "policy"):
            _error(path + ".approval", "审核方式未注册")
        if "field_review" in stage:
            from studio.services.execution_field_review import validate_field_review_choice
            # Frozen historical plans keep the exact policy identity. Current
            # registration is checked before a new approval, never replaced.
            validate_field_review_choice(stage["field_review"], interaction_mode=definition["interaction_mode"], current=False)
            if stage["field_review"]["mode"] == "policy" and mode in {"manual", "skip", "review"}:
                _error(path + ".field_review", "此阶段职责不能自动批准生成字段")
        seen.add(key)
    if singleton_order != sorted(singleton_order, key=order.index):
        _error("definition.stages", "阶段顺序与受控生产职责顺序不一致")
    return deepcopy(stages)


def _rule_scope(scope, path):
    # A recipe describes a unit scope, not an already chosen project/shot.
    # Concrete IDs come from the authorized RunPlan when a unit is prepared.
    _object(scope, {"kind"}, path)
    if scope["kind"] not in ("request", "project", "episode", "shot"):
        _error(path, "配方来源范围必须是本次明确单元或本轮项目、剧集、镜头")


def _source_rule(source, *, kind, prior_stages, path, text_outputs=None, structured_leaf=False):
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        _error(path, "必须提供明确的来源规则")
    if source["kind"] == "material_difference":
        _object(source, {"kind"}, path)
        if kind != "text" or "bible_review" not in prior_stages:
            _error(path, "变体差异须使用本轮更早Bible审核的文字")
    elif source["kind"] == "literal":
        _object(source, {"kind", "value"}, path)
        if kind not in ("text", "json") or (kind == "text" and not isinstance(source["value"], str)):
            _error(path, "字面值仅适用于对应的文本或 JSON 输入")
    elif source["kind"] == "shot_field":
        _object(source, {"kind", "field", "scope"}, path)
        if not structured_leaf:
            _error(path, "独立字段须通过共用结构映射接入，不是另一套直接输入协议")
        if source["scope"] != {"kind": "shot"} or "storyboard_review" not in prior_stages:
            _error(path, "独立字段必须来自本轮更早的分镜审核和当前镜头")
        from studio.services.execution_shot_field_mapping import shot_field_source_option
        if shot_field_source_option(source)["kind"] != kind:
            _error(path, "独立字段类型与确切结构不一致")
    elif source["kind"] == "context_field":
        _object(source, {"kind", "selector_key", "scope"}, path)
        _rule_scope(source["scope"], path + ".scope")
        rows = [row for row in execution_context_field_options() if row["selector_key"] == source["selector_key"]]
        if len(rows) != 1 or rows[0]["kind"] != kind or rows[0]["scope_kind"] != source["scope"]["kind"]:
            _error(path, "上下文字段、类型或范围不在受控目录内")
        if rows[0]["revision_kind"] == "storyboard_approval" and "storyboard_review" not in prior_stages:
            _error(path, "当前镜头内容必须来自本轮更早的分镜审核阶段")
        if rows[0]["revision_kind"] == "bible_approval" and "bible_review" not in prior_stages:
            _error(path, "当前实体内容必须来自本轮更早的Bible审核阶段")
        if rows[0].get("required_stage_key") and rows[0]["required_stage_key"] not in prior_stages:
            _error(path, "此来源需要本轮更早的影视规则审核阶段")
        if any(item["stage_key"] not in prior_stages for item in rows[0].get("revision_dependencies", [])):
            _error(path, "关联内容必须同时具备目录要求的更早审核阶段（分镜与 Bible）")
    elif source["kind"] == "selected_output":
        if kind not in {"text", "json"}:
            _error(path, "媒体输入必须配置已审核参考清单的逐项映射，不能只填写上游输出标识")
        _object(source, {"kind", "stage_key", "output_key", "scope", "selection_policy"}, path)
        _rule_scope(source["scope"], path + ".scope")
        if (not isinstance(source["stage_key"], str) or source["stage_key"] not in prior_stages
                or source["selection_policy"] != "reviewed_selected"):
            _error(path, "只能引用本配方中更早阶段明确审核采用的输出，不能引用自身、未来或最新候选")
        if not isinstance(source["output_key"], str) or not source["output_key"].strip() or len(source["output_key"]) > 191:
            _error(path, "需要明确的上游输出标识；实际选片和类型在准备时核对")
        if kind in {"text", "json"} and not any(row["output_key"] == source["output_key"]
                and row["stage_key"] == source["stage_key"] and row["kind"] == kind
                and row["scope_kind"] == source["scope"]["kind"]
                for row in (execution_text_output_options() if text_outputs is None else text_outputs)):
            _error(path, "文本输出、上游职责、类型及范围必须来自同一受控目录项")
    else:
        _error(path, "来源规则未注册；不接受路径、表达式或独立执行器")


@shot_field_mapping_cache()
def validate_recipe_input_sources(definition, sources, *, prior_stages, require_complete=True, text_outputs=None):
    """Check author choices only; future selection counts are not invented."""
    json_contract(sources, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    from studio.services.execution_input_structures import input_structure_schemas, input_template_for_validation
    from studio.services.execution_structure_mapping import structure_target_schemas, validate_template_schema
    from studio.services.data_structures import _value_errors
    input_schemas = input_structure_schemas(definition.value)

    def validate_logical_value(value, schema, path):
        errors = _value_errors(schema, value, path=path)
        if errors:
            raise ValidationError(errors)

    def validate_template(key, spec, template, path):
        preview = input_template_for_validation(definition.value, key, template, path=path)
        if key in input_schemas:
            validate_template_schema(preview, input_schemas[key], path, validate_value=validate_logical_value, text_outputs=text_outputs)
        for schema in structure_target_schemas(definition, spec):
            validate_template_schema(preview, schema, path, text_outputs=text_outputs)
    specs = {row["key"]: row for row in definition.value["configuration"]["inputs"]}
    if not isinstance(sources, dict) or set(sources) - set(specs):
        _error("input_sources", "来源必须按实例已声明的输入 key 配置")
    for key, spec in specs.items():
        path = f"input_sources.{key}"
        if key not in sources:
            if require_complete and (spec["required"] or spec["min_items"]):
                _error(path, "必需输入未配置来源规则")
            continue
        source = sources[key]
        # A future selection is resolved to real, ordered items in the unit
        # adapter. It does not pretend to satisfy dynamic cardinality here.
        if isinstance(source, dict) and source.get("kind") == "material_primary":
            from studio.services.production_recipe_material_inputs import validate_material_primary
            validate_material_primary(source, spec)
        elif isinstance(source, dict) and source.get("kind") == "media_selection":
            from studio.services.production_recipe_media_selection import bind_recipe_media_selection
            bind_recipe_media_selection(source, spec)
        elif isinstance(source, dict) and source.get("kind") == "reviewed_references":
            from studio.services.production_reference_mapping import validate_reference_mapping
            validate_reference_mapping(source, spec=spec, prior_stages=prior_stages, path=path, require_complete=require_complete)
        elif isinstance(source, dict) and source.get("kind") == "project_snapshot":
            from studio.services.execution_project_sources import input_project_reader
            _object(source, {"kind"}, path)
            if (spec["kind"] != "text" or not spec["min_items"] <= 1 <= spec["max_items"]
                    or "project" not in spec["semantic_contract"]["allowed_scope_kinds"]
                    or input_project_reader(spec, definition.value["interface"]) is None):
                _error(path, "项目快照必须由精确输入合同声明项目读取协议，不允许自填来源路径")
        elif isinstance(source, dict) and source.get("kind") == "structured":
            from studio.services.execution_structure_mapping import validate_structure
            _object(source, {"kind", "template"}, path)
            if spec["kind"] != "json" or not spec["min_items"] <= 1 <= spec["max_items"]:
                _error(path, "结构映射仅用于实例明确声明的单值JSON输入")
            def validate_leaf(node, option, leaf_path):
                _source_rule(node, kind=option["kind"], prior_stages=prior_stages, path=leaf_path,
                    text_outputs=text_outputs, structured_leaf=True)
                if node["kind"] != "material_difference" and node["scope"]["kind"] not in spec["semantic_contract"]["allowed_scope_kinds"]:
                    _error(leaf_path, "实例输入没有开放该上下文范围")
            validate_structure(source["template"], leaf_validator=validate_leaf, path=path, text_outputs=text_outputs, allow_material_difference=True)
            if require_complete:
                validate_template(key, spec, source["template"], path)
        else:
            _source_rule(source, kind=spec["kind"], prior_stages=prior_stages, path=path, text_outputs=text_outputs)
        if source["kind"] in {"literal", "material_difference"} and not spec["min_items"] <= 1 <= spec["max_items"]:
            _error(path, "单个字面值不满足输入数量合同")
        if require_complete and source["kind"] == "literal" and key in input_schemas:
            validate_template(key, spec, source, path)
        if source["kind"] in {"context_field", "selected_output"} and source["scope"]["kind"] not in spec["semantic_contract"]["allowed_scope_kinds"]:
            _error(path, "实例输入没有开放该上下文范围")
        for target in spec["targets"]:
            if target["target_kind"] not in {"operation_input", "profile_input"}:
                continue
            group = "profile_inputs" if target["target_kind"] == "profile_input" else "input_targets"
            target_spec = definition.value["interface"][group][target["target_key"]]
            resolved_kind = "literal" if source["kind"] == "material_difference" else source["kind"]
            if "source_kinds" in target_spec and resolved_kind not in target_spec["source_kinds"]:
                _error(path, "来源不满足目标业务协议，不能用字面值伪造已确认上下文")
            if (source["kind"] == "context_field" and "context_selectors" in target_spec
                    and source["selector_key"] not in target_spec["context_selectors"]):
                _error(path, "所选上下文字段与目标业务协议不一致")
            if (source["kind"] == "selected_output" and "selected_output_keys" in target_spec
                    and source["output_key"] not in target_spec["selected_output_keys"]):
                _error(path, "所选已采用输出与目标业务协议不一致")


def recipe_execution_reference(route):
    """Validate author-supplied route identity before lookup or cache indexing."""
    return recipe_route_reference(route)


def recipe_execution_contract(route):
    """Resolve an exact route identity; no readiness or input interpretation."""
    identifier, digest = recipe_execution_reference(route)
    contract = stored_execution_contract(identifier)
    if digest != contract.sha256:
        _error("route.expected_definition_sha256", "执行定义已变化，请重新读取并确认")
    return contract


@security_boundary_resolution_cache()
def _recipe_route_definition(route, *, prior_stages, require_complete=True, stage_key=None, text_outputs=None):
    """Verify one current route and its sources, without dispatch or a new snapshot."""
    contract = recipe_execution_contract(route)
    if contract.version.status != "published" or not contract.version.instance.is_active:
        _error("route.execution_instance_version_id", "实例必须启用且精确版本已发布")
    definition = verify_current_execution_definition(contract)
    if stage_key is not None:
        from studio.services.production_stage_catalog import validate_stage_execution_definition
        try:
            validate_stage_execution_definition(stage_key, definition)
        except ValidationError as exc:
            _error("route.execution_instance_version_id", "; ".join(exc.messages))
    validate_recipe_input_sources(definition, route["input_sources"], prior_stages=prior_stages,
        require_complete=require_complete, text_outputs=text_outputs)
    from studio.services.production_recipe_material_inputs import recipe_material_binding
    if recipe_material_binding(definition, route["input_sources"], require_complete=require_complete) is not None:
        if stage_key != "bible_image_generation":
            _error("input_sources", "本轮主图和设定差异只能用于Bible变体素材职责")
    from studio.services.execution_structure_mapping import structure_leaves
    route_sources = [leaf for source in route["input_sources"].values()
        for leaf in (structure_leaves(source["template"]) if source["kind"] == "structured" else [source])]
    if stage_key is not None and stage_key != "media_generation" and any(
            row["kind"] in {"context_field", "shot_field"} and row["scope"] == {"kind": "shot"} for row in route_sources):
        _error("route.input_sources", "逐镜上下文只能用于当前镜头的媒体生成职责")
    if stage_key is not None and stage_key != "bible_image_generation" and any(
            row["kind"] == "context_field" and row["scope"] == {"kind": "request"} for row in route_sources):
        _error("route.input_sources", "当前实体上下文只能用于Bible参考图生成职责")
    layers = {key: row["layers"] for key, row in definition.value["interface"]["parameters"].items()}
    parameters = validate_execution_step_parameter_policies(layers, route["parameter_policies"])
    from studio.services.execution_resource_parameters import has_system_output
    if has_system_output(definition.value["interface"]) and any(row["key"] == "max_tokens" for row in route["parameter_policies"]):
        _error("parameter_policies", "输出长度由系统资源配置提供，配方不能覆盖")
    for policy in route["parameter_policies"]:
        if policy["policy"] == "locked_source":
            from studio.services.production_parameter_sources import validate_recipe_parameter_source
            try:
                validate_recipe_parameter_source(policy["source"], parameter_type=parameters[policy["key"]]["rule"]["type"],
                    require_complete=require_complete, parameter_key=policy["key"], definition=definition, stage_key=stage_key)
            except ValidationError as exc:
                _error("parameter_policies", "; ".join(exc.messages))
    return definition


@registry_reference_writer
def validate_recipe_execution_route(route, *, prior_stages, require_complete=True, stage_key=None, text_outputs=None):
    _recipe_route_definition(route, prior_stages=prior_stages,
        require_complete=require_complete, stage_key=stage_key, text_outputs=text_outputs)
    return deepcopy(route)


def validate_recipe_route_output_identity(definitions):
    """One step has one typed output, even when its model route can change.

    Receives verified definitions so callers retain their current/frozen read
    boundary. Media protocols without BusinessOutputVersion are unchanged.
    """
    identities = []
    for definition in definitions:
        value = definition.value
        output = value["interface"].get("business_output")
        identities.append(None if output is None else (
            value["configuration"]["operation"], output["id"], output["sha256"]))
    if not any(identity is not None for identity in identities):
        return
    expected = identities[0]
    for index, identity in enumerate(identities):
        if identity != expected:
            with recipe_validation_context(route_index=index):
                _error(f"routing.routes[{index}].execution_instance_version_id",
                    "同一步的所有路线必须使用相同能力、确切输出版本及摘要；不能按相同字段名合并不同输出合同")


def verify_recipe_routing_output_identity(routing):
    """Check frozen alternatives before preparation, not current availability."""
    from studio.services.execution_definitions import verify_stored_execution_definition
    routes = routing["routes"]
    if len(routes) < 2:
        return
    definitions = []
    for index, route in enumerate(routes):
        contract = stored_execution_contract(route["execution_instance_version_id"])
        if route["expected_definition_sha256"] != contract.sha256:
            _error(f"routing.routes[{index}].expected_definition_sha256", "冻结路线与确切执行定义摘要不一致")
        definitions.append(verify_stored_execution_definition(contract))
    validate_recipe_route_output_identity(definitions)


def verify_recipe_successor_references(definition):
    """Copy authority only: frozen identities, never current execution readiness.

    Saving or publishing the successor still uses validate_recipe_definition.
    In particular, a newly registered input must not prevent opening the old
    recipe to replace its execution version.
    """
    from studio.models import BusinessOutputVersion
    from studio.services.business_outputs import output_reference
    from studio.services.execution_definitions import verify_stored_execution_definition
    from studio.services.execution_shot_field_mapping import shot_field_source_option
    from studio.services.execution_structure_mapping import structure_leaves
    from studio.services.production_recipe_assembly_output import PRODUCTION_AUTHOR_KEYS

    for stage in recipe_stage_declarations(definition):
        for _, routing in recipe_stage_routings(stage):
            validate_recipe_routing_shape(routing, require_complete=False)
            for route in routing["routes"]:
                frozen = verify_stored_execution_definition(recipe_execution_contract(route))
                for identity in frozen.value["interface"]["auxiliaries"].values():
                    leaf = verify_stored_execution_definition(stored_execution_contract(identity["version_id"]))
                    if (leaf.sha256 != identity["definition_sha256"]
                            or leaf.value["target_fingerprint"] != identity["target_fingerprint"]):
                        _error("auxiliaries", "辅助版本与冻结执行定义的引用不一致")
                for source in route["input_sources"].values():
                    if source.get("kind") == "structured":
                        for node in structure_leaves(source["template"]):
                            if node["kind"] == "shot_field":
                                shot_field_source_option(node)
        for key in PRODUCTION_AUTHOR_KEYS:
            if key in stage:
                rule = stage[key]
                output = BusinessOutputVersion.objects.filter(pk=rule["output_version_id"]).first()
                if output is None or output_reference(output)["sha256"] != rule["expected_contract_sha256"]:
                    _error(key, "所选输出版本不存在或冻结摘要不一致")


@registry_reference_writer
def validate_recipe_execution_routing(routing, *, prior_stages, require_complete=True, stage_key=None, text_outputs=None):
    return read_recipe_execution_routing(routing, prior_stages=prior_stages,
        require_complete=require_complete, stage_key=stage_key, text_outputs=text_outputs)


def read_recipe_execution_routing(routing, *, prior_stages, require_complete=True, stage_key=None, text_outputs=None):
    """The same current-route checks, without acquiring a reference-writer lock."""
    validate_recipe_routing_shape(routing, require_complete=require_complete)
    routes = routing["routes"]
    if len(routes) > boundary_int("security.production.max_routing_routes"):
        _error("routing.routes", "路线数量必须在平台明确配置的范围内")
    if routing["variant_count"] > boundary_int("security.production.max_variants"):
        _error("routing.variant_count", "候选数量必须在平台明确配置的范围内")
    definitions = []
    for index, route in enumerate(routes):
        with recipe_validation_context(route_index=index):
            definitions.append(_recipe_route_definition(route, prior_stages=prior_stages,
                require_complete=require_complete, stage_key=stage_key, text_outputs=text_outputs))
    validate_recipe_route_output_identity(definitions)
    return deepcopy(routing)


def validate_recipe_stage_policies(stage, *, interaction_mode, current=True):
    """Stage approval and field approval are separate, explicit authorities."""
    from studio.services.execution_field_review import validate_field_review_choice
    from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS, UNATTENDED_DECISION_POLICY_REGISTRY
    unattended = interaction_mode == "unattended"
    policy = stage.get("decision_policy")
    if STAGE_EXECUTION_OPERATIONS.get(recipe_stage_kind(stage)) == "text.structured":
        if stage["approval"] != "auto" or policy is not None:
            _error("approval", "通用文本步骤按候选选择结果推进，不另设阶段审核；请配置auto及候选选择方式")
        if unattended and "routing" in stage and stage["routing"].get("evaluator") != "first_succeeded":
            _error("routing.evaluator", "无人值守通用文本步骤需要自动选择候选")
    if not unattended:
        if stage["approval"] == "policy" or policy is not None:
            _error("decision_policy", "辅助制作不能采用无人值守阶段审核策略")
    elif stage["mode"] in {"manual", "skip"} or stage["approval"] in {"required", "on_warning"}:
        _error("approval", "无人值守不能包含等待人工动作的阶段")
    elif stage["mode"] == "review":
        expected = UNATTENDED_DECISION_POLICY_REGISTRY.get(stage["key"])
        if stage["approval"] != "policy" or not expected or policy != expected:
            _error("decision_policy", "无人值守审核必须明确选择该阶段已注册的审核策略")
    elif stage["approval"] != "auto" or policy is not None:
        _error("approval", "无人值守非审核阶段必须声明自动推进，不得冒用阶段审核策略")
    if "field_review" in stage:
        validate_field_review_choice(stage["field_review"], interaction_mode=interaction_mode, current=current)


@registry_reference_writer
def validate_recipe_definition(definition, *, require_complete=False):
    """The schema3 draft authoring boundary, not dispatch or Smoke admission.

    An empty route list or omitted required input can remain in a draft. Once a
    route/source/parameter is supplied it must be valid; we never erase a bad
    reference or convert an old resource combination into an execution version.
    """
    return read_recipe_definition(definition, require_complete=require_complete)


@security_boundary_resolution_cache()
def read_recipe_definition(definition, *, require_complete=False):
    """Read current contracts with the authoring core, never dispatch or write."""
    from studio.services.production_stage_catalog import PROJECT_TEXT_REVIEW_SOURCES, stage_requires_execution_route
    stages = recipe_stage_declarations(definition)
    text_outputs = execution_text_output_options(stages)
    prior = []
    for index, stage in enumerate(stages):
        kind = recipe_stage_kind(stage)
        with recipe_validation_context(stage_index=index, stage_key=stage["key"], stage_kind=kind):
            from studio.services.production_recipe_assembly_output import PRODUCTION_AUTHOR_KEYS, validate_recipe_assembly_author
            for author_key in PRODUCTION_AUTHOR_KEYS:
                if author_key in stage:
                    validate_recipe_assembly_author(stage, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES,
                        prior_stages=prior, text_outputs=text_outputs, key=author_key)
            validate_recipe_stage_policies(stage, interaction_mode=definition["interaction_mode"])
            producers = PROJECT_TEXT_REVIEW_SOURCES.get(kind)
            if require_complete and producers is not None and not producers.intersection(prior):
                _error("stages", f"审核阶段 {stage['key']} 缺少对应的候选生成阶段；已确认的导入原稿不重复作为AI候选审核")
            required = stage_requires_execution_route(kind, stage["mode"])
            if not required:
                if "routing" in stage:
                    _error("routing", "人工、审核和确定性阶段不能声明模型调用路线")
            elif "routing" not in stage:
                from studio.services.production_recipe_material_sources import recipe_material_source
                primary_generated = kind != "bible_image_generation" or recipe_material_source(stage, "primary") == "generate"
                if require_complete and primary_generated:
                    _error("routing", f"阶段 {stage['key']} 尚未选择执行实例版本")
            for routing_key, routing_value in recipe_stage_routings(stage):
                with recipe_validation_context(routing_key=routing_key):
                    routing = read_recipe_execution_routing(routing_value, prior_stages=prior,
                        require_complete=require_complete, stage_key=kind, text_outputs=text_outputs)
                    from studio.services.production_recipe_material_inputs import has_material_sources
                    if routing_key != "variant_routing" and any(has_material_sources(route["input_sources"]) for route in routing["routes"]):
                        _error("input_sources", "本轮主图及差异绑定只能配置在变体路线，不用于主图生成")
                    if definition["interaction_mode"] == "unattended" and routing["mode"] == "manual_choice":
                        _error(routing_key + ".mode", "无人值守不能等待人工选路")
        prior.append(stage["key"])
    return stages
