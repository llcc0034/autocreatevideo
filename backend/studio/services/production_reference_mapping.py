"""Recipe rules for reviewed media: selection only, never semantic inference.

The review reader supplies exact ordered records. This module expands those
records to the existing execution input contract; it cannot approve, query a
latest Asset or extract audio. Optional creative descriptions remain review
facts; only this invocation's explicit mapping supplies a model purpose.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.execution_input_catalog import EXECUTION_SUBJECT_KINDS
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_selected_outputs import selected_output_item_key
from studio.services.json_contracts import json_contract


REFERENCE_SOURCE_OPTIONS = (
    ("images", "镜头用途图片", "image"),
    ("character_images", "人物参考图", "image"),
    ("scene_images", "场景参考图", "image"),
    ("prop_images", "道具参考图", "image"),
    ("first_frame", "首帧", "image"),
    ("last_frame", "尾帧", "image"),
    ("continuity_images", "连续性参考图", "image"),
    ("videos", "参考视频", "video"),
    ("paired_audio", "所选视频的配套音频", "audio"),
    ("audios", "独立音频", "audio"),
)


def reference_source_options():
    # This catalog is also shown before review; approval comes from the contract.
    return [{"value": key, "label": label, "kind": kind, "scope_kind": "shot",
        "stage_key": "storyboard_review", "paired": key == "paired_audio"}
        for key, label, kind in REFERENCE_SOURCE_OPTIONS]


def _error(path, message):
    raise ValidationError({path: message}, code="input_mapping_invalid")


def _identifier(value, path):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValidationError({path: "需要正式对象的规范UUID"}, code="input_mapping_invalid") from exc
    return value


def reviewed_reference_item_key(*, input_key, shot_id, reference_key):
    """One stable per-call identity for a reviewed use, not its selected Asset."""
    return "reviewed:" + json_contract({"input_key": input_key, "shot_id": shot_id,
        "reference_key": reference_key}, max_bytes=execution_configuration_max_bytes()).sha256


def reference_use_records(uses):
    """Project the shared exact-use resolver into the existing input mapper.

    The reviewed use key is the identity, not its asset or array position. The
    original review freezes selection/version/hash facts; execution rechecks
    this use identity and retains that evidence with the exact resolved Asset.
    The mapper never accepts a caller-authored provenance snapshot. A free-form
    purpose cannot imply a first/last frame, entity, or audio/video pairing.
    """
    records = []
    for use in uses:
        setting = use["setting"]
        records.append({"reference_key": "shot-use:" + use["use_key"],
            "source_key": {"image": "images", "video": "videos", "audio": "audios"}[use["media_kind"]],
            "kind": use["media_kind"], "source": {"kind": "asset", "asset_id": use["asset"]["asset_id"]},
            "purpose": use["purpose"], "subject_ref": None if setting is None else {
                "kind": setting["entity_kind"], "id": setting["entity_id"]}, "paired_reference_key": None})
    return records


def validate_reference_mapping(source, *, spec, prior_stages, path, require_complete=True):
    if spec["kind"] not in {"image", "video", "audio"}:
        _error(path, "参考映射只能用于媒体输入")
    if not isinstance(source, dict) or set(source) != {"kind", "stage_key", "scope", "groups"}:
        _error(path, "参考映射必须包含审核阶段、范围和有序来源组")
    if (not require_complete and source == {"kind": "reviewed_references", "stage_key": "",
            "scope": {"kind": ""}, "groups": []}):
        return  # Explicit unconfigured draft, never accepted by a preparation.
    if (source["kind"] != "reviewed_references" or source["stage_key"] != "storyboard_review"
            or source["stage_key"] not in prior_stages or source["scope"] != {"kind": "shot"}):
        _error(path, "只能读取本配方更早阶段已审核的当前镜头参考清单")
    if "shot" not in spec["semantic_contract"]["allowed_scope_kinds"]:
        _error(path, "实例未允许镜头范围的素材输入")
    choices = {row["value"]: row for row in reference_source_options()}
    groups = source["groups"]
    if not isinstance(groups, list) or require_complete and not groups or len(groups) > len(choices):
        _error(path, "请选择至少一个有序来源组")
    seen = set()
    for index, group in enumerate(groups):
        field = f"{path}.groups.{index}"
        if (not isinstance(group, dict) or not {"source_key", "purposes", "subject_kinds"} <= set(group)
                or set(group) - {"source_key", "purposes", "subject_kinds", "purpose"}):
            _error(field, "来源组只接受目录标识、创作描述筛选、主体类别筛选和接口要求的目标用途")
        key = group["source_key"]
        if not isinstance(key, str) or key not in choices or key in seen or choices[key]["kind"] != spec["kind"]:
            _error(field, "来源组重复、未登记或媒体类型不匹配")
        seen.add(key)
        parent = spec["semantic_contract"]["paired_with_input_key"]
        if choices[key]["paired"] != (parent is not None):
            _error(field, "视频配套音频必须进入声明了父输入的槽位；独立素材不能冒充配对输入")
        allowed_purposes = spec["semantic_contract"]["allowed_purposes"]
        if allowed_purposes:
            if (require_complete or "purpose" in group) and group.get("purpose") not in allowed_purposes:
                _error(field + ".purpose", "请选择本次调用合同允许的模型用途；不会从创作描述推断")
        elif "purpose" in group:
            _error(field + ".purpose", "此输入没有额外用途参数，不应配置模型用途")
        for name, allowed in (("purposes", None),
                ("subject_kinds", spec["semantic_contract"]["allowed_subject_kinds"])):
            values = group[name]
            if values is not None and (not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value or len(value) > 160
                        or allowed is not None and value not in allowed for value in values)
                    or len(set(values)) != len(values)):
                _error(field + "." + name, "筛选须为不重复的明确值；不限筛选为null，创作描述不受模型用途枚举限制")
    json_contract(source, max_bytes=execution_configuration_max_bytes())


def expand_reviewed_reference_sources(*, specs, sources, records, shot_id):
    """Preserve reviewed semantics and group order, then reconnect exact uses.

    Caller must verify review/currentness before supplying these records, then
    call resolve_execution_input_sources for original Asset/Artifact validation.
    """
    _identifier(shot_id, "shot_id")
    if not isinstance(sources, dict) or not isinstance(records, list):
        _error("references", "审核结果缺少明确的参考清单")
    json_contract(records, max_bytes=execution_configuration_max_bytes())
    by_key = {}
    options = {key: kind for key, _, kind in REFERENCE_SOURCE_OPTIONS}
    record_keys = {"reference_key", "source_key", "kind", "source", "purpose", "subject_ref", "paired_reference_key"}
    for record in records:
        if not isinstance(record, dict) or set(record) != record_keys:
            _error("references", "审核素材事实不完整，不能临时补写用途或配对")
        key = record["reference_key"]
        if not isinstance(key, str) or not key or len(key) > 191 or key in by_key:
            _error("references", "审核引用身份必须明确且唯一")
        if not isinstance(record["source_key"], str) or options.get(record["source_key"]) != record["kind"]:
            _error("references", "审核引用类别与媒体类型不一致")
        origin = record["source"]
        if not isinstance(origin, dict) or (origin.get("kind") == "asset" and set(origin) != {"kind", "asset_id"}
                or origin.get("kind") == "selected_output" and set(origin) != {"kind", "decision_id", "artifact_id"}
                or origin.get("kind") == "video_audio" and (set(origin) != {"kind"} or record["source_key"] != "paired_audio")
                or origin.get("kind") not in {"asset", "selected_output", "video_audio"}):
            _error("references", "参考必须来自正式素材或精确采用产物")
        for field in set(origin) - {"kind"}:
            _identifier(origin[field], "references." + field)
        if record["purpose"] is not None and not isinstance(record["purpose"], str):
            _error("references", "审核用途无效")
        subject = record["subject_ref"]
        if subject is not None and (not isinstance(subject, dict) or set(subject) != {"kind", "id"}
                or not isinstance(subject["kind"], str) or subject["kind"] not in EXECUTION_SUBJECT_KINDS
                or not isinstance(subject["id"], str)):
            _error("references", "审核主体身份无效")
        if subject is not None:
            _identifier(subject["id"], "references.subject_ref.id")
        by_key[key] = record
    for record in records:
        parent = record["paired_reference_key"]
        if record["source_key"] == "paired_audio":
            if not isinstance(parent, str) or parent not in by_key or by_key[parent]["kind"] != "video":
                _error("references", "配套音频必须明确指向本审核中的具体视频")
        elif parent is not None:
            _error("references", "非配套音频不能带有音画配对")
    result, used, pending = {}, {}, []
    for spec in specs:
        key = spec["key"]
        source = sources.get(key)
        if not isinstance(source, dict) or source.get("kind") != "reviewed_references":
            continue
        validate_reference_mapping(source, spec=spec, prior_stages=("storyboard_review",), path=f"input_sources.{key}")
        items = []
        for group in source["groups"]:
            # No purpose parameter exists for a plain slot: null is transport
            # representation, not a second required user-authored field.
            purpose = group.get("purpose")
            for record in records:
                subject_kind = record["subject_ref"]["kind"] if record["subject_ref"] is not None else None
                if (record["source_key"] != group["source_key"]
                        or group["purposes"] is not None and record["purpose"] not in group["purposes"]
                        or group["subject_kinds"] is not None and subject_kind not in group["subject_kinds"]):
                    continue
                origin = record["source"]
                item_key = (selected_output_item_key(input_key=key, decision_id=origin["decision_id"],
                    artifact_id=origin["artifact_id"], purpose=purpose) if origin["kind"] == "selected_output"
                    else reviewed_reference_item_key(input_key=key, shot_id=shot_id,
                        reference_key=record["reference_key"]))
                item = {**deepcopy(origin), "item_key": item_key, "purpose": purpose,
                    "subject_ref": deepcopy(record["subject_ref"]), "scope_ref": {"kind": "shot", "id": shot_id},
                    "order": len(items), "paired_to": None}
                items.append(item)
                used.setdefault(record["reference_key"], []).append((key, item_key))
                if record["paired_reference_key"] is not None:
                    pending.append((item, record["paired_reference_key"], spec["semantic_contract"]["paired_with_input_key"]))
        if not spec["min_items"] <= len(items) <= spec["max_items"] or spec["required"] and not items:
            _error(f"input_sources.{key}", f"实际匹配{len(items)}项，合同要求{spec['min_items']}–{spec['max_items']}项；不会截断或补图")
        result[key] = {"items": items}
    for item, parent_key, parent_input in pending:
        matches = [use for use in used.get(parent_key, []) if use[0] == parent_input]
        if len(matches) != 1:
            _error("references", "配套音频的视频未映射到父输入，或存在重复配对歧义")
        item["paired_to"] = matches[0][1]
    return result, [record["reference_key"] for record in records if record["reference_key"] not in used]
