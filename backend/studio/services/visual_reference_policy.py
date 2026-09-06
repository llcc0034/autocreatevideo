"""Model-agnostic visual-reference eligibility for character Bible rows.

``Shot.characters`` is the authoritative story/speaker relationship.  It must
therefore keep an off-screen speaker so dialogue validation can still resolve
its stable speaker ID.  Visual conditioning is a different concern: a
globally voice-only/unseen character must not consume a Bible image or a
classified image slot.

An explicit structured policy in ``CharacterBible.metadata`` always wins::

    {
        "visual_reference_policy": {
            "schema_version": 1,
            "mode": "auto" | "include" | "exclude"
        }
    }

``auto`` (and an absent policy) uses a deliberately small, high-confidence
semantic fallback over the Bible description/visual identity.  The fallback
is platform vocabulary, not a model/workflow name, so every image/video
adapter consumes the same decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable


VISUAL_REFERENCE_POLICY_METADATA_KEY = "visual_reference_policy"
VISUAL_REFERENCE_POLICY_SCHEMA_VERSION = 1
VISUAL_REFERENCE_POLICY_MODES = frozenset({"auto", "include", "exclude"})

_VOICE_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "voice_only",
        re.compile(r"\bvoice[\s_-]*only\b", re.IGNORECASE),
    ),
    (
        "never_visible",
        re.compile(
            r"\b(?:never|not)\s+(?:appears?|shown|seen|visible)"
            r"(?:\s+(?:on[\s-]*camera|on[\s-]*screen|in\s+(?:the\s+)?frame))?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "offscreen_speaker",
        re.compile(
            r"\b(?:unseen|off[\s-]*screen)\s+"
            r"(?:speaker|voice|character|companion|narrator|caller)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "voice_only_zh",
        re.compile(
            r"(?:只闻其声|仅闻其声|不出镜|从不出镜|永不出镜|未出镜|"
            r"不露面|从未露面|不在画面(?:中)?|不可见(?:角色|人物|说话者)|"
            r"画外(?:音|声音|说话者|角色)|镜头外(?:声音|说话者|角色)|"
            r"(?:仅|只)(?:有)?(?:对白|声音|语音|声线)(?:出场|出现)?)"
        ),
    ),
)


class VisualReferencePolicyError(ValueError):
    """An explicit visual-reference policy is malformed."""


@dataclass(frozen=True)
class CharacterVisualReferenceDecision:
    eligible: bool
    mode: str
    reason: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VISUAL_REFERENCE_POLICY_SCHEMA_VERSION,
            "eligible": self.eligible,
            "mode": self.mode,
            "reason": self.reason,
            "source": self.source,
        }


def _explicit_mode(metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get(VISUAL_REFERENCE_POLICY_METADATA_KEY)
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "mode"}
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != VISUAL_REFERENCE_POLICY_SCHEMA_VERSION
        or raw.get("mode") not in VISUAL_REFERENCE_POLICY_MODES
    ):
        raise VisualReferencePolicyError(
            "metadata.visual_reference_policy 必须且只能包含 "
            "schema_version=1 与 mode=auto/include/exclude",
        )
    return str(raw["mode"])


def validate_visual_reference_policy_metadata(metadata: object) -> None:
    """Validate the optional structured override without classifying prose."""

    _explicit_mode(metadata)


def _semantic_text(character: Any) -> str:
    values = (
        getattr(character, "description", ""),
        getattr(character, "visual_identity", ""),
        getattr(character, "role", ""),
    )
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip()
        for value in values
        if str(value or "").strip()
    )


def character_visual_reference_decision(
    character: Any,
) -> CharacterVisualReferenceDecision:
    """Resolve one stable character-level visual-conditioning decision."""

    mode = _explicit_mode(getattr(character, "metadata", None))
    if mode == "include":
        return CharacterVisualReferenceDecision(
            eligible=True,
            mode=mode,
            reason="explicit_include",
            source="structured_metadata",
        )
    if mode == "exclude":
        return CharacterVisualReferenceDecision(
            eligible=False,
            mode=mode,
            reason="explicit_exclude",
            source="structured_metadata",
        )

    text = _semantic_text(character)
    for reason, pattern in _VOICE_ONLY_PATTERNS:
        if pattern.search(text):
            return CharacterVisualReferenceDecision(
                eligible=False,
                mode="auto",
                reason=reason,
                source="semantic_fallback",
            )
    return CharacterVisualReferenceDecision(
        eligible=True,
        mode="auto",
        reason="visual_identity_default",
        source="semantic_fallback",
    )


def character_requires_visual_reference(character: Any) -> bool:
    return character_visual_reference_decision(character).eligible


def visual_reference_characters(characters: Iterable[Any]) -> list[Any]:
    return [
        character
        for character in characters
        if character_requires_visual_reference(character)
    ]
