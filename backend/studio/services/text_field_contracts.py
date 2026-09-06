"""Versioned text-field policies shared by text generation operations.

The field names and object shape are protocol identifiers.  Actual limits and
requiredness are supplied by the published ModelProfileVersion and are copied
into each immutable request plan before any provider call.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from django.core.exceptions import ValidationError

from studio.models import Episode, SeriesProject


TEXT_FIELD_CONTRACT_SCHEMA_VERSION = 1
TEXT_FIELD_RULE_KEYS = frozenset({"max_chars", "required", "single_line"})

SERIES_CONCEPT_FIELD_KEYS = (
    "idea",
    "style_hint",
    "title",
    "logline",
    "bible.entity.name",
    "bible.entity.description",
    "bible.entity.visual_identity_en",
    "episode.title",
    "shot.setting",
    "shot.action",
    "shot.dialogue.speaker",
    "shot.dialogue.delivery",
    "shot.dialogue.text",
)

SCRIPT_STRUCTURE_FIELD_KEYS = (
    "title",
    "logline",
    "bible.entity.name",
    "bible.entity.description",
    "bible.entity.visual_identity_en",
    "episode.title",
    "shot.setting",
    "shot.action",
    "shot.dialogue.speaker",
    "shot.dialogue.delivery",
    "shot.dialogue.text",
)


def validate_text_field_contract(
    value: object,
    *,
    fields: Iterable[str],
    label: str,
) -> dict[str, Any]:
    """Return a detached exact contract or reject unknown/missing policy.

    Positive character counts are versioned business rules.  ``required`` and
    ``single_line`` are explicit so prompt construction and final validation
    cannot silently disagree about an omitted default.
    """

    expected_fields = tuple(fields)
    expected_keys = {"schema_version", *expected_fields}
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != TEXT_FIELD_CONTRACT_SCHEMA_VERSION
    ):
        raise ValidationError({
            "specification": (
                f"{label}.field_contract 必须是 schema v"
                f"{TEXT_FIELD_CONTRACT_SCHEMA_VERSION} 的精确字段合同"
            ),
        })
    normalized: dict[str, Any] = {
        "schema_version": TEXT_FIELD_CONTRACT_SCHEMA_VERSION,
    }
    for field in expected_fields:
        rule = value.get(field)
        if (
            not isinstance(rule, dict)
            or set(rule) != TEXT_FIELD_RULE_KEYS
            or type(rule.get("max_chars")) is not int
            or rule["max_chars"] < 1
            or type(rule.get("required")) is not bool
            or type(rule.get("single_line")) is not bool
        ):
            raise ValidationError({
                "specification": f"{label}.field_contract.{field} 规则无效",
            })
        # Output strings are serialized into the canonical line-oriented
        # script grammar.  Their one-line shape is a protocol invariant; the
        # versioned contract still owns requiredness and character capacity.
        if field not in {"idea", "style_hint"} and not rule["single_line"]:
            raise ValidationError({
                "specification": (
                    f"{label}.field_contract.{field}.single_line 必须为 true"
                ),
            })
        database_field = {
            "title": SeriesProject._meta.get_field("title"),
            "episode.title": Episode._meta.get_field("title"),
        }.get(field)
        if (
            database_field is not None
            and rule["max_chars"] > database_field.max_length
        ):
            raise ValidationError({
                "specification": (
                    f"{label}.field_contract.{field}.max_chars 不能超过"
                    f"数据库字段上限 {database_field.max_length}"
                ),
            })
        normalized[field] = {
            "max_chars": int(rule["max_chars"]),
            "required": rule["required"],
            "single_line": rule["single_line"],
        }
    return deepcopy(normalized)


def text_field_rule(
    contract: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    rule = contract.get(field)
    if not isinstance(rule, dict):
        raise ValidationError({"specification": f"冻结 field_contract 缺少 {field}"})
    return rule


def field_contract_prompt_lines(contract: dict[str, Any]) -> str:
    """Render deterministic human/model-readable constraints from the contract."""

    lines = []
    for field, rule in contract.items():
        if field == "schema_version":
            continue
        required = "required/non-empty" if rule["required"] else "optional"
        line_shape = "single-line" if rule["single_line"] else "multiline allowed"
        lines.append(
            f"- {field}: {required}; {line_shape}; max {int(rule['max_chars'])} characters"
        )
    return "\n".join(lines)
