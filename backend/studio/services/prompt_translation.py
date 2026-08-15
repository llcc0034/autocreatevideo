"""On-demand, review-gated translation for prompt-impacting fields.

The official Qwen-MT product is currently exposed as an Alibaba Cloud API; no
official downloadable Qwen-MT checkpoint is advertised.  This local runtime
therefore uses a separately configured open-weight Qwen model downloaded from
ModelScope.  It is deliberately executed as a one-request subprocess so model
memory is returned to the OS before ComfyUI generation starts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable
import unicodedata

from django.conf import settings

from studio.models import (
    CharacterBible,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
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
        run_translation as run_deepseek_translation,
    )
except ModuleNotFoundError as exc:
    # Keep the local-Qwen application available while the optional remote
    # provider is not installed.  This is deliberately fail-closed: the
    # capability is advertised as unavailable and selecting it raises instead
    # of silently falling back or pretending that a translation succeeded.
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

    def run_deepseek_translation(_request: dict) -> dict:
        raise DeepSeekConfigurationError()


_PROTECTED_BLOCK_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PROTECTED_TOKEN_RE = re.compile(r"__AUTOPROMPT_LITERAL_[0-9A-F]{12}_[0-9]{4}__")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
PINNED_MODEL_REPO_ID = "Qwen/Qwen3-4B-GGUF"
PINNED_MODEL_FILENAME = "Qwen3-4B-Q4_K_M.gguf"
PINNED_MODEL_SIZE = 2_497_280_256
PINNED_MODEL_SHA256 = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
TRANSLATION_CONTEXT_VERSION = 2
LOCAL_QWEN_PROVIDER = "local_qwen"
DEEPSEEK_API_PROVIDER = "deepseek_api"
SUPPORTED_MODEL_PROVIDERS = {LOCAL_QWEN_PROVIDER, DEEPSEEK_API_PROVIDER}


class CanonicalTargetCollisionError(ValueError):
    """Two authoritative Bible entities resolve to one canonical identity."""

    code = "canonical_target_collision"

    def __init__(self, collisions: list[dict]):
        super().__init__("canonical English targets must be unique across Bible entities")
        self.collisions = collisions

ALLOWED_FIELD_KEYS = {
    PromptFieldTranslation.ScopeType.PROJECT: {
        "style.visual_prompt",
        "project.production_settings.color_script",
        "project.continuity_summary",
    },
    PromptFieldTranslation.ScopeType.CHARACTER: {
        "character.name",
        "character.age_appearance",
        "character.visual_identity",
        "character.face_identity",
        "character.wardrobe",
        "character.voice_profile",
        "character.personality",
    },
    PromptFieldTranslation.ScopeType.SCENE: {
        "scene.name",
        "scene.visual_identity",
        "scene.location",
        "scene.time_of_day",
        "scene.lighting",
        "scene.color_palette",
        "scene.ambient_sound",
    },
    PromptFieldTranslation.ScopeType.PROP: {
        "prop.name",
        "prop.visual_identity",
        "prop.material",
        "prop.distinctive_marks",
        "prop.continuity_state",
    },
    PromptFieldTranslation.ScopeType.SHOT: {
        "shot.narrative",
        "shot.detailed_description",
        "shot.action",
        "shot.camera",
        "shot.continuity_notes",
        "shot.sound_effects",
        "shot.music",
    },
}
_DIALOGUE_DELIVERY_FIELD_RE = re.compile(r"^shot\.dialogue\.([0-9]+)\.delivery$")


def field_key_allowed(scope_type: str, field_key: str) -> bool:
    if field_key in ALLOWED_FIELD_KEYS.get(scope_type, set()):
        return True
    return bool(
        scope_type == PromptFieldTranslation.ScopeType.SHOT
        and _DIALOGUE_DELIVERY_FIELD_RE.fullmatch(field_key)
    )


class PromptTranslationError(RuntimeError):
    """Safe, persisted description of one local-translation failure."""

    def __init__(
        self, message: str, *, code: str = "translation_failed",
        metadata: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "translation_failed")
        self.metadata = dict(metadata or {})


class PromptTranslationResourceBusy(PromptTranslationError):
    pass


class TranslationRuntimeUnavailable(PromptTranslationError):
    pass


class TranslationValidationError(PromptTranslationError):
    pass


_SAFE_RUNTIME_ERROR_MESSAGES = {
    "visual_rules_validation_failed": "本地模型候选经一次确定性修复后仍未通过全剧视觉规则校验，已拒绝采用。",
    "visual_rules_runtime_failed": "本地模型未能生成可用的全剧视觉规则候选，请稍后重试。",
    "asset_draft_validation_failed": "本地模型候选经一次确定性修复后仍未通过素材设定校验，已拒绝采用。",
    "asset_draft_runtime_failed": "本地模型未能生成可用的素材设定草稿，请稍后重试。",
    "residual_cjk_after_repair": "英文候选经一次确定性修复后仍含中文字符，已拒绝采用；请重试或人工填写英文译文。",
    "semantic_guard_failed": "英文候选未通过专名、性别或导演术语校验，已拒绝采用。",
    "protected_token_mismatch": "英文候选改变了受保护的对白、歌词或屏显占位符，已拒绝采用。",
    "invalid_model_output": "本地模型未返回文本译文。",
    "empty_translation": "本地模型返回了空译文。",
    "invalid_repair_output": "本地模型的修复结果不是有效文本。",
    "empty_repair_output": "本地模型的修复结果为空。",
    "translation_failed": "该字段翻译失败；中文原文未被覆盖。",
}


def _safe_runtime_error(row: dict) -> PromptTranslationError:
    code = str(row.get("error_code") or "translation_failed")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        code = "translation_failed"
    raw_details = row.get("error_details")
    details = raw_details if isinstance(raw_details, dict) else {}
    residual = details.get("residual_cjk")
    safe_residual = [
        str(value)[:32] for value in residual[:8]
        if isinstance(value, str) and _CJK_RE.search(value)
    ] if isinstance(residual, list) else []
    metadata = {
        "error_code": code,
        "elapsed_ms": row.get("elapsed_ms"),
        "repair_attempted": bool(row.get("repair_attempted")),
        "repair_reason_code": str(row.get("repair_reason_code") or ""),
    }
    if safe_residual:
        metadata["residual_cjk"] = safe_residual
    return PromptTranslationError(
        _SAFE_RUNTIME_ERROR_MESSAGES.get(code, _SAFE_RUNTIME_ERROR_MESSAGES["translation_failed"]),
        code=code,
        metadata=metadata,
    )


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider: str
    model: str
    metadata: dict


def source_text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_identity_mappings(project: SeriesProject) -> dict[str, str]:
    """Return the project's current canonical source-to-target terminology."""

    mappings: dict[str, str] = {}
    configured_glossary = (project.production_settings or {}).get(
        "translation_glossary", {},
    )
    if isinstance(configured_glossary, dict):
        mappings.update({
            str(source): str(target)
            for source, target in configured_glossary.items()
            if str(source).strip() and str(target).strip()
        })
    for character in project.characters.all():
        mappings.setdefault(character.name, character.stable_speaker_id)
    for scene in project.scenes.all():
        mappings.setdefault(scene.name, f"scene:{scene.slug}")
    for prop in project.props.all():
        mappings.setdefault(prop.name, f"prop:{prop.slug}")
    return mappings


