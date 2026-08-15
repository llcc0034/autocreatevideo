"""Review-gated, one-shot high-detail Qwen proposals for MiniMax H3.

The compact storyboard remains the formal timing and continuity source.  This
module produces a separate bilingual director-description candidate for exactly
one already-existing ``Shot`` and promotes it only after human confirmation.
It deliberately does not reuse EpisodeCreativeDraft's multi-shot regeneration
or merge behaviour.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    CharacterBible,
    Episode,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
    ShotDetailDraft,
    ShotDetailDraftBatchItem,
)
from studio.services.creative_drafts import (
    CreativeContextConflict,
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRevisionConflict,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
    _require_runtime,
    _run_runtime,
    _run_provider_runtime,
    creative_config,
    ensure_creative_resources_available,
    normalise_creative_provider,
    LOCAL_QWEN_PROVIDER,
)
from studio.services.prompt_translation import (
    PINNED_MODEL_REPO_ID,
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    relevant_canonical_identity_mappings,
    source_text_hash,
    translation_context_hash,
)
from studio.services.direction_semantics import direction_semantic_mismatches


SHOT_DETAIL_SCHEMA_VERSION = 1
SHOT_DETAIL_OPERATION = "shot_high_detail"
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ENGLISH_DIRECTOR_FORBIDDEN_RE = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af\u0400-\u052f"
    r"\u200b-\u200f\u202a-\u202e\u2060\ufeff]"
)


class ShotDetailDraftError(CreativeDraftError):
    """Base error with the existing Qwen resource/error transport contract."""


class ShotDetailContextConflict(CreativeContextConflict):
    pass


class ShotDetailRevisionConflict(CreativeRevisionConflict):
    pass


class ShotDetailValidationError(CreativeValidationError):
    pass


@dataclass
class PreparedShotDetailApplication:
    draft: ShotDetailDraft
    project: SeriesProject
    episode: Episode
    shot: Shot
    user: Any
    language_mode: str
    data: dict[str, str]
    validation: dict[str, Any]
    translated: bool
    next_value: str
    generation_context_hash: str
    generation_context_snapshot: dict[str, Any]
    policy: dict[str, Any]


def shot_detail_policy(provider: str | None = None) -> dict[str, Any]:
    """Return the only runtime policy for one high-detail operation."""
    selected = normalise_creative_provider(
        provider,
        default=getattr(settings, "PROMPT_CREATIVE_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER),
    )
    common = dict(getattr(settings, "SHOT_DETAIL_POLICY", {}) or {})
    provider_values = dict(
        (getattr(settings, "SHOT_DETAIL_PROVIDER_POLICIES", {}) or {}).get(selected, {})
    )
    return {"provider": selected, **common, **provider_values}


def resolve_shot_detail_policy(
    provider: str | None = None, override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a historic frozen policy over today's complete policy contract."""
    selected = normalise_creative_provider(provider)
    policy = shot_detail_policy(selected)
    if isinstance(override, dict):
        policy.update(override)
    policy["provider"] = selected
    return policy


def shot_detail_provider_label(provider: str | None) -> str:
    return "DeepSeek API" if normalise_creative_provider(provider) == "deepseek_api" else "本地 Qwen"


def shot_detail_draft_policy(draft: ShotDetailDraft) -> dict[str, Any]:
    """Resolve a draft with its server-owned generation-time policy snapshot."""
    provider = normalise_creative_provider(draft.provider)
    stored = (draft.input_settings or {}).get("policy")
    return resolve_shot_detail_policy(
        provider, stored if isinstance(stored, dict) else None,
    )


def _has_forbidden_english_director_text(value: str) -> bool:
    """Reject scripts and invisible controls that cannot safely enter H3 prose."""
    if CJK_RE.search(value) or ENGLISH_DIRECTOR_FORBIDDEN_RE.search(value):
        return True
    return any(
        (char.isalpha() and "LATIN" not in unicodedata.name(char, ""))
        or (char not in "\n\r\t" and unicodedata.category(char) in {"Cc", "Cf"})
        for char in value
    )


def _has_unsafe_text_controls(value: str) -> bool:
    """Return only controls that are unsafe to persist or render for review.

    Language quality is intentionally *not* decided here: mixed-language and
    non-Latin output remains an editable review item.  Bidi/zero-width/control
    characters are different because they can alter what the reviewer sees.
    """
    return any(
        char not in "\n\r\t" and unicodedata.category(char) in {"Cc", "Cf"}
        for char in value
    )


def shot_detail_capability() -> dict[str, Any]:
    """Expose a small capability payload without duplicating runtime checks."""
    from studio.services.creative_drafts import get_creative_capability

    capability = dict(get_creative_capability())
    policies = {
        provider["id"]: shot_detail_policy(provider["id"])
        for provider in capability.get("providers", [])
    }
    from studio.models import GenerationTask

    shared_resource_busy = GenerationTask.objects.filter(status__in=(
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )).exists()
    providers = capability.get("providers", [])
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        runtime_error = str(provider.get("runtime_probe_error_code") or "")
        local_install_ready = bool(
            provider.get("id") == LOCAL_QWEN_PROVIDER
            and provider.get("enabled")
            and provider.get("model_downloaded")
            and provider.get("model_verified")
            and runtime_error in {"", "shared_resource_busy"}
            and (
                provider.get("configured")
                or provider.get("runtime_ready")
                or runtime_error == "shared_resource_busy"
            )
        )
        provider["temporarily_busy"] = bool(
            local_install_ready
            and (
                shared_resource_busy
                or runtime_error == "shared_resource_busy"
            )
        )
    capability.update({
        "shot_high_detail_supported": True,
        "shot_high_detail_schema_version": SHOT_DETAIL_SCHEMA_VERSION,
        "shot_high_detail_policies": policies,
        # Busy is transient scheduling state and never changes whether a
        # provider is installed/configured.
        "shot_high_detail_shared_resource_busy": shared_resource_busy,
        "shot_high_detail_review_required": True,
    })
    return capability


