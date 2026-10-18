"""Shared structure authoring and JSON Schema projection; no business execution."""
from copy import deepcopy
import json
import math
import re
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from studio.models import DataStructureNode, DataStructureVersion, FieldDefinitionVersion
from studio.services.field_definitions import field_definition_snapshot, validate_field_definition_snapshot
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, JsonExpansionBudget, canonical_json_bytes, json_contract


NODE_FIELDS = (
    "node_key", "property_name", "position", "required", "allow_null", "allow_blank", "allow_empty",
    "has_default", "default_value", "constraints",
)
NODE_DEFAULTS = {
    "parent_key": None, "field_definition_version": None, "property_name": "", "position": 0,
    "required": False, "allow_null": False, "allow_blank": True, "allow_empty": True, "constraints": {},
    "has_default": False, "default_value": None,
}
MEDIA_TYPES = ("image", "video", "audio")
_JSON_TYPES = {
    "text": "string", "long_text": "string", "integer": "integer", "decimal": "number",
    "boolean": "boolean", "enum": "string", "object": "object", "list": "array", "media": "object",
}

# One registry feeds both authoring controls and the compiler's allowed rules.
CONSTRAINT_SCHEMAS = {
    "text": {"min_length": {"type": "integer", "minimum": 0, "title": "最少字符"}, "max_length": {"type": "integer", "minimum": 0, "title": "最多字符"}},
    "long_text": {"min_length": {"type": "integer", "minimum": 0, "title": "最少字符"}, "max_length": {"type": "integer", "minimum": 0, "title": "最多字符"}},
    "integer": {"minimum": {"type": "integer", "title": "最小值"}, "maximum": {"type": "integer", "title": "最大值"}},
    "decimal": {"minimum": {"type": "number", "title": "最小值"}, "maximum": {"type": "number", "title": "最大值"}},
    "list": {"min_items": {"type": "integer", "minimum": 0, "title": "最少条目"}, "max_items": {"type": "integer", "minimum": 0, "title": "最多条目"}},
    "enum": {"enum": {"type": "array", "title": "允许的枚举子集", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True, "x-field-enum": True}},
    "media": {"media_types": {"type": "array", "title": "素材类型", "items": {"type": "string", "enum": list(MEDIA_TYPES)}, "minItems": 1, "uniqueItems": True}, "usage": {"type": "string", "minLength": 1, "title": "素材用途"}},
    "object": {}, "boolean": {},
}


def structure_authoring_options():
    return {
        "node_defaults": deepcopy(NODE_DEFAULTS),
        "statuses": [{"value": value, "label": label} for value, label in DataStructureVersion._meta.get_field("status").choices],
        "constraint_schemas": {kind: {"type": "object", "properties": deepcopy(rules), "additionalProperties": False} for kind, rules in CONSTRAINT_SCHEMAS.items()},
        "value_schemas": {kind: {"type": value} for kind, value in _JSON_TYPES.items()},
    }


def _error(path, message):
    raise ValidationError({path: message})


