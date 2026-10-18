"""Project and check execution input wiring from verified Workflow contracts.

Only frozen scalar media slots and the two registered dynamic wiring contracts
are projected. No graph-name matching, first-slot fallback, material selection
or Smoke fixture defaults are used here. Prompt-context and direct-operation
inputs are validated by their own interface contracts in the definition builder.
"""
from django.core.exceptions import ValidationError
from copy import deepcopy

from studio.services.execution_instances import (
    execution_configuration_max_bytes, validate_execution_input_mapping,
)
from studio.services.json_contracts import json_contract
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_asset_groups import group_form_schema
from studio.services.workflow_classified_references import CLASSIFIED_GROUP_INDEX_SOURCES


_MEDIA_TYPES = {"asset_image": "image", "asset_video": "video", "asset_audio": "audio"}


def _error(key, message):
    raise ValidationError({f"inputs.{key}": message})


def _target(key, kind, minimum, maximum, source, wiring):
    if not isinstance(key, str) or not key or kind not in {"image", "video", "audio"}:
        _error(str(key), f"{source} 未声明有效的素材目标和类型")
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum:
        _error(key, f"{source} 未声明完整的数量边界")
    return {
        "target_kind": "workflow_slot", "target_key": key, "kind": kind,
        "min_items": minimum, "max_items": maximum, "required": minimum > 0,
        "source": source, "wiring": wiring,
    }


def _reference_binding(key, definition, kind):
    if "asset_category" not in definition and "input_slot" not in definition:
        return None
    category, slot = definition.get("asset_category"), definition.get("input_slot")
    if not isinstance(category, str) or not category or type(slot) is not int or slot < 1:
        _error(key, "冻结素材角色与引用序号必须同时明确声明")
    if category in {"first_frame", "last_frame"} and kind != "image":
        _error(key, "首尾帧角色只适用于图片槽位")
    return {"category": category, "input_slot": slot}


@security_boundary_resolution_cache()
def workflow_input_targets(workflow):
    """Project an already integrity-verified Workflow; not a second verifier.

    The definition/draft service first calls resolve_execution_resources. This
    pure projection must never be used as evidence of publication or readiness.
    """
    contract = workflow.input_contract
    parameters = contract.get("parameters")
    if not isinstance(parameters, dict):
        _error("workflow", "工作流缺少参数合同")
    targets = {}
    grouped_semantics = set()

    def add(row):
        key = row["target_key"]
        if key in targets:
            _error(key, "固定素材槽与动态素材组重名，不能推断要写入哪个目标")
        targets[key] = row

    for group in group_form_schema(contract):
        grouped_semantics.update(group["semantic_keys"])
        target = _target(
            group["key"], group["media_kind"], group["min_items"], group["max_items"],
            f"workflow.asset_groups.{group['key']}",
            {"kind": "semantic_group", "semantic_keys": group["semantic_keys"]},
        )
        # A group has per-port roles, not a role inferred from its name or
        # array position. Freeze the reviewed declarations with the interface.
        target["reference_bindings"] = {}
        for key in group["semantic_keys"]:
            definition = parameters.get(key)
            if not isinstance(definition, dict) or _MEDIA_TYPES.get(definition.get("type")) != group["media_kind"]:
                _error(key, "动态素材端口缺少类型一致的参数合同")
            role = _reference_binding(key, definition, group["media_kind"])
            if role is not None:
                target["reference_bindings"][key] = role
        add(target)
    classified = contract.get("classified_references")
    if classified is not None:
        if not isinstance(classified, dict) or not isinstance(classified.get("groups"), dict):
            _error("workflow", "分类动态素材接线合同缺失")
        for key, group in classified["groups"].items():
            if not isinstance(group, dict):
                _error(key, "分类动态素材组必须为对象")
            add(_target(
                key, group.get("media_kind"), group.get("min_items"), group.get("max_items"),
                f"workflow.classified_references.groups.{key}",
                {"kind": "classified_group", "node_id": classified["target_node_id"], "input_names": group["input_names"],
                 **({"index_source_target_key": CLASSIFIED_GROUP_INDEX_SOURCES[key]} if key in CLASSIFIED_GROUP_INDEX_SOURCES else {})},
            ))
    bindings = {}
    for row in workflow.semantic_bindings.values("semantic_key", "node_id", "input_name", "value_type", "is_required"):
        bindings.setdefault(row["semantic_key"], []).append(row)
    for key, definition in parameters.items():
        if not isinstance(definition, dict):
            _error(key, "工作流参数定义必须为对象")
        kind = _MEDIA_TYPES.get(definition.get("type"))
        if kind is None or key in grouped_semantics:
            continue
        matched = bindings.get(key, [])
        if not matched or any(row["value_type"] != definition["type"] for row in matched):
            _error(key, "固定素材槽缺少类型一致的已校验接线")
        target = _target(
            key, kind, definition.get("min_items"), definition.get("max_items"),
            f"workflow.parameters.{key}",
            {"kind": "semantic", "semantic_key": key},
        )
        role = _reference_binding(key, definition, kind)
        if role is not None:
            # Reuse the exact role/slot already consumed by workflow_semantics;
            # never derive first/last-frame semantics from a key or array order.
            target["reference_binding"] = role
        if target["max_items"] != 1 or type(definition.get("required")) is not bool:
            _error(key, "固定素材槽必须明确声明单值数量与必填性")
        required = definition["required"] or any(row["is_required"] for row in matched)
        if target["required"] != required:
            _error(key, "固定素材槽数量和接线的必填合同不一致")
        add(target)
    # Detect unmanaged media bindings as well as incomplete parameter entries.
    for key, rows in bindings.items():
        if any(row["value_type"] in _MEDIA_TYPES for row in rows) and key not in targets and key not in grouped_semantics:
            _error(key, "工作流素材接线未声明完整的目标合同")
    return json_contract(targets, max_bytes=execution_configuration_max_bytes()).value


