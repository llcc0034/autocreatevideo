"""Shared source-choice contract; owners retain their own versioned configuration.

This module does not save configuration, select a latest artifact, upload files,
or create execution jobs. Media bytes are read by the existing execution readers.
Caller-provided run context must already be authorized by the owning service.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.json_contracts import canonical_json_bytes


MEDIA_KINDS = frozenset(("image", "video", "audio"))
SOURCE_OPTIONS = (
    {"value": "literal", "label": "明确固定值", "kinds": ["text", "json"],
     "reader": "structure_mapping", "available": True, "requires": [],
     "description": "按所选结构校验固定值，不从示例或其他来源补齐。"},
    {"value": "upstream_field", "label": "上游结构字段", "kinds": ["text", "json"],
     "reader": "structure_mapping", "available": True, "requires": ["source_structure", "source_value"],
     "description": "从确切上游结构目录选择路径；复用结构映射，不猜测名称。"},
    {"value": "asset_library", "label": "素材库已有素材", "kinds": ["image", "video", "audio"],
     "reader": "asset", "available": True, "requires": ["owner"],
     "description": "明确选择正式Asset；校验所属范围、类型和文件，不自动换图。"},
    {"value": "asset_upload", "label": "本地上传后入库", "kinds": ["image", "video", "audio"],
     "reader": "asset", "available": True, "requires": ["owner", "completed_asset_upload"],
     "description": "先通过素材上传API创建Asset，再传入其ID；不接受临时路径。"},
    {"value": "selected_output", "label": "上游已选择产物", "kinds": ["image", "video", "audio"],
     "reader": "selected_output", "available": True, "requires": ["owner", "production_run", "upstream_stages"],
     "description": "指定选择决定及产物ID；只读取已授权的本次上游，不自动选最新版本。"},
    {"value": "bible_reference", "label": "Bible主图／变体素材版本", "kinds": ["image", "video", "audio"],
     "reader": None, "available": False, "requires": ["asset_reference_version"],
     "description": "尚未接入新素材版本关系（M9/M5）；不能用名称或当前主图代替确切版本。"},
)
_SOURCES = {row["value"]: row for row in SOURCE_OPTIONS}
_UNSET = object()


def input_source_options():
    """Catalog describes readers, not availability in every business page."""
    return {"schema_version": 1, "sources": deepcopy(list(SOURCE_OPTIONS)),
            "orders": [{"value": "provided", "label": "按明确提供的列表顺序"}],
            "configuration_owner": "recipe_instance_or_profile",
            "description": "目录不保存映射；实际入口需提供所列上下文，未接入入口不能执行。"}


def _fail(path, message):
    raise ValidationError({path: message})


def _strings(value, path, *, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        _fail(path, "必须是无重复的明确字符串列表" + ("且不能为空" if nonempty else ""))


def validate_media_source_policy(policy, *, path="source_policy"):
    """Validate explicit use-site settings; no field-global defaults or fallbacks."""
    keys = {"schema_version", "media_kind", "allowed_sources", "default_source", "allow_override",
            "min_items", "max_items", "allowed_purposes", "order"}
    if not isinstance(policy, dict) or set(policy) != keys:
        _fail(path, "须明确提供完整媒体来源配置，不能补猜缺省策略")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        _fail(path + ".schema_version", "仅支持来源策略版本1")
    if not isinstance(policy["media_kind"], str) or policy["media_kind"] not in MEDIA_KINDS:
        _fail(path + ".media_kind", "须选择图片、视频或音频类型")
    _strings(policy["allowed_sources"], path + ".allowed_sources", nonempty=True)
    for source in policy["allowed_sources"]:
        entry = _SOURCES.get(source)
        if entry is None or policy["media_kind"] not in entry["kinds"]:
            _fail(path + ".allowed_sources", f"{source}不是该媒体类型的来源")
        if not entry["available"]:
            _fail(path + ".allowed_sources", entry["description"])
    if not isinstance(policy["default_source"], str) or policy["default_source"] not in policy["allowed_sources"]:
        _fail(path + ".default_source", "默认来源必须属于允许来源；只默认来源方式，不自动选择素材")
    if type(policy["allow_override"]) is not bool:
        _fail(path + ".allow_override", "本次是否可改选必须明确为布尔值")
    minimum, maximum = policy["min_items"], policy["max_items"]
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum:
        _fail(path + ".min_items", "数量上下限须为非负整数且下限不大于上限")
    _strings(policy["allowed_purposes"], path + ".allowed_purposes")
    if policy["order"] != "provided":
        _fail(path + ".order", "仅按明确提供的列表顺序，不按名称排序或去重")
    canonical_json_bytes(policy)
    return deepcopy(policy)


def _uuid(value, path):
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except ValueError as exc:
        raise ValidationError({path: "须选择确切正式对象ID"}) from exc


def select_media_source(policy, configured_selection, *, override=_UNSET):
    """Choose once, before I/O. An override replaces the whole ordered selection.

    A configured default is only a source mode, never a hidden Asset choice.
    Explicit empty selections remain empty and still undergo cardinality checks.
    """
    validate_media_source_policy(policy)
    overridden = override is not _UNSET
    if overridden and not policy["allow_override"]:
        _fail("override", "此输入不允许本次改选，请修改所属配置后再创建准备")
    selected = override if overridden else configured_selection
    if not isinstance(selected, dict) or set(selected) != {"source", "items"}:
        _fail("selection", "须明确提供来源方式及有序items；缺失不自动取默认图")
    source = selected["source"]
    if not isinstance(source, str) or source not in policy["allowed_sources"]:
        _fail("selection.source", "所选来源不在当前允许范围内")
    if not overridden and source != policy["default_source"]:
        _fail("selection.source", "配置的选择须使用默认来源；其他来源需通过明确的本次改选")
    items = selected["items"]
    if not isinstance(items, list) or not policy["min_items"] <= len(items) <= policy["max_items"]:
        _fail("selection.items", f"素材数量须为{policy['min_items']}–{policy['max_items']}；不会补图或截断")
    identities = set()
    for index, item in enumerate(items):
        path = f"selection.items[{index}]"
        identity_fields = {"asset_id"} if source in {"asset_library", "asset_upload"} else {"decision_id", "artifact_id"}
        if not isinstance(item, dict) or set(item) != {"item_key", "purpose", *identity_fields}:
            _fail(path, "须提供每次使用的item_key、用途及该来源的确切引用，不能混入其他来源")
        key = item["item_key"]
        if not isinstance(key, str) or not key.strip() or key in identities:
            _fail(path + ".item_key", "每次素材使用须有不同的非空标识")
        identities.add(key)
        purposes = policy["allowed_purposes"]
        if (purposes and (not isinstance(item["purpose"], str) or item["purpose"] not in purposes)) or (not purposes and item["purpose"] is not None):
            _fail(path + ".purpose", "用途须来自该输入的允许目录；未声明用途时须明确为null")
        for field in identity_fields:
            _uuid(item[field], path + "." + field)
    canonical_json_bytes(selected)
    return deepcopy(selected), overridden


def resolve_media_source_selection(policy, configured_selection, *, owner, max_media_bytes, max_media_items,
                                   override=_UNSET, production_run_id=None, eligible_stage_ids=()):
    """Read exact selections through existing readers. Never dispatch or adopt.

    Library and completed uploads both resolve an Asset. Uploaded sources must
    have the original upload writer's fact; this is not a new upload receipt.
    Model/Workflow port limits are still checked by the existing target adapter.
    """
    from studio.services.execution_media_sources import resolve_execution_asset_media
    from studio.services.execution_selected_outputs import resolve_selected_execution_media

    selection, overridden = select_media_source(policy, configured_selection, override=override)
    if not getattr(owner, "is_active", False) or not getattr(owner, "pk", None):
        _fail("selection", "须提供有效业务所有者")
    if type(max_media_bytes) is not int or max_media_bytes <= 0:
        _fail("selection", "须由调用服务提供有效媒体大小边界")
    if type(max_media_items) is not int or max_media_items < 0:
        _fail("selection", "须由调用服务提供有效媒体读取数量边界")
    if len(selection["items"]) > max_media_items:
        _fail("selection.items", f"本入口单次最多核对{max_media_items}项；请减少本次选择，不会截断")
    source = selection["source"]
    if source == "selected_output" and (production_run_id is None or not eligible_stage_ids):
        _fail("selection.source", "此入口未提供已授权运行和上游阶段，不能读取上游产物")
    resolved, verified = [], {}
    for position, item in enumerate(selection["items"]):
        try:
            if source in {"asset_library", "asset_upload"}:
                identity = ("asset", _uuid(item["asset_id"], "selection.asset_id"))
                if identity not in verified:
                    verified[identity] = resolve_execution_asset_media(owner=owner, asset_id=identity[1],
                        expected_kind=policy["media_kind"], max_media_bytes=max_media_bytes)
                media = verified[identity]
                if source == "asset_upload":
                    from studio.services.asset_provenance import is_uploaded_asset
                    if not is_uploaded_asset(media.asset):
                        _fail("selection", "此来源须先通过正式上传入库；当前Asset没有上传来源事实")
                asset_id, artifact_id = str(media.asset.pk), None
                origin = {"kind": "asset", "asset_id": asset_id}
            else:
                identity = ("selected_output", _uuid(item["decision_id"], "selection.decision_id"),
                            _uuid(item["artifact_id"], "selection.artifact_id"))
                if identity not in verified:
                    verified[identity] = resolve_selected_execution_media(owner=owner, decision_id=identity[1],
                        artifact_id=identity[2], production_run_id=production_run_id,
                        eligible_stage_ids=eligible_stage_ids, expected_kind=policy["media_kind"], max_media_bytes=max_media_bytes)
                media = verified[identity]
                asset_id = str(media.artifact.asset_id) if media.artifact.asset_id else None
                artifact_id, origin = str(media.artifact.pk), media.selection
        except ValidationError as exc:
            _fail(f"selection.items[{position}]", "；".join(exc.messages))
        resolved.append({"item_key": item["item_key"], "position": position, "purpose": item["purpose"],
                         "kind": policy["media_kind"], "asset_id": asset_id, "artifact_id": artifact_id,
                         "origin": deepcopy(origin), "media_facts": deepcopy(media.media_facts)})
    return {"schema_version": 1, "policy": deepcopy(policy), "source": source,
            "overridden": overridden, "items": resolved}