def _usage_schema(node, definition):
    path = f"nodes.{node['node_key']}.constraints"
    kind = definition["value_type"]
    rules = node["constraints"]
    if not isinstance(rules, dict):
        _error(path, "使用约束必须是对象")
    try:
        json_contract(rules, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    except ValidationError as exc:
        _error(path, exc.messages)
    allowed = set(CONSTRAINT_SCHEMAS[kind])
    for key in rules.keys() - allowed:
        _error(f"{path}.{key}", f"{kind} 不支持此使用约束")
    projected = {}
    for lower, upper, out_lower, out_upper in (
        ("min_length", "max_length", "minLength", "maxLength"),
        ("min_items", "max_items", "minItems", "maxItems"),
        ("minimum", "maximum", "minimum", "maximum"),
    ):
        for key, out in ((lower, out_lower), (upper, out_upper)):
            if key not in rules:
                continue
            value = rules[key]
            numeric = key in {"minimum", "maximum"}
            valid = type(value) in {int, float} if numeric and kind == "decimal" else type(value) is int
            if not valid or (type(value) is float and not math.isfinite(value)) or (not numeric and value < 0):
                _error(f"{path}.{key}", "范围必须是有限数字；长度/数量为非负整数，整数边界不能为小数")
            projected[out] = value
        if lower in rules and upper in rules and rules[lower] > rules[upper]:
            _error(path, "下限不能大于上限")
    if kind in {"text", "long_text", "enum"} and not node["allow_blank"]:
        projected["minLength"] = max(1, projected.get("minLength", 0))
    if kind in {"list", "object"} and not node["allow_empty"]:
        projected["minItems" if kind == "list" else "minProperties"] = max(1, projected.get("minItems", 0))
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if lower in projected and upper in projected and projected[lower] > projected[upper]:
            _error(path, "空值设置与长度/数量上限冲突")
    if kind == "enum":
        values = rules.get("enum", definition["value_options"])
        if (not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values)) or set(values) - set(definition["value_options"])):
            _error(f"{path}.enum", "使用处枚举必须为字段版本选项的非空子集")
        projected["enum"] = deepcopy(values)
    if kind == "media":
        types = rules.get("media_types")
        if (not isinstance(types, list) or not types or any(item not in MEDIA_TYPES for item in types)
                or len(types) != len(set(types))):
            _error(f"{path}.media_types", "请选择非重复的图片、视频或音频类型")
        if "usage" in rules and (not isinstance(rules["usage"], str) or not rules["usage"].strip()):
            _error(f"{path}.usage", "素材用途必须为非空文本")
        projected.update({
            "properties": {"asset_id": {"type": "string", "format": "uuid"}},
            "required": ["asset_id"], "additionalProperties": False,
            "x-media-types": deepcopy(types),
        })
        if "usage" in rules:
            projected["x-media-usage"] = rules["usage"]
    return projected


