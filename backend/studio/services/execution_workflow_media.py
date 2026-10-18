"""Project verified per-use media tokens onto frozen Workflow input wiring.

Tokens come from the trusted staging/placeholder adapter, never from public
request JSON. This module does not upload files or invent an Asset for an
upstream generated artifact. Context-only uses are not graph inputs.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_workflow_uses import read_execution_workflow_uses, execution_workflow_reference_slots, native_video_audio_uses
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_classified_references import MEDIA_GROUPS, CLASSIFIED_GROUP_INDEX_SOURCES
from studio.services.workflow_media_tokens import workflow_media_use_token


@security_boundary_resolution_cache()
def prepare_execution_workflow_media(definition, resolved_inputs):
    """Prepare repeatable per-use placeholders; no upload side effects."""
    uses = read_execution_workflow_uses(definition, resolved_inputs)
    required = {row["item_key"] for rows in uses.slots.values() for row in rows} - native_video_audio_uses(uses)
    tokens = {key: workflow_media_use_token(item_key=key, source_sha256=uses.items[key]["source_sha256"])
        for key in uses.items if key in required}
    return project_execution_workflow_media(definition, resolved_inputs, tokens_by_use=tokens)


@security_boundary_resolution_cache()
def project_execution_workflow_media(definition, resolved_inputs, *, tokens_by_use):
    from studio.services.execution_media_processing import read_execution_media_processing_profile
    uses = read_execution_workflow_uses(definition, resolved_inputs)
    native_audio = native_video_audio_uses(uses)
    required = {row["item_key"] for rows in uses.slots.values() for row in rows} - native_audio
    if (not isinstance(tokens_by_use, dict) or set(tokens_by_use) != required
            or any(not isinstance(token, str) or not token for token in tokens_by_use.values())
            or len(set(tokens_by_use.values())) != len(tokens_by_use)):
        raise ValidationError("图素材必须逐使用提供唯一token；不能遗漏端口、去重文件或混入上下文素材")
    has_classified = any(row["wiring"]["kind"] == "classified_group" for row in uses.targets.values())
    groups = {key: [] for key in ("images", "videos", "audios")} if has_classified else None
    semantics, bindings, videos, paired = {}, {}, {}, []
    for target_key, assigned in uses.slots.items():
        target = uses.targets[target_key]
        for slot in assigned:
            item_key = slot["item_key"]
            item = uses.items[item_key]
            if item_key in native_audio:
                paired.append((target_key, slot, item_key))
                continue
            token = tokens_by_use[item_key]
            port = slot["target_port_ref"]
            binding = bindings.setdefault(item_key, {"item_key": item_key, "token": token, "kind": item["kind"],
                "asset_id": item["asset_id"], "artifact_id": item["artifact_id"], "origin": deepcopy(item["origin"]),
                "source_sha256": item["source_sha256"], "media_facts": deepcopy(item["media_facts"]), "ports": []})
            binding["ports"].append({"target_key": target_key, "port": deepcopy(port)})
            if port["kind"] == "semantic":
                key = port["semantic_key"]
                if key in semantics:
                    raise ValidationError("多个素材使用不能争写同一语义槽")
                semantics[key] = {"item_key": item_key, "token": token}
                continue
            if target["wiring"]["kind"] != "classified_group" or MEDIA_GROUPS.get(target_key) != item["kind"]:
                raise ValidationError("素材节点端口没有对应的已注册分类接线")
            if target_key in CLASSIFIED_GROUP_INDEX_SOURCES:
                paired.append((target_key, slot, item_key))
                continue
            if target_key not in groups or slot["target_position"] != len(groups[target_key]):
                raise ValidationError("分类素材实际端口必须与有序使用一一对应")
            row = {"token": token}
            if target_key == "videos":
                row["paired_audio"] = None
                videos[item_key] = (target_key, slot["target_position"], row)
            groups[target_key].append(row)
    for target_key, slot, item_key in paired:
        parent = videos.get(uses.items[item_key]["paired_to"])
        if (parent is None or parent[0] != CLASSIFIED_GROUP_INDEX_SOURCES[target_key]
                or parent[1] != slot["target_position"] or parent[2]["paired_audio"] is not None):
            raise ValidationError("配套音轨必须对应本次明确的视频使用及实际端口序号")
        parent[2]["paired_audio"] = ({"kind": "embedded"} if item_key in native_audio
            else {"kind": "external", "token": tokens_by_use[item_key]})
    reference_slots = execution_workflow_reference_slots(uses, require_roles=False)
    labels = list(dict.fromkeys(value for key, slot in reference_slots.items()
        for value in (slot["input_label"], (uses.items[key]["subject"] or {}).get("name"))
        if isinstance(value, str) and value.startswith("<") and value.endswith(">")))
    return json_contract({"schema_version": 1, "definition_sha256": definition.sha256,
        "resolved_inputs_sha256": resolved_inputs.sha256, "semantic_inputs": semantics, "classified_groups": groups,
        "reference_slots": reference_slots, "declared_reference_labels": labels,
        "bindings": list(bindings.values()), "reference_media_profile": read_execution_media_processing_profile(definition, resolved_inputs),
        "context_only_item_keys": [key for key in uses.items if key not in required and key not in native_audio]},
        max_bytes=execution_configuration_max_bytes())