def canonical_target_collisions(
    project: SeriesProject, *, production_settings: dict | None = None,
) -> list[dict]:
    """Return cross-entity collisions in explicit canonical-English mappings.

    Only current authoritative Character/Scene/Prop Bible names participate.
    Comparison is Unicode NFKC, whitespace-normalised and case-insensitive, so
    cosmetic spelling differences cannot create two identities that downstream
    prompts treat as the same proper name.
    """

    settings_value = (
        production_settings
        if production_settings is not None
        else (project.production_settings or {})
    )
    raw_glossary = (
        settings_value.get("translation_glossary", {})
        if isinstance(settings_value, dict) else {}
    )
    if not isinstance(raw_glossary, dict):
        return []
    glossary = {
        str(source).strip(): str(target).strip()
        for source, target in raw_glossary.items()
        if str(source).strip() and str(target).strip()
    }
    grouped: dict[str, list[dict]] = {}
    for scope_type, model in (
        (PromptFieldTranslation.ScopeType.CHARACTER, CharacterBible),
        (PromptFieldTranslation.ScopeType.SCENE, SceneBible),
        (PromptFieldTranslation.ScopeType.PROP, PropBible),
    ):
        for entity in model.objects.filter(project=project).only("id", "name"):
            name = str(entity.name or "").strip()
            target = glossary.get(name, "")
            if not target:
                continue
            normalized = " ".join(
                unicodedata.normalize("NFKC", target).split()
            ).casefold()
            if not normalized:
                continue
            grouped.setdefault(normalized, []).append({
                "scope_type": scope_type,
                "scope_id": str(entity.id),
                "name": name,
                "canonical_target": target,
                "_normalized_source": " ".join(
                    unicodedata.normalize("NFKC", name).split()
                ).casefold(),
            })
    collisions = []
    for normalized, entities in sorted(grouped.items()):
        if len({item["_normalized_source"] for item in entities}) < 2:
            continue
        public_entities = [{
            key: value for key, value in item.items()
            if key != "_normalized_source"
        } for item in entities]
        collisions.append({
            "code": CanonicalTargetCollisionError.code,
            "normalized_target": normalized,
            "entities": sorted(
                public_entities,
                key=lambda item: (item["scope_type"], item["name"], item["scope_id"]),
            ),
        })
    return collisions


def ensure_no_canonical_target_collisions(
    project: SeriesProject, *, production_settings: dict | None = None,
) -> None:
    collisions = canonical_target_collisions(
        project, production_settings=production_settings,
    )
    if collisions:
        raise CanonicalTargetCollisionError(collisions)


def relevant_canonical_identity_mappings(
    project: SeriesProject, source_text: str,
) -> dict[str, str]:
    """Keep only terminology that can influence this exact source field.

    The previous context hashed every Bible/glossary entry. Adding an unrelated
    prop therefore invalidated already-confirmed character translations even
    though neither their source nor the required terminology changed.
    """

    source = str(source_text or "")
    return {
        term: canonical
        for term, canonical in canonical_identity_mappings(project).items()
        if term in source
    }


