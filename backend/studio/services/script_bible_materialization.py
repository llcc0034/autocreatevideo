from __future__ import annotations

import hashlib
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from studio.models import CharacterBible, PropBible, SceneBible
from studio.services.consistency import sync_consistency_locks
from studio.services.json_contracts import canonical_json_bytes
from studio.services.script_bible import (
    SCRIPT_BIBLE_METADATA_KEY,
    SCRIPT_BIBLE_SCHEMA_VERSION,
    classify_script_bible_candidates,
    parse_script_bible,
    script_bible_reviewed_content_sha256,
)


def freeze_script_bible_candidates(content: str) -> dict:
    parsed = parse_script_bible(content)
    if not parsed.has_block:
        raise ValidationError("confirmed script 缺少 SCRIPT_BIBLE_V1 前置块")
    collections = {"characters": [], "scenes": [], "props": []}
    collection_by_type = {"character": "characters", "scene": "scenes", "prop": "props"}
    for candidate in parsed.candidates:
        collection = collection_by_type[candidate["entity_type"]]
        row = {
            key: candidate[key]
            for key in (
                "candidate_id", "declaration_fingerprint", "entity_type", "name",
                "description", "visual_identity_en", "occurrence", "recommended",
            )
        }
        row["reviewed_content_sha256"] = script_bible_reviewed_content_sha256(candidate)
        collections[collection].append(row)
    missing = [key for key, rows in collections.items() if not rows]
    if missing:
        raise ValidationError(f"SCRIPT_BIBLE_V1 缺少三类必需候选: {', '.join(missing)}")
    canonical = {
        "source_fingerprint": parsed.source_fingerprint,
        "collections": collections,
        "diagnostics": list(parsed.diagnostics),
    }
    return {
        "schema_version": 1,
        "kind": "script_bible_candidates",
        **canonical,
        "candidate_sha256": hashlib.sha256(canonical_json_bytes(canonical)).hexdigest(),
    }


def _stable_slug(candidate: dict, used: set[str]) -> str:
    prefix = {"character": "character", "scene": "scene", "prop": "prop"}[
        candidate["entity_type"]
    ]
    base = slugify(candidate["name"]) or prefix
    for salt in range(1000):
        seed = candidate["candidate_id"] if salt == 0 else f"{candidate['candidate_id']}:{salt}"
        value = f"{base[:67]}-{hashlib.sha256(seed.encode('ascii')).hexdigest()[:12]}"[:80]
        if value not in used:
            used.add(value)
            return value
    raise ValidationError("无法为 Bible 候选分配稳定 slug")


@transaction.atomic
def apply_frozen_script_bible(*, project, source_content: str, frozen: dict) -> dict:
    project = type(project).objects.select_for_update().get(pk=project.pk)
    current = freeze_script_bible_candidates(source_content)
    if current["source_fingerprint"] != frozen.get("source_fingerprint"):
        raise ValidationError("SCRIPT_BIBLE_V1 来源已漂移")
    if current["candidate_sha256"] != frozen.get("candidate_sha256"):
        raise ValidationError("SCRIPT_BIBLE_V1 候选内容已漂移")
    rows = {
        "characters": list(CharacterBible.objects.select_for_update().filter(project=project).order_by("id")),
        "scenes": list(SceneBible.objects.select_for_update().filter(project=project).order_by("id")),
        "props": list(PropBible.objects.select_for_update().filter(project=project).order_by("id")),
    }
    candidates = [
        row for key in ("characters", "scenes", "props")
        for row in current["collections"][key]
    ]
    classified = classify_script_bible_candidates(
        candidates,
        characters=rows["characters"], scenes=rows["scenes"], props=rows["props"],
    )
    conflicts = [item for item in classified if item.get("conflict_code")]
    if conflicts:
        raise ValidationError({
            "bible": [
                {"candidate_id": item["candidate_id"], "code": item["conflict_code"]}
                for item in conflicts
            ],
        })
    by_id = {str(row.id): row for values in rows.values() for row in values}
    used_slugs = {
        "character": {row.slug for row in rows["characters"]},
        "scene": {row.slug for row in rows["scenes"]},
        "prop": {row.slug for row in rows["props"]},
    }
    speaker_numbers = [
        int(row.stable_speaker_id[1:]) for row in rows["characters"]
        if re.fullmatch(r"S[1-9][0-9]*", row.stable_speaker_id)
    ]
    next_speaker = max(speaker_numbers, default=0) + 1
    models = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}
    created = []
    reused = []
    try:
        for candidate in classified:
            provenance = {
                "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "declaration_fingerprint": candidate["declaration_fingerprint"],
                "source_fingerprint": current["source_fingerprint"],
                "reviewed_snapshot": {
                    "name": candidate["name"],
                    "description": candidate["description"],
                    "visual_identity_en": candidate["visual_identity_en"],
                },
                "reviewed_content_sha256": script_bible_reviewed_content_sha256(candidate),
            }
            if candidate.get("existing"):
                obj = by_id[str(candidate["matched_bible_id"])]
                metadata = dict(obj.metadata or {})
                existing_provenance = metadata.get(SCRIPT_BIBLE_METADATA_KEY)
                update_fields = []
                if existing_provenance != provenance:
                    metadata[SCRIPT_BIBLE_METADATA_KEY] = provenance
                    obj.metadata = metadata
                    update_fields.append("metadata")
                if not obj.is_locked:
                    obj.is_locked = True
                    update_fields.append("is_locked")
                if update_fields:
                    obj.save(update_fields=(*update_fields, "updated_at"))
                reused.append(str(obj.id))
                continue
            kwargs = {
                "project": project,
                "slug": _stable_slug(candidate, used_slugs[candidate["entity_type"]]),
                "name": candidate["name"],
                "description": candidate["description"],
                "visual_identity": candidate["visual_identity_en"],
                "metadata": {SCRIPT_BIBLE_METADATA_KEY: provenance},
                "is_locked": True,
            }
            if candidate["entity_type"] == "character":
                kwargs["stable_speaker_id"] = f"S{next_speaker}"
                next_speaker += 1
            obj = models[candidate["entity_type"]].objects.create(**kwargs)
            created.append(str(obj.id))
    except IntegrityError as exc:
        raise ValidationError("并发写入导致 Bible 唯一键冲突；本批次已回滚") from exc
    sync_consistency_locks(project)
    return {
        "schema_version": 1,
        "source_fingerprint": current["source_fingerprint"],
        "candidate_sha256": current["candidate_sha256"],
        "created_ids": created,
        "reused_ids": reused,
    }
