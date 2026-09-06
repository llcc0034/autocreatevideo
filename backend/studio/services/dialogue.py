"""Canonical persisted contract for one authored dialogue line.

``Shot.dialogue`` intentionally remains a JSONField so this feature does not
require a migration.  All API write paths and the prompt compiler use this
module, preventing a permissive JSON payload from silently losing delivery or
voiceover intent.
"""
from __future__ import annotations

import re
from typing import Any

from studio.services.configuration import ConfigurationResolutionError
from studio.services.security_boundaries import boundary_resolution

DIALOGUE_ALLOWED_KEYS = frozenset({
    "speaker_id", "text", "delivery", "voiceover", "start_ms", "end_ms",
})
_SPEAKER_ID_RE = re.compile(r"^S[1-9][0-9]*$")
_VOICEOVER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:voice[\s_-]?over|off[\s_-]?screen(?:\s+voice)?|vo|旁白|画外音)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


class DialogueContractError(ValueError):
    pass


DIALOGUE_SECURITY_KEYS = {
    "max_entries": "security.dialogue.max_entries",
    "speaker_id_max_characters": "security.dialogue.speaker_id_max_characters",
    "text_max_characters": "security.dialogue.text_max_characters",
    "delivery_max_characters": "security.dialogue.delivery_max_characters",
}


def dialogue_security_limits() -> dict[str, int]:
    """Resolve the live superadmin-owned dialogue ingestion envelope."""

    resolved = boundary_resolution(DIALOGUE_SECURITY_KEYS.values())
    limits: dict[str, int] = {}
    for name, key in DIALOGUE_SECURITY_KEYS.items():
        value = resolved.values.get(key)
        if type(value) is not int or value < 1:
            raise ConfigurationResolutionError(
                f"安全边界 {key} 必须是正整数",
                code="security_boundary_value_invalid",
                missing_keys=(key,),
            )
        limits[name] = value
    return limits


def parse_delivery_annotation(value: str) -> tuple[str, bool]:
    """Split the UI/script parenthetical into delivery prose and VO intent."""
    voiceover = bool(_VOICEOVER_TOKEN_RE.search(value))
    delivery = _VOICEOVER_TOKEN_RE.sub(" ", value)
    delivery = re.sub(r"^[\s;；,，|/]+|[\s;；,，|/]+$", "", delivery)
    delivery = re.sub(r"\s*[;；]\s*", "; ", delivery).strip()
    return delivery, voiceover


def normalize_dialogue_entry(
    value: Any,
    *,
    index: int | None = None,
    _limits: dict[str, int] | None = None,
) -> dict:
    limits = _limits or dialogue_security_limits()
    label = f"dialogue[{index}]" if index is not None else "dialogue 项"
    if not isinstance(value, dict):
        raise DialogueContractError(f"{label} 必须是 JSON 对象")
    unknown = set(value) - DIALOGUE_ALLOWED_KEYS
    if unknown:
        raise DialogueContractError(f"{label} 包含未知字段: {', '.join(sorted(unknown))}")
    missing = {"speaker_id", "text"} - set(value)
    if missing:
        raise DialogueContractError(f"{label} 缺少必填字段: {', '.join(sorted(missing))}")

    speaker_id = value["speaker_id"]
    if not isinstance(speaker_id, str):
        raise DialogueContractError(f"{label}.speaker_id 必须是字符串")
    speaker_max = limits["speaker_id_max_characters"]
    if not speaker_id or len(speaker_id) > speaker_max:
        raise DialogueContractError(
            f"{label}.speaker_id 长度必须为 1–{speaker_max} 个字符"
        )
    if not _SPEAKER_ID_RE.fullmatch(speaker_id):
        raise DialogueContractError(f"{label}.speaker_id 必须为 S1、S2 这样的格式")

    text = value["text"]
    if not isinstance(text, str):
        raise DialogueContractError(f"{label}.text 必须是字符串")
    text_max = limits["text_max_characters"]
    if not text.strip() or len(text) > text_max:
        raise DialogueContractError(
            f"{label}.text 必须为 1–{text_max} 个字符的非空文本"
        )

    result = {"speaker_id": speaker_id, "text": text}
    if "delivery" in value:
        delivery = value["delivery"]
        if not isinstance(delivery, str):
            raise DialogueContractError(f"{label}.delivery 必须是字符串")
        delivery_max = limits["delivery_max_characters"]
        if not delivery.strip() or len(delivery) > delivery_max:
            raise DialogueContractError(
                f"{label}.delivery 必须为 1–{delivery_max} 个字符的非空文本"
            )
        result["delivery"] = delivery.strip()
    if "voiceover" in value:
        voiceover = value["voiceover"]
        if type(voiceover) is not bool:  # bool is deliberate; reject 0/1.
            raise DialogueContractError(f"{label}.voiceover 必须是布尔值")
        result["voiceover"] = voiceover
    timing_keys = {"start_ms", "end_ms"} & set(value)
    if timing_keys and timing_keys != {"start_ms", "end_ms"}:
        raise DialogueContractError(
            f"{label}.start_ms 与 {label}.end_ms 必须同时提供"
        )
    if timing_keys:
        start_ms = value["start_ms"]
        end_ms = value["end_ms"]
        if type(start_ms) is not int or type(end_ms) is not int:
            raise DialogueContractError(
                f"{label}.start_ms 与 {label}.end_ms 必须是整数毫秒"
            )
        if start_ms < 0 or end_ms <= start_ms:
            raise DialogueContractError(
                f"{label} 时间范围必须满足 0 <= start_ms < end_ms"
            )
        result["start_ms"] = start_ms
        result["end_ms"] = end_ms
    return result


def normalize_dialogue(value: Any) -> list[dict]:
    limits = dialogue_security_limits()
    if not isinstance(value, list):
        raise DialogueContractError("dialogue 必须是数组")
    max_entries = limits["max_entries"]
    if len(value) > max_entries:
        raise DialogueContractError(f"dialogue 最多包含 {max_entries} 行")
    return [
        normalize_dialogue_entry(item, index=index, _limits=limits)
        for index, item in enumerate(value)
    ]


def validate_dialogue_timing(value: Any, *, duration_ms: int) -> list[dict]:
    """Normalize dialogue and ensure explicit ranges fit the effective shot."""
    normalized = normalize_dialogue(value)
    for index, item in enumerate(normalized):
        if "start_ms" not in item:
            continue
        if item["end_ms"] > duration_ms:
            raise DialogueContractError(
                f"dialogue[{index}].end_ms 不能超过镜头时长 {duration_ms}ms"
            )
    return normalized
