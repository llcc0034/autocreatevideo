from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any

from django.core.exceptions import ValidationError

from studio.services.security_boundaries import boundary_int


BIBLE_IMAGE_GENERATION_STAGE_KEY = "bible_image_generation"
BIBLE_IMAGE_REVIEW_STAGE_KEY = "bible_image_review"
BIBLE_IMAGE_GENERATION_PURPOSE = "bible_reference_image"
BIBLE_IMAGE_SOURCE_SCHEMA_VERSION = 1

# This rejected purpose is removed with the remaining legacy materializers;
# it has no read-side adapter or current provenance projection.
RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE = "bible_image_candidate"

ENTITY_TYPES = ("character", "scene", "prop")


def bible_reference_name_key(value: str) -> str:
    """Shared conflict identity only; never change the authored display name."""
    return unicodedata.normalize("NFKC", value).casefold()


_UNIT_RE = re.compile(
    r"^bible:(character|scene|prop):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r":variant:([1-9][0-9]*)$",
    re.IGNORECASE,
)


def bible_candidate_unit_key(entity_type: str, entity_id: Any, variant_number: int) -> str:
    normalized_type = str(entity_type or "").strip().lower()
    if normalized_type not in ENTITY_TYPES:
        raise ValidationError("Bible 参考图实体类型无效")
    try:
        normalized_id = str(uuid.UUID(str(entity_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Bible 参考图实体 ID 无效") from exc
    maximum_variants = boundary_int(
        "security.production.max_bible_candidates_per_entity"
    )
    if (
        type(variant_number) is not int
        or not 1 <= variant_number <= maximum_variants
    ):
        raise ValidationError(
            f"Bible 参考图 variant 必须为 1 到 {maximum_variants}"
        )
    return f"bible:{normalized_type}:{normalized_id}:variant:{variant_number}"


def parse_bible_candidate_unit_key(unit_key: str) -> dict[str, Any]:
    match = _UNIT_RE.fullmatch(str(unit_key or ""))
    if match is None:
        raise ValidationError("Bible 参考图 attempt unit_key 无效")
    entity_type, entity_id, variant = match.groups()
    normalized_id = str(uuid.UUID(entity_id))
    variant_number = int(variant)
    return {
        "entity_type": entity_type.lower(),
        "entity_id": normalized_id,
        "variant_number": variant_number,
        # Every candidate is an independently selectable group.  This is the
        # durable multi-select semantic: the database's one-selected-per-group
        # constraint no longer collapses all candidates for one Bible entity.
        "variant_group": (
            f"bible:{entity_type.lower()}:{normalized_id}:candidate:{variant_number}"
        ),
    }


def validate_bible_prompt_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "entity_type", "entity_id", "entity_name",
        "source_snapshot_sha256",
    }:
        raise ValidationError("Bible 参考图 Prompt target 合同无效")
    entity_type = str(value.get("entity_type") or "").strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValidationError("Bible 参考图 Prompt target 实体类型无效")
    try:
        entity_id = str(uuid.UUID(str(value.get("entity_id") or "")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Bible 参考图 Prompt target 实体 ID 无效") from exc
    source_sha = str(value.get("source_snapshot_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        raise ValidationError("Bible 参考图 Prompt target 来源摘要无效")
    if value.get("schema_version") != BIBLE_IMAGE_SOURCE_SCHEMA_VERSION:
        raise ValidationError("Bible 参考图 Prompt target 版本无效")
    name = str(value.get("entity_name") or "").strip()
    if not name:
        raise ValidationError("Bible 参考图 Prompt target 名称为空")
    return {
        "schema_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": name,
        "source_snapshot_sha256": source_sha,
    }


def is_retired_bible_image_candidate_prompt(prompt: Any) -> bool:
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    return options.get("generation_purpose") == RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE
