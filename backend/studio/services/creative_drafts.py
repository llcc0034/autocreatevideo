"""Review-gated local-Qwen structured storyboard drafts.

The local model is a proposal engine, never an author of record.  Its output is
validated as a small storyboard IR, persisted outside formal Episode/Shot rows,
and can enter production only after a current-context confirmation plus an
explicit apply request.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    Episode,
    EpisodeCreativeDraft,
    GenerationTask,
    PromptFieldTranslation,
    SeriesProject,
    Shot,
)
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    PINNED_MODEL_FILENAME,
    PINNED_MODEL_REPO_ID,
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    ensure_no_canonical_target_collisions,
    qwen_runtime_device_state,
    source_text_hash,
    translation_context_hash,
)
from studio.services.local_qwen_lock import (
    LocalQwenResourceBusy,
    local_qwen_runtime_lock,
)
try:
    from studio.services.deepseek_api import (
        DeepSeekAPIError,
        DeepSeekConfigurationError,
        get_deepseek_capability,
        run_structured_operation as run_deepseek_structured_operation,
    )
except ModuleNotFoundError as exc:
    if exc.name != "studio.services.deepseek_api":
        raise

    class DeepSeekConfigurationError(RuntimeError):
        code = "deepseek_provider_unavailable"

        def __init__(self, message: str = "DeepSeek API provider is not installed"):
            super().__init__(message)
            self.runtime_metadata = {"provider": "deepseek_api", "configured": False}

    class DeepSeekAPIError(DeepSeekConfigurationError):
        pass

    def get_deepseek_capability() -> dict:
        return {
            "id": "deepseek_api",
            "provider": "deepseek_api",
            "label": "DeepSeek API",
            "enabled": False,
            "configured": False,
            "ready": False,
            "error_code": "deepseek_provider_unavailable",
            "notice": "DeepSeek API 提供方尚未安装；本地 Qwen 不受影响。",
        }

    def run_deepseek_structured_operation(_request: dict) -> dict:
        raise DeepSeekConfigurationError()
from studio.services.direction_semantics import (
    direction_semantic_mismatches,
    direction_semantic_presence,
)


CREATIVE_SCHEMA_VERSION = 1
CREATIVE_OPERATION = "creative_storyboard"
LOCAL_QWEN_PROVIDER = "local_qwen"
DEEPSEEK_API_PROVIDER = "deepseek_api"
CREATIVE_PROVIDERS = {LOCAL_QWEN_PROVIDER, DEEPSEEK_API_PROVIDER}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ASCII_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_NEGATIVE_ZH_RE = re.compile(r"(?:不|没有|无|勿|禁止|未)")
_NEGATIVE_EN_RE = re.compile(
    r"\b(?:not|no|without|never|do not|does not|must not|unchanged|remain(?:s)? unchanged|keep)\b",
    re.I,
)
_SEQUENCE_RE = re.compile(r"(?:然后|接着|随后|继而|紧接着|同时|\bthen\b|\bnext\b|\bafterward\b)", re.I)
_SCRIPT_ACTION_VERB_RE = re.compile(
    r"(?:拔|出鞘|冲|扑|跃|跳|闪|避|转|翻|撩|荡|挡|架|格|"
    r"劈|斩|刺|挥|踢|踹|踏|撤|退|进|逼|包抄|绕|点|落|"
    r"抬|按|握|抓|放|抽|甩|抛|推|拉|走|跑|奔|停|蹲|起|倒|消失)"
)
_SCRIPT_ACTION_CLAUSE_RE = re.compile(r"[^\n。！？；;,，；!?]+")
_CAMERA_MOVEMENTS = {
    "push_in": (re.compile(r"(?:推近|推进|\bpush(?:es|ed|ing)? in\b|\bdolly in\b)", re.I)),
    "pull_back": (re.compile(r"(?:拉远|后移|\bpull(?:s|ed|ing)? back\b|\bdolly out\b)", re.I)),
    "pan": (re.compile(r"(?:横摇|摇摄|\bpan(?:s|ned|ning)?\b)", re.I)),
    "tilt": (re.compile(r"(?:俯仰摇|\btilt(?:s|ed|ing)?\b)", re.I)),
    "track": (re.compile(r"(?:跟拍|跟随|\btrack(?:s|ed|ing)?\b|\bfollow(?:s|ed|ing)?\b)", re.I)),
    "orbit": (re.compile(r"(?:环绕|\borbit(?:s|ed|ing)?\b)", re.I)),
    "crane": (re.compile(r"(?:升降|摇臂|\bcrane\b|\bboom\b)", re.I)),
    "handheld": (re.compile(r"(?:手持|\bhandheld\b)", re.I)),
}
_DIRECTION_CATEGORY_LABELS = {
    "subject_spatial_side": "角色方位",
    "body_side": "身体左右侧",
    "motion_direction": "运动方向",
    "spatial_side": "空间方位",
    "bilateral_sides": "双侧站位",
    "bilateral_motion": "双向运动",
}


class CreativeDraftError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "creative_runtime_failed",
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.runtime_metadata = dict(runtime_metadata or {})
        self.attempt_context_snapshot: dict[str, Any] | None = None
        self.attempt_context_hash = ""
        self.source_drift_during_attempt = False


class CreativeRuntimeUnavailable(CreativeDraftError):
    pass


class CreativeResourceBusy(CreativeDraftError):
    pass


class CreativeContextConflict(CreativeDraftError):
    pass


class CreativeRevisionConflict(CreativeDraftError):
    pass


class CreativeValidationError(CreativeDraftError):
    def __init__(
        self, message: str, *, errors: list[str] | None = None,
        code: str = "creative_validation_failed",
    ):
        super().__init__(message, code=code)
        self.errors = errors or [message]


def creative_required_output_token_budget(
    shot_count: int,
    dialogue_manifest: list[dict[str, Any]] | None = None,
) -> int:
    """Return the exact CPU output budget for shots plus authored dialogue."""
    count = max(1, min(4, int(shot_count)))
    dialogue_cost = sum(
        128 + max(
            0,
            len(str(item.get("text_zh") or ""))
            + len(str(item.get("source_delivery_zh") or ""))
            - 32,
        )
        for item in (dialogue_manifest or [])
    )
    return max(1_024, 768 + (384 * count)) + dialogue_cost


def required_dialogue_manifest(
    snapshot: dict[str, Any],
    *,
    include_audio: bool,
) -> list[dict[str, Any]]:
    """Extract the one immutable, source-ordered authored-dialogue contract."""
    if not include_audio:
        return []
    source = snapshot["episode"].get("script") or snapshot["project"].get("master_script") or ""
    if not source:
        return []
    manifest: list[dict[str, Any]] = []
    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        for character in snapshot["bible"]["characters"]:
            for name in (character.get("name"), character.get("stable_speaker_id")):
                if not name:
                    continue
                match = re.match(
                    rf"^\s*{re.escape(str(name))}\s*(?:[（(](?P<delivery>[^）)\n]*)[）)])?"
                    rf"\s*[：:]\s*(?P<text>.+?)\s*$",
                    line,
                )
                if not match:
                    continue
                item = {
                    "speaker_id": str(character["stable_speaker_id"]),
                    "text_zh": match.group("text"),
                    "source_order": len(manifest) + 1,
                    "source_delivery_zh": (match.group("delivery") or "").strip(),
                }
                # Every authored occurrence is significant, including repeated
                # text with the same or a different delivery annotation.
                manifest.append(item)
                break
            else:
                continue
            break
    return manifest


def creative_config() -> dict[str, Any]:
    configured = getattr(settings, "LOCAL_QWEN_CREATIVE", {}) or {}

    def resolve_path(raw) -> Path:
        value = Path(str(raw or "")).expanduser()
        return value if value.is_absolute() else (Path(settings.BASE_DIR) / value).resolve()

    return {
        "enabled": bool(configured.get("enabled", True)),
        "model_id": str(configured.get("model_id") or "Qwen/Qwen3-4B-GGUF"),
        "model_dir": resolve_path(configured.get("model_dir")),
        "runtime_script": resolve_path(configured.get("runtime_script")),
        "runtime_python": resolve_path(configured.get("runtime_python") or sys.executable),
        "device": str(configured.get("device") or "cpu").casefold(),
        "gpu_layers": int(configured.get("gpu_layers", -1)),
        "timeout_seconds": float(configured.get("timeout_seconds", 900)),
        "max_new_tokens": int(configured.get("max_new_tokens", 3072)),
        "context_size": int(configured.get("context_size", 8192)),
        "min_shots": int(configured.get("min_shots", 1)),
        "max_shots": int(configured.get("max_shots", 4)),
        "max_source_chars": int(configured.get("max_source_chars", 16000)),
    }


def _model_state(config: dict[str, Any]) -> dict[str, Any]:
    # Verify the creative path itself; never inherit a green translation
    # capability if an operator overrides the creative directory or model id.
    from studio.services.prompt_translation import _model_installation

    installation = _model_installation(config["model_dir"])
    model_matches = config["model_id"] == PINNED_MODEL_REPO_ID
    return {
        "downloaded": bool(installation.get("downloaded")),
        "verified": bool(model_matches and installation.get("verified")),
        "size": int(installation.get("size") or 0),
        "sha256": str(installation.get("sha256") or ""),
    }


def get_creative_capability() -> dict[str, Any]:
    config = creative_config()
    state = _model_state(config)
    runtime_state = qwen_runtime_device_state(config)
    configured = bool(
        config["enabled"] and config["runtime_script"].is_file()
        and config["runtime_python"].is_file() and state["verified"]
        and runtime_state["runtime_ready"]
    )
    local_capability = {
        "id": LOCAL_QWEN_PROVIDER,
        "label": "本地 Qwen",
        "creative_generation_supported": True,
        "configured": configured,
        "provider": "local_qwen",
        "model": config["model_id"],
        "model_downloaded": state["downloaded"],
        "model_verified": state["verified"],
        "device": config["device"],
        "gpu_layers": config["gpu_layers"],
        **runtime_state,
        "min_shots": config["min_shots"],
        "max_shots": config["max_shots"],
        "max_source_chars": config["max_source_chars"],
        "baseline_max_new_tokens": creative_required_output_token_budget(config["max_shots"]),
        "max_new_tokens": config["max_new_tokens"],
        "configured_max_new_tokens": config["max_new_tokens"],
        "context_size": config["context_size"],
        "runtime_timeout_seconds": config["timeout_seconds"],
        "process_timeout_seconds": config["timeout_seconds"] + 60,
        "loaded_on_demand": True,
        "unloaded_after_request": True,
        "review_required": True,
        "translation_review_reused": True,
        "notice": "本地 Qwen 仅生成隔离草稿；确认不会写正式分镜，只有显式应用才会写入。",
    }
    local_capability.update({
        "enabled": bool(config["enabled"]),
        "ready": configured,
        "transport": "local_process",
        "data_egress": False,
    })
    deepseek_capability = dict(get_deepseek_capability())
    deepseek_capability.update({
        "id": DEEPSEEK_API_PROVIDER,
        "provider": DEEPSEEK_API_PROVIDER,
        "label": "DeepSeek API",
        "creative_generation_supported": True,
        "min_shots": config["min_shots"],
        "max_shots": config["max_shots"],
        "max_source_chars": config["max_source_chars"],
        "review_required": True,
        "translation_review_reused": True,
        "ready": bool(
            deepseek_capability.get("enabled") and deepseek_capability.get("configured")
        ),
        "transport": "remote_api",
        "data_egress": True,
        "data_egress_notice": (
            "选择 DeepSeek API 会把本次项目设定、剧集、Bible、对白和视觉上下文发送到外部服务。"
        ),
    })
    default_provider = str(
        getattr(settings, "PROMPT_CREATIVE_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER)
        or LOCAL_QWEN_PROVIDER
    )
    if default_provider not in CREATIVE_PROVIDERS:
        default_provider = LOCAL_QWEN_PROVIDER
    return {
        **local_capability,
        "default_provider": default_provider,
        "providers": [local_capability, deepseek_capability],
    }


def normalise_creative_provider(value: str | None, *, default: str | None = None) -> str:
    provider = str(value or default or LOCAL_QWEN_PROVIDER).strip().casefold()
    if provider not in CREATIVE_PROVIDERS:
        raise CreativeValidationError(
            "不支持的模型通道",
            code="unsupported_model_provider",
        )
    return provider


def _run_provider_runtime(
    request: dict[str, Any], config: dict[str, Any], provider: str,
) -> dict[str, Any]:
    if provider == LOCAL_QWEN_PROVIDER:
        ensure_creative_resources_available()
        _require_runtime(config)
        return _run_runtime(request, config)
    try:
        return run_deepseek_structured_operation(request)
    except (DeepSeekConfigurationError, DeepSeekAPIError) as exc:
        raise CreativeDraftError(
            str(exc),
            code=str(getattr(exc, "code", "deepseek_api_failed")),
            runtime_metadata=dict(getattr(exc, "runtime_metadata", {}) or {}),
        ) from exc


def ensure_creative_resources_available() -> None:
    if GenerationTask.objects.filter(status__in=(
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )).exists():
        raise CreativeResourceBusy(
            "视频生成任务正在排队或执行；为避免本地 Qwen 与 ComfyUI 同时占用内存，请稍后再创作。"
        )


def creative_context_snapshot(project: SeriesProject, episode: Episode) -> dict[str, Any]:
    """Return the complete stable source-of-truth snapshot used for staleness."""
    if episode.project_id != project.id:
        raise CreativeValidationError("剧集不属于当前项目")
    style = project.style
    production_settings = project.production_settings or {}
    return {
        "schema_version": CREATIVE_SCHEMA_VERSION,
        "project": {
            "id": str(project.id),
            "title": project.title,
            "logline": project.logline,
            "master_script": project.master_script,
            "continuity_summary": project.continuity_summary,
            "language": project.language,
            "aspect_ratio": project.aspect_ratio,
            "default_shot_duration_ms": project.default_shot_duration_ms,
            "continuity_seed": project.continuity_seed,
            "color_script": production_settings.get("color_script", ""),
            "translation_glossary": production_settings.get("translation_glossary", {}),
            "default_h3_mode": production_settings.get("default_h3_mode", Shot.H3Mode.T2VA),
        },
        "style": ({
            "id": str(style.id), "name": style.name, "category": style.category,
            "description": style.description, "visual_prompt": style.visual_prompt,
            "negative_prompt": style.negative_prompt,
        } if style else None),
        "episode": {
            "id": str(episode.id), "number": episode.number, "title": episode.title,
            "synopsis": episode.synopsis, "script": episode.script,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings or {},
        },
        "bible": {
            "characters": [{
                "id": str(item.id), "stable_speaker_id": item.stable_speaker_id,
                "slug": item.slug, "name": item.name, "role": item.role,
                "description": item.description, "visual_identity": item.visual_identity,
                "age_appearance": item.age_appearance, "face_identity": item.face_identity,
                "wardrobe": item.wardrobe, "voice_profile": item.voice_profile,
                "personality": item.personality,
            } for item in project.characters.order_by("stable_speaker_id")],
            "scenes": [{
                "id": str(item.id), "slug": item.slug, "name": item.name,
                "description": item.description, "visual_identity": item.visual_identity,
                "location": item.location, "time_of_day": item.time_of_day,
                "lighting": item.lighting, "color_palette": item.color_palette,
                "ambient_sound": item.ambient_sound,
            } for item in project.scenes.order_by("slug")],
            "props": [{
                "id": str(item.id), "slug": item.slug, "name": item.name,
                "description": item.description, "visual_identity": item.visual_identity,
                "material": item.material, "distinctive_marks": item.distinctive_marks,
                "continuity_state": item.continuity_state,
            } for item in project.props.order_by("slug")],
        },
        "existing_shots": [{
            "id": str(item.id), "number": item.number, "title": item.title,
            "narrative": item.narrative, "camera": item.camera, "action": item.action,
            "dialogue": item.dialogue, "sound_effects": item.sound_effects,
            "music": item.music, "duration_ms": item.duration_ms,
            "media_type": item.media_type, "h3_mode": item.h3_mode,
            "include_audio": item.include_audio,
            "scene_id": str(item.scene_id) if item.scene_id else None,
            "character_ids": sorted(str(value.id) for value in item.characters.all()),
            "prop_ids": sorted(str(value.id) for value in item.props.all()),
            "reference_bindings": item.reference_bindings,
            "continuity_notes": item.continuity_notes,
        } for item in episode.shots.order_by("number").prefetch_related("characters", "props")],
    }


def creative_context_hash(project: SeriesProject, episode: Episode) -> str:
    canonical = json.dumps(
        creative_context_snapshot(project, episode), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _live_creative_context_hash(project_id, episode_id) -> str:
    live_project = SeriesProject.objects.select_related("style").get(pk=project_id)
    live_episode = Episode.objects.get(pk=episode_id, project=live_project)
    return creative_context_hash(live_project, live_episode)


def _runtime_source(snapshot: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """Build the authored runtime context and reject unsafe implicit truncation."""
    project = dict(snapshot["project"])
    payload = {
        "project": project,
        "style": snapshot["style"],
        "episode": snapshot["episode"],
        "bible": snapshot["bible"],
    }
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    warnings: list[str] = []
    # An episode's own script is the authoritative creative source.  Avoid
    # duplicating an entire series master script in the small local context;
    # the full master remains in context_snapshot/hash, so any edit still
    # invalidates this draft.  This is explicit source selection, not truncation.
    if (
        size > max_chars and snapshot["episode"].get("script")
        and project.get("master_script")
    ):
        project["master_script"] = ""
        warnings.append(
            "本集已有独立剧本；为满足本地上下文预算，本次创作未重复发送整剧 master_script，"
            "但整剧原文仍纳入 context hash。"
        )
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if size > max_chars:
        raise CreativeValidationError(
            f"创作上下文共 {size} 字符，超过本地模型单次 {max_chars} 字符上限；请缩短本集剧本或分段创作。"
        )
    payload["_input_warnings"] = warnings
    return payload


def _require_runtime(config: dict[str, Any]) -> None:
    if not config["enabled"]:
        raise CreativeRuntimeUnavailable("本地 Qwen 创作功能未启用")
    state = _model_state(config)
    runtime_state = qwen_runtime_device_state(config)
    configured = bool(
        config["runtime_script"].is_file()
        and config["runtime_python"].is_file()
        and state["verified"]
        and runtime_state["runtime_ready"]
    )
    if not configured:
        raise CreativeRuntimeUnavailable("本地 Qwen 创作运行时或模型尚未正确安装")


def _run_runtime(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    process_timeout = float(config["timeout_seconds"]) + 60
    diagnostic = {
        # Other local-Qwen proposal services deliberately reuse this exact
        # resource lock/process transport.  Preserve their operation name in
        # audit metadata instead of mislabelling every failure as a full
        # creative storyboard request.
        "operation": str(request.get("operation") or CREATIVE_OPERATION),
        "shot_count": int((request.get("options") or {}).get("shot_count") or 0),
        "max_tokens": int(request.get("max_new_tokens") or 0),
        "actual_output_token_budget": int(request.get("max_new_tokens") or 0),
        "required_dialogue_count": len(request.get("required_dialogue_manifest") or []),
        "context_size": int(request.get("context_size") or 0),
        "runtime_timeout_seconds": float(request.get("timeout_seconds") or 0),
        "process_timeout_seconds": process_timeout,
        "request_chars": len(json.dumps(request, ensure_ascii=False, separators=(",", ":"))),
        "loaded_on_demand": True,
        "unloaded_after_request": True,
    }
    child_environment = os.environ.copy()
    # Windows' inherited console encoding may be GBK even when the parent writes
    # UTF-8.  Make the stdin/stdout JSON contract deterministic for Chinese text.
    child_environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        with local_qwen_runtime_lock():
            completed = subprocess.run(
                [str(config["runtime_python"]), str(config["runtime_script"])],
                input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
                errors="strict", capture_output=True, timeout=process_timeout,
                cwd=str(settings.BASE_DIR), check=False,
                env=child_environment,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                ),
            )
    except LocalQwenResourceBusy as exc:
        raise CreativeResourceBusy(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        diagnostic.update({
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error_code": "creative_parent_timeout",
            "timeout_stage": "parent_process",
            "partial_stdout_present": bool(exc.stdout),
            "partial_stderr_present": bool(exc.stderr),
        })
        raise CreativeDraftError(
            "本地 Qwen 创作超过父进程清理时限；模型进程已终止，正式分镜未改变",
            code="creative_parent_timeout", runtime_metadata=diagnostic,
        ) from exc
    except OSError as exc:
        raise CreativeRuntimeUnavailable("无法启动本地 Qwen 创作进程") from exc
    diagnostic.update({
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "returncode": completed.returncode,
        "stderr_present": bool(completed.stderr.strip()),
        "stdout_chars": len(completed.stdout or ""),
    })
    try:
        payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        diagnostic["error_code"] = "creative_invalid_runtime_json"
        raise CreativeDraftError(
            "本地 Qwen 创作进程返回了无效 JSON；正式分镜未改变",
            code="creative_invalid_runtime_json", runtime_metadata=diagnostic,
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        runtime = _runtime_metadata(payload) if isinstance(payload, dict) else {}
        error_code = str(payload.get("error_code") or "creative_runtime_failed")[:80]
        raw_error = str(payload.get("error") or "").strip()
        safe_error = "".join(
            char for char in raw_error[:1000] if ord(char) >= 32 or char in "\n\r\t"
        ).strip()
        diagnostic.update(runtime)
        diagnostic["error_code"] = error_code
        raise CreativeDraftError(
            safe_error or "本地 Qwen 未生成有效创作草稿；正式分镜未改变",
            code=error_code, runtime_metadata=diagnostic,
        )
    if completed.returncode != 0:
        diagnostic["error_code"] = "creative_runtime_exit"
        raise CreativeDraftError(
            "本地 Qwen 创作进程异常退出；正式分镜未改变",
            code="creative_runtime_exit", runtime_metadata=diagnostic,
        )
    return payload


def _runtime_request(
    *, project: SeriesProject, episode: Episode, snapshot: dict[str, Any],
    context_hash: str, target_shot_count: int, creativity: str,
    variation_note: str, attempt: int,
    regeneration_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = creative_config()
    source = _runtime_source(snapshot, max_chars=config["max_source_chars"])
    input_warnings = source.pop("_input_warnings", [])
    default_mode = snapshot["project"].get("default_h3_mode")
    if default_mode not in set(Shot.H3Mode.values):
        default_mode = Shot.H3Mode.T2VA
    if len(str(snapshot["episode"].get("title") or "")) > 80:
        raise CreativeValidationError("剧集标题超过 80 字符；请先缩短标题再生成创作草稿。")
    include_audio = bool((episode.prompt_settings or {}).get("include_audio", True))
    recommended_durations = _recommended_shot_durations_ms(
        str(snapshot["episode"].get("script") or ""),
        target_shot_count,
        project.default_shot_duration_ms,
    )
    dialogue_manifest = required_dialogue_manifest(snapshot, include_audio=include_audio)
    if len(dialogue_manifest) > target_shot_count * 2:
        raise CreativeValidationError(
            f"本段剧本包含 {len(dialogue_manifest)} 句必须保留的原文对白，但 "
            f"{target_shot_count} 个镜头按每镜最多 2 句只能容纳 {target_shot_count * 2} 句；"
            "请增加镜头数或拆分本集片段后再生成。"
        )
    for item in dialogue_manifest:
        if len(item["text_zh"]) > 240 or len(item["source_delivery_zh"]) > 100:
            raise CreativeValidationError(
                f"第 {item['source_order']} 句原文对白或表演标注过长，无法放入 5–15 秒单镜；"
                "请先拆分剧本对白。"
            )
    output_token_budget = creative_required_output_token_budget(
        target_shot_count, dialogue_manifest,
    )
    if output_token_budget > config["max_new_tokens"]:
        raise CreativeValidationError(
            f"当前 {target_shot_count} 镜和 {len(dialogue_manifest)} 句原文对白需要约 "
            f"{output_token_budget} 个输出 token，超过本地 CPU 单次 {config['max_new_tokens']} 上限；"
            "请拆分本集片段后再生成。"
        )
    request = {
        "operation": CREATIVE_OPERATION,
        "request_id": context_hash[:24],
        "source_fingerprint": context_hash,
        "context_hash": context_hash,
        "project": source["project"],
        "style": source["style"],
        "glossary": source["project"].get("translation_glossary", {}),
        "episode": source["episode"],
        "bible": source["bible"],
        "attempt": max(1, int(attempt)),
        "feedback": variation_note,
        "options": {
            "shot_count": target_shot_count,
            "min_shots": config["min_shots"],
            "max_shots": config["max_shots"],
            "minimum_duration_ms": 5000,
            "maximum_duration_ms": 15000,
            "default_duration_ms": _action_density_duration_ms(
                str(snapshot["episode"].get("script") or ""),
                target_shot_count,
                project.default_shot_duration_ms,
            ),
            "recommended_shot_durations_ms": recommended_durations,
            "h3_mode": default_mode,
            "include_audio": include_audio,
            "creativity": creativity,
        },
        "seed": int(project.continuity_seed) + max(1, int(attempt)),
        "model_dir": str(config["model_dir"]),
        "model_id": config["model_id"],
        "device": config["device"],
        "gpu_layers": config["gpu_layers"],
        "max_new_tokens": output_token_budget,
        "context_size": config["context_size"],
        "timeout_seconds": config["timeout_seconds"],
        "input_warnings": input_warnings,
        "required_dialogue_manifest": dialogue_manifest,
    }
    if regeneration_context:
        combined_chars = len(json.dumps(
            {"source": source, "regeneration_context": regeneration_context},
            ensure_ascii=False, separators=(",", ":"),
        ))
        if combined_chars > config["max_source_chars"]:
            raise CreativeValidationError(
                f"逐镜重生成上下文共 {combined_chars} 字符，超过 {config['max_source_chars']} 字符上限；"
                "请缩短本集剧本/分镜描述，或改用整稿重新生成。"
            )
        request["regeneration_context"] = regeneration_context
    return request


def _normalise_runtime_result(payload: dict[str, Any], *, context_hash: str) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise CreativeValidationError("创作运行时缺少 result JSON 对象")
    echoed = str(result.get("source_fingerprint") or payload.get("source_fingerprint") or "")
    if echoed != context_hash:
        raise CreativeValidationError("创作运行时未原样回显 source_fingerprint")
    candidate = dict(result)
    candidate.pop("source_fingerprint", None)
    # Review metadata is runtime-owned, not editable storyboard data.  It is
    # sanitised and merged into server validation separately.
    candidate.pop("review_issues", None)
    candidate.pop("review_summary", None)
    # Episode title/synopsis are source-of-truth fields, not creative model
    # output. Drop both the current runtime's injected display copy and any
    # legacy flat fields before the service projects the saved Episode values.
    candidate.pop("episode", None)
    for field_name in (
        "episode_title_zh", "episode_title_en", "synopsis_zh", "synopsis_en",
    ):
        candidate.pop(field_name, None)
    return candidate


def _project_authoritative_episode_source(
    episode: Episode, candidate: dict[str, Any],
) -> dict[str, Any]:
    """Reattach immutable saved Chinese episode source to draft data.

    The local creative model never owns the formal episode title or synopsis.
    Human reviewers may optionally add English review text, but any attempted
    Chinese edit is deterministically replaced with the live Episode source
    before generation, patch, confirmation, or application validation.
    """
    projected = dict(candidate)
    supplied_episode = projected.get("episode")
    supplied_episode = supplied_episode if isinstance(supplied_episode, dict) else {}
    projected["episode"] = {
        "title_zh": episode.title,
        "title_en": supplied_episode.get("title_en", ""),
        "synopsis_zh": episode.synopsis,
        "synopsis_en": supplied_episode.get("synopsis_en", ""),
    }
    return projected


def _pair_checks(zh: str, en: str, *, label: str, glossary: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if _CJK_RE.search(en or ""):
        errors.append(f"{label} 英文字段包含中文")
    zh_numbers = _ASCII_NUMBER_RE.findall(zh or "")
    en_numbers = _ASCII_NUMBER_RE.findall(en or "")
    if sorted(zh_numbers) != sorted(en_numbers):
        errors.append(f"{label} 中英文数字不一致")
    for category, zh_codes, en_codes in direction_semantic_mismatches(zh or "", en or ""):
        errors.append(
            f"{label} {_DIRECTION_CATEGORY_LABELS[category]}语义不一致"
            f"（zh={','.join(zh_codes) or '-'}；en={','.join(en_codes) or '-'}）"
        )
    if _NEGATIVE_ZH_RE.search(zh or "") and not _NEGATIVE_EN_RE.search(en or ""):
        errors.append(f"{label} 未保留否定约束")
    for source, target in glossary.items():
        if source and source in (zh or "") and target and target not in (en or ""):
            errors.append(f"{label} 未使用术语表：{source} = {target}")
    return errors


_CREATIVE_REVIEW_SUGGESTIONS = {
    "english_contains_cjk": "英文栏仍含中文，请人工改写或确认保留。",
    "numeric_facts_mismatch": "请核对中英文中的数字、数量和顺序。",
    "direction_semantics_mismatch": "请核对左右、手脚、站位和运动方向。",
    "bilateral_scope": "双侧攻击可能出现在连续性、声音或其他重复字段中，请确认是否应删除或移回画面叙事。",
    "negation_mismatch": "中文中的否定或禁止含义可能未在英文中保留。",
    "canonical_glossary_missing": "请核对项目术语表中的 canonical English 名称。",
    "field_length_above_recommendation": "内容超过建议长度；可人工精简，也可知情保留。",
    "required_content_missing": "该字段内容为空，请补写、重新生成或知情保留。",
    "shot_count_mismatch": "实际镜头数与本次请求不同，请确认是否符合创作意图。",
    "shot_order_irregular": "镜头顺序或草稿标识不连续，建议人工整理。",
    "duplicate_binding": "同一素材绑定重复出现，建议删除重复项。",
    "unknown_scene_reference": "场景不属于当前项目；正式应用前需替换或删除。",
    "unknown_character_reference": "人物不属于当前项目；正式应用前需替换或删除。",
    "unknown_prop_reference": "道具不属于当前项目；正式应用前需替换或删除。",
    "unknown_speaker_id": "说话人不在当前 Bible 中，请核对或删除该对白。",
    "dialogue_binding_missing": "说话人尚未绑定到该镜头，请人工核对。",
    "dialogue_not_authored": "对白正文可能不是剧本原文，请人工核对。",
    "dialogue_delivery_differs_from_script": "对白语气与剧本中的表演标注不同，请在对应对白的中文语气字段中核对。",
    "audio_disabled_content_present": "镜头标记为静音，但仍包含对白、声景或配乐。",
    "dialogue_density_high": "短镜头中的对白较多，请确认节奏是否可执行。",
    "camera_movement_dense": "同一镜头可能包含多种运镜，请人工取舍。",
    "action_arc_dense": "动作节拍较多，请确认时长内是否可完成。",
    "action_duration_may_be_short": "当前时长可能难以完整展示这组连续动作，可在 5–15 秒内延长或人工加快动作节奏。",
    "script_action_coverage_low": (
        "剧本中似乎还有可见动作未在分镜画面叙事中体现；该结果仅为启发式提醒，"
        "请人工对照剧本补全或调整镜头时长。"
    ),
    "duration_outside_recommendation": "时长超出 MiniMax H3 常用的 5–15 秒范围。",
    "missing_scene_binding": "本镜尚未绑定场景 Bible。",
    "missing_character_binding": "本镜尚未绑定人物 Bible。",
    "canonical_target_collision": "多个 Bible 实体共用了同一英文 canonical 名称，请人工核对。",
}


def _creative_review_issue(
    code: str,
    path: str,
    *,
    severity: str = "risk",
) -> dict[str, str]:
    safe_code = re.sub(r"[^a-z0-9_.-]+", "_", str(code).lower()).strip("_")
    safe_path = re.sub(r"[^A-Za-z0-9_.\[\]-]+", "", str(path))[:180]
    safe_severity = severity if severity in {"warning", "risk", "requires_edit"} else "risk"
    issue = {
        "code": safe_code or "review_required",
        "severity": safe_severity,
        "path": safe_path,
    }
    suggestion = _CREATIVE_REVIEW_SUGGESTIONS.get(issue["code"])
    if suggestion:
        issue["suggestion_zh"] = suggestion
    return issue


def _normalise_runtime_review_issues(value: Any) -> list[dict[str, str]]:
    """Accept only finite, text-free issue metadata produced by our runtime."""
    if not isinstance(value, list):
        return []
    issues: list[dict[str, str]] = []
    for raw in value[:100]:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "")
        path = str(raw.get("path") or raw.get("field_path") or "")
        severity = str(raw.get("severity") or "risk").lower()
        if severity == "blocking":
            severity = "risk"
        if not code:
            continue
        issues.append(_creative_review_issue(code, path, severity=severity))
    return issues


def _dedupe_review_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.get("code", ""), issue.get("path", ""), issue.get("severity", ""))
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


_RUNTIME_FACT_RECHECK_CODES = frozenset({
    # The runtime can report these before its deterministic normalization has
    # finished.  The persisted candidate is the authoritative object the user
    # can actually edit, so only the Django validator may retain them after
    # that object is materialized.
    "dialogue_manifest_mismatch",
    "unknown_speaker_id",
    "dialogue_not_authored",
    "dialogue_binding_missing",
    "unknown_bible_reference",
    "source_binding_missing",
    "invented_speaker_id",
})


def _runtime_advisory_review_issues(value: Any) -> list[dict[str, str]]:
    """Keep runtime prose advisories, not stale pre-normalization fact errors."""
    return [
        item for item in _normalise_runtime_review_issues(value)
        if item.get("code") not in _RUNTIME_FACT_RECHECK_CODES
    ]


def _creative_review_summary(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": "needs_review" if issues else "ready",
        "issue_count": len(issues),
        "risk_count": sum(issue.get("severity") == "risk" for issue in issues),
        "requires_edit_count": sum(
            issue.get("severity") == "requires_edit" for issue in issues
        ),
    }


def _pair_review_issues(
    zh: str,
    en: str,
    *,
    path: str,
    glossary: dict[str, str],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if _CJK_RE.search(en or ""):
        issues.append(_creative_review_issue("english_contains_cjk", path))
    zh_numbers = _ASCII_NUMBER_RE.findall(zh or "")
    en_numbers = _ASCII_NUMBER_RE.findall(en or "")
    if sorted(zh_numbers) != sorted(en_numbers):
        issues.append(_creative_review_issue("numeric_facts_mismatch", path))
    if direction_semantic_mismatches(zh or "", en or ""):
        issues.append(_creative_review_issue("direction_semantics_mismatch", path))
    if _NEGATIVE_ZH_RE.search(zh or "") and not _NEGATIVE_EN_RE.search(en or ""):
        issues.append(_creative_review_issue("negation_mismatch", path))
    if any(
        source and source in (zh or "") and target and target not in (en or "")
        for source, target in glossary.items()
    ):
        issues.append(_creative_review_issue("canonical_glossary_missing", path))
    return issues


def _script_action_clauses(value: str) -> list[str]:
    """Return conservative, ordered action-like clauses for review heuristics.

    This is deliberately not an action parser.  It only lets us warn when a
    visibly action-heavy source appears substantially absent from the draft;
    it must never block, rewrite, or claim an exact coverage count.
    """

    clauses: list[str] = []
    # Strip obvious authored dialogue lines; speech verbs/content are not
    # visible fight beats for this duration heuristic.
    source = re.sub(r"(?m)^\s*[^\n：:]{1,24}[：:][^\n]*$", "", str(value or ""))
    for match in _SCRIPT_ACTION_CLAUSE_RE.finditer(source):
        clause = match.group(0).strip()
        if len(clause) >= 2 and _SCRIPT_ACTION_VERB_RE.search(clause):
            clauses.append(clause)
    return clauses


def _action_density_duration_ms(script: str, shot_count: int, default_ms: int) -> int:
    """Suggest one bounded per-shot duration from a conservative density cue."""

    count = len(_script_action_clauses(script))
    if count <= 0:
        return max(5_000, min(15_000, int(default_ms)))
    # Opening/end readability plus compact chained actions.  Round upward to
    # half seconds so the recommendation is stable and editor-friendly.
    per_shot = count / max(1, int(shot_count))
    estimate = 1_600 + (per_shot * 1_050)
    rounded = int(((estimate + 499) // 500) * 500)
    return max(5_000, min(15_000, rounded))


def _recommended_shot_durations_ms(
    script: str, shot_count: int, default_ms: int,
) -> list[int]:
    """Partition coarse source action weight into ordered 5-15s suggestions."""

    count = max(1, int(shot_count))
    clauses = _script_action_clauses(script)
    if not clauses:
        value = max(5_000, min(15_000, int(default_ms)))
        return [value] * count
    # Clause-level weights avoid treating a tightly phrased compound such as
    # "翻腕斤撩" as several full-duration events.  Long clauses receive only
    # modest extra room; this remains an editor suggestion, not a fact claim.
    weights = [1.25 if len(clause) >= 22 else (1.05 if len(clause) >= 12 else 0.85) for clause in clauses]
    groups: list[list[float]] = []
    cursor = 0
    for group_index in range(count):
        remaining_groups = count - group_index
        remaining_weights = weights[cursor:]
        if not remaining_weights:
            groups.append([])
            continue
        if remaining_groups == 1:
            groups.append(remaining_weights)
            cursor = len(weights)
            continue
        target = sum(remaining_weights) / remaining_groups
        group: list[float] = []
        while cursor < len(weights) - (remaining_groups - 1):
            next_weight = weights[cursor]
            if group and sum(group) + next_weight > target:
                break
            group.append(next_weight)
            cursor += 1
        if not group:
            group.append(weights[cursor])
            cursor += 1
        groups.append(group)

    values: list[int] = []
    for group in groups:
        if not group:
            estimate = max(5_000, min(15_000, int(default_ms)))
        else:
            # Fast readable micro-actions may share a second; the fixed 1.6s
            # allowance preserves an entry and exit state for the whole chain.
            estimate = 1_600 + (sum(group) * 1_000) + (max(0, len(group) - 1) * 150)
        values.append(max(5_000, min(15_000, int(((estimate + 499) // 500) * 500))))
    return values


def _script_action_coverage_issue(
    script: str, shots: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Return one non-blocking advisory when coarse lexical coverage is low."""

    clauses = _script_action_clauses(script)
    if len(clauses) < 3:
        return None
    storyboard = "\n".join(
        f"{item.get('narrative_zh', '')}\n{item.get('action_zh', '')}"
        for item in shots
    )
    represented = 0
    for clause in clauses:
        verbs = {match.group(0) for match in _SCRIPT_ACTION_VERB_RE.finditer(clause)}
        if verbs and any(verb in storyboard for verb in verbs):
            represented += 1
    if represented * 5 < len(clauses) * 4:
        return _creative_review_issue("script_action_coverage_low", "shots")
    return None


def validate_creative_draft_data(
    project: SeriesProject, episode: Episode, candidate: dict[str, Any],
    *,
    target_shot_count: int | None = None,
    runtime_review_issues: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an editable draft plus human-review issues.

    Only malformed transport/schema data is a failed draft.  Direction,
    translation, glossary, density and other creative judgements remain in the
    proposal and are surfaced as finite issue codes for a human decision.
    References that cannot be written through database relations are marked
    ``requires_edit``; they still do not discard the draft.
    """
    from studio.serializers import CreativeDraftDataSerializer

    candidate = _project_authoritative_episode_source(episode, candidate)
    serializer = CreativeDraftDataSerializer(data=candidate)
    if not serializer.is_valid():
        errors = [json.dumps(serializer.errors, ensure_ascii=False, default=str)]
        return candidate, {
            "status": "failed",
            "errors": errors,
            "warnings": [],
            "review_issues": [],
            "summary": _creative_review_summary([]),
        }
    data = json.loads(json.dumps(serializer.validated_data, ensure_ascii=False, default=str))
    issues = _runtime_advisory_review_issues(runtime_review_issues)
    warnings: list[str] = []
    if target_shot_count is not None and len(data["shots"]) != target_shot_count:
        issues.append(_creative_review_issue("shot_count_mismatch", "shots"))

    characters = {str(item.id): item for item in project.characters.all()}
    speaker_ids = {item.stable_speaker_id: str(item.id) for item in characters.values()}
    scenes = {str(item.id): item for item in project.scenes.all()}
    props = {str(item.id): item for item in project.props.all()}
    dialogue_source = "\n".join(filter(None, (episode.script, project.master_script)))
    authored_dialogue = required_dialogue_manifest({
        "episode": {"script": episode.script},
        "project": {"master_script": project.master_script},
        "bible": {"characters": [{
            "name": item.name,
            "stable_speaker_id": item.stable_speaker_id,
        } for item in characters.values()]},
    }, include_audio=any(bool(item.get("include_audio")) for item in data["shots"]))
    materialized_dialogue: list[tuple[str, dict[str, Any]]] = []
    glossary_raw = (project.production_settings or {}).get("translation_glossary", {})
    glossary = {
        str(source): str(target) for source, target in glossary_raw.items()
    } if isinstance(glossary_raw, dict) else {}

    if data["episode"]["title_en"].strip():
        issues.extend(_pair_review_issues(
            data["episode"]["title_zh"], data["episode"]["title_en"],
            path="episode.title_en", glossary=glossary,
        ))
    if data["episode"]["synopsis_en"].strip():
        issues.extend(_pair_review_issues(
            data["episode"]["synopsis_zh"], data["episode"]["synopsis_en"],
            path="episode.synopsis_en", glossary=glossary,
        ))

    shot_keys = [str(item.get("client_key") or "") for item in data["shots"]]
    shot_orders = [item.get("order") for item in data["shots"]]
    if len(shot_keys) != len(set(shot_keys)) or shot_orders != list(range(1, len(data["shots"]) + 1)):
        issues.append(_creative_review_issue(
            "shot_order_irregular", "shots", severity="requires_edit",
        ))

    recommended_limits = {
        "title_zh": 48, "title_en": 72,
        "narrative_zh": 180, "narrative_en": 1000,
        "camera_zh": 220, "camera_en": 320,
        "action_zh": 120, "action_en": 1000,
        "continuity_zh": 160, "continuity_en": 240,
        "soundscape_zh": 120, "soundscape_en": 180,
        "music_zh": 80, "music_en": 120,
    }
    required_fields = {
        "title_zh", "title_en", "narrative_zh", "narrative_en",
        "camera_zh", "camera_en", "action_zh", "action_en",
    }

    for shot_index, shot in enumerate(data["shots"]):
        base_path = f"shots[{shot_index}]"
        if shot.get("scene_id") and shot["scene_id"] not in scenes:
            issues.append(_creative_review_issue(
                "unknown_scene_reference", f"{base_path}.scene_id", severity="requires_edit",
            ))
        unknown_characters = set(shot.get("character_ids", [])) - set(characters)
        unknown_props = set(shot.get("prop_ids", [])) - set(props)
        if unknown_characters:
            issues.append(_creative_review_issue(
                "unknown_character_reference", f"{base_path}.character_ids",
                severity="requires_edit",
            ))
        if unknown_props:
            issues.append(_creative_review_issue(
                "unknown_prop_reference", f"{base_path}.prop_ids", severity="requires_edit",
            ))
        for field_name in ("character_ids", "prop_ids"):
            values = [str(value) for value in shot.get(field_name, [])]
            if len(values) != len(set(values)):
                issues.append(_creative_review_issue(
                    "duplicate_binding", f"{base_path}.{field_name}",
                ))
        bound_speakers = {
            item.stable_speaker_id for key, item in characters.items()
            if key in set(shot.get("character_ids", []))
        }
        for line_index, line in enumerate(shot.get("dialogue", [])):
            dialogue_path = f"{base_path}.dialogue[{line_index}]"
            materialized_dialogue.append((dialogue_path, line))
            if line["speaker_id"] not in speaker_ids:
                issues.append(_creative_review_issue(
                    "unknown_speaker_id", f"{dialogue_path}.speaker_id",
                    severity="requires_edit",
                ))
            if line["speaker_id"] not in bound_speakers:
                issues.append(_creative_review_issue(
                    "dialogue_binding_missing", f"{dialogue_path}.speaker_id",
                ))
            if line["text_zh"] not in dialogue_source:
                issues.append(_creative_review_issue(
                    "dialogue_not_authored", f"{dialogue_path}.text_zh",
                ))
            issues.extend(_pair_review_issues(
                line.get("delivery_zh", ""), line.get("delivery_en", ""),
                path=f"{dialogue_path}.delivery_en", glossary=glossary,
            ))
        pairs = ("title", "narrative", "camera", "action", "continuity", "soundscape", "music")
        for field_name in pairs:
            zh_key, en_key = f"{field_name}_zh", f"{field_name}_en"
            issues.extend(_pair_review_issues(
                shot.get(zh_key, ""), shot.get(en_key, ""),
                path=f"{base_path}.{en_key}", glossary=glossary,
            ))
            if field_name not in {"narrative", "action"}:
                zh_presence = direction_semantic_presence(
                    shot.get(zh_key, ""), english=False,
                )
                en_presence = direction_semantic_presence(
                    shot.get(en_key, ""), english=True,
                )
                if zh_presence.get("bilateral_sides") or en_presence.get("bilateral_sides"):
                    issues.append(_creative_review_issue(
                        "bilateral_scope", f"{base_path}.{field_name}",
                    ))
        for field_name, limit in recommended_limits.items():
            value = str(shot.get(field_name) or "")
            if field_name in required_fields and not value.strip():
                issues.append(_creative_review_issue(
                    "required_content_missing", f"{base_path}.{field_name}",
                ))
            if len(value) > limit:
                issues.append(_creative_review_issue(
                    "field_length_above_recommendation", f"{base_path}.{field_name}",
                ))
        if not 5_000 <= int(shot.get("duration_ms") or 0) <= 15_000:
            issues.append(_creative_review_issue(
                "duration_outside_recommendation", f"{base_path}.duration_ms",
            ))
        if not shot.get("include_audio") and any((
            shot.get("dialogue"), shot.get("soundscape_zh"), shot.get("soundscape_en"),
            shot.get("music_zh"), shot.get("music_en"),
        )):
            issues.append(_creative_review_issue(
                "audio_disabled_content_present", f"{base_path}.include_audio",
            ))
        if len(shot.get("dialogue", [])) > 2:
            issues.append(_creative_review_issue(
                "dialogue_density_high", f"{base_path}.dialogue",
            ))
        camera_text = f"{shot['camera_zh']}\n{shot['camera_en']}"
        active_movements = [
            key for key, pattern in _CAMERA_MOVEMENTS.items() if pattern.search(camera_text)
        ]
        if len(active_movements) > 1:
            issues.append(_creative_review_issue(
                "camera_movement_dense", f"{base_path}.camera_en",
            ))
        if len(_SEQUENCE_RE.findall(f"{shot['action_zh']} {shot['action_en']}")) > 4:
            issues.append(_creative_review_issue(
                "action_arc_dense", f"{base_path}.action_en",
            ))
        if not shot.get("scene_id"):
            issues.append(_creative_review_issue(
                "missing_scene_binding", f"{base_path}.scene_id", severity="warning",
            ))
        if not shot.get("character_ids"):
            issues.append(_creative_review_issue(
                "missing_character_binding", f"{base_path}.character_ids", severity="warning",
            ))

    coverage_issue = _script_action_coverage_issue(episode.script, data["shots"])
    if coverage_issue:
        issues.append(coverage_issue)

    # Runtime normalization can resolve dialogue placement and identity after
    # its first validation pass. Recheck the exact persisted rows here and
    # retain only an addressable advisory when the authored performance note
    # itself still differs. It is not an absent asset/speaker reference.
    for index, (dialogue_path, line) in enumerate(materialized_dialogue):
        if index >= len(authored_dialogue):
            break
        authored = authored_dialogue[index]
        if (
            line.get("speaker_id") == authored.get("speaker_id")
            and line.get("text_zh") == authored.get("text_zh")
            and authored.get("source_delivery_zh")
            and line.get("delivery_zh") != authored.get("source_delivery_zh")
        ):
            issues.append(_creative_review_issue(
                "dialogue_delivery_differs_from_script",
                f"{dialogue_path}.delivery_zh",
            ))

    try:
        ensure_no_canonical_target_collisions(project)
    except CanonicalTargetCollisionError:
        issues.append(_creative_review_issue(
            "canonical_target_collision", "project.translation_glossary",
        ))

    issues = _dedupe_review_issues(issues)
    summary = _creative_review_summary(issues)
    validation = {
        "status": summary["status"],
        "errors": [],
        "warnings": list(dict.fromkeys(warnings)),
        "review_issues": issues,
        "summary": summary,
    }
    return data, validation


def _runtime_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("runtime") or payload.get("metadata") or {}
    metadata = dict(raw) if isinstance(raw, dict) else {}
    metadata.update({
        "provider": str(payload.get("provider") or metadata.get("provider") or LOCAL_QWEN_PROVIDER),
        "model": str(payload.get("model") or metadata.get("model") or ""),
        "operation": CREATIVE_OPERATION,
    })
    is_local = metadata["provider"] == LOCAL_QWEN_PROVIDER
    metadata.setdefault("loaded_on_demand", is_local)
    metadata.setdefault("unloaded_after_request", is_local)
    metadata.setdefault("transport", "local_process" if is_local else "remote_api")
    return metadata


def _attach_attempt_audit(
    exc: CreativeDraftError,
    *, snapshot: dict[str, Any], context_hash: str,
    project: SeriesProject, episode: Episode,
) -> None:
    exc.attempt_context_snapshot = snapshot
    exc.attempt_context_hash = context_hash
    try:
        exc.source_drift_during_attempt = (
            _live_creative_context_hash(project.id, episode.id) != context_hash
        )
    except Exception:
        # Failure audit must remain writable even if a concurrent delete or
        # transient read error prevents a second live snapshot.
        exc.source_drift_during_attempt = True
    exc.runtime_metadata.update({
        "attempt_context_hash": context_hash,
        "source_drift_during_attempt": exc.source_drift_during_attempt,
    })


def generate_creative_candidate(
    *, project: SeriesProject, episode: Episode, target_shot_count: int,
    creativity: str, variation_note: str,
    attempt: int = 1,
    regeneration_context: dict[str, Any] | None = None,
    provider: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], str]:
    provider = normalise_creative_provider(
        provider,
        default=getattr(settings, "PROMPT_CREATIVE_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER),
    )
    config = creative_config()
    if not config["min_shots"] <= target_shot_count <= config["max_shots"]:
        raise CreativeValidationError(
            f"一次仅支持 {config['min_shots']}–{config['max_shots']} 个镜头；更多镜头请分段生成。"
        )
    snapshot = creative_context_snapshot(project, episode)
    context_hash = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    request = _runtime_request(
        project=project, episode=episode, snapshot=snapshot, context_hash=context_hash,
        target_shot_count=target_shot_count, creativity=creativity,
        variation_note=variation_note, attempt=attempt,
        regeneration_context=regeneration_context,
    )
    try:
        payload = _run_provider_runtime(request, config, provider)
    except CreativeDraftError as exc:
        _attach_attempt_audit(
            exc, snapshot=snapshot, context_hash=context_hash,
            project=project, episode=episode,
        )
        raise
    result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    runtime_review_issues = _normalise_runtime_review_issues(
        payload.get("review_issues", result_payload.get("review_issues")),
    )
    try:
        candidate = _normalise_runtime_result(payload, context_hash=context_hash)
        data, validation = validate_creative_draft_data(
            project, episode, candidate, target_shot_count=target_shot_count,
            runtime_review_issues=runtime_review_issues,
        )
    except CreativeDraftError as exc:
        _attach_attempt_audit(
            exc, snapshot=snapshot, context_hash=context_hash,
            project=project, episode=episode,
        )
        raise
    validation["warnings"] = list(dict.fromkeys([
        *request.get("input_warnings", []), *validation.get("warnings", []),
    ]))
    if _live_creative_context_hash(project.id, episode.id) != context_hash:
        raise CreativeContextConflict("创作期间项目或剧集上下文发生变化，请重新生成")
    return data, validation, _runtime_metadata(payload), validation["warnings"], context_hash


def next_generation_no(episode: Episode) -> int:
    return int(
        episode.creative_drafts.aggregate(value=Max("generation_no"))["value"] or 0
    ) + 1


def create_creative_draft(
    *, project: SeriesProject, episode: Episode, user, target_shot_count: int,
    creativity: str, variation_note: str, parent: EpisodeCreativeDraft | None = None,
    merge_from: EpisodeCreativeDraft | None = None, shot_keys: list[str] | None = None,
    expected_parent_revision: int | None = None,
    provider: str | None = None,
) -> EpisodeCreativeDraft:
    provider = normalise_creative_provider(
        provider,
        default=getattr(settings, "PROMPT_CREATIVE_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER),
    )
    attempt = max(next_generation_no(episode), (parent.generation_no + 1) if parent else 1)
    regeneration_context = None
    if merge_from and shot_keys:
        regeneration_context = {
            "selected_shot_keys": list(shot_keys),
            "previous_draft": merge_from.draft_data,
            "instruction": (
                "Regenerate only selected_shot_keys. Preserve all unselected shots and maintain "
                "their identities, locations, prop states, screen direction, and boundary continuity."
            ),
        }
    data, validation, runtime, warnings, context_hash = generate_creative_candidate(
        project=project, episode=episode, target_shot_count=target_shot_count,
        creativity=creativity, variation_note=variation_note, attempt=attempt,
        regeneration_context=regeneration_context,
        provider=provider,
    )
    if merge_from and shot_keys:
        previous_by_key = {
            item["client_key"]: item for item in (merge_from.draft_data or {}).get("shots", [])
        }
        generated_by_order = {item["order"]: item for item in data["shots"]}
        merged = []
        for old in (merge_from.draft_data or {}).get("shots", []):
            if old["client_key"] in set(shot_keys):
                replacement = dict(generated_by_order.get(old["order"], old))
                replacement["client_key"] = old["client_key"]
                replacement["order"] = old["order"]
                merged.append(replacement)
            else:
                merged.append(old)
        data = {**merge_from.draft_data, "episode": data["episode"], "shots": merged}
        data, validation = validate_creative_draft_data(
            project, episode, data, target_shot_count=len(merged),
            runtime_review_issues=validation.get("review_issues", []),
        )
        warnings = validation["warnings"]

    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=project.pk,
        )
        locked_episode = Episode.objects.select_for_update().get(pk=episode.pk)
        if creative_context_hash(locked_project, locked_episode) != context_hash:
            raise CreativeContextConflict("保存草稿前上下文发生变化，请重新生成")
        if parent:
            locked_parent = EpisodeCreativeDraft.objects.select_for_update().get(pk=parent.pk)
            if (
                expected_parent_revision is not None
                and locked_parent.revision != expected_parent_revision
            ):
                raise CreativeRevisionConflict("原草稿版本已变化，请刷新后再重新生成")
        else:
            locked_parent = None
        draft = EpisodeCreativeDraft.objects.create(
            project=locked_project, episode=locked_episode, created_by=user,
            parent_draft=parent, generation_no=next_generation_no(locked_episode),
            context_hash=context_hash,
            context_snapshot=creative_context_snapshot(locked_project, locked_episode),
            input_settings={
                "provider": provider,
                "target_shot_count": target_shot_count,
                "attempt": attempt,
                "actual_output_token_budget": runtime.get("actual_output_token_budget"),
                "required_dialogue_count": runtime.get("required_dialogue_count"),
            },
            creativity=creativity, variation_note=variation_note,
            draft_data=data, validation=validation, warnings=warnings,
            status=(EpisodeCreativeDraft.Status.DRAFT
                    if validation["status"] in {"passed", "ready", "needs_review"}
                    else EpisodeCreativeDraft.Status.FAILED),
            provider=provider,
            model=str(runtime.get("model") or (
                creative_config()["model_id"] if provider == LOCAL_QWEN_PROVIDER else ""
            )),
            runtime_metadata=runtime,
            error_message=("" if validation["status"] in {"passed", "ready", "needs_review"}
                           else "；".join(validation["errors"])[:10000]),
        )
        if (
            locked_parent
            and draft.status == EpisodeCreativeDraft.Status.DRAFT
            and locked_parent.status != EpisodeCreativeDraft.Status.APPLIED
        ):
            locked_parent.status = EpisodeCreativeDraft.Status.SUPERSEDED
            locked_parent.revision += 1
            locked_parent.save(update_fields=("status", "revision", "updated_at"))
    return draft


def create_failed_creative_draft(
    *, project: SeriesProject, episode: Episode, user, target_shot_count: int,
    creativity: str, variation_note: str, error: str,
    error_code: str = "creative_runtime_failed",
    runtime_metadata: dict[str, Any] | None = None,
    attempt_context_snapshot: dict[str, Any] | None = None,
    attempt_context_hash: str = "",
    source_drift_during_attempt: bool = False,
    parent: EpisodeCreativeDraft | None = None,
    expected_parent_revision: int | None = None,
    provider: str = LOCAL_QWEN_PROVIDER,
) -> EpisodeCreativeDraft:
    """Persist an auditable runtime failure without touching formal shots."""
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=project.pk,
        )
        locked_episode = Episode.objects.select_for_update().get(pk=episode.pk)
        locked_parent = None
        if parent:
            locked_parent = EpisodeCreativeDraft.objects.select_for_update().get(pk=parent.pk)
            if (
                expected_parent_revision is not None
                and locked_parent.revision != expected_parent_revision
            ):
                raise CreativeRevisionConflict("原草稿版本已变化，未记录过期失败尝试")
        snapshot = attempt_context_snapshot or creative_context_snapshot(
            locked_project, locked_episode,
        )
        context_hash = attempt_context_hash or hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        return EpisodeCreativeDraft.objects.create(
            project=locked_project, episode=locked_episode, created_by=user,
            parent_draft=locked_parent, generation_no=next_generation_no(locked_episode),
            context_hash=context_hash, context_snapshot=snapshot,
            input_settings={
                "provider": provider,
                "target_shot_count": target_shot_count,
                "runtime_error_code": error_code,
                "source_drift_during_attempt": bool(source_drift_during_attempt),
            },
            creativity=creativity, variation_note=variation_note,
            status=EpisodeCreativeDraft.Status.FAILED,
            validation={"status": "failed", "errors": [str(error)], "warnings": []},
            error_message=str(error)[:10000], provider=provider,
            model=str((runtime_metadata or {}).get("model") or "")[:200],
            runtime_metadata={
                "provider": provider,
                "transport": "local_process" if provider == LOCAL_QWEN_PROVIDER else "remote_api",
                "loaded_on_demand": provider == LOCAL_QWEN_PROVIDER,
                "unloaded_after_request": provider == LOCAL_QWEN_PROVIDER,
                "error_code": error_code,
                "attempt_context_hash": context_hash,
                "source_drift_during_attempt": bool(source_drift_during_attempt),
                **dict(runtime_metadata or {}),
            },
        )


def append_revision_history(draft: EpisodeCreativeDraft) -> None:
    draft.revision_history = [*(draft.revision_history or []), {
        "revision": draft.revision, "status": draft.status,
        "draft_data": draft.draft_data, "validation": draft.validation,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }]


def creative_review_issues(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (validation or {}).get("review_issues", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def creative_requires_edit_issues(
    validation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        item for item in creative_review_issues(validation)
        if item.get("severity") == "requires_edit"
    ]


def append_creative_review_acknowledgement(
    draft: EpisodeCreativeDraft,
    *,
    user,
    action: str,
) -> None:
    """Audit an informed human override without storing candidate prose."""
    issues = creative_review_issues(draft.validation)
    if not issues:
        return
    metadata = dict(draft.runtime_metadata or {})
    existing = metadata.get("review_acknowledgements", [])
    acknowledgements = list(existing) if isinstance(existing, list) else []
    acknowledgements.append({
        "action": action if action in {"confirm", "apply"} else "review",
        "acknowledged_by": str(user.pk),
        "acknowledged_at": timezone.now().isoformat(),
        "draft_revision": int(draft.revision),
        "issue_codes": sorted({
            str(item.get("code") or "review_required")[:80] for item in issues
        }),
    })
    metadata["review_acknowledgements"] = acknowledgements[-20:]
    draft.runtime_metadata = metadata


def _write_confirmed_translation(
    *, project: SeriesProject, shot: Shot, field_key: str, source: str,
    translated: str, user, draft: EpisodeCreativeDraft,
    dialogue_identity: dict | None = None,
) -> bool:
    if not source or not translated:
        return False
    context = build_translation_context(
        project, scope_type=PromptFieldTranslation.ScopeType.SHOT,
        scope_id=shot.id, field_key=field_key, source_text=translated,
    )
    entry, created = PromptFieldTranslation.objects.select_for_update().get_or_create(
        project=project, scope_type=PromptFieldTranslation.ScopeType.SHOT,
        scope_id=shot.id, field_key=field_key,
        defaults={
            "source_text": translated,
            "source_hash": source_text_hash(translated),
        },
    )
    if created:
        entry.revision_history = [{
            "revision": 0,
            "source_text": source,
            "translated_text": translated,
            "status": "creative_bilingual_confirmed_before_promotion",
            "event": "creative_translation_promoted",
            "confirmed_by": str(user.pk),
            "updated_at": timezone.now().isoformat(),
        }]
    else:
        entry.revision_history = [*(entry.revision_history or []), {
            "revision": entry.revision, "source_text": entry.source_text,
            "translated_text": entry.translated_text, "status": entry.status,
            "event": "creative_translation_promoted",
            "confirmed_by": str(user.pk),
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }]
        entry.revision += 1
    entry.source_text = translated
    entry.source_hash = source_text_hash(translated)
    entry.translated_text = translated
    entry.status = PromptFieldTranslation.Status.CONFIRMED
    entry.provider = f"{draft.provider}_creative"
    entry.model = draft.model
    entry.error_message = ""
    entry.runtime_metadata = {
        "translation_context_hash": translation_context_hash(context),
        "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        "creative_draft_id": str(draft.id),
        "source_provider": draft.provider,
        "transport": (draft.runtime_metadata or {}).get("transport", ""),
        "loaded_on_demand": bool((draft.runtime_metadata or {}).get("loaded_on_demand")),
        "unloaded_after_request": bool((draft.runtime_metadata or {}).get("unloaded_after_request")),
        "human_review_acknowledged": True,
        **({"dialogue_identity": dialogue_identity} if dialogue_identity else {}),
        "promotion": {
            "promoted": True,
            "event": "creative_translation_promoted",
            "promoted_by": str(user.pk),
            "shot_id": str(shot.id),
            "field_key": field_key,
            "dialogue_index": (
                dialogue_identity.get("source_order") if dialogue_identity else None
            ),
            "original_source_hash": source_text_hash(source),
            "original_source_text_sha256": source_text_hash(source),
            "promoted_value_sha256": source_text_hash(translated),
            "applied_from_revision": entry.revision,
            "first_promoted_at": timezone.now().isoformat(),
            "last_confirmed_at": timezone.now().isoformat(),
            "shot_updated_at": shot.updated_at.isoformat(),
        },
    }
    entry.confirmed_by = user
    entry.confirmed_at = timezone.now()
    entry.save()
    return True


def _canonical_storyboard_narrative(
    item: dict, language: str, *, source_script: str = "",
) -> str:
    """Choose/merge the richest canonical action chain without reordering it.

    ``narrative_*`` is the full visible chain and ``action_*`` is its summary
    in the current contract.  Legacy drafts sometimes inverted those roles.
    We only concatenate two independently located, non-overlapping source
    fragments when their order is provable from the authoritative script;
    otherwise the more complete field wins rather than risking a false order.
    """

    narrative = str(item.get(f"narrative_{language}") or "").strip()
    action = str(item.get(f"action_{language}") or "").strip()
    if not narrative:
        return action
    if not action:
        return narrative

    def compact(value: str) -> str:
        return re.sub(r"[\s。！？；,，；!?]+", "", value)

    narrative_key = compact(narrative)
    action_key = compact(action)
    if action_key in narrative_key:
        return narrative
    if narrative_key in action_key:
        return action

    if language == "zh" and len(narrative) <= 30 and len(action) > len(narrative):
        narrative_semantics = direction_semantic_presence(narrative, english=False)
        action_semantics = direction_semantic_presence(action, english=False)
        semantic_codes = (
            "subject_spatial_side", "body_side", "motion_direction", "spatial_side",
            "bilateral_sides", "bilateral_motion",
        )
        if all(
            not narrative_semantics.get(code) or action_semantics.get(code)
            for code in semantic_codes
        ):
            return action

    # Source-order proof is available only for Chinese authored prose.  English
    # has no authoritative script alignment, so it safely selects completeness.
    if language == "zh" and source_script:
        def source_position(value: str) -> int:
            exact = source_script.find(value)
            if exact >= 0:
                return exact
            positions = [
                source_script.find(fragment.strip())
                for fragment in re.split(r"[。！？；,，；!?]+", value)
                if len(fragment.strip()) >= 4 and source_script.find(fragment.strip()) >= 0
            ]
            if positions:
                return min(positions)
            # Legacy summaries may paraphrase the source but retain one clear
            # action phrase. Locate the longest source-contained compact span
            # rather than guessing field order.
            compact_value = compact(value)
            compact_source = compact(source_script)
            best = ""
            for start in range(len(compact_value)):
                for end in range(len(compact_value), start + 3, -1):
                    fragment = compact_value[start:end]
                    if fragment in compact_source:
                        if len(fragment) > len(best):
                            best = fragment
                        break
            if len(best) >= max(4, int(len(compact_value) * 0.4)):
                return compact_source.find(best)
            return -1

        narrative_pos = source_position(narrative)
        action_pos = source_position(action)
        if narrative_pos >= 0 and action_pos >= 0:
            ordered = (
                (narrative, action) if narrative_pos < action_pos else (action, narrative)
            )
            separator = "" if ordered[0].endswith(("。", "！", "？", "；")) else "。"
            return f"{ordered[0]}{separator}{ordered[1]}"

        narrative_semantics = direction_semantic_presence(narrative, english=False)
        action_semantics = direction_semantic_presence(action, english=False)
        narrative_has_unique_bilateral = bool(
            narrative_semantics.get("bilateral_sides")
            and not action_semantics.get("bilateral_sides")
        )
        action_has_unique_bilateral = bool(
            action_semantics.get("bilateral_sides")
            and not narrative_semantics.get("bilateral_sides")
        )
        # Never silently lose an authored left/right encirclement.  If only
        # one field can be located in source, use that position as a boundary:
        # legacy narrative was the event/context slot and action the detailed
        # chain.  This preserves both without claiming an NLP-perfect order.
        if narrative_has_unique_bilateral or action_has_unique_bilateral:
            if narrative_pos >= 0 and action_pos < 0:
                ordered = (action, narrative)
            elif action_pos >= 0 and narrative_pos < 0:
                # An action chain anchored at the source beginning precedes an
                # unlocated bilateral summary; do not put that summary first.
                ordered = (action, narrative)
            else:
                ordered = (action, narrative) if narrative_has_unique_bilateral else (narrative, action)
            separator = "" if ordered[0].endswith(("。", "！", "？", "；")) else "。"
            return f"{ordered[0]}{separator}{ordered[1]}"

    narrative_actions = len(_SCRIPT_ACTION_VERB_RE.findall(narrative))
    action_actions = len(_SCRIPT_ACTION_VERB_RE.findall(action))
    if action_actions != narrative_actions:
        return action if action_actions > narrative_actions else narrative
    return action if len(action) > len(narrative) else narrative


def _resolve_draft_h3_mode(project: SeriesProject, item: dict[str, Any]):
    """Return a runnable mode plus classified references for an applied shot.

    Creative IR never invents frame/reference bindings.  Ref2VA may reuse
    explicit Bible anchors, while first/last-frame modes safely fall back to
    T2VA until a user binds the required frame in the regular shot editor.
    """
    requested = item.get("h3_mode") or Shot.H3Mode.T2VA
    if requested != Shot.H3Mode.REF2VA:
        if requested in {Shot.H3Mode.I2VA, Shot.H3Mode.FL2VA, Shot.H3Mode.L2VA}:
            return Shot.H3Mode.T2VA, {}, [], requested
        return requested, {}, [], ""

    characters = {
        str(value.id): value for value in project.characters.filter(
            id__in=item.get("character_ids", []),
        ).prefetch_related("reference_assets")
    }
    props = {
        str(value.id): value for value in project.props.filter(
            id__in=item.get("prop_ids", []),
        ).prefetch_related("reference_assets")
    }
    scene = project.scenes.filter(pk=item.get("scene_id")).prefetch_related(
        "reference_assets",
    ).first() if item.get("scene_id") else None
    bindings: dict[str, Any] = {"characters": [], "props": []}
    assets = []

    def pick(entity):
        asset = entity.anchor_asset or entity.reference_assets.order_by("kind", "name", "id").first()
        if asset:
            assets.append(asset)
        return asset

    for entity_id in item.get("character_ids", []):
        entity = characters.get(str(entity_id))
        asset = pick(entity) if entity else None
        if asset:
            bindings["characters"].append({"entity_id": str(entity.id), "asset_id": str(asset.id)})
    if scene:
        asset = pick(scene)
        if asset:
            bindings["scene"] = {"entity_id": str(scene.id), "asset_id": str(asset.id)}
    for entity_id in item.get("prop_ids", []):
        entity = props.get(str(entity_id))
        asset = pick(entity) if entity else None
        if asset:
            bindings["props"].append({"entity_id": str(entity.id), "asset_id": str(asset.id)})
    bindings = {key: value for key, value in bindings.items() if value}
    asset_ids = [str(asset.id) for asset in assets]
    if not bindings or len(asset_ids) != len(set(asset_ids)) or len(assets) > 9:
        return Shot.H3Mode.T2VA, {}, [], requested
    return Shot.H3Mode.REF2VA, bindings, assets, ""


def apply_creative_draft(
    *, draft: EpisodeCreativeDraft, user, language_mode: str, placement: str,
    shot_keys: list[str] | None, expected_revision: int,
    review_acknowledged: bool = False,
) -> tuple[Episode, list[Shot], int]:
    """Apply a confirmed draft atomically; this is the sole formal write path."""
    with transaction.atomic():
        locked = EpisodeCreativeDraft.objects.select_for_update().select_related(
            "project", "episode",
        ).get(pk=draft.pk)
        project = SeriesProject.objects.select_for_update().select_related("style").get(
            pk=locked.project_id,
        )
        episode = Episode.objects.select_for_update().get(pk=locked.episode_id)
        if locked.revision != expected_revision:
            raise CreativeRevisionConflict("草稿版本已变化，请刷新后再应用")
        if locked.status != EpisodeCreativeDraft.Status.CONFIRMED:
            raise CreativeValidationError("只有已确认草稿可以应用")
        if (locked.validation or {}).get("status") not in {
            "passed", "ready", "needs_review",
        }:
            raise CreativeValidationError("草稿结构不完整，无法应用；请返回草稿修复")
        if creative_context_hash(project, episode) != locked.context_hash:
            raise CreativeContextConflict("项目、Bible、剧集或正式镜头已变化，请重新生成并确认")
        checked_data, checked_validation = validate_creative_draft_data(
            project, episode, locked.draft_data,
            target_shot_count=len((locked.draft_data or {}).get("shots", [])),
        )
        if checked_validation["status"] not in {"passed", "ready", "needs_review"}:
            raise CreativeValidationError(
                "持久化草稿结构复核失败，无法应用", errors=checked_validation["errors"],
            )
        locked.draft_data = checked_data
        locked.validation = checked_validation
        locked.warnings = checked_validation.get("warnings", [])
        current_issues = creative_review_issues(checked_validation)
        if current_issues and not review_acknowledged:
            raise CreativeValidationError(
                "请先阅读草稿问题清单并明确确认风险",
                errors=[str(item.get("code") or "review_required") for item in current_issues],
                code="creative_review_acknowledgement_required",
            )
        requires_edit = creative_requires_edit_issues(checked_validation)
        if requires_edit:
            raise CreativeValidationError(
                "草稿仍包含无法写入的素材或说话人引用，请先替换或删除",
                errors=[str(item.get("code") or "requires_edit") for item in requires_edit],
                code="creative_review_requires_edit",
            )
        if current_issues:
            append_creative_review_acknowledgement(
                locked, user=user, action="apply",
            )
        shots_data = list((locked.draft_data or {}).get("shots", []))
        if shot_keys:
            requested = set(shot_keys)
            available = {item["client_key"] for item in shots_data}
            if not requested <= available:
                raise CreativeValidationError("shot_keys 包含不存在的草稿镜头")
            if placement == "replace":
                raise CreativeValidationError("replace 只能应用完整草稿；部分镜头只能 append")
            shots_data = [item for item in shots_data if item["client_key"] in requested]
        if not shots_data:
            raise CreativeValidationError("没有可应用的草稿镜头")

        if placement == "replace":
            if (
                episode.prompt_artifacts.exists()
                or project.generation_tasks.filter(episode=episode).exists()
                or episode.assembly_jobs.exists()
            ):
                raise CreativeValidationError(
                    "该集已有提示词、生成任务或组装审计记录，不能替换正式镜头；请使用追加。"
                )
            old_ids = list(episode.shots.values_list("id", flat=True))
            PromptFieldTranslation.objects.filter(
                project=project,
                scope_type=PromptFieldTranslation.ScopeType.SHOT,
                scope_id__in=old_ids,
            ).delete()
            episode.shots.all().delete()
            next_number = 1
        else:
            next_number = int(
                episode.shots.aggregate(value=Max("number"))["value"] or 0
            ) + 1

        # Episode title and synopsis are authored source records. Applying a
        # storyboard can only create/replace its formal shots; it never writes
        # the surrounding episode source from an isolated creative draft.
        created: list[Shot] = []
        translation_count = 0
        for offset, item in enumerate(shots_data):
            effective_mode, reference_bindings, reference_assets, downgraded_from = (
                _resolve_draft_h3_mode(project, item)
            )
            promote_english = language_mode == "bilingual_confirmed"
            canonical_narrative_zh = _canonical_storyboard_narrative(
                item, "zh", source_script=episode.script,
            )
            canonical_narrative_en = _canonical_storyboard_narrative(item, "en")
            dialogue = [{
                "speaker_id": line["speaker_id"], "text": line["text_zh"],
                **({
                    "delivery": (
                        line.get("delivery_en", "") if promote_english
                        else line.get("delivery_zh", "")
                    ),
                } if line.get("delivery_en" if promote_english else "delivery_zh") else {}),
                **({"voiceover": True} if line.get("voiceover") else {}),
            } for line in item.get("dialogue", [])]
            shot = Shot.objects.create(
                episode=episode, number=next_number + offset,
                title=item["title_zh"],
                narrative=canonical_narrative_en if promote_english else canonical_narrative_zh,
                camera=item["camera_en"] if promote_english else item["camera_zh"],
                # Storyboard v2 has one canonical action/visual field:
                # narrative. Keep the legacy action column empty.
                action="",
                dialogue=dialogue, sound_effects=(
                    item.get("soundscape_en", "") if promote_english
                    else item.get("soundscape_zh", "")
                ),
                music=(
                    item.get("music_en", "") if promote_english
                    else item.get("music_zh", "")
                ), duration_ms=item["duration_ms"],
                media_type=Shot.MediaType.VIDEO, h3_mode=effective_mode,
                include_audio=item["include_audio"], scene_id=item.get("scene_id"),
                continuity_notes=(
                    item.get("continuity_en", "") if promote_english
                    else item.get("continuity_zh", "")
                ),
                reference_bindings=reference_bindings,
                metadata={
                    "creative_draft_id": str(locked.id), "client_key": item["client_key"],
                    # Split positive continuity from true negative constraints
                    # on first persistence.  Older clients treated continuity
                    # as a fallback negative prompt when this marker was
                    # absent, then wrote the positive story/dialogue back into
                    # metadata.negative_prompt on their next sync.
                    "prompt_field_version": 2,
                    "negative_prompt": "",
                    **({
                        "requested_h3_mode": downgraded_from,
                        "h3_mode_adjustment": "downgraded_to_T2VA_missing_classified_references",
                    } if downgraded_from else {}),
                },
            )
            shot.characters.set(item.get("character_ids", []))
            shot.props.set(item.get("prop_ids", []))
            if reference_assets:
                shot.reference_assets.set(reference_assets)
            created.append(shot)
            if language_mode == "bilingual_confirmed":
                fields = (
                    ("shot.narrative", canonical_narrative_zh, canonical_narrative_en),
                    ("shot.camera", item["camera_zh"], item["camera_en"]),
                    ("shot.continuity_notes", item.get("continuity_zh", ""), item.get("continuity_en", "")),
                    ("shot.sound_effects", item.get("soundscape_zh", ""), item.get("soundscape_en", "")),
                    ("shot.music", item.get("music_zh", ""), item.get("music_en", "")),
                )
                for field_key, source, translated in fields:
                    translation_count += int(_write_confirmed_translation(
                        project=project, shot=shot, field_key=field_key,
                        source=source, translated=translated, user=user, draft=locked,
                    ))
                for index, line in enumerate(item.get("dialogue", [])):
                    translation_count += int(_write_confirmed_translation(
                        project=project, shot=shot,
                        field_key=f"shot.dialogue.{index}.delivery",
                        source=line.get("delivery_zh", ""),
                        translated=line.get("delivery_en", ""), user=user, draft=locked,
                        dialogue_identity={
                            "source_order": index,
                            "speaker_id": line.get("speaker_id", ""),
                            "text_sha256": source_text_hash(line.get("text_zh", "")),
                            "voiceover": bool(line.get("voiceover")),
                        },
                    ))

        append_revision_history(locked)
        locked.revision += 1
        locked.status = EpisodeCreativeDraft.Status.APPLIED
        locked.applied_by = user
        locked.applied_at = timezone.now()
        locked.applied_shot_ids = [str(item.id) for item in created]
        locked.save()
    return episode, created, translation_count
