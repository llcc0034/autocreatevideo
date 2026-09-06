from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from django.core.exceptions import ValidationError

from studio.services.execution_monitoring import (
    normalize_execution_monitoring_source,
)
from studio.services.security_boundaries import boundary_int


CONDITIONING_CONTRACT_SCHEMA_VERSION = 1
MEDIA_CONSTRAINTS_SCHEMA_VERSION = 1
REFERENCE_TOPOLOGY_SCHEMA_VERSION = 1

CONFIGURED_CONDITIONING_ADAPTER_KEY = "media_conditioning_v1"
NO_CONDITIONING_ADAPTER_KEY = "none_v1"

CONDITIONING_INPUT_KINDS = frozenset({
    "text_only",
    "frame_slots",
    "classified_references",
})
CONDITIONING_INPUT_SLOTS = (
    "total_images",
    "first_frame",
    "last_frame",
    "entity_images",
    "reference_videos",
    "paired_video_audios",
    "reference_audios",
)
_SAFE_MODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$")
SAFE_CONDITIONING_IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"


def _reference_max_bindings() -> int:
    return boundary_int("security.reference.max_bindings")


def _media_max_duration_ms() -> int:
    return boundary_int("security.media.max_duration_ms")


def _media_max_fps() -> int:
    return boundary_int("security.media.max_fps")


def _media_max_dimension() -> int:
    return boundary_int("security.media.max_dimension")


def _media_contract_max_rules() -> int:
    return boundary_int("security.contract_complexity.media_contract_max_rules")


def _media_contract_max_modes() -> int:
    return boundary_int("security.contract_complexity.media_contract_max_modes")


def _media_contract_text_max_characters() -> int:
    return boundary_int("security.contract_complexity.media_contract_text_max_characters")


