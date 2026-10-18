"""Project compiled native-text channels, not their audit copy, onto the wire."""
from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError


def native_prompt_payload(assembly: Any, *, operation: str) -> tuple[str, dict[str, str]]:
    """Keep field values in the frozen plan; send their rendered channels once.

    These native protocols accept text channels only. A structured-only value
    cannot be silently discarded or used as a fallback for a missing channel.
    No template expansion, truncation or content-based deduplication occurs here.
    """
    if not isinstance(assembly, dict):
        raise ValidationError(f"{operation} PromptProfile assembly 无效")
    channels = assembly.get("channels")
    structured = assembly.get("structured_fields")
    if not isinstance(channels, dict) or not isinstance(structured, dict):
        raise ValidationError(f"{operation} PromptProfile channels 无效")
    allowed = {"system_instruction", "positive_prompt", "negative_prompt", "structured_input"}
    if set(channels) - allowed or any(not isinstance(value, str) for value in channels.values()):
        raise ValidationError(f"{operation} PromptProfile 含未知通道或非文本通道")
    if channels.get("structured_input", ""):
        raise ValidationError(f"{operation} 不支持 structured_input 通道，请显式配置文本通道")
    if structured and not any(value.strip() for value in channels.values()):
        raise ValidationError(f"{operation} 字段尚未组装到文本通道，不能用字段快照代替提示词")
    return channels.get("system_instruction", "").strip(), {
        "positive_prompt": channels.get("positive_prompt", ""),
        "negative_prompt": channels.get("negative_prompt", ""),
    }
