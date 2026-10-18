"""Explicit recipe material sources, resolved into the existing input pipeline.

Only a reviewed variant and its exact in-run primary choice can supply these
values. The recipe contains selectors, never another copy of setting text or
an Asset/version choice. This module neither dispatches nor registers media.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.media_appearance_input_catalog import media_appearance_input_options


def _fail(message):
    raise ValidationError({"input_sources": message})


def difference_locations(node, path=()):
    """Only explicit object/list positions can identify one Profile field."""
    if not isinstance(node, dict):
        return []  # The shared structure validator reports malformed nodes.
    kind = node.get("kind")
    if kind == "material_difference":
        if set(node) != {"kind"}:
            _fail("变体差异只引用当前已审核设定，不接受另填正文或路径")
        return [path]
    if kind == "object":
        return [location for key, child in node.get("fields", {}).items()
            for location in difference_locations(child, (*path, key))]
    if kind == "array":
        return [location for index, child in enumerate(node.get("items", []))
            for location in difference_locations(child, (*path, index))]
    if kind in {"each", "unit_map"}:
        from studio.services.execution_structure_mapping import structure_leaves
        if any(row.get("kind") == "material_difference" for row in structure_leaves(node)):
            _fail("变体差异必须位于明确的对象字段或列表位置，不接受动态重复位置")
    return []


def validate_material_primary(source, spec):
    if (set(source) != {"kind", "item_key", "purpose"}
            or not isinstance(source["item_key"], str) or not source["item_key"].strip()
            or len(source["item_key"]) > 191):
        _fail("本轮主图须明确原图片输入的使用标识及用途，不填写素材ID")
    policy = spec.get("media_source_policy")
    if (spec["kind"] != "image" or not spec["min_items"] <= 1 <= spec["max_items"]
            or not policy or "asset_library" not in policy["allowed_sources"]
            or (policy["default_source"] != "asset_library" and not policy["allow_override"])
            or "request" not in spec["semantic_contract"]["allowed_scope_kinds"]
            or spec["semantic_contract"]["paired_with_input_key"] is not None):
        _fail("本轮主图需要允许素材库来源的单图、本次请求图片输入")
    purpose = source["purpose"]
    allowed = spec["semantic_contract"]["allowed_purposes"]
    if (allowed and (type(purpose) is not str or purpose not in allowed)) or (not allowed and purpose is not None):
        _fail("主图用途必须从精确实例的允许用途选择；未声明用途时须明确为null")


def _reference_difference_matches(target, field, template, location):
    """Match the existing reference-unit protocol, not a caller-chosen path.

    A recipe has no unit identity yet. Only the original unit_key node can
    bind the selected field to the exact material unit during preparation.
    """
    from studio.services.structure_paths import nonnull_schema, path_schema
    if (field.get("usage") != "reference_units" or not field.get("source_path")
            or len(location) < 4 or location[0] != "ordered_units"
            or type(location[1]) is not int or location[2] != "structured_input"
            or list(location[3:]) != field["source_path"]):
        return False
    units = template.get("fields", {}).get("ordered_units", {})
    if units.get("kind") != "array" or not 0 <= location[1] < len(units.get("items", [])):
        return False
    unit = units["items"][location[1]]
    if (unit.get("kind") != "object"
            or unit.get("fields", {}).get("unit_key") != {"kind": "unit_key"}):
        _fail("参考单元差异须使用当前制作单元标识，不能填写或猜测单元身份")
    return nonnull_schema(path_schema(target["value_schema"], list(location), "material_difference")).get("type") == "string"


def recipe_material_binding(definition, sources, *, require_complete=True, unit_key=None):
    """Derive selectors from explicitly marked inputs and exact Profile paths."""
    primary, differences = [], []
    options = media_appearance_input_options(definition.value)
    for key, source in sources.items():
        if source.get("kind") == "material_primary":
            primary.append({"input_key": key, "item_key": source["item_key"]})
            if not any(row["input_key"] == key for row in options["primary_inputs"]):
                _fail("本轮主图输入未映射到实例的主图片端口")
        locations = difference_locations(source.get("template")) if source.get("kind") == "structured" else []
        if source.get("kind") == "material_difference":
            if set(source) != {"kind"}:
                _fail("变体差异只引用当前已审核设定，不接受另填正文")
            selector = {"kind": "logical_input", "key": key}
            if selector not in options["difference_sources"]:
                _fail("变体差异须映射到策略正文输入，不能只作辅助上下文")
            differences.append(selector)
        for location in locations:
            spec = next(row for row in definition.value["configuration"]["inputs"] if row["key"] == key)
            matches = []
            for mapping in spec["targets"]:
                if mapping["target_kind"] != "profile_input":
                    continue
                target = definition.value["interface"]["profile_inputs"][mapping["target_key"]]
                for field in target["fields"]:
                    selector = {"kind": "structured_field", "key": key, "field_key": field["field_key"]}
                    if selector in options["difference_sources"] and field.get("source_path") == list(location):
                        matches.append(selector)
                    selector = {"kind": "reference_field", "key": key, "field_key": field["field_key"]}
                    if selector in options["difference_sources"] and _reference_difference_matches(
                            target, field, source["template"], location):
                        matches.append({**selector, **({"unit_key": unit_key} if unit_key is not None else {})})
            if len(matches) != 1:
                _fail("差异标记位置须对应唯一的已发布策略直通文字字段或当前单元贡献字段，不能猜测字段或使用默认值")
            differences.extend(matches)
    if not primary and not differences:
        return None
    if len(primary) > 1 or len(differences) > 1 or (require_complete and (len(primary) != 1 or len(differences) != 1)):
        _fail("变体路线须明确且仅配置一处本轮主图及一处当前设定差异")
    return {"primary_input": primary[0] if primary else None,
        "difference_input": differences[0] if differences else None}


def has_material_sources(sources):
    return any(row.get("kind") in {"material_primary", "material_difference"}
        or row.get("kind") == "structured" and difference_locations(row.get("template"))
        for row in sources.values())


def bind_material_sources(sources, *, version, difference):
    """Substitute authored markers only. Never replace unrelated literals."""
    def bind(node):
        if node["kind"] == "material_difference":
            return {"kind": "literal", "value": difference}
        result = deepcopy(node)
        if node["kind"] == "object":
            result["fields"] = {key: bind(child) for key, child in node["fields"].items()}
        elif node["kind"] == "array":
            result["items"] = [bind(child) for child in node["items"]]
        return result
    result = deepcopy(sources)
    for key, source in sources.items():
        if source["kind"] == "material_primary":
            result[key] = {"kind": "media_selection", "selection": {"source": "asset_library", "items": [{
                "item_key": source["item_key"], "asset_id": str(version.asset_id), "purpose": source["purpose"]}]}}
        elif source["kind"] == "material_difference":
            result[key] = bind(source)
        elif source["kind"] == "structured":
            result[key]["template"] = bind(source["template"])
    return result


def recipe_material_primary_reference(dependency):
    """Reference the actual choice type, without manufacturing generation facts."""
    version = {"version_id": str(dependency["version"].pk)}
    if "fulfillment" in dependency:
        if any(key in dependency for key in ("decision", "artifact", "preparation")):
            _fail("主图来源不能同时声明素材选择和生成记录")
        return {**version, "fulfillment_id": str(dependency["fulfillment"].pk)}
    return {**version, "decision_id": str(dependency["decision"].pk),
        "artifact_id": str(dependency["artifact"].pk), "preparation_id": str(dependency["preparation"].pk)}


def read_recipe_material_binding(preparation, *, detail=None):
    """Verify a variant's frozen dependency receipt, without reselecting it."""
    from uuid import UUID
    from studio.services.execution_preparations import read_execution_preparation_detail
    from studio.services.media_generation_targets import read_media_generation_target
    detail = read_execution_preparation_detail(preparation, detail=detail)
    appearance = read_media_generation_target(preparation, detail=detail).value["appearance"]
    receipt = detail.resolved_inputs.get("recipe_material_binding")
    try:
        valid = (appearance["role"] == "variant" and set(receipt) == {"primary", "difference"}
            and set(receipt["primary"]) in ({"decision_id", "artifact_id", "preparation_id", "version_id"},
                {"fulfillment_id", "version_id"})
            and all(str(UUID(value)) == value for value in receipt["primary"].values())
            and receipt["primary"]["version_id"] == appearance["primary"]["version_id"]
            and receipt["difference"] == {"setting_revision_id": appearance["setting"]["setting_revision_id"],
                "value_sha256": appearance["difference"]["value_sha256"]})
    except (TypeError, KeyError, AttributeError, ValueError):
        valid = False
    if not valid:
        _fail("变体缺少本轮确切主图及设定差异绑定，不能派发或登记手填来源")
    return receipt


def require_current_recipe_material_binding(preparation, *, stage):
    from studio.services.production_recipe_material_registration import read_registered_recipe_primary_dependency
    from rest_framework.exceptions import ValidationError as APIValidationError
    receipt = read_recipe_material_binding(preparation)
    try:
        dependency = read_registered_recipe_primary_dependency(stage=stage, unit_key=preparation.unit_key)
    except APIValidationError as exc:
        _fail(str(exc.detail))
    if dependency is None or receipt["primary"] != recipe_material_primary_reference(dependency):
        _fail("原变体准备的主图选择已改变或未登记，不能用新选择提交旧请求")