def missing_relevant_canonical_identity_mappings(
    project: SeriesProject, source_text: str, translated_text: str,
) -> list[dict[str, object]]:
    """Describe required canonical identities removed by a manual edit.

    Explicit glossary values are safe to show to the operator.  Stable
    character/scene/prop identifiers remain an internal fallback; for those we
    expose only the Chinese source term and require the operator to configure a
    human-readable canonical English name through the glossary workflow.
    """

    candidate = str(translated_text or "")
    production_settings = project.production_settings or {}
    raw_glossary = (
        production_settings.get("translation_glossary", {})
        if isinstance(production_settings, dict) else {}
    )
    glossary = raw_glossary if isinstance(raw_glossary, dict) else {}
    missing: list[dict[str, object]] = []
    for source_term, canonical_target in sorted(
        relevant_canonical_identity_mappings(project, source_text).items(),
        key=lambda item: item[0],
    ):
        if canonical_target in candidate:
            continue
        configured_target = str(glossary.get(source_term) or "").strip()
        item: dict[str, object] = {"source_term": source_term}
        if configured_target and configured_target == canonical_target:
            item["canonical_target"] = canonical_target
        else:
            item["requires_glossary"] = True
        missing.append(item)
    return missing


def build_translation_context(
    project: SeriesProject, *, scope_type: str, scope_id, field_key: str,
    source_text: str | None = None,
) -> str:
    """Build the canonical identity context that constrains local translation.

    Only mappings whose source term occurs in this authoritative field are
    hashed. A relevant canonical change still invalidates the candidate, while
    adding an unrelated Bible entity does not.
    """
    if source_text is None:
        source_text = resolve_current_source(
            project=project, scope_type=scope_type, scope_id=scope_id,
            field_key=field_key,
        )
    return json.dumps({
        "context_version": TRANSLATION_CONTEXT_VERSION,
        "purpose": "MiniMax H3 prompt field",
        "scope": scope_type,
        "scope_id": str(scope_id or project.id),
        "field_key": field_key,
        "canonical_identity_mappings": relevant_canonical_identity_mappings(
            project, source_text,
        ),
        "rule": "When a source term is present, reuse its mapped target verbatim.",
    }, ensure_ascii=False, sort_keys=True)


def translation_context_hash(context: str) -> str:
    return source_text_hash(str(context or ""))


