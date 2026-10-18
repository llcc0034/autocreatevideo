"""Explain original per-use media wiring; never compile, upload or read live assets."""
from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_media_processing import execution_media_processing_uses
from studio.services.execution_workflow_staging import read_execution_workflow_receipts
from studio.services.execution_workflow_uses import read_execution_workflow_uses, native_video_audio_uses
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.json_contracts import json_contract
from studio.services.workflow_media_tokens import collect_workflow_media_use_tokens
from studio.task_center_workflow_wiring import scalar_wiring, read_pinned_workflow, graph_node_rows
from studio.task_center_media_routes import media_port_routes
from studio.task_center_media_processing import processing_summary


def _locations(value, path):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _locations(child, [*path, key])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _locations(child, [*path, index])
    else:
        yield value, path


def media_trace(definition, transport, resolved, *, owner_id, root_id, receipts):
    """Caller supplies the authorized selected root, not a latest/other attempt."""
    if transport.value.get("driver_type") != "comfyui":
        return None
    inputs = json_contract(resolved["inputs"], max_bytes=execution_configuration_max_bytes())
    uses = read_execution_workflow_uses(definition, inputs)
    correspondence = frozen_reference_display(resolved, uses)
    checked = read_execution_workflow_receipts(definition=definition, resolved_inputs=inputs,
        transport=transport, owner_id=owner_id, root_id=root_id, receipts=receipts)
    media = transport.value["payload"]["media_preparation"]
    bindings = {row["item_key"]: row for row in media["bindings"]}
    tokens = {row["token"] for row in bindings.values()}
    if collect_workflow_media_use_tokens(checked.graph) != tokens:
        raise ValidationError("原图媒体占位符与本次素材用途不一致，不能解释为完整接线")
    locations = {token: [] for token in tokens}
    edges = []
    for node_id, node in checked.graph.items():
        for value, path in _locations(node["inputs"], []):
            if isinstance(value, str) and value in locations:
                locations[value].append({"node_id": node_id, "class_type": node["class_type"], "input_path": path})
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int and value[0] in checked.graph:
                edges.append({"from_node": value[0], "output_index": value[1], "to_node": node_id, "input_name": name})
    # Explain the whole frozen graph once, including text-only requests and
    # upstream/isolated nodes. Presence is not evidence that a node executed.
    processing = execution_media_processing_uses(definition, inputs)
    native = native_video_audio_uses(uses)
    evidence = {row["item_key"]: row for row in checked.sources}
    originals = {row["item_key"]: row for row in receipts}
    snapshot = read_pinned_workflow(definition, transport)
    rows = []
    for key, item in uses.items.items():
        staged_key = item["paired_to"] if key in native else key
        binding = bindings.get(staged_key)
        receipt = evidence.get(staged_key)
        spec = processing.get(staged_key)
        adapter = (spec["contract"]["adapter_key"] if spec else "source_bytes/v1") if binding else None
        metadata = originals[staged_key]["metadata"] if receipt else None
        ports = [{"target_key": target, "port": slot["target_port_ref"]}
            for target, slots in uses.slots.items() for slot in slots if slot["item_key"] == key]
        rows.append({"item_key": key, "input_key": item["input_key"], "kind": item["kind"],
            "reference_binding": correspondence[key],
            "display_name": (item["asset_facts"] or {}).get("name") or (item["subject"] or {}).get("name") or key,
            "asset_id": item["asset_id"], "artifact_id": item["artifact_id"], "origin": item["origin"],
            "purpose": item["purpose"], "paired_to": item["paired_to"], "source_facts_sha256": item["source_sha256"],
            "source_bytes_sha256": item["media_facts"]["sha256"],
            "input_label": (media["reference_slots"].get(key) or {}).get("input_label"),
            "mode": "embedded_audio" if key in native else "workflow_input" if binding else "context_only",
            "ports": ports, "graph_inputs": locations[binding["token"]] if binding else [],
            "port_routes": media_port_routes(ports, locations[binding["token"]] if binding else [], edges, snapshot),
            "processing": spec, "adapter": adapter,
            "processing_summary": processing_summary(adapter=adapter, spec=spec, profile=media["reference_media_profile"],
                receipt=receipt, metadata=metadata, shared_with=staged_key if key in native else None),
            "receipt_state": "recorded" if receipt else "missing" if binding else "not_applicable",
            "receipt": receipt, "processing_metadata": metadata})
    return {"state": "recorded", "workflow_version_id": definition.value["target"]["workflow"]["id"],
        "request_sha256": transport.sha256, "root_id": str(root_id), "items": rows,
        "media_profile": media["reference_media_profile"],
        "scalar_wiring": scalar_wiring(definition, transport, snapshot=snapshot),
        "nodes": graph_node_rows(checked.graph, snapshot, checked.graph.keys()),
        "edges": edges,
        "message": "仅解释完整冻结图、原媒体用途和本次根任务已校验的暂存回执；包含无参考素材请求及未连通节点。节点存在不代表已执行，不查询当前素材、不执行转换、不证明模型已收到或生成成功。"}
