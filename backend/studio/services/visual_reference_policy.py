"""Model-agnostic visual-reference eligibility for character Bible rows.

This is a Bible-level visual policy, not a source of shot visibility, speakers
or per-call material selections. ShotCharacterRelation and ShotReferenceUse
own those explicitly authored facts; this policy must not rewrite them.

An explicit structured policy in ``CharacterBible.metadata`` always wins::

    {
        "visual_reference_policy": {
            "schema_version": 1,
            "mode": "auto" | "include" | "exclude"
        }
    }

``auto`` (and an absent policy) uses a deliberately small, high-confidence
semantic rule over the Bible description/role and verified primary identity.
It never reads the obsolete entity visual text or interprets an appearance
change as a permanent change to speaker eligibility. The rule
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
    """A visual-reference policy or its authoritative source is invalid."""


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


def _semantic_text(character: Any, primary_setting) -> str:
    values = (
        getattr(character, "description", ""),
        primary_setting.identity_anchor if primary_setting is not None else "",
        getattr(character, "role", ""),
    )
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip()
        for value in values
        if str(value or "").strip()
    )


def _decision(character, mode, primary_setting) -> CharacterVisualReferenceDecision:
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

    text = _semantic_text(character, primary_setting)
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
        reason="visual_reference_default",
        source="semantic_fallback",
    )


def character_visual_reference_decisions(
    characters: Iterable[Any], *, primary_settings=None,
) -> dict[str, CharacterVisualReferenceDecision]:
    """Read auto-mode primary identities in batches, scoped to each project.

    ``primary_settings`` is an internal reuse point for an operation that has
    already used read_entity_primary_settings (such as consistency capture).
    Explicit overrides need no primary. A missing primary is allowed; a broken
    pointer/body is not absence and must not produce a guessed decision.
    Nothing is attached to ORM instances or cached across operations.
    """
    from studio.services.bible_setting_evidence import read_entity_primary_settings
    from studio.services.bible_variants import BibleVariantConflict

    rows = {str(row.pk): row for row in characters}
    modes = {key: _explicit_mode(row.metadata) for key, row in rows.items()}
    groups = {}
    for key, row in rows.items():
        if modes[key] not in {"include", "exclude"}:
            groups.setdefault(row.project_id, []).append(("character", key))
    settings = {} if primary_settings is None else primary_settings
    if primary_settings is None:
        try:
            for project_id, keys in groups.items():
                settings.update(read_entity_primary_settings(project_id=project_id, entity_keys=keys))
        except BibleVariantConflict as exc:
            raise VisualReferencePolicyError(f"人物出镜判断的主图设定无效: {exc}") from exc
    if any(key not in settings for keys in groups.values() for key in keys):
        raise VisualReferencePolicyError("人物出镜判断缺少已核验的主图读取结果")
    return {key: _decision(row, modes[key], settings.get(("character", key))) for key, row in rows.items()}


def character_visual_reference_decision(character: Any) -> CharacterVisualReferenceDecision:
    """Resolve one character through the same batched current-source reader."""
    return character_visual_reference_decisions([character])[str(character.pk)]


def visual_reference_characters(characters: Iterable[Any]) -> list[Any]:
    characters = list(characters)
    decisions = character_visual_reference_decisions(characters)
    return [
        character
        for character in characters
        if decisions[str(character.pk)].eligible
    ]
