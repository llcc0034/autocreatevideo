"""Read the exact, already-authorized media uses and frozen Workflow ports.

Prompt reference projection and graph staging share this check. Neither may
guess physical slots or silently remap a persisted use after preparation.
"""
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.execution_inputs import allocate_workflow_input_slots
from studio.services.execution_input_catalog import ExecutionSubjectCategory
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.reference_bindings import media_reference_label
from studio.services.execution_shot_reference_facts import validate_frozen_shot_reference
from studio.services.workflow_classified_references import MEDIA_GROUPS, CLASSIFIED_GROUP_INDEX_SOURCES


@dataclass(frozen=True)
class ExecutionWorkflowUses:
    items: dict
    primary_targets: dict
    targets: dict
    slots: dict


def _fail(message):
    raise ValidationError({"workflow_inputs": message})


def _verified(value, *, max_bytes):
    if not isinstance(value, JsonContract) or value.schema_version != 1 or not isinstance(value.value, dict):
        _fail("必须提供正式解析后带摘要的事实对象")
    return verify_json_contract(value.value, schema_version=value.schema_version, size_bytes=value.size_bytes,
        sha256=value.sha256, max_bytes=max_bytes).value


def _validate_subject_snapshot(subject, spec):
    """Validate frozen associations only; never reload or upgrade old bodies."""
    if subject is None:
        return
    if not isinstance(subject, dict):
        _fail("冻结主体必须是明确关联对象或null")
    kind = subject.get("kind")
    contract = spec.get("semantic_contract")
    if (not isinstance(kind, str) or not isinstance(contract, dict)
            or not isinstance(contract.get("allowed_subject_kinds"), list)
            or kind not in contract["allowed_subject_kinds"]):
        _fail("冻结主体类型不在输入合同中")
    if kind not in {"external", "character", "scene", "prop"}:
        _fail("冻结主体类型未登记")
    if kind == "external" and subject.get("category") not in ExecutionSubjectCategory.values:
        _fail("本次主体缺少明确语义类别；不自动推测")
    required = {"kind", "id", "name", "description", "category" if kind == "external" else "project_id"}
    if set(subject) != required:
        _fail("冻结主体字段不符合当前合同；请重新准备，不读取或转换旧身份正文")
    for field in ("id",) if kind == "external" else ("id", "project_id"):
        try:
            valid = isinstance(subject[field], str) and str(UUID(subject[field])) == subject[field]
        except ValueError:
            valid = False
        if not valid:
            _fail("冻结主体及项目必须使用正式UUID")
    if any(not isinstance(subject[field], str) for field in ("name", "description")) or not subject["name"].strip():
        _fail("冻结主体名称或描述无效")


@security_boundary_resolution_cache()
def read_execution_workflow_uses(definition, resolved_inputs):
    payload = _verified(definition, max_bytes=execution_configuration_max_bytes())
    resolved = _verified(resolved_inputs, max_bytes=execution_resolved_inputs_max_bytes())
    specs, targets = payload["configuration"]["inputs"], payload["interface"]["input_targets"]
    values, slots = resolved.get("logical_values"), resolved.get("workflow_slots")
    if not isinstance(values, dict) or not isinstance(slots, dict):
        _fail("缺少已解析的逻辑素材与实际端口分配")
    items, target_items, primary = {}, {}, {}
    for spec in specs:
        if spec["kind"] not in {"image", "video", "audio"}:
            continue
        rows = values.get(spec["key"], [])
        if not isinstance(rows, list):
            _fail("逻辑素材必须是已解析的有序数组")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("item_key"), str) or not row["item_key"] or row["item_key"] in items:
                _fail("每次素材使用必须有唯一标识，不能按Asset ID去重")
            if row.get("input_key") != spec["key"] or row.get("kind") != spec["kind"]:
                _fail("素材使用与逻辑输入身份不一致")
            digest = json_contract({key: value for key, value in row.items() if key != "source_sha256"},
                max_bytes=execution_configuration_max_bytes()).sha256
            if row.get("source_sha256") != digest:
                _fail("素材来源事实摘要不一致")
            if "subject" not in row:
                _fail("冻结素材缺少主体关联字段；无主体须明确为null")
            _validate_subject_snapshot(row["subject"], spec)
            validate_frozen_shot_reference(row)
            items[row["item_key"]] = row
        for mapping in spec["targets"]:
            if mapping["target_kind"] != "workflow_slot":
                continue
            key = mapping["target_key"]
            if key not in targets or key in target_items:
                _fail("素材目标缺失或由多个输入争写")
            indexed = targets[key]["wiring"].get("index_source_target_key") is not None
            target_items[key] = [{"item_key": row["item_key"], "kind": row["kind"],
                **({"paired_to": row["paired_to"]} if indexed else {})} for row in rows]
            if mapping["is_primary"]:
                for row in rows:
                    if row["item_key"] in primary:
                        _fail("一次素材使用不能占用多个主槽位")
                    primary[row["item_key"]] = key
    workflow_targets = {key: row for key, row in targets.items() if row["target_kind"] == "workflow_slot"}
    if slots != allocate_workflow_input_slots(workflow_targets, target_items):
        _fail("冻结素材端口与实际输入使用不一致")
    for item in items.values():
        if item["origin"].get("kind") != "video_audio":
            continue
        parent = items.get(item.get("paired_to"))
        if (parent is None or parent["kind"] != "video" or item["kind"] != "audio"
                or parent["item_key"] not in primary or item["item_key"] not in primary
                or item["origin"] != {"kind": "video_audio", "parent_item_key": parent["item_key"], "parent_source_sha256": parent["source_sha256"]}
                or item["asset_id"] is not None or item["artifact_id"] is not None
                or item["scope_ref"] != parent["scope_ref"] or item["media_facts"] != parent["media_facts"]
                or parent["media_facts"].get("observed_media", {}).get("has_audio") is not True):
            _fail("视频原音来源与本次父视频、范围或实际音轨不一致")
    return ExecutionWorkflowUses(items, primary, workflow_targets, slots)


