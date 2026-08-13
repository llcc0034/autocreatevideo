from __future__ import annotations

import hashlib
import re
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from studio.models import EpisodeCreativeDraft, Shot


def normalize_legacy_negative_prompt(value: object) -> str:
    seen = set()
    values = []
    for raw in re.split(r"[,，;；\n]+", str(value or "")):
        item = raw.strip()
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            values.append(item)
    return "; ".join(values)


def legacy_negative_prompt_repair_evidence(shot: Shot) -> dict | None:
    """Return exact CreativeDraft provenance only for the known legacy corruption."""

    metadata = dict(shot.metadata or {})
    current = str(metadata.get("negative_prompt") or "")
    if not current:
        return None
    draft_id = str(metadata.get("creative_draft_id") or "")
    client_key = str(metadata.get("client_key") or "")
    try:
        parsed_draft_id = uuid.UUID(draft_id)
        draft = EpisodeCreativeDraft.objects.get(
            pk=parsed_draft_id, project=shot.episode.project, episode=shot.episode,
            status=EpisodeCreativeDraft.Status.APPLIED,
        )
    except (
        EpisodeCreativeDraft.DoesNotExist, DjangoValidationError,
        ValueError, TypeError, AttributeError,
    ):
        return None
    source_item = next((
        item for item in (draft.draft_data or {}).get("shots", [])
        if isinstance(item, dict) and str(item.get("client_key") or "") == client_key
    ), None)
    expected = normalize_legacy_negative_prompt(
        source_item.get("continuity_zh", "") if source_item else ""
    )
    if not expected or normalize_legacy_negative_prompt(current) != expected:
        return None
    return {
        "draft": draft,
        "client_key": client_key,
        "current": current,
    }


def repair_legacy_negative_prompt(*, shot: Shot, user) -> dict:
    metadata = dict(shot.metadata or {})
    current = str(metadata.get("negative_prompt") or "")
    previous_recovery = metadata.get("legacy_negative_prompt_recovery") or {}
    if not current and previous_recovery.get("recovered"):
        return {"metadata": metadata, "updated_at": shot.updated_at.isoformat()}
    if not current:
        return {}
    evidence = legacy_negative_prompt_repair_evidence(shot)
    if evidence is None:
        raise ValueError("legacy_negative_value_mismatch")
    draft = evidence["draft"]
    recovered_at = timezone.now()
    metadata["prompt_field_version"] = 2
    metadata["negative_prompt"] = ""
    metadata["legacy_negative_prompt_recovery"] = {
        "recovered": True,
        "recovered_at": recovered_at.isoformat(),
        "recovered_by": str(user.id),
        "creative_draft_id": str(draft.id),
        "client_key": evidence["client_key"],
        "original_value": current,
        "original_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(),
        "source": "creative_draft.continuity_zh",
    }
    shot.metadata = metadata
    shot.save(update_fields=("metadata", "updated_at"))
    return {"metadata": metadata, "updated_at": shot.updated_at.isoformat()}
