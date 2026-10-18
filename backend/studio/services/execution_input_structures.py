"""One exact logical-input structure, shared by forms and value resolution."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.data_structures import (
    data_structure_snapshot, resolve_structure_value, validate_data_structure_snapshot,
)


def _schema(snapshot, kind, path):
    schema = validate_data_structure_snapshot(snapshot)
    if kind not in {"text", "json"} or (kind == "text" and schema.get("type") != "string"):
        raise ValidationError({path: "通用结构只用于文本/JSON逻辑输入；文本必须引用不允许null的字符串根节点"})
    root = next(node for node in snapshot["nodes"] if node["parent_key"] is None)["field_definition"]
    schema.setdefault("title", root["label"])
    schema.setdefault("description", root["description"])
    return schema


def input_structure_snapshot(spec):
    from studio.models import DataStructureVersion
    try:
        structure = spec.data_structure_version
    except DataStructureVersion.DoesNotExist:
        raise ValidationError({"data_structure_version_id": "所选结构版本不存在"}) from None
    if structure.status != "published":
        raise ValidationError({"data_structure_version_id": "新配置需要选择已发布的确切结构版本"})
    snapshot = data_structure_snapshot(structure)
    _schema(snapshot, spec.kind, f"inputs.{spec.key}.data_structure_version_id")
    return snapshot


def freeze_input_structures(version):
    snapshots, result = {}, {}
    for spec in version.inputs.select_related("data_structure_version").filter(data_structure_version__isnull=False):
        if spec.data_structure_version_id not in snapshots:
            snapshots[spec.data_structure_version_id] = input_structure_snapshot(spec)
        snapshot = snapshots[spec.data_structure_version_id]
        _schema(snapshot, spec.kind, f"inputs.{spec.key}.data_structure_version_id")
        result[spec.key] = snapshot
    return result


def input_structure_schemas(definition):
    """Validate identity pairs without reading today's registry or choosing latest."""
    structures = definition["interface"].get("input_structures", {})
    if not isinstance(structures, dict):
        raise ValidationError({"input_structures": "冻结输入结构必须按逻辑输入标识编排"})
    specs = {spec["key"]: spec for spec in definition["configuration"]["inputs"]
        if spec.get("data_structure_version_id") is not None}
    if set(specs) != set(structures):
        raise ValidationError({"input_structures": "输入结构引用与冻结快照必须成对存在"})
    schemas = {}
    for key, spec in specs.items():
        snapshot = structures[key]
        schema = _schema(snapshot, spec["kind"], f"inputs.{key}.data_structure_version_id")
        if snapshot["id"] != spec["data_structure_version_id"]:
            raise ValidationError({f"inputs.{key}": "输入结构引用与冻结版本身份不一致"})
        schemas[key] = schema
    return schemas


def resolve_input_structure(definition, key, value):
    snapshot = definition["interface"].get("input_structures", {}).get(key)
    if snapshot is None:
        return value, None
    from studio.services.execution_request_storage import execution_request_max_bytes
    from studio.services.json_contracts import JsonExpansionBudget
    budget = JsonExpansionBudget(execution_request_max_bytes(), path=f"inputs.{key}")
    result = resolve_structure_value(snapshot, value, path=f"inputs.{key}", consume=budget.consume)
    if not result["valid"]:
        raise ValidationError(result["errors"])
    return result["value"], {"structure_version_id": snapshot["id"], "defaults": result["defaults"]}


def input_template_for_validation(definition, key, template, *, path):
    """Expand known defaults on an ephemeral template, never the authored rule.

    Actual values still go through resolve_input_structure. Dynamic leaves are
    not read here, and their values/default evidence are only known at runtime.
    """
    snapshot = definition["interface"].get("input_structures", {}).get(key)
    if snapshot is None:
        return template
    from studio.services.data_structures import _apply_defaults, _value_path
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.json_contracts import JsonExpansionBudget, json_contract

    max_bytes = execution_configuration_max_bytes()
    budget = JsonExpansionBudget(max_bytes, path=path)
    by_key, children = {}, {}
    for row in snapshot["nodes"]:
        by_key[row["node_key"]] = row
        children.setdefault(row["parent_key"], []).append(row["node_key"])

    def defaults(node_key, value, location):
        return _apply_defaults(by_key, children, node_key, value,
            path=location, consume=budget.consume)[0]

    def visit(node, node_key, location):
        if node_key is None:
            return node
        kind = node["kind"]
        value_type = by_key[node_key]["field_definition"]["value_type"]
        nested = children.get(node_key, [])
        if kind == "literal":
            return {**node, "value": defaults(node_key, node["value"], location)}
        if kind == "item_field" and "if_missing" in node:
            return {**node, "if_missing": defaults(node_key, node["if_missing"], location)}
        if kind == "object" and value_type == "object":
            properties = {by_key[child]["property_name"]: child for child in nested}
            fields = {name: visit(child, properties.get(name), _value_path(location, name))
                for name, child in node["fields"].items()}
            # Use the same missing-key writer as runtime. Existing template
            # leaves, including explicit null/empty literals, always win.
            for name, value in defaults(node_key, dict.fromkeys(fields), location).items():
                if name not in fields:
                    fields[name] = {"kind": "literal", "value": value}
            return {**node, "fields": fields}
        if kind == "array" and value_type == "list":
            return {**node, "items": [visit(child, nested[0], _value_path(location, i))
                for i, child in enumerate(node["items"])]}
        if kind == "each" and value_type == "list":
            return {**node, "item": visit(node["item"], nested[0], location + ".item")}
        # unit_map keys and source-returned objects are unknown until binding;
        # do not invent an identity or a value to make preflight succeed.
        return node

    preview = visit(deepcopy(template), children[None][0], path)
    json_contract(preview, max_bytes=max_bytes)
    return preview
