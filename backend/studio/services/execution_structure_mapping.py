"""Closed value composition, shared by Recipe authoring and input resolution.

No expressions, project queries or model-specific templates live here. Leaves
are resolved by the existing authorized source reader; identities come from
the original production unit and already-normalized media in the same call.
"""
from copy import deepcopy
from hashlib import sha256

from django.core.exceptions import ValidationError

from studio.services.execution_input_catalog import execution_context_field_options, execution_text_output_options
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_shot_field_mapping import shot_field_mapping_cache
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.structure_paths import MISSING, nonnull_schema, path_schema, read_structure_path


NODE_LABELS = {"object": "按字段组成对象", "array": "按顺序组成列表", "unit_map": "按当前制作单元分组",
    "each": "逐条映射已审核列表", "literal": "明确固定值", "context_field": "受控内容字段",
    "selected_output": "已采用上游文本", "unit_key": "当前制作单元标识",
    "reference_subjects": "本次主参考图的主体", "item_field": "当前列表条目的字段",
    "material_difference": "当前已审核变体的差异", "shot_field": "独立分镜字段"}


def structure_node_options():
    return [{"value": key, "label": label} for key, label in NODE_LABELS.items()]


def _fail(path, message):
    raise ValidationError({path: message})


def structure_source_option(node, path, text_outputs=None):
    if node.get("kind") == "shot_field":
        from studio.services.execution_shot_field_mapping import shot_field_source_option
        return shot_field_source_option(node)
    rows = ([row for row in (execution_text_output_options() if text_outputs is None else text_outputs)
        if row["output_key"] == node.get("output_key")
        and ("stage_key" not in node or row["stage_key"] == node["stage_key"])]
        if node.get("kind") == "selected_output" else
        [row for row in execution_context_field_options() if row["selector_key"] == node.get("selector_key")])
    if len(rows) != 1:
        _fail(path, "来源字段未在服务器目录登记")
    return rows[0]


def structure_value_schema(node, path, item_schema=None, text_outputs=None):
    if node["kind"] == "item_field":
        if item_schema is None:
            _fail(path, "条目路径只能在逐条映射内选择")
        schema = item_schema
    else:
        option = structure_source_option(node, path, text_outputs)
        schema = option.get("value_schema", {"type": "string"} if option["kind"] == "text" else {})
    return path_schema(schema, node.get("path", []), path + ".path")


def source_schema_sha256(node, path, text_outputs=None):
    option = structure_source_option(node, path, text_outputs)
    schema = option.get("value_schema", {"type": "string"} if option["kind"] == "text" else {})
    return sha256(canonical_json_bytes(schema)).hexdigest()


@shot_field_mapping_cache()
def freeze_structure_source_schemas(text_outputs=None, *, stages=()):
    """Server-derived reader schema identities, frozen once with the RunPlan."""
    from studio.services.execution_text_output_catalog import text_output_schema_key
    result = {kind: {(text_output_schema_key(row) if kind == "selected_output" else row[key]): sha256(canonical_json_bytes(
                row.get("value_schema", {"type": "string"} if row["kind"] == "text" else {}))).hexdigest() for row in rows}
        for kind, key, rows in (("context_field", "selector_key", execution_context_field_options()),
            ("selected_output", "output_key", execution_text_output_options() if text_outputs is None else text_outputs))}
    from studio.services.execution_shot_field_mapping import recipe_shot_field_nodes, shot_field_schema_key
    fields = {}
    for node in recipe_shot_field_nodes(stages):
        key = shot_field_schema_key(node)
        if key not in fields:
            fields[key] = source_schema_sha256(node, "shot_field")
    if fields:
        result["shot_field"] = fields
    return result


def validate_structure_source_schemas(value):
    if (not isinstance(value, dict) or not {"context_field", "selected_output"} <= set(value)
            or set(value) - {"context_field", "selected_output", "shot_field"}):
        _fail("structure_source_schemas", "缺少冻结的来源结构目录；不以当前目录补齐旧计划")
    for group in value.values():
        if not isinstance(group, dict) or any(type(key) is not str or not key or type(digest) is not str
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                for key, digest in group.items()):
            _fail("structure_source_schemas", "冻结来源结构目录格式无效")


def structure_children(node):
    if node["kind"] == "object":
        return list(node["fields"].items())
    if node["kind"] == "array":
        return [(str(i), row) for i, row in enumerate(node["items"])]
    if node["kind"] == "unit_map":
        return [("value", node["value"])]
    if node["kind"] == "each":
        return [("source", node["source"]), ("item", node["item"])]
    return []


def structure_leaves(node):
    children = structure_children(node)
    if children:
        return [leaf for _, child in children for leaf in structure_leaves(child)]
    return [node]


def validate_composite_node(node, path):
    """Shared shapes for value composition, independent of business source readers."""
    fields = {"object": {"fields"}, "array": {"items"}, "each": {"source", "item"}, "literal": {"value"}}
    if not isinstance(node, dict) or node.get("kind") not in fields:
        return False
    kind = node["kind"]
    if set(node) != {"kind", *fields[kind]}:
        _fail(path, "结构映射字段不完整或包含未声明字段")
    if kind == "object" and (not isinstance(node["fields"], dict) or any(not isinstance(key, str) or not key for key in node["fields"])):
        _fail(path, "对象字段必须使用明确的非空字段名")
    if kind == "array" and not isinstance(node["items"], list):
        _fail(path, "列表必须按明确顺序配置")
    if kind == "literal":
        canonical_json_bytes(node["value"])
    return True


def validate_structure(node, *, leaf_validator, path, item_schema=None, text_outputs=None, allow_material_difference=False):
    if not isinstance(node, dict) or not isinstance(node.get("kind"), str) or node["kind"] not in NODE_LABELS:
        _fail(path, "请选择已登记的结构或字段来源")
    kind = node["kind"]
    validate_composite_node(node, path)
    if kind == "material_difference":
        if not allow_material_difference:
            _fail(path, "当前入口不是配方变体差异映射")
        leaf_validator(node, {"kind": "text"}, path)
        return
    fields = {"object": {"fields"}, "array": {"items"}, "unit_map": {"value"}, "each": {"source", "item"},
        "literal": {"value"}, "unit_key": set(), "reference_subjects": set(), "item_field": {"path"}}
    if kind in {"context_field", "selected_output", "shot_field"}:
        leaf_validator({key: value for key, value in node.items() if key != "path"}, structure_source_option(node, path, text_outputs), path)
        structure_value_schema(node, path, text_outputs=text_outputs)
        return
    allowed = {"kind", *fields[kind]}
    if kind == "item_field" and "if_missing" in node:
        allowed.add("if_missing")
    if set(node) != allowed:
        _fail(path, "结构映射字段不完整或包含未声明字段")
    if kind == "item_field":
        schema = structure_value_schema(node, path, item_schema, text_outputs)
        if "if_missing" in node:
            if not node["path"] or type(node["path"][-1]) is not str:
                _fail(path, "缺失值只能配置在条目路径的末级对象属性，不替代整条或列表下标")
            validate_structure_value(node["if_missing"], schema, path + ".if_missing")
            json_contract(node["if_missing"], max_bytes=execution_configuration_max_bytes())
    if kind == "each":
        source = node["source"]
        if not isinstance(source, dict) or source.get("kind") not in {"context_field", "selected_output", "shot_field", "item_field"}:
            _fail(path, "逐条映射只能读取受控目录的已审核列表")
        validate_structure(source, leaf_validator=leaf_validator, path=path + ".source", item_schema=item_schema, text_outputs=text_outputs, allow_material_difference=allow_material_difference)
        schema = nonnull_schema(structure_value_schema(source, path + ".source", item_schema, text_outputs))
        if schema.get("type") != "array" or not isinstance(schema.get("items"), dict):
            _fail(path, "来源目录未声明可映射的列表结构")
        validate_structure(node["item"], leaf_validator=leaf_validator, path=path + ".item", item_schema=schema["items"], text_outputs=text_outputs, allow_material_difference=allow_material_difference)
        return
    for key, child in structure_children(node):
        validate_structure(child, leaf_validator=leaf_validator, path=path + "." + key, item_schema=item_schema, text_outputs=text_outputs, allow_material_difference=allow_material_difference)


@shot_field_mapping_cache()
def bind_structure(node, bind_leaf, *, source_schemas, text_outputs=None):
    """Replace only authorized source references, not their values."""
    result = deepcopy(node)
    kind = node["kind"]
    if kind in {"context_field", "selected_output", "shot_field"}:
        from studio.services.execution_text_output_catalog import text_output_schema_key
        if kind == "shot_field":
            from studio.services.execution_shot_field_mapping import shot_field_schema_key
            identifier = shot_field_schema_key(node)
        else:
            identifier = (node.get("selector_key") if kind == "context_field" else
                text_output_schema_key(structure_source_option(node, "source", text_outputs)))
        digest = source_schemas.get(kind, {}).get(identifier)
        if digest != source_schema_sha256(node, "source", text_outputs):
            _fail("source", "来源结构目录与冻结运行计划不一致，请创建使用当前合同的新运行")
        bound = bind_leaf({key: value for key, value in node.items() if key != "path"})
        return {**bound, "source_schema_sha256": digest,
            **({"path": deepcopy(node["path"])} if "path" in node else {})}
    if kind == "object":
        result["fields"] = {key: bind_structure(child, bind_leaf, source_schemas=source_schemas, text_outputs=text_outputs) for key, child in node["fields"].items()}
    elif kind == "array":
        result["items"] = [bind_structure(child, bind_leaf, source_schemas=source_schemas, text_outputs=text_outputs) for child in node["items"]]
    elif kind == "unit_map":
        result["value"] = bind_structure(node["value"], bind_leaf, source_schemas=source_schemas, text_outputs=text_outputs)
    elif kind == "each":
        result["source"] = bind_structure(node["source"], bind_leaf, source_schemas=source_schemas, text_outputs=text_outputs)
        result["item"] = bind_structure(node["item"], bind_leaf, source_schemas=source_schemas, text_outputs=text_outputs)
    return result


def evaluate_structure(node, *, read_leaf, unit_key, reference_subjects, path, item=None,
        read_value=None, item_source_path=None, consume=None):
    """Return values and evidence; only an authored if_missing converts absence."""
    kind = node["kind"]
    if kind == "material_difference":
        _fail(path, "配方变体差异必须先由原素材需求绑定，不能直接求值")
    if consume is not None:
        consume({"path": path, "kind": kind})
    if kind in {"path", "lookup"}:
        if read_value is None:
            _fail(path, "当前调用未提供结构路径读取器")
        return read_value(node, item, item_source_path, path)
    if kind in {"context_field", "selected_output", "shot_field"}:
        value, origin = read_leaf({key: value for key, value in node.items() if key not in {"path", "source_schema_sha256"}})
        source_hash = json_contract(value, max_bytes=execution_configuration_max_bytes()).sha256
        value, location, _ = read_structure_path(value, node.get("path", []), "source", path, consume)
        return value, [{**origin, "path": path, "source_path": location,
            "source_schema_sha256": node.get("source_schema_sha256"),
            "source_value_sha256": source_hash, "value_path": deepcopy(node.get("path", [])),
            "value_sha256": json_contract(value, max_bytes=execution_configuration_max_bytes()).sha256}]
    if kind == "literal":
        if consume is not None:
            consume(node["value"])
        return deepcopy(node["value"]), [{"path": path, "kind": kind}]
    if kind in {"unit_key", "reference_subjects", "unit_map"} and not unit_key:
        _fail(path, "需要原制作准备明确的真实单元，不能自行填写或从名称推断")
    if kind in {"unit_key", "reference_subjects"}:
        value = unit_key if kind == "unit_key" else reference_subjects
        return deepcopy(value), [{"path": path, "kind": kind, "unit_key": unit_key}]
    if kind == "item_field":
        value, location, present = read_structure_path(item, node["path"], item_source_path or "item", path, consume,
            if_missing=node.get("if_missing", MISSING))
        return value, [{"path": path, "kind": kind, "value_path": deepcopy(node["path"]), "source_path": location,
            **({"resolution": "explicit_if_missing", "source_field_present": False, "value": deepcopy(value)} if not present else {})}]
    kwargs = dict(read_leaf=read_leaf, unit_key=unit_key, reference_subjects=reference_subjects,
        read_value=read_value, consume=consume)
    if kind == "each":
        values, evidence = evaluate_structure(node["source"], path=path + ".source", item=item,
            item_source_path=item_source_path, **kwargs)
        if not isinstance(values, list):
            _fail(path, "实际已审核来源不是列表")
        result = []
        source_path = evidence[-1].get("source_path") if evidence else None
        for index, row in enumerate(values):
            value, origin = evaluate_structure(node["item"], path=f"{path}[{index}]", item=row,
                item_source_path=f"{source_path}[{index}]" if source_path is not None else None, **kwargs)
            result.append(value); evidence.extend(origin)
        return result, evidence
    result, evidence = ([] if kind == "array" else {}), []
    for key, child in structure_children(node):
        target_key = unit_key if kind == "unit_map" else key
        from studio.services.data_structures import _value_path
        child_path = _value_path(path, int(key) if kind == "array" else target_key)
        value, origin = evaluate_structure(child, path=child_path, item=item, item_source_path=item_source_path, **kwargs)
        if kind == "array":
            result.append(value)
        else:
            result[target_key] = value
        evidence.extend(origin)
    return result, evidence


def structure_target_schemas(definition, spec):
    schemas = []
    for mapping in spec["targets"]:
        group = "profile_inputs" if mapping["target_kind"] == "profile_input" else "input_targets"
        target = definition.value["interface"].get(group, {}).get(mapping["target_key"], {})
        if isinstance(target.get("value_schema"), dict):
            schemas.append(target["value_schema"])
        elif target.get("value_domain") in {"string", "boolean", "number", "string_list", "unit_context"}:
            domain = target["value_domain"]
            schema = ({"type": "array", "items": {"type": "string"}} if domain == "string_list" else
                {"type": "object", "additionalProperties": {}} if domain == "unit_context" else {"type": domain})
            if target.get("enum") is not None:
                schema["enum"] = target["enum"]
            schemas.append(schema)
    return schemas