def _dialogue_snapshot(
    shot: Shot, *, policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    policy = policy or shot_detail_policy()
    if not isinstance(shot.dialogue, list):
        raise ShotDetailValidationError("正式镜头的对白必须是列表，修复后再扩写")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(shot.dialogue):
        if not isinstance(item, dict):
            raise ShotDetailValidationError(f"正式镜头的第 {index + 1} 条对白格式无效")
        speaker_id = str(item.get("speaker_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not speaker_id or not text:
            raise ShotDetailValidationError(f"正式镜头的第 {index + 1} 条对白缺少角色或正文")
        entries.append({
            "speaker_id": speaker_id,
            "text": text,
            "delivery": str(item.get("delivery") or ""),
            "voiceover": bool(item.get("voiceover")),
            **({
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
            } if "start_ms" in item and "end_ms" in item else {}),
        })
    if len(entries) > policy["max_dialogue_per_shot"]:
        raise ShotDetailValidationError(
            "单镜高细节扩写当前对白数量超过配置的审阅范围"
        )
    return entries


def _shot_snapshot(
    shot: Shot, *, boundary: bool = False,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the formal source snapshot for one detail-review candidate.

    ``expansion_focus`` is deliberately narrow: the model expands camera motion
    and the action/visible picture only.  The remaining shot fields are kept as
    read-only continuity constraints and must never be rewritten by this flow.
    """
    value: dict[str, Any] = {
        "id": str(shot.id),
        "number": shot.number,
        "title": str(shot.title or ""),
        "narrative": str(shot.narrative or ""),
        "detailed_description": str(shot.detailed_description or ""),
        "camera": str(shot.camera or ""),
        "continuity_notes": str(shot.continuity_notes or ""),
        "requires_first_frame_continuity": bool(shot.requires_first_frame_continuity),
        "requires_last_frame_continuity": bool(shot.requires_last_frame_continuity),
        "scene_id": str(shot.scene_id) if shot.scene_id else None,
        "character_ids": [str(item.id) for item in shot.characters.all()],
        "prop_ids": [str(item.id) for item in shot.props.all()],
    }
    if boundary:
        return value
    value.update({
        "media_type": shot.media_type,
        "action": str(shot.action or ""),
        "dialogue": _dialogue_snapshot(shot, policy=policy),
        "sound_effects": str(shot.sound_effects or ""),
        "music": str(shot.music or ""),
        "duration_ms": shot.duration_ms,
        "h3_mode": shot.h3_mode,
        "include_audio": bool(shot.include_audio),
        "reference_bindings": shot.reference_bindings or {},
        "expansion_focus": {
            "camera_motion": str(shot.camera or ""),
            "action_and_visual_description": str(shot.action or ""),
            # Narrative is a visual-context fallback only.  It is not a third
            # topic to elaborate and never overwrites formal shot fields.
            "narrative_visual_context": str(shot.narrative or ""),
        },
    })
    return value


def _character_snapshot(item: CharacterBible) -> dict[str, str]:
    return {
        "id": str(item.id),
        "stable_speaker_id": item.stable_speaker_id,
        "name": item.name,
        "name_en": "",
        "role": item.role,
        "description": item.description,
        "visual_identity": item.visual_identity,
        "age_appearance": item.age_appearance,
        "face_identity": item.face_identity,
        "wardrobe": item.wardrobe,
        "voice_profile": item.voice_profile,
        "personality": item.personality,
        "negative_constraints": "",
    }


def _scene_snapshot(item: SceneBible) -> dict[str, str]:
    return {
        "id": str(item.id), "name": item.name, "name_en": "",
        "description": item.description, "visual_identity": item.visual_identity,
        "location": item.location, "time_of_day": item.time_of_day,
        "lighting": item.lighting, "color_palette": item.color_palette,
        "ambient_sound": item.ambient_sound, "negative_constraints": "",
    }


def _prop_snapshot(item: PropBible) -> dict[str, str]:
    return {
        "id": str(item.id), "name": item.name, "name_en": "",
        "description": item.description, "visual_identity": item.visual_identity,
        "material": item.material, "distinctive_marks": item.distinctive_marks,
        "continuity_state": item.continuity_state, "negative_constraints": "",
    }


def _adjacent_shots(shot: Shot) -> tuple[Shot | None, Shot | None]:
    """Return the immediate numeric boundaries only, never an entire episode."""
    base = Shot.objects.filter(episode_id=shot.episode_id).select_related("scene").prefetch_related(
        "characters", "props",
    )
    previous = base.filter(number__lt=shot.number).order_by("-number").first()
    following = base.filter(number__gt=shot.number).order_by("number").first()
    return previous, following


def shot_detail_context_snapshot(
    project: SeriesProject, episode: Episode, shot: Shot,
    *, policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact narrow source contract captured with each proposal."""
    if episode.project_id != project.id or shot.episode_id != episode.id:
        raise ShotDetailValidationError("镜头、剧集和项目必须属于同一项目")
    if shot.media_type != Shot.MediaType.VIDEO:
        raise ShotDetailValidationError("逐镜高细节扩写只支持视频镜头")
    policy = policy or shot_detail_policy()
    previous, following = _adjacent_shots(shot)
    character_values = list(shot.characters.all())
    prop_values = list(shot.props.all())
    scene_value = shot.scene
    target = _shot_snapshot(shot, policy=policy)
    # Canonical terms are collected from the two expansion sources and the
    # visual context only.  Dialogue, title, audio and continuity remain
    # constraints; pulling their names into the expansion glossary can make a
    # harmless locked line look like material that must be elaborated.
    source_text = "\n".join([
        target["camera"], target["action"], target["narrative"],
        *[item.name for item in character_values],
        *( [scene_value.name] if scene_value else [] ),
        *[item.name for item in prop_values],
    ])
    return {
        "schema_version": SHOT_DETAIL_SCHEMA_VERSION,
        "project": {
            "id": str(project.id),
            "title": project.title,
            "logline": project.logline,
            "continuity_summary": project.continuity_summary,
            "color_script": str((project.production_settings or {}).get("color_script", "")),
            "aspect_ratio": project.aspect_ratio,
            "translation_glossary": relevant_canonical_identity_mappings(project, source_text),
        },
        "style": ({
            "id": str(project.style_id), "name": project.style.name,
            "category": project.style.category, "description": project.style.description,
            "visual_prompt": project.style.visual_prompt,
        } if project.style else None),
        "episode": {
            "id": str(episode.id), "number": episode.number,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
        },
        "target_shot": target,
        "boundaries": {
            "previous": _shot_snapshot(previous, boundary=True, policy=policy) if previous else None,
            "next": _shot_snapshot(following, boundary=True, policy=policy) if following else None,
        },
        "bible": {
            "characters": [_character_snapshot(item) for item in character_values],
            "scenes": [_scene_snapshot(scene_value)] if scene_value else [],
            "props": [_prop_snapshot(item) for item in prop_values],
        },
    }


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def shot_detail_context_hash(
    project: SeriesProject, episode: Episode, shot: Shot,
    *, policy: dict[str, Any] | None = None,
) -> str:
    return _snapshot_hash(shot_detail_context_snapshot(
        project, episode, shot, policy=policy,
    ))


def _live_context_hash(
    project_id, episode_id, shot_id, *, policy: dict[str, Any] | None = None,
) -> str:
    project = SeriesProject.objects.select_related("style").get(pk=project_id)
    episode = Episode.objects.get(pk=episode_id, project=project)
    shot = Shot.objects.select_related("episode", "scene").prefetch_related("characters", "props").get(
        pk=shot_id, episode=episode,
    )
    return shot_detail_context_hash(project, episode, shot, policy=policy)


def _runtime_request(
    *, snapshot: dict[str, Any], context_hash: str, variation_note: str, attempt: int,
    project: SeriesProject, provider: str, request_id: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = creative_config()
    policy = policy or shot_detail_policy(provider)
    source = {
        "project": {
            "title": snapshot["project"]["title"],
            "logline": snapshot["project"]["logline"],
            "continuity_summary": "\n".join(filter(None, [
                snapshot["project"]["continuity_summary"],
                snapshot["episode"]["continuity_from_previous"],
                snapshot["episode"]["continuity_to_next"],
            ])),
            "color_script": snapshot["project"]["color_script"],
            "aspect_ratio": snapshot["project"]["aspect_ratio"],
        },
        "style": snapshot["style"],
        "glossary": snapshot["project"]["translation_glossary"],
        "target_shot": snapshot["target_shot"],
        "expansion_scope": {
            "primary_fields": snapshot["target_shot"].get("expansion_focus", {}),
            "read_only_constraints": {
                "title": snapshot["target_shot"].get("title", ""),
                "continuity_notes": snapshot["target_shot"].get("continuity_notes", ""),
                "dialogue": snapshot["target_shot"].get("dialogue", []),
                "sound_effects": snapshot["target_shot"].get("sound_effects", ""),
                "music": snapshot["target_shot"].get("music", ""),
            },
        },
        "boundaries": snapshot["boundaries"],
        "bible": snapshot["bible"],
    }
    context_size = len(json.dumps(source, ensure_ascii=False, separators=(",", ":")))
    if context_size > policy["context_max_chars"]:
        raise ShotDetailValidationError(
            f"当前镜头、相邻连续性和绑定 Bible 共 {context_size} 字符，超过单镜高细节 "
            f"{policy['context_max_chars']} 字符上限；请先精简正式字段或解绑无关素材",
        )
    return {
        "operation": SHOT_DETAIL_OPERATION,
        "request_id": str(request_id or context_hash[:24])[:160],
        "source_fingerprint": context_hash,
        "project": source["project"],
        "style": source["style"],
        "glossary": source["glossary"],
        "target_shot": source["target_shot"],
        # This mirrors the narrow scope sent to the model.  It is redundant
        # with target_shot.expansion_focus by design, so older runtimes can
        # ignore it while newer ones can present the boundary explicitly.
        "expansion_scope": source["expansion_scope"],
        "boundaries": source["boundaries"],
        "bible": source["bible"],
        "feedback": variation_note,
        "attempt": max(1, int(attempt)),
        "seed": int(project.continuity_seed) + max(1, int(attempt)),
        "model_dir": str(config["model_dir"]),
        "model_id": config["model_id"],
        "device": config["device"],
        "gpu_layers": config["gpu_layers"],
        "max_new_tokens": policy["output_tokens"],
        "shot_detail_policy": {
            key: value for key, value in policy.items() if key != "provider"
        },
        "context_size": config["context_size"],
        "min_context_size": config["min_context_size"],
        "max_context_size": config["max_context_size"],
        "reserved_input_tokens": config["reserved_input_tokens"],
        "timeout_seconds": config["timeout_seconds"],
    }


_STABLE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")
_STABLE_SPEAKER_RE = re.compile(r"(?<![A-Za-z0-9])S[1-9][0-9]*(?![A-Za-z0-9])")
_DIRECTION_CATEGORY_LABELS = {
    "subject_spatial_side": "角色方位",
    "body_side": "身体左右侧",
    "motion_direction": "运动方向",
    "spatial_side": "空间方位",
    "bilateral_sides": "双侧站位",
    "bilateral_motion": "双向运动",
}

_DETAIL_REVIEW_SUGGESTIONS = {
    "model_output_shape_normalized": "本地模型返回的 JSON 形状已自动整理；请人工核对两个正文栏是否完整。",
    "model_output_unusable_fallback": "本地模型输出不可用；当前候选由原镜头的镜头运动和动作/画面字段生成，请人工补写并核对。",
    "required_content_missing": "请补写缺失的中英文说明，或重新生成后再人工编辑。",
    "detail_zh_missing": "请补写中文高细节说明，供人工核验和后续翻译使用。",
    "detail_en_missing": "请补写英文高细节说明；英文确认后才会进入 H3 正式提示词。",
    "detail_zh_not_chinese": "中文栏可能缺少中文可视化说明，请人工改写。",
    "english_contains_cjk": "英文栏含中文，请人工改为可用于 H3 的英文导演说明。",
    "english_prompt_charset_invalid": "英文栏含非标准可用字符，请人工改写为可读英文。",
    "numeric_facts_mismatch": "请核对中英文中的数字、时长和数量事实。",
    "stable_speaker_id_mismatch": "请核对中英文中的 S-ID 是否一致。",
    "direction_semantics_mismatch": "请核对左右、手脚、站位和运动方向。",
    "canonical_glossary_missing": "请核对人物、场景和道具的 canonical English 名称。",
    "field_length_above_recommendation": "内容超过推荐长度；可人工精简，也可知情保留。",
    "field_length_above_transport_envelope": (
        "模型正文超过当前通道配置的传输建议范围；候选已完整保留，"
        "请人工精简并保存校验后再应用。"
    ),
    "canonical_target_collision": "多个 Bible 实体共用了同一英文 canonical 名称，请人工核对。",
    "model_output_protocol_validation_failed": (
        "模型返回了可编辑正文，但没有通过共享结构协议；请修改并重新保存后再应用。"
    ),
}

_SHOT_DETAIL_PROVIDER_ERROR_CODES = {
    "shot_high_detail_empty_output": "shot_detail_output_invalid_json",
    "shot_high_detail_output_invalid_json": "shot_detail_output_invalid_json",
    "shot_high_detail_incomplete_response": "shot_detail_output_invalid_json",
    "shot_high_detail_validation_failed": "shot_detail_validation_failed",
    "shot_high_detail_repair_validation_failed": "shot_detail_validation_failed",
    "shot_high_detail_output_budget_exhausted": "shot_detail_output_budget_exhausted",
}


def _detail_review_issue(
    code: str, path: str, *, severity: str = "risk",
) -> dict[str, str]:
    """Build text-free review metadata safe for APIs and audit records."""
    safe_code = re.sub(r"[^a-z0-9_.-]+", "_", str(code).lower()).strip("_")
    safe_path = re.sub(r"[^A-Za-z0-9_.\[\]-]+", "", str(path))[:180]
    safe_severity = severity if severity in {"warning", "risk", "requires_edit"} else "risk"
    issue = {
        "code": safe_code or "review_required",
        "severity": safe_severity,
        "path": safe_path,
    }
    if suggestion := _DETAIL_REVIEW_SUGGESTIONS.get(issue["code"]):
        issue["suggestion_zh"] = suggestion
    return issue


def _normalise_detail_review_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    issues: list[dict[str, str]] = []
    for item in value[:100]:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        severity = str(item.get("severity") or "risk").lower()
        if severity == "blocking":
            severity = "risk"
        issues.append(_detail_review_issue(
            str(item["code"]), str(item.get("path") or item.get("field_path") or ""),
            severity=severity,
        ))
    return issues


def _dedupe_detail_review_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.get("code", ""), issue.get("path", ""), issue.get("severity", ""))
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def _detail_review_summary(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": "needs_review" if issues else "ready",
        "issue_count": len(issues),
        "risk_count": sum(issue.get("severity") == "risk" for issue in issues),
        "requires_edit_count": sum(issue.get("severity") == "requires_edit" for issue in issues),
    }


def _detail_bilingual_review_issues(
    *, project: SeriesProject, detail_zh: str, detail_en: str,
) -> list[dict[str, str]]:
    """Return review prompts instead of discarding an editable draft."""
    issues: list[dict[str, str]] = []
    if _STABLE_NUMBER_RE.findall(detail_zh) != _STABLE_NUMBER_RE.findall(detail_en):
        issues.append(_detail_review_issue("numeric_facts_mismatch", "detail_en"))
    if _STABLE_SPEAKER_RE.findall(detail_zh) != _STABLE_SPEAKER_RE.findall(detail_en):
        issues.append(_detail_review_issue("stable_speaker_id_mismatch", "detail_en"))
    if direction_semantic_mismatches(detail_zh, detail_en):
        issues.append(_detail_review_issue("direction_semantics_mismatch", "detail_en"))
    for source, target in relevant_canonical_identity_mappings(project, detail_zh).items():
        if target.casefold() not in detail_en.casefold():
            issues.append(_detail_review_issue("canonical_glossary_missing", "detail_en"))
    return issues


def _validate_detail_data(
    candidate: Any, *, project: SeriesProject | None = None,
    runtime_review_issues: list[dict[str, str]] | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    policy = policy or shot_detail_policy()
    if not isinstance(candidate, dict):
        return dict(candidate or {}), {
            "status": "failed", "errors": ["高细节草稿必须只包含 schema_version、detail_zh、detail_en"],
            "warnings": [], "review_issues": [], "summary": _detail_review_summary([]),
        }
    # The isolated runtime can safely materialise this constant when it uses
    # llama.cpp's lightweight json_object mode.  Older/manual requests still
    # carry it explicitly.  Any other unknown/missing key remains structural.
    if "schema_version" not in candidate and set(candidate) == {"detail_zh", "detail_en"}:
        candidate = {"schema_version": SHOT_DETAIL_SCHEMA_VERSION, **candidate}
    if set(candidate) != {"schema_version", "detail_zh", "detail_en"}:
        return dict(candidate or {}), {
            "status": "failed", "errors": ["高细节草稿必须只包含 schema_version、detail_zh、detail_en"],
            "warnings": [], "review_issues": [], "summary": _detail_review_summary([]),
        }
    try:
        schema_version = int(candidate["schema_version"])
    except (TypeError, ValueError):
        schema_version = 0
    detail_zh = candidate.get("detail_zh")
    detail_en = candidate.get("detail_en")
    errors: list[str] = []
    warnings: list[str] = []
    if schema_version != SHOT_DETAIL_SCHEMA_VERSION:
        errors.append("高细节草稿 schema_version 不受支持")
    if not isinstance(detail_zh, str) or not isinstance(detail_en, str):
        errors.append("detail_zh 和 detail_en 必须是文本")
    if isinstance(detail_zh, str) and _has_unsafe_text_controls(detail_zh):
        errors.append("detail_zh 包含不可安全存储的控制字符")
    if isinstance(detail_en, str) and _has_unsafe_text_controls(detail_en):
        errors.append("detail_en 包含不可安全存储的控制字符")
    normalized = {
        "schema_version": SHOT_DETAIL_SCHEMA_VERSION,
        "detail_zh": detail_zh if isinstance(detail_zh, str) else "",
        "detail_en": detail_en if isinstance(detail_en, str) else "",
    }
    if not errors and not (normalized["detail_zh"].strip() or normalized["detail_en"].strip()):
        errors.append("高细节草稿没有可编辑正文")
    if errors:
        return normalized, {
            "status": "failed", "errors": errors, "warnings": [],
            "review_issues": [], "summary": _detail_review_summary([]),
        }

    issues = _normalise_detail_review_issues(runtime_review_issues)
    if len(normalized["detail_zh"]) > policy["transport_zh_max_chars"]:
        issues.append(_detail_review_issue(
            "field_length_above_transport_envelope", "detail_zh",
            severity="requires_edit",
        ))
    if len(normalized["detail_en"]) > policy["transport_en_max_chars"]:
        issues.append(_detail_review_issue(
            "field_length_above_transport_envelope", "detail_en",
            severity="requires_edit",
        ))
    if not normalized["detail_zh"].strip():
        issues.append(_detail_review_issue("detail_zh_missing", "detail_zh", severity="requires_edit"))
    elif not CJK_RE.search(normalized["detail_zh"]):
        issues.append(_detail_review_issue("detail_zh_not_chinese", "detail_zh", severity="requires_edit"))
    if not normalized["detail_en"].strip():
        issues.append(_detail_review_issue("detail_en_missing", "detail_en", severity="requires_edit"))
    elif CJK_RE.search(normalized["detail_en"]):
        issues.append(_detail_review_issue("english_contains_cjk", "detail_en", severity="requires_edit"))
    elif _has_forbidden_english_director_text(normalized["detail_en"]):
        issues.append(_detail_review_issue("english_prompt_charset_invalid", "detail_en", severity="requires_edit"))
    if len(normalized["detail_zh"]) > policy["recommended_zh_chars"]:
        issues.append(_detail_review_issue("field_length_above_recommendation", "detail_zh"))
    if len(normalized["detail_en"]) > policy["recommended_en_chars"]:
        issues.append(_detail_review_issue("field_length_above_recommendation", "detail_en"))
    if project is not None and normalized["detail_zh"].strip() and normalized["detail_en"].strip():
        issues.extend(_detail_bilingual_review_issues(
            project=project, detail_zh=normalized["detail_zh"], detail_en=normalized["detail_en"],
        ))
    if (
        len(normalized["detail_zh"]) < policy["review_min_zh_chars"]
        or len(normalized["detail_en"]) < policy["review_min_en_chars"]
    ):
        warnings.append("高细节草稿较短；可补充构图、空间关系、动作过程、收束状态和光线材质")
    if len(normalized["detail_zh"]) >= max(
        1,
        policy["recommended_zh_chars"] - policy["review_near_zh_margin_chars"],
    ):
        warnings.append("中文细节已接近推荐长度；避免继续叠加新的事件")
    if len(normalized["detail_en"]) >= max(
        1,
        policy["recommended_en_chars"] - policy["review_near_en_margin_chars"],
    ):
        warnings.append("英文细节已接近推荐长度；避免继续叠加新的事件")
    issues = _dedupe_detail_review_issues(issues)
    summary = _detail_review_summary(issues)
    return normalized, {
        "status": summary["status"],
        "errors": [],
        "warnings": warnings,
        "review_issues": issues,
        "summary": summary,
    }


def _safe_source_starter(
    snapshot: dict[str, Any], *, policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build an editable Chinese starter from authoritative expansion fields.

    This is a no-model recovery path for malformed local-model output.  It
    never copies dialogue/audio/title fields, never invents English, and strips
    display-affecting controls before the normal validator sees the candidate.
    """
    target = snapshot.get("target_shot") if isinstance(snapshot, dict) else None
    if not isinstance(target, dict):
        return None
    focus = target.get("expansion_focus")
    if not isinstance(focus, dict):
        focus = {}

    def clean(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return "".join(
            char for char in value.strip()
            if char in "\n\r\t" or unicodedata.category(char) not in {"Cc", "Cf"}
        )

    camera = clean(focus.get("camera_motion") or target.get("camera"))
    action = clean(focus.get("action_and_visual_description") or target.get("action"))
    narrative = clean(focus.get("narrative_visual_context") or target.get("narrative"))
    labelled = []
    if camera:
        labelled.append(("镜头运动：", camera))
    if action:
        labelled.append(("动作与画面描述：", action))
    elif narrative:
        labelled.append(("动作与画面描述：", narrative))

    parts: list[str] = []
    remaining = (policy or shot_detail_policy())["transport_zh_max_chars"]
    for label, value in labelled:
        separator = "\n" if parts else ""
        available = remaining - len(separator) - len(label)
        if available <= 0:
            break
        piece = separator + label + value[:available]
        parts.append(piece)
        remaining -= len(piece)
    detail_zh = "".join(parts).strip()
    if not detail_zh:
        return None
    return {
        "schema_version": SHOT_DETAIL_SCHEMA_VERSION,
        "detail_zh": detail_zh,
        "detail_en": "",
    }


def _source_fallback_candidate(
    snapshot: dict[str, Any], *, project: SeriesProject,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]] | None:
    starter = _safe_source_starter(snapshot, policy=policy)
    if starter is None:
        return None
    data, validation = _validate_detail_data(
        starter,
        project=project,
        policy=policy,
        runtime_review_issues=[_detail_review_issue(
            "model_output_unusable_fallback", "detail_zh", severity="requires_edit",
        )],
    )
    return data, validation


def _salvage_deepseek_detail_candidate(
    error: CreativeDraftError, *, project: SeriesProject,
    policy: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]] | None:
    """Keep only a bounded, editable bilingual candidate from protocol failure.

    The raw model response never crosses this boundary.  At least one known
    prose field must contain text; a missing peer becomes the existing
    requires-edit issue.  The normal server validator still rejects unsafe
    controls and transport-limit violations.  The protocol failure also
    remains a requires-edit issue, so salvaging never makes the model output
    directly applicable.
    """
    raw = getattr(error, "structured_candidate", None)
    if not isinstance(raw, dict):
        return None
    detail_zh = raw.get("detail_zh") if isinstance(raw.get("detail_zh"), str) else ""
    detail_en = raw.get("detail_en") if isinstance(raw.get("detail_en"), str) else ""
    if not detail_zh.strip() and not detail_en.strip():
        return None
    data, validation = _validate_detail_data(
        {
            "schema_version": SHOT_DETAIL_SCHEMA_VERSION,
            "detail_zh": detail_zh,
            "detail_en": detail_en,
        },
        project=project,
        policy=policy,
        runtime_review_issues=[_detail_review_issue(
            "model_output_protocol_validation_failed",
            "result",
            severity="requires_edit",
        )],
    )
    if validation.get("status") == "failed":
        return None
    return data, validation


def _runtime_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("runtime") or payload.get("metadata") or {}
    metadata = dict(raw) if isinstance(raw, dict) else {}
    metadata.update({
        "provider": str(payload.get("provider") or metadata.get("provider") or LOCAL_QWEN_PROVIDER),
        "model": str(payload.get("model") or metadata.get("model") or ""),
        "operation": SHOT_DETAIL_OPERATION,
    })
    is_local = metadata["provider"] == LOCAL_QWEN_PROVIDER
    metadata.setdefault("transport", "local_process" if is_local else "remote_api")
    metadata.setdefault("loaded_on_demand", is_local)
    metadata.setdefault("unloaded_after_request", is_local)
    return metadata


def _next_generation_no(shot: Shot) -> int:
    return int(shot.detail_drafts.aggregate(value=Max("generation_no"))["value"] or 0) + 1


def generate_shot_detail_candidate(
    *, project: SeriesProject, episode: Episode, shot: Shot, variation_note: str, attempt: int,
    provider: str | None = None, expected_context_hash: str | None = None,
    request_id: str | None = None, policy_override: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any], list[str], str, dict[str, Any]]:
    provider = normalise_creative_provider(provider)
    policy = resolve_shot_detail_policy(provider, policy_override)
    config = creative_config()
    snapshot = shot_detail_context_snapshot(project, episode, shot, policy=policy)
    context_hash = _snapshot_hash(snapshot)
    if expected_context_hash and context_hash != expected_context_hash:
        raise ShotDetailContextConflict(
            "当前镜头、相邻连续性或绑定 Bible 已变化，本次批量扩写未调用模型"
        )
    request = _runtime_request(
        snapshot=snapshot, context_hash=context_hash, variation_note=variation_note,
        attempt=attempt, project=project, provider=provider, request_id=request_id,
        policy=policy,
    )
    try:
        if provider == LOCAL_QWEN_PROVIDER:
            # Keep the established local runtime seam explicit.  Besides
            # preserving the resource/availability contract, this lets tests
            # and operators probe the local path without invoking an optional
            # remote provider.
            ensure_creative_resources_available()
            _require_runtime(config)
            payload = _run_runtime(request, config)
        else:
            payload = _run_provider_runtime(request, config, provider)
    except CreativeDraftError as exc:
        exc.attempt_context_snapshot = snapshot
        exc.attempt_context_hash = context_hash
        exc.code = _SHOT_DETAIL_PROVIDER_ERROR_CODES.get(exc.code, exc.code)
        try:
            exc.source_drift_during_attempt = (
                _live_context_hash(
                    project.id, episode.id, shot.id, policy=policy,
                ) != context_hash
            )
        except Exception:
            exc.source_drift_during_attempt = True
        exc.runtime_metadata.update({
            "error_code": exc.code,
            "attempt_context_hash": context_hash,
            "source_drift_during_attempt": exc.source_drift_during_attempt,
            "operation": SHOT_DETAIL_OPERATION,
        })
        salvaged = None
        if (
            provider == "deepseek_api"
            and exc.code == "shot_detail_validation_failed"
            and not exc.source_drift_during_attempt
        ):
            salvaged = _salvage_deepseek_detail_candidate(
                exc, project=project, policy=policy,
            )
        if salvaged is not None:
            if _live_context_hash(
                project.id, episode.id, shot.id, policy=policy,
            ) != context_hash:
                raise ShotDetailContextConflict(
                    "生成期间镜头、相邻连续性或绑定 Bible 已变更，请重新生成"
                ) from exc
            data, validation = salvaged
            runtime = _runtime_metadata({
                "provider": provider,
                "model": exc.runtime_metadata.get("model", ""),
                "metadata": exc.runtime_metadata,
            })
            runtime.update({
                "error_code": exc.code,
                "review_required": True,
                "review_issue_count": len(validation.get("review_issues", [])),
                "fallback_generated": False,
                "salvaged_structured_candidate": True,
                "salvage_reason_code": exc.code,
            })
            return (
                data, validation, runtime, validation["warnings"],
                context_hash, snapshot,
            )
        raise
    result = payload.get("result")
    echoed_fingerprint = str(
        payload.get("source_fingerprint")
        or (result.get("source_fingerprint", "") if isinstance(result, dict) else "")
    )
    if echoed_fingerprint != context_hash:
        raise ShotDetailValidationError("高细节运行时未回显匹配的 source_fingerprint")
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    runtime_issues = _normalise_detail_review_issues(
        payload.get("review_issues", result_payload.get("review_issues")),
    )
    data, validation = _validate_detail_data(
        result, project=project, runtime_review_issues=runtime_issues,
        policy=policy,
    )
    fallback_used = False
    if validation.get("status") == "failed" and provider == LOCAL_QWEN_PROVIDER:
        fallback = _source_fallback_candidate(
            snapshot, project=project, policy=policy,
        )
        if fallback is not None:
            data, validation = fallback
            fallback_used = True
    if _live_context_hash(
        project.id, episode.id, shot.id, policy=policy,
    ) != context_hash:
        raise ShotDetailContextConflict("生成期间镜头、相邻连续性或绑定 Bible 已变更，请重新生成")
    runtime = _runtime_metadata(payload)
    runtime.update({
        "review_required": bool(validation.get("review_issues")),
        "review_issue_count": len(validation.get("review_issues", [])),
        "fallback_generated": fallback_used,
        "fallback_reason_code": "model_output_unusable" if fallback_used else "",
    })
    return data, validation, runtime, validation["warnings"], context_hash, snapshot


def append_revision_history(draft: ShotDetailDraft) -> None:
    draft.revision_history = [*(draft.revision_history or []), {
        "revision": draft.revision,
        "status": draft.status,
        "context_hash": draft.context_hash,
        "context_snapshot": draft.context_snapshot,
        "draft_data": draft.draft_data,
        "validation": draft.validation,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }]


def create_shot_detail_draft(
    *, project: SeriesProject, episode: Episode, shot: Shot, user, variation_note: str,
    parent: ShotDetailDraft | None = None, expected_parent_revision: int | None = None,
    provider: str = LOCAL_QWEN_PROVIDER,
    expected_context_hash: str | None = None, request_id: str | None = None,
    batch_item: ShotDetailDraftBatchItem | None = None,
    policy_override: dict[str, Any] | None = None,
) -> ShotDetailDraft:
    provider = normalise_creative_provider(provider)
    policy = resolve_shot_detail_policy(provider, policy_override)
    attempt = max(_next_generation_no(shot), (parent.generation_no + 1) if parent else 1)
    data, validation, runtime, warnings, context_hash, snapshot = generate_shot_detail_candidate(
        project=project, episode=episode, shot=shot, variation_note=variation_note, attempt=attempt,
        provider=provider, expected_context_hash=expected_context_hash, request_id=request_id,
        policy_override=policy,
    )
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().select_related("style").get(pk=project.pk)
        locked_episode = Episode.objects.select_for_update().get(pk=episode.pk, project=locked_project)
        locked_shot = Shot.objects.select_for_update().select_related("scene").prefetch_related(
            "characters", "props",
        ).get(pk=shot.pk, episode=locked_episode)
        if shot_detail_context_hash(
            locked_project, locked_episode, locked_shot, policy=policy,
        ) != context_hash:
            raise ShotDetailContextConflict("保存高细节草稿前上下文已变更，请重新生成")
        locked_parent = None
        if parent:
            locked_parent = ShotDetailDraft.objects.select_for_update().get(pk=parent.pk, shot=locked_shot)
            if expected_parent_revision is not None and locked_parent.revision != expected_parent_revision:
                raise ShotDetailRevisionConflict("原高细节草稿版本已变化，请刷新后重新生成")
        locked_batch_item = None
        if batch_item:
            locked_batch_item = ShotDetailDraftBatchItem.objects.select_for_update().get(
                pk=batch_item.pk, shot=locked_shot,
            )
        draft = ShotDetailDraft.objects.create(
            project=locked_project, episode=locked_episode, shot=locked_shot, created_by=user,
            parent_draft=locked_parent, generation_no=_next_generation_no(locked_shot),
            context_hash=context_hash, context_snapshot=snapshot,
            input_settings={
                "provider": provider,
                "actual_output_token_budget": runtime.get("actual_output_token_budget"),
                "policy": {key: value for key, value in policy.items() if key != "provider"},
                "attempt": attempt,
            },
            variation_note=variation_note, draft_data=data, validation=validation, warnings=warnings,
            status=(ShotDetailDraft.Status.DRAFT if validation["status"] in {"passed", "ready", "needs_review"}
                    else ShotDetailDraft.Status.FAILED),
            provider=provider,
            model=str(runtime.get("model") or (
                PINNED_MODEL_REPO_ID if provider == LOCAL_QWEN_PROVIDER else ""
            )),
            runtime_metadata=runtime,
            error_message=("" if validation["status"] in {"passed", "ready", "needs_review"}
                            else "；".join(validation["errors"])[:10_000]),
        )
        if locked_parent and draft.status == ShotDetailDraft.Status.DRAFT and locked_parent.status != ShotDetailDraft.Status.APPLIED:
            locked_parent.status = ShotDetailDraft.Status.SUPERSEDED
            locked_parent.revision += 1
            locked_parent.save(update_fields=("status", "revision", "updated_at"))
        if locked_batch_item:
            locked_batch_item.draft = draft
            locked_batch_item.save(update_fields=("draft", "updated_at"))
    return draft


def create_failed_shot_detail_draft(
    *, project: SeriesProject, episode: Episode, shot: Shot, user, variation_note: str,
    error: str, error_code: str, runtime_metadata: dict[str, Any] | None = None,
    attempt_context_snapshot: dict[str, Any] | None = None, attempt_context_hash: str = "",
    source_drift_during_attempt: bool = False, parent: ShotDetailDraft | None = None,
    expected_parent_revision: int | None = None,
    provider: str = LOCAL_QWEN_PROVIDER,
    batch_item: ShotDetailDraftBatchItem | None = None,
    policy_override: dict[str, Any] | None = None,
) -> ShotDetailDraft:
    provider = normalise_creative_provider(provider)
    policy = resolve_shot_detail_policy(provider, policy_override)
    provider_label = shot_detail_provider_label(provider)
    safe_messages = {
        "shot_detail_output_invalid_json": f"{provider_label} 返回的高细节格式不完整，未生成可审核候选。",
        "shot_high_detail_output_invalid_json": f"{provider_label} 返回的高细节格式不完整，未生成可审核候选。",
        "shot_detail_validation_failed": f"{provider_label} 返回了高细节正文，但结构仍需人工修订。",
        "shot_high_detail_validation_failed": f"{provider_label} 返回了高细节正文，但结构仍需人工修订。",
        "shot_detail_output_budget_exhausted": f"{provider_label} 在完成高细节候选前达到输出容量上限。",
        "shot_detail_timeout": f"{provider_label} 生成高细节候选超时。",
    }
    safe_error = safe_messages.get(
        error_code, f"{provider_label} 未返回可审核的高细节候选；正式镜头未修改。",
    )
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().select_related("style").get(pk=project.pk)
        locked_episode = Episode.objects.select_for_update().get(pk=episode.pk, project=locked_project)
        locked_shot = Shot.objects.select_for_update().select_related("scene").prefetch_related(
            "characters", "props",
        ).get(pk=shot.pk, episode=locked_episode)
        locked_parent = None
        if parent:
            locked_parent = ShotDetailDraft.objects.select_for_update().get(pk=parent.pk, shot=locked_shot)
            if expected_parent_revision is not None and locked_parent.revision != expected_parent_revision:
                raise ShotDetailRevisionConflict("原高细节草稿版本已变化，未记录过期失败尝试")
        locked_batch_item = None
        if batch_item:
            locked_batch_item = ShotDetailDraftBatchItem.objects.select_for_update().get(
                pk=batch_item.pk, shot=locked_shot,
            )
        snapshot = attempt_context_snapshot or shot_detail_context_snapshot(
            locked_project, locked_episode, locked_shot, policy=policy,
        )
        context_hash = attempt_context_hash or _snapshot_hash(snapshot)
        fallback = None
        if (
            provider == LOCAL_QWEN_PROVIDER
            and not source_drift_during_attempt
            and error_code in {
                "shot_detail_output_invalid_json", "shot_detail_validation_failed",
                "shot_high_detail_output_invalid_json", "shot_high_detail_validation_failed",
            }
        ):
            fallback = _source_fallback_candidate(
                snapshot, project=locked_project, policy=policy,
            )
        fallback_data, fallback_validation = fallback or ({}, None)
        draft = ShotDetailDraft.objects.create(
            project=locked_project, episode=locked_episode, shot=locked_shot, created_by=user,
            parent_draft=locked_parent, generation_no=_next_generation_no(locked_shot),
            context_hash=context_hash, context_snapshot=snapshot,
            input_settings={
                "provider": provider,
                "runtime_error_code": error_code,
                "source_drift_during_attempt": bool(source_drift_during_attempt),
                "policy": {key: value for key, value in policy.items() if key != "provider"},
            },
            variation_note=variation_note,
            draft_data=fallback_data,
            status=(ShotDetailDraft.Status.DRAFT if fallback_validation else ShotDetailDraft.Status.FAILED),
            validation=(fallback_validation or {
                "status": "failed", "errors": [safe_error], "warnings": [],
                "review_issues": [], "summary": _detail_review_summary([]),
            }),
            warnings=(fallback_validation.get("warnings", []) if fallback_validation else []),
            error_message=("" if fallback_validation else safe_error), provider=provider,
            model=str((runtime_metadata or {}).get("model") or "")[:200],
            runtime_metadata={
                "operation": SHOT_DETAIL_OPERATION,
                "provider": provider,
                "transport": "local_process" if provider == LOCAL_QWEN_PROVIDER else "remote_api",
                "loaded_on_demand": provider == LOCAL_QWEN_PROVIDER,
                "unloaded_after_request": provider == LOCAL_QWEN_PROVIDER,
                "error_code": error_code,
                "attempt_context_hash": context_hash,
                "source_drift_during_attempt": bool(source_drift_during_attempt),
                "fallback_generated": bool(fallback_validation),
                "fallback_reason_code": (
                    "model_output_unusable" if fallback_validation else ""
                ),
                **dict(runtime_metadata or {}),
            },
        )
        if (
            fallback_validation and locked_parent
            and locked_parent.status != ShotDetailDraft.Status.APPLIED
        ):
            locked_parent.status = ShotDetailDraft.Status.SUPERSEDED
            locked_parent.revision += 1
            locked_parent.save(update_fields=("status", "revision", "updated_at"))
        if locked_batch_item:
            locked_batch_item.draft = draft
            locked_batch_item.save(update_fields=("draft", "updated_at"))
        return draft


def shot_detail_review_issues(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (validation or {}).get("review_issues", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def shot_detail_requires_edit_issues(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        item for item in shot_detail_review_issues(validation)
        if item.get("severity") == "requires_edit"
    ]


def append_shot_detail_review_acknowledgement(
    draft: ShotDetailDraft, *, user, action: str,
) -> None:
    """Audit a human override without duplicating candidate prose in metadata."""
    issues = shot_detail_review_issues(draft.validation)
    if not issues:
        return
    metadata = dict(draft.runtime_metadata or {})
    entries = metadata.get("review_acknowledgements", [])
    acknowledgements = list(entries) if isinstance(entries, list) else []
    acknowledgements.append({
        "action": action if action in {"confirm", "apply"} else "review",
        "acknowledged_by": str(user.pk),
        "acknowledged_at": timezone.now().isoformat(),
        "draft_revision": int(draft.revision),
        "issue_codes": sorted({str(item.get("code") or "review_required")[:80] for item in issues}),
    })
    metadata["review_acknowledgements"] = acknowledgements[-20:]
    draft.runtime_metadata = metadata


def confirm_shot_detail_draft(
    *, draft: ShotDetailDraft, user, expected_revision: int, review_acknowledged: bool = False,
) -> ShotDetailDraft:
    identity = ShotDetailDraft.objects.only(
        "project_id", "episode_id", "shot_id",
    ).get(pk=draft.pk)
    with transaction.atomic():
        project = SeriesProject.objects.select_for_update().select_related("style").get(pk=identity.project_id)
        episode = Episode.objects.select_for_update().get(pk=identity.episode_id, project=project)
        shot = Shot.objects.select_for_update().select_related("scene").prefetch_related("characters", "props").get(
            pk=identity.shot_id, episode=episode,
        )
        locked = ShotDetailDraft.objects.select_for_update().select_related(
            "project", "episode", "shot",
        ).get(
            pk=draft.pk, project=project, episode=episode, shot=shot,
        )
        if locked.revision != expected_revision:
            raise ShotDetailRevisionConflict("高细节草稿版本已变化，请刷新后再确认")
        if locked.status != ShotDetailDraft.Status.DRAFT:
            raise ShotDetailValidationError("只有待审核的高细节草稿可以确认")
        policy = shot_detail_draft_policy(locked)
        if shot_detail_context_hash(
            project, episode, shot, policy=policy,
        ) != locked.context_hash:
            raise ShotDetailContextConflict("当前镜头、相邻连续性或绑定 Bible 已变化，请重新生成")
        # Preserve runtime-only advisory signals across the human-review flow.
        # Editing may clear issues that no longer apply, but confirming a draft
        # must not silently erase an unresolved warning merely because it was
        # first detected by the isolated Qwen runtime.
        data, validation = _validate_detail_data(
            locked.draft_data,
            project=project,
            runtime_review_issues=shot_detail_review_issues(locked.validation),
            policy=policy,
        )
        if validation["status"] not in {"passed", "ready", "needs_review"}:
            raise ShotDetailValidationError("高细节草稿未通过校验", errors=validation["errors"])
        if shot_detail_review_issues(validation) and not review_acknowledged:
            raise ShotDetailValidationError(
                "请先阅读高细节草稿的人工核验项并明确确认风险",
                code="shot_detail_review_acknowledgement_required",
            )
        append_revision_history(locked)
        locked.revision += 1
        locked.draft_data = data
        locked.validation = validation
        locked.warnings = validation["warnings"]
        locked.status = ShotDetailDraft.Status.CONFIRMED
        locked.confirmed_by = user
        locked.confirmed_at = timezone.now()
        locked.error_message = ""
        if shot_detail_review_issues(validation):
            append_shot_detail_review_acknowledgement(locked, user=user, action="confirm")
        locked.save()
    return locked


def patch_shot_detail_draft(
    *, draft: ShotDetailDraft, expected_revision: int, draft_data: dict[str, Any],
) -> ShotDetailDraft:
    identity = ShotDetailDraft.objects.only(
        "project_id", "episode_id", "shot_id",
    ).get(pk=draft.pk)
    with transaction.atomic():
        project = SeriesProject.objects.select_for_update().select_related("style").get(pk=identity.project_id)
        episode = Episode.objects.select_for_update().get(pk=identity.episode_id, project=project)
        shot = Shot.objects.select_for_update().select_related("scene").prefetch_related("characters", "props").get(
            pk=identity.shot_id, episode=episode,
        )
        locked = ShotDetailDraft.objects.select_for_update().select_related(
            "project", "episode", "shot",
        ).get(
            pk=draft.pk, project=project, episode=episode, shot=shot,
        )
        if locked.revision != expected_revision:
            raise ShotDetailRevisionConflict("高细节草稿版本已变化，请刷新后再编辑")
        if locked.status not in {ShotDetailDraft.Status.DRAFT, ShotDetailDraft.Status.FAILED}:
            raise ShotDetailValidationError("只有待审核或校验失败的高细节草稿可以编辑")
        policy = shot_detail_draft_policy(locked)
        if shot_detail_context_hash(
            project, episode, shot, policy=policy,
        ) != locked.context_hash:
            raise ShotDetailContextConflict("当前镜头、相邻连续性或绑定 Bible 已变化，请重新生成")
        data, validation = _validate_detail_data(
            draft_data, project=project, policy=policy,
        )
        append_revision_history(locked)
        locked.revision += 1
        locked.draft_data = data
        locked.validation = validation
        locked.warnings = validation["warnings"]
        locked.status = (ShotDetailDraft.Status.DRAFT if validation["status"] in {"passed", "ready", "needs_review"}
                          else ShotDetailDraft.Status.FAILED)
        locked.error_message = "" if validation["status"] in {"passed", "ready", "needs_review"} else "；".join(validation["errors"])[:10_000]
        locked.confirmed_by = None
        locked.confirmed_at = None
        locked.save()
    return locked


def _write_confirmed_translation(
    *, project: SeriesProject, shot: Shot, source: str, translated: str, user,
    draft: ShotDetailDraft,
) -> None:
    """Record the bilingual approval using the existing current-source overlay.

    The formal Shot stores English after bilingual application.  The audit
    history still preserves the original Chinese candidate and the draft ID.
    """
    context = build_translation_context(
        project, scope_type=PromptFieldTranslation.ScopeType.SHOT, scope_id=shot.id,
        field_key="shot.detailed_description", source_text=translated,
    )
    entry, created = PromptFieldTranslation.objects.select_for_update().get_or_create(
        project=project, scope_type=PromptFieldTranslation.ScopeType.SHOT, scope_id=shot.id,
        field_key="shot.detailed_description",
        defaults={"source_text": translated, "source_hash": source_text_hash(translated)},
    )
    history = {
        "revision": 0 if created else entry.revision,
        "source_text": source,
        "translated_text": translated,
        "status": "shot_detail_bilingual_confirmed_before_promotion",
        "event": "shot_detail_translation_promoted",
        "confirmed_by": str(user.pk),
        "updated_at": timezone.now().isoformat(),
    }
    entry.revision_history = [*(entry.revision_history or []), history]
    if not created:
        entry.revision += 1
    entry.source_text = translated
    entry.source_hash = source_text_hash(translated)
    entry.translated_text = translated
    entry.status = PromptFieldTranslation.Status.CONFIRMED
    entry.provider = f"{draft.provider}_shot_detail"
    entry.model = draft.model
    entry.error_message = ""
    entry.runtime_metadata = {
        "translation_context_hash": translation_context_hash(context),
        "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        "shot_detail_draft_id": str(draft.id),
        "human_review_acknowledged": True,
        "source_provider": draft.provider,
        "transport": (draft.runtime_metadata or {}).get("transport", ""),
        "loaded_on_demand": bool((draft.runtime_metadata or {}).get("loaded_on_demand")),
        "unloaded_after_request": bool((draft.runtime_metadata or {}).get("unloaded_after_request")),
        "promotion": {
            "promoted": True,
            "event": "shot_detail_translation_promoted",
            "promoted_by": str(user.pk),
            "shot_id": str(shot.id),
            "field_key": "shot.detailed_description",
            "original_source_hash": source_text_hash(source),
            "promoted_value_sha256": source_text_hash(translated),
            "shot_detail_draft_id": str(draft.id),
            "applied_from_revision": entry.revision,
            "last_confirmed_at": timezone.now().isoformat(),
        },
    }
    entry.confirmed_by = user
    entry.confirmed_at = timezone.now()
    entry.save()


def prepare_locked_shot_detail_application(
    *, draft: ShotDetailDraft, project: SeriesProject, episode: Episode, shot: Shot,
    user, language_mode: str, expected_revision: int,
    review_acknowledged: bool = False, allow_draft: bool = False,
) -> PreparedShotDetailApplication:
    """Preflight one already-locked draft without writing formal data."""
    if draft.revision != expected_revision:
        raise ShotDetailRevisionConflict("高细节草稿版本已变化，请刷新后再应用")
    allowed_statuses = {ShotDetailDraft.Status.CONFIRMED}
    if allow_draft:
        allowed_statuses.add(ShotDetailDraft.Status.DRAFT)
    if draft.status not in allowed_statuses:
        raise ShotDetailValidationError(
            "只有待审核或已确认的高细节草稿可以应用"
            if allow_draft else "只有已确认的高细节草稿可以应用",
        )
    policy = shot_detail_draft_policy(draft)
    if shot_detail_context_hash(
        project, episode, shot, policy=policy,
    ) != draft.context_hash:
        raise ShotDetailContextConflict(
            "当前镜头、相邻连续性或绑定 Bible 已变化，请重新生成并确认",
        )
    data, validation = _validate_detail_data(
        draft.draft_data,
        project=project,
        runtime_review_issues=shot_detail_review_issues(draft.validation),
        policy=policy,
    )
    if validation["status"] not in {"passed", "ready", "needs_review"}:
        raise ShotDetailValidationError(
            "高细节草稿复验失败，不能应用", errors=validation["errors"],
        )
    if shot_detail_requires_edit_issues(validation):
        raise ShotDetailValidationError(
            "高细节草稿仍有必须人工修复的内容，修正后再应用",
            code="shot_detail_requires_edit",
        )
    if shot_detail_review_issues(validation) and not review_acknowledged:
        raise ShotDetailValidationError(
            "请先阅读高细节草稿的人工核验项并明确确认风险",
            code="shot_detail_review_acknowledgement_required",
        )
    translated = language_mode == "bilingual_confirmed"
    if translated:
        next_value = data["detail_en"]
    elif language_mode == "chinese_only":
        next_value = data["detail_zh"]
    else:
        raise ShotDetailValidationError(
            "language_mode 必须为 chinese_only 或 bilingual_confirmed",
        )
    return PreparedShotDetailApplication(
        draft=draft, project=project, episode=episode, shot=shot, user=user,
        language_mode=language_mode, data=data, validation=validation,
        translated=translated, next_value=next_value,
        generation_context_hash=draft.context_hash,
        generation_context_snapshot=draft.context_snapshot,
        policy=policy,
    )


def commit_prepared_shot_detail_application(
    prepared: PreparedShotDetailApplication,
) -> None:
    """Write a preflighted plan; caller owns the surrounding transaction."""
    draft = prepared.draft
    shot = prepared.shot
    now = timezone.now()
    if draft.status == ShotDetailDraft.Status.DRAFT:
        append_revision_history(draft)
        draft.revision += 1
        draft.status = ShotDetailDraft.Status.CONFIRMED
        draft.confirmed_by = prepared.user
        draft.confirmed_at = now
        draft.draft_data = prepared.data
        draft.validation = prepared.validation
        draft.warnings = prepared.validation["warnings"]
        draft.error_message = ""
        if shot_detail_review_issues(prepared.validation):
            append_shot_detail_review_acknowledgement(
                draft, user=prepared.user, action="confirm",
            )
        draft.save()

    metadata = dict(shot.metadata or {})
    metadata["shot_detail_provenance"] = {
        "shot_detail_draft_id": str(draft.id),
        "generation_context_hash": prepared.generation_context_hash,
        "applied_language_mode": prepared.language_mode,
        "applied_at": now.isoformat(),
    }
    shot.detailed_description = prepared.next_value
    shot.metadata = metadata
    shot.save(update_fields=("detailed_description", "metadata", "updated_at"))
    if prepared.translated:
        _write_confirmed_translation(
            project=prepared.project, shot=shot,
            source=prepared.data["detail_zh"],
            translated=prepared.data["detail_en"],
            user=prepared.user, draft=draft,
        )

    append_revision_history(draft)
    draft.revision += 1
    draft.project = prepared.project
    draft.episode = prepared.episode
    draft.shot = shot
    draft.draft_data = prepared.data
    draft.validation = prepared.validation
    draft.warnings = prepared.validation["warnings"]
    draft.status = ShotDetailDraft.Status.APPLIED
    draft.applied_by = prepared.user
    draft.applied_at = now
    if shot_detail_review_issues(prepared.validation):
        append_shot_detail_review_acknowledgement(
            draft, user=prepared.user, action="apply",
        )
    draft.save()


def finalize_prepared_shot_detail_application(
    prepared: PreparedShotDetailApplication,
) -> None:
    """Rebase context after every selected batch shot has been written."""
    draft = prepared.draft
    shot = prepared.shot
    applied_snapshot = shot_detail_context_snapshot(
        prepared.project, prepared.episode, shot, policy=prepared.policy,
    )
    applied_context_hash = _snapshot_hash(applied_snapshot)
    metadata = dict(shot.metadata or {})
    provenance = dict(metadata.get("shot_detail_provenance") or {})
    provenance["applied_context_hash"] = applied_context_hash
    metadata["shot_detail_provenance"] = provenance
    shot.metadata = metadata
    shot.save(update_fields=("metadata", "updated_at"))
    input_settings = dict(draft.input_settings or {})
    input_settings["generation_context_hash"] = prepared.generation_context_hash
    input_settings["generation_context_snapshot"] = prepared.generation_context_snapshot
    input_settings["applied_context_hash"] = applied_context_hash
    draft.context_hash = applied_context_hash
    draft.context_snapshot = applied_snapshot
    draft.input_settings = input_settings
    draft.project = prepared.project
    draft.episode = prepared.episode
    draft.shot = shot
    draft.save(update_fields=(
        "context_hash", "context_snapshot", "input_settings", "updated_at",
    ))


def apply_shot_detail_draft(
    *, draft: ShotDetailDraft, user, language_mode: str, expected_revision: int,
    review_acknowledged: bool = False,
) -> tuple[ShotDetailDraft, Shot, bool]:
    identity = ShotDetailDraft.objects.only(
        "project_id", "episode_id", "shot_id",
    ).get(pk=draft.pk)
    with transaction.atomic():
        project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=identity.project_id,
        )
        episode = Episode.objects.select_for_update().get(
            pk=identity.episode_id, project=project,
        )
        shot = Shot.objects.select_for_update().select_related("scene").prefetch_related(
            "characters", "props",
        ).get(pk=identity.shot_id, episode=episode)
        locked = ShotDetailDraft.objects.select_for_update().select_related(
            "project", "episode", "shot",
        ).get(
            pk=draft.pk, project=project, episode=episode, shot=shot,
        )
        prepared = prepare_locked_shot_detail_application(
            draft=locked, project=project, episode=episode, shot=shot,
            user=user, language_mode=language_mode,
            expected_revision=expected_revision,
            review_acknowledged=review_acknowledged, allow_draft=False,
        )
        commit_prepared_shot_detail_application(prepared)
        finalize_prepared_shot_detail_application(prepared)
    return locked, shot, prepared.translated
