"""Model-agnostic consistency snapshots and canonical project locks."""
from __future__ import annotations

from django.db import transaction

from studio.models import ConsistencyLock, SeriesProject
from studio.services.bible_setting_evidence import (
    primary_setting_content,
    primary_setting_content_reference,
    read_entity_primary_settings,
)
from studio.services.visual_reference_policy import (
    character_visual_reference_decisions,
)


def normalized_translation_glossary(value: object) -> dict[str, str]:
    """Return deterministic non-empty glossary rows for prompt fingerprints."""

    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for source, target in sorted(value.items(), key=lambda item: str(item[0])):
        source_text = str(source)
        target_text = str(target)
        if source_text.strip() and target_text.strip():
            normalized[source_text] = target_text
    return normalized


def asset_snapshot(asset) -> dict | None:
    if not asset:
        return None
    return {
        "id": str(asset.id),
        "name": asset.name,
        "local_path": asset.local_path,
        "checksum": asset.checksum,
    }


def preferred_subject_asset(subject) -> object | None:
    """Return the explicitly anchored or deterministic first reference asset."""

    if subject.anchor_asset:
        return subject.anchor_asset
    return subject.reference_assets.all().order_by("kind", "name", "id").first()


def _bible_state(project):
    """Read one entity set and its verified current primaries for this operation."""
    entities = {}
    for kind, section in (("character", "characters"), ("scene", "scenes"), ("prop", "props")):
        rows = getattr(project, section).select_related("anchor_asset").prefetch_related("reference_assets")
        entities[kind] = list(rows)
    settings = read_entity_primary_settings(project_id=project.pk,
        entity_keys=[(kind, str(row.pk)) for kind, rows in entities.items() for row in rows])
    return entities, settings


def capture_consistency_snapshot(project: SeriesProject) -> dict:
    """Capture immutable prompt-time state used to reproduce a generation."""

    entities, settings = _bible_state(project)
    visual_decisions = character_visual_reference_decisions(entities["character"], primary_settings=settings)
    characters = []
    for item in entities["character"]:
        characters.append({
            "id": str(item.id),
            "slug": item.slug,
            "name": item.name,
            "speaker_id": item.stable_speaker_id,
            "age_appearance": item.age_appearance,
            "primary_setting": primary_setting_content(settings[("character", str(item.pk))]),
            "face_identity": item.face_identity,
            "wardrobe": item.wardrobe,
            "voice_profile": item.voice_profile,
            "personality": item.personality,
            "visual_reference_policy": (
                visual_decisions[str(item.pk)].as_dict()
            ),
            "negative_constraints": item.negative_constraints,
            "anchor_asset": asset_snapshot(item.anchor_asset),
            "reference_assets": [
                asset_snapshot(asset) for asset in item.reference_assets.all()
            ],
        })
    scenes = [{
        "id": str(item.id),
        "slug": item.slug,
        "name": item.name,
        "primary_setting": primary_setting_content(settings[("scene", str(item.pk))]),
        "location": item.location,
        "time_of_day": item.time_of_day,
        "lighting": item.lighting,
        "color_palette": item.color_palette,
        "ambient_sound": item.ambient_sound,
        "negative_constraints": item.negative_constraints,
        "anchor_asset": asset_snapshot(item.anchor_asset),
        "reference_assets": [
            asset_snapshot(asset) for asset in item.reference_assets.all()
        ],
    } for item in entities["scene"]]
    props = [{
        "id": str(item.id),
        "slug": item.slug,
        "name": item.name,
        "primary_setting": primary_setting_content(settings[("prop", str(item.pk))]),
        "material": item.material,
        "distinctive_marks": item.distinctive_marks,
        "continuity_state": item.continuity_state,
        "negative_constraints": item.negative_constraints,
        "anchor_asset": asset_snapshot(item.anchor_asset),
        "reference_assets": [
            asset_snapshot(asset) for asset in item.reference_assets.all()
        ],
    } for item in entities["prop"]]
    locks = [{
        "scope": lock.scope,
        "target_id": str(lock.target_id) if lock.target_id else None,
        "key": lock.lock_key,
        "value": lock.locked_value,
        "version": lock.version,
    } for lock in project.consistency_locks.filter(is_active=True)]
    style = None
    if project.style:
        style = {
            "id": str(project.style.id),
            "slug": project.style.slug,
            "name": project.style.name,
            "visual_prompt": project.style.visual_prompt,
            "negative_prompt": project.style.negative_prompt,
            "defaults": project.style.defaults,
        }
    return {
        "project_id": str(project.id),
        "language": project.language,
        "continuity_seed": project.continuity_seed,
        "continuity_summary": project.continuity_summary,
        "model": project.target_model,
        "prompt_settings": {
            "color_script": str(
                (project.production_settings or {}).get("color_script", ""),
            ),
            "negative_prompt": str(
                (project.production_settings or {}).get("negative_prompt", ""),
            ),
            "translation_glossary": normalized_translation_glossary(
                (project.production_settings or {}).get("translation_glossary", {}),
            ),
        },
        "style": style,
        "characters": characters,
        "scenes": scenes,
        "props": props,
        "locks": locks,
    }