def validate_template_schema(node, schema, path, *, validate_value=None, item_schema=None, text_outputs=None):
    """Validate declared shape now; dynamic facts are checked after resolution."""
    validate_value = validate_value or validate_structure_value
    kind, expected = node["kind"], schema.get("type")
    produced = {"object": "object", "unit_map": "object", "array": "array", "each": "array",
        "unit_key": "string", "reference_subjects": "array", "material_difference": "string"}.get(kind)
    if kind == "literal":
        validate_value(node["value"], schema, path)
        return
    if "anyOf" in schema:
        validate_template_schema(node, {key: value for key, value in schema.items() if key != "anyOf"},
            path, validate_value=validate_value, item_schema=item_schema, text_outputs=text_outputs)
        failures = []
        for branch in schema["anyOf"]:
            try:
                validate_template_schema(node, branch, path, validate_value=validate_value, item_schema=item_schema, text_outputs=text_outputs)
                return
            except ValidationError as exc:
                failures.append(exc)
        raise failures[0]
    if kind == "item_field" and "if_missing" in node:
        validate_value(node["if_missing"], schema, path + ".if_missing")
    if kind in {"context_field", "selected_output", "shot_field", "item_field"}:
        produced = nonnull_schema(structure_value_schema(node, path, item_schema, text_outputs)).get("type")
    if produced and expected and produced != expected and not (produced == "integer" and expected == "number"):
        _fail(path, "所选来源或结构与目标字段类型不匹配")
    if kind == "object":
        fields, properties = node["fields"], schema.get("properties", {})
        if set(schema.get("required", [])) - set(fields):
            _fail(path, "目标合同必需字段尚未配置")
        if schema.get("additionalProperties") is False and set(fields) - set(properties):
            _fail(path, "目标合同没有声明这些字段")
        if len(fields) < schema.get("minProperties", 0) or ("maxProperties" in schema and len(fields) > schema["maxProperties"]):
            _fail(path, "配置的对象字段数量不满足目标合同")
        for key, child in fields.items():
            from studio.services.data_structures import _value_path
            extra = schema.get("additionalProperties")
            validate_template_schema(child, properties.get(key, extra if isinstance(extra, dict) else {}),
                _value_path(path, key), validate_value=validate_value, item_schema=item_schema, text_outputs=text_outputs)
    elif kind == "array":
        if len(node["items"]) < schema.get("minItems", 0) or ("maxItems" in schema and len(node["items"]) > schema["maxItems"]):
            _fail(path, "配置的列表数量不满足目标合同")
        for index, child in enumerate(node["items"]):
            validate_template_schema(child, schema.get("items", {}), f"{path}[{index}]", validate_value=validate_value, item_schema=item_schema, text_outputs=text_outputs)
    elif kind == "each":
        source_schema = nonnull_schema(structure_value_schema(node["source"], path + ".source", item_schema, text_outputs))
        validate_template_schema(node["item"], schema.get("items", {}), path + ".item", validate_value=validate_value,
            item_schema=source_schema["items"], text_outputs=text_outputs)
    elif kind == "unit_map":
        if schema.get("additionalProperties") is False:
            _fail(path, "封闭对象不允许以动态单元标识作为字段")
        validate_template_schema(node["value"], schema.get("additionalProperties") if isinstance(schema.get("additionalProperties"), dict) else {}, path + ".value", validate_value=validate_value, item_schema=item_schema, text_outputs=text_outputs)


def validate_structure_value(value, schema, path):
    # Reuse the platform's closed JSON contract validator. Open JSON contexts
    # remain bounded JSON; no second validation dialect or model schema.
    from studio.services.configuration import _validate_closed_configuration_contract
    prepared = deepcopy(schema)
    if isinstance(value, dict):
        prepared.setdefault("properties", {})
        extra = prepared.get("additionalProperties")
        if isinstance(extra, dict):
            prepared["properties"].update({key: extra for key in value if key not in prepared["properties"]})
    # Its recursive checks are complemented here for the reference schema's
    # const/uniqueItems, and open context objects get the same handling below.
    shallow = {key: val for key, val in prepared.items() if key not in {"items", "properties", "enum"}}
    if isinstance(value, dict):
        shallow["properties"] = {key: {} for key in prepared["properties"]}
        # Avoid re-entering open objects with an empty schema.
        scalar_view = {key: None for key in value}
        _validate_closed_configuration_contract(scalar_view, shallow, definition_key=path)
    else:
        _validate_closed_configuration_contract(value, shallow, definition_key=path)
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        _fail(path, "值与合同固定值不一致")
    if "enum" in schema and value not in schema["enum"]:
        _fail(path, "值不在目标合同枚举中")
    if isinstance(value, list):
        if schema.get("uniqueItems") and len({json_contract(row, max_bytes=execution_configuration_max_bytes()).sha256 for row in value}) != len(value):
            _fail(path, "列表不允许重复项")
        for index, row in enumerate(value):
            validate_structure_value(row, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, dict):
        for key, row in value.items():
            validate_structure_value(row, prepared["properties"].get(key, {}), path + "." + key)
