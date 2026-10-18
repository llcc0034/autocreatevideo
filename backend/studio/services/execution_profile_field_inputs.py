"""Shared final-field input resolution for every strategy-based execution.

Only already authorized/frozen values enter here. The rule itself is the result
destination; no model, operation, field key or renderer-slot allow-list exists.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_field_contracts import ExecutionFieldInput, FIELD_OPERATION_MODES
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import json_contract


def final_profile_field_inputs(*, contract, profile_id, projection, unit_key,
                               source_language, target_language, projected_values=None, reference_facts=None):
    inputs = []
    references = reference_facts or {"logical_inputs": [], "subject_facts": []}
    labels = {row[key] for row in references["logical_inputs"] for key in ("input_label", "audio_input_label")
              if isinstance(row.get(key), str) and row[key]}
    labels.update(row["reference_label"] for row in references["subject_facts"] if row.get("reference_label"))
    for rule in contract["rules"]:
        if rule["generation_mode"] not in FIELD_OPERATION_MODES.values() or rule["generation_output_semantics"] != "final":
            continue
        key = rule["field_key"]
        selector = rule["generation_source_selector"]
        if selector == "projection.output":
            source = (projected_values or {}).get(key)
        else:
            source = projection["generation_sources"].get(key)
        if not isinstance(source, str):
            raise ValidationError({f"fields.{key}": "字段生成来源必须是本次明确文本，不能隐式转换或读取旧值"})
        provenance = [{**deepcopy(row), "uses": [deepcopy(use) for use in row["uses"] if use["field_key"] == key]}
            for row in projection["provenance"] if any(use["field_key"] == key for use in row["uses"])]
        inputs.append(ExecutionFieldInput(
            request_id=json_contract({"profile_id": str(profile_id), "unit_key": unit_key, "field_key": key},
                max_bytes=execution_configuration_max_bytes()).sha256,
            unit_key=unit_key, field_key=key, source_text=source,
            source_language=source_language, target_language=target_language,
            context_facts=deepcopy(projection["generation_contexts"].get(key, {})),
            reference_facts={"protected_tokens": sorted(label for label in labels if label in source),
                "logical_inputs": deepcopy(references["logical_inputs"]) if reference_facts is not None else provenance,
                "subject_facts": deepcopy(references["subject_facts"])},
        ))
    return inputs