def compile_structure_nodes(nodes):
    """Pure compiler over exact field snapshots, usable after the registry changes.

    Unknown business properties remain available to parsing/diagnostics; they
    are not silently dropped or registered. Media is a reference, not a file.
    """
    if not isinstance(nodes, list) or not nodes:
        _error("nodes", "结构必须包含一个显式根节点")
    by_key, children, roots = {}, {}, []
    expected = set(NODE_FIELDS) | {"parent_key", "field_definition"}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or set(node) != expected:
            _error(f"nodes.{index}", "节点合同不完整或包含未知属性")
        key = node["node_key"]
        for name in NODE_FIELDS:
            if name in {"constraints", "default_value"}:
                continue
            value = node[name]
            if name in {"required", "allow_null", "allow_blank", "allow_empty", "has_default"} and type(value) is not bool:
                _error(f"nodes.{key}.{name}", "必须是布尔值，不进行隐式转换")
            if name == "position" and (type(value) is not int or value < 0):
                _error(f"nodes.{key}.{name}", "顺序必须是非负整数")
            if name in {"node_key", "property_name"} and not isinstance(value, str):
                _error(f"nodes.{index}.{name}", "必须是文本")
            try:
                DataStructureNode._meta.get_field(name).clean(value, None)
            except ValidationError as exc:
                _error(f"nodes.{key}.{name}", exc.messages)
        if key in by_key:
            _error(f"nodes.{key}", "节点key重复")
        if not node["has_default"] and node["default_value"] is not None:
            _error(f"nodes.{key}.default_value", "填写默认值须显式启用 has_default")
        try:
            json_contract(node["default_value"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        except ValidationError as exc:
            _error(f"nodes.{key}.default_value", exc.messages)
        parent = node["parent_key"]
        if parent is not None and not isinstance(parent, str):
            _error(f"nodes.{key}.parent_key", "父节点必须是节点key或null")
        try:
            validate_field_definition_snapshot(node["field_definition"])
        except ValidationError as exc:
            _error(f"nodes.{key}.field_definition", exc.messages)
        by_key[key] = node
        children.setdefault(parent, []).append(key)
        if parent is None:
            roots.append(key)
    # Check all components, including cycles disconnected from an otherwise valid root.
    checked = set()
    for key in by_key:
        chain = set()
        cursor = key
        while cursor is not None and cursor not in checked:
            if cursor not in by_key:
                _error(f"nodes.{key}.parent_key", f"父节点 {cursor} 不存在")
            if cursor in chain:
                _error(f"nodes.{cursor}.parent_key", "结构包含循环父子关系")
            chain.add(cursor)
            cursor = by_key[cursor]["parent_key"]
        checked.update(chain)
    if len(roots) != 1:
        _error("nodes", "结构必须且只能有一个根节点")
    for key, node in by_key.items():
        parent = node["parent_key"]
        if parent is None or by_key[parent]["field_definition"]["value_type"] == "list":
            if node["property_name"] or node["required"] or node["has_default"]:
                _error(f"nodes.{key}", "根节点/列表元素不设置对象属性名、必填或缺键默认规则")
        elif not node["property_name"]:
            _error(f"nodes.{key}.property_name", "对象子节点必须填写属性名")
    order = [roots[0]]
    for key in order:
        order.extend(sorted(children.get(key, []), key=lambda child: (by_key[child]["position"], child)))
    schemas = {}
    for key in reversed(order):
        node = by_key[key]
        definition = node["field_definition"]
        kind = definition["value_type"]
        nested = sorted(children.get(key, []), key=lambda child: (by_key[child]["position"], child))
        schema = {
            "type": _JSON_TYPES[kind], "title": definition["label"], "description": definition["description"],
            "x-node-key": key, "x-field-definition": deepcopy(definition),
            **_usage_schema(node, definition),
        }
        if kind == "object":
            names = [by_key[child]["property_name"] for child in nested]
            if len(set(names)) != len(names):
                _error(f"nodes.{key}", "同一对象内的属性名不能重复")
            schema["properties"] = {by_key[child]["property_name"]: schemas[child] for child in nested}
            schema["required"] = [by_key[child]["property_name"] for child in nested if by_key[child]["required"]]
            schema["additionalProperties"] = True
        elif kind == "list":
            if len(nested) != 1:
                _error(f"nodes.{key}", "列表必须且只能有一个元素定义")
            schema["items"] = schemas[nested[0]]
        elif nested:
            _error(f"nodes.{key}", "只有对象/列表字段能够包含子节点")
        if node["allow_null"]:
            # anyOf keeps nullable enums, objects and media valid without
            # accidentally rejecting null through enum/required constraints.
            schema = {"anyOf": [schema, {"type": "null"}], "x-node-key": key}
        if node["has_default"]:
            # A default follows the same nested missing-key rules as supplied
            # data. JSON Schema's default is annotation only, not a value writer.
            budget = JsonExpansionBudget(DATABASE_JSON_FIELD_MAX_BYTES, path=f"nodes.{key}.default_value")
            default, _sources = _apply_defaults(by_key, children, key, node["default_value"], consume=budget.consume)
            errors = _value_errors(schema, default, path=f"nodes.{key}.default_value")
            if errors:
                raise ValidationError(errors)
            schema["default"] = deepcopy(node["default_value"])
        schemas[key] = schema
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schemas[roots[0]]}


def structure_node_contracts(structure, *, require_published_fields=False):
    nodes = list(structure.nodes.select_related("field_definition_version__field_definition").order_by("position", "node_key"))
    keys = {node.pk: node.node_key for node in nodes}
    result = []
    for node in nodes:
        path = f"nodes.{node.node_key}"
        definition = node.field_definition_version
        if definition is None:
            _error(f"{path}.field_definition_version", "请选择确切字段版本")
        if node.parent_id is not None and node.parent_id not in keys:
            _error(f"{path}.parent_key", "父节点必须属于同一结构版本")
        if require_published_fields and (definition.status != "published" or not definition.field_definition.is_active):
            _error(f"{path}.field_definition_version", "启用新结构必须引用可用字段版本")
        try:
            frozen = field_definition_snapshot(definition)
        except ValidationError as exc:
            _error(f"{path}.field_definition_version", exc.messages)
        result.append({
            **{name: deepcopy(getattr(node, name)) for name in NODE_FIELDS},
            "parent_key": keys[node.parent_id] if node.parent_id else None, "field_definition": frozen,
        })
    return result


def compile_data_structure(structure, *, require_published_fields=False):
    return compile_structure_nodes(structure_node_contracts(structure, require_published_fields=require_published_fields))


def data_structure_snapshot(structure):
    if structure.status == "draft":
        _error("data_structure_version", "草稿结构不能作为执行合同")
    return describe_data_structure(structure)


def describe_data_structure(structure):
    """Exact configuration for authoring previews; does not authorize execution."""
    nodes = structure_node_contracts(structure, require_published_fields=structure.status == "draft")
    compile_structure_nodes(nodes)
    # The JSON Schema is derived, never persisted as another editable schema.
    return {
        "schema_version": 2, "id": str(structure.pk), "structure_key": str(structure.structure_key),
        "version": structure.version, "name": structure.name,
        "nodes": nodes,
    }


def validate_data_structure_snapshot(snapshot):
    """Validate frozen metadata and derive its schema without registry lookups."""
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"schema_version", "id", "structure_key", "version", "name", "nodes"}
            or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 2
            or type(snapshot["version"]) is not int or snapshot["version"] < 1
            or not isinstance(snapshot["name"], str) or not snapshot["name"].strip()):
        _error("data_structure_version", "结构快照缺失或格式不完整，不会查询当前版本替代")
    for key in ("id", "structure_key"):
        try:
            if not isinstance(snapshot[key], str):
                raise ValueError
            UUID(snapshot[key])
        except ValueError:
            _error(f"data_structure_version.{key}", "结构标识必须为 UUID")
    json_contract(snapshot, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    return compile_structure_nodes(snapshot["nodes"])


def _value_path(path, part):
    if isinstance(part, int):
        return f"{path}[{part}]"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
        return f"{path}.{part}"
    return f"{path}[{json.dumps(part, ensure_ascii=False)}]"


def _value_errors(schema, value, *, path):
    errors = {}
    pending = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    while pending:
        error = pending.pop(0)
        # This compiler only emits anyOf for nullable fields. Report the actual
        # non-null branch failure rather than hiding a child path at its parent.
        if error.validator == "anyOf" and error.instance is not None:
            pending[0:0] = [child for child in error.context if child.schema_path[0] == 0]
            continue
        location = path
        for part in error.absolute_path:
            location = _value_path(location, part)
        if error.validator == "required" and isinstance(error.instance, dict):
            for name in error.validator_value:
                if name not in error.instance:
                    errors[_value_path(location, name)] = ["缺少必填字段"]
            continue
        # Do not echo complete user input into an error, particularly long text.
        detail = f"不符合结构约束（{error.validator}）"
        if error.validator in {"minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "type"}:
            detail += f"：{error.validator_value}"
        errors.setdefault(location, []).append(detail)
    return errors


def validate_structure_value(snapshot, value, *, path="value"):
    """Strict validation only: never applies defaults or changes a raw response."""
    errors = _value_errors(validate_data_structure_snapshot(snapshot), value, path=path)
    if errors:
        raise ValidationError(errors)


def _apply_defaults(by_key, children, root_key, value, *, path="value", consume=None):
    if consume is not None:
        consume(value)
    result, sources = deepcopy(value), []
    pending = [(root_key, result, path)]
    while pending:
        key, current, location = pending.pop()
        if consume is not None:
            consume({"path": location, "node_key": key})
        kind = by_key[key]["field_definition"]["value_type"]
        nested = sorted(children.get(key, []), key=lambda child: (by_key[child]["position"], child))
        descend = []
        if kind == "object" and isinstance(current, dict):
            for child_key in nested:
                child = by_key[child_key]
                prop = child["property_name"]
                child_path = _value_path(location, prop)
                if prop not in current and child["has_default"]:
                    if consume is not None:
                        consume(child["default_value"])
                    current[prop] = deepcopy(child["default_value"])
                    source = {"path": child_path, "node_key": child_key,
                        "field_definition_version_id": child["field_definition"]["field_definition_version_id"],
                        "source": "configured_default", "value": child["default_value"]}
                    if consume is not None:
                        consume(source)
                    sources.append(deepcopy(source))
                if prop in current:
                    if consume is not None:
                        consume({"path": child_path})
                    descend.append((child_key, current[prop], child_path))
        elif kind == "list" and isinstance(current, list):
            for index, item in enumerate(current):
                child_path = _value_path(location, index)
                if consume is not None:
                    consume({"path": child_path})
                descend.append((nested[0], item, child_path))
        pending.extend(reversed(descend))
    return result, sources


def resolve_structure_value(snapshot, value, *, path="value", consume=None):
    """Resolve missing keys on a copy; preserve raw input and explicit empty values.

    Invalid values remain visible with path errors; consumers must check valid.
    Contract errors raise instead of pretending an unusable structure succeeded.
    """
    schema = validate_data_structure_snapshot(snapshot)
    return _resolve_structure_nodes(snapshot["nodes"], schema, value, path=path,
        structure_version_id=snapshot["id"], consume=consume)


def _resolve_structure_nodes(nodes, schema, value, *, path, structure_version_id, consume=None):
    try:
        canonical_json_bytes(value)
    except ValidationError as exc:
        _error(path, exc.messages)
    by_key, children, root = {}, {}, None
    for node in nodes:
        by_key[node["node_key"]] = node
        children.setdefault(node["parent_key"], []).append(node["node_key"])
        if node["parent_key"] is None:
            root = node["node_key"]
    resolved, sources = _apply_defaults(by_key, children, root, value, path=path, consume=consume)
    for source in sources:
        if consume is not None:
            consume({"structure_version_id": str(structure_version_id)})
        source["structure_version_id"] = str(structure_version_id)
    errors = _value_errors(schema, resolved, path=path)
    return {"valid": not errors, "value": resolved, "defaults": sources, "errors": errors}


def preview_structure_value(structure, value, *, consume=None):
    """Side-effect-free draft/exact-version preview, not a frozen execution contract."""
    nodes = structure_node_contracts(structure, require_published_fields=structure.status == "draft")
    schema = compile_structure_nodes(nodes)
    return _resolve_structure_nodes(nodes, schema, value, path="value", structure_version_id=structure.pk, consume=consume)


def _replace_nodes(structure, values):
    if not isinstance(values, list):
        _error("nodes", "节点必须为数组")
    prepared, by_key = [], {}
    for index, data in enumerate(values):
        if not isinstance(data, dict) or set(data) - (set(NODE_FIELDS) | set(NODE_DEFAULTS)):
            _error(f"nodes.{index}", "节点包含未知属性")
        if data.get("has_default") is True and "default_value" not in data:
            _error(f"nodes.{index}.default_value", "启用默认值时必须显式提供 default_value；允许 null 时可明确填写 null")
        row = {**deepcopy(NODE_DEFAULTS), **deepcopy(data)}
        if "node_key" not in row:
            _error(f"nodes.{index}.node_key", "请输入稳定节点key")
        key = row["node_key"]
        if not isinstance(key, str):
            _error(f"nodes.{index}.node_key", "节点key必须为文本")
        if key in by_key:
            _error(f"nodes.{key}", "节点key重复")
        try:
            definition_id = row.pop("field_definition_version")
            if definition_id is not None:
                if not isinstance(definition_id, str):
                    raise ValidationError("字段版本必须为UUID文本或null")
                FieldDefinitionVersion._meta.pk.clean(definition_id, None)
            if not isinstance(row["property_name"], str):
                raise ValidationError("对象属性名必须是文本，不进行隐式转换")
            node = DataStructureNode(
                structure=structure, field_definition_version_id=definition_id,
                **{name: row[name] for name in NODE_FIELDS},
            )
            node.clean_fields(exclude=("parent",))
            if any(type(row[name]) is not bool for name in ("required", "allow_null", "allow_blank", "allow_empty", "has_default")):
                raise ValidationError("节点开关必须是布尔值")
            if not row["has_default"] and row["default_value"] is not None:
                raise ValidationError("填写默认值须显式启用 has_default")
            if type(row["position"]) is not int or row["position"] < 0:
                raise ValidationError("节点顺序必须是非负整数")
            if not isinstance(row["constraints"], dict):
                raise ValidationError("使用约束必须是对象")
            json_contract(row["constraints"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            json_contract(row["default_value"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        except (ValidationError, ValueError, TypeError) as exc:
            _error(f"nodes.{key}", exc.messages if isinstance(exc, ValidationError) else str(exc))
        by_key[key] = node
        prepared.append((node, row["parent_key"]))
    for node, parent in prepared:
        if parent is not None:
            if not isinstance(parent, str) or parent not in by_key:
                _error(f"nodes.{node.node_key}.parent_key", "父节点不存在")
            node.parent = by_key[parent]
    structure.nodes.all().delete()
    DataStructureNode.objects.bulk_create([node for node, _parent in prepared])


@transaction.atomic
def write_data_structure(*, content, version_id=None, source_version_id=None):
    if set(content) - {"name", "description", "nodes"}:
        raise ValidationError("结构身份、版本和状态由服务端维护")
    if version_id and source_version_id:
        raise ValidationError("已有版本不能更换来源")
    if version_id:
        structure = DataStructureVersion.objects.select_for_update().get(pk=version_id)
        if structure.status != "draft":
            raise ValidationError("只能修改草稿；请创建新结构版本")
    elif source_version_id:
        source = DataStructureVersion.objects.get(pk=source_version_id)
        group = DataStructureVersion.objects.select_for_update().filter(structure_key=source.structure_key)
        list(group.values_list("id", flat=True))
        source.refresh_from_db()
        number = group.aggregate(value=Max("version"))["value"] + 1
        structure = DataStructureVersion(structure_key=source.structure_key, version=number, name=source.name, description=source.description)
        if "nodes" not in content:
            nodes = list(source.nodes.all())
            keys = {node.pk: node.node_key for node in nodes}
            content = {**content, "nodes": [{
                **{name: deepcopy(getattr(node, name)) for name in NODE_FIELDS},
                "parent_key": keys[node.parent_id] if node.parent_id else None,
                "field_definition_version": str(node.field_definition_version_id) if node.field_definition_version_id else None,
            } for node in nodes]}
    else:
        structure = DataStructureVersion()
    for name in ("name", "description"):
        if name in content:
            if not isinstance(content[name], str):
                _error(name, "必须是文本")
            setattr(structure, name, content[name])
    structure.save()
    if "nodes" in content:
        _replace_nodes(structure, content["nodes"])
    return structure


@transaction.atomic
def transition_data_structure(*, version_id, status):
    structure = DataStructureVersion.objects.select_for_update().get(pk=version_id)
    if status == "published" and structure.status == "draft":
        # Model.save performs the same compiler check for every publish writer.
        pass
    elif status == "retired" and structure.status == "published":
        pass
    elif structure.status != status or status not in {"published", "retired"}:
        raise ValidationError("只能启用草稿或停用已发布结构；停用后请创建新版本")
    structure.status = status
    structure.save()
    return structure
