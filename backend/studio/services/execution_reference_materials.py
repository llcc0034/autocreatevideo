"""Resolved media uses -> reference facts for the shared primitive renderer.

The caller has already authorized and probed sources. This adapter uses frozen
ports, not Asset names, model names or graph examples. It never creates Assets
for generated artifacts, reads files, approves fields or dispatches work.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict

from django.core.exceptions import ValidationError

from studio.services.bible_reference_contract import bible_reference_name_key
from studio.services.execution_workflow_uses import read_execution_workflow_uses, execution_workflow_reference_slots
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.services.security_boundaries import security_boundary_resolution_cache


def _fail(message):
    raise ValidationError({"reference_materials": message})


def _name_reference_uses(records):
    """Disambiguate this call's names, never identities or authored prose.

    A declared subject has one display name across its media uses. Unassociated
    media remain separate uses; equal names never establish identity. The Bible
    conflict key detects collisions between these groups without normalizing
    displayed text. Reserve authored names, including alias-like names. Choose
    each group's first stable use key so port reordering cannot rename it.
    """
    groups = {}
    for row in sorted(records, key=lambda row: row["item_key"]):
        identity = (("subject", row["entity_category"], row["entity_id"]) if row["entity_id"]
                    else ("use", row["item_key"]))
        groups.setdefault(identity, []).append(row)
    counts = Counter(bible_reference_name_key(rows[0]["original_name"]) for rows in groups.values() if rows[0]["original_name"])
    reserved = set(counts)
    for rows in groups.values():
        row = rows[0]
        name = row["original_name"]
        duplicate = bool(name) and counts[bible_reference_name_key(name)] > 1
        if duplicate:
            suffix = f" [{row['item_key']}]"
            name += suffix
            while bible_reference_name_key(name) in reserved:
                name += suffix
            reserved.add(bible_reference_name_key(name))
        for member in rows:
            member.update(entity_label=name, name_disambiguated=duplicate)


@security_boundary_resolution_cache()
def project_execution_reference_materials(definition, resolved_inputs, *, prompt_type, unit_keys, scope_units):
    """Project primary Workflow media uses; context-only media get no fake slot.

    scope_units is supplied by the authorized root adapter as
    {(scope_kind, scope_id): [unit_key, ...]}; request scope means the explicitly
    provided units. No Shot IDs are inferred from caller-authored unit names.
    prompt_type is the root adapter's frozen output kind, not a media purpose.
    Video references carry identity/ports, never implicit appearance prose or
    Asset prompt hints. Image preparation retains those appearance sources.
    """
    if not isinstance(prompt_type, str) or prompt_type not in {"image", "video"}:
        _fail("参考投影必须明确使用本次冻结的图片或视频输出类型")
    uses = read_execution_workflow_uses(definition, resolved_inputs)
    if (not isinstance(unit_keys, (list, tuple)) or (not unit_keys and prompt_type != "image")
            or any(not isinstance(key, str) or not key for key in unit_keys) or len(set(unit_keys)) != len(unit_keys)):
        _fail("必须提供本次唯一且有序的单元目录")
    if not isinstance(scope_units, dict):
        _fail("来源范围必须由根任务适配器明确解析")
    for key, members in scope_units.items():
        if (not isinstance(key, tuple) or len(key) != 2 or any(not isinstance(part, str) or not part for part in key)
                or not isinstance(members, (list, tuple)) or (not members and unit_keys)
                or any(member not in unit_keys for member in members) or len(set(members)) != len(members)):
            _fail("来源范围引用了本次未声明或重复的单元")
    items, primary, targets, slots = uses.items, uses.primary_targets, uses.targets, uses.slots
    records, subjects, paired = [], {}, {}
    reference_slots = execution_workflow_reference_slots(uses, require_roles=True)
    for item_key, target_key in primary.items():
        item, target = items[item_key], targets[target_key]
        assigned = next((row for row in slots[target_key] if row["item_key"] == item_key), None)
        if assigned is None:
            _fail("主素材缺少已分配端口")
        kind = item["kind"]
        slot = reference_slots[item_key]
        category, ordinal, is_paired = slot["category"], slot["input_slot"], slot["is_paired"]
        subject = item["subject"]
        entity_id, entity_label = None, ""
        if subject is not None:
            if kind != "image":
                _fail("视频/音频主体关系尚需明确投影，不能当作图片主体")
            semantic_category = subject["kind"]
            if subject["kind"] == "external":
                semantic_category = subject["category"]
            category = semantic_category if category == "image" else category
            entity_id, entity_label = subject["id"], subject["name"]
            fact = ReferenceSubjectFacts(subject["kind"], entity_id,
                (subject["name"], subject["description"]) if prompt_type == "image" else (subject["name"],))
            identity = (subject["kind"], entity_id)
            if identity in subjects and subjects[identity] != fact:
                _fail("同一主体的冻结描述不一致")
            subjects[identity] = fact
        scope = item["scope_ref"]
        if scope == {"kind": "request"}:
            members = list(unit_keys)
        elif isinstance(scope, dict) and set(scope) == {"kind", "id"} and (scope["kind"], scope["id"]) in scope_units:
            members = [key for key in unit_keys if key in scope_units[(scope["kind"], scope["id"])]]
        else:
            _fail("素材范围尚未映射到本次实际单元，不能默认为全局")
        asset_facts = item["asset_facts"]
        if asset_facts is None and not item["artifact_id"] and item["origin"]["kind"] != "video_audio":
            _fail("素材缺少正式Asset或已选择产物身份")
        if subject is not None:
            original_name = entity_label
        elif asset_facts is None:
            original_name = ""
        elif isinstance(asset_facts, dict) and isinstance(asset_facts.get("name"), str):
            original_name = asset_facts["name"]
        else:
            _fail("冻结素材原名缺失或类型无效，不能猜测名称")
        record = {"item_key": item_key, "asset_id": item["asset_id"], "artifact_id": item["artifact_id"],
            "category": category, "entity_id": entity_id, "entity_label": entity_label,
            "original_name": original_name,
            "entity_category": subject["kind"] if subject is not None else None,
            "input_slot": ordinal, "input_label": slot["input_label"],
            "purpose": item["purpose"] if item["purpose"] is not None else "", "unit_keys": members,
            "source_sha256": item["source_sha256"], "target_key": target_key,
            "target_port_ref": deepcopy(assigned["target_port_ref"])}
        if not unit_keys:
            del record["unit_keys"]
            # Preserve the authorized source scope even without a timeline.
            # An empty membership never turns a production source into global.
            record["scope_ref"] = deepcopy(scope)
        if prompt_type == "image":
            record.update(description=asset_facts["description"] if asset_facts is not None else "",
                prompt_hint=asset_facts["prompt_hint"] if asset_facts is not None else "")
        # Keep the resolver's choice evidence with this exact use. Profile
        # rendering never selects sources or reads the current instance policy.
        if "source_selection" in item:
            record["source_selection"] = deepcopy(item["source_selection"])
        if "shot_reference" in item:
            record["shot_reference"] = deepcopy(item["shot_reference"])
        if is_paired:
            parent = item["paired_to"]
            if not isinstance(parent, str) or parent in paired or parent not in primary or items[parent]["kind"] != "video":
                _fail("配套音轨必须指向本次唯一视频使用")
            paired[parent] = record
        else:
            if item["paired_to"] is not None:
                _fail("当前素材槽没有声明配对音轨接线")
            records.append(record)
    _name_reference_uses([*records, *paired.values()])
    # Match the shared classified protocol: images, videos with paired audio,
    # standalone audio. Audio labels form one namespace across both sources.
    ordered = []
    for kind in ("image", "video", "audio"):
        group = sorted((row for row in records if items[row["item_key"]]["kind"] == kind), key=lambda row: row["input_slot"])
        for record in group:
            if kind == "video" and record["item_key"] in paired:
                audio = paired[record["item_key"]]
                if audio.get("unit_keys") != record.get("unit_keys"):
                    _fail("配套音轨与视频必须使用相同的单元范围")
                if not unit_keys and audio["scope_ref"] != record["scope_ref"]:
                    _fail("配套音轨与视频必须使用相同的来源范围")
                record.update(audio_asset_id=audio["asset_id"], audio_artifact_id=audio["artifact_id"],
                    audio_input_label=audio["input_label"], audio_purpose=audio["purpose"],
                    audio_use=audio, use_embedded_audio=items[audio["item_key"]]["origin"]["kind"] == "video_audio")
                if prompt_type == "image":
                    record.update(audio_description=audio["description"], audio_prompt_hint=audio["prompt_hint"])
            ordered.append(record)
    return json_contract({"schema_version": 1, "references": ordered,
        "subjects": [{**asdict(row), "description_parts": list(row.description_parts)} for row in subjects.values()],
        "context_only_item_keys": [key for key in items if key not in primary]}, max_bytes=execution_resolved_inputs_max_bytes())
