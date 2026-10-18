"""Configured JSON paths and key references over the shared composition engine.

Only supplied snapshots/values are read. No project queries, model calls, task
writes, implicit entity creation, expression evaluation or name matching.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.data_structures import (
    _value_path, resolve_structure_value, validate_data_structure_snapshot,
)
from studio.services.execution_structure_mapping import NODE_LABELS, evaluate_structure, validate_composite_node
from studio.services.json_contracts import JsonExpansionBudget, canonical_json_bytes, parse_json_value_text
from studio.services.structure_paths import nonnull_schema as _base, path_schema as _path_schema, read_structure_path


MAPPING_NODE_LABELS = {
    "path": "字段路径", "lookup": "按稳定key引用",
    **{key: NODE_LABELS[key] for key in ("object", "array", "literal")},
    "each": "列表逐项映射",
}
MAPPING_SCOPE_LABELS = {"root": "整个来源", "item": "当前列表条目"}


def mapping_authoring_options():
    return {"schema_version": 1,
        "nodes": [{"value": key, "label": label} for key, label in MAPPING_NODE_LABELS.items()],
        "scopes": [{"value": key, "label": label} for key, label in MAPPING_SCOPE_LABELS.items()]}


def _fail(path, message):
    raise ValidationError({path: message})


def structure_path_options(snapshot):
    """Catalog from the same exact field definitions, including relative list paths."""
    schema = validate_data_structure_snapshot(snapshot)
    options, pending = [], [(schema, [])]
    while pending:
        current, tokens = pending.pop()
        base = _base(current)
        definition = base.get("x-field-definition")
        if definition is not None:
            options.append({"path": tokens, "node_key": base["x-node-key"], "field_definition": deepcopy(definition),
                "nullable": "anyOf" in current, "structure_version_id": snapshot["id"]})
        if base.get("type") == "object":
            pending.extend(reversed([(child, [*tokens, key]) for key, child in base.get("properties", {}).items()]))
        elif base.get("type") == "array":
            pending.append((base["items"], [*tokens, {"list_item": True}]))
    return options


def _mapping_types(schema):
    value = _base(schema).get("type")
    types = set(value) if isinstance(value, list) else {value} if value else set()
    # Nullable declarations still permit nonnull values. Actual null/missing
    # values are checked by the caller's target validator, never coerced here.
    return types - {"null"} or types


def _validate_mapping_result(source, target, path, *, recursive=False):
    produced, expected = _mapping_types(source), _mapping_types(target)
    if expected and any(kind not in expected and not (kind == "integer" and "number" in expected)
                        for kind in produced):
        _fail(path, "所选来源类型与目标字段类型不匹配，不进行隐式转换")
    if not recursive:
        return
    source, target = _base(source), _base(target)
    if produced == expected == {"object"}:
        properties, targets = source.get("properties", {}), target.get("properties", {})
        missing = set(target.get("required", ())) - set(properties)
        if missing:
            _fail(path, "来源对象未声明必需目标字段：" + ", ".join(sorted(missing)))
        if target.get("additionalProperties") is False and set(properties) - set(targets):
            _fail(path, "来源对象含目标未登记属性，请逐字段配置映射")
        for key in properties.keys() & targets.keys():
            _validate_mapping_result(properties[key], targets[key], _value_path(path, key), recursive=True)
    elif produced == expected == {"array"} and isinstance(source.get("items"), dict) and isinstance(target.get("items"), dict):
        _validate_mapping_result(source["items"], target["items"], path + ".items", recursive=True)


def _validate_mapping(node, source_schema, *, path="mapping.node", item_schema=None, target_schema=None, require_complete=False):
    if not isinstance(node, dict) or type(node.get("kind")) is not str:
        _fail(path, "请选择结构映射节点")
    kind = node["kind"]
    if kind not in MAPPING_NODE_LABELS:
        _fail(path, "不支持此映射节点；不执行脚本、表达式或业务动作")
    composite = validate_composite_node(node, path)
    result_schema = None
    target = _base(target_schema) if target_schema else None
    if kind == "path":
        if (set(node) != {"kind", "scope", "path"} or type(node["scope"]) is not str
                or node["scope"] not in MAPPING_SCOPE_LABELS):
            _fail(path, "字段路径须明确root/item作用域及path，不接受隐式来源")
        origin = source_schema if node["scope"] == "root" else item_schema
        if origin is None:
            _fail(path, "item仅能在列表逐项映射内使用")
        result_schema = _path_schema(origin, node["path"], path + ".path")
    elif kind == "lookup":
        if set(node) != {"kind", "collection", "key_path", "key", "value_path"}:
            _fail(path, "稳定key查找须明确collection/key_path/key/value_path")
        if not isinstance(node["collection"], dict) or node["collection"].get("kind") != "path":
            _fail(path + ".collection", "查找集合只能选择已声明路径")
        collection = _base(_validate_mapping(node["collection"], source_schema, path=path + ".collection", item_schema=item_schema))
        if collection.get("type") != "array" or _base(collection["items"]).get("type") != "object":
            _fail(path + ".collection", "查找集合必须是声明的对象列表")
        key_schema = _path_schema(collection["items"], node["key_path"], path + ".key_path")
        if _base(key_schema).get("type") != "string":
            _fail(path + ".key_path", "稳定声明key必须是文本字段")
        if not isinstance(node["key"], dict) or node["key"].get("kind") not in ("literal", "path"):
            _fail(path + ".key", "查找key须为明确固定值或字段路径")
        _validate_mapping(node["key"], source_schema, path=path + ".key", item_schema=item_schema, target_schema={"type": "string"})
        result_schema = _path_schema(collection["items"], node["value_path"], path + ".value_path")
    elif composite:
        if kind == "object":
            properties = target.get("properties", {}) if target else {}
            if target and set(node["fields"]) - set(properties):
                _fail(path + ".fields", "目标属性未登记到所选结构，不自动创建字段")
            if require_complete and target:
                missing = set(target.get("required", ())) - set(node["fields"])
                if missing:
                    _fail(path + ".fields", "映射缺少必需目标字段：" + ", ".join(sorted(missing)))
            for name, child in node["fields"].items():
                _validate_mapping(child, source_schema, path=_value_path(path + ".fields", name), item_schema=item_schema,
                    target_schema=properties.get(name), require_complete=require_complete)
            result_schema = {"type": "object"}
        elif kind == "array":
            if require_complete and target:
                count = len(node["items"])
                if count < target.get("minItems", 0) or ("maxItems" in target and count > target["maxItems"]):
                    _fail(path + ".items", "显式列表数量不满足目标结构要求")
            for index, child in enumerate(node["items"]):
                _validate_mapping(child, source_schema, path=f"{path}.items[{index}]", item_schema=item_schema,
                    target_schema=target.get("items") if target else None, require_complete=require_complete)
            result_schema = {"type": "array"}
        elif kind == "each":
            if not isinstance(node["source"], dict) or node["source"].get("kind") not in ("path", "lookup"):
                _fail(path + ".source", "逐项来源必须为已声明路径或明确key查找的列表")
            source = _base(_validate_mapping(node["source"], source_schema, path=path + ".source", item_schema=item_schema))
            if source.get("type") != "array":
                _fail(path + ".source", "逐项映射来源不是列表")
            _validate_mapping(node["item"], source_schema, path=path + ".item", item_schema=source["items"],
                target_schema=target.get("items") if target else None, require_complete=require_complete)
            result_schema = {"type": "array"}
        elif kind == "literal" and require_complete and target_schema:
            from studio.services.data_structures import _value_errors
            errors = _value_errors(target_schema, node["value"], path=path + ".value")
            if errors:
                raise ValidationError(errors)
        # Literal values, including objects, are checked after the shared
        # target default resolver. A missing key with a configured default is
        # not an invalid literal merely because its raw object is incomplete.
    else:
        _fail(path, "不支持此映射节点；不执行脚本、表达式或业务动作")
    if result_schema and target:
        _validate_mapping_result(result_schema, target_schema, path,
            recursive=require_complete and kind in ("path", "lookup"))
    return result_schema


def validate_mapping(mapping, source_schema, target_schema, *, require_complete=False):
    if (not isinstance(mapping, dict) or set(mapping) != {"schema_version", "node"}
            or type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1):
        _fail("mapping", "映射须为schema_version=1及显式node；不猜测旧配置")
    canonical_json_bytes(mapping)
    _validate_mapping(mapping["node"], source_schema, target_schema=target_schema, require_complete=require_complete)


def _map_value(node, source_value, *, consume):
    def read(node, item, item_source_path, target_path):
        if node["kind"] == "path":
            origin, location = (source_value, "source") if node["scope"] == "root" else (item, item_source_path)
            value, location, _ = read_structure_path(origin, node["path"], location, target_path, consume)
            origin = {"path": target_path, "kind": "path", "source_path": location}
            consume(origin)
            return value, [origin]
        collection, origins = read(node["collection"], item, item_source_path, target_path)
        collection_path = origins[-1]["source_path"]
        if not isinstance(collection, list):
            _fail(collection_path, "实际查找集合不是列表")
        if node["key"]["kind"] == "literal":
            key = node["key"]["value"]
        else:
            key, key_origins = read(node["key"], item, item_source_path, target_path)
            origins.extend(key_origins)
        if type(key) is not str or not key:
            _fail(target_path, "查找key须为非空文本，不按名称转换或猜测")
        index = {}
        for offset, row in enumerate(collection):
            row_path = _value_path(collection_path, offset)
            declared, key_path, _ = read_structure_path(row, node["key_path"], row_path, target_path, consume)
            if type(declared) is not str or not declared:
                _fail(key_path, "稳定声明key必须为非空文本")
            if declared in index:
                _fail(key_path, "稳定声明key重复，不能自动选第一项")
            index[declared] = (row, row_path)
        if key not in index:
            _fail(target_path, "查找key没有对应声明，不回退到同名或首项")
        row, row_path = index[key]
        value, selected_path, _ = read_structure_path(row, node["value_path"], row_path, target_path, consume)
        origin = {"path": target_path, "kind": "lookup", "key": key, "source_path": selected_path}
        consume(origin)
        return value, [*origins, origin]

    return evaluate_structure(node, read_leaf=None, unit_key=None, reference_subjects=[],
        read_value=read, path="value", consume=consume)


def evaluate_mapping(mapping, source_value, *, source_schema, target_schema, consume):
    """One mapper for structure previews and typed business input projections.

    The caller owns source/default resolution and target value validation; a
    business input schema is not another registered field dictionary.
    """
    validate_mapping(mapping, source_schema, target_schema)
    return _map_value(mapping["node"], source_value, consume=consume)


def unmapped_fields(schema, value, *, path, consume):
    warnings, pending = [], [(schema, value, path)]
    while pending:
        schema, value, location = pending.pop()
        schema = _base(schema)
        if schema.get("type") == "object" and isinstance(value, dict):
            properties = schema.get("properties", {})
            for key, child in value.items():
                child_path = _value_path(location, key)
                if key not in properties:
                    warning = {"path": child_path, "code": "unmapped_field", "message": "未登记/未映射字段，原始值保留"}
                    consume(warning)
                    warnings.append(warning)
                else:
                    consume({"path": child_path})
                    pending.append((properties[key], child, child_path))
        elif schema.get("type") == "array" and isinstance(value, list):
            for index, child in enumerate(value):
                child_path = _value_path(location, index)
                consume({"path": child_path})
                pending.append((schema["items"], child, child_path))
    return warnings


def preview_structure_mapping(*, source_contract, target_contract, source_text, mapping, max_bytes):
    """Report every boundary honestly: raw parse, resolution, mapping, target check."""
    result = {"preview_only": True, "valid": False, "parsed": False, "raw_value": None,
        "source_value": None, "value": None, "defaults": [], "provenance": [], "warnings": [], "errors": {},
        "source_structure_version_id": source_contract.get("id") if isinstance(source_contract, dict) else None,
        "target_structure_version_id": target_contract.get("id") if isinstance(target_contract, dict) else None}
    try:
        source_schema = validate_data_structure_snapshot(source_contract)
        target_schema = validate_data_structure_snapshot(target_contract)
        raw = parse_json_value_text(source_text, max_bytes=max_bytes)
        result.update(parsed=True, raw_value=raw)
        budget = JsonExpansionBudget(max_bytes, path="mapping.preview_budget")
        source = resolve_structure_value(source_contract, raw, path="source", consume=budget.consume)
        result.update(source_value=source["value"], defaults=source["defaults"],
            warnings=unmapped_fields(source_schema, raw, path="source", consume=budget.consume))
        if not source["valid"]:
            result["errors"] = source["errors"]
            return result
        value, origins = evaluate_mapping(mapping, source["value"], source_schema=source_schema,
            target_schema=target_schema, consume=budget.consume)
        for origin in origins:
            record = {**origin, "source_structure_version_id": source_contract["id"],
                "target_structure_version_id": target_contract["id"]}
            budget.consume(record)
            result["provenance"].append(record)
        target = resolve_structure_value(target_contract, value, consume=budget.consume)
        result.update(valid=target["valid"], value=target["value"], errors=target["errors"])
        result["defaults"].extend(target["defaults"])
        result["warnings"].extend(unmapped_fields(target_schema, value, path="value", consume=budget.consume))
    except ValidationError as exc:
        result["errors"] = getattr(exc, "message_dict", None) or {"source_text" if not result["parsed"] else "mapping": exc.messages}
    except RecursionError:
        result["errors"] = {"mapping": ["当前嵌套无法解析，请检查结构与样例"]}
    return result
