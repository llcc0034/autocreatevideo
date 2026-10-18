"""The shared first-resolution pipeline called inside an original root transaction.

This is not a public snapshot API or a worker. The caller checks idempotency and
locks its real root before entering; a replay reads the saved preparation.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import PromptProfileVersion
from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_preparations import ResolvedExecutionPreparation
from studio.services.execution_profile_preparation import prepare_execution_profile
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_prompts import normalize_manual_execution_channels
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


@security_boundary_resolution_cache()
def resolve_execution_request_facts(definition, request, *, owner, unit_key, max_media_bytes,
        review_contract, parameter_policies, resolve_parameter_source, project_id=None,
        production_run_id=None, eligible_stage_ids=(), scope_units=None, production_scope=None, reviewed_smoke_context=None,
        new_candidate_seed_exclusions=None, production_appearance_role="primary"):
    """Resolve declared sources, parameters and field tasks without approving them.

    Context, step policies, review authority and upstream eligibility are supplied
    by the original root adapter, never deserialized from the request body.
    """
    definition = verify_execution_definition_payload(definition)
    if production_appearance_role not in ("primary", "variant"):
        raise ValidationError("生产外观角色必须明确为主图或变体")
    if production_appearance_role == "variant" and (production_run_id is None
            or not isinstance(production_scope, dict) or production_scope.get("kind") not in {"character", "scene", "prop"}):
        raise ValidationError("配方变体须有明确的原运行及实体范围")
    required = {"inputs", "parameters", "manual_channels", "subject_facts", "source_language", "target_language"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValidationError("执行请求必须完整声明输入、参数、提示词、主体及语言，不能提供批准或执行快照")
    if any(not isinstance(request[key], dict) for key in ("inputs", "parameters", "manual_channels")):
        raise ValidationError("输入、参数和提示词通道必须是明确对象")
    if (not isinstance(unit_key, str) or not unit_key.strip() or len(unit_key) > 191
            or any(not isinstance(request[key], str) or len(request[key]) > 40 for key in ("source_language", "target_language"))):
        raise ValidationError("需要有效的执行单元和语言字段")
    json_contract(request, max_bytes=execution_request_max_bytes())
    if not isinstance(review_contract, JsonContract):
        raise ValidationError("审核合同必须来自根任务的明确策略")
    review = verify_json_contract(review_contract.value, schema_version=review_contract.schema_version,
        size_bytes=review_contract.size_bytes, sha256=review_contract.sha256, max_bytes=execution_configuration_max_bytes())
    mode = definition.value["configuration"]["prompt_mode"]
    if mode == "profile" and request["manual_channels"]:
        raise ValidationError("策略组装不能同时覆盖人工提示词通道")
    inputs = resolve_execution_input_sources(definition, request["inputs"], owner=owner,
        subject_facts=request["subject_facts"], max_media_bytes=max_media_bytes, project_id=project_id,
        production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids, production_scope=production_scope,
        reviewed_smoke_context=reviewed_smoke_context, unit_key=unit_key)
    if mode != "profile":
        labels = []
        if definition.value["target"]["workflow"] is not None:
            from studio.services.execution_workflow_media import prepare_execution_workflow_media
            labels = prepare_execution_workflow_media(definition, inputs).value["declared_reference_labels"]
        normalize_manual_execution_channels(definition.value["interface"]["prompt"], request["manual_channels"],
            declared_reference_labels=labels)
    parameters = initialize_execution_definition_parameters(definition, request["parameters"],
        policies=parameter_policies, resolve_source=resolve_parameter_source,
        new_candidate_seed_exclusions=new_candidate_seed_exclusions)
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS, build_native_text_plan, native_text_source_reference
    if definition.value["configuration"]["operation"] == "visual_rules.generate" and definition.value["target"]["workflow"] is None:
        from studio.services.visual_rules_inputs import capture_visual_rules_baseline
        frozen_inputs = deepcopy(inputs.value)
        frozen_inputs["visual_rules_baseline"] = capture_visual_rules_baseline(project_id=project_id,
            owner=owner, inputs=inputs.value)
        inputs = json_contract(frozen_inputs, max_bytes=execution_resolved_inputs_max_bytes())
    if (definition.value["configuration"]["operation"] in NATIVE_TEXT_OPERATIONS
            and definition.value["target"]["workflow"] is None):
        build_native_text_plan(definition.value["interface"], inputs=inputs.value["operation_inputs"], parameters=parameters["values"],
            reference_max_bytes=execution_request_max_bytes(), source_reference=native_text_source_reference(definition.value, inputs.value))
    facts = {"schema_version": 1, "inputs": inputs.value, "parameters": parameters,
        "profile_sources": {}, "field_requests": []}
    if production_run_id is not None and definition.value["configuration"]["operation"] in {"script.generate", "script.structure"}:
        from studio.services.production_script_baselines import capture_script_adoption_baseline
        facts["script_adoption_baseline"] = capture_script_adoption_baseline(run_id=production_run_id,
            project_id=project_id, owner=owner)
    if production_run_id is not None and definition.value["target"]["driver_type"] in {"async_rest", "comfyui"}:
        from studio.models import ProductionRun
        from studio.services.execution_production_media import freeze_production_media_runtime
        from studio.services.production_execution_units import resolve_production_unit, production_unit_scope_members
        run = ProductionRun.objects.get(pk=production_run_id, project_id=project_id, owner=owner)
        facts["execution_deadlines"] = freeze_production_media_runtime(run=run)
        facts["production_unit"] = resolve_production_unit(run, production_scope)
        from studio.services.production_execution_units import ENTITY_SCOPE_MODELS
        if facts["production_unit"]["scope"]["kind"] in ENTITY_SCOPE_MODELS and production_appearance_role == "primary":
            from studio.services.production_bible_targets import capture_bible_target
            facts["bible_target"] = capture_bible_target(run, facts["production_unit"])
        members = production_unit_scope_members(facts["production_unit"], unit_key)
        if scope_units is not None and scope_units != members:
            raise ValidationError("素材范围映射必须来自本次明确生产单元，不能引用其他镜头")
        scope_units = members
    if mode == "profile":
        profile = PromptProfileVersion.objects.get(pk=definition.value["configuration"]["prompt_profile_version_id"])
        projected = prepare_execution_profile(definition, inputs, profile=profile, unit_key=unit_key,
            source_language=request["source_language"], target_language=request["target_language"],
            prepared_parameters=parameters, scope_units=scope_units)
        facts.update(projected.value)
    return ResolvedExecutionPreparation(json_contract(deepcopy(facts), max_bytes=execution_resolved_inputs_max_bytes()), review)
