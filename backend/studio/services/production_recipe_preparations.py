"""Bind frozen Recipe project-stage sources to the shared preparation store.

This is an internal producer, not another queue or a publication shortcut.
The project supplies request language; the original stage worker owns dispatch.
Upstream selection and per-shot producers are not inferred by this adapter.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import ArtifactSelectionDecision, GeneratedArtifact, StageRun
from studio.services.execution_field_review import production_field_review_contract
from studio.services.execution_text_output_catalog import plan_text_output_options
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_owners import locked_preparation_owners
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import recipe_stage_kind, validate_recipe_execution_route, verify_recipe_routing_output_identity
from studio.services.production_recipe_routing import (
    candidate_request_key, recipe_routing, resolve_recipe_candidate_route, record_recipe_route_preparation,
)
from studio.services.production_run_plans import confirmed_script_reference, get_run_execution_plan
from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_parameter_sources import bind_recipe_parameter_sources, resolved_recipe_parameter_source
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _unavailable(message):
    raise ValidationError(message, code="execution_interface_unavailable")


def _project_inputs(sources, plan, stage, *, definition=None, text_outputs=None):
    """Turn rules into explicit source identities; never select the latest row."""
    result, eligible = {}, set()
    text_outputs = plan_text_output_options(plan) if text_outputs is None else text_outputs
    allowed_stage_ids = execution_source_stage_ids(stage)
    for key, source in sources.items():
        if source["kind"] == "structured":
            from studio.services.execution_structure_mapping import bind_structure
            def bind_leaf(row):
                nested, stages = _project_inputs({"leaf": row}, plan, stage, text_outputs=text_outputs)
                eligible.update(stages)
                return nested["leaf"]
            result[key] = {"kind": "structured", "template": bind_structure(source["template"], bind_leaf,
                source_schemas=plan.value["structure_source_schemas"], text_outputs=text_outputs)}
        elif source["kind"] == "media_selection":
            from studio.services.production_recipe_media_selection import bind_recipe_media_selection
            specs = definition.value["configuration"]["inputs"] if definition is not None else []
            spec = next((row for row in specs if row["key"] == key), None)
            if spec is None:
                _unavailable("固定素材选择需要精确实例输入合同")
            result[key] = bind_recipe_media_selection(source, spec)
        elif source["kind"] == "literal":
            result[key] = deepcopy(source)
        elif source["kind"] == "project_snapshot":
            if definition is None:
                _unavailable("项目快照需要精确执行输入合同，不允许嵌入任意结构路径")
            from studio.services.production_project_snapshots import recipe_project_snapshot_source
            result[key] = recipe_project_snapshot_source(run=stage.production_run, plan=plan,
                definition=definition, input_key=key)
        elif source["kind"] == "context_field":
            if source["scope"] != {"kind": "project"}:
                _unavailable("此准备入口只处理项目级上下文，不推断剧集或镜头范围")
            from studio.services.execution_input_catalog import execution_context_field_options
            options = {row["selector_key"]: row for row in execution_context_field_options()}
            if options.get(source["selector_key"], {}).get("revision_kind") == "visual_rules_approval":
                from studio.services.production_reviewed_visual_rules import read_reviewed_visual_rules
                reviewed = read_reviewed_visual_rules(run=stage.production_run, eligible_stage_ids=allowed_stage_ids)
                result[key] = {**deepcopy(source), "scope": {"kind": "project", "id": plan.value["project_id"]},
                    "content_revision_id": reviewed["review"]["decision_id"]}
                eligible.add(reviewed["review"]["stage_run_id"])
                continue
            if options.get(source["selector_key"], {}).get("revision_kind") == "production_request":
                result[key] = {**deepcopy(source), "scope": {"kind": "project", "id": plan.value["project_id"]},
                    "content_revision_id": str(stage.production_run_id)}
                continue
            reference = plan.value["initial_content"]
            if reference is None:
                raise ValidationError({f"input_sources.{key}": "本轮未选择已确认剧本；不能读取最新剧本或空文本代替"})
            actual = confirmed_script_reference(plan.value["project_id"], {
                "kind": "script_revision", "id": reference["id"], "expected_sha256": reference["sha256"]})
            if actual != reference:
                raise ValidationError("本轮冻结的剧本确认事实已不一致，不能替换初始内容")
            result[key] = {**deepcopy(source), "scope": {"kind": "project", "id": plan.value["project_id"]},
                "content_revision_id": reference["id"]}
        elif source["kind"] == "selected_output":
            choices = [row for row in text_outputs if row["output_key"] == source["output_key"]
                and row["stage_key"] == source["stage_key"]]
            if len(choices) != 1 or source["scope"] != {"kind": "project"}:
                _unavailable("项目文本准备需要已登记的项目级上游输出，不推断素材用途或镜头范围")
            upstream = StageRun.objects.filter(production_run_id=stage.production_run_id,
                pk__in=allowed_stage_ids, stage_key=source["stage_key"]).first()
            if upstream is None:
                raise ValidationError({f"input_sources.{key}": "指定上游尚未完成，不能读取初始内容或最新候选代替"})
            artifacts = list(GeneratedArtifact.objects.filter(production_run_id=stage.production_run_id,
                stage_run=upstream, role=choices[0]["artifact_role"], status="selected").order_by("id")[:2])
            if len(artifacts) != 1:
                raise ValidationError({f"input_sources.{key}": "单值输入必须对应唯一已采用输出；没有采用或多选均不能自动决定"})
            artifact = artifacts[0]
            decision = ArtifactSelectionDecision.objects.filter(stage_run=upstream,
                variant_group=artifact.variant_group).order_by("-revision").first()
            if decision is None or decision.artifact_id != artifact.pk:
                raise ValidationError({f"input_sources.{key}": "已采用产物缺少一致的正式选择记录"})
            result[key] = {"kind": "selected_output", "decision_id": str(decision.pk),
                "artifact_id": str(artifact.pk), "output_key": source["output_key"],
                **({"stage_key": source["stage_key"]} if choices[0].get("reader") == "structured_text" else {}),
                "scope": {"kind": "project", "id": plan.value["project_id"]}}
            eligible.add(str(upstream.pk))
        else:
            _unavailable("来源类型尚未登记，不会以初始剧本、最新候选或默认素材代替")
    return result, eligible


def recipe_stage_context(stage, expected_run_plan_sha256=None, *, apply_input_amendment=True):
    """Read only this stage's frozen declaration; never current Recipe/routing."""
    plan = get_run_execution_plan(stage.production_run)
    if expected_run_plan_sha256 is not None and expected_run_plan_sha256 != plan.sha256:
        raise ValidationError("运行计划已不一致，请重新读取", code="execution_preparation_conflict")
    if plan.value["source"] != "recipe":
        raise ValidationError("人工制作不能借用配方准备入口")
    stages = plan.value["recipe"]["definition"]["stages"]
    rows = [row for row in stages if row["key"] == stage.stage_key]
    if len(rows) != 1 or rows[0]["mode"] != stage.mode or stages.index(rows[0]) + 1 != stage.ordinal:
        raise ValidationError("阶段职责与本轮冻结计划不一致")
    declaration = rows[0]
    if apply_input_amendment:
        from studio.services.production_stage_input_amendments import read_stage_input_amendment
        declaration, _ = read_stage_input_amendment(stage, plan, declaration)
    return plan, declaration


