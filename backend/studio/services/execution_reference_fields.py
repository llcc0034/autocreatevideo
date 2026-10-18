"""Per-unit reference contributions through the existing field-task contract.

Reference facts and scoped contexts must come from authorized source adapters.
This module freezes tasks; it does not call a leaf instance, grant approval,
select output or replace the common PromptProfile primitive renderer.
"""
from copy import deepcopy
from dataclasses import asdict

from django.core.exceptions import ValidationError

from studio.services.execution_field_contracts import ExecutionFieldInput, FIELD_OPERATION_MODES, freeze_execution_field_request
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.prompt_normalization_contract import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
)
from studio.services.prompt_profiles import verify_prompt_profile_contract
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context
from studio.services.reference_subject_labels import reference_subject_labels
from studio.services.reference_context_facts import freeze_reference_context_facts
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.source_projection_primitives import REFERENCE_CONTEXT_ENGINE_KEY
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR, resolve_profile_structure, read_profile_path
from studio.services.prompt_generation_contexts import structured_context_path, read_structured_context


@security_boundary_resolution_cache()
def prepare_reference_profile_fields(*, profile, reference_context, generation_contexts, source_language, target_language,
                                     structured_inputs=None, final_inputs=()):
    """Build one immutable item per declared field and actual ordered unit.

    generation_contexts is {unit_key: {declared_selector: resolved_value}}.
    A root adapter may project actual project/shot context or explicit manual
    input into that shape. This adapter never invents a Project or treats a
    caller unit_key as the identity of a real Shot.
    """
    if profile.status != "published":
        raise ValidationError("字段准备必须引用已发布的精确策略")
    contract = verify_prompt_profile_contract(profile)
    if contract["schema_version"] != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION or contract["source_projection_key"] != "declarative_v1":
        raise ValidationError("参考字段准备需要当前完整声明式策略，不转换旧策略")
    frozen_context = freeze_reference_context_facts(reference_context, max_bytes=execution_request_max_bytes())
    if profile.mode and profile.mode != reference_context.mode:
        raise ValidationError("参考模式必须与精确策略一致")
    if reference_context.language != target_language:
        raise ValidationError("参考事实与本次字段的目标语言必须一致")
    if any(not isinstance(value, str) or len(value) > 40 for value in (source_language, target_language)):
        raise ValidationError("来源与目标语言必须明确且有界")
    # Validate the original facts with the same configured renderer before any
    # auxiliary batch can be allocated. This is not a final prompt or approval:
    # generated contributions are still absent and must be reviewed separately.
    rendering = contract["source_projection_config"].get("primitive_options", {}).get(REFERENCE_CONTEXT_ENGINE_KEY, {}).get("rendering")
    if rendering is None:
        raise ValidationError({"reference_context": "策略缺少明确的参考渲染合同"})
    try:
        from studio.services.source_projection_primitives import reference_result_fields
        compile_reference_context(reference_context, rendering, result_fields=reference_result_fields(contract["rules"]))
    except PromptBuildError as exc:
        raise ValidationError({"reference_context": str(exc)}) from exc
    operations = {mode: operation for operation, mode in FIELD_OPERATION_MODES.items()}
    rules = [row for row in contract["rules"] if row["generation_mode"] in operations and row["generation_output_semantics"] == "contribution"]
    selectors = {key for rule in rules for key in rule["generation_context_selectors"] if structured_context_path(key) is None}
    units = {unit.unit_key: unit for unit in reference_context.ordered_units}
    if (not isinstance(generation_contexts, dict) or set(generation_contexts) != (set(units) if selectors else set())
            or any(not isinstance(value, dict) or set(value) != selectors for value in generation_contexts.values())):
        raise ValidationError("辅助上下文必须按实际单元精确提供策略选定来源，不能缺失或附带其他来源")
    json_contract(generation_contexts, max_bytes=execution_request_max_bytes())
    subjects = {(row.category, row.identity): row for row in reference_context.subject_facts}
    structured_rules = [row for row in rules if row["generation_source_selector"] == STRUCTURED_INPUT_SELECTOR]
    unit_keys = {unit.unit_key for unit in reference_context.ordered_units}
    if structured_rules:
        if not isinstance(structured_inputs, dict) or set(structured_inputs) != unit_keys:
            raise ValidationError("结构化来源必须与本次有序单元一一对应")
        json_contract(structured_inputs, max_bytes=execution_resolved_inputs_max_bytes())
    elif structured_inputs is not None:
        raise ValidationError("策略未声明逐单元结构化来源，不能注入额外原值")
    batches = {}
    rule_modes = {row["field_key"]: row["generation_mode"] for row in contract["rules"]}
    for item in final_inputs:
        batches.setdefault(operations[rule_modes[item.field_key]], []).append(item)
    logical_subjects = reference_subject_labels(reference_context, rendering)
    for unit in reference_context.ordered_units:
        structured = None
        if structured_rules:
            values, _ = resolve_profile_structure(contract["input_structure"],
                {STRUCTURED_INPUT_SELECTOR: structured_inputs[unit.unit_key]}, max_bytes=execution_request_max_bytes())
            structured = values[STRUCTURED_INPUT_SELECTOR]
        references = []
        for row in reference_context.reference_bindings:
            if "unit_keys" in row and unit.unit_key not in row["unit_keys"]:
                continue
            category = row.get("entity_category", row.get("category"))
            if row.get("entity_id") and (category, str(row["entity_id"])) not in unit.subject_keys:
                continue
            references.append(deepcopy(row))
        # Names are creative facts, even when their spelling resembles a
        # protocol marker. Only allocated media slots declare legal markers.
        labels = {row[key] for row in references for key in ("input_label", "audio_input_label")
                  if isinstance(row.get(key), str) and row[key].startswith("<") and row[key].endswith(">")}
        subject_rows = [{**asdict(subjects[key]), "description_parts": list(subjects[key].description_parts)} for key in unit.subject_keys]
        for row in subject_rows:
            logical = logical_subjects.get((row["category"], row["identity"]))
            if logical is not None and unit.unit_key in logical["unit_keys"]:
                row.update(reference_label=logical["label"], source_labels=[label for label in logical["source_labels"] if label in labels])
                labels.add(logical["label"])
        for rule in rules:
            if rule["generation_output_semantics"] != "contribution" or rule["generation_source_selector"] != STRUCTURED_INPUT_SELECTOR:
                raise ValidationError("参考策略辅助字段必须明确贡献到受控单元来源；不能合并成一个最终字段",
                    code="execution_interface_unavailable")
            field_key = rule["field_key"]
            text, _, present = read_profile_path(structured, rule["source_path"], field_key)
            if not present or not isinstance(text, str):
                raise ValidationError({f"fields.{unit.unit_key}.{field_key}": "结构化来源路径必须提供文本原值"})
            request_id = json_contract({"profile_id": str(profile.pk), "unit_key": unit.unit_key, "field_key": field_key},
                max_bytes=execution_configuration_max_bytes()).sha256
            batches.setdefault(operations[rule["generation_mode"]], []).append(ExecutionFieldInput(
                request_id=request_id, unit_key=unit.unit_key, field_key=field_key, source_text=text,
                source_language=source_language, target_language=target_language,
                context_facts={key: (read_structured_context(structured, key, field_key) if structured_context_path(key) is not None
                    else deepcopy(generation_contexts[unit.unit_key][key])) for key in rule["generation_context_selectors"]},
                reference_facts={"protected_tokens": sorted(label for label in labels if label in text),
                    "logical_inputs": references, "subject_facts": subject_rows},
            ))
    return json_contract({"schema_version": 1, "reference_context": asdict(frozen_context),
        "generation_contexts": deepcopy(generation_contexts),
        "field_requests": [asdict(freeze_execution_field_request(operation=operation, profile=profile, inputs=items))
            for operation, items in batches.items()]}, max_bytes=execution_resolved_inputs_max_bytes())
