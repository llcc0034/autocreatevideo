"""Shared original Episode/Shot content for explicit production review."""
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from studio.models import (Episode, PropBible, Shot,
    ShotCharacterRelation, ShotReferenceUse, ShotScriptSource)
from studio.services.shot_character_relations import shot_character_relations
from studio.services.shot_field_revisions import shot_field_revision_references


def storyboard_content(*, project_id, episodes, include_references=True, lock_shots=False) -> dict:
    """Read original facts; creative adoption excludes downstream media bindings."""
    episode_ids = [item.id for item in episodes]
    if len(set(episode_ids)) != len(episode_ids):
        raise ValidationError("分镜内容不能包含重复剧集")
    fields = ("id", "episode_id", "number", "title",
        "dialogue", "duration_ms", "episode_start_ms", "episode_end_ms", "media_type",
        "conditioning_mode", "include_audio", "scene_id", "requires_first_frame_continuity",
        "requires_last_frame_continuity", "metadata")
    shots = Shot.objects.only(*fields).order_by("number", "id").prefetch_related(
        Prefetch("props", queryset=PropBible.objects.only("id")),
    )
    if lock_shots:
        shots = shots.select_for_update()
    fetched = {
        item.id: item
        for item in Episode.objects.filter(project_id=project_id, pk__in=episode_ids).order_by("number", "id").prefetch_related(
            Prefetch("shots", queryset=shots),
        )
    }
    if set(fetched) != set(episode_ids):
        raise ValidationError("所选分镜的剧集已缺失或不属于本项目")
    # Freeze the new writer's original facts in the existing review receipt.
    # Values remain in M2 detail; neither an empty legacy character pool nor a
    # reference use is a substitute for explicitly visible/speaking characters.
    shot_ids = [shot.pk for episode in fetched.values() for shot in episode.shots.all()]
    field_revisions = shot_field_revision_references(shot_ids)
    sources, characters, uses = {}, {pk: [] for pk in shot_ids}, {pk: [] for pk in shot_ids}
    for row in ShotScriptSource.objects.filter(shot_id__in=shot_ids).values(
            "shot_id", "structure_id", "stage_id", "episode_number", "shot_number",
            "document_sha256", "materialization_sha256"):
        shot_id = row.pop("shot_id")
        sources[shot_id] = {**row, "structure_id": str(row["structure_id"]), "stage_id": str(row["stage_id"])}
    for row in ShotCharacterRelation.objects.filter(shot_id__in=shot_ids).order_by("role", "position").values(
            "shot_id", "role", "position", "character_id"):
        shot_id = row.pop("shot_id")
        characters[shot_id].append({**row, "character_id": str(row["character_id"])})
    use_fields = ("use_key", "position", "media_kind", "purpose", "setting_revision_id")
    if include_references:
        use_fields += ("source_mode", "reference_version_id", "asset_id", "asset_sha256")
    for row in ShotReferenceUse.objects.filter(shot_id__in=shot_ids).order_by("position").values("shot_id", *use_fields):
        shot_id = row.pop("shot_id")
        for field in ("setting_revision_id", "reference_version_id", "asset_id"):
            if field in row:
                row[field] = str(row[field]) if row[field] else None
        uses[shot_id].append(row)
    rows = []
    for episode_stub in episodes:
        episode = fetched[episode_stub.id]
        shots = []
        for shot in episode.shots.all():
            shot_character_relations({"character_relations": characters[shot.pk], "dialogue": shot.dialogue})
            shots.append({
                "id": str(shot.id),
                "number": shot.number,
                "title": shot.title,
                "dialogue": shot.dialogue,
                "duration_ms": shot.duration_ms,
                "episode_start_ms": shot.episode_start_ms,
                "episode_end_ms": shot.episode_end_ms,
                "media_type": shot.media_type,
                "conditioning_mode": shot.conditioning_mode,
                "include_audio": shot.include_audio,
                "scene_id": str(shot.scene_id) if shot.scene_id else None,
                "script_source": sources.get(shot.pk),
                **({"creative_field_revision": field_revisions[shot.pk]} if shot.pk in field_revisions else {}),
                "character_relations": characters[shot.pk],
                "reference_uses": uses[shot.pk],
                "prop_ids": sorted(str(item.pk) for item in shot.props.all()),
                "requires_first_frame_continuity": shot.requires_first_frame_continuity,
                "requires_last_frame_continuity": shot.requires_last_frame_continuity,
                "metadata": {
                    key: value for key, value in shot.metadata.items()
                    if key not in {"reference_bindings", "referenceBindingConfigured", "framing", "negative_prompt"}
                },
            })
        rows.append({
            "id": str(episode.id),
            "number": episode.number,
            "title": episode.title,
            "synopsis": episode.synopsis,
            "script": episode.script,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings,
            "shots": shots,
        })
    return {"episodes": rows}