@security_boundary_resolution_cache()
def validate_workflow_input_targets(version, *, require_complete=False):
    """Drafts may be incomplete, but authored mappings must already be valid.

    A definition publication must require complete coverage. Optional unmapped
    slots receive no source; they never inherit a file from the imported graph.
    """
    if version.workflow_version_id is None:
        _error("workflow", "必须选择精确工作流")
    targets = workflow_input_targets(version.workflow_version)
    inputs = list(version.inputs.prefetch_related("targets"))
    validate_execution_input_mapping(inputs)
    assigned = set()
    for spec in inputs:
        for mapping in spec.targets.all():
            if mapping.target_kind == "profile_input":
                continue
            if mapping.target_kind != "workflow_slot":
                _error(spec.key, "ComfyUI 输入必须指向工作流素材槽，不能写入直接 API 输入")
            target = targets.get(mapping.target_key)
            if target is None:
                _error(spec.key, f"工作流未声明目标 {mapping.target_key}")
            if spec.kind != target["kind"]:
                _error(spec.key, f"{target['source']} 需要 {target['kind']}，不能传入 {spec.kind}")
            if spec.min_items < target["min_items"] or spec.max_items > target["max_items"]:
                _error(spec.key, f"数量范围不能超出 {target['source']} 的 {target['min_items']}–{target['max_items']}")
            if target["required"] and not spec.required:
                _error(spec.key, f"{target['source']} 是必需素材，不能改为可选输入")
            assigned.add(mapping.target_key)
    if require_complete:
        missing = [key for key, target in targets.items() if target["required"] and key not in assigned]
        if missing:
            _error("workflow", "必需素材槽尚未接线：" + ", ".join(sorted(missing)))
    return targets