@transaction.atomic
def sync_consistency_locks(project: SeriesProject) -> list[ConsistencyLock]:
    """Create or update canonical locks from project and Bible state."""

    # Match the existing setting writer's project-first serialization; failure
    # must not leave some locks updated and others at the previous revision.
    project = SeriesProject.objects.select_for_update().get(pk=project.pk)
    entities, settings = _bible_state(project)
    specs: list[tuple[str, object | None, str, dict]] = [
        (ConsistencyLock.Scope.GLOBAL, None, "production_format", {
            "language": project.language,
            "aspect_ratio": project.aspect_ratio,
            "width": project.width,
            "height": project.height,
            "fps": project.fps,
            "seed": project.continuity_seed,
            "model": project.target_model,
        }),
    ]
    if project.style:
        specs.append((ConsistencyLock.Scope.STYLE, project.style.id, "visual_style", {
            "slug": project.style.slug,
            "visual_prompt": project.style.visual_prompt,
            "negative_prompt": project.style.negative_prompt,
        }))
    for character in entities["character"]:
        specs.append((ConsistencyLock.Scope.CHARACTER, character.id, "identity", {
            "name": character.name,
            "speaker_id": character.stable_speaker_id,
            "primary_setting_ref": primary_setting_content_reference(settings[("character", str(character.pk))]),
            "face_identity": character.face_identity,
            "wardrobe": character.wardrobe,
            "voice_profile": character.voice_profile,
        }))
    for scene in entities["scene"]:
        specs.append((ConsistencyLock.Scope.SCENE, scene.id, "identity", {
            "name": scene.name,
            "primary_setting_ref": primary_setting_content_reference(settings[("scene", str(scene.pk))]),
            "lighting": scene.lighting,
            "color_palette": scene.color_palette,
        }))
    for prop in entities["prop"]:
        specs.append((ConsistencyLock.Scope.PROP, prop.id, "identity", {
            "name": prop.name,
            "primary_setting_ref": primary_setting_content_reference(settings[("prop", str(prop.pk))]),
            "material": prop.material,
            "distinctive_marks": prop.distinctive_marks,
            "continuity_state": prop.continuity_state,
        }))

    existing_slot_lock = project.consistency_locks.filter(
        scope=ConsistencyLock.Scope.GLOBAL,
        target_id=None,
        lock_key="reference_slots",
    ).first()
    existing_slot_value = existing_slot_lock.locked_value if existing_slot_lock else {}
    slot_value = dict(existing_slot_value.get("picture", {}))
    subject_value = dict(existing_slot_value.get("subject", {}))
    assets = {}
    for bible in (row for rows in entities.values() for row in rows):
        if bible.anchor_asset:
            assets[str(bible.anchor_asset_id)] = bible.anchor_asset
        for asset in bible.reference_assets.all():
            assets[str(asset.id)] = asset
    used_numbers = {
        int(value.get("slot", 0))
        for value in slot_value.values()
        if isinstance(value, dict)
    }
    next_number = max(used_numbers, default=0) + 1
    for asset_id, asset in assets.items():
        if asset_id not in slot_value:
            slot_value[asset_id] = {
                "slot": next_number,
                "label": f"<Picture {next_number}>",
                "name": asset.name,
            }
            next_number += 1
    used_subject_numbers = {
        int(value.get("slot", 0))
        for value in subject_value.values()
        if isinstance(value, dict)
    }
    next_subject_number = max(used_subject_numbers, default=0) + 1
    for prefix, bibles in entities.items():
        for bible in bibles:
            subject_key = f"{prefix}:{bible.id}"
            if subject_key in subject_value:
                existing = subject_value[subject_key]
                if isinstance(existing, dict) and existing.get("slot"):
                    existing["label"] = f"<Subject {int(existing['slot'])}>"
                continue
            preferred_asset = preferred_subject_asset(bible)
            subject_value[subject_key] = {
                "slot": next_subject_number,
                "label": f"<Subject {next_subject_number}>",
                "picture_asset_id": (
                    str(preferred_asset.id) if preferred_asset else None
                ),
                "name": bible.name,
            }
            next_subject_number += 1
    specs.append((ConsistencyLock.Scope.GLOBAL, None, "reference_slots", {
        "picture": slot_value,
        "subject": subject_value,
    }))

    active_keys = {
        (scope, str(target_id) if target_id else None, key)
        for scope, target_id, key, _value in specs
    }
    obsolete_lock_ids = [
        lock.id
        for lock in project.consistency_locks.filter(is_active=True)
        if (
            lock.scope,
            str(lock.target_id) if lock.target_id else None,
            lock.lock_key,
        ) not in active_keys
    ]
    if obsolete_lock_ids:
        ConsistencyLock.objects.filter(id__in=obsolete_lock_ids).update(
            is_active=False,
        )

    synced = []
    for scope, target_id, key, value in specs:
        lock, created = ConsistencyLock.objects.get_or_create(
            project=project,
            scope=scope,
            target_id=target_id,
            lock_key=key,
            defaults={"locked_value": value},
        )
        if not created and (lock.locked_value != value or not lock.is_active):
            lock.locked_value = value
            lock.version += 1
            lock.is_active = True
            lock.save(update_fields=(
                "locked_value", "version", "is_active", "updated_at",
            ))
        synced.append(lock)
    return synced
