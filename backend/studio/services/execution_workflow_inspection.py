"""Read-only projection of an actual production call, never a legacy trace.

No uploads, compilation, provider discovery or mutation. A UI canvas may be
derived separately using the exact target's node catalog, not another server.
"""
from django.core.exceptions import ValidationError

from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
from studio.services.execution_production_workflow_queue import read_prepared_generation_task
from studio.services.execution_workflow_staging import read_execution_workflow_receipts
from studio.services.execution_workflow_uses import read_execution_workflow_uses, native_video_audio_uses
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_media_tokens import collect_workflow_media_use_tokens, substitute_workflow_media_uses


@security_boundary_resolution_cache()
def inspect_prepared_task_workflow(task):
    context = read_prepared_generation_task(task)
    preparation = context.attempt.preparation
    detail, definition, request = read_frozen_execution_request(preparation)
    checked = read_execution_workflow_receipts(definition=definition,
        resolved_inputs=json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes()),
        transport=request, owner_id=task.owner_id, root_id=task.pk, receipts=task.staged_inputs)
    # Do not mix a partially uploaded graph with frozen input placeholders.
    # All existing receipts were verified even when the set is incomplete.
    graph = checked.graph if checked.missing_tokens else substitute_workflow_media_uses(checked.graph, checked.mapping)
    return {
        "task_id": str(task.pk), "attempt_id": str(context.attempt.pk), "preparation_id": str(preparation.pk),
        "execution_instance_version_id": str(context.attempt.execution_instance_version_id),
        "workflow_version_id": definition.value["target"]["workflow"]["id"],
        "request_sha256": preparation.request_sha256, "definition_sha256": definition.sha256,
        "graph_state": "prepared" if checked.missing_tokens else "staged",
        "staged_placeholder_count": len(collect_workflow_media_use_tokens(graph)),
        "missing_receipt_count": len(checked.missing_tokens), "workflow": graph,
    }


def frozen_workflow_prompt_rows(definition, request):
    """Verify recorded channels against compiled semantics for every workflow root."""
    prompt = definition["interface"]["prompt"]
    resolved = request["prompt_channels"]
    if (not isinstance(resolved, dict) or not isinstance(resolved.get("values"), dict)
            or not isinstance(resolved.get("sources"), dict)
            or set(resolved["values"]) != set(resolved["sources"])
            or set(resolved["values"]) - set(prompt["channels"])):
        raise ValidationError("冻结提示词通道与实际输入来源不一致")
    rows = []
    semantics = request["payload"]["compiled_workflow"]["resolved_semantics"]
    for channel, text in resolved["values"].items():
        target = prompt["channels"][channel]["semantic_key"]
        source = resolved["sources"][channel]
        if not isinstance(text, str) or not isinstance(source, str) or semantics.get(target) != text:
            raise ValidationError("冻结提示词与本次工作流实际语义输入不一致")
        rows.append({"key": channel, "semantic_key": target, "text": text,
            "character_count": len(text), "value_source": source})
    return rows


@security_boundary_resolution_cache()
def inspect_prepared_task_prompt(task):
    """Exact frozen channels, including explicit empty strings; never old text."""
    context = read_prepared_generation_task(task)
    preparation = context.attempt.preparation
    _, definition, request = read_frozen_execution_request(preparation)
    rows = frozen_workflow_prompt_rows(definition.value, request.value)
    prompt = definition.value["interface"]["prompt"]
    return {"task_id": str(task.pk), "attempt_id": str(context.attempt.pk),
        "preparation_id": str(preparation.pk), "execution_instance_version_id": str(context.attempt.execution_instance_version_id),
        "request_sha256": preparation.request_sha256, "prompt_mode": prompt["mode"],
        "prompt_profile_version_id": prompt.get("profile_id"), "channels": rows,
        "declared_reference_labels": context.media["payload"]["media_preparation"]["declared_reference_labels"]}


@security_boundary_resolution_cache()
def inspect_prepared_task_inputs(task):
    """Public input facts per use, not raw graph, prompts or worker receipts."""
    context = read_prepared_generation_task(task)
    preparation = context.attempt.preparation
    detail, definition, request = read_frozen_execution_request(preparation)
    resolved = json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes())
    uses = read_execution_workflow_uses(definition, resolved)
    correspondence = frozen_reference_display(detail.resolved_inputs, uses)
    checked = read_execution_workflow_receipts(definition=definition, resolved_inputs=resolved,
        transport=request, owner_id=task.owner_id, root_id=task.pk, receipts=task.staged_inputs)
    media = request.value["payload"]["media_preparation"]
    bindings = {item["item_key"]: item for item in media["bindings"]}
    staged = {item["item_key"] for item in checked.sources}
    native_audio = native_video_audio_uses(uses)
    rows = []
    for key, item in uses.items.items():
        binding = bindings.get(key)
        reference = media["reference_slots"].get(key)
        name = (item["asset_facts"] or {}).get("name")
        if name is None:
            name = "配对视频原音" if item["origin"]["kind"] == "video_audio" else (item["subject"] or {}).get("name", key)
        ports = binding["ports"] if binding else ([{"target_key": target, "port": slot["target_port_ref"]}
            for target, slots in uses.slots.items() for slot in slots if slot["item_key"] == key] if key in native_audio else [])
        staged_key = item["paired_to"] if key in native_audio else key
        rows.append({"item_key": key, "input_key": item["input_key"], "kind": item["kind"],
            "asset_id": item["asset_id"], "artifact_id": item["artifact_id"], "display_name": name,
            "reference_binding": correspondence[key],
            "purpose": item["purpose"], "paired_to": item["paired_to"],
            "source_sha256": item["source_sha256"], "checksum": item["media_facts"]["sha256"],
            "input_label": reference["input_label"] if reference else None,
            "ports": ports,
            "staging_status": ("staged" if staged_key in staged else "not_staged") if binding or key in native_audio else "not_applicable",
            "preview_url": ""})
    # Values include both adjustable parameters and frozen workflow semantics.
    # Reuse the original verifier for their identities and sources; static
    # facts must not be rejected as missing parameter contracts or made editable.
    parameters = verify_prepared_execution_parameters(definition, request.value["parameters"])
    statics = definition.value["interface"]["static_semantics"]
    prompt = inspect_prepared_task_prompt(task)
    return {"preparation_id": str(preparation.pk), "attempt_id": str(context.attempt.pk),
        "execution_instance_version_id": str(context.attempt.execution_instance_version_id),
        "workflow_version_id": definition.value["target"]["workflow"]["id"],
        "request_sha256": preparation.request_sha256, "operation": request.value["operation"],
        "output_kind": context.media["output_kind"],
        "parameters": [{"key": key, "value": value, "value_source": parameters["sources"][key],
            "rule": ({"const": statics[key]} if key in statics else parameters["contracts"][key]["rule"])}
            for key, value in parameters["values"].items()],
        "media_uses": rows, "prompt_mode": prompt["prompt_mode"],
        "prompt_profile_version_id": prompt["prompt_profile_version_id"],
        "prompt_channels": [{key: row[key] for key in ("key", "semantic_key", "character_count")}
            for row in prompt["channels"]]}
