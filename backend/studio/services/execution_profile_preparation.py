"""Prepare exact Profile sources and field requests from resolved inputs.

This internal adapter does not resolve permissions, approve text or call the
auxiliary instance. It feeds the shared preparation store and Profile compiler.
Direct fields and per-unit reference contributions share the same store.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError

from studio.services.execution_field_contracts import FIELD_OPERATION_MODES, freeze_execution_field_request
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_profile_inputs import profile_input_targets, project_execution_profile_sources
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.prompt_profile_compiler import MediaPromptProfileCompilation
from studio.services.execution_reference_fields import prepare_reference_profile_fields
from studio.services.execution_reference_inputs import project_reference_unit_input
from studio.services.execution_reference_materials import project_execution_reference_materials
from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
from studio.services.reference_context_facts import ReferenceContextFacts, ReferenceSubjectFacts, read_reference_context_facts
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.workflow import aligned_frames, WorkflowBuildError
from studio.services.prompt_profiles import verify_prompt_profile_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.execution_profile_field_inputs import final_profile_field_inputs


@dataclass(frozen=True)
class ExecutionProfilePreview:
    preparation_id: str
    preparation_revision: int
    field_selection_sha256: str
    compilation: MediaPromptProfileCompilation
    resolved_channels: dict


def _verified(contract, label):
    if not isinstance(contract, JsonContract) or contract.schema_version != 1 or not isinstance(contract.value, dict):
        raise ValidationError({label: "需要有摘要的明确事实对象"})
    return verify_json_contract(contract.value, schema_version=contract.schema_version, size_bytes=contract.size_bytes,
        sha256=contract.sha256, max_bytes=(execution_resolved_inputs_max_bytes()
            if label == "resolved_inputs" else execution_configuration_max_bytes())).value


@security_boundary_resolution_cache()
def prepare_execution_profile(definition, resolved_inputs, *, profile, unit_key, source_language, target_language,
                              prepared_parameters=None, scope_units=None):
    """Freeze declared direct or structured reference facts, never approval."""
    payload = _verified(definition, "definition")
    resolved = _verified(resolved_inputs, "resolved_inputs")
    for name, value, maximum in (("unit_key", unit_key, 191), ("source_language", source_language, 40), ("target_language", target_language, 40)):
        if not isinstance(value, str) or len(value) > maximum or (name == "unit_key" and not value.strip()):
            raise ValidationError({name: "需要明确且有界的文本"})
    prompt = payload.get("interface", {}).get("prompt", {})
    if (prompt.get("mode") != "profile" or prompt.get("profile_id") != str(profile.pk)
            or prompt.get("profile_sha256") != profile.contract_sha256):
        raise ValidationError("准备策略必须与执行实例冻结的精确版本一致")
    contract = verify_prompt_profile_contract(profile)
    if profile_input_targets(profile) != payload["interface"].get("profile_inputs"):
        raise ValidationError("策略来源目录与冻结定义不一致")
    values = resolved.get("logical_values")
    if type(resolved.get("schema_version")) is not int or resolved["schema_version"] != 1 or not isinstance(values, dict):
        raise ValidationError("准备需要正式来源解析的逻辑值")
    projection = project_execution_profile_sources(definition, values)
    if contract["source_projection_key"] == "declarative_v1":
        return _prepare_reference_profile(definition, resolved_inputs, profile=profile, unit_key=unit_key,
            source_language=source_language, target_language=target_language, projection=projection,
            prepared_parameters=prepared_parameters, scope_units=scope_units)
    reference_facts = None
    if any(spec["kind"] in {"image", "video", "audio"} and values.get(spec["key"])
           for spec in payload["configuration"]["inputs"]):
        output = payload["interface"]["output"]
        kind = output["contract"]["primary"]["kind"] if isinstance(output["adapter"], dict) else output["kind"]
        materials = project_execution_reference_materials(definition, resolved_inputs, prompt_type=kind,
            unit_keys=[unit_key], scope_units=scope_units if scope_units is not None else {}).value
        reference_facts = {"logical_inputs": materials["references"], "subject_facts": materials["subjects"]}
    operations = {mode: operation for operation, mode in FIELD_OPERATION_MODES.items()}
    batches = {}
    modes = {row["field_key"]: row["generation_mode"] for row in contract["rules"]}
    for item in final_profile_field_inputs(contract=contract, profile_id=profile.pk, projection=projection,
            unit_key=unit_key, source_language=source_language, target_language=target_language,
            reference_facts=reference_facts):
        batches.setdefault(operations[modes[item.field_key]], []).append(item)
    requests = [asdict(freeze_execution_field_request(operation=operation, profile=profile, inputs=entries))
                for operation, entries in batches.items()]
    return json_contract({"schema_version": 1,
        "profile_sources": {"profile": {"version_id": str(profile.pk), "contract_sha256": profile.contract_sha256},
            "unit_key": unit_key, "source_language": source_language, "target_language": target_language,
            "projection": projection}, "field_requests": requests}, max_bytes=execution_resolved_inputs_max_bytes())


def _prepare_reference_profile(definition, resolved_inputs, *, profile, unit_key, source_language, target_language,
                               projection, prepared_parameters, scope_units):
    interface = definition.value["interface"]
    parameters = verify_prepared_execution_parameters(definition, prepared_parameters)
    contract = verify_prompt_profile_contract(profile)
    from studio.services.source_projection_primitives import reference_profile_requires_units
    needs_units = reference_profile_requires_units(contract)
    unit_input = projection.get("reference", {}).get("unit_input")
    if needs_units and (not isinstance(unit_input, dict) or not isinstance(unit_input.get("ordered_units"), list)):
        raise ValidationError("参考策略缺少明确的有序单元来源")
    if not needs_units and unit_input is not None:
        raise ValidationError("当前参考策略不使用单元输入")
    unit_keys = [row.get("unit_key") if isinstance(row, dict) else None for row in unit_input["ordered_units"]] if needs_units else []
    from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
    from studio.services.reference_rendering_inputs import resolve_unit_render_values
    bindings = contract["source_projection_config"]["primitive_options"]["reference_context_ir_v1"]["rendering"]["unit_text_bindings"]
    structured_inputs = None
    renderer_input = deepcopy(unit_input)
    generated_structured = any(row["generation_output_semantics"] == "contribution" and row["generation_source_selector"] == STRUCTURED_INPUT_SELECTOR
                              for row in contract["rules"])
    if bindings or generated_structured:
        structured_inputs = {}
        for row in renderer_input["ordered_units"]:
            if not isinstance(row, dict) or not isinstance(row.get("unit_key"), str) or not row["unit_key"] or "structured_input" not in row:
                raise ValidationError("每个有序单元必须提供明确标识与结构化原值")
            if row["unit_key"] in structured_inputs:
                raise ValidationError("结构化来源单元标识不能重复")
            structured_inputs[row["unit_key"]] = row.pop("structured_input")
    # An authorized production scope owns this preparation, whose reference
    # payload can contain several declared prompt units. Expand that root's
    # membership to those units; never infer entity/shot scope from their names.
    # Unknown memberships still fail in the original material projector.
    scope_units = {scope: list(unit_keys) if isinstance(members, (list, tuple)) and list(members) == [unit_key] else members
        for scope, members in scope_units.items()} if isinstance(scope_units, dict) else scope_units
    output = interface["output"]
    kind = output["contract"]["primary"]["kind"] if isinstance(output["adapter"], dict) else output["kind"]
    if kind not in {"image", "video"}:
        raise ValidationError("当前参考原语只支持明确的图像或视频输出", code="execution_interface_unavailable")
    materials = project_execution_reference_materials(definition, resolved_inputs,
        prompt_type=kind, unit_keys=unit_keys, scope_units=scope_units).value
    values = parameters["values"]
    # Image output has no audio track; video requires the frozen target fact.
    if kind == "image":
        include_audio = False
        if "audio.enabled" in values and values["audio.enabled"] is not False:
            raise ValidationError("图像输出不能同时要求音轨")
    else:
        include_audio = values.get("audio.enabled")
        if type(include_audio) is not bool:
            raise ValidationError("参考视频缺少目标合同中明确的音轨开关", code="execution_interface_unavailable")
    mode = definition.value["resources"]["conditioning_mode"]
    mode_key = definition.value["configuration"]["mode"]
    if kind == "image":
        # Image counts/roles/ports were verified by the original input resolver
        # and material projector. Images have no video conditioning mode.
        if mode is not None or mode_key != "":
            raise ValidationError("图像参考不能声明视频模式数量合同")
    elif not isinstance(mode, dict) or mode.get("key") != mode_key:
        raise ValidationError("参考准备缺少精确模式数量合同")
    frame_policy = deepcopy(interface.get("frame_policy"))
    if kind == "video":
        if not isinstance(frame_policy, dict) or "video.fps" not in values:
            raise ValidationError("参考视频缺少明确时钟合同", code="execution_interface_unavailable")
        frame_policy["fps"] = values["video.fps"]
    try:
        unit_render_values = resolve_unit_render_values(bindings=bindings, structure=contract.get("input_structure"),
            structured_inputs=structured_inputs, unit_keys=unit_keys, max_bytes=execution_request_max_bytes())
        shared_facts = dict(prompt_type=kind, mode=mode_key, include_audio=include_audio,
            language=target_language, subject_facts=tuple(ReferenceSubjectFacts(**row) for row in materials["subjects"]),
            reference_bindings=tuple(materials["references"]), conditioning_mode_contract=mode, frame_policy=frame_policy)
        if needs_units:
            context = project_reference_unit_input(renderer_input, **shared_facts,
                max_bytes=execution_request_max_bytes(), unit_render_values=unit_render_values)
        else:
            # These slots are unused, not fabricated video/business facts.
            context = ReferenceContextFacts(**shared_facts, ordered_units=(), color_script="", extra_direction="",
                overall_soundscape="", non_diegetic_music="", generation_contributions={})
        if kind == "video":
            duration = sum(unit.duration_ms for unit in context.ordered_units)
            if "video.duration_ms" in values and values["video.duration_ms"] != duration:
                raise ValidationError("参考单元总时长与本次参数不一致")
            if values.get("video.frames") != aligned_frames(duration, frame_policy):
                raise ValidationError("参考单元总时长与本次视频帧数不一致")
    except (PromptBuildError, WorkflowBuildError) as exc:
        raise ValidationError({"reference_context": str(exc)}) from exc
    per_selector = projection.get("reference", {}).get("generation_contexts", {})
    if any(not isinstance(rows, dict) or set(rows) != set(unit_keys) for rows in per_selector.values()):
        raise ValidationError("参考辅助上下文必须与本次有序单元一一对应")
    contexts = {key: {selector: rows[key] for selector, rows in per_selector.items()} for key in unit_keys} if per_selector else {}
    from studio.services.prompt_profile_compiler import project_profile_reference_fields
    from studio.services.reference_subject_labels import reference_subject_labels
    logical_subjects = reference_subject_labels(context, contract["source_projection_config"]["primitive_options"]["reference_context_ir_v1"]["rendering"])
    subject_facts = []
    for subject in context.subject_facts:
        row = {**asdict(subject), "description_parts": list(subject.description_parts)}
        logical = logical_subjects.get((subject.category, subject.identity))
        if logical:
            row.update(reference_label=logical["label"], source_labels=list(logical["source_labels"]))
        subject_facts.append(row)
    projected_values = project_profile_reference_fields(profile, context) if any(
        row["generation_mode"] in FIELD_OPERATION_MODES.values() and row["generation_source_selector"] == "projection.output"
        for row in contract["rules"]) else None
    final_inputs = final_profile_field_inputs(contract=contract, profile_id=profile.pk, projection=projection,
        unit_key=unit_key, source_language=source_language, target_language=target_language,
        projected_values=projected_values,
        reference_facts={"logical_inputs": list(context.reference_bindings), "subject_facts": subject_facts})
    prepared = prepare_reference_profile_fields(profile=profile, reference_context=context, generation_contexts=contexts,
        source_language=source_language, target_language=target_language,
        structured_inputs=structured_inputs if generated_structured else None, final_inputs=final_inputs).value
    return json_contract({"schema_version": 1, "field_requests": prepared["field_requests"],
        "profile_sources": {"profile": {"version_id": str(profile.pk), "contract_sha256": profile.contract_sha256},
            "unit_key": unit_key, "source_language": source_language, "target_language": target_language,
            "projection": projection, "reference_context": prepared["reference_context"],
            "scope_units": [{"kind": key[0], "id": key[1], "unit_keys": list(members)} for key, members in sorted(scope_units.items())]}},
        max_bytes=execution_resolved_inputs_max_bytes())


@registry_reference_writer
@security_boundary_resolution_cache()
def compile_preparation_profile(*, preparation_id, expected_preparation_revision, actor):
    """Read frozen sources plus explicit choices through the common compiler.

    This is a read/preview result, not a finalized request or permission to
    dispatch. The caller must recheck the preparation revision when freezing.
    """
    from studio.models import ExecutionPreparation
    from studio.services.execution_preparation_fields import read_execution_field_selection_snapshot
    selected = read_execution_field_selection_snapshot(preparation_id=preparation_id,
        expected_preparation_revision=expected_preparation_revision, actor=actor)
    preparation = ExecutionPreparation.objects.select_related("execution_instance_version__prompt_profile_version",
        "deployment_smoke__prompt_profile_version").get(pk=preparation_id)
    return _compile_preparation_profile(preparation, selected)


def _compile_preparation_profile(preparation, selected, *, evidence_key="field_selections"):
    """Single compiler for verified selection or explicitly labelled display evidence.

    Display callers cannot freeze or dispatch its result. Production still calls
    compile_preparation_profile, which only reads actual confirmed selections.
    """
    from studio.services.execution_preparations import read_execution_preparation_detail, preparation_execution_definition
    from studio.services.execution_prompts import normalize_compiled_profile_channels
    from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts

    detail = read_execution_preparation_detail(preparation)
    definition = preparation_execution_definition(preparation, detail=detail)
    if detail.intent["request"].get("definition_sha256") != definition.sha256:
        raise ValidationError("准备引用的冻结定义与精确实例不一致")
    facts = detail.resolved_inputs.get("profile_sources")
    expected_keys = {"profile", "unit_key", "source_language", "target_language", "projection"}
    profile = (preparation.execution_instance_version.prompt_profile_version if preparation.execution_instance_version_id
        else preparation.deployment_smoke.prompt_profile_version if preparation.deployment_smoke_id else None)
    reference = profile is not None and profile.source_projection_key == "declarative_v1"
    if reference:
        expected_keys |= {"reference_context", "scope_units"}
    if (not isinstance(facts, dict) or set(facts) != expected_keys or profile is None
            or facts["profile"] != {"version_id": str(profile.pk), "contract_sha256": profile.contract_sha256}
            or facts["unit_key"] != preparation.unit_key):
        raise ValidationError("准备缺少与本单元一致的冻结策略来源")
    if profile_input_targets(profile) != definition.value["interface"].get("profile_inputs"):
        raise ValidationError("策略输入目录与冻结定义不一致")
    inputs = detail.resolved_inputs.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("logical_values"), dict):
        raise ValidationError("策略准备缺少已解析的逻辑输入事实")
    expected_projection = project_execution_profile_sources(definition, inputs["logical_values"])
    if facts["projection"] != expected_projection:
        raise ValidationError("冻结策略投影与本次已解析来源不一致，不能重新选取当前内容补齐")
    if any(row["required"] for row in selected.value["unselected_fields"]):
        raise ValidationError("必需字段尚未明确采用，不能组装最终提示词", code="execution_fields_incomplete")
    fields = selected.value["fields"]
    reference_context, reviewed, contributions, labels = None, {}, {}, []
    if reference:
        scope_rows = facts["scope_units"]
        if (not isinstance(scope_rows, list) or any(not isinstance(row, dict) or set(row) != {"kind", "id", "unit_keys"} for row in scope_rows)
                or len({(row["kind"], row["id"]) for row in scope_rows}) != len(scope_rows)):
            raise ValidationError("冻结参考范围事实不完整或重复")
        scopes = {(row["kind"], row["id"]): row["unit_keys"] for row in scope_rows}
        expected = prepare_execution_profile(definition, json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes()),
            profile=profile, unit_key=facts["unit_key"], source_language=facts["source_language"], target_language=facts["target_language"],
            prepared_parameters=detail.resolved_inputs.get("parameters"), scope_units=scopes)
        if expected.value["profile_sources"] != facts or expected.value["field_requests"] != detail.resolved_inputs.get("field_requests"):
            raise ValidationError("参考准备与原素材/单元/字段来源不一致，不能读取当前来源补齐")
        reference_context = read_reference_context_facts(JsonContract(**facts["reference_context"]), max_bytes=execution_request_max_bytes())
        units = {unit.unit_key for unit in reference_context.ordered_units}
        from studio.services.source_projection_primitives import contribution_target
        profile_rules = {rule["field_key"]: rule for rule in verify_prompt_profile_contract(profile)["rules"]}
        for row in fields:
            if row["generation_output_semantics"] == "final":
                if row["unit_key"] != preparation.unit_key or row["field_key"] in reviewed:
                    raise ValidationError("最终字段须唯一属于本次请求，不能隐含合并多个单元结果")
                reviewed[row["field_key"]] = row["text"]
                continue
            if row["unit_key"] not in units or row["generation_output_semantics"] != "contribution":
                raise ValidationError("逐单元贡献须属于本次实际单元")
            identity = (row["unit_key"], contribution_target(profile_rules[row["field_key"]]["projection_primitive"]))
            if identity in contributions:
                raise ValidationError("多个字段不能争写同一单元贡献")
            contributions[identity] = row["text"]
        from studio.services.reference_subject_labels import declared_reference_labels
        labels = declared_reference_labels(reference_context, {})
    else:
        if any(row["unit_key"] != preparation.unit_key or row["generation_output_semantics"] != "final" for row in fields):
            raise ValidationError("直接策略字段必须属于本单元；参考贡献不能当成最终文本")
        reviewed = {row["field_key"]: row["text"] for row in fields}
        if any(spec["kind"] in {"image", "video", "audio"} and inputs["logical_values"].get(spec["key"])
               for spec in definition.value["configuration"]["inputs"]):
            from studio.services.execution_workflow_media import prepare_execution_workflow_media
            labels = prepare_execution_workflow_media(definition, json_contract(inputs,
                max_bytes=execution_resolved_inputs_max_bytes())).value["declared_reference_labels"]
    compilation = compile_prompt_profile_facts(profile, facts=PromptProfileCompilationFacts(
        sources=deepcopy(facts["projection"]["assembly_sources"]),
        source_language=facts["source_language"], target_language=facts["target_language"], reference_context=reference_context,
        reviewed_values=reviewed, generation_contributions=contributions,
        reviewed_evidence={evidence_key: deepcopy(fields)},
        reviewed_fingerprint={"field_selection_sha256": selected.sha256},
        declared_reference_tokens=tuple(labels),
    ))
    channels = normalize_compiled_profile_channels(definition.value["interface"]["prompt"], compilation,
        declared_reference_labels=labels)
    return ExecutionProfilePreview(str(preparation.pk), selected.value["preparation_revision"], selected.sha256, compilation, channels)
