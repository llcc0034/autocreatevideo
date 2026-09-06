"""Shared content-entry and operator-audit safety contracts.

Business/version contracts may choose lower capacities.  These helpers expose
only the system security envelope so API serializers and services cannot drift
into separate numeric limits.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError

from studio.services.configuration import resolve_configuration
from studio.services.security_boundaries import boundary_int, boundary_values


CANONICAL_NAME_POLICY_KEY = "production.canonical_name_policy"
CANONICAL_CONTEXT_POLICY_KEY = "production.canonical_context_policy"


def translation_text_max_characters() -> int:
    return boundary_int("security.prompt_normalization.text_max_characters")


def translation_request_max_entries() -> int:
    return boundary_int("security.translation.max_entries_per_request")


def translation_promotion_max_entries() -> int:
    return boundary_int("security.translation.max_promotions_per_request")


def canonical_glossary_max_entities() -> int:
    return boundary_int("security.canonical_glossary.max_entities_per_request")


def canonical_glossary_max_runtime_items() -> int:
    return boundary_int("security.canonical_glossary.max_runtime_items_per_request")


def canonical_name_policy(
    *,
    values: dict[str, object] | None = None,
    project=None,
    run=None,
) -> dict[str, int]:
    """Resolve the versioned canonical-name policy under its hard envelope.

    The ordinary policy may be selected at system or project scope.  Production
    runs already freeze the resolved configuration snapshot before execution;
    callers operating outside a run intentionally use the active project view.
    """

    if values is None:
        values = resolve_configuration(
            project=project,
            run=run,
            required_keys=(CANONICAL_NAME_POLICY_KEY,),
        ).values
    raw = values.get(CANONICAL_NAME_POLICY_KEY)
    required = {
        "schema_version",
        "max_characters",
        "max_words",
        "max_entities_per_request",
        "max_entities_with_visual_identity_translation",
        "max_runtime_items_per_request",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema_version") != 1:
        raise ValidationError({"canonical_name_policy": "canonical name 策略合同无效"})
    result = {key: raw.get(key) for key in required if key != "schema_version"}
    if any(type(value) is not int or value < 1 for value in result.values()):
        raise ValidationError({"canonical_name_policy": "canonical name 策略必须使用正整数"})
    if (
        result["max_characters"]
        > boundary_int("security.contract_complexity.canonical_name_max_characters")
        or result["max_words"]
        > boundary_int("security.contract_complexity.canonical_name_max_words")
        or result["max_entities_per_request"] > canonical_glossary_max_entities()
        or result["max_runtime_items_per_request"] > canonical_glossary_max_runtime_items()
        or result["max_entities_with_visual_identity_translation"]
        > result["max_entities_per_request"]
    ):
        raise ValidationError({"canonical_name_policy": "canonical name 策略超出安全边界"})
    return {key: int(value) for key, value in result.items()}


def canonical_context_policy(
    *,
    values: dict[str, object] | None = None,
    project=None,
    run=None,
) -> dict[str, int]:
    """Resolve advisory capacities for full canonical model context.

    These values never authorize authored-fact truncation and never replace the
    hard request/snapshot byte boundaries.  They exist so operators can tune
    warnings without changing the canonical-glossary service.
    """

    if values is None:
        values = resolve_configuration(
            project=project,
            run=run,
            required_keys=(CANONICAL_CONTEXT_POLICY_KEY,),
        ).values
    raw = values.get(CANONICAL_CONTEXT_POLICY_KEY)
    required = {
        "schema_version",
        "proper_name_glossary_advisory_entries",
        "visual_identity_glossary_advisory_entries",
        "description_advisory_characters",
        "visual_identity_advisory_characters",
        "glossary_term_advisory_characters",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema_version") != 1:
        raise ValidationError({
            "canonical_context_policy": "canonical 上下文建议策略合同无效",
        })
    result = {key: raw.get(key) for key in required if key != "schema_version"}
    if any(type(value) is not int or value < 1 for value in result.values()):
        raise ValidationError({
            "canonical_context_policy": "canonical 上下文建议值必须使用正整数",
        })
    return {key: int(value) for key, value in result.items()}


def validate_translation_text(value: object, *, field: str) -> str:
    text = str(value or "")
    maximum = translation_text_max_characters()
    if len(text) > maximum:
        raise ValidationError({
            field: f"不能超过当前安全边界 {maximum} 个字符",
        })
    return text


def audit_note_bounds() -> tuple[int, int]:
    values = boundary_values(
        "security.audit_note.minimum_characters",
        "security.audit_note.maximum_characters",
    )
    minimum = values["security.audit_note.minimum_characters"]
    maximum = values["security.audit_note.maximum_characters"]
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or not 1 <= minimum <= maximum
    ):
        raise ValidationError({
            "note": "审计说明安全边界配置无效",
        })
    return minimum, maximum


def normalize_audit_note(value: object) -> str:
    note = str(value or "").strip()
    minimum, maximum = audit_note_bounds()
    if not minimum <= len(note) <= maximum:
        raise ValidationError({
            "note": f"审计说明必须为 {minimum} 到 {maximum} 个字符",
        })
    return note


def normalize_optional_audit_comment(value: object) -> str:
    """Normalize an optional operator comment without imposing note minimums."""
    comment = str(value or "").strip()
    _minimum, maximum = audit_note_bounds()
    if len(comment) > maximum:
        raise ValidationError({
            "comment": f"审批说明不能超过当前安全边界 {maximum} 个字符",
        })
    return comment
