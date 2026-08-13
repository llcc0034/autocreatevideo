"""MiniMax H3-native prompt construction.

The field order and timeline rules intentionally follow the H3 prompt-writing
contract. Keeping this logic isolated makes it possible to evolve model syntax
without changing API or persistence code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Iterable

from django.conf import settings as django_settings
from django.db.models import Prefetch

from studio.models import (
    CharacterBible,
    ConsistencyLock,
    Episode,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
)
from studio.services.reference_bindings import (
    AUDIO_REFERENCE_PURPOSES,
    DEFAULT_AUDIO_REFERENCE_PURPOSE,
    DEFAULT_VIDEO_REFERENCE_PURPOSE,
    ReferenceBindingError,
    VIDEO_REFERENCE_PURPOSES,
    effective_conditioning_mode,
    prompt_template_key,
    resolve_group_reference_bindings,
)
from studio.services.h3_prompt_optimizer import (
    H3PromptOptimizationBlocked,
    h3_prompt_optimizer_input_fingerprint,
    optimize_h3_video_prompt,
)
from studio.services.dialogue import DialogueContractError, normalize_dialogue_entry
from studio.services.prompt_translation import (
    canonical_target_collisions,
    confirmed_translation,
    confirmed_translation_fingerprint,
)


BASE_MODES = {Shot.H3Mode.T2VA, Shot.H3Mode.I2VA, Shot.H3Mode.FL2VA, Shot.H3Mode.L2VA}
H3_PROMPT_CHARACTER_LIMIT = 7_000
PROMPT_COMPILER_VERSION = 8
PROMPT_INPUT_FINGERPRINT_VERSION = 4
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_PROTECTED_AUTHORED_TEXT_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)

SDXL_BASE_MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"
_FRAMING_DIRECTIONS = {
    "特写": "a close-up",
    "近景": "a close shot",
    "中近景": "a medium close-up",
    "中景": "a medium shot",
    "全景": "a wide shot",
    "大全景": "an extreme wide shot",
    "俯拍": "a high-angle overhead shot",
    "航拍": "an aerial establishing shot",
}


class PromptBuildError(ValueError):
    pass


@dataclass(frozen=True)
class H3PromptResult:
    text: str
    payload: dict
    consistency_snapshot: dict
    total_duration_ms: int
    content_hash: str


@dataclass
class H3PromptOptions:
    mode: str = Shot.H3Mode.T2VA
    include_audio: bool = True
    language: str = "Chinese"
    duration_overrides: dict[str, int] = field(default_factory=dict)
    overall_soundscape: str = ""
    non_diegetic_music: str = ""
    extra_direction: str = ""
    single_shot_reference_bindings: dict | None = None
    single_shot_reference_assets: list | None = None
    consistency_snapshot: dict | None = None


def _compact(value: str | None) -> str:
    return " ".join((value or "").split())


def _normalized_translation_glossary(value: object) -> dict[str, str]:
    """Return the effective glossary in deterministic fingerprint order.

    Translation contexts ignore blank keys/values but otherwise preserve the
    authored spelling verbatim.  Mirror that contract here: sorting removes
    irrelevant JSON object order without collapsing whitespace or Unicode
    distinctions that can change which source term is matched.
    """

    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for source, target in sorted(value.items(), key=lambda item: str(item[0])):
        source_text = str(source)
        target_text = str(target)
        if source_text.strip() and target_text.strip():
            normalized[source_text] = target_text
    return normalized


def _translated(project_id, scope_type: str, scope_id, field_key: str, value: str | None) -> str:
    source = str(value or "")
    return confirmed_translation(
        project_id=project_id,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_text=source,
    )


def _dedupe_text(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = _compact(value)
        key = compact.casefold().rstrip(".。;；")
        if compact and key not in seen:
            seen.add(key)
            result.append(compact)
    return result


def _framing_direction(shot: Shot) -> str:
    raw = _compact(str((shot.metadata or {}).get("framing", "")))
    return _FRAMING_DIRECTIONS.get(raw, raw or "a motivated cinematic composition")


def _positive_continuity(shot: Shot) -> str:
    """Return only explicitly versioned positive continuity instructions.

    Older clients stored their negative prompt in ``continuity_notes``.  Treating
    that legacy value as positive H3 prose made requests such as "extra limbs"
    part of the generation prompt.  New clients mark the split-field schema and
    keep positive and exclusion semantics separate.
    """
    metadata = shot.metadata or {}
    try:
        prompt_field_version = int(metadata.get("prompt_field_version", 0) or 0)
    except (TypeError, ValueError):
        prompt_field_version = 0
    if prompt_field_version < 2:
        return ""
    return _compact(_translated(
        shot.episode.project_id, "shot", shot.id,
        "shot.continuity_notes", shot.continuity_notes,
    ))


def _shot_negative_constraints(shot: Shot) -> str:
    metadata = shot.metadata or {}
    try:
        prompt_field_version = int(metadata.get("prompt_field_version", 0) or 0)
    except (TypeError, ValueError):
        prompt_field_version = 0
    if prompt_field_version >= 2:
        return _compact(str(metadata.get("negative_prompt", "")))
    # Backward compatibility for records written before the field split.
    return _compact(shot.continuity_notes)


def _timestamp(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _scoped_shot_summary(shots: list[Shot], durations: list[int]) -> str:
    """Summarize only the shots represented by the current prompt artifact.

    Episode synopses commonly describe the entire episode.  Reusing them in a
    per-shot Ref2VA child prompt leaks other shots and their episode-level time
    ranges into an otherwise correctly scoped task.  Keep the summary concise,
    deterministic, and based on the same local timeline used by the detailed
    description and workflow duration.
    """
    elapsed = 0
    entries = []
    for index, (shot, duration) in enumerate(zip(shots, durations, strict=True), start=1):
        end = elapsed + duration
        label = _compact(
            shot.title or shot.detailed_description or shot.narrative or shot.action
        ) or f"Shot {index}"
        entries.append(
            f"[Shot {index}] {_timestamp(elapsed)}-{_timestamp(end)}: {label}"
        )
        elapsed = end
    return "; ".join(entries)


def _asset_snapshot(asset) -> dict | None:
    if not asset:
        return None
    return {
        "id": str(asset.id),
        "name": asset.name,
        "local_path": asset.local_path,
        "checksum": asset.checksum,
    }


def capture_consistency_snapshot(project: SeriesProject) -> dict:
    """Capture immutable prompt-time state used to reproduce a generation."""
    characters = []
    for item in project.characters.select_related("anchor_asset").prefetch_related("reference_assets").all():
        characters.append({
            "id": str(item.id),
            "slug": item.slug,
            "name": item.name,
            "speaker_id": item.stable_speaker_id,
            "age_appearance": item.age_appearance,
            "visual_identity": item.visual_identity,
            "face_identity": item.face_identity,
            "wardrobe": item.wardrobe,
            "voice_profile": item.voice_profile,
            "personality": item.personality,
            "negative_constraints": item.negative_constraints,
            "anchor_asset": _asset_snapshot(item.anchor_asset),
            "reference_assets": [_asset_snapshot(asset) for asset in item.reference_assets.all()],
        })
    scenes = [{
        "id": str(item.id), "slug": item.slug, "name": item.name,
        "visual_identity": item.visual_identity, "location": item.location,
        "time_of_day": item.time_of_day, "lighting": item.lighting,
        "color_palette": item.color_palette, "ambient_sound": item.ambient_sound,
        "negative_constraints": item.negative_constraints,
        "anchor_asset": _asset_snapshot(item.anchor_asset),
        "reference_assets": [_asset_snapshot(asset) for asset in item.reference_assets.all()],
    } for item in project.scenes.select_related("anchor_asset").prefetch_related("reference_assets").all()]
    props = [{
        "id": str(item.id), "slug": item.slug, "name": item.name,
        "visual_identity": item.visual_identity, "material": item.material,
        "distinctive_marks": item.distinctive_marks, "continuity_state": item.continuity_state,
        "negative_constraints": item.negative_constraints,
        "anchor_asset": _asset_snapshot(item.anchor_asset),
        "reference_assets": [_asset_snapshot(asset) for asset in item.reference_assets.all()],
    } for item in project.props.select_related("anchor_asset").prefetch_related("reference_assets").all()]
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
            "id": str(project.style.id), "slug": project.style.slug, "name": project.style.name,
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
            "color_script": str((project.production_settings or {}).get("color_script", "")),
            "negative_prompt": str((project.production_settings or {}).get("negative_prompt", "")),
            "translation_glossary": _normalized_translation_glossary(
                (project.production_settings or {}).get("translation_glossary", {}),
            ),
        },
        "style": style,
        "characters": characters,
        "scenes": scenes,
        "props": props,
        "locks": locks,
    }


def sync_consistency_locks(project: SeriesProject) -> list[ConsistencyLock]:
    """Create/update canonical locks from project and bible state."""
    specs: list[tuple[str, object | None, str, dict]] = [
        (ConsistencyLock.Scope.GLOBAL, None, "production_format", {
            "language": project.language, "aspect_ratio": project.aspect_ratio,
            "width": project.width, "height": project.height, "fps": project.fps,
            "seed": project.continuity_seed, "model": project.target_model,
        })
    ]
    if project.style:
        specs.append((ConsistencyLock.Scope.STYLE, project.style.id, "visual_style", {
            "slug": project.style.slug,
            "visual_prompt": project.style.visual_prompt,
            "negative_prompt": project.style.negative_prompt,
        }))
    for character in project.characters.all():
        specs.append((ConsistencyLock.Scope.CHARACTER, character.id, "identity", {
            "name": character.name, "speaker_id": character.stable_speaker_id,
            "visual_identity": character.visual_identity, "face_identity": character.face_identity,
            "wardrobe": character.wardrobe, "voice_profile": character.voice_profile,
        }))
    for scene in project.scenes.all():
        specs.append((ConsistencyLock.Scope.SCENE, scene.id, "identity", {
            "name": scene.name, "visual_identity": scene.visual_identity,
            "lighting": scene.lighting, "color_palette": scene.color_palette,
        }))
    for prop in project.props.all():
        specs.append((ConsistencyLock.Scope.PROP, prop.id, "identity", {
            "name": prop.name, "visual_identity": prop.visual_identity,
            "material": prop.material, "distinctive_marks": prop.distinctive_marks,
            "continuity_state": prop.continuity_state,
        }))

    # Preserve picture labels forever so later episodes cannot silently reorder identities.
    existing_slot_lock = project.consistency_locks.filter(
        scope=ConsistencyLock.Scope.GLOBAL, target_id=None, lock_key="reference_slots"
    ).first()
    existing_slot_value = existing_slot_lock.locked_value if existing_slot_lock else {}
    slot_value = dict(existing_slot_value.get("picture", {}))
    # A subject slot is deliberately separate from an asset slot.  One asset can
    # be used by more than one prompt, while a character/prop identity must keep
    # its own stable subject label for the lifetime of a series.
    subject_value = dict(existing_slot_value.get("subject", {}))
    assets = {}
    for bible in (*project.characters.all(), *project.scenes.all(), *project.props.all()):
        if bible.anchor_asset:
            assets[str(bible.anchor_asset_id)] = bible.anchor_asset
        for asset in bible.reference_assets.all():
            assets[str(asset.id)] = asset
    for shot in Shot.objects.filter(episode__project=project).prefetch_related("reference_assets"):
        for asset in shot.reference_assets.all():
            assets[str(asset.id)] = asset
    used_numbers = {int(value.get("slot", 0)) for value in slot_value.values() if isinstance(value, dict)}
    next_number = max(used_numbers, default=0) + 1
    for asset_id, asset in assets.items():
        if asset_id not in slot_value:
            slot_value[asset_id] = {"slot": next_number, "label": f"<Picture {next_number}>", "name": asset.name}
            next_number += 1
    used_subject_numbers = {
        int(value.get("slot", 0)) for value in subject_value.values()
        if isinstance(value, dict)
    }
    next_subject_number = max(used_subject_numbers, default=0) + 1
    for prefix, bibles in (
        ("character", project.characters.all()),
        ("scene", project.scenes.all()),
        ("prop", project.props.all()),
    ):
        for bible in bibles:
            subject_key = f"{prefix}:{bible.id}"
            if subject_key in subject_value:
                existing = subject_value[subject_key]
                if isinstance(existing, dict) and existing.get("slot"):
                    existing["label"] = f"<Subject {int(existing['slot'])}>"
                continue
            preferred_asset = _preferred_subject_asset(bible)
            # The official Ref2VA rewrite grammar uses <Subject N> for every
            # reusable visible unit, including environments and props.
            label_prefix = "Subject"
            subject_value[subject_key] = {
                "slot": next_subject_number,
                "label": f"<{label_prefix} {next_subject_number}>",
                # Save the selected picture rather than recalculating it on
                # later episodes: a newly-added reference must not make an old
                # character silently point at a different visual identity.
                "picture_asset_id": str(preferred_asset.id) if preferred_asset else None,
                "name": bible.name,
            }
            next_subject_number += 1
    specs.append((ConsistencyLock.Scope.GLOBAL, None, "reference_slots", {
        "picture": slot_value,
        "subject": subject_value,
    }))

    synced = []
    for scope, target_id, key, value in specs:
        lock, created = ConsistencyLock.objects.get_or_create(
            project=project, scope=scope, target_id=target_id, lock_key=key,
            defaults={"locked_value": value},
        )
        if not created and lock.locked_value != value:
            lock.locked_value = value
            lock.version += 1
            lock.is_active = True
            lock.save(update_fields=("locked_value", "version", "is_active", "updated_at"))
        synced.append(lock)
    return synced


def load_shots(ids: Iterable[str]) -> list[Shot]:
    shots = list(
        Shot.objects.filter(id__in=list(ids))
        .select_related("episode__project__style", "scene")
        .prefetch_related("characters", "props", "reference_assets")
    )
    order = {str(value): index for index, value in enumerate(ids)}
    shots.sort(key=lambda shot: order[str(shot.id)])
    return shots


def _shot_duration(shot: Shot, options: H3PromptOptions) -> int:
    value = options.duration_overrides.get(str(shot.id), shot.duration_ms)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise PromptBuildError(f"分镜 {shot.id} 的时长不是整数毫秒") from exc
    if value < 100:
        raise PromptBuildError("每个分镜时长必须至少 100ms，才能保持严格递增时间戳")
    return value


def _semantic_key(value: str | None) -> str:
    return _compact(value).casefold().rstrip(".。;；")


def _semantic_override(value: str, existing_values: Iterable[str | None]) -> str:
    """Keep only a one-off override that changes the compiled prompt.

    The editor submits saved sound/music values again as generation overrides.
    Treating that transport duplication as a distinct input used to reorder the
    preview soundscape even though the user had not changed any content.
    """

    compact = _compact(value)
    if not compact:
        return ""
    key = _semantic_key(compact)
    existing = {
        _semantic_key(candidate)
        for raw in existing_values
        if (candidate := _compact(raw))
    }
    return "" if key in existing else compact


def _director_content_tokens(value: object) -> set[str]:
    """Return coarse lexical units for fail-closed positive/negative overlap."""

    text = str(value or "").casefold()
    return set(re.findall(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", text))


def _negative_repeats_positive(negative: str, positive_values: Iterable[str]) -> bool:
    negative_tokens = _director_content_tokens(negative)
    positive_tokens = _director_content_tokens(" ".join(str(value or "") for value in positive_values))
    if not negative_tokens or not positive_tokens:
        return False
    shared = negative_tokens & positive_tokens
    return len(shared) >= 6 and (
        len(shared) / max(1, min(len(negative_tokens), len(positive_tokens))) >= 0.65
    )


def prompt_input_fingerprint(
    project: SeriesProject,
    shots: list[Shot],
    options: H3PromptOptions,
    *,
    prompt_type: str,
    consistency_snapshot: dict,
    reference_bindings: list[dict],
) -> str:
    """Hash every persisted input that can affect a prompt artifact.

    This is deliberately more conservative than the final prompt hash.  It lets
    production reuse the exact preview artifact without calling a semantic
    optimizer a second time, while any edited shot/Bible/reference/config value
    forces a new immutable revision.
    """

    sound_values = [
        value
        for shot in shots
        for value in (
            shot.scene.ambient_sound if shot.scene else "",
            shot.sound_effects,
        )
    ]
    music_values = [shot.music for shot in shots]
    direction_values = [
        value for shot in shots for value in (
            shot.narrative, shot.detailed_description, shot.action, shot.camera,
        )
    ]
    effective_audio = bool(options.include_audio and prompt_type == "video")
    translation_scope_ids = {project.id}
    for shot in shots:
        translation_scope_ids.add(shot.id)
        if shot.scene_id:
            translation_scope_ids.add(shot.scene_id)
        translation_scope_ids.update(item.id for item in shot.characters.all())
        translation_scope_ids.update(item.id for item in shot.props.all())
    payload = {
        "version": PROMPT_INPUT_FINGERPRINT_VERSION,
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "prompt_type": prompt_type,
        "mode": options.mode,
        "language": options.language,
        "include_audio": effective_audio,
        "effective_durations_ms": [
            _shot_duration(shot, options) if prompt_type == "video" else 0
            for shot in shots
        ],
        "overall_soundscape_override": (
            _semantic_override(options.overall_soundscape, sound_values) if effective_audio else ""
        ),
        "non_diegetic_music_override": (
            _semantic_override(options.non_diegetic_music, music_values) if effective_audio else ""
        ),
        "extra_direction": _semantic_override(options.extra_direction, direction_values),
        "optimizer_fingerprint": (
            h3_prompt_optimizer_input_fingerprint() if prompt_type == "video" else "not-applicable"
        ),
        # Keep this explicit as well as in the consistency snapshot.  Some
        # callers supply a pre-captured snapshot for a whole batch; the direct
        # field makes glossary edits fail closed even if a legacy/custom
        # snapshot omitted project prompt settings.
        "translation_glossary": _normalized_translation_glossary(
            (project.production_settings or {}).get("translation_glossary", {}),
        ),
        "confirmed_translation_fingerprint": confirmed_translation_fingerprint(
            project_id=project.id, scope_ids=translation_scope_ids,
        ),
        "consistency_snapshot": consistency_snapshot,
        "reference_bindings": reference_bindings,
        "shots": [
            {
                "id": str(shot.id),
                "number": shot.number,
                "title": shot.title,
                "narrative": shot.narrative,
                "detailed_description": shot.detailed_description,
                "action": shot.action,
                "camera": shot.camera,
                "dialogue": shot.dialogue,
                "sound_effects": shot.sound_effects,
                "music": shot.music,
                "media_type": shot.media_type,
                "h3_mode": shot.h3_mode,
                "include_audio": shot.include_audio,
                "scene_id": str(shot.scene_id) if shot.scene_id else None,
                "character_ids": [str(item.id) for item in shot.characters.all()],
                "prop_ids": [str(item.id) for item in shot.props.all()],
                "reference_assets": [
                    _asset_snapshot(asset) for asset in shot.reference_assets.all()
                ],
                "reference_bindings": shot.reference_bindings or {},
                "continuity_notes": shot.continuity_notes,
                "metadata": shot.metadata or {},
            }
            for shot in shots
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _character_direction(character: CharacterBible) -> str:
    parts = [
        _translated(character.project_id, "character", character.id, "character.age_appearance", character.age_appearance),
        _translated(character.project_id, "character", character.id, "character.visual_identity", character.visual_identity),
        _translated(character.project_id, "character", character.id, "character.face_identity", character.face_identity),
        _translated(character.project_id, "character", character.id, "character.wardrobe", character.wardrobe),
    ]
    return "; ".join(_dedupe_text(parts))


def _scene_direction(scene: SceneBible | None) -> str:
    if not scene:
        return ""
    parts = [
        _translated(scene.project_id, "scene", scene.id, "scene.visual_identity", scene.visual_identity),
        _translated(scene.project_id, "scene", scene.id, "scene.location", scene.location),
        _translated(scene.project_id, "scene", scene.id, "scene.time_of_day", scene.time_of_day),
        _translated(scene.project_id, "scene", scene.id, "scene.lighting", scene.lighting),
        _translated(scene.project_id, "scene", scene.id, "scene.color_palette", scene.color_palette),
    ]
    return "; ".join(_dedupe_text(parts))


def _prop_direction(prop: PropBible) -> str:
    parts = [
        _translated(prop.project_id, "prop", prop.id, "prop.visual_identity", prop.visual_identity),
        _translated(prop.project_id, "prop", prop.id, "prop.material", prop.material),
        _translated(prop.project_id, "prop", prop.id, "prop.distinctive_marks", prop.distinctive_marks),
        _translated(prop.project_id, "prop", prop.id, "prop.continuity_state", prop.continuity_state),
    ]
    return "; ".join(_dedupe_text(parts))


def _preferred_subject_asset(subject) -> object | None:
    """Return the canonical visual reference for a character or prop.

    Anchor assets are the intentional source of truth.  When a bible has no
    anchor, choose its first reference deterministically so the slot can be
    written to the project lock and reused by every later episode.
    """
    if subject.anchor_asset:
        return subject.anchor_asset
    return subject.reference_assets.all().order_by("kind", "name", "id").first()


def _dialogue_lines(
    shot: Shot,
    language: str,
    reference_labels: dict[tuple[str, str], str] | None = None,
) -> list[str]:
    reference_labels = reference_labels or {}
    lines = []
    for index, raw_entry in enumerate(shot.dialogue or []):
        try:
            entry = normalize_dialogue_entry(raw_entry, index=index)
        except DialogueContractError as exc:
            raise PromptBuildError(f"分镜 {shot.number} 的 {exc}") from exc
        speaker_id = _compact(str(entry.get("speaker_id", "")))
        # Dialogue is the one part of an H3 prompt that must preserve the
        # author's original language and punctuation verbatim.
        text = entry["text"]
        if not speaker_id or not text:
            continue
        known = {character.stable_speaker_id for character in shot.characters.all()}
        if speaker_id not in known:
            raise PromptBuildError(f"分镜 {shot.number} 的 speaker_id {speaker_id} 未绑定到该分镜人物")
        character = next(item for item in shot.characters.all() if item.stable_speaker_id == speaker_id)
        if character.voice_profile:
            speaker_description = _compact(_translated(
                character.project_id, "character", character.id,
                "character.voice_profile", character.voice_profile,
            ))
        else:
            speaker_description = _compact(_translated(
                character.project_id, "character", character.id,
                "character.personality", character.personality,
            ))
        subject_label = reference_labels.get(("character", str(character.id)))
        if subject_label:
            # Ref2VA distinguishes the referenced visual identity from the
            # stable vocal-source ID.  Keeping both on the speaking action is
            # essential in multi-character shots.
            prefix = subject_label
        elif speaker_description:
            prefix = f"The on-screen character with {speaker_description}"
        else:
            prefix = "The on-screen character"
        delivery_source = str(entry.get("delivery", ""))
        delivery = _translated(
            shot.episode.project_id, "shot", shot.id,
            f"shot.dialogue.{index}.delivery", delivery_source,
        ).strip()
        if entry.get("voiceover") is True:
            delivery_clause = f", {delivery}" if delivery else ""
            lines.append(
                f"{prefix} ({speaker_id}) says in an off-screen voiceover{delivery_clause}: "
                f"<d>[{language}] {text}</d>"
            )
            visible_subject = subject_label or "the corresponding visible on-screen character"
            lines.append(
                f"During this voiceover, {visible_subject} ({speaker_id}) keeps their lips closed "
                "and does not speak on camera"
            )
        else:
            delivery_clause = f" {delivery}" if delivery else ""
            lines.append(
                f"{prefix} ({speaker_id}) says{delivery_clause}: <d>[{language}] {text}</d>"
            )
    return lines


def _shot_description(
    shot: Shot,
    options: H3PromptOptions,
    style_prompt: str,
    reference_labels: dict[tuple[str, str], str] | None = None,
    *,
    is_first_shot: bool = False,
    color_script: str = "",
    reference_detail_entities: set[tuple[str, str]] | None = None,
) -> str:
    reference_labels = reference_labels or {}
    reference_detail_entities = reference_detail_entities or set()
    parts: list[str] = []
    opening = []
    if is_first_shot and style_prompt:
        opening.append(_compact(style_prompt))
    opening.append(_framing_direction(shot))
    if opening:
        parts.append(", ".join(opening).rstrip(".。") + " establishes the opening composition")
    if is_first_shot and color_script:
        parts.append(f"The color and lighting design follows {_compact(color_script)}")
    if shot.scene:
        key = ("scene", str(shot.scene_id))
        label = reference_labels.get(key)
        direction = _scene_direction(shot.scene)
        scene_text = (
            f"{label}, preserving {direction}" if label and key in reference_detail_entities and direction
            else label or direction
        )
        if scene_text:
            parts.append(f"The environment is {scene_text}")
    character_values = []
    for item in shot.characters.all():
        key = ("character", str(item.id))
        label = reference_labels.get(key)
        direction = _character_direction(item)
        value = (
            f"{label}, preserving {direction}" if label and key in reference_detail_entities and direction
            else label or direction
        )
        if value:
            character_values.append(value)
    character_text = ", ".join(character_values)
    if character_text:
        if is_first_shot:
            parts.append(f"The opening composition positions the visible subjects as {character_text}")
        else:
            parts.append(f"The visible subjects are {character_text}")
    prop_values = []
    for item in shot.props.all():
        key = ("prop", str(item.id))
        label = reference_labels.get(key)
        direction = _prop_direction(item)
        value = (
            f"{label}, preserving {direction}" if label and key in reference_detail_entities and direction
            else label or direction
        )
        if value:
            prop_values.append(value)
    prop_text = ", ".join(prop_values)
    if prop_text:
        parts.append(f"The key props are {prop_text}")
    # UI v1 wrote the same text to both fields. Keep distinct user intent when
    # present, but never repeat an identical action twice.
    narrative = _translated(
        shot.episode.project_id, "shot", shot.id, "shot.narrative", shot.narrative,
    )
    detailed_description = _translated(
        shot.episode.project_id, "shot", shot.id,
        "shot.detailed_description", shot.detailed_description,
    )
    action = _translated(
        shot.episode.project_id, "shot", shot.id, "shot.action", shot.action,
    )
    camera = _translated(
        shot.episode.project_id, "shot", shot.id, "shot.camera", shot.camera,
    )
    sound_effects = _translated(
        shot.episode.project_id, "shot", shot.id, "shot.sound_effects", shot.sound_effects,
    )
    # The review-gated high-detail field is a complete one-shot director
    # description.  Once it is applied, prefer it to the compact storyboard
    # prose so the compiler does not repeat the same event in two competing
    # descriptions.  References, dialogue, soundscape and positive continuity
    # remain independently classified below.
    if detailed_description:
        parts.append(_compact(detailed_description))
    else:
        parts.extend(_dedupe_text([narrative, action]))
        if shot.camera:
            parts.append(_compact(camera))
    if options.include_audio and shot.sound_effects:
        parts.append(f"Synchronized diegetic sound: {_compact(sound_effects)}")
    dialogue = _dialogue_lines(shot, options.language, reference_labels)
    if dialogue and options.include_audio:
        parts.extend(dialogue)
    continuity = _positive_continuity(shot)
    if continuity:
        parts.append(f"Maintain this visible continuity: {continuity}")
    return ". ".join(value.rstrip(".。") for value in _dedupe_text(parts)) + "."


def _soundscape(shots: list[Shot], options: H3PromptOptions) -> str:
    if not options.include_audio:
        return "N/A"
    source_values = []
    translated_values = []
    for shot in shots:
        if shot.scene and shot.scene.ambient_sound:
            source_values.append(shot.scene.ambient_sound)
            translated_values.append(_compact(_translated(
                shot.scene.project_id, "scene", shot.scene.id,
                "scene.ambient_sound", shot.scene.ambient_sound,
            )))
        if shot.sound_effects:
            source_values.append(shot.sound_effects)
            translated_values.append(_compact(_translated(
                shot.episode.project_id, "shot", shot.id,
                "shot.sound_effects", shot.sound_effects,
            )))
    # The editor submits the saved source audio again as a generation option.
    # Compare that transport value with the authoritative *source* before
    # replacing confirmed fields with their English overlays.  Comparing a
    # Chinese source option to the translated values misclassified it as a new
    # direction and produced ``Chinese; English`` in the final prompt.  Also
    # compare with the effective values so an explicitly entered English value
    # identical to an overlay is not duplicated.
    override = _semantic_override(
        options.overall_soundscape,
        [*source_values, *translated_values],
    )
    values = ([override] if override else []) + translated_values
    return "; ".join(_dedupe_text(values)) or (
        "Natural room tone continues under localized movement, cloth, footsteps, and object sounds "
        "synchronized to the visible actions."
    )


def _music(shots: list[Shot], options: H3PromptOptions) -> str:
    if not options.include_audio:
        return "N/A"
    source_values = [shot.music for shot in shots if shot.music]
    translated_values = [
        _compact(_translated(
            shot.episode.project_id, "shot", shot.id, "shot.music", shot.music,
        ))
        for shot in shots if shot.music
    ]
    override = _semantic_override(
        options.non_diegetic_music,
        [*source_values, *translated_values],
    )
    values = ([override] if override else []) + translated_values
    return "; ".join(_dedupe_text(values)) or "N/A"


def _reference_slot_maps(snapshot: dict) -> tuple[dict, dict]:
    for lock in snapshot.get("locks", []):
        if lock.get("scope") == ConsistencyLock.Scope.GLOBAL and lock.get("key") == "reference_slots":
            value = lock.get("value") or {}
            return value.get("picture", {}), value.get("subject", {})
    return {}, {}


def _ref2va_subjects(shots: list[Shot], reference_assets: list[dict], snapshot: dict) -> list[dict]:
    """Build locked <Subject N> -> <Picture N> bindings for Ref2VA fields."""
    _, subject_slots = _reference_slot_maps(snapshot)
    pictures_by_asset = {item["id"]: item for item in reference_assets}
    subjects: list[dict] = []
    seen: set[str] = set()
    next_fallback_slot = 1
    for kind, getter, describe in (
        ("character", lambda shot: shot.characters.all(), _character_direction),
        ("prop", lambda shot: shot.props.all(), _prop_direction),
    ):
        for shot in shots:
            for bible in getter(shot):
                key = f"{kind}:{bible.id}"
                if key in seen:
                    continue
                seen.add(key)
                locked = subject_slots.get(key, {})
                preferred = _preferred_subject_asset(bible)
                asset_id = locked.get("picture_asset_id") or (str(preferred.id) if preferred else None)
                picture = pictures_by_asset.get(asset_id)
                if not picture:
                    raise PromptBuildError(
                        f"Ref2VA requires a stable anchor_asset or reference_assets image for {bible.name}"
                    )
                slot = int(locked.get("slot", 0)) or next_fallback_slot
                next_fallback_slot = max(next_fallback_slot, slot + 1)
                subjects.append({
                    "key": key,
                    "slot": slot,
                    "subject_label": locked.get("label") or f"<Subject {slot}>",
                    "picture_label": picture["input_label"],
                    "picture_asset_id": asset_id,
                    "name": bible.name,
                    "kind": kind,
                    "direction": describe(bible),
                })
    subjects.sort(key=lambda item: (item["slot"], item["key"]))
    return subjects


def _subject_definitions(subjects: list[dict]) -> list[str]:
    return [
        f"{item['subject_label']} is defined by {item['picture_label']}: {item['direction']}"
        for item in subjects
    ]


def _retention_analysis(subjects: list[dict]) -> list[str]:
    lines = []
    for item in subjects:
        if item["kind"] == "character":
            retained = "face identity, body proportions, hairstyle, wardrobe, color palette, and distinguishing features"
        else:
            retained = "silhouette, material, color palette, distinctive marks, scale, and continuity state"
        lines.append(
            f"{item['subject_label']} / {item['picture_label']}: retain {item['name']}'s {retained}; "
            "change only pose, expression, camera, and story-required action stated in the timeline."
        )
    return lines


def _reference_assets(shots: list[Shot], snapshot: dict) -> list[dict]:
    assets = {}
    for shot in shots:
        for asset in shot.reference_assets.all():
            assets[str(asset.id)] = asset
        for bible in (*shot.characters.all(), *shot.props.all()):
            if bible.anchor_asset:
                assets[str(bible.anchor_asset_id)] = bible.anchor_asset
            for asset in bible.reference_assets.all():
                assets[str(asset.id)] = asset
        if shot.scene:
            if shot.scene.anchor_asset:
                assets[str(shot.scene.anchor_asset_id)] = shot.scene.anchor_asset
            for asset in shot.scene.reference_assets.all():
                assets[str(asset.id)] = asset
    slot_map, _ = _reference_slot_maps(snapshot)
    result = []
    for asset_id, asset in assets.items():
        slot = slot_map.get(asset_id, {})
        result.append({
            "id": asset_id, "name": asset.name,
            "local_path": asset.file.path if asset.file else asset.local_path,
            "checksum": asset.checksum, "slot": slot.get("slot"), "label": slot.get("label"),
        })
    result.sort(key=lambda value: (value["slot"] is None, value["slot"] or 10_000, value["id"]))
    for index, value in enumerate(result, start=1):
        value["input_slot"] = index
        value["input_label"] = f"<Picture {index}>"
    return result


def _video_reference_purpose(item: dict) -> str:
    purpose = str(item.get("purpose") or DEFAULT_VIDEO_REFERENCE_PURPOSE)
    if purpose not in VIDEO_REFERENCE_PURPOSES:
        raise PromptBuildError(f"Unsupported Ref2VA video purpose: {purpose}")
    return purpose


def _audio_reference_purpose(item: dict, field: str = "purpose") -> str:
    purpose = str(item.get(field) or DEFAULT_AUDIO_REFERENCE_PURPOSE)
    if purpose not in AUDIO_REFERENCE_PURPOSES:
        raise PromptBuildError(f"Unsupported Ref2VA audio purpose: {purpose}")
    return purpose


def _classified_reference_fields(
    shots: list[Shot],
    bindings: list[dict],
    subject_labels: dict[tuple[str, str], str] | None = None,
) -> dict:
    """Create separate identity, environment, and prop prompt sections."""
    subject_labels = subject_labels or {}
    characters = {
        str(item.id): item for shot in shots for item in shot.characters.all()
    }
    scenes = {
        str(shot.scene.id): shot.scene for shot in shots if shot.scene
    }
    props = {str(item.id): item for shot in shots for item in shot.props.all()}
    binding_lines = []
    character_definitions, character_retention = [], []
    environment_definitions, environment_retention = [], []
    prop_definitions, prop_retention = [], []
    media_definitions, media_retention = [], []
    bound_entities: set[tuple[str, str]] = set()
    for item in bindings:
        label = item["input_label"]
        entity_label = item["entity_label"]
        category = item["category"]
        if item.get("entity_id"):
            bound_entities.add((category, item["entity_id"]))
        if category == "character":
            bible = characters[item["entity_id"]]
            character_definitions.append(
                f"{entity_label} is the on-screen character defined by {label}: {_character_direction(bible)}"
            )
            character_retention.append(
                f"{entity_label}: fully_preserved — preserve face identity, body proportions, hairstyle, "
                "wardrobe colors, materials, and distinguishing marks; change only the scripted pose, "
                "expression, and action."
            )
        elif category == "scene":
            bible = scenes[item["entity_id"]]
            environment_definitions.append(
                f"{entity_label} is the environment defined by {label}: {_scene_direction(bible)}"
            )
            retained = _dedupe_text([
                "spatial layout and architecture",
                f"time of day: {_compact(bible.time_of_day)}" if bible.time_of_day else "",
                f"lighting: {_compact(_translated(bible.project_id, 'scene', bible.id, 'scene.lighting', bible.lighting))}" if bible.lighting else "",
                f"color palette: {_compact(_translated(bible.project_id, 'scene', bible.id, 'scene.color_palette', bible.color_palette))}" if bible.color_palette else "",
            ])
            environment_retention.append(
                f"{entity_label}: fully_preserved — preserve {'; '.join(retained)} and fixed object placement; "
                "do not transfer people or wardrobe from the environment source."
            )
        elif category == "prop":
            bible = props[item["entity_id"]]
            prop_definitions.append(
                f"{entity_label} is the visible prop defined by {label}: {_prop_direction(bible)}"
            )
            prop_retention.append(
                f"{entity_label}: fully_preserved — preserve silhouette, material, scale, color, distinctive "
                "marks, and continuity state; change only scripted position or interaction."
            )
        elif category == "video":
            video_purpose = _video_reference_purpose(item)
            detail = _compact(item.get("prompt_hint") or item.get("description"))
            detail_suffix = f" Declared guidance: {detail}." if detail else ""
            if video_purpose == "video_editing":
                media_definitions.append(
                    f"{label} is the source video for the target video edit.{detail_suffix}"
                )
                media_retention.append(
                    f"{label} (source video edit): partially_preserved - preserve the source timeline, "
                    "subject continuity, camera geometry, and all content not explicitly changed by the script."
                )
            elif video_purpose == "video_continuation":
                media_definitions.append(
                    f"{label} is the source video whose ending state is continued by the target video."
                    f"{detail_suffix}"
                )
                media_retention.append(
                    f"{label} (continuation source): fully_preserved - preserve its ending composition, "
                    "subject states, motion direction, lighting, and acoustic continuity as the new starting state."
                )
            else:
                media_definitions.append(
                    f"{label} is the reference for the target motion, camera behavior, rhythm, and temporal structure."
                    f"{detail_suffix}"
                )
                media_retention.append(
                    f"{label} (motion, camera, and timing structure): attribute_transfer - transfer only the "
                    "declared motion grammar, temporal rhythm, or camera behavior without copying unrelated identity."
                )
            audio_label = item.get("audio_input_label")
            if audio_label:
                audio_purpose = _audio_reference_purpose(item, "audio_purpose")
                if item.get("audio_asset_id"):
                    audio_name = _compact(item.get("audio_name"))
                    audio_source = (
                        f"the separately supplied soundtrack asset {audio_name} paired with {label}"
                        if audio_name else
                        f"the separately supplied soundtrack paired with {label}"
                    )
                    copied_source = "this separately supplied audio signal"
                else:
                    audio_source = f"the enabled embedded soundtrack of {label}"
                    copied_source = f"the enabled soundtrack of {label}"
                if audio_purpose == "audio_reuse":
                    media_definitions.append(
                        f"{audio_label} is the reusable audio signal from {audio_source}."
                    )
                    media_retention.append(
                        f"{audio_label}: partially_copy - copy the declared soundtrack portions or layers from "
                        f"{copied_source} without synthesizing replacement content."
                    )
                else:
                    media_definitions.append(
                        f"{audio_label} is the acoustic reference from {audio_source}."
                    )
                    media_retention.append(
                        f"{audio_label}: reference - follow only the declared soundtrack, voice, rhythm, or music "
                        "qualities without copying the original signal or unrequested words."
                    )
        elif category == "audio":
            audio_purpose = _audio_reference_purpose(item)
            detail = _compact(item.get("prompt_hint") or item.get("description"))
            detail_suffix = f" Declared guidance: {detail}." if detail else ""
            if audio_purpose == "audio_reuse":
                media_definitions.append(
                    f"{label} is the reusable standalone audio signal.{detail_suffix}"
                )
                media_retention.append(
                    f"{label}: partially_copy - copy the declared signal portions or layers without "
                    "synthesizing replacement content."
                )
            else:
                media_definitions.append(
                    f"{label} is the standalone acoustic reference.{detail_suffix}"
                )
                media_retention.append(
                    f"{label}: reference - follow only the declared acoustic identity without transferring "
                    "visual attributes, copying the original signal, or reproducing unrequested words."
                )
    # Ref2VA's native node supports zero references.  Every unbound entity is
    # still defined from the locked Bible in the same six-section prompt; it
    # simply has no <Picture N> visual claim.
    for entity_id, bible in sorted(
        characters.items(), key=lambda item: (item[1].stable_speaker_id, item[0]),
    ):
        if ("character", entity_id) not in bound_entities:
            label = subject_labels.get(("character", entity_id), f"[Character {bible.stable_speaker_id}]")
            character_definitions.append(
                f"{label} is text-only, no Picture binding, and text-defined from the continuity Bible: "
                f"{_character_direction(bible)}"
            )
            character_retention.append(
                f"{label}: fully_preserved — preserve face, hairstyle, body proportions, wardrobe, colors, "
                "and marks exactly as stated in the continuity Bible."
            )
    for entity_id, bible in sorted(scenes.items(), key=lambda item: (item[1].slug, item[0])):
        if ("scene", entity_id) not in bound_entities:
            label = subject_labels.get(("scene", entity_id), f"[Environment {bible.slug}]")
            environment_definitions.append(
                f"{label} is text-only, no Picture binding, and text-defined from the continuity Bible: "
                f"{_scene_direction(bible)}"
            )
            environment_retention.append(
                f"{label}: fully_preserved — preserve spatial layout, time of day, lighting, and color palette exactly "
                "as stated in the continuity Bible."
            )
    for entity_id, bible in sorted(props.items(), key=lambda item: (item[1].slug, item[0])):
        if ("prop", entity_id) not in bound_entities:
            label = subject_labels.get(("prop", entity_id), f"[Prop {bible.slug}]")
            prop_definitions.append(
                f"{label} is text-only, no Picture binding, and text-defined from the continuity Bible: "
                f"{_prop_direction(bible)}"
            )
            prop_retention.append(
                f"{label}: fully_preserved — preserve silhouette, material, scale, color, marks, and continuity state "
                "exactly as stated in the continuity Bible."
            )
    return {
        "reference_bindings": binding_lines,
        "character_definitions": character_definitions,
        "character_retention": character_retention,
        "environment_definitions": environment_definitions,
        "environment_retention": environment_retention,
        "prop_definitions": prop_definitions,
        "prop_retention": prop_retention,
        "media_definitions": media_definitions,
        "media_retention": media_retention,
    }


def _append_list_section(lines: list[str], name: str, values: list[str]) -> None:
    lines.append(f"{name}:")
    lines.extend(f"- {value}" for value in values)


def _aligned_h3_duration_seconds(duration_ms: int) -> float:
    requested_frames = max(5, round(duration_ms * 24 / 1000))
    aligned_frames = requested_frames + (5 - requested_frames % 17) % 17
    return aligned_frames / 24


def _reference_summary(bindings: list[dict], shots: list[Shot]) -> str:
    """Write the official Ref2VA task prefix and concrete reference roles."""
    selected_task_types: set[str] = set()
    video_purposes: dict[str, str] = {}
    audio_purposes: dict[str, str] = {}
    has_generation_reference = not bindings
    for item in bindings:
        category = item.get("category")
        input_label = item.get("input_label")
        if category in {"character", "scene", "prop"}:
            has_generation_reference = True
        elif category == "video" and input_label:
            purpose = _video_reference_purpose(item)
            video_purposes[input_label] = purpose
            if purpose == "motion_camera_reference":
                has_generation_reference = True
            elif purpose == "video_editing":
                selected_task_types.add("video editing")
            elif purpose == "video_continuation":
                selected_task_types.add("video continuation")
        elif category == "audio" and input_label:
            purpose = _audio_reference_purpose(item)
            audio_purposes[input_label] = purpose
            selected_task_types.add("audio reuse" if purpose == "audio_reuse" else "audio reference")
            has_generation_reference = True
        if item.get("audio_input_label"):
            purpose = _audio_reference_purpose(item, "audio_purpose")
            audio_purposes[item["audio_input_label"]] = purpose
            selected_task_types.add("audio reuse" if purpose == "audio_reuse" else "audio reference")
    if has_generation_reference:
        selected_task_types.add("reference generation")
    # The official grammar permits combining task types only when every
    # relationship is concrete.  Our current binding contract has no temporal
    # range that could explain which source segment is edited and which source
    # ending is continued.  Accepting both roles would therefore produce an
    # apparently valid but semantically ambiguous target-video summary.
    if {"video editing", "video continuation"}.issubset(selected_task_types):
        raise PromptBuildError(
            "Ref2VA 暂不支持在同一任务中混用 video_editing 与 video_continuation；"
            "请统一参考视频用途，或拆成先编辑、后续写的两个任务"
        )
    task_type_order = (
        "video editing", "video continuation", "reference generation",
        "audio reuse", "audio reference",
    )
    task_types = [value for value in task_type_order if value in selected_task_types]
    shot_count = len(shots)
    editing_labels = [
        label for label, purpose in video_purposes.items() if purpose == "video_editing"
    ]
    continuation_labels = [
        label for label, purpose in video_purposes.items() if purpose == "video_continuation"
    ]
    statements = []
    if editing_labels:
        statements.append(f"The target video is an edited version of {editing_labels[0]}.")
        for label in editing_labels[1:]:
            statements.append(f"{label} is an additional source video for the target edit.")
    elif continuation_labels:
        statements.append(f"The target video continues from {continuation_labels[0]}.")
    else:
        statements.append(
            f"The target video is a {shot_count}-shot scripted audiovisual sequence."
        )
    if editing_labels or continuation_labels:
        statements.append(f"The target sequence contains {shot_count} scripted shots.")
    for label in continuation_labels:
        if not statements[0].endswith(f"{label}."):
            statements.append(f"The new sequence continues from the ending state of {label}.")
    role_names = {
        "character": "character identity",
        "scene": "environment identity",
        "prop": "prop identity",
    }
    for item in bindings:
        category = item.get("category")
        input_label = item.get("input_label")
        if category in role_names and item.get("entity_label") and input_label:
            statements.append(
                f"{item['entity_label']} is sourced from {input_label} for the {role_names[category]}."
            )
        elif category == "video" and input_label:
            if video_purposes[input_label] == "motion_camera_reference":
                statements.append(
                    f"{input_label} guides the declared motion, camera behavior, and timing."
                )
        elif category == "audio" and input_label:
            if audio_purposes[input_label] == "audio_reuse":
                statements.append(f"{input_label} is reused in the target audio track.")
            else:
                statements.append(
                    f"{input_label} guides the declared acoustic qualities without signal copying."
                )
        if item.get("audio_input_label"):
            audio_label = item["audio_input_label"]
            paired_source = (
                f"the separately supplied audio paired with {input_label}"
                if item.get("audio_asset_id") else
                f"the enabled embedded soundtrack of {input_label}"
            )
            if audio_purposes[audio_label] == "audio_reuse":
                statements.append(
                    f"{audio_label} is {paired_source} and is reused in the target audio track."
                )
            else:
                statements.append(
                    f"{audio_label} is {paired_source} and guides the declared soundtrack or voice qualities "
                    "without signal copying."
                )
    if not bindings:
        statements.append(
            "All visible and audible content is generated from the text-defined detailed description."
        )
    return f"[{' + '.join(task_types)}] {' '.join(statements)}"


def _prompt_quality_report(
    project: SeriesProject,
    shots: list[Shot],
    options: H3PromptOptions,
    text: str,
    *,
    detailed_description: list[str] | None = None,
    optimizer_report: dict | None = None,
    reference_bindings: list[dict] | None = None,
) -> dict:
    """Return non-destructive guidance for source fields that need English IR.

    MiniMax's official rewrite guides require English director prose while
    preserving original dialogue/lyrics/on-screen text.  This compiler cannot
    safely invent a translation, so it exposes every source that still needs a
    human or configured optimizer rewrite instead of silently claiming success.
    """
    sources: list[tuple[str, str]] = []
    if project.style:
        sources.append(("视觉风格", _translated(
            project.id, "project", project.id,
            "style.visual_prompt", project.style.visual_prompt,
        )))
    settings = project.production_settings or {}
    canonical_collisions = canonical_target_collisions(project)
    collision_fields = (
        ["canonical_target_collision"] if canonical_collisions else []
    )
    color_script_source = str(settings.get("color_script", ""))
    sources.append(("全剧色彩脚本", _translated(
        project.id, "project", project.id,
        "project.production_settings.color_script", color_script_source,
    )))
    raw_sound_values: list[str] = []
    effective_sound_values: list[str] = []
    raw_music_values: list[str] = []
    effective_music_values: list[str] = []
    raw_direction_values: list[str] = []
    effective_direction_values: list[str] = []
    for shot in shots:
        prefix = f"镜头 {shot.number}"
        narrative = _translated(
            project.id, "shot", shot.id, "shot.narrative", shot.narrative,
        )
        detailed_description = _translated(
            project.id, "shot", shot.id,
            "shot.detailed_description", shot.detailed_description,
        )
        camera = _translated(
            project.id, "shot", shot.id, "shot.camera", shot.camera,
        )
        raw_direction_values.extend([
            shot.narrative, shot.detailed_description, shot.action, shot.camera,
        ])
        effective_direction_values.extend([narrative, detailed_description, camera])
        sources.extend([
            (f"{prefix} 动作与画面描述", narrative),
            (f"{prefix} 单镜高细节描述", detailed_description),
            (f"{prefix} 镜头运动", camera),
            (f"{prefix} 正向连续性", _positive_continuity(shot)),
        ])
        # UI v1 persisted the same action prose in both fields.  The compiled
        # prompt already emits it once, so the quality gate must not present
        # two rewrite jobs for one source sentence either.
        if _semantic_override(shot.action, [shot.narrative]):
            action = _translated(
                project.id, "shot", shot.id, "shot.action", shot.action,
            )
            sources.append((f"{prefix} 主体动作", action))
            effective_direction_values.append(action)
        if options.include_audio:
            sound_effects = _translated(
                project.id, "shot", shot.id,
                "shot.sound_effects", shot.sound_effects,
            )
            music = _translated(
                project.id, "shot", shot.id, "shot.music", shot.music,
            )
            sources.extend([
                (f"{prefix} 声景", sound_effects),
                (f"{prefix} 非叙事音乐", music),
            ])
            if shot.scene and shot.scene.ambient_sound:
                raw_sound_values.append(shot.scene.ambient_sound)
                effective_sound_values.append(_translated(
                    project.id, "scene", shot.scene.id,
                    "scene.ambient_sound", shot.scene.ambient_sound,
                ))
            if shot.sound_effects:
                raw_sound_values.append(shot.sound_effects)
                effective_sound_values.append(sound_effects)
            if shot.music:
                raw_music_values.append(shot.music)
                effective_music_values.append(music)
            for dialogue_index, entry in enumerate(shot.dialogue or [], start=1):
                if not isinstance(entry, dict):
                    continue
                speaker_id = str(entry.get("speaker_id") or dialogue_index)
                delivery_source = str(entry.get("delivery", ""))
                sources.append((
                    f"{prefix} 对白 {speaker_id} 演绎方式",
                    _translated(
                        project.id, "shot", shot.id,
                        f"shot.dialogue.{dialogue_index - 1}.delivery",
                        delivery_source,
                    ),
                ))
        for character in shot.characters.all():
            sources.append((
                f"人物 {character.stable_speaker_id} 视觉锚点",
                _character_direction(character),
            ))
            if options.include_audio:
                sources.append((
                    f"人物 {character.stable_speaker_id} 声线",
                    _translated(
                        project.id, "character", character.id,
                        "character.voice_profile", character.voice_profile,
                    ),
                ))
        if shot.scene:
            sources.append((f"场景 {shot.scene.slug} 视觉锚点", _scene_direction(shot.scene)))
        for prop in shot.props.all():
            sources.append((f"道具 {prop.slug} 视觉锚点", _prop_direction(prop)))
    override_sources = []
    if options.include_audio:
        override_sources.extend([
            ("本次整体声景", _semantic_override(
                options.overall_soundscape,
                [*raw_sound_values, *effective_sound_values],
            )),
            ("本次非叙事音乐", _semantic_override(
                options.non_diegetic_music,
                [*raw_music_values, *effective_music_values],
            )),
        ])
    override_sources.append(("本次全局导演要求", _semantic_override(
        options.extra_direction,
        [*raw_direction_values, *effective_direction_values],
    )))
    sources.extend((label, value) for label, value in override_sources if value)
    negative_sources = [
        ("全剧负面约束", str(settings.get("negative_prompt", ""))),
        ("视觉风格负面约束", project.style.negative_prompt if project.style else ""),
    ]
    contamination_fields = []
    for shot in shots:
        label = f"镜头 {shot.number} 负面约束"
        value = _shot_negative_constraints(shot)
        negative_sources.append((label, value))
        positive_values = [shot.narrative, shot.detailed_description, shot.action]
        positive_values.extend(
            str(item.get("text") or "")
            for item in (shot.dialogue or []) if isinstance(item, dict)
        )
        if value and _negative_repeats_positive(value, positive_values):
            contamination_fields.append(f"{label}（疑似混入正向剧情或对白）")
        negative_sources.extend(
            (f"人物 {character.stable_speaker_id} 负面约束", character.negative_constraints)
            for character in shot.characters.all()
        )
        if shot.scene:
            negative_sources.append((
                f"场景 {shot.scene.slug} 负面约束", shot.scene.negative_constraints,
            ))
        negative_sources.extend(
            (f"道具 {prop.slug} 负面约束", prop.negative_constraints)
            for prop in shot.props.all()
        )
    negative_chinese_fields = [
        label for label, value in negative_sources
        if value and _CJK_RE.search(str(value))
    ]
    chinese_sources = [
        label for label, value in sources
        if value and _CJK_RE.search(_PROTECTED_AUTHORED_TEXT_RE.sub("", str(value)))
    ]
    optimizer_report = optimizer_report or {
        "status": "not_configured",
        "enabled": False,
        "provider": "none",
        "model": "",
        "transport": "local",
        "failure_mode": "warn",
        "message": "",
        "validation_errors": [],
    }
    optimizer_succeeded = optimizer_report.get("status") == "optimized"
    director_text = _PROTECTED_AUTHORED_TEXT_RE.sub("", text)
    has_unclassified_chinese = bool(_CJK_RE.search(director_text))
    warnings = []
    if negative_chinese_fields:
        warnings.append(
            "负面约束必须是英文排除短语；以下字段仍含中文："
            + "、".join(dict.fromkeys(negative_chinese_fields))
            + "。负面约束不会由导演正文优化器代写。"
        )
    if contamination_fields:
        warnings.append(
            "检测到负面约束与正向剧情/对白高度重合，疑似旧字段迁移污染："
            + "、".join(dict.fromkeys(contamination_fields))
            + "。必须显式审计恢复后才能进入生产。"
        )
    if canonical_collisions:
        warnings.append(
            "检测到多个当前 Bible 实体共用同一 canonical English target；"
            "请先修正术语表，系统不会自动选择或改写人物、场景、道具身份。"
        )
    missing_fields = []
    if not project.style or not _compact(project.style.visual_prompt):
        missing_fields.append("视觉风格")
    for shot in shots:
        if not _compact(shot.detailed_description or shot.narrative or shot.action):
            missing_fields.append(f"镜头 {shot.number} 动作与画面描述")
        if not _compact(shot.camera):
            missing_fields.append(f"镜头 {shot.number} 镜头运动/机位")
        if options.include_audio and not _compact(shot.sound_effects) and not (
            shot.scene and _compact(shot.scene.ambient_sound)
        ):
            missing_fields.append(f"镜头 {shot.number} 声景")
        for character in shot.characters.all():
            if not _compact(_character_direction(character)):
                missing_fields.append(f"人物 {character.stable_speaker_id} 英文视觉锚点")
        if shot.scene and not _compact(_scene_direction(shot.scene)):
            missing_fields.append(f"场景 {shot.scene.slug} 英文视觉锚点")
        for prop in shot.props.all():
            if not _compact(_prop_direction(prop)):
                missing_fields.append(f"道具 {prop.slug} 英文视觉锚点")
        action_text = _compact(" ".join(_dedupe_text([shot.narrative, shot.action])))
        duration_ms = _shot_duration(shot, options)
        if duration_ms <= 5_500 and (
            len(action_text) > 420 or len(re.findall(r"[,，;；]", action_text)) >= 6
        ):
            warnings.append(
                f"镜头 {shot.number} 约 5 秒但动作描述过密；建议只保留一条主动作链和一种主要运镜，"
                "其余动作拆成新镜头。"
            )
    if missing_fields:
        warnings.append(
            f"建议补全这些高影响字段：{'、'.join(dict.fromkeys(missing_fields))}。"
            "空字段会使模型自行猜测，降低人物、动作和声音的可控性。"
        )
    optimizer_message = _compact(str(optimizer_report.get("message", "")))
    if optimizer_report.get("status") in {
        "disabled", "misconfigured", "failed_request", "failed_validation",
    } and optimizer_message:
        warnings.append(optimizer_message)
    if chinese_sources and not optimizer_succeeded:
        preview = "、".join(dict.fromkeys(chinese_sources))
        warnings.append(
            f"以下会进入 H3 的导演描述仍含中文：{preview}。官方建议将这些字段改写为英文；"
            "对白原文不需要翻译。"
        )
    elif has_unclassified_chinese:
        warnings.append(
            "最终导演提示词仍含未归类中文（可能来自参考素材说明）。请将参考素材职责改写为英文；"
            "对白、歌词和真实屏显文字可以保留原语言。"
        )
    if len(text) > 6_200:
        warnings.append("提示词已接近官方 7000 字符上限，请删去重复设定或减少同镜头实体。")
    detailed_word_count = len(re.findall(
        r"[A-Za-z]+(?:[-'][A-Za-z]+)?", " ".join(detailed_description or []),
    ))
    is_video_editing = any(
        item.get("category") == "video" and item.get("purpose") == "video_editing"
        for item in (reference_bindings or [])
    )
    if options.mode == Shot.H3Mode.REF2VA and not is_video_editing and detailed_word_count < 350:
        warnings.append(
            "Ref2VA 的 detailed_description 少于官方建议的 350–500 个英文词；请逐镜补全构图、"
            "主体位置、光线、动作状态变化、运镜、声音和参考生效时刻。"
        )
    elif options.mode == Shot.H3Mode.REF2VA and not is_video_editing and detailed_word_count > 500:
        warnings.append(
            "Ref2VA 的 detailed_description 超过官方建议的 350–500 个英文词；请删除重复身份设定，"
            "只保留当前镜头可观察的变化。"
        )
    non_optimizable_fields = list(dict.fromkeys([
        *negative_chinese_fields, *contamination_fields, *collision_fields,
    ]))
    fields_requiring_rewrite = list(dict.fromkeys([
        *([] if optimizer_succeeded else chinese_sources),
        *non_optimizable_fields,
    ]))
    if has_unclassified_chinese and not chinese_sources and not optimizer_succeeded:
        # Reference asset descriptions and binding-role hints are not stored on
        # Shot/Bible fields, but still enter the final director prompt. Expose a
        # concrete gate field so block mode cannot be bypassed merely because
        # the Chinese prose came from a reference asset.
        fields_requiring_rewrite.append(
            "未归类导演描述（参考素材职责或绑定说明）"
        )
    language_gate_mode = str(
        getattr(django_settings, "H3_PROMPT_LANGUAGE_GATE", "block")
    ).strip().lower()
    if language_gate_mode not in {"block", "warn"}:
        language_gate_mode = "block"
    # Negative-channel corruption is not a stylistic language warning.  It can
    # invert the intended shot or feed positive story prose to the negative
    # encoder, so it remains fail-closed even when ordinary director Chinese is
    # configured as warn-only during development.
    gate_would_block = bool(non_optimizable_fields) or (
        bool(fields_requiring_rewrite) and language_gate_mode == "block"
    )
    return {
        "status": "needs_attention" if warnings else "ready",
        "language_policy": "English director prose; preserve original dialogue, lyrics, and visible text only",
        "dialogue_language": options.language,
        "prompt_character_count": len(text),
        "prompt_character_limit": H3_PROMPT_CHARACTER_LIMIT,
        "detailed_description_word_count": detailed_word_count,
        "warnings": warnings,
        "integrity_errors": canonical_collisions,
        "fields_requiring_english_rewrite": fields_requiring_rewrite,
        "hard_block_fields": non_optimizable_fields,
        "source_fields_optimized": (
            list(dict.fromkeys(chinese_sources)) if optimizer_succeeded else []
        ),
        "optimizer": optimizer_report,
        "production_language_gate": {
            "mode": language_gate_mode,
            "would_block_enqueue": gate_would_block,
            "message": (
                "Production enqueue will be blocked until the listed director fields are rewritten in English."
                if gate_would_block else
                "Production enqueue is not blocked by the current language-gate decision."
            ),
        },
    }


def build_negative_prompt(project: SeriesProject, shots: list[Shot]) -> str:
    """Create a de-duplicated exclusion audit field for a prompt artifact."""
    values = []
    settings = project.production_settings or {}
    if settings.get("negative_prompt"):
        values.append(str(settings["negative_prompt"]))
    if project.style and project.style.negative_prompt:
        values.append(project.style.negative_prompt)
    for shot in shots:
        if value := _shot_negative_constraints(shot):
            values.append(value)
        values.extend(item.negative_constraints for item in shot.characters.all() if item.negative_constraints)
        if shot.scene and shot.scene.negative_constraints:
            values.append(shot.scene.negative_constraints)
        values.extend(item.negative_constraints for item in shot.props.all() if item.negative_constraints)
    phrases = []
    for value in values:
        # Legacy records used both Chinese/English commas and semicolons.  Split
        # all of them here so repeated audit phrases do not grow on each save.
        phrases.extend(re.split(r"[,，;；\n]+", str(value)))
    return "; ".join(_dedupe_text(phrases))


def build_h3_video_prompt(
    project: SeriesProject,
    shots: list[Shot],
    options: H3PromptOptions | None = None,
) -> H3PromptResult:
    options = options or H3PromptOptions(language=project.language)
    if not shots:
        raise PromptBuildError("至少需要一个分镜")
    if options.mode not in {choice for choice, _ in Shot.H3Mode.choices}:
        raise PromptBuildError(f"不支持的 H3 模式: {options.mode}")
    if any(shot.episode.project_id != project.id for shot in shots):
        raise PromptBuildError("所有分镜必须属于同一连续剧项目")

    # Ensure all categorized assets have persistent project-level slots before
    # deriving the input order.  Prompt and Comfy graph will both consume the
    # same ordered binding records captured below.
    if options.consistency_snapshot is None:
        sync_consistency_locks(project)
        snapshot = capture_consistency_snapshot(project)
    else:
        # Batch callers lock the project and synchronize once before sharing
        # this immutable snapshot across independently generated child prompts.
        snapshot = options.consistency_snapshot
    try:
        reference_bindings = resolve_group_reference_bindings(
            project=project, shots=shots, mode=options.mode, snapshot=snapshot,
            single_shot_bindings=options.single_shot_reference_bindings,
            single_shot_reference_assets=options.single_shot_reference_assets,
        )
    except ReferenceBindingError as exc:
        raise PromptBuildError(str(exc)) from exc
    effective_mode = effective_conditioning_mode(options.mode, reference_bindings)

    # Ref2VA timelines refer to stable semantic subjects, not to image files.
    # This lets a Picture define identity once in subject_definitions while
    # later shots use only <Subject N>, avoiding repeated full appearance text.
    reference_labels = {
        (item["category"], item["entity_id"]): item["entity_label"]
        for item in reference_bindings if item.get("entity_id")
    } if options.mode == Shot.H3Mode.REF2VA else {}
    durations = [_shot_duration(shot, options) for shot in shots]
    elapsed = 0
    timeline = []
    style_prompt = _translated(
        project.id, "project", project.id, "style.visual_prompt",
        project.style.visual_prompt if project.style else "",
    )
    color_script_source = str((project.production_settings or {}).get("color_script", ""))
    color_script = _translated(
        project.id, "project", project.id,
        "project.production_settings.color_script", color_script_source,
    )
    effective_duration_seconds = _aligned_h3_duration_seconds(sum(durations))
    introduced_reference_entities: set[tuple[str, str]] = set()
    for index, (shot, duration) in enumerate(zip(shots, durations, strict=True), start=1):
        # H3: Shot 1 has no timestamp; Shot 2+ has an exact strictly increasing timestamp.
        prefix = f"[Shot {index}]" if index == 1 else f"[Shot {index}] At {_timestamp(elapsed)},"
        keyframe_path = ""
        if index == 1 and effective_mode == Shot.H3Mode.I2VA:
            keyframe_path = (
                "Begin from the style, subject appearance, clothing, colors, key objects, and spatial "
                "relationships established by <Picture 1>, then develop the action continuously. "
            )
        elif index == 1 and effective_mode == Shot.H3Mode.FL2VA:
            keyframe_path = (
                "Begin in the state and composition of <Picture 1>, show observable intermediate changes, "
                "then progressively converge on <Picture 2> at the end. "
            )
        elif index == 1 and effective_mode == Shot.H3Mode.L2VA:
            keyframe_path = (
                "Begin from a plausible preceding state and make the subjects, objects, camera, and scene "
                "gradually converge on <Picture 1> at the end. "
            )
        current_reference_entities = {
            ("character", str(item.id)) for item in shot.characters.all()
        }
        current_reference_entities.update(
            ("prop", str(item.id)) for item in shot.props.all()
        )
        if shot.scene_id:
            current_reference_entities.add(("scene", str(shot.scene_id)))
        current_reference_entities.intersection_update(reference_labels)
        first_reference_entities = current_reference_entities - introduced_reference_entities
        shot_description = _shot_description(
            shot,
            options,
            "" if options.mode == Shot.H3Mode.REF2VA else style_prompt,
            reference_labels,
            is_first_shot=index == 1,
            color_script="" if options.mode == Shot.H3Mode.REF2VA else color_script,
            reference_detail_entities=first_reference_entities,
        )
        timeline.append(f"{prefix} {keyframe_path}{shot_description}")
        introduced_reference_entities.update(current_reference_entities)
        elapsed += duration
    existing_directions = {
        _compact(value).casefold().rstrip(".。")
        for shot in shots for value in (
            shot.narrative, shot.detailed_description, shot.action, shot.camera,
        )
        if _compact(value)
    }
    if options.extra_direction and _compact(options.extra_direction).casefold().rstrip(".。") not in existing_directions:
        timeline.append(f"Across all shots, {_compact(options.extra_direction).rstrip('.。')}.")

    soundscape = _soundscape(shots, options)
    music = _music(shots, options)

    if options.mode == Shot.H3Mode.REF2VA:
        style_opening = []
        if _compact(style_prompt):
            style_opening.append(
                f"The target video uses {_compact(style_prompt).rstrip('.。')}."
            )
        if _compact(color_script):
            style_opening.append(
                f"The color and lighting design follows {_compact(color_script).rstrip('.。')}."
            )
        detailed_description = [*style_opening, *timeline]
        classified = _classified_reference_fields(shots, reference_bindings, reference_labels)
        subject_definitions = [
            *classified["reference_bindings"],
            *classified["character_definitions"],
            *classified["environment_definitions"],
            *classified["prop_definitions"],
            *classified["media_definitions"],
        ]
        if not subject_definitions:
            subject_definitions = [
                "No reusable reference subject is supplied; all visible content is text-defined in the detailed description."
            ]
        retention_analysis = [
            *classified["character_retention"],
            *classified["environment_retention"],
            *classified["prop_retention"],
            *classified["media_retention"],
        ]
        fields = {
            **classified,
            "subject_definitions": subject_definitions,
            "retention_analysis": retention_analysis,
            "summary": _reference_summary(reference_bindings, shots),
            "detailed_description": detailed_description,
            "overall_soundscape": soundscape,
            "non_diegetic_music": music,
        }
        lines: list[str] = []
        _append_list_section(lines, "subject_definitions", fields["subject_definitions"])
        lines.append(f"summary: {fields['summary']}")
        _append_list_section(lines, "retention_analysis", fields["retention_analysis"])
        lines.extend([
            "detailed_description:", *detailed_description,
            f"overall_soundscape: {soundscape}",
            f"non_diegetic_music: {music}",
        ])
    else:
        fields = {
            "integrated_multimodal_description": timeline,
            "overall_soundscape": soundscape,
            "non_diegetic_music": music,
        }
        lines = []
        if effective_mode == Shot.H3Mode.T2VA:
            fields["text_only_policy"] = "No external visual references; derive visuals only from the continuity bible."
        elif effective_mode == Shot.H3Mode.I2VA:
            fields["first_frame"] = reference_bindings[0]
            lines.append(
                "For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced."
            )
            lines.append("")
        elif effective_mode == Shot.H3Mode.FL2VA:
            fields["first_frame"] = reference_bindings[0]
            fields["last_frame"] = reference_bindings[1]
            lines.append(
                "How the reference pictures align with the target video — "
                "<Picture 1> (from [Shot 1]) aligns with the 0.00-second mark of the target video; "
                f"<Picture 2> (from [Shot {len(shots)}]) aligns with the {effective_duration_seconds:.2f}-second "
                "mark of the target video."
            )
            lines.append("")
        elif effective_mode == Shot.H3Mode.L2VA:
            fields["last_frame"] = reference_bindings[0]
            lines.append(
                "How the reference pictures align with the target video — "
                f"<Picture 1> (from [Shot {len(shots)}]) aligns with the {effective_duration_seconds:.2f}-second "
                "mark of the target video."
            )
            lines.append("")
        lines.extend([
            "integrated_multimodal_description:", *timeline,
            f"overall_soundscape: {soundscape}",
            f"non_diegetic_music: {music}",
        ])
    text = "\n".join(lines)
    if len(text) > H3_PROMPT_CHARACTER_LIMIT:
        raise PromptBuildError(
            f"H3 提示词共 {len(text)} 字符，超过官方 {H3_PROMPT_CHARACTER_LIMIT} 字符上限；"
            "请精简视觉锚点、动作或参考素材说明后重试"
        )
    found_reference_labels = set(re.findall(r"<(?:Picture|Video|Audio)\s+[1-9][0-9]*>", text))
    expected_reference_labels = {
        label
        for item in reference_bindings
        for label in (item.get("input_label"), item.get("audio_input_label"))
        if label
    }
    if found_reference_labels != expected_reference_labels:
        raise PromptBuildError(
            "提示词中的 <Picture/Video/Audio N> 必须与规范化 reference_bindings 一一对应；"
            f"expected={sorted(expected_reference_labels)}, found={sorted(found_reference_labels)}"
        )
    try:
        optimization = optimize_h3_video_prompt(text, mode=options.mode)
    except H3PromptOptimizationBlocked as exc:
        raise PromptBuildError(str(exc)) from exc
    text = optimization.text
    detailed_for_quality = None
    if options.mode == Shot.H3Mode.REF2VA:
        detail_match = re.search(
            r"^detailed_description:\s*(.*?)^overall_soundscape:",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        detailed_for_quality = [detail_match.group(1)] if detail_match else []
    quality = _prompt_quality_report(
        project, shots, options, text,
        detailed_description=detailed_for_quality,
        optimizer_report=optimization.report,
        reference_bindings=reference_bindings,
    )
    payload = {
        "model": project.target_model or "minimax-h3",
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "mode": options.mode,
        "effective_conditioning_mode": effective_mode,
        "prompt_template_key": prompt_template_key(
            options.mode, effective_mode, len(reference_bindings),
        ),
        "duration_ms": elapsed,
        "shot_durations_ms": durations,
        "fields": fields,
        "quality": quality,
        "reference_bindings": reference_bindings,
        # Compatibility alias consumed by task staging.  It is deliberately
        # identical to ``reference_bindings`` rather than inferred separately.
        "reference_assets": reference_bindings,
    }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return H3PromptResult(text, payload, snapshot, elapsed, digest)


def build_consistent_image_prompt(
    project: SeriesProject,
    shot: Shot,
    extra_direction: str = "",
    *,
    consistency_snapshot: dict | None = None,
) -> H3PromptResult:
    """Compile concise SDXL text conditioning plus an auditable Bible snapshot."""
    style_source = project.style.visual_prompt if project.style else "cinematic production still"
    style = _translated(
        project.id, "project", project.id, "style.visual_prompt", style_source,
    )
    characters = list(shot.characters.all())
    shot_props = list(shot.props.all())
    subjects = [value for character in characters if (value := _character_direction(character))]
    props = [value for prop in shot_props if (value := _prop_direction(prop))]
    production_settings = project.production_settings or {}
    scene = shot.scene
    fields = {
        # ``purpose`` is audit/UI metadata only; it is intentionally not sent
        # to CLIP, where labels and model names consume a small text budget.
        "purpose": "SDXL continuity production still",
        "style": _compact(style),
        "composition": "; ".join(_dedupe_text([
            _translated(project.id, "shot", shot.id, "shot.narrative", shot.narrative),
            _translated(project.id, "shot", shot.id, "shot.action", shot.action),
        ])),
        "camera": "; ".join(_dedupe_text([
            _framing_direction(shot),
            _translated(project.id, "shot", shot.id, "shot.camera", shot.camera),
        ])),
        "scene": "; ".join(_dedupe_text([
            _translated(project.id, "scene", scene.id, "scene.visual_identity", scene.visual_identity) if scene else "",
            _translated(project.id, "scene", scene.id, "scene.location", scene.location) if scene else "",
            _translated(project.id, "scene", scene.id, "scene.time_of_day", scene.time_of_day) if scene else "",
        ])),
        "lighting_color": "; ".join(_dedupe_text([
            _translated(project.id, "scene", scene.id, "scene.lighting", scene.lighting) if scene else "",
            _translated(project.id, "scene", scene.id, "scene.color_palette", scene.color_palette) if scene else "",
        ])),
        "color_script": _compact(_translated(
            project.id, "project", project.id,
            "project.production_settings.color_script",
            str(production_settings.get("color_script", "")),
        )),
        "subjects": subjects,
        "props": props,
        "continuity": _positive_continuity(shot) or _compact(_translated(
            project.id, "project", project.id,
            "project.continuity_summary", project.continuity_summary,
        )),
        "extra_direction": _compact(extra_direction),
    }
    # SDXL Base uses a finite CLIP text-encoding budget. Keep the positive
    # prompt as natural comma-separated visual phrases, in priority order,
    # while retaining the structured fields above for UI/audit purposes.
    prompt_segments = [
        segment.rstrip(" .。;；,，")
        for segment in _dedupe_text([
        *subjects,
        # Identity, wardrobe, and distinctive prop state must stay ahead of
        # lower-priority mood/style phrases so CLIP truncation cannot silently
        # discard the very continuity this still is meant to establish.
        fields["continuity"],
        *props,
        fields["composition"],
        fields["scene"],
        fields["camera"] or "a motivated cinematic composition",
        fields["lighting_color"],
        fields["color_script"],
        fields["style"],
        fields["extra_direction"],
        ])
        if segment.rstrip(" .。;；,，")
    ]
    text = ", ".join(prompt_segments)
    canonical_collisions = canonical_target_collisions(project)
    collision_fields = (
        ["canonical_target_collision"] if canonical_collisions else []
    )
    quality_sources = [
        ("视觉风格", fields["style"]),
        (f"镜头 {shot.number} 静态构图", fields["composition"]),
        (f"镜头 {shot.number} 机位与构图", fields["camera"]),
        (f"镜头 {shot.number} 场景视觉锚点", fields["scene"]),
        (f"镜头 {shot.number} 光线与色彩", fields["lighting_color"]),
        (f"镜头 {shot.number} 正向连续性", fields["continuity"]),
        ("本次图片导演要求", fields["extra_direction"]),
    ]
    quality_sources.extend(
        (f"人物 {character.stable_speaker_id} 视觉锚点", _character_direction(character))
        for character in characters
    )
    quality_sources.extend(
        (f"道具 {prop.slug} 视觉锚点", _prop_direction(prop))
        for prop in shot_props
    )
    shot_negative = _shot_negative_constraints(shot)
    quality_sources.append(
        ("全剧色彩脚本", str(production_settings.get("color_script", "")))
    )
    negative_sources = [
        ("全剧负面约束", str(production_settings.get("negative_prompt", ""))),
        ("视觉风格负面约束", project.style.negative_prompt if project.style else ""),
        (f"镜头 {shot.number} 负面约束", shot_negative),
    ]
    negative_sources.extend(
        (f"人物 {character.stable_speaker_id} 负面约束", character.negative_constraints)
        for character in characters
    )
    if shot.scene:
        negative_sources.append((f"场景 {shot.scene.slug} 负面约束", shot.scene.negative_constraints))
    negative_sources.extend(
        (f"道具 {prop.slug} 负面约束", prop.negative_constraints)
        for prop in shot_props
    )
    quality_sources.extend(negative_sources)
    negative_chinese_fields = [
        label for label, value in negative_sources
        if value and _CJK_RE.search(_PROTECTED_AUTHORED_TEXT_RE.sub("", str(value)))
    ]
    positive_values = [shot.narrative, shot.detailed_description, shot.action]
    positive_values.extend(
        str(item.get("text") or "")
        for item in (shot.dialogue or []) if isinstance(item, dict)
    )
    contamination_fields = []
    if shot_negative and _negative_repeats_positive(shot_negative, positive_values):
        contamination_fields.append(
            f"镜头 {shot.number} 负面约束（疑似混入正向剧情或对白）"
        )
    cjk_rewrite_fields = list(dict.fromkeys(
        label for label, value in quality_sources
        if value and _CJK_RE.search(_PROTECTED_AUTHORED_TEXT_RE.sub("", str(value)))
    ))
    fields_requiring_rewrite = list(dict.fromkeys([
        *cjk_rewrite_fields, *contamination_fields, *collision_fields,
    ]))
    director_text = _PROTECTED_AUTHORED_TEXT_RE.sub("", text)
    if _CJK_RE.search(director_text) and not fields_requiring_rewrite:
        fields_requiring_rewrite.append("未归类图片导演描述")
    gate_mode = str(
        getattr(django_settings, "H3_PROMPT_LANGUAGE_GATE", "block")
    ).strip().lower()
    if gate_mode not in {"block", "warn"}:
        gate_mode = "block"
    prompt_word_count = len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", text))
    warnings = []
    if cjk_rewrite_fields:
        warnings.append(
            "以下会进入本地 SDXL 的导演/负面描述仍含中文："
            + "、".join(cjk_rewrite_fields)
            + "。当前未配置图片提示词自动英文改写；请手工改成简洁英文后重新生成。"
            "<screen_text> 中的真实屏显文字保留原文。"
        )
    if contamination_fields:
        warnings.append(
            "检测到图片负面约束与正向构图、剧情或对白高度重合，疑似旧字段迁移污染；"
            + "、".join(contamination_fields)
            + "。必须显式审计恢复后才能进入生产。"
        )
    if canonical_collisions:
        warnings.append(
            "检测到多个当前 Bible 实体共用同一 canonical English target；"
            "请先修正术语表，系统不会自动选择或改写人物、场景、道具身份。"
        )
    if prompt_word_count > 75:
        warnings.append(
            f"SDXL 正向提示词约含 {prompt_word_count} 个英文词，已接近或超过有限的文本编码预算；"
            "靠后的描述可能被文本编码器截断，请优先保留主体身份、服装、构图、光线和关键道具。"
        )
    hard_negative_fields = list(dict.fromkeys([
        *negative_chinese_fields, *contamination_fields, *collision_fields,
    ]))
    gate_would_block = bool(hard_negative_fields) or (
        bool(fields_requiring_rewrite) and gate_mode == "block"
    )
    quality = {
        "status": "needs_attention" if warnings else "ready",
        "language_policy": (
            "English image director and negative prose; preserve authored text inside "
            "<screen_text> only"
        ),
        "prompt_character_count": len(text),
        "prompt_word_count": prompt_word_count,
        "prompt_word_count_is_approximate": True,
        "warnings": warnings,
        "integrity_errors": canonical_collisions,
        "fields_requiring_english_rewrite": fields_requiring_rewrite,
        "hard_block_fields": hard_negative_fields,
        "source_fields_optimized": [],
        "optimizer": {
            "status": "not_configured" if fields_requiring_rewrite else "not_required",
            "enabled": False,
            "provider": "none",
            "model": "",
            "transport": "local",
            "failure_mode": "warn",
            "message": (
                "当前没有图片提示词自动英文改写器；系统不会伪装成已经优化。"
                if fields_requiring_rewrite else "图片导演描述已是英文，无需自动改写。"
            ),
            "validation_errors": [],
        },
        "production_language_gate": {
            "mode": gate_mode,
            "would_block_enqueue": gate_would_block,
            "message": (
                "Production enqueue will be blocked until the listed image fields are rewritten in English."
                if gate_would_block else
                "Production enqueue is not blocked by the current language-gate decision."
            ),
        },
    }
    snapshot = (
        consistency_snapshot
        if consistency_snapshot is not None
        else capture_consistency_snapshot(project)
    )
    continuity_assets = _reference_assets([shot], snapshot)
    payload = {
        # The image compiler is model-specific even when the series target is
        # MiniMax H3. Persist the canonical model identity here; executable
        # workflows later pin this to their audited local checkpoint filename.
        "model": SDXL_BASE_MODEL_ID,
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "mode": "REFERENCE_IMAGE",
        "effective_conditioning_mode": "TEXT_ONLY",
        "prompt_template_key": "sdxl_txt2img_bible_v1",
        "conditioning_contract": "text_and_bible_anchors_only",
        "pixel_reference_inputs": False,
        "duration_ms": 0,
        "fields": fields,
        "quality": quality,
        # Selected library images do not become pixels in the reviewed SDXL v1
        # graph. Keep them as provenance/continuity material only.
        "reference_bindings": [],
        "reference_assets": [],
        "continuity_assets": continuity_assets,
    }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return H3PromptResult(text, payload, snapshot, 0, digest)
