"""Explicit pre-execution input correction, not replanning.

The original RunPlan, raw context, model, routes, parameters, reviews and output
facts stay unchanged. Only the signed, bounded mapping amendment is applied.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionStageInputAmendment
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_owners import locked_preparation_owners
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.registry_reference_lock import registry_reference_writer


POLICY = "dialogue_missing_voiceover_is_false_v1"
SOURCE_POLICY = "explicit_media_input_sources_v1"
TEXT_SOURCE_POLICY = "explicit_project_text_input_sources_v1"
SOURCE_POLICIES = (SOURCE_POLICY, TEXT_SOURCE_POLICY)
POLICIES = (POLICY, *SOURCE_POLICIES)
DIALOGUE_FIELDS = ("speaker_id", "text", "delivery", "subject", "voice_profile", "voiceover", "start_ms", "end_ms")


def _contract(value):
    return json_contract(value, max_bytes=min(65536, execution_configuration_max_bytes()))


def _proposal(plan, declaration):
    if declaration["key"] != "media_generation" or declaration["mode"] != "existing_pipeline":
        raise ValidationError("此修订仅适用于尚未执行的配方媒体阶段")
    effective = deepcopy(declaration)
    changes = []

    def visit(node, path, route_index, input_key):
        if not isinstance(node, dict):
            return
        if node.get("kind") == "object":
            for key, child in node["fields"].items():
                child_path = [*path, "fields", key]
                if key == "dialogue" and child == {"kind": "context_field",
                        "selector_key": "shot.dialogue_with_speakers", "scope": {"kind": "shot"}}:
                    replacement = {"kind": "each", "source": deepcopy(child), "item": {"kind": "object", "fields": {
                        name: {"kind": "item_field", "path": [name], **({"if_missing": False} if name == "voiceover" else {})}
                        for name in DIALOGUE_FIELDS}}}
                    changes.append({"route_index": route_index, "input_key": input_key, "path": child_path,
                        "before": deepcopy(child), "after": deepcopy(replacement)})
                    node["fields"][key] = replacement
                else:
                    visit(child, child_path, route_index, input_key)
        elif node.get("kind") == "array":
            for index, child in enumerate(node["items"]):
                visit(child, [*path, "items", index], route_index, input_key)
        elif node.get("kind") == "unit_map":
            visit(node["value"], [*path, "value"], route_index, input_key)
        elif node.get("kind") == "each":
            visit(node["item"], [*path, "item"], route_index, input_key)

    for index, route in enumerate(effective["routing"]["routes"]):
        for key, source in route["input_sources"].items():
            if source.get("kind") == "structured":
                visit(source["template"], ["template"], index, key)
    if not changes:
        raise ValidationError("没有可修订的原始对白直连映射；不会覆盖自定义转换或其他输入")
    return effective, _contract({"schema_version": 1, "policy": POLICY, "run_plan_sha256": plan.sha256,
        "original_stage_sha256": _contract(declaration).sha256, "effective_stage_sha256": _contract(effective).sha256,
        "changes": changes})


def source_amendment_policy(declaration):
    from studio.services.production_recipe_contracts import recipe_stage_kind
    from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
    kind = recipe_stage_kind(declaration)
    if kind == "media_generation" and declaration["mode"] == "existing_pipeline":
        return SOURCE_POLICY
    if declaration["mode"] == "ai" and STAGE_EXECUTION_OPERATIONS.get(kind) in PRODUCTION_TEXT_OPERATIONS:
        return TEXT_SOURCE_POLICY
    return None


def _source_proposal(plan, declaration, source_changes, policy):
    """Explicit text/JSON source edits only, against the original exact routes."""
    from studio.services.production_recipe_contracts import recipe_execution_contract
    from studio.services.execution_definitions import verify_stored_execution_definition
    if source_amendment_policy(declaration) != policy:
        raise ValidationError("来源修订规则与阶段职责不一致")
    _contract(source_changes)  # Bound the request before traversing it.
    if not isinstance(source_changes, list) or not source_changes:
        raise ValidationError("请明确提供要修订的文本或JSON输入来源")
    effective, changes, seen, definitions = deepcopy(declaration), [], set(), {}
    routes = effective["routing"]["routes"]
    for row in source_changes:
        if not isinstance(row, dict) or set(row) != {"route_index", "input_key", "source"}:
            raise ValidationError("来源修订仅接受路线序号、输入字段和新来源")
        index, key, source = row["route_index"], row["input_key"], row["source"]
        if (type(index) is not int or not 0 <= index < len(routes) or not isinstance(key, str)
                or not isinstance(source, dict) or (index, key) in seen):
            raise ValidationError("修订路线、字段或来源无效，且不允许重复修改同一输入")
        seen.add((index, key))
        route = routes[index]
        if index not in definitions:
            definitions[index] = verify_stored_execution_definition(recipe_execution_contract(route))
        specs = definitions[index].value["configuration"]["inputs"]
        if key not in route["input_sources"] or not any(spec["key"] == key and spec["kind"] in {"text", "json"} for spec in specs):
            raise ValidationError("只能修订原路线已声明的文本或JSON输入；不能更换媒体、模型或参数")
        before = route["input_sources"][key]
        if before == source:
            raise ValidationError("来源没有变化，请移除未修改项")
        changes.append({"route_index": index, "input_key": key, "path": [],
            "before": deepcopy(before), "after": deepcopy(source)})
        route["input_sources"][key] = deepcopy(source)
    changes.sort(key=lambda row: (row["route_index"], row["input_key"]))
    return effective, _contract({"schema_version": 1, "policy": policy, "run_plan_sha256": plan.sha256,
        "original_stage_sha256": _contract(declaration).sha256, "effective_stage_sha256": _contract(effective).sha256,
        "changes": changes})


def _policy_proposal(plan, declaration, policy, source_changes):
    if policy == POLICY and source_changes is None:
        return _proposal(plan, declaration)
    if policy in SOURCE_POLICIES:
        return _source_proposal(plan, declaration, source_changes, policy)
    raise ValidationError("修订规则无效；对白定向规则不接受自定义来源")


def read_stage_input_amendment(stage, plan, declaration):
    record = ProductionStageInputAmendment.objects.filter(stage_run=stage).first()
    if record is None:
        return declaration, None
    evidence = verify_json_contract(record.evidence, schema_version=record.evidence_schema_version,
        size_bytes=record.evidence_size_bytes, sha256=record.evidence_sha256,
        max_bytes=min(65536, execution_configuration_max_bytes()))
    policy = evidence.value.get("policy")
    source_changes = None
    if policy in SOURCE_POLICIES:
        source_changes = [{"route_index": row["route_index"], "input_key": row["input_key"], "source": row["after"]}
            for row in evidence.value["changes"]]
    effective, proposed = _policy_proposal(plan, declaration, policy, source_changes)
    if proposed != evidence:
        raise ValidationError("阶段输入修订与原冻结计划或明确转换规则不一致")
    return effective, record


def stage_input_amendment_identity(stage):
    row = ProductionStageInputAmendment.objects.filter(stage_run=stage).first()
    return {"id": str(row.pk), "sha256": row.evidence_sha256} if row else None


def _result(record, evidence):
    return {"status": "applied" if record else "proposed", "sha256": evidence.sha256, "evidence": evidence.value,
        "id": str(record.pk) if record else None, "created_by": str(record.created_by_id) if record else None,
        "created_at": record.created_at if record else None,
        "notice": ("仅采用已确认的文本/JSON来源映射；不更换模型、参数、参考素材、剧本或审核。"
            if evidence.value["policy"] in SOURCE_POLICIES else "仅为未标注画外音的对白明确补 false；保留显式标记、原剧本和审核。")
            + "未启动生成，也未解除原阻断状态。"}


@registry_reference_writer
def amend_stage_inputs(*, stage_run_id, expected_run_plan_sha256, action, expected_amendment_sha256, actor,
                       policy, source_changes=None):
    from studio.services.production_recipe_preparations import recipe_stage_context, _project_inputs
    from studio.services.production_recipe_contracts import validate_recipe_execution_routing, recipe_stage_kind, recipe_execution_contract
    from studio.services.execution_text_output_catalog import plan_text_output_options
    from studio.services.execution_definitions import verify_stored_execution_definition
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        plan, original = recipe_stage_context(stage, expected_run_plan_sha256, apply_input_amendment=False)
        effective, evidence = _policy_proposal(plan, original, policy, source_changes)
        if action not in {"preview", "apply"}:
            raise ValidationError("需要明确预览或确认修订")
        if action == "apply" and expected_amendment_sha256 != evidence.sha256:
            raise ValidationError("修订内容已变化，请重新核对并确认", code="execution_preparation_conflict")
        _, existing = read_stage_input_amendment(stage, plan, original)
        if existing:
            if existing.evidence_sha256 != evidence.sha256:
                raise ValidationError("此阶段已有不同的不可变输入修订，不允许覆盖", code="execution_preparation_conflict")
            return _result(existing, evidence)
        run = stage.production_run
        if (run.current_stage_key != stage.stage_key or run.status not in {"blocked", "running"}
                or stage.status not in {"blocked", "pending", "running"}
                or run.project.owner_id != run.owner_id):
            raise ValidationError("只能修订当前未执行且归属一致的阶段")
        if (ExecutionPreparation.objects.filter(stage_run=stage).exists()
                or GenerationAttempt.objects.filter(stage_run=stage).exists()):
            raise ValidationError("此阶段已有执行准备或调用，不允许改写其输入；请保留原执行事实")
        stages = plan.value["recipe"]["definition"]["stages"]
        text_outputs = plan_text_output_options(plan)
        validate_recipe_execution_routing(effective["routing"], stage_key=recipe_stage_kind(effective),
            prior_stages=[row["key"] for row in stages[:stage.ordinal - 1]], text_outputs=text_outputs)
        if policy == TEXT_SOURCE_POLICY:
            for route in effective["routing"]["routes"]:
                _project_inputs(route["input_sources"], plan, stage,
                    definition=verify_stored_execution_definition(recipe_execution_contract(route)), text_outputs=text_outputs)
        if action == "preview":
            return _result(None, evidence)
        record = ProductionStageInputAmendment(stage_run=stage, created_by=actor)
        assign_json_contract(record, "evidence", evidence.value, max_bytes=min(65536, execution_configuration_max_bytes()))
        record.save(force_insert=True)
        return _result(record, evidence)
