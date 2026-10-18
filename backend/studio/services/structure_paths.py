"""Declared JSON paths shared by previews and actual input composition."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.data_structures import _value_path


MISSING = object()


def nonnull_schema(schema):
    if "anyOf" in schema:
        branches = [row for row in schema["anyOf"] if row.get("type") != "null"]
        if len(branches) == 1:
            return {**{key: value for key, value in schema.items() if key != "anyOf"}, **branches[0]}
    return schema


def path_schema(schema, tokens, location):
    if not isinstance(tokens, list):
        raise ValidationError({location: "路径必须为对象属性名/非负列表下标组成的数组"})
    for index, token in enumerate(tokens):
        current = nonnull_schema(schema)
        if type(token) is str and current.get("type") == "object" and token in current.get("properties", {}):
            schema = current["properties"][token]
        elif type(token) is int and token >= 0 and current.get("type") == "array" and isinstance(current.get("items"), dict):
            schema = current["items"]
        else:
            raise ValidationError({f"{location}[{index}]": "路径不在所选结构目录中；列表逐项请配置each，不使用表达式/隐式展开"})
    return schema


def read_structure_path(value, tokens, location, target_path, consume=None, *, if_missing=MISSING):
    """Only a missing final object property can use the explicit item fallback.

    Missing parents, null, wrong types and absent array offsets remain errors.
    Paths are validated against the source schema before this reader is called.
    """
    for index, token in enumerate(tokens):
        next_path = _value_path(location, token)
        if type(token) is str and isinstance(value, dict) and token in value:
            value = value[token]
        elif type(token) is int and isinstance(value, list) and 0 <= token < len(value):
            value = value[token]
        elif (type(token) is str and isinstance(value, dict) and token not in value
                and index == len(tokens) - 1 and if_missing is not MISSING):
            if consume is not None:
                consume(if_missing)
            return deepcopy(if_missing), next_path, False
        else:
            raise ValidationError({next_path: f"实际输入缺少所选字段路径或类型不符；目标：{target_path}"})
        location = next_path
    if consume is not None:
        consume(value)
    return deepcopy(value), location, True
