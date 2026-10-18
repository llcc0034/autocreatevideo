"""Independent shot fields are leaves of the existing M1 mapping language."""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ValidationError

from studio.models import DataStructureVersion
from studio.services.data_structures import data_structure_snapshot, validate_data_structure_snapshot
from studio.services.json_contracts import canonical_json_bytes
from studio.services.production_shot_field_sources import validate_shot_field_selection
from studio.services.structure_paths import nonnull_schema


_schemas = ContextVar("shot_field_mapping_schemas", default=None)


@contextmanager
def shot_field_mapping_cache():
    """One call's exact configuration reads only; never cache shot values."""
    if _schemas.get() is not None:
        yield
        return
    token = _schemas.set({})
    try:
        yield
    finally:
        _schemas.reset(token)


def shot_field_schema_key(node):
    validate_shot_field_selection(node.get("field"))
    return canonical_json_bytes(node["field"]).decode("utf-8")


def shot_field_source_option(node):
    """Derive type from an exact existing M1 version, never client JSON Schema."""
    selection = node.get("field")
    validate_shot_field_selection(selection)
    cache = _schemas.get()
    key = selection["structure_version_id"]
    if cache is None or key not in cache:
        structure = DataStructureVersion.objects.filter(pk=key).first()
        if structure is None:
            raise ValidationError("独立字段的确切结构版本不存在")
        schema = validate_data_structure_snapshot(data_structure_snapshot(structure))
        if cache is not None:
            cache[key] = schema
    else:
        schema = cache[key]

    def find(value):
        base = nonnull_schema(value)
        if base.get("x-node-key") == selection["node_key"]:
            return [value]
        children = list(base.get("properties", {}).values())
        if isinstance(base.get("items"), dict):
            children.append(base["items"])
        return [match for child in children for match in find(child)]

    matches = find(schema)
    if (len(matches) != 1 or nonnull_schema(matches[0]).get("x-field-definition", {}).get(
            "field_definition_version_id") != selection["field_definition_version_id"]):
        raise ValidationError("结构节点与确切字段定义版本不匹配，不能用同名字段替代")
    value_schema = matches[0]
    return {"kind": "text" if nonnull_schema(value_schema).get("type") == "string" else "json",
        "scope_kind": "shot", "value_schema": deepcopy(value_schema)}


def recipe_shot_field_nodes(stages):
    from studio.services.execution_structure_mapping import structure_leaves
    for stage in stages:
        for route in stage.get("routing", {}).get("routes", []):
            for source in route.get("input_sources", {}).values():
                if source.get("kind") == "structured":
                    yield from (node for node in structure_leaves(source["template"]) if node["kind"] == "shot_field")