def translation_entry_context_current(
    entry: PromptFieldTranslation, *, project: SeriesProject | None = None,
    current_source: str | None = None, translated_text: str | None = None,
) -> bool:
    """Apply one context-currentness contract across review and compilation.

    Version-2 rows require an exact narrowed-context hash. Legacy rows did not
    record a version and hashed the entire project mapping. They can remain
    usable only when their source is unchanged and every *currently relevant*
    canonical target occurs verbatim in the reviewed translation. This safely
    tolerates unrelated additions without accepting a changed relevant name.
    """

    project = project or entry.project
    try:
        source = current_source if current_source is not None else resolve_current_source(
            project=project, scope_type=entry.scope_type, scope_id=entry.scope_id,
            field_key=entry.field_key,
        )
        if source_text_hash(str(source or "")) != entry.source_hash:
            return False
        current_context = build_translation_context(
            project, scope_type=entry.scope_type, scope_id=entry.scope_id,
            field_key=entry.field_key, source_text=str(source or ""),
        )
    except TranslationValidationError:
        return False

    metadata = entry.runtime_metadata or {}
    stored_hash = str(metadata.get("translation_context_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return False
    candidate = str(
        entry.translated_text if translated_text is None else translated_text
    )
    relevant = relevant_canonical_identity_mappings(project, str(source or ""))
    canonical_targets_present = bool(candidate) and all(
        canonical in candidate for canonical in relevant.values()
    )
    if stored_hash == translation_context_hash(current_context):
        # A matching context proves which terminology was required, but a
        # human can still edit the candidate after generation.  Recheck the
        # actual text before confirmation/promotion so canonical names cannot
        # be deleted and then written into authoritative Shot fields.
        return canonical_targets_present
    if metadata.get("translation_context_version") is not None:
        return False

    return canonical_targets_present


def resolve_current_source(
    *, project: SeriesProject, scope_type: str, scope_id, field_key: str,
) -> str:
    """Resolve an allow-listed field and prove that it belongs to project."""
    if not field_key_allowed(scope_type, field_key):
        raise TranslationValidationError("不支持的翻译字段")
    if scope_type == PromptFieldTranslation.ScopeType.PROJECT:
        if scope_id and str(scope_id) != str(project.id):
            raise TranslationValidationError("project scope_id 与当前项目不一致")
        if field_key == "style.visual_prompt":
            return str(project.style.visual_prompt if project.style else "")
        if field_key == "project.continuity_summary":
            return str(project.continuity_summary or "")
        return str((project.production_settings or {}).get("color_script", ""))

    models = {
        PromptFieldTranslation.ScopeType.CHARACTER: CharacterBible,
        PromptFieldTranslation.ScopeType.SCENE: SceneBible,
        PromptFieldTranslation.ScopeType.PROP: PropBible,
        PromptFieldTranslation.ScopeType.SHOT: Shot,
    }
    model = models.get(scope_type)
    if model is Shot:
        obj = model.objects.filter(pk=scope_id, episode__project=project).first()
    elif model:
        obj = model.objects.filter(pk=scope_id, project=project).first()
    else:
        obj = None
    if obj is None:
        raise TranslationValidationError("翻译字段作用域不存在或不属于当前项目")
    delivery_match = _DIALOGUE_DELIVERY_FIELD_RE.fullmatch(field_key)
    if delivery_match and isinstance(obj, Shot):
        index = int(delivery_match.group(1))
        dialogue = obj.dialogue if isinstance(obj.dialogue, list) else []
        if index >= len(dialogue) or not isinstance(dialogue[index], dict):
            raise TranslationValidationError("对白 delivery 索引已失效")
        return str(dialogue[index].get("delivery", "") or "")
    attribute = field_key.split(".", 1)[1]
    return str(getattr(obj, attribute, "") or "")


def _config() -> dict:
    configured = getattr(settings, "LOCAL_PROMPT_TRANSLATION", {}) or {}
    def resolve_path(raw) -> Path:
        value = Path(str(raw or "")).expanduser()
        return value if value.is_absolute() else (Path(settings.BASE_DIR) / value).resolve()

    model_dir = resolve_path(configured.get("model_dir"))
    runtime_script = resolve_path(configured.get("runtime_script"))
    runtime_python = resolve_path(configured.get("runtime_python") or sys.executable)
    return {
        "enabled": bool(configured.get("enabled", True)),
        "model_id": str(configured.get("model_id") or "Qwen/Qwen3-4B-GGUF"),
        "model_dir": model_dir,
        "runtime_script": runtime_script,
        "runtime_python": runtime_python,
        "device": str(configured.get("device") or "cpu").casefold(),
        "gpu_layers": int(configured.get("gpu_layers", -1)),
        "timeout_seconds": float(configured.get("timeout_seconds", 300)),
        "max_new_tokens": int(configured.get("max_new_tokens", 768)),
    }


def _model_installation(model_dir: Path) -> dict:
    manifest = {}
    try:
        raw_manifest = json.loads((model_dir / ".autoprompt-model.json").read_text(encoding="utf-8-sig"))
        if isinstance(raw_manifest, dict):
            manifest = raw_manifest
    except (OSError, json.JSONDecodeError, TypeError):
        manifest = {}
    try:
        manifest_size = int(manifest.get("size") or 0)
    except (TypeError, ValueError):
        manifest_size = 0
    manifest_matches = bool(
        manifest.get("repo_id") == PINNED_MODEL_REPO_ID
        and manifest.get("filename") == PINNED_MODEL_FILENAME
        and manifest_size == PINNED_MODEL_SIZE
        and str(manifest.get("sha256") or "").casefold() == PINNED_MODEL_SHA256
    )
    model_file = model_dir / PINNED_MODEL_FILENAME
    expected_size = manifest_size
    size_matches = bool(
        expected_size > 0 and model_file.is_file() and model_file.stat().st_size == expected_size
    )
    return {
        "downloaded": model_file.is_file(),
        "verified": bool(manifest_matches and size_matches),
        "size": expected_size if size_matches else 0,
        "sha256": str(manifest.get("sha256") or "") if size_matches else "",
        "verified_at": str(manifest.get("verified_at") or "") if size_matches else "",
    }


def qwen_runtime_device_state(config: dict) -> dict:
    """Return the selected isolated runtime's safe device readiness snapshot.

    CUDA is only usable after its own Python process both reports GPU-offload
    support and completes a disposable small-context model-load smoke test.
    The smoke test uses the same shared Qwen/video lock as real inference, so
    a capability check cannot race a video generation task for GPU memory.
    """
    device = str(config.get("device") or "cpu").casefold()
    try:
        gpu_layers = int(config.get("gpu_layers", -1))
    except (TypeError, ValueError):
        gpu_layers = None
    state = {
        "cuda_requested": device == "cuda",
        "gpu_layers": gpu_layers,
        "gpu_offload_supported": False,
        "gpu_context_smoke_passed": False,
        "gpu_ready": False,
        "runtime_ready": False,
        "runtime_version": "",
        "runtime_probe_error_code": "",
    }
    if device == "cpu":
        state["runtime_ready"] = True
        return state
    if device != "cuda":
        state["runtime_probe_error_code"] = "unsupported_device"
        return state
    if gpu_layers is None or gpu_layers < -1 or gpu_layers == 0:
        state["runtime_probe_error_code"] = "invalid_gpu_layers"
        return state
    runtime_script = config.get("runtime_script")
    runtime_python = config.get("runtime_python")
    model_dir = config.get("model_dir")
    if not isinstance(runtime_script, Path) or not runtime_script.is_file():
        state["runtime_probe_error_code"] = "runtime_script_missing"
        return state
    if not isinstance(runtime_python, Path) or not runtime_python.is_file():
        state["runtime_probe_error_code"] = "runtime_python_missing"
        return state
    if not isinstance(model_dir, Path) or not model_dir.is_dir():
        state["runtime_probe_error_code"] = "model_dir_missing"
        return state
    child_environment = os.environ.copy()
    child_environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        # This operation loads then immediately closes the model. Keep it
        # under the same cross-process lease as ComfyUI/Qwen inference: a
        # wheel that crashes during context construction must never be called
        # "ready", and the probe must not contend with video VRAM.
        with local_qwen_runtime_lock():
            completed = subprocess.run(
                [str(runtime_python), str(runtime_script)],
                input=json.dumps({
                    "operation": "runtime_probe",
                    "device": "cuda",
                    "gpu_layers": gpu_layers,
                    "model_dir": str(model_dir),
                    "model_id": config.get("model_id"),
                    "smoke_test": True,
                }),
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=90,
                cwd=str(settings.BASE_DIR),
                check=False,
                env=child_environment,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                ),
            )
    except LocalQwenResourceBusy:
        state["runtime_probe_error_code"] = "shared_resource_busy"
        return state
    except subprocess.TimeoutExpired:
        state["runtime_probe_error_code"] = "runtime_probe_timeout"
        return state
    except OSError:
        state["runtime_probe_error_code"] = "runtime_probe_launch_failed"
        return state
    try:
        payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        state["runtime_probe_error_code"] = "runtime_probe_invalid_json"
        return state
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        effective_layers = int(metadata.get("n_gpu_layers"))
    except (TypeError, ValueError):
        effective_layers = None
    offload_supported = bool(metadata.get("gpu_offload_supported"))
    smoke_passed = bool(metadata.get("gpu_context_smoke_passed"))
    ready = bool(
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("ok") is True
        and metadata.get("device") == "cuda"
        and offload_supported
        and smoke_passed
        and effective_layers == gpu_layers
        and effective_layers != 0
    )
    probe_error_code = ""
    if isinstance(payload, dict):
        probe_error_code = str(payload.get("error_code") or "").strip()[:80]
    state.update({
        "gpu_offload_supported": offload_supported,
        "gpu_context_smoke_passed": smoke_passed,
        "gpu_ready": ready,
        "runtime_ready": ready,
        "runtime_version": str(metadata.get("runtime_version") or ""),
        "runtime_probe_error_code": "" if ready else probe_error_code or "runtime_probe_failed",
    })
    return state


def get_translation_capability() -> dict:
    config = _config()
    model_dir = config["model_dir"]
    runtime_script = config["runtime_script"]
    installation = _model_installation(model_dir)
    runtime_state = qwen_runtime_device_state(config)
    local_capability = {
        "id": LOCAL_QWEN_PROVIDER,
        "label": "本地 Qwen",
        "enabled": config["enabled"],
        "configured": bool(
            config["enabled"] and model_dir.is_dir() and runtime_script.is_file()
            and config["runtime_python"].is_file()
            and installation["verified"] and runtime_state["runtime_ready"]
        ),
        "provider": "local_qwen",
        "model": config["model_id"],
        "model_downloaded": installation["downloaded"],
        "model_verified": installation["verified"],
        "model_size_bytes": installation["size"],
        "model_sha256": installation["sha256"],
        "verified_at": installation["verified_at"],
        "device": config["device"],
        "gpu_layers": config.get("gpu_layers", -1),
        **runtime_state,
        "timeout_seconds": config["timeout_seconds"],
        "loaded_on_demand": True,
        "unloaded_after_request": True,
        "review_required": True,
        "official_qwen_mt_checkpoint": False,
        "estimated_download_gb": 2.5,
        "install_command": (
            "powershell -ExecutionPolicy Bypass -File "
            ".\\scripts\\download_local_translation_model.ps1"
        ),
        "notice": (
            "官方 Qwen-MT 当前为云 API 模型；本地运行使用 ModelScope 可下载的开源 Qwen "
            "模型完成翻译，必须人工确认后才会进入正式提示词。"
        ),
    }
    local_capability["ready"] = local_capability["configured"]
    local_capability["transport"] = "local_process"
    local_capability["data_egress"] = False

    deepseek_capability = dict(get_deepseek_capability())
    deepseek_capability.setdefault("id", DEEPSEEK_API_PROVIDER)
    deepseek_capability.setdefault("provider", DEEPSEEK_API_PROVIDER)
    deepseek_capability.setdefault("label", "DeepSeek API")
    deepseek_capability.setdefault("transport", "remote_api")
    deepseek_capability.setdefault("data_egress", True)
    deepseek_capability.setdefault(
        "data_egress_notice",
        "选择 DeepSeek API 会把本次待翻译文本和相关提示上下文发送到外部服务。",
    )
    deepseek_capability["ready"] = bool(
        deepseek_capability.get("enabled") and deepseek_capability.get("configured")
    )
    default_provider = str(
        getattr(settings, "PROMPT_TRANSLATION_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER)
        or LOCAL_QWEN_PROVIDER
    )
    if default_provider not in SUPPORTED_MODEL_PROVIDERS:
        default_provider = LOCAL_QWEN_PROVIDER
    return {
        # Preserve the original top-level local-Qwen fields for older clients.
        **local_capability,
        "default_provider": default_provider,
        "providers": [local_capability, deepseek_capability],
    }


def normalise_model_provider(value: str | None, *, default: str | None = None) -> str:
    """Return a server-supported provider id without accepting arbitrary transports."""
    provider = str(value or default or LOCAL_QWEN_PROVIDER).strip().casefold()
    if provider not in SUPPORTED_MODEL_PROVIDERS:
        raise TranslationValidationError(
            "不支持的模型通道",
            code="unsupported_model_provider",
        )
    return provider


def _run_translation_provider(request: dict, config: dict, provider: str) -> dict:
    if provider == LOCAL_QWEN_PROVIDER:
        _require_runtime(config)
        local_request = {
            **request,
            "model_dir": str(config["model_dir"]),
            "model_id": config["model_id"],
            "device": config["device"],
            "gpu_layers": config.get("gpu_layers", -1),
            "max_new_tokens": config["max_new_tokens"],
        }
        return _run_runtime(local_request, config)
    try:
        remote_request = dict(request)
        if "items" in remote_request:
            remote_request["items"] = [
                {**item, "max_new_tokens": config["max_new_tokens"]}
                for item in remote_request["items"]
            ]
        else:
            remote_request["max_new_tokens"] = config["max_new_tokens"]
        return run_deepseek_translation(remote_request)
    except (DeepSeekConfigurationError, DeepSeekAPIError) as exc:
        raise PromptTranslationError(
            str(exc),
            code=str(getattr(exc, "code", "deepseek_api_failed")),
            metadata=dict(getattr(exc, "runtime_metadata", {}) or {}),
        ) from exc


def _protect_authored_blocks(text: str) -> tuple[str, dict[str, str]]:
    blocks: dict[str, str] = {}
    salt = source_text_hash(text)[:12].upper()

    def replace(match: re.Match) -> str:
        token = f"__AUTOPROMPT_LITERAL_{salt}_{len(blocks) + 1:04d}__"
        blocks[token] = match.group(0)
        return token

    return _PROTECTED_BLOCK_RE.sub(replace, text), blocks


def _canonical_mappings_from_context(context: str) -> dict[str, str]:
    """Read the server-owned canonical map from one translation context.

    The context is also sent to the model as reference prose, but small models
    may paraphrase a required canonical target even after being told to copy it
    verbatim.  Extracting only this allow-listed object lets the transport
    protect each applicable source term as an exact token instead of relying
    on prompt compliance.
    """

    try:
        payload = json.loads(str(context or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    raw = payload.get("canonical_identity_mappings") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        source: target
        for source, target in raw.items()
        if isinstance(source, str) and source
        and isinstance(target, str) and target
    }


def _protect_translation_contract(
    text: str, context: str,
) -> tuple[str, dict[str, str]]:
    """Protect authored literals and canonical identities in source order."""

    protected, replacements = _protect_authored_blocks(text)
    mappings = _canonical_mappings_from_context(context)
    active_terms = sorted(
        (source for source in mappings if source in protected),
        key=len,
        reverse=True,
    )
    if active_terms:
        salt = source_text_hash(text)[:12].upper()
        pattern = re.compile("|".join(re.escape(source) for source in active_terms))

        def replace_canonical(match: re.Match) -> str:
            token = (
                f"__AUTOPROMPT_LITERAL_{salt}_"
                f"{len(replacements) + 1:04d}__"
            )
            replacements[token] = mappings[match.group(0)]
            return token

        protected = pattern.sub(replace_canonical, protected)

    # Authored blocks are protected before canonical terms, so rebuild the
    # dictionary in actual source order for the existing order guard.
    token_order = _PROTECTED_TOKEN_RE.findall(protected)
    replacements = {token: replacements[token] for token in token_order}
    return protected, replacements


def _restore_authored_blocks(candidate: str, blocks: dict[str, str]) -> str:
    for token in blocks:
        if candidate.count(token) != 1:
            raise TranslationValidationError(
                f"翻译结果修改、删除或复制了受保护占位符 {token}"
            )
    unknown = set(_PROTECTED_TOKEN_RE.findall(candidate)) - set(blocks)
    if unknown:
        raise TranslationValidationError("翻译结果新增了未知受保护占位符")
    if _PROTECTED_TOKEN_RE.findall(candidate) != list(blocks):
        raise TranslationValidationError("翻译结果改变了受保护原文块的顺序")
    restored = candidate
    for token, original in blocks.items():
        restored = restored.replace(token, original)
    if _PROTECTED_BLOCK_RE.findall(restored) != _PROTECTED_BLOCK_RE.findall(
        "".join(blocks.values())
    ):
        # The exact block strings are checked below; this shape check primarily
        # catches a model adding its own authored-text tag.
        original_blocks = list(blocks.values())
        restored_blocks = [match.group(0) for match in _PROTECTED_BLOCK_RE.finditer(restored)]
        if restored_blocks != original_blocks:
            raise TranslationValidationError("翻译结果改变了 <d>/<lyrics>/<screen_text> 原文块")
    return restored.strip()


def _run_runtime(request: dict, config: dict) -> dict:
    """Execute one disposable local-model process and parse its JSON result."""
    try:
        with local_qwen_runtime_lock():
            completed = subprocess.run(
                [str(config["runtime_python"]), str(config["runtime_script"])],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=config["timeout_seconds"],
                cwd=str(settings.BASE_DIR),
                check=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                ),
            )
    except LocalQwenResourceBusy as exc:
        raise PromptTranslationResourceBusy(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise PromptTranslationError("本地翻译超时；模型进程已终止，原文未被覆盖") from exc
    except OSError as exc:
        raise TranslationRuntimeUnavailable("无法启动本地翻译进程") from exc
    try:
        payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        if completed.returncode != 0:
            raise PromptTranslationError("本地翻译进程执行失败；原文未被覆盖") from exc
        raise PromptTranslationError("本地翻译进程返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise PromptTranslationError("本地翻译进程返回了无效结果")
    if payload.get("ok") is not True:
        raise _safe_runtime_error(payload)
    if completed.returncode != 0:
        raise PromptTranslationError("本地翻译进程异常退出；原文未被覆盖")
    return payload


def _require_runtime(config: dict) -> None:
    if not config["enabled"]:
        raise TranslationRuntimeUnavailable("本地翻译功能未启用")
    if not config["runtime_script"].is_file():
        raise TranslationRuntimeUnavailable("本地翻译运行脚本不存在")
    if not config["runtime_python"].is_file():
        raise TranslationRuntimeUnavailable("本地翻译独立 Python 运行环境尚未安装")
    installation = _model_installation(config["model_dir"])
    if not installation["downloaded"]:
        raise TranslationRuntimeUnavailable("本地翻译模型尚未下载")
    if not installation["verified"]:
        raise TranslationRuntimeUnavailable("本地翻译模型未通过安装清单校验")
    if config["device"] == "cuda":
        runtime_state = qwen_runtime_device_state(config)
        if not runtime_state["gpu_ready"]:
            raise TranslationRuntimeUnavailable(
                "CUDA Qwen runtime is not ready for GPU offload; configure the separate CUDA runtime "
                "and QWEN_GPU_LAYERS, then retry. No CPU fallback was performed"
            )


def translate_text_once(
    text: str,
    *,
    source_lang: str = "Chinese",
    target_lang: str = "English",
    field_key: str = "",
    label: str = "",
    context: str = "",
    attempt: int = 1,
    provider: str | None = None,
) -> TranslationResult:
    """Translate one field in a disposable child process.

    stdout is a single JSON object. stderr is never returned to API clients
    because third-party model loaders may print local paths and diagnostics.
    """
    source = str(text or "")
    if not source.strip():
        raise TranslationValidationError("source_text 不能为空")
    provider = normalise_model_provider(
        provider,
        default=getattr(settings, "PROMPT_TRANSLATION_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER),
    )
    config = _config()

    protected_text, blocks = _protect_translation_contract(source, context)
    request = {
        "request_id": source_text_hash(source)[:24],
        "text": protected_text,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "field_key": field_key,
        "label": label,
        "context": context,
        "protected_tokens": list(blocks),
        "attempt": max(1, min(int(attempt), 100)),
    }
    payload = _run_translation_provider(request, config, provider)
    candidate = payload.get("translation")
    if not isinstance(candidate, str) or not candidate.strip():
        raise PromptTranslationError("所选模型通道没有返回有效译文")
    restored = _restore_authored_blocks(candidate, blocks)
    restored = validate_manual_translation(source, restored)
    payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    is_local = provider == LOCAL_QWEN_PROVIDER
    return TranslationResult(
        text=restored,
        provider=provider,
        model=str(payload.get("model") or payload_metadata.get("model") or (
            config["model_id"] if is_local else ""
        )),
        metadata={
            "elapsed_ms": payload.get("elapsed_ms"),
            "provider": provider,
            "transport": "local_process" if is_local else "remote_api",
            "device": config["device"] if is_local else "remote",
            "gpu_layers": payload_metadata.get(
                "n_gpu_layers",
                config.get("gpu_layers", -1) if is_local and config["device"] == "cuda" else 0,
            ),
            "loaded_on_demand": is_local,
            "unloaded_after_request": is_local,
            "repair_attempted": bool(payload_metadata.get("repair_attempted")),
            "repair_reason_code": str(
                payload_metadata.get("repair_reason_code") or ""
            ),
            "usage": payload_metadata.get("usage", {}),
            "protected_block_count": len(blocks),
            "translation_context_hash": translation_context_hash(context),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        },
    )


def translate_texts_once(
    texts: list[str], *, source_lang: str = "Chinese", target_lang: str = "English",
    field_keys: list[str] | None = None,
    labels: list[str] | None = None,
    contexts: list[str] | None = None,
    provider: str | None = None,
) -> list[TranslationResult | PromptTranslationError]:
    """Translate multiple fields with one model load and one process exit."""
    if not texts:
        return []
    provider = normalise_model_provider(
        provider,
        default=getattr(settings, "PROMPT_TRANSLATION_DEFAULT_PROVIDER", LOCAL_QWEN_PROVIDER),
    )
    config = _config()
    prepared = []
    blocks_by_id: dict[str, dict[str, str]] = {}
    sources_by_id: dict[str, str] = {}
    field_keys = field_keys or [""] * len(texts)
    labels = labels or [""] * len(texts)
    contexts = contexts or [""] * len(texts)
    if not (len(texts) == len(field_keys) == len(labels) == len(contexts)):
        raise TranslationValidationError("批量翻译 hints 数量与 texts 不一致")
    for index, raw in enumerate(texts):
        source = str(raw or "")
        if not source.strip():
            prepared.append(None)
            continue
        request_id = f"{index:04d}-{source_text_hash(source)[:20]}"
        protected, blocks = _protect_translation_contract(source, contexts[index])
        blocks_by_id[request_id] = blocks
        sources_by_id[request_id] = source
        prepared.append({
            "request_id": request_id,
            "text": protected,
            "protected_tokens": list(blocks),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "field_key": field_keys[index],
            "label": labels[index],
            "context": contexts[index],
        })
    runtime_items = [item for item in prepared if item is not None]
    if not runtime_items:
        return [TranslationValidationError("source_text 不能为空") for _ in texts]
    payload = _run_translation_provider({
        "items": runtime_items,
    }, config, provider)
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise PromptTranslationError("批量翻译进程没有返回 items")
    by_id = {
        str(row.get("request_id")): row for row in rows if isinstance(row, dict)
    }
    results: list[TranslationResult | PromptTranslationError] = []
    for item in prepared:
        if item is None:
            results.append(TranslationValidationError("source_text 不能为空"))
            continue
        row = by_id.get(item["request_id"])
        if not row or row.get("ok") is not True:
            results.append(_safe_runtime_error(row or {}))
            continue
        candidate = row.get("translation")
        if not isinstance(candidate, str) or not candidate.strip():
            results.append(PromptTranslationError("该字段没有返回有效译文"))
            continue
        try:
            restored = _restore_authored_blocks(
                candidate, blocks_by_id[item["request_id"]],
            )
            restored = validate_manual_translation(
                sources_by_id[item["request_id"]], restored,
            )
        except PromptTranslationError as exc:
            results.append(exc)
            continue
        payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        is_local = provider == LOCAL_QWEN_PROVIDER
        results.append(TranslationResult(
            text=restored,
            provider=provider,
            model=str(payload.get("model") or payload_metadata.get("model") or (
                config["model_id"] if is_local else ""
            )),
            metadata={
                "elapsed_ms": row.get("elapsed_ms"),
                "batch_elapsed_ms": payload.get("elapsed_ms"),
                "provider": provider,
                "transport": "local_process" if is_local else "remote_api",
                "device": config["device"] if is_local else "remote",
                "gpu_layers": (payload_metadata.get(
                    "n_gpu_layers",
                    config.get("gpu_layers", -1) if is_local and config["device"] == "cuda" else 0,
                )),
                "loaded_on_demand": is_local,
                "unloaded_after_request": is_local,
                "repair_attempted": bool(row.get("repair_attempted")),
                "repair_reason_code": str(row.get("repair_reason_code") or ""),
                "usage": payload_metadata.get("usage", {}),
                "protected_block_count": len(blocks_by_id[item["request_id"]]),
                "translation_context_hash": translation_context_hash(
                    contexts[int(item["request_id"].split("-", 1)[0])]
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        ))
    return results


def validate_manual_translation(source: str, translated: str) -> str:
    """Validate an edited candidate without invoking the model."""
    protected, blocks = _protect_authored_blocks(source)
    candidate = translated
    for token, original in blocks.items():
        candidate = candidate.replace(original, token)
    # Every original protected block must occur once and in order. Replacing
    # exact originals above makes the same fail-closed validator reusable.
    restored = _restore_authored_blocks(candidate, blocks)
    director_text = _PROTECTED_BLOCK_RE.sub("", restored)
    non_latin = [
        character for character in director_text
        if character.isalpha() and "LATIN" not in unicodedata.name(character, "")
    ]
    if non_latin:
        raise TranslationValidationError("英文译文在受保护原文块之外包含非拉丁文字")
    return restored


def confirmed_translation(
    *, project_id, scope_type: str, scope_id, field_key: str, source_text: str,
) -> str:
    source = str(source_text or "")
    if not source:
        return source
    entry = PromptFieldTranslation.objects.filter(
        project_id=project_id,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_hash=source_text_hash(source),
        status=PromptFieldTranslation.Status.CONFIRMED,
    ).only(
        "scope_type", "scope_id", "field_key", "source_hash",
        "translated_text", "runtime_metadata",
    ).first()
    if entry is None:
        return source
    try:
        project = SeriesProject.objects.get(pk=project_id)
    except SeriesProject.DoesNotExist:
        return source
    if not translation_entry_context_current(
        entry, project=project, current_source=source,
    ):
        return source
    return entry.translated_text or source


def confirmed_translation_fingerprint(
    *, project_id, scope_ids: Iterable,
) -> str:
    rows = PromptFieldTranslation.objects.filter(
        project_id=project_id,
        scope_id__in=list(scope_ids),
        status=PromptFieldTranslation.Status.CONFIRMED,
    ).values(
        "scope_type", "scope_id", "field_key", "source_hash", "translated_text", "revision",
    ).order_by("scope_type", "scope_id", "field_key")
    canonical = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