@security_boundary_resolution_cache()
def allocate_workflow_input_slots(targets, items_by_target):
    """Map already resolved item identities to the exact frozen ordered ports.

    Ownership, checksums, source selection and logical-to-target authorization
    belong to the preceding input resolver. This function only allocates ports;
    it never reads files, guesses a prefix, sorts user items or deduplicates an
    Asset used for different purposes. item_key identifies a use, not an Asset.
    Returned positions and port refs must also be used by prompt fact assembly.
    """
    if not isinstance(targets, dict) or not isinstance(items_by_target, dict):
        _error("workflow", "必须提供冻结目标目录及已解析的有序输入")
    if any(not isinstance(key, str) or not key for key in (*targets, *items_by_target)):
        _error("workflow", "目标必须使用明确字符串 key")
    json_contract([targets, items_by_target], max_bytes=execution_request_max_bytes())
    unknown = set(items_by_target) - targets.keys()
    if unknown:
        _error("workflow", "工作流未声明素材目标：" + ", ".join(sorted(unknown)))
    result, occupied = {}, {}
    for key, target in targets.items():
        if not isinstance(target, dict) or target.get("target_kind") != "workflow_slot" or target.get("target_key") != key or not isinstance(target.get("kind"), str) or target["kind"] not in {"image", "video", "audio"}:
            _error(key, "素材目录目标身份无效")
        minimum, maximum = target.get("min_items"), target.get("max_items")
        if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum:
            _error(key, "素材目录必须明确数量上下限")
        items = items_by_target.get(key, [])
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            _error(key, f"展开后的素材数量必须为 {minimum}–{maximum}，不能截断或补入图内素材")
        wiring = target.get("wiring")
        if not isinstance(wiring, dict):
            _error(key, "素材目录缺少接线合同")
        kind = wiring.get("kind")
        index_source = wiring.get("index_source_target_key")
        parent_positions = {}
        capacity = maximum
        if index_source is not None:
            if not isinstance(index_source, str) or index_source not in targets or index_source == key or kind != "classified_group":
                _error(key, "配对端口必须引用明确且不同的已声明父目标")
            parent = targets[index_source]
            if not isinstance(parent, dict) or type(parent.get("max_items")) is not int or parent["max_items"] < maximum or not isinstance(parent.get("wiring"), dict) or parent["wiring"].get("index_source_target_key") is not None:
                _error(key, "配对目标数量不能超过父目标，不允许递归索引")
            capacity = parent["max_items"] if maximum else 0
            parent_items = items_by_target.get(index_source, [])
            if not isinstance(parent_items, list):
                _error(key, "父目标必须提供有序素材列表")
            for position, item in enumerate(parent_items):
                if not isinstance(item, dict) or not isinstance(item.get("item_key"), str) or not item["item_key"] or item["item_key"] in parent_positions:
                    _error(key, "配对父素材必须具有唯一使用标识")
                parent_positions[item["item_key"]] = position
        if kind == "semantic":
            if maximum != 1 or not isinstance(wiring.get("semantic_key"), str) or not wiring["semantic_key"]:
                _error(key, "固定素材目标必须明确单个语义槽")
            ports = [{"kind": "semantic", "semantic_key": wiring["semantic_key"]}]
        elif kind == "semantic_group":
            names = wiring.get("semantic_keys")
            if not isinstance(names, list) or len(names) != maximum or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
                _error(key, "动态语义槽目录数量或顺序无效")
            ports = [{"kind": "semantic", "semantic_key": name} for name in names]
        elif kind == "classified_group":
            names, node_id = wiring.get("input_names"), wiring.get("node_id")
            if not isinstance(node_id, str) or not node_id or not isinstance(names, list) or len(names) != capacity or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
                _error(key, "分类素材必须完整声明节点与有序端口，不能按名称推测")
            ports = [{"kind": "node_input", "node_id": node_id, "input_name": name} for name in names]
        else:
            _error(key, "素材目标使用了未注册的接线方式")
        # Check even currently empty optional groups: their declared ports must
        # not collide as soon as a later request actually supplies those items.
        for port in ports:
            identity = tuple(sorted(port.items()))
            if identity in occupied:
                _error(key, f"与 {occupied[identity]} 争写同一冻结端口")
            occupied[identity] = key
        assigned, seen, used_positions = [], set(), set()
        for position, item in enumerate(items):
            fields = {"item_key", "kind", "paired_to"} if index_source is not None else {"item_key", "kind"}
            if not isinstance(item, dict) or set(item) != fields or not isinstance(item["item_key"], str) or not item["item_key"]:
                _error(key, "必须提供精确素材使用标识和类型，不能传路径或临时文件")
            if item["item_key"] in seen:
                _error(key, "同一目标的素材使用标识重复")
            if item["kind"] != target.get("kind") or item["kind"] not in {"image", "video", "audio"}:
                _error(key, "素材类型与已冻结目标不符")
            seen.add(item["item_key"])
            target_position = position
            if index_source is not None:
                if not isinstance(item["paired_to"], str) or item["paired_to"] not in parent_positions:
                    _error(key, "配对素材必须明确引用本次父目标的素材使用标识")
                target_position = parent_positions[item["paired_to"]]
            if target_position >= len(ports) or target_position in used_positions:
                _error(key, "素材使用不能争写同一端口或超出端口目录")
            used_positions.add(target_position)
            assigned.append({
                "item_key": item["item_key"], "position": position,
                "target_position": target_position, "target_key": key,
                "target_port_ref": deepcopy(ports[target_position]),
            })
        result[key] = assigned
    return json_contract(result, max_bytes=execution_request_max_bytes()).value