def recipe_project_stage_context(stage, expected_run_plan_sha256=None):
    plan, declaration = recipe_stage_context(stage, expected_run_plan_sha256)
    if STAGE_EXECUTION_OPERATIONS.get(recipe_stage_kind(declaration)) not in PRODUCTION_TEXT_OPERATIONS or stage.mode != "ai":
        _unavailable("此入口仅处理已有项目级文本生产职责；其他单元须由对应阶段准备")
    verify_recipe_routing_output_identity(recipe_routing(declaration))
    return plan, declaration


@registry_reference_writer
@security_boundary_resolution_cache()
def prepare_recipe_project_stage(*, stage_run_id, expected_run_plan_sha256, candidate_number,
        actor):
    """Resolve once per project/candidate from the real immutable RunPlan.

    No caller-supplied instance, route, prompt text, sources, language, approval or seed.
    Replays go through the original creator before current-source validation,
    preserving prior fields and the one generated seed even after cancellation.
    """
    if type(candidate_number) is not int or candidate_number < 1:
        raise ValidationError("需要从1开始的明确候选编号")
    owner_ref = ("stage_run_id", stage_run_id)
    with locked_preparation_owners([owner_ref], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        run = stage.production_run
        plan, declaration = recipe_project_stage_context(stage, expected_run_plan_sha256)
        stages = plan.value["recipe"]["definition"]["stages"]
        routing = declaration["routing"]
        if candidate_number > routing["variant_count"]:
            raise ValidationError("候选编号与冻结计划不一致")
        unit_key = f"project:{run.project_id}"
        choice = resolve_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=unit_key, candidate_number=candidate_number)
        route = routing["routes"][choice.index]
        request_key = candidate_request_key(choice.index, candidate_number)
        from studio.services.production_stage_input_amendments import stage_input_amendment_identity
        amendment = stage_input_amendment_identity(stage)
        intent = json_contract({"schema_version": 1, "run_plan_sha256": plan.sha256,
            **({"input_amendment": amendment} if amendment else {}),
            "stage_key": stage.stage_key, "route_index": choice.index, "candidate_number": candidate_number,
            "language_source": {"kind": "project", "project_id": str(run.project_id)},
            "parameters": {}, "manual_channels": {}},
            max_bytes=execution_configuration_max_bytes())

        def resolve(definition, locked_stage):
            if (locked_stage.production_run.status != "running" or locked_stage.status != "running"
                    or locked_stage.production_run.project.owner_id != locked_stage.production_run.owner_id):
                raise ValidationError("只有正在执行且项目归属一致的阶段可以创建新准备")
            validate_recipe_execution_route(route,
                prior_stages=[row["key"] for row in stages[:stage.ordinal - 1]], stage_key=recipe_stage_kind(declaration),
                text_outputs=plan_text_output_options(plan))
            if definition.value["configuration"]["prompt_mode"] == "manual":
                raise ValidationError("自动制作需要策略组装或明确免提示词的实例；不生成第三套人工提示词模板")
            # Resolve only on first creation. Replays must not depend on a
            # later project edit, and fixed Profile language rules retain their
            # own precedence in the shared compiler.
            language = locked_stage.production_run.project.language
            if not isinstance(language, str) or not language.strip() or len(language) > 40:
                raise ValidationError({"project.language": "请先配置项目语言；自动制作不会默认中文或英文"})
            inputs, eligible = _project_inputs(route["input_sources"], plan, locked_stage, definition=definition)
            request = {"inputs": inputs, "parameters": {}, "manual_channels": {}, "subject_facts": [],
                "source_language": language, "target_language": language}
            return resolve_execution_request_facts(definition, request, owner=locked_stage.production_run.owner,
                unit_key=unit_key, max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                review_contract=production_field_review_contract(locked_stage),
                parameter_policies=bind_recipe_parameter_sources(route["parameter_policies"], run=locked_stage.production_run),
                resolve_parameter_source=resolved_recipe_parameter_source,
                project_id=run.project_id, production_run_id=str(run.pk), eligible_stage_ids=eligible,
                production_scope={"kind": "project", "id": str(run.project_id)})

        preparation = create_execution_instance_preparation(owner_ref=owner_ref,
            version_id=route["execution_instance_version_id"], expected_definition_sha256=route["expected_definition_sha256"],
            unit_key=unit_key, request_key=request_key, intent=intent, resolve_facts=resolve, actor=actor)
        record_recipe_route_preparation(stage=stage, preparation=preparation, choice=choice, candidate_number=candidate_number)
        return preparation
