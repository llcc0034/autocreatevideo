"""Canonical persisted contract for one authored dialogue line.

``Shot.dialogue`` intentionally remains a JSONField so this feature does not
require a migration.  All API write paths and the prompt compiler use this
module, preventing a permissive JSON payload from silently losing delivery or
voiceover intent.
"""
from __future__ import annotations

import re
from typing import Any


DIALOGUE_MAX_ENTRIES = 64
DIALOGUE_SPEAKER_ID_MAX_LENGTH = 16
DIALOGUE_TEXT_MAX_LENGTH = 2_000
DIALOGUE_DELIVERY_MAX_LENGTH = 500
DIALOGUE_ALLOWED_KEYS = frozenset({"speaker_id", "text", "delivery", "voiceover"})
_SPEAKER_ID_RE = re.compile(r"^S[1-9][0-9]*$")
_VOICEOVER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])voiceover(?![A-Za-z0-9_])", re.IGNORECASE)


class DialogueContractError(ValueError):
    pass


def parse_delivery_annotation(value: str) -> tuple[str, bool]:
    """Split the UI/script parenthetical into delivery prose and VO intent."""
    voiceover = bool(_VOICEOVER_TOKEN_RE.search(value))
    delivery = _VOICEOVER_TOKEN_RE.sub(" ", value)
    delivery = re.sub(r"^[\s;；,，|/]+|[\s;；,，|/]+$", "", delivery)
    delivery = re.sub(r"\s*[;；]\s*", "; ", delivery).strip()
    return delivery, voiceover


def normalize_dialogue_entry(value: Any, *, index: int | None = None) -> dict:
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
    if not speaker_id or len(speaker_id) > DIALOGUE_SPEAKER_ID_MAX_LENGTH:
        raise DialogueContractError(
            f"{label}.speaker_id 长度必须为 1–{DIALOGUE_SPEAKER_ID_MAX_LENGTH} 个字符"
        )
    if not _SPEAKER_ID_RE.fullmatch(speaker_id):
        raise DialogueContractError(f"{label}.speaker_id 必须为 S1、S2 这样的格式")

    text = value["text"]
    if not isinstance(text, str):
        raise DialogueContractError(f"{label}.text 必须是字符串")
    if not text.strip() or len(text) > DIALOGUE_TEXT_MAX_LENGTH:
        raise DialogueContractError(
            f"{label}.text 必须为 1–{DIALOGUE_TEXT_MAX_LENGTH} 个字符的非空文本"
        )

    result = {"speaker_id": speaker_id, "text": text}
    if "delivery" in value:
        delivery = value["delivery"]
        if not isinstance(delivery, str):
            raise DialogueContractError(f"{label}.delivery 必须是字符串")
        if not delivery.strip() or len(delivery) > DIALOGUE_DELIVERY_MAX_LENGTH:
            raise DialogueContractError(
                f"{label}.delivery 必须为 1–{DIALOGUE_DELIVERY_MAX_LENGTH} 个字符的非空文本"
            )
        result["delivery"] = delivery.strip()
    if "voiceover" in value:
        voiceover = value["voiceover"]
        if type(voiceover) is not bool:  # bool is deliberate; reject 0/1.
            raise DialogueContractError(f"{label}.voiceover 必须是布尔值")
        result["voiceover"] = voiceover
    return result


def normalize_dialogue(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise DialogueContractError("dialogue 必须是数组")
    if len(value) > DIALOGUE_MAX_ENTRIES:
        raise DialogueContractError(f"dialogue 最多包含 {DIALOGUE_MAX_ENTRIES} 行")
    return [normalize_dialogue_entry(item, index=index) for index, item in enumerate(value)]