def native_video_audio_uses(uses):
    """Only the registered classified graph can consume the video's audio output.

    Other declared audio ports keep their own staged derivative. No model name,
    inferred node class or user-provided passthrough flag chooses this path.
    """
    result = set()
    for key, item in uses.items.items():
        if item["origin"].get("kind") != "video_audio":
            continue
        parent = item["paired_to"]
        ports = [target for target, rows in uses.slots.items() if any(row["item_key"] == key for row in rows)]
        if ports and all(uses.targets[target]["wiring"]["kind"] == "classified_group"
                and CLASSIFIED_GROUP_INDEX_SOURCES.get(target) == uses.primary_targets[parent] for target in ports):
            result.add(key)
    return result


def execution_workflow_reference_slots(uses, *, require_roles):
    """One reference-label inventory for manual text and Profile projection.

    Fixed inputs without a reference role may still carry raw media, but do
    not acquire a guessed prompt label. Rendering structured references needs
    that role and therefore explicitly rejects such an incomplete contract.
    """
    result, occupied = {}, set()
    for item_key, target_key in uses.primary_targets.items():
        item, target = uses.items[item_key], uses.targets[target_key]
        assigned = next((row for row in uses.slots[target_key] if row["item_key"] == item_key), None)
        if assigned is None:
            _fail("主素材缺少已分配端口")
        kind, wiring, role = item["kind"], target["wiring"], target.get("reference_binding")
        if wiring["kind"] == "semantic_group":
            # Resolve the allocated physical semantic, not the item index.
            # No current Workflow lookup may enrich an old frozen definition.
            bindings = target.get("reference_bindings", {})
            if not isinstance(bindings, dict) or set(bindings) - set(wiring["semantic_keys"]):
                _fail("动态素材参考角色与冻结端口目录不一致")
            role = bindings.get(assigned["target_port_ref"]["semantic_key"])
        paired = False
        if wiring["kind"] == "classified_group":
            if MEDIA_GROUPS.get(target_key) != kind:
                _fail("分类素材组与已注册媒体协议不一致")
            category, ordinal = kind, assigned["target_position"] + 1
            paired = target_key in CLASSIFIED_GROUP_INDEX_SOURCES
        elif isinstance(role, dict):
            category, ordinal = role.get("category"), role.get("input_slot")
            if (set(role) != {"category", "input_slot"} or not isinstance(category, str) or not category
                    or category in {"first_frame", "last_frame"} and kind != "image"):
                _fail("冻结素材角色与引用序号必须明确声明且符合媒体类型")
            paired = kind == "audio" and item["paired_to"] is not None
        elif require_roles:
            raise ValidationError("固定/模板素材槽尚未声明明确参考角色与序号，不能按名称或数组位置猜测",
                code="execution_interface_unavailable")
        else:
            continue
        if type(ordinal) is not int or ordinal < 1 or (kind, paired, ordinal) in occupied:
            _fail("参考槽位序号无效或重复")
        occupied.add((kind, paired, ordinal))
        result[item_key] = {"category": category, "input_slot": ordinal, "is_paired": paired,
            "input_label": media_reference_label(kind, ordinal)}
    paired_uses = {}
    for key, slot in result.items():
        if slot["is_paired"]:
            parent = uses.items[key]["paired_to"]
            if parent not in result or uses.items[parent]["kind"] != "video" or parent in paired_uses:
                _fail("配套音轨必须指向本次唯一视频使用")
            paired_uses[parent] = key
    audio_ordinal = 1
    for kind in ("video", "audio"):
        ordered = sorted((key for key, slot in result.items() if uses.items[key]["kind"] == kind and not slot["is_paired"]),
            key=lambda key: result[key]["input_slot"])
        for key in ordered:
            audio = paired_uses.get(key) if kind == "video" else key
            if audio is not None:
                result[audio]["input_label"] = media_reference_label("audio", audio_ordinal)
                audio_ordinal += 1
    return result