def _exact_keys(value: dict[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError({label: f"包含未知字段: {', '.join(unknown)}"})


def _bounded_text(value: object, *, label: str, maximum: int, required: bool) -> str:
    text = str(value or "").strip()
    if (required and not text) or len(text) > maximum or _SAFE_TEXT_RE.fullmatch(text) is None:
        raise ValidationError({label: f"必须是长度不超过 {maximum} 的安全文本"})
    return text


def _cardinality(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValidationError({label: "必须是对象"})
    _exact_keys(value, {"minimum_items", "maximum_items"}, label=label)
    minimum = value.get("minimum_items")
    maximum = value.get("maximum_items")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not 0 <= minimum <= maximum <= _reference_max_bindings()
    ):
        raise ValidationError({
            label: (
                "minimum_items/maximum_items 必须满足 0 <= min <= max <= "
                f"{_reference_max_bindings()}"
            ),
        })
    return {"minimum_items": minimum, "maximum_items": maximum}


def _purpose_definitions(
    value: object,
    *,
    label: str,
    group_cardinality: dict[str, int],
    allow_when_group_empty: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _reference_max_bindings():
        raise ValidationError({
            label: f"必须是不超过 {_reference_max_bindings()} 项的数组",
        })
    if group_cardinality["maximum_items"] > 0 and not value:
        raise ValidationError({label: "输入上限大于 0 时必须声明至少一个用途"})
    if (
        group_cardinality["maximum_items"] == 0
        and value
        and not allow_when_group_empty
    ):
        raise ValidationError({label: "输入上限为 0 时不能声明用途"})
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError({item_label: "必须是对象"})
        _exact_keys(
            raw,
            {"key", "label", "description", "minimum_items", "maximum_items"},
            label=item_label,
        )
        key = str(raw.get("key") or "").strip()
        if _SAFE_MODE_RE.fullmatch(key) is None or key in seen:
            raise ValidationError({f"{item_label}.key": "必须是安全且唯一的 identifier"})
        seen.add(key)
        bounds = _cardinality(
            {
                "minimum_items": raw.get("minimum_items"),
                "maximum_items": raw.get("maximum_items"),
            },
            label=item_label,
        )
        if (
            not allow_when_group_empty
            and bounds["maximum_items"] > group_cardinality["maximum_items"]
        ):
            raise ValidationError({item_label: "用途上限不能超过媒体组上限"})
        result.append({
            "key": key,
            "label": _bounded_text(
                raw.get("label"), label=f"{item_label}.label", maximum=80, required=True,
            ),
            "description": _bounded_text(
                raw.get("description"),
                label=f"{item_label}.description",
                maximum=_media_contract_text_max_characters(),
                required=False,
            ),
            **bounds,
        })
    if sum(item["minimum_items"] for item in result) > group_cardinality["maximum_items"]:
        raise ValidationError({label: "各用途下限之和不能超过媒体组上限"})
    if (
        result
        and sum(item["maximum_items"] for item in result)
        < group_cardinality["minimum_items"]
    ):
        raise ValidationError({label: "各用途上限之和不足以满足媒体组下限"})
    return result


def _reference_media_group(
    value: object,
    *,
    label: str,
    expected_cardinality: dict[str, int],
    allow_purposes_when_empty: bool = False,
    purpose_cardinality: dict[str, int] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError({label: "必须是对象"})
    _exact_keys(
        value,
        {"minimum_items", "maximum_items", "purposes", "default_purpose_key"},
        label=label,
    )
    if set(value) != {
        "minimum_items", "maximum_items", "purposes", "default_purpose_key",
    }:
        raise ValidationError({label: "必须完整声明基数、用途及默认用途"})
    cardinality = _cardinality({
        "minimum_items": value.get("minimum_items"),
        "maximum_items": value.get("maximum_items"),
    }, label=label)
    if cardinality != expected_cardinality:
        raise ValidationError({
            label: "minimum_items/maximum_items 必须与 mode.inputs 的冻结基数完全一致",
        })
    purposes = _purpose_definitions(
        value.get("purposes"),
        label=f"{label}.purposes",
        group_cardinality=purpose_cardinality or cardinality,
        allow_when_group_empty=allow_purposes_when_empty,
    )
    default_purpose_key = value.get("default_purpose_key")
    if purposes:
        default_purpose_key = str(default_purpose_key or "").strip()
        if default_purpose_key not in {item["key"] for item in purposes}:
            raise ValidationError({
                f"{label}.default_purpose_key": "必须明确指向已声明用途",
            })
    elif default_purpose_key is not None:
        raise ValidationError({
            f"{label}.default_purpose_key": "没有用途时必须为 null",
        })
    return {
        **cardinality,
        "purposes": purposes,
        "default_purpose_key": default_purpose_key,
    }


def _reference_preprocessing_contract(
    value: object,
    *,
    label: str,
    required: bool,
    include_target_fps: bool,
) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise ValidationError({label: "该媒体输入已启用，必须声明预处理 adapter"})
        return None
    if not isinstance(value, dict):
        raise ValidationError({label: "必须是对象或 null"})
    allowed = {"adapter_key", "minimum_duration_ms", "maximum_duration_ms"}
    if include_target_fps:
        allowed.update({"target_fps", "requires_source_fps"})
    _exact_keys(value, allowed, label=label)
    if set(value) != allowed:
        raise ValidationError({label: "必须完整声明预处理字段"})
    adapter_key = str(value.get("adapter_key") or "").strip()
    if _SAFE_MODE_RE.fullmatch(adapter_key) is None:
        raise ValidationError({f"{label}.adapter_key": "必须是安全 identifier"})
    minimum = value.get("minimum_duration_ms")
    maximum = value.get("maximum_duration_ms")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not 1 <= minimum <= maximum <= _media_max_duration_ms()
    ):
        raise ValidationError({
            label: (
                "时长范围必须满足 1 <= min <= max <= "
                f"{_media_max_duration_ms()}ms"
            ),
        })
    result: dict[str, Any] = {
        "adapter_key": adapter_key,
        "minimum_duration_ms": minimum,
        "maximum_duration_ms": maximum,
    }
    if include_target_fps:
        target_fps = value.get("target_fps")
        if target_fps is not None and (
            isinstance(target_fps, bool)
            or not isinstance(target_fps, int)
            or not 1 <= target_fps <= _media_max_fps()
        ):
            raise ValidationError({
                f"{label}.target_fps": (
                    f"必须是 null 或 1 到 {_media_max_fps()} 的整数"
                ),
            })
        result["target_fps"] = target_fps
        if type(value.get("requires_source_fps")) is not bool:
            raise ValidationError({f"{label}.requires_source_fps": "必须显式声明布尔值"})
        result["requires_source_fps"] = value["requires_source_fps"]
    return result


def _normalize_dynamic_reference_topology(
    value: object,
    *,
    label: str,
    inputs: dict[str, dict[str, int]],
) -> dict[str, Any] | None:
    dynamic_maximum = max(
        inputs["reference_videos"]["maximum_items"],
        inputs["paired_video_audios"]["maximum_items"],
        inputs["reference_audios"]["maximum_items"],
    )
    if value in (None, ""):
        if dynamic_maximum:
            raise ValidationError({label: "动态参考输入上限大于 0 时必须显式声明"})
        return None
    if not isinstance(value, dict):
        raise ValidationError({label: "必须是对象"})
    _exact_keys(
        value,
        {
            "schema_version", "videos", "audios", "preprocessing", "pairing_rules",
            "mutual_exclusion_rules",
        },
        label=label,
    )
    if value.get("schema_version") != REFERENCE_TOPOLOGY_SCHEMA_VERSION:
        raise ValidationError({label: "schema_version 必须为 1"})
    videos = _reference_media_group(
        value.get("videos"),
        label=f"{label}.videos",
        expected_cardinality=inputs["reference_videos"],
    )
    audios = _reference_media_group(
        value.get("audios"),
        label=f"{label}.audios",
        expected_cardinality=inputs["reference_audios"],
        allow_purposes_when_empty=inputs["paired_video_audios"]["maximum_items"] > 0,
        purpose_cardinality={
            "minimum_items": 0,
            "maximum_items": max(
                inputs["reference_audios"]["maximum_items"],
                inputs["paired_video_audios"]["maximum_items"],
            ),
        },
    )
    purpose_keys = {
        "video": {item["key"] for item in videos["purposes"]},
        "audio": {item["key"] for item in audios["purposes"]},
    }
    preprocessing_raw = value.get("preprocessing")
    if not isinstance(preprocessing_raw, dict) or set(preprocessing_raw) != {
        "videos", "paired_video_audios", "reference_audios",
    }:
        raise ValidationError({
            f"{label}.preprocessing": (
                "必须精确声明 videos/paired_video_audios/reference_audios"
            ),
        })
    preprocessing = {
        "videos": _reference_preprocessing_contract(
            preprocessing_raw.get("videos"),
            label=f"{label}.preprocessing.videos",
            required=inputs["reference_videos"]["maximum_items"] > 0,
            include_target_fps=True,
        ),
        "paired_video_audios": _reference_preprocessing_contract(
            preprocessing_raw.get("paired_video_audios"),
            label=f"{label}.preprocessing.paired_video_audios",
            required=inputs["paired_video_audios"]["maximum_items"] > 0,
            include_target_fps=False,
        ),
        "reference_audios": _reference_preprocessing_contract(
            preprocessing_raw.get("reference_audios"),
            label=f"{label}.preprocessing.reference_audios",
            required=inputs["reference_audios"]["maximum_items"] > 0,
            include_target_fps=False,
        ),
    }

    raw_pairing = value.get("pairing_rules")
    if not isinstance(raw_pairing, list) or len(raw_pairing) > _media_contract_max_rules():
        raise ValidationError({
            f"{label}.pairing_rules": (
                f"必须是不超过 {_media_contract_max_rules()} 项的数组"
            ),
        })
    pairing_rules: list[dict[str, Any]] = []
    seen_pairing_keys: set[str] = set()
    paired_source_purposes: set[str] = set()
    for index, raw in enumerate(raw_pairing):
        item_label = f"{label}.pairing_rules[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError({item_label: "必须是对象"})
        _exact_keys(
            raw,
            {
                "key", "label", "description", "source_media_kind",
                "target_media_kind", "source_purpose_keys", "target_purpose_keys",
                "default_target_purpose_key",
                "minimum_targets_per_source", "maximum_targets_per_source",
                "allow_embedded_source", "allow_external_target",
                "source_and_external_mutually_exclusive",
            },
            label=item_label,
        )
        key = str(raw.get("key") or "").strip()
        if _SAFE_MODE_RE.fullmatch(key) is None or key in seen_pairing_keys:
            raise ValidationError({f"{item_label}.key": "必须是安全且唯一的 identifier"})
        seen_pairing_keys.add(key)
        source_kind = str(raw.get("source_media_kind") or "").strip()
        target_kind = str(raw.get("target_media_kind") or "").strip()
        if (source_kind, target_kind) != ("video", "audio"):
            raise ValidationError({
                item_label: "media_conditioning_v1 当前只注册 video -> audio 配对形态",
            })
        source_keys = raw.get("source_purpose_keys")
        target_keys = raw.get("target_purpose_keys")
        if (
            not isinstance(source_keys, list)
            or not source_keys
            or len(source_keys) != len(set(source_keys))
            or any(key not in purpose_keys["video"] for key in source_keys)
        ):
            raise ValidationError({f"{item_label}.source_purpose_keys": "必须唯一且来自视频用途"})
        if (
            not isinstance(target_keys, list)
            or not target_keys
            or len(target_keys) != len(set(target_keys))
            or any(key not in purpose_keys["audio"] for key in target_keys)
        ):
            raise ValidationError({f"{item_label}.target_purpose_keys": "必须唯一且来自音频用途"})
        default_target_key = str(raw.get("default_target_purpose_key") or "").strip()
        if default_target_key not in target_keys:
            raise ValidationError({
                f"{item_label}.default_target_purpose_key": "必须明确指向允许的目标用途",
            })
        if paired_source_purposes.intersection(source_keys):
            raise ValidationError({
                f"{item_label}.source_purpose_keys": "同一视频用途不能命中多条配对规则",
            })
        paired_source_purposes.update(source_keys)
        minimum_targets = raw.get("minimum_targets_per_source")
        maximum_targets = raw.get("maximum_targets_per_source")
        if (
            isinstance(minimum_targets, bool)
            or isinstance(maximum_targets, bool)
            or not isinstance(minimum_targets, int)
            or not isinstance(maximum_targets, int)
            or not 0 <= minimum_targets <= maximum_targets <= 1
        ):
            raise ValidationError({item_label: "当前绑定结构要求 0 <= 每来源配对数 <= 1"})
        boolean_keys = (
            "allow_embedded_source", "allow_external_target",
            "source_and_external_mutually_exclusive",
        )
        if any(type(raw.get(boolean_key)) is not bool for boolean_key in boolean_keys):
            raise ValidationError({item_label: "配对开关必须全部显式声明为布尔值"})
        if not raw["allow_embedded_source"] and not raw["allow_external_target"]:
            raise ValidationError({item_label: "配对规则至少允许一种音频来源"})
        if (
            raw["allow_external_target"]
            and inputs["paired_video_audios"]["maximum_items"] == 0
        ):
            raise ValidationError({item_label: "允许外部配对时 paired_video_audios 上限必须大于 0"})
        pairing_rules.append({
            "key": key,
            "label": _bounded_text(
                raw.get("label"), label=f"{item_label}.label", maximum=80, required=True,
            ),
            "description": _bounded_text(
                raw.get("description"),
                label=f"{item_label}.description",
                maximum=_media_contract_text_max_characters(),
                required=False,
            ),
            "source_media_kind": source_kind,
            "target_media_kind": target_kind,
            "source_purpose_keys": list(source_keys),
            "target_purpose_keys": list(target_keys),
            "default_target_purpose_key": default_target_key,
            "minimum_targets_per_source": minimum_targets,
            "maximum_targets_per_source": maximum_targets,
            **{boolean_key: raw[boolean_key] for boolean_key in boolean_keys},
        })
    if inputs["paired_video_audios"]["maximum_items"] > 0 and not pairing_rules:
        raise ValidationError({f"{label}.pairing_rules": "配对音频上限大于 0 时必须声明规则"})

    raw_exclusions = value.get("mutual_exclusion_rules")
    if not isinstance(raw_exclusions, list) or len(raw_exclusions) > _media_contract_max_rules():
        raise ValidationError({
            f"{label}.mutual_exclusion_rules": (
                f"必须是不超过 {_media_contract_max_rules()} 项的数组"
            ),
        })
    exclusion_rules: list[dict[str, Any]] = []
    seen_exclusion_keys: set[str] = set()
    for index, raw in enumerate(raw_exclusions):
        item_label = f"{label}.mutual_exclusion_rules[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError({item_label: "必须是对象"})
        _exact_keys(
            raw,
            {"key", "label", "description", "media_kind", "purpose_keys", "maximum_distinct_purposes"},
            label=item_label,
        )
        key = str(raw.get("key") or "").strip()
        if _SAFE_MODE_RE.fullmatch(key) is None or key in seen_exclusion_keys:
            raise ValidationError({f"{item_label}.key": "必须是安全且唯一的 identifier"})
        seen_exclusion_keys.add(key)
        media_kind = str(raw.get("media_kind") or "").strip()
        keys = raw.get("purpose_keys")
        maximum_distinct = raw.get("maximum_distinct_purposes")
        if media_kind not in purpose_keys:
            raise ValidationError({f"{item_label}.media_kind": "必须是 video 或 audio"})
        if (
            not isinstance(keys, list)
            or len(keys) < 2
            or len(keys) != len(set(keys))
            or any(key not in purpose_keys[media_kind] for key in keys)
        ):
            raise ValidationError({f"{item_label}.purpose_keys": "必须是至少两个唯一的已声明用途"})
        if (
            isinstance(maximum_distinct, bool)
            or not isinstance(maximum_distinct, int)
            or not 1 <= maximum_distinct < len(keys)
        ):
            raise ValidationError({f"{item_label}.maximum_distinct_purposes": "必须限制在 1 到用途数减 1"})
        exclusion_rules.append({
            "key": key,
            "label": _bounded_text(
                raw.get("label"), label=f"{item_label}.label", maximum=80, required=True,
            ),
            "description": _bounded_text(
                raw.get("description"),
                label=f"{item_label}.description",
                maximum=_media_contract_text_max_characters(),
                required=False,
            ),
            "media_kind": media_kind,
            "purpose_keys": list(keys),
            "maximum_distinct_purposes": maximum_distinct,
        })
    return {
        "schema_version": REFERENCE_TOPOLOGY_SCHEMA_VERSION,
        "videos": videos,
        "audios": audios,
        "preprocessing": preprocessing,
        "pairing_rules": pairing_rules,
        "mutual_exclusion_rules": exclusion_rules,
    }


def empty_conditioning_contract() -> dict[str, Any]:
    return {
        "schema_version": CONDITIONING_CONTRACT_SCHEMA_VERSION,
        "adapter_key": NO_CONDITIONING_ADAPTER_KEY,
        "modes": [],
    }


def conditioning_contract_editor_options() -> dict[str, Any]:
    """Authoring metadata, not a claim about any model's actual capacity."""
    kind_labels = {"text_only": "纯文本（不接收素材）", "frame_slots": "首尾帧素材", "classified_references": "多参考素材"}
    slot_labels = {
        "total_images": "图片总数", "first_frame": "首帧图片", "last_frame": "尾帧图片",
        "entity_images": "参考图片", "reference_videos": "参考视频",
        "paired_video_audios": "视频配对音频", "reference_audios": "独立参考音频",
    }
    return {
        "schema_version": CONDITIONING_CONTRACT_SCHEMA_VERSION,
        "adapter_key": CONFIGURED_CONDITIONING_ADAPTER_KEY,
        "input_kinds": [{"value": key, "label": kind_labels[key]} for key in sorted(CONDITIONING_INPUT_KINDS)],
        "input_slots": [{"value": key, "label": slot_labels[key]} for key in CONDITIONING_INPUT_SLOTS],
        "maximum_items": _reference_max_bindings(),
        "maximum_modes": _media_contract_max_modes(),
        "identifier_pattern": SAFE_CONDITIONING_IDENTIFIER_PATTERN,
        "mode_template": {
            "key": "", "label": "", "description": "", "input_kind": "",
            "requires_entity_reference_bible": False, "supports_native_audio": False,
            "inputs": {key: {"minimum_items": 0, "maximum_items": 0} for key in CONDITIONING_INPUT_SLOTS},
        },
    }


def normalize_conditioning_contract(value: object) -> dict[str, Any]:
    """Validate the declarative media-conditioning contract.

    The contract contains data only. ``adapter_key`` selects reviewed server/UI
    code and cannot contain a script, template, regular expression, or import.
    """

    if not isinstance(value, dict):
        raise ValidationError({"conditioning_contract": "必须是对象"})
    _exact_keys(
        value,
        {"schema_version", "adapter_key", "modes"},
        label="conditioning_contract",
    )
    if value.get("schema_version") != CONDITIONING_CONTRACT_SCHEMA_VERSION:
        raise ValidationError({"conditioning_contract": "schema_version 必须为 1"})
    adapter_key = str(value.get("adapter_key") or "").strip()
    if adapter_key != CONFIGURED_CONDITIONING_ADAPTER_KEY:
        raise ValidationError({
            "conditioning_contract": (
                "adapter_key 未注册；当前只允许 media_conditioning_v1"
            ),
        })
    raw_modes = value.get("modes")
    if not isinstance(raw_modes, list) or not 1 <= len(raw_modes) <= _media_contract_max_modes():
        raise ValidationError({
            "conditioning_contract": (
                f"modes 必须是 1 到 {_media_contract_max_modes()} 项数组"
            ),
        })

    modes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_modes):
        label = f"conditioning_contract.modes[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError({label: "必须是对象"})
        _exact_keys(
            raw,
            {
                "key", "label", "description", "input_kind",
                "requires_entity_reference_bible", "supports_native_audio",
                "inputs", "dynamic_reference_topology",
            },
            label=label,
        )
        key = str(raw.get("key") or "").strip()
        if _SAFE_MODE_RE.fullmatch(key) is None or key in seen:
            raise ValidationError({f"{label}.key": "格式无效或重复"})
        seen.add(key)
        input_kind = str(raw.get("input_kind") or "").strip()
        if input_kind not in CONDITIONING_INPUT_KINDS:
            raise ValidationError({f"{label}.input_kind": "未注册"})
        requires_bible = raw.get("requires_entity_reference_bible")
        supports_native_audio = raw.get("supports_native_audio")
        if type(requires_bible) is not bool or type(supports_native_audio) is not bool:
            raise ValidationError({
                label: (
                    "requires_entity_reference_bible/supports_native_audio 必须是布尔值"
                ),
            })
        raw_inputs = raw.get("inputs")
        if not isinstance(raw_inputs, dict) or set(raw_inputs) != set(CONDITIONING_INPUT_SLOTS):
            raise ValidationError({
                f"{label}.inputs": (
                    "必须精确声明 " + ", ".join(CONDITIONING_INPUT_SLOTS)
                ),
            })
        inputs = {
            slot: _cardinality(raw_inputs[slot], label=f"{label}.inputs.{slot}")
            for slot in CONDITIONING_INPUT_SLOTS
        }
        total_images = inputs["total_images"]
        first_frame = inputs["first_frame"]
        last_frame = inputs["last_frame"]
        entity_images = inputs["entity_images"]
        if (
            inputs["paired_video_audios"]["maximum_items"]
            > inputs["reference_videos"]["maximum_items"]
        ):
            raise ValidationError({
                label: "paired_video_audios 上限不能超过 reference_videos 上限",
            })
        if input_kind == "text_only" and any(
            item["maximum_items"] for item in inputs.values()
        ):
            raise ValidationError({label: "text_only 的所有输入上限必须为 0"})
        if input_kind == "frame_slots" and any(
            inputs[slot]["maximum_items"]
            for slot in (
                "entity_images", "reference_videos", "paired_video_audios",
                "reference_audios",
            )
        ):
            raise ValidationError({label: "frame_slots 不能声明实体、参考视频或参考音频输入"})
        if (
            first_frame["maximum_items"] + last_frame["maximum_items"]
            + entity_images["maximum_items"]
            < total_images["maximum_items"]
        ):
            raise ValidationError({label: "total_images 上限不能超过各图片槽位上限之和"})
        if requires_bible and entity_images["maximum_items"] == 0:
            raise ValidationError({
                label: "requires_entity_reference_bible=true 时 entity_images 上限必须大于 0",
            })
        dynamic_reference_topology = _normalize_dynamic_reference_topology(
            raw.get("dynamic_reference_topology"),
            label=f"{label}.dynamic_reference_topology",
            inputs=inputs,
        )
        mode = {
            "key": key,
            "label": _bounded_text(raw.get("label"), label=f"{label}.label", maximum=80, required=True),
            "description": _bounded_text(
                raw.get("description"),
                label=f"{label}.description",
                maximum=_media_contract_text_max_characters(),
                required=False,
            ),
            "input_kind": input_kind,
            "requires_entity_reference_bible": requires_bible,
            "supports_native_audio": supports_native_audio,
            "inputs": inputs,
        }
        if dynamic_reference_topology is not None:
            mode["dynamic_reference_topology"] = dynamic_reference_topology
        modes.append(mode)
    return {
        "schema_version": CONDITIONING_CONTRACT_SCHEMA_VERSION,
        "adapter_key": adapter_key,
        "modes": modes,
    }


def resolve_conditioning_contract(
    *,
    workflow_input_contract: object = None,
    workflow_compiler_id: str = "",
    model_specification: object = None,
    output_kind: str = "",
    required: bool = False,
) -> dict[str, Any]:
    del workflow_compiler_id
    workflow_contract = (
        workflow_input_contract.get("conditioning_contract")
        if isinstance(workflow_input_contract, dict)
        else None
    )
    if output_kind and output_kind != "video":
        if workflow_contract is not None:
            raise ValidationError({
                "conditioning_contract": "非 video workflow 不能声明 conditioning modes",
            })
        return empty_conditioning_contract()
    if workflow_contract is not None:
        contract = normalize_conditioning_contract(workflow_contract)
    else:
        model_contract = (
            model_specification.get("conditioning_contract")
            if isinstance(model_specification, dict)
            else None
        )
        contract = (
            normalize_conditioning_contract(model_contract)
            if model_contract is not None
            else empty_conditioning_contract()
        )
    if required and output_kind == "video" and not contract["modes"]:
        raise ValidationError({
            "conditioning_contract": "视频 route 必须在 Workflow 或 ModelProfile 冻结 conditioning_contract",
        })
    return deepcopy(contract)


def merge_conditioning_contract_modes(
    contracts: object,
) -> list[dict[str, Any]]:
    """Merge configured route contracts without hiding conflicting mode keys."""

    if not isinstance(contracts, (list, tuple)):
        raise ValidationError({"conditioning_contracts": "必须是合同数组"})
    modes: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            raise ValidationError({
                "conditioning_contracts": f"contracts[{index}] 必须是对象",
            })
        for raw_mode in contract.get("modes") or []:
            if not isinstance(raw_mode, dict):
                raise ValidationError({
                    "conditioning_contracts": f"contracts[{index}] mode 必须是对象",
                })
            mode = deepcopy(raw_mode)
            key = str(mode.get("key") or "").strip()
            existing = by_key.get(key)
            if existing is not None and existing != mode:
                raise ValidationError({
                    "conditioning_contracts": (
                        f"已发布 route 对模式 {key or '(empty)'} 声明了冲突的能力合同"
                    ),
                })
            if existing is None:
                by_key[key] = mode
                modes.append(mode)
    return modes


def conditioning_mode_contract(
    contract: object,
    mode: object,
    *,
    required: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(contract, dict):
        if required:
            raise ValidationError({"conditioning_contract": "冻结合同缺失"})
        return None
    mode_key = str(mode or "").strip()
    matches = [
        item
        for item in (contract.get("modes") or [])
        if isinstance(item, dict) and item.get("key") == mode_key
    ]
    if len(matches) != 1:
        if required:
            raise ValidationError({
                "conditioning_mode": f"模式 {mode_key or '(empty)'} 未被冻结 conditioning_contract 唯一声明",
            })
        return None
    return deepcopy(matches[0])


def conditioning_contract_from_frozen_route(route: object) -> dict[str, Any]:
    """Read the single frozen conditioning contract from a route."""

    if not isinstance(route, dict):
        raise ValidationError({"conditioning_contract": "冻结 route 必须是对象"})
    configured = route.get("conditioning_contract")
    if isinstance(configured, dict):
        return deepcopy(configured)
    raise ValidationError({
        "conditioning_contract": "冻结 route 缺少 conditioning_contract",
    })


def normalize_model_media_constraints(specification: object) -> dict[str, Any]:
    """Return one generic executable projection from a ModelProfile specification."""

    if not isinstance(specification, dict):
        raise ValidationError({"specification": "必须是对象"})
    raw = specification.get("media_constraints")
    if raw is None:
        return {
            "schema_version": MEDIA_CONSTRAINTS_SCHEMA_VERSION,
            "resolution": {},
            "fps": {"allowed": []},
            "duration_ms": {},
        }

    if not isinstance(raw, dict):
        raise ValidationError({"specification.media_constraints": "必须是对象"})
    _exact_keys(
        raw,
        {"schema_version", "resolution", "fps", "duration_ms"},
        label="specification.media_constraints",
    )
    if raw.get("schema_version") != MEDIA_CONSTRAINTS_SCHEMA_VERSION:
        raise ValidationError({"specification.media_constraints": "schema_version 必须为 1"})
    resolution = raw.get("resolution", {})
    if not isinstance(resolution, dict):
        raise ValidationError({"specification.media_constraints.resolution": "必须是对象"})
    resolution_keys = {
        "width_multiple", "height_multiple", "minimum_width", "maximum_width",
        "minimum_height", "maximum_height", "maximum_short_side", "maximum_long_side",
    }
    _exact_keys(
        resolution,
        resolution_keys,
        label="specification.media_constraints.resolution",
    )
    normalized_resolution: dict[str, int] = {}
    for key, value in resolution.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _media_max_dimension()
        ):
            raise ValidationError({
                f"specification.media_constraints.resolution.{key}": (
                    f"必须是 1 到 {_media_max_dimension()} 的整数"
                ),
            })
        normalized_resolution[key] = value
    for minimum_key, maximum_key in (
        ("minimum_width", "maximum_width"),
        ("minimum_height", "maximum_height"),
    ):
        if (
            minimum_key in normalized_resolution
            and maximum_key in normalized_resolution
            and normalized_resolution[minimum_key] > normalized_resolution[maximum_key]
        ):
            raise ValidationError({"specification.media_constraints.resolution": f"{minimum_key} 不能大于 {maximum_key}"})

    fps_contract = raw.get("fps", {"allowed": []})
    if not isinstance(fps_contract, dict):
        raise ValidationError({"specification.media_constraints.fps": "必须是对象"})
    _exact_keys(fps_contract, {"allowed"}, label="specification.media_constraints.fps")
    allowed = fps_contract.get("allowed", [])
    if not isinstance(allowed, list) or len(allowed) > _reference_max_bindings():
        raise ValidationError({
            "specification.media_constraints.fps.allowed": (
                f"必须是不超过 {_reference_max_bindings()} 项的数组"
            ),
        })
    if any(
        isinstance(item, bool)
        or not isinstance(item, int)
        or not 1 <= item <= _media_max_fps()
        for item in allowed
    ):
        raise ValidationError({
            "specification.media_constraints.fps.allowed": (
                f"每项必须是 1 到 {_media_max_fps()} 的整数"
            ),
        })
    if len(set(allowed)) != len(allowed):
        raise ValidationError({"specification.media_constraints.fps.allowed": "不能重复"})

    duration = raw.get("duration_ms", {})
    if not isinstance(duration, dict):
        raise ValidationError({"specification.media_constraints.duration_ms": "必须是对象"})
    _exact_keys(duration, {"minimum", "maximum"}, label="specification.media_constraints.duration_ms")
    if duration and set(duration) != {"minimum", "maximum"}:
        raise ValidationError({"specification.media_constraints.duration_ms": "必须同时声明 minimum 与 maximum"})
    normalized_duration: dict[str, int] = {}
    for key, value in duration.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _media_max_duration_ms()
        ):
            raise ValidationError({
                f"specification.media_constraints.duration_ms.{key}": (
                    f"必须是 1 到 {_media_max_duration_ms()} 的整数毫秒"
                ),
            })
        normalized_duration[key] = value
    if normalized_duration and normalized_duration["minimum"] > normalized_duration["maximum"]:
        raise ValidationError({"specification.media_constraints.duration_ms": "minimum 不能大于 maximum"})
    return {
        "schema_version": MEDIA_CONSTRAINTS_SCHEMA_VERSION,
        "resolution": normalized_resolution,
        "fps": {"allowed": list(allowed)},
        "duration_ms": normalized_duration,
    }


def validate_model_profile_media_capabilities(
    specification: object,
    *,
    modality: str,
) -> None:
    constraints = normalize_model_media_constraints(specification)
    has_constraints = bool(
        constraints["resolution"]
        or constraints["fps"]["allowed"]
        or constraints["duration_ms"]
    )
    has_conditioning = (
        isinstance(specification, dict)
        and specification.get("conditioning_contract") is not None
    )
    has_execution_monitoring = (
        isinstance(specification, dict)
        and specification.get("execution_monitoring") is not None
    )
    if modality not in {"image", "video", "multimodal"} and (
        has_constraints or has_conditioning or has_execution_monitoring
    ):
        raise ValidationError({"specification": "只有 image/video/multimodal 模型能声明媒体能力合同"})
    if has_conditioning:
        resolve_conditioning_contract(
            model_specification=specification,
            output_kind="video" if modality in {"video", "multimodal"} else modality,
        )
    if has_execution_monitoring:
        normalize_execution_monitoring_source(
            specification["execution_monitoring"],
            source="model",
        )


def validate_media_execution_settings(
    *,
    constraints: object,
    width: int,
    height: int,
    fps: int,
    duration_ms: int,
    media_type: str,
) -> None:
    if not isinstance(constraints, dict):
        raise ValidationError({"media_constraints": "冻结模型约束缺失"})
    resolution = constraints.get("resolution") or {}
    checks = (
        ("minimum_width", width, lambda actual, limit: actual >= limit, "width 低于模型下限"),
        ("maximum_width", width, lambda actual, limit: actual <= limit, "width 超过模型上限"),
        ("minimum_height", height, lambda actual, limit: actual >= limit, "height 低于模型下限"),
        ("maximum_height", height, lambda actual, limit: actual <= limit, "height 超过模型上限"),
        ("maximum_short_side", min(width, height), lambda actual, limit: actual <= limit, "短边超过模型上限"),
        ("maximum_long_side", max(width, height), lambda actual, limit: actual <= limit, "长边超过模型上限"),
    )
    for key, actual, predicate, message in checks:
        if key in resolution and not predicate(actual, int(resolution[key])):
            raise ValidationError({"resolution": f"{message}: {actual} / {resolution[key]}"})
    for dimension, key in ((width, "width_multiple"), (height, "height_multiple")):
        multiple = int(resolution.get(key) or 0)
        if multiple and dimension % multiple:
            raise ValidationError({"resolution": f"width/height 必须满足冻结倍数；{key}={multiple}"})
    if media_type != "video":
        return
    allowed_fps = list((constraints.get("fps") or {}).get("allowed") or [])
    if allowed_fps and fps not in allowed_fps:
        raise ValidationError({"fps": f"必须属于冻结允许值 {allowed_fps}"})
    duration = constraints.get("duration_ms") or {}
    if duration and not int(duration["minimum"]) <= duration_ms <= int(duration["maximum"]):
        raise ValidationError({
            "duration_ms": (
                f"必须在冻结模型范围 {duration['minimum']}–{duration['maximum']}ms"
            ),
        })
