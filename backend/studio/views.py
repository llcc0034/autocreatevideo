from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from studio.models import (
    Asset,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    GeneratedWorkflow,
    GenerationTask,
    PromptFieldTranslation,
    PromptArtifact,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplate,
    WorkerLease,
)
from studio.permissions import IsOwnerOrStaff, IsOwnerOrStaffOrGlobalReadOnly, IsStaffOrReadOnly
from studio.media_signing import classify_task_output, load_signed_media_token, signed_media_token
from studio.serializers import (
    AssetSerializer,
    AssemblyJobSerializer,
    AssembleEpisodeSerializer,
    AssembleSeriesSerializer,
    CharacterBibleSerializer,
    CanonicalGlossaryEnsureSerializer,
    CanonicalGlossaryEnsureResponseSerializer,
    ComfyUIWorkflowExportSerializer,
    ComfyUIWorkflowSyncSerializer,
    ConsistencyLockSerializer,
    EpisodeSerializer,
    EpisodeShotSyncSerializer,
    GeneratedWorkflowSerializer,
    GenerationTaskCreateSerializer,
    GenerationTaskInputPromptSerializer,
    GenerationTaskSerializer,
    PromptFieldTranslationSerializer,
    PromptTranslationBatchRequestSerializer,
    PromptTranslationConfirmSerializer,
    PromptTranslationPromoteShotResponseSerializer,
    PromptTranslationPromoteShotSerializer,
    PromptTranslationRetranslateSerializer,
    TaskOutputPromoteSerializer,
    NextEpisodeSerializer,
    ProductionBuildSerializer,
    ProductionWorkflowTemplateSerializer,
    ProjectBibleSyncSerializer,
    PromptArtifactSerializer,
    PromptGenerateSerializer,
    PropBibleSerializer,
    RegisterSerializer,
    ReleaseBundleCreateSerializer,
    ReleaseBundleProposeSerializer,
    ReleaseBundleSerializer,
    SceneBibleSerializer,
    ScriptBreakdownSerializer,
    SeriesProjectSerializer,
    ShotSerializer,
    StyleSerializer,
    UserSerializer,
    VIDEO_SHOT_MAX_DURATION_MS,
    VIDEO_SHOT_MIN_DURATION_MS,
    WorkflowGenerateSerializer,
    WorkflowTemplateSerializer,
)
from studio.services.comfyui import ComfyUIClient, ComfyUIError
from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
)
from studio.services.assembly import AssemblyError, create_assembly
from studio.services.h3_prompts import (
    H3PromptOptions,
    PROMPT_COMPILER_VERSION,
    PROMPT_INPUT_FINGERPRINT_VERSION,
    PromptBuildError,
    build_consistent_image_prompt,
    build_h3_video_prompt,
    build_negative_prompt,
    capture_consistency_snapshot,
    load_shots,
    prompt_input_fingerprint,
    sync_consistency_locks,
)
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.shot_prompt_recovery import repair_legacy_negative_prompt
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    PromptTranslationError,
    PromptTranslationResourceBusy,
    TranslationResult,
    TranslationValidationError,
    TRANSLATION_CONTEXT_VERSION,
    LOCAL_QWEN_PROVIDER,
    build_translation_context,
    ensure_no_canonical_target_collisions,
    resolve_current_source,
    source_text_hash,
    translation_entry_context_current,
    translation_context_hash,
    translate_text_once,
    translate_texts_once,
    validate_manual_translation,
)
from studio.services.canonical_glossary import (
    CanonicalGlossaryCapacityError,
    ensure_canonical_glossary as ensure_project_canonical_glossary,
)
from studio.services.workflow import WorkflowBuildError, build_workflow
from studio.services.script_breakdown import breakdown_script
from studio.services.resolution import (
    H3_RESOLUTION_MULTIPLE_DEFAULT,
    ResolutionProfileError,
    resolve_production_resolution,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    normalize_shot_reference_bindings,
    resolve_group_reference_bindings,
)
from studio.services.provenance import (
    ProvenanceError,
    assembly_manifest_path,
    persist_assembly_provenance,
    persist_task_provenance,
    sha256_file,
    verified_assembly_manifest,
    verified_task_manifest,
)
from studio.services.distribution import (
    DistributionError,
    distribution_artifact_path,
    prepare_task_distribution,
    verified_distribution_manifest,
)
from studio.services.release_bundles import (
    ReleaseBundleError,
    asset_is_release_evidence,
    create_release_draft,
    live_verified_release_bundle,
    mark_release_bundle_released,
    normalize_release_selection,
    propose_release_selection,
    seal_release_bundle,
    task_is_release_evidence,
)

User = get_user_model()

_STAGED_ASSET_TOKEN_PREFIX = "__AUTOPROMPT_ASSET__:"


def _count_staged_asset_tokens(value) -> int:
    if isinstance(value, str):
        return int(value.startswith(_STAGED_ASSET_TOKEN_PREFIX))
    if isinstance(value, dict):
        return sum(_count_staged_asset_tokens(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_count_staged_asset_tokens(item) for item in value)
    return 0


def _collect_staged_asset_tokens(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith(_STAGED_ASSET_TOKEN_PREFIX) else set()
    if isinstance(value, dict):
        return set().union(*(_collect_staged_asset_tokens(item) for item in value.values()), set())
    if isinstance(value, (list, tuple)):
        return set().union(*(_collect_staged_asset_tokens(item) for item in value), set())
    return set()


def _replace_export_staged_tokens(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_export_staged_tokens(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_export_staged_tokens(item, mapping) for item in value]
    if isinstance(value, str) and value in mapping:
        return mapping[value]
    return value


def _resolved_task_api_workflow(task: GenerationTask) -> tuple[dict, set[str]]:
    """Resolve staged input tokens without trusting malformed legacy records."""
    persisted_workflow = task.generated_workflow.workflow_json
    required_tokens = _collect_staged_asset_tokens(persisted_workflow)
    staged_mapping = {
        item.get("token"): item.get("comfy_path")
        for item in (task.staged_inputs or [])
        if isinstance(item, dict) and item.get("token") and item.get("comfy_path")
    }
    missing_tokens = required_tokens - set(staged_mapping)
    workflow = (
        _replace_export_staged_tokens(persisted_workflow, staged_mapping)
        if required_tokens and not missing_tokens else persisted_workflow
    )
    return workflow, missing_tokens


def _is_comfyui_api_graph(value) -> bool:
    """Match the API-graph checks used by the installed ComfyUI frontend."""

    if not isinstance(value, dict) or not value:
        return False
    return all(
        isinstance(node, dict)
        and isinstance(node.get("class_type"), str)
        and bool(node["class_type"])
        and isinstance(node.get("inputs"), dict)
        for node in value.values()
    )


def _safe_comfyui_browser_url() -> str:
    """Expose only a browser-safe root URL, never credentials or URL intents."""

    raw_url = str(getattr(settings, "COMFYUI_BASE_URL", "")).strip()
    if not raw_url or "\r" in raw_url or "\n" in raw_url:
        return ""
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    path = f"{parsed.path.rstrip('/')}/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _owned_or_global(queryset, user):
    if not getattr(user, "is_authenticated", False):
        return queryset.filter(owner__isnull=True)
    return queryset if user.is_staff else queryset.filter(Q(owner=user) | Q(owner__isnull=True))


def _owned_projects(queryset, user):
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset if user.is_staff else queryset.filter(owner=user)


def _can_execute_template(template: WorkflowTemplate, user) -> bool:
    """Only curated global system templates may be run by non-staff users.

    Older releases allowed a user-owned template to remain executable after it
    was created.  That turns a saved arbitrary Comfy graph into a durable code
    execution path, so ownership is deliberately *not* sufficient here.
    """
    if not template.is_active:
        return False
    return bool(
        getattr(user, "is_staff", False)
        or (template.owner_id is None and template.is_system)
    )


def _executable_templates(queryset, user):
    """Return the templates this actor is allowed to submit to ComfyUI."""
    queryset = queryset.filter(is_active=True)
    if getattr(user, "is_staff", False):
        return queryset
    return queryset.filter(owner__isnull=True, is_system=True)


def _assert_queue_capacity(user, additional: int = 1):
    WorkerLease.objects.get_or_create(key="generation-capacity")
    WorkerLease.objects.select_for_update().get(key="generation-capacity")
    active_statuses = (
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )
    active = GenerationTask.objects.filter(status__in=active_statuses).count()
    if active + additional > settings.TASK_MAX_PENDING:
        raise ValidationError(
            f"本地 H3 安全队列最多保留 {settings.TASK_MAX_PENDING} 个活动任务；请等待完成或先只生成工作流"
        )
    user_active = GenerationTask.objects.filter(owner=user, status__in=active_statuses).count()
    if user_active + additional > settings.TASK_MAX_ACTIVE_PER_USER:
        raise ValidationError(
            f"每个用户最多保留 {settings.TASK_MAX_ACTIVE_PER_USER} 个活动生成任务；请等待现有任务完成"
        )


def _create_generation_task(
    *, user, workflow: GeneratedWorkflow, priority: int, max_attempts: int,
    capability_snapshot_overrides: dict | None = None,
) -> GenerationTask:
    injected = workflow.injected_values or {}
    prompt = workflow.prompt_artifact
    is_image = prompt.prompt_type == PromptArtifact.PromptType.IMAGE
    episode = workflow.episode or prompt.episode
    shot = prompt.shot
    request_shot_ids = (prompt.request_options or {}).get("shot_ids")
    shot_ids = (
        [str(value) for value in request_shot_ids]
        if isinstance(request_shot_ids, list)
        else ([str(shot.id)] if shot else [])
    )
    if is_image:
        backend = "comfyui_local"
        profile = f"image:{workflow.template.slug}:v{workflow.template.version}"
    else:
        backend = "comfy_local_h3"
        workflow_mode = (workflow.template.input_schema or {}).get("workflow_mode", "legacy")
        profile = (workflow.template.input_schema or {}).get("model_profile") or {
            "text_to_video": "local_h3_t2va_official",
            "first_last_to_video": "local_h3_i2v_official",
            "reference_to_video": "local_h3_ref2va_official",
        }.get(workflow_mode, "local_h3_t2va_official")
    reference_records = prompt.structured_payload.get("reference_bindings", [])
    user_parameter_definitions = (
        (workflow.template.input_schema or {}).get("user_parameters") or {}
    )
    workflow_parameters = {
        key: injected.get(key, definition.get("default"))
        for key, definition in user_parameter_definitions.items()
        if isinstance(key, str) and isinstance(definition, dict)
    }
    capability_snapshot = {
        "template_id": str(workflow.template_id), "template_hash": workflow.content_hash,
        "template_slug": workflow.template.slug,
        "model": prompt.model, "h3_mode": prompt.h3_mode,
        "effective_conditioning_mode": prompt.structured_payload.get(
            "effective_conditioning_mode", prompt.h3_mode,
        ),
        "prompt_template_key": prompt.structured_payload.get("prompt_template_key", ""),
        "workflow_mode": (workflow.template.input_schema or {}).get("workflow_mode", "legacy"),
        "source_workflow": (workflow.template.input_schema or {}).get("source_workflow", ""),
        "workflow_parameters": workflow_parameters,
        "resolution_profile": injected.get("RESOLUTION_PROFILE", "project"),
        "resolution_multiple": int(injected.get(
            "RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT,
        )),
        "width": int(injected.get("WIDTH", 0)),
        "height": int(injected.get("HEIGHT", 0)),
        "episode_id": str(episode.id) if episode else None,
        "episode_number": episode.number if episode else None,
        "episode_title": episode.title if episode else "",
        "shot_ids": shot_ids,
        "shot_id": str(shot.id) if shot else None,
        "shot_number": shot.number if shot else None,
        "shot_title": shot.title if shot else "",
        "reference_assets": [
            {
                "id": item.get("asset_id") or item.get("id"),
                "checksum": item.get("checksum"),
                "input_slot": item.get("input_slot"),
                "input_label": item.get("input_label"),
                "category": item.get("category"),
                "entity_id": item.get("entity_id"),
                "entity_name": item.get("entity_name"),
                "audio_asset_id": item.get("audio_asset_id"),
                "audio_input_label": item.get("audio_input_label"),
                "use_embedded_audio": item.get("use_embedded_audio", False),
            }
            for item in reference_records
        ],
    }
    if not is_image:
        capability_snapshot.update({
            "fps": int(injected.get("FPS", 0)),
            "include_audio": prompt.include_audio,
            "requested_duration_ms": prompt.total_duration_ms,
        })
    capability_snapshot.update(capability_snapshot_overrides or {})
    if is_image:
        # The image graph may carry FPS/FRAMES as internal node defaults, but
        # they are not user-facing image-generation parameters.  Do not freeze
        # misleading video semantics into a new image task snapshot.
        for key in ("fps", "include_audio", "requested_duration_ms"):
            capability_snapshot.pop(key, None)
    return GenerationTask.objects.create(
        owner=user, project=workflow.project, episode=workflow.episode,
        generated_workflow=workflow, kind=prompt.prompt_type,
        priority=priority, max_attempts=max_attempts,
        backend=backend, model_profile=profile,
        workflow_version=workflow.template.version,
        seed=int(injected.get("SEED", workflow.project.continuity_seed)),
        effective_frames=0 if is_image else int(injected.get("FRAMES", 0)),
        effective_duration_ms=(
            0 if is_image else int(injected.get("EFFECTIVE_DURATION_MS", 0))
        ),
        capability_snapshot=capability_snapshot,
    )


def _clone_generation_task_for_retry(task: GenerationTask) -> GenerationTask:
    """Create an auditable retry without mutating the terminal source task."""
    injected = task.generated_workflow.injected_values or {}
    capability_snapshot = {
        **(task.capability_snapshot or {}),
        "resolution_multiple": int(
            (task.capability_snapshot or {}).get("resolution_multiple")
            or injected.get("RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT)
        ),
        "retry_of": str(task.id),
        "batch_role": "retry",
    }
    is_image = task.kind == GenerationTask.Kind.IMAGE
    if is_image:
        for key in ("fps", "include_audio", "requested_duration_ms"):
            capability_snapshot.pop(key, None)
    return GenerationTask.objects.create(
        owner=task.owner,
        project=task.project,
        episode=task.episode,
        generated_workflow=task.generated_workflow,
        kind=task.kind,
        backend=task.backend,
        model_profile=task.model_profile,
        workflow_version=task.workflow_version,
        seed=task.seed,
        effective_frames=0 if is_image else task.effective_frames,
        effective_duration_ms=0 if is_image else task.effective_duration_ms,
        capability_snapshot=capability_snapshot,
        priority=task.priority,
        max_attempts=task.max_attempts,
    )


def _episode_batch_fingerprint(
    *, project: SeriesProject, episode: Episode, shots: list[Shot], data: dict,
    consistency_snapshot: dict,
) -> str:
    """Fingerprint the requested batch and the ordered source-shot snapshot.

    The project row is locked before this is called, so the fingerprint and
    any tasks created from it describe one consistent editor snapshot.
    """
    payload = {
        "version": 1,
        "project_id": str(project.id),
        "episode_id": str(episode.id),
        "episode": {
            "number": episode.number,
            "title": episode.title,
            "synopsis": episode.synopsis,
            "script": episode.script,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings,
        },
        "consistency_snapshot": consistency_snapshot,
        "whole_episode": True,
        "template_id": str(data["template_id"]) if data.get("template_id") else None,
        "priority": data["priority"],
        "max_attempts": data["max_attempts"],
        "resolution_profile": data["resolution_profile"],
        "resolution_multiple": data["resolution_multiple"],
        "image_resolution_profile": data["image_resolution_profile"],
        "image_resolution_multiple": data["image_resolution_multiple"],
        "workflow_overrides": data["workflow_overrides"],
        "workflow_selections": [
            {
                **selection,
                "template_id": str(selection["template_id"]),
            }
            for selection in data.get("workflow_selections", [])
        ],
        "shots": [
            {
                "id": str(shot.id),
                "number": shot.number,
                "title": shot.title,
                "narrative": shot.narrative,
                "detailed_description": shot.detailed_description,
                "camera": shot.camera,
                "action": shot.action,
                "dialogue": shot.dialogue,
                "sound_effects": shot.sound_effects,
                "music": shot.music,
                "duration_ms": shot.duration_ms,
                "media_type": shot.media_type,
                "h3_mode": shot.h3_mode,
                "include_audio": shot.include_audio,
                "scene_id": str(shot.scene_id) if shot.scene_id else None,
                "character_ids": [str(item.id) for item in shot.characters.all()],
                "prop_ids": [str(item.id) for item in shot.props.all()],
                "reference_asset_ids": [str(item.id) for item in shot.reference_assets.all()],
                "reference_bindings": shot.reference_bindings or {},
                "continuity_notes": shot.continuity_notes,
                "metadata": shot.metadata,
            }
            for shot in shots
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _episode_batch_tasks(*, user, project: SeriesProject, idempotency_key: str) -> list[GenerationTask]:
    queryset = GenerationTask.objects.filter(
        owner=user,
        project=project,
        capability_snapshot__idempotency_key=idempotency_key,
        capability_snapshot__batch_role="child",
    ).select_related(
        "project",
        "episode",
        "generated_workflow__template",
        "generated_workflow__prompt_artifact__episode",
        "generated_workflow__prompt_artifact__shot",
    )
    return sorted(
        queryset,
        key=lambda task: int((task.capability_snapshot or {}).get("batch_index", 0)),
    )


def _production_unit_from_task(task: GenerationTask) -> dict:
    capability = task.capability_snapshot or {}
    workflow = task.generated_workflow
    prompt = workflow.prompt_artifact
    injected = workflow.injected_values or {}
    shot_ids = capability.get("shot_ids") or (prompt.request_options or {}).get("shot_ids") or []
    unit = {
        "batch_id": capability.get("batch_id"),
        "batch_kind": capability.get("batch_kind"),
        "batch_index": capability.get("batch_index"),
        "batch_size": capability.get("batch_size"),
        "episode_id": capability.get("episode_id") or str(task.episode_id),
        "episode_number": capability.get("episode_number"),
        "episode_title": capability.get("episode_title", ""),
        "shot_ids": [str(value) for value in shot_ids],
        "shot_id": capability.get("shot_id"),
        "shot_number": capability.get("shot_number"),
        "shot_order": capability.get("shot_number"),
        "shot_title": capability.get("shot_title", ""),
        "media_type": task.kind,
        "h3_mode": prompt.h3_mode if task.kind == Shot.MediaType.VIDEO else None,
        "effective_conditioning_mode": capability.get("effective_conditioning_mode"),
        "prompt_template_key": capability.get("prompt_template_key"),
        "workflow_mode": capability.get("workflow_mode"),
        "source_workflow": capability.get("source_workflow"),
        "workflow_parameters": capability.get("workflow_parameters", {}),
        "resolution_profile": capability.get("resolution_profile", "project"),
        "resolution_multiple": capability.get(
            "resolution_multiple",
            int(injected.get("RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT)),
        ),
        "width": capability.get("width", int(injected.get("WIDTH", 0))),
        "height": capability.get("height", int(injected.get("HEIGHT", 0))),
        "template_id": str(workflow.template_id),
        "template_slug": workflow.template.slug,
        "prompt_artifact_id": str(prompt.id),
        "prompt_artifact_reused": bool(
            capability.get(
                "prompt_artifact_reused",
                getattr(prompt, "_production_prompt_reused", False),
            )
        ),
        "workflow_id": str(workflow.id),
        "task_id": str(task.id),
        "task_status": task.status,
    }
    if task.kind == Shot.MediaType.VIDEO:
        unit.update({
            "include_audio": capability.get("include_audio", prompt.include_audio),
            "requested_duration_ms": capability.get(
                "requested_duration_ms", prompt.total_duration_ms,
            ),
            "effective_duration_ms": task.effective_duration_ms,
            "fps": capability.get("fps", int(injected.get("FPS", 0))),
        })
    return unit


def _episode_batch_response(
    *, project: SeriesProject, tasks: list[GenerationTask], idempotency_key: str,
    idempotent_replay: bool,
) -> dict:
    if not tasks:
        raise ValidationError("整集生成批次没有可复用的子任务")
    first = tasks[0].capability_snapshot or {}
    batch_size = int(first.get("batch_size", 0))
    if batch_size != len(tasks) or [
        int((task.capability_snapshot or {}).get("batch_index", 0)) for task in tasks
    ] != list(range(1, batch_size + 1)):
        raise ValidationError({
            "idempotency_key": "检测到不完整的整集生成批次；请使用新的 idempotency_key 重新提交",
        })
    units = [_production_unit_from_task(task) for task in tasks]
    return {
        "project_id": str(project.id),
        "whole_episode": True,
        "batch_id": first.get("batch_id"),
        "batch_kind": "episode",
        "idempotency_key": idempotency_key,
        "idempotent_replay": idempotent_replay,
        "episode_ids": [first.get("episode_id")],
        "episode_number": first.get("episode_number"),
        "episode_title": first.get("episode_title", ""),
        "prompt_artifact_ids": [unit["prompt_artifact_id"] for unit in units],
        "workflow_ids": [unit["workflow_id"] for unit in units],
        "task_ids": [unit["task_id"] for unit in units],
        "counts": {"prompts": len(tasks), "workflows": len(tasks), "tasks": len(tasks)},
        "production_units": units,
        "tasks": GenerationTaskSerializer(tasks, many=True).data,
    }


def _prompt_language_gate_payload(prompt: PromptArtifact, **context) -> dict | None:
    """Return the production gate decision without blocking preview-only work."""

    report = evaluate_prompt_language_gate(prompt)
    if not report.applies:
        return None
    return {
        **report.as_dict(),
        **{key: value for key, value in context.items() if value is not None},
    }


def _require_prompt_language_gate_for_enqueue(
    prompt: PromptArtifact, **context,
) -> dict | None:
    """Fail closed at every API entry point that can create queued GPU work."""

    payload = _prompt_language_gate_payload(prompt, **context)
    if payload and payload["would_block_enqueue"]:
        raise ValidationError({"prompt_language_gate": payload})
    return payload


_BIBLE_SCALAR_FIELDS = {
    "characters": (
        "slug", "name", "description", "visual_identity", "negative_constraints", "metadata",
        "is_locked", "stable_speaker_id", "role", "age_appearance", "face_identity", "wardrobe",
        "voice_profile", "personality",
    ),
    "scenes": (
        "slug", "name", "description", "visual_identity", "negative_constraints", "metadata",
        "is_locked", "location", "time_of_day", "lighting", "color_palette", "ambient_sound",
    ),
    "props": (
        "slug", "name", "description", "visual_identity", "negative_constraints", "metadata",
        "is_locked", "material", "distinctive_marks", "continuity_state",
    ),
}


def _validate_asset_access(user, assets) -> None:
    if user.is_staff:
        return
    if any(asset.owner_id not in (None, user.id) for asset in assets):
        raise ValidationError("不能使用其他用户的素材")


def _validate_bible_asset_access(user, assets, *, expected_kind: str) -> None:
    _validate_asset_access(user, assets)
    for asset in assets:
        if not asset.is_active:
            raise ValidationError(f"素材“{asset.name}”已停用，不能绑定到 Bible")
        if not str(asset.mime_type or "").casefold().startswith("image/"):
            raise ValidationError(f"素材“{asset.name}”不是图片，不能绑定到 Bible")
        if asset.kind == Asset.Kind.REFERENCE:
            category = str(
                (asset.metadata or {}).get("reference_category") or ""
            ).strip().casefold()
            if category != expected_kind:
                raise ValidationError(
                    f"通用素材“{asset.name}”未标注 reference_category={expected_kind}"
                )
        elif asset.kind != expected_kind:
            raise ValidationError(
                f"素材“{asset.name}”类别为 {asset.kind}，不能绑定到 {expected_kind} Bible"
            )


def _locked_assets_in_order(assets) -> list[Asset]:
    ids = [asset.pk for asset in assets]
    found = {
        item.pk: item
        for item in Asset.objects.select_for_update().select_related("style").filter(pk__in=ids)
    }
    if len(found) != len(ids):
        raise ValidationError("绑定素材已被删除，请刷新后重试")
    return [found[asset_id] for asset_id in ids]


def _validate_asset_draft_provenance(
    *, user, entity_type: str, data: dict, assets: list[Asset],
) -> None:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return
    provenance = metadata.get("asset_draft_provenance")
    if provenance is None:
        return
    if not isinstance(provenance, dict):
        raise ValidationError({"metadata": "asset_draft_provenance 必须是对象"})
    required = {
        "evidence_basis", "pixel_vision_used", "reference_mode", "entity_type",
        "asset_id", "asset_checksum", "asset_updated_at", "asset_metadata_sha256",
        "source_metadata_sha256",
    }
    if not required.issubset(provenance):
        raise ValidationError({
            "metadata": "asset_draft_provenance 缺少完整素材快照，请重新选择参考图。",
        })
    if (
        provenance.get("evidence_basis") != "asset_metadata"
        or provenance.get("pixel_vision_used") is not False
        or provenance.get("reference_mode") != "single_reference"
        or provenance.get("entity_type") != entity_type
    ):
        raise ValidationError({"metadata": "asset_draft_provenance 模式或类别无效"})
    asset_id = str(provenance.get("asset_id") or "")
    matching = [asset for asset in assets if str(asset.id) == asset_id]
    if len(matching) != 1:
        raise ValidationError({
            "metadata": "草稿来源素材必须仍绑定为该 Bible 的参考素材。",
        })
    asset = matching[0]
    _validate_bible_asset_access(user, [asset], expected_kind=entity_type)
    from studio.services.asset_bible_drafts import _snapshot_hash

    current_hash = _snapshot_hash([asset])
    if {
        str(provenance.get("asset_metadata_sha256") or "").casefold(),
        str(provenance.get("source_metadata_sha256") or "").casefold(),
    } != {current_hash}:
        raise ValidationError({
            "metadata": "参考素材的名称、描述、视觉锚点或风格已变化；请重新生成设定草稿。",
        })
    if str(provenance.get("asset_checksum") or "").casefold() != str(asset.checksum or "").casefold():
        raise ValidationError({"metadata": "参考素材文件哈希已变化；请刷新素材库。"})
    try:
        provided_updated_at = parse_datetime(str(provenance.get("asset_updated_at") or ""))
    except (TypeError, ValueError, OverflowError):
        provided_updated_at = None
    if provided_updated_at is None or provided_updated_at != asset.updated_at:
        raise ValidationError({"metadata": "参考素材版本已变化；请重新生成设定草稿。"})


def _validate_final_bible_names(project: SeriesProject, data: dict) -> None:
    final_names: dict[str, str] = {}
    model_by_key = {
        "characters": CharacterBible, "scenes": SceneBible, "props": PropBible,
    }
    for key, model in model_by_key.items():
        rows = data[key] if key in data else list(
            model.objects.filter(project=project).values("name")
        )
        for row in rows:
            name = str(row.get("name") or "")
            normalized = unicodedata.normalize("NFKC", name).strip().casefold()
            previous = final_names.get(normalized)
            if normalized and previous is not None:
                raise ValidationError({
                    key: f"名称“{name}”与 {previous} 重复；项目内人物、场景和道具名称必须唯一。",
                })
            if normalized:
                final_names[normalized] = f"{key}/{name}"


def _temporary_speaker_ids(existing, desired: set[str]) -> dict:
    forbidden = {item.stable_speaker_id for item in existing} | desired
    result = {}
    candidate = 90_000_000_000_000
    for item in existing:
        value = f"S{candidate}"
        while value in forbidden:
            candidate += 1
            value = f"S{candidate}"
        result[item.pk] = value
        forbidden.add(value)
        candidate += 1
    return result


def _sync_bible_collection(*, project, user, key: str, items: list[dict]):
    model = {"characters": CharacterBible, "scenes": SceneBible, "props": PropBible}[key]
    existing = list(model.objects.select_for_update().filter(project=project).prefetch_related("reference_assets"))
    existing_by_id = {item.pk: item for item in existing}
    requested_ids = {item["id"] for item in items if item.get("id")}
    unknown_ids = requested_ids - set(existing_by_id)
    if unknown_ids:
        raise ValidationError({key: f"以下 id 不属于该项目: {', '.join(sorted(map(str, unknown_ids)))}"})

    delete_ids = set(existing_by_id) - requested_ids
    deleted_count = len(delete_ids)
    if delete_ids:
        model.objects.filter(pk__in=delete_ids).delete()
    retained = [item for item in existing if item.pk in requested_ids]

    # Move all retained unique keys out of the way before applying the final
    # snapshot. This makes slug/speaker swaps safe on every supported database.
    for instance in retained:
        model.objects.filter(pk=instance.pk).update(slug=f"tmp-{uuid.uuid4().hex}")
    if key == "characters":
        desired_speakers = {item["stable_speaker_id"] for item in items}
        for instance_id, temp_value in _temporary_speaker_ids(retained, desired_speakers).items():
            model.objects.filter(pk=instance_id).update(stable_speaker_id=temp_value)

    created_count = 0
    saved = []
    expected_kind = {"characters": "character", "scenes": "scene", "props": "prop"}[key]
    for data in items:
        instance = existing_by_id.get(data.get("id"))
        is_new_instance = instance is None
        previous_metadata = dict(getattr(instance, "metadata", {}) or {}) if instance else {}
        previous_provenance = previous_metadata.get("asset_draft_provenance")
        if instance is None:
            instance = model(project=project)
            created_count += 1
        for field in _BIBLE_SCALAR_FIELDS[key]:
            if field in data:
                setattr(instance, field, data[field])

        references_supplied = "reference_assets" in data
        references = list(data.get("reference_assets", [])) if references_supplied else None
        if references_supplied:
            references = _locked_assets_in_order(references)
            _validate_bible_asset_access(user, references, expected_kind=expected_kind)
            # The first ordered reference is the canonical identity anchor.
            instance.anchor_asset = references[0] if references else None
        elif "anchor_asset" in data:
            anchor = data["anchor_asset"]
            anchor = _locked_assets_in_order([anchor])[0] if anchor else None
            _validate_bible_asset_access(
                user, [anchor] if anchor else [], expected_kind=expected_kind,
            )
            instance.anchor_asset = anchor
        provenance_assets = references if references_supplied else (
            _locked_assets_in_order([instance.anchor_asset]) if instance.anchor_asset else []
        )
        current_provenance = previous_provenance
        if "metadata" in data:
            current_provenance = (
                (data.get("metadata") or {}).get("asset_draft_provenance")
                if isinstance(data.get("metadata"), dict) else None
            )
        from studio.services.asset_bible_drafts import provenance_snapshots_equal

        if previous_provenance is not None and not provenance_snapshots_equal(
            current_provenance, previous_provenance,
        ):
            raise ValidationError({
                "metadata": "已应用草稿的 asset_draft_provenance 是不可修改的历史审计快照。",
            })
        if previous_provenance is not None and "metadata" in data:
            instance.metadata = {
                **(instance.metadata or {}),
                "asset_draft_provenance": previous_provenance,
            }
        # First application is checked against the locked live asset. After
        # that, an unchanged provenance block is immutable audit history; a
        # later source edit must not make every ordinary project save fail.
        if is_new_instance or (
            previous_provenance is None and current_provenance is not None
        ):
            _validate_asset_draft_provenance(
                user=user, entity_type=expected_kind, data=data, assets=provenance_assets,
            )
        instance.save()
        if references_supplied:
            instance.reference_assets.set(references)
        saved.append(instance)
    return saved, {"created": created_count, "updated": len(saved) - created_count, "deleted": deleted_count}


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({"status": "ok", "service": "autoprompt-studio", "version": "1.0.0"})


_SINGLE_BYTE_RANGE = re.compile(r"bytes=(\d*)-(\d*)\Z", re.IGNORECASE)


def _iter_file_range(path: Path, start: int, length: int, chunk_size: int = 64 * 1024):
    handle = path.open("rb")
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _range_file_response(path: Path, request, *, content_type: str, as_attachment: bool):
    """Serve one RFC 7233 byte range so browser video seeking stays efficient."""

    size = path.stat().st_size
    range_header = str(request.headers.get("Range", "")).strip()
    if not range_header:
        response = FileResponse(
            path.open("rb"), content_type=content_type,
            as_attachment=as_attachment, filename=path.name,
        )
        response["Accept-Ranges"] = "bytes"
        return response

    match = _SINGLE_BYTE_RANGE.fullmatch(range_header)
    if not match or not size:
        response = HttpResponse(status=416)
        response["Accept-Ranges"] = "bytes"
        response["Content-Range"] = f"bytes */{size}"
        return response

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        response = HttpResponse(status=416)
        response["Accept-Ranges"] = "bytes"
        response["Content-Range"] = f"bytes */{size}"
        return response
    if start_text:
        start = int(start_text)
        end = min(int(end_text), size - 1) if end_text else size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            response = HttpResponse(status=416)
            response["Accept-Ranges"] = "bytes"
            response["Content-Range"] = f"bytes */{size}"
            return response
        start = max(0, size - suffix_length)
        end = size - 1
    if start >= size or end < start:
        response = HttpResponse(status=416)
        response["Accept-Ranges"] = "bytes"
        response["Content-Range"] = f"bytes */{size}"
        return response

    length = end - start + 1
    response = StreamingHttpResponse(
        _iter_file_range(path, start, length), status=206, content_type=content_type,
    )
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(length)
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Disposition"] = content_disposition_header(as_attachment, path.name)
    return response


class SignedMediaView(APIView):
    """Short-lived signed access for browser img/video elements without Bearer headers."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(exclude=True)
    def get(self, request, token: str):
        provenance_url = ""
        verified_manifest = None
        try:
            payload = load_signed_media_token(
                token, max_age=settings.MEDIA_SIGNED_URL_MAX_AGE_SECONDS,
            )
            purpose = payload.get("purpose")
            object_id = payload.get("object_id")
            owner_id = payload.get("owner_id")
            media_root = Path(settings.MEDIA_ROOT).resolve()
            if purpose == "asset":
                if owner_id:
                    asset = Asset.objects.get(pk=object_id, owner_id=owner_id, is_active=True)
                else:
                    asset = Asset.objects.get(pk=object_id, owner__isnull=True, is_active=True)
                if asset.file:
                    path = Path(asset.file.path).resolve()
                    allowed_root = (media_root / "assets" / str(owner_id)).resolve()
                elif asset.local_path:
                    path = Path(asset.local_path).resolve()
                    allowed_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
                else:
                    raise ValueError("asset has no locally served file")
            elif purpose == "assembly":
                job = AssemblyJob.objects.get(
                    pk=object_id, owner_id=owner_id, status=AssemblyJob.Status.SUCCEEDED,
                )
                path = Path(job.output_path).resolve()
                allowed_root = (
                    media_root / "assemblies" / str(owner_id) / str(job.project_id)
                ).resolve()
                provenance_token = signed_media_token(
                    purpose="assembly_provenance", object_id=job.id, owner_id=job.owner_id,
                )
                provenance_url = reverse(
                    "signed-media", kwargs={"token": provenance_token},
                )
                if not assembly_manifest_path(job).is_file():
                    persist_assembly_provenance(job, generated_at=job.finished_at)
                # Preview and download must both fail closed if either the
                # signed sidecar or media bytes no longer match.  Verification
                # uses a stat-keyed digest cache, so byte-range playback does
                # not re-hash an unchanged large video for every seek.
                verified_manifest = verified_assembly_manifest(job)
            elif purpose == "task_output":
                task = GenerationTask.objects.get(
                    pk=object_id, owner_id=owner_id, status=GenerationTask.Status.SUCCEEDED,
                )
                output_index = payload.get("output_index")
                if isinstance(output_index, bool) or not isinstance(output_index, int):
                    raise ValueError("invalid task output index")
                if not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
                    raise ValueError("task output index is unavailable")
                output = task.outputs[output_index]
                if not classify_task_output(output):
                    raise ValueError("task output is not browser media")
                filename = output.get("filename")
                subfolder = output.get("subfolder", "")
                if not isinstance(filename, str) or not isinstance(subfolder, str):
                    raise ValueError("task output path is malformed")
                configured_root = str(settings.COMFYUI_OUTPUT_DIR or "").strip()
                if not configured_root:
                    raise ValueError("ComfyUI output directory is not configured")
                allowed_root = Path(configured_root).resolve()
                path = (allowed_root / subfolder / filename).resolve()
                provenance_token = signed_media_token(
                    purpose="task_provenance", object_id=task.id, owner_id=task.owner_id,
                    claims={"output_index": output_index},
                )
                provenance_url = reverse(
                    "signed-media", kwargs={"token": provenance_token},
                )
                provenance = output.get("provenance") if isinstance(output, dict) else None
                if not isinstance(provenance, dict):
                    persist_task_provenance(
                        task, list(task.outputs or []), generated_at=task.finished_at,
                    )
                verified_manifest = verified_task_manifest(task, output_index)
                if (
                    str(request.query_params.get("download", "")).casefold()
                    in {"1", "true", "yes", "on"}
                    and str(getattr(settings, "DEPLOYMENT_PROFILE", "development")).casefold()
                    == "production"
                ):
                    raise ValueError("production masters are preview-only")
            elif purpose == "task_distribution":
                task = GenerationTask.objects.get(
                    pk=object_id, owner_id=owner_id, status=GenerationTask.Status.SUCCEEDED,
                )
                output_index = payload.get("output_index")
                if isinstance(output_index, bool) or not isinstance(output_index, int):
                    raise ValueError("invalid task output index")
                verified_manifest = verified_distribution_manifest(task, output_index)
                path = distribution_artifact_path(task, output_index)
                allowed_root = (
                    media_root / "distributions" / str(owner_id) / "tasks" / str(task.id)
                ).resolve()
                provenance_token = signed_media_token(
                    purpose="task_distribution_provenance",
                    object_id=task.id,
                    owner_id=task.owner_id,
                    claims={"output_index": output_index},
                )
                provenance_url = reverse(
                    "signed-media", kwargs={"token": provenance_token},
                )
            elif purpose == "task_provenance":
                task = GenerationTask.objects.get(
                    pk=object_id, owner_id=owner_id, status=GenerationTask.Status.SUCCEEDED,
                )
                output_index = payload.get("output_index")
                if isinstance(output_index, bool) or not isinstance(output_index, int):
                    raise ValueError("invalid task output index")
                document = verified_task_manifest(task, output_index)
                response = JsonResponse(
                    document, json_dumps_params={"ensure_ascii": False},
                )
                response["Cache-Control"] = "private, no-store"
                response["X-AI-Generated"] = "true"
                response["X-Content-Type-Options"] = "nosniff"
                response["Content-Security-Policy"] = "default-src 'none'"
                return response
            elif purpose == "task_distribution_provenance":
                task = GenerationTask.objects.get(
                    pk=object_id, owner_id=owner_id, status=GenerationTask.Status.SUCCEEDED,
                )
                output_index = payload.get("output_index")
                if isinstance(output_index, bool) or not isinstance(output_index, int):
                    raise ValueError("invalid task output index")
                document = verified_distribution_manifest(task, output_index)
                response = JsonResponse(
                    document, json_dumps_params={"ensure_ascii": False},
                )
                response["Cache-Control"] = "private, no-store"
                response["X-AI-Generated"] = "true"
                response["X-Managed-Distribution"] = "true"
                response["X-Content-Type-Options"] = "nosniff"
                response["Content-Security-Policy"] = "default-src 'none'"
                return response
            elif purpose == "assembly_provenance":
                job = AssemblyJob.objects.get(
                    pk=object_id, owner_id=owner_id, status=AssemblyJob.Status.SUCCEEDED,
                )
                document = verified_assembly_manifest(job)
                response = JsonResponse(
                    document, json_dumps_params={"ensure_ascii": False},
                )
                response["Cache-Control"] = "private, no-store"
                response["X-AI-Generated"] = "true"
                response["X-Content-Type-Options"] = "nosniff"
                response["Content-Security-Policy"] = "default-src 'none'"
                return response
            else:
                raise ValueError("unknown media purpose")
            if not path.is_relative_to(allowed_root) or not path.is_file():
                raise ValueError("media path is unavailable")
        except Exception as exc:
            raise NotFound("媒体链接无效或已过期") from exc
        response = _range_file_response(
            path,
            request,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            as_attachment=(
                (
                    purpose in {"assembly", "task_distribution"}
                    or (
                        purpose == "task_output"
                        and str(getattr(settings, "DEPLOYMENT_PROFILE", "development")).casefold()
                        != "production"
                    )
                )
                and str(request.query_params.get("download", "")).casefold() in {"1", "true", "yes", "on"}
            ),
        )
        response["Cache-Control"] = "private, max-age=60, no-transform"
        response["X-Content-Type-Options"] = "nosniff"
        if purpose in {"assembly", "task_output", "task_distribution"}:
            response["X-AI-Generated"] = "true"
            if purpose == "task_distribution":
                response["X-Managed-Distribution"] = "true"
                response["X-Distribution-Version"] = str(
                    (verified_manifest.get("disclosure") or {}).get("version") or ""
                )
                response["X-Source-Artifact-SHA256"] = str(
                    (verified_manifest.get("source_artifact") or {}).get("sha256") or ""
                )
            if provenance_url:
                response["X-Provenance-Manifest"] = provenance_url
            if verified_manifest:
                response["X-Artifact-SHA256"] = str(
                    (verified_manifest.get("artifact") or {}).get("sha256") or ""
                )
        return response


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        self.throttle_scope = "registration"
        return [*super().get_throttles(), ScopedRateThrottle()]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({
            "public_registration_enabled": settings.ALLOW_PUBLIC_REGISTRATION,
        })

    @extend_schema(request=RegisterSerializer, responses={201: UserSerializer})
    def post(self, request):
        if not settings.ALLOW_PUBLIC_REGISTRATION:
            raise PermissionDenied("公开注册已关闭，请联系管理员创建账号")
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=UserSerializer)
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class StyleViewSet(viewsets.ModelViewSet):
    serializer_class = StyleSerializer
    permission_classes = [IsOwnerOrStaffOrGlobalReadOnly]
    filterset_fields = ("category", "is_active", "is_system")
    search_fields = ("name", "slug", "description", "visual_prompt")
    ordering_fields = ("name", "created_at", "updated_at")

    def get_queryset(self):
        queryset = Style.objects.annotate(asset_count=Count("assets", distinct=True)).order_by("name")
        return _owned_or_global(queryset, self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, is_system=False)

class AssetViewSet(viewsets.ModelViewSet):
    serializer_class = AssetSerializer
    permission_classes = [IsOwnerOrStaffOrGlobalReadOnly]
    filterset_fields = ("style", "kind", "is_active")
    search_fields = ("name", "description", "prompt_hint")
    ordering_fields = ("name", "kind", "created_at", "updated_at")

    def get_throttles(self):
        throttles = super().get_throttles()
        if getattr(self, "action", None) in {"create", "update", "partial_update"}:
            self.throttle_scope = "asset_upload"
            throttles.append(ScopedRateThrottle())
        return throttles

    def get_queryset(self):
        return _owned_or_global(Asset.objects.select_related("style", "owner"), self.request.user)

    def perform_create(self, serializer):
        with transaction.atomic():
            User.objects.select_for_update().get(pk=self.request.user.pk)
            serializer.enforce_tenant_quota()
            serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.owner_id is None:
            raise ValidationError(
                "全局内置素材为只读；请通过受审计的素材 manifest/import 流程维护。"
            )
        with transaction.atomic():
            User.objects.select_for_update().get(pk=self.request.user.pk)
            serializer.enforce_tenant_quota()
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            try:
                asset = self.get_queryset().select_for_update().get(pk=kwargs["pk"])
            except Asset.DoesNotExist as exc:
                raise NotFound("素材不存在") from exc
            self.check_object_permissions(request, asset)
            if asset.owner_id is None:
                raise ValidationError(
                    "全局内置素材为只读；请通过受审计的素材 manifest/import 流程维护。"
                )
            if asset_is_release_evidence(asset):
                raise ValidationError("该素材已进入封存或历史发行清单，不能删除；可停用但必须保留审计证据。")
            asset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _translation_scope_id(project: SeriesProject, item: dict):
    return project.id if item["scope_type"] == PromptFieldTranslation.ScopeType.PROJECT else item["scope_id"]


def _save_translation_candidate(
    *, project: SeriesProject, request_user, item: dict,
    result: TranslationResult | PromptTranslationError,
    attempted_provider: str = LOCAL_QWEN_PROVIDER,
) -> PromptFieldTranslation:
    scope_id = _translation_scope_id(project, item)
    source = item["source_text"]
    source_hash = source_text_hash(source)
    with transaction.atomic():
        entry = PromptFieldTranslation.objects.select_for_update().filter(
            project=project,
            scope_type=item["scope_type"],
            scope_id=scope_id,
            field_key=item["field_key"],
        ).first()
        is_new = entry is None
        source_changed = bool(entry is not None and entry.source_hash != source_hash)
        if is_new:
            entry = PromptFieldTranslation(
                project=project,
                scope_type=item["scope_type"],
                scope_id=scope_id,
                field_key=item["field_key"],
                source_text=source,
                source_hash=source_hash,
                revision=1,
            )
        elif isinstance(result, TranslationResult) or source_changed:
            _append_translation_history(entry)
            entry.revision += 1

        if isinstance(result, TranslationResult):
            identity_metadata = {}
            target = _promotion_target(item["field_key"])
            if (
                item["scope_type"] == PromptFieldTranslation.ScopeType.SHOT
                and target and target[0] == "dialogue"
            ):
                dialogue_shot = Shot.objects.select_for_update().get(
                    pk=scope_id, episode__project=project,
                )
                identity_metadata["dialogue_identity"] = _dialogue_identity(
                    dialogue_shot, int(target[1]),
                )
            entry.source_text = source
            entry.source_hash = source_hash
            entry.translated_text = result.text
            entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
            entry.provider = result.provider
            entry.model = result.model
            entry.error_message = ""
            entry.runtime_metadata = {
                **(result.metadata or {}),
                **identity_metadata,
                "translation_context_hash": translation_context_hash(
                    _translation_context(project, item)
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            }
            entry.confirmed_by = None
            entry.confirmed_at = None
        elif is_new or source_changed:
            # A failed attempt for a changed source cannot inherit the old
            # translation. The source itself remains untouched in its owning
            # model; this row records the current value for review/retry.
            entry.source_text = source
            entry.source_hash = source_hash
            entry.translated_text = ""
            entry.status = PromptFieldTranslation.Status.FAILED
            entry.provider = attempted_provider
            entry.model = ""
            entry.error_message = str(result)
            entry.runtime_metadata = {
                **(getattr(result, "metadata", {}) or {}),
                "error_code": getattr(result, "code", "translation_failed"),
                "provider": attempted_provider,
                "loaded_on_demand": attempted_provider == LOCAL_QWEN_PROVIDER,
                "unloaded_after_request": attempted_provider == LOCAL_QWEN_PROVIDER,
            }
            entry.confirmed_by = None
            entry.confirmed_at = None
        else:
            # Preserve the last candidate/confirmation if a retry fails.
            entry.error_message = str(result)
            entry.runtime_metadata = {
                **(entry.runtime_metadata or {}),
                "last_attempt_error": str(result),
                "last_attempt_error_code": getattr(
                    result, "code", "translation_failed",
                ),
                "last_attempt_metadata": getattr(result, "metadata", {}) or {},
                "last_attempt_failed_at": timezone.now().isoformat(),
                "last_attempt_provider": attempted_provider,
                "last_attempt_transport": (
                    "local_process" if attempted_provider == LOCAL_QWEN_PROVIDER else "remote_api"
                ),
                "last_attempt_loaded_on_demand": attempted_provider == LOCAL_QWEN_PROVIDER,
                "last_attempt_unloaded_after_request": attempted_provider == LOCAL_QWEN_PROVIDER,
            }
        entry.save()
    return entry


def _validated_translation_items(project: SeriesProject, items: list[dict]) -> list[dict]:
    validated = []
    for item in items:
        scope_id = _translation_scope_id(project, item)
        try:
            current = resolve_current_source(
                project=project,
                scope_type=item["scope_type"],
                scope_id=scope_id,
                field_key=item["field_key"],
            )
        except TranslationValidationError as exc:
            raise ValidationError({"entries": str(exc)}) from exc
        if item["source_text"] != current:
            raise ValidationError({
                "entries": (
                    f"{item['field_key']} 的 source_text 已过期；请刷新页面后重新翻译"
                ),
            })
        validated.append({**item, "scope_id": scope_id})
    return validated


def _translation_context(project: SeriesProject, item: dict) -> str:
    return build_translation_context(
        project,
        scope_type=item["scope_type"],
        scope_id=item.get("scope_id"),
        field_key=item["field_key"],
    )


class TranslationResourceBusy(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "视频生成任务正在排队或执行；为避免本地翻译与 ComfyUI 同时占用内存，请等待任务完成后再翻译。"
    )
    default_code = "translation_resource_busy"


def _ensure_translation_resources_available() -> None:
    if GenerationTask.objects.filter(status__in=(
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )).exists():
        raise TranslationResourceBusy()


class SeriesProjectViewSet(viewsets.ModelViewSet):
    serializer_class = SeriesProjectSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("status", "style", "target_model")
    search_fields = ("title", "logline", "master_script")
    ordering_fields = ("title", "created_at", "updated_at")

    def get_queryset(self):
        queryset = SeriesProject.objects.select_related("style", "owner").annotate(
            episode_count=Count("episodes", distinct=True),
            character_count=Count("characters", distinct=True),
            scene_count=Count("scenes", distinct=True),
            prop_count=Count("props", distinct=True),
        ).order_by("-updated_at")
        return _owned_projects(queryset, self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @extend_schema(
        request=CanonicalGlossaryEnsureSerializer,
        responses=CanonicalGlossaryEnsureResponseSerializer,
    )
    @action(detail=True, methods=["post"], url_path="ensure-canonical-glossary")
    def ensure_canonical_glossary(self, request, pk=None):
        """Fill missing Bible-name mappings without ever overwriting a value."""
        project = self.get_object()
        serializer = CanonicalGlossaryEnsureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        try:
            result = ensure_project_canonical_glossary(
                project=project,
                selectors=payload.get("entries", []),
                include_all_missing=payload["include_all_missing"],
                ensure_visual_identity_translation=payload[
                    "ensure_visual_identity_translation"
                ],
                user=request.user,
            )
        except CanonicalGlossaryCapacityError as exc:
            raise ValidationError(str(exc)) from exc
        except CanonicalTargetCollisionError as exc:
            raise ValidationError({
                "code": exc.code,
                "collisions": exc.collisions,
            }) from exc
        return Response(result)

    @extend_schema(responses=PromptFieldTranslationSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="translations")
    def translations(self, request, pk=None):
        project = self.get_object()
        queryset = project.prompt_field_translations.all()
        episode_id = request.query_params.get("episode")
        shot_id = request.query_params.get("shot")
        translation_status = request.query_params.get("status")
        if shot_id:
            queryset = queryset.filter(
                Q(scope_type=PromptFieldTranslation.ScopeType.SHOT, scope_id=shot_id)
                | ~Q(scope_type=PromptFieldTranslation.ScopeType.SHOT)
            )
        elif episode_id:
            shot_ids = Shot.objects.filter(
                episode_id=episode_id, episode__project=project,
            ).values_list("id", flat=True)
            queryset = queryset.filter(
                Q(scope_type=PromptFieldTranslation.ScopeType.SHOT, scope_id__in=shot_ids)
                | ~Q(scope_type=PromptFieldTranslation.ScopeType.SHOT)
            )
        if translation_status:
            queryset = queryset.filter(status=translation_status)
        return Response(PromptFieldTranslationSerializer(queryset, many=True).data)

    @extend_schema(request=PromptTranslationBatchRequestSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="translations/translate")
    def translate_prompt_fields(self, request, pk=None):
        project = self.get_object()
        serializer = PromptTranslationBatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data["provider"]
        items = _validated_translation_items(project, data["entries"])
        if provider == LOCAL_QWEN_PROVIDER:
            _ensure_translation_resources_available()
        labels = [item["field_key"].replace(".", " / ") for item in items]
        contexts = [_translation_context(project, item) for item in items]
        try:
            results = translate_texts_once(
                [item["source_text"] for item in items],
                source_lang=data["source_lang"],
                target_lang=data["target_lang"],
                field_keys=[item["field_key"] for item in items],
                labels=labels,
                contexts=contexts,
                provider=provider,
            )
        except PromptTranslationResourceBusy as exc:
            raise TranslationResourceBusy(str(exc)) from exc
        except PromptTranslationError as exc:
            results = [exc for _ in items]
        saved = [
            _save_translation_candidate(
                project=project, request_user=request.user, item=item, result=result,
                attempted_provider=provider,
            )
            for item, result in zip(items, results, strict=True)
        ]
        succeeded = sum(isinstance(result, TranslationResult) for result in results)
        return Response({
            "entries": PromptFieldTranslationSerializer(saved, many=True).data,
            "summary": {
                "requested": len(items), "succeeded": succeeded,
                "failed": len(items) - succeeded,
            },
            "runtime": {
                "provider": provider,
                "transport": "local_process" if provider == LOCAL_QWEN_PROVIDER else "remote_api",
                "loaded_on_demand": provider == LOCAL_QWEN_PROVIDER,
                "unloaded_after_request": provider == LOCAL_QWEN_PROVIDER,
            },
        })

    @action(detail=True, methods=["post"], url_path="sync-locks")
    def sync_locks(self, request, pk=None):
        project = self.get_object()
        locks = sync_consistency_locks(project)
        return Response(ConsistencyLockSerializer(locks, many=True, context={"request": request}).data)

    @extend_schema(request=ProjectBibleSyncSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="sync-bibles")
    def sync_bibles(self, request, pk=None):
        """Apply authoritative character/scene/prop snapshots atomically."""
        project = self.get_object()
        serializer = ProjectBibleSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            project = SeriesProject.objects.select_for_update().get(pk=project.pk)
            _validate_final_bible_names(project, data)
            results = {}
            counts = {}
            serializers_by_key = {
                "characters": CharacterBibleSerializer,
                "scenes": SceneBibleSerializer,
                "props": PropBibleSerializer,
            }
            for key in ("characters", "scenes", "props"):
                if key not in data:
                    continue
                saved, counts[key] = _sync_bible_collection(
                    project=project, user=request.user, key=key, items=data[key],
                )
                results[key] = serializers_by_key[key](
                    saved, many=True, context={"request": request},
                ).data
            sync_consistency_locks(project)
        return Response({"project_id": str(project.id), "counts": counts, **results})

    @action(detail=True, methods=["get"], url_path="continuity")
    def continuity(self, request, pk=None):
        return Response(capture_consistency_snapshot(self.get_object()))

    @action(detail=True, methods=["post"], url_path="next-episode")
    def next_episode(self, request, pk=None):
        project = self.get_object()
        serializer = NextEpisodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        next_number = (project.episodes.aggregate(value=Max("number"))["value"] or 0) + 1
        episode = Episode.objects.create(project=project, number=next_number, **serializer.validated_data)
        return Response(EpisodeSerializer(episode, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ScriptBreakdownSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="breakdown-script")
    def breakdown_script(self, request, pk=None):
        project = self.get_object()
        serializer = ScriptBreakdownSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        script = data["script"].strip() or project.master_script.strip()
        if not script:
            raise ValidationError({"script": "请求 script 与项目 master_script 均为空"})
        if project.episodes.exists() and not data["replace_existing"]:
            raise ValidationError({"replace_existing": "项目已有剧集；若要按完整剧本重建，请显式设为 true"})
        if data["replace_existing"] and (
            project.generation_tasks.exists() or project.assembly_jobs.exists()
        ):
            raise ValidationError("项目已有生成或组装记录，为保护审计链不能自动删除；请新建项目或手工追加剧集")
        try:
            with transaction.atomic():
                project = SeriesProject.objects.select_for_update().get(pk=project.pk)
                if project.episodes.exists() and not data["replace_existing"]:
                    raise ValidationError({"replace_existing": "并发请求期间项目已创建剧集；请刷新后重试"})
                if data["replace_existing"] and (
                    project.generation_tasks.exists() or project.assembly_jobs.exists()
                ):
                    raise ValidationError("并发请求期间项目新增了生成或组装记录，已停止覆盖")
                if data["replace_existing"]:
                    project.episodes.all().delete()
                if data["script"] and data["script"] != project.master_script:
                    project.master_script = data["script"]
                    project.save(update_fields=("master_script", "updated_at"))
                result = breakdown_script(
                    project, script,
                    episode_count_hint=data["episode_count_hint"],
                    default_shot_duration_ms=data["default_shot_duration_ms"],
                    multi_shot=data["multi_shot"],
                    include_audio=data["include_audio"],
                )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        episodes = []
        for episode in result.episodes:
            episodes.append({
                "episode": EpisodeSerializer(episode, context={"request": request}).data,
                "shots": ShotSerializer(
                    episode.shots.select_related("scene").prefetch_related("characters", "props", "reference_assets"),
                    many=True, context={"request": request},
                ).data,
            })
        return Response({
            "project_id": str(project.id), "method": result.method,
            "warnings": result.warnings, "episodes": episodes,
            "counts": {
                "episodes": len(episodes),
                "shots": sum(len(item["shots"]) for item in episodes),
            },
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        project = self.get_object()
        if not project.assembly_jobs.filter(
            scope=AssemblyJob.Scope.SERIES, status=AssemblyJob.Status.SUCCEEDED
        ).exists():
            raise ValidationError("整剧必须先通过 assemblies/series 实际组装并验证输出，不能直接标记完成")
        project.status = SeriesProject.Status.COMPLETED
        project.completed_at = timezone.now()
        project.save(update_fields=("status", "completed_at", "updated_at"))
        return Response(self.get_serializer(project).data)

    @extend_schema(request=ProductionBuildSerializer)
    @action(detail=True, methods=["post"], url_path="build-production")
    def build_production(self, request, pk=None):
        project = self.get_object()
        input_serializer = ProductionBuildSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        whole_episode = data["whole_episode"]
        # Template visibility is broader than execution.  Non-staff may read
        # their legacy entries, but may submit only reviewed global templates.
        available_templates = list(_executable_templates(
            WorkflowTemplate.objects.all(), request.user,
        ))
        preferred_template = None
        if data["template_id"]:
            preferred_template = next(
                (item for item in available_templates if item.pk == data["template_id"]), None,
            )
            if preferred_template is None:
                raise NotFound("工作流模板不存在或已停用")
        selected_workflows = {}
        for selection in data.get("workflow_selections", []):
            selected_template = next(
                (
                    item for item in available_templates
                    if item.pk == selection["template_id"]
                ),
                None,
            )
            if selected_template is None:
                raise NotFound("选择的工作流模板不存在或已停用")
            _select_production_template(
                templates=available_templates,
                preferred=None,
                selected=selected_template,
                output_kind=selection["output_kind"],
                h3_mode=selection["h3_mode"],
            )
            allowed_parameters = set(
                ((selected_template.input_schema or {}).get("user_parameters") or {})
            )
            unknown_parameters = set(selection["parameters"]) - allowed_parameters
            if unknown_parameters:
                raise ValidationError({
                    "workflow_selections": (
                        f"工作流 {selected_template.name} 不开放参数: "
                        f"{', '.join(sorted(unknown_parameters))}"
                    ),
                })
            selected_workflows[(selection["output_kind"], selection["h3_mode"])] = {
                "template": selected_template,
                "parameters": selection["parameters"],
            }

        generated_prompts = []
        workflows = []
        tasks = []
        production_units = []
        prompt_language_gate_reports = []
        used_workflow_selections = set()
        with transaction.atomic():
            project = SeriesProject.objects.select_for_update().select_related("style").get(pk=project.pk)
            try:
                profile_width, profile_height = resolve_production_resolution(
                    profile=data["resolution_profile"],
                    aspect_ratio=project.aspect_ratio,
                    project_width=project.width,
                    project_height=project.height,
                    multiple=data["resolution_multiple"],
                )
            except ResolutionProfileError as exc:
                raise ValidationError({"resolution_profile": str(exc)}) from exc
            try:
                image_profile_width, image_profile_height = resolve_production_resolution(
                    profile=data["image_resolution_profile"],
                    aspect_ratio=project.aspect_ratio,
                    project_width=project.width,
                    project_height=project.height,
                    multiple=data["image_resolution_multiple"],
                )
            except ResolutionProfileError as exc:
                raise ValidationError({"image_resolution_profile": str(exc)}) from exc

            def media_workflow_overrides(*, profile, multiple, width, height):
                resolved = dict(data["workflow_overrides"])
                resolved.update({
                    "RESOLUTION_PROFILE": profile,
                    "RESOLUTION_MULTIPLE": multiple,
                })
                if profile != "project":
                    resolved.update({"WIDTH": width, "HEIGHT": height})
                else:
                    # Project-size mode keeps explicit operator dimensions,
                    # normalized to the chosen media-specific multiple.
                    resolved.setdefault("WIDTH", width)
                    resolved.setdefault("HEIGHT", height)
                return resolved

            video_workflow_overrides = media_workflow_overrides(
                profile=data["resolution_profile"],
                multiple=data["resolution_multiple"],
                width=profile_width,
                height=profile_height,
            )
            image_workflow_overrides = media_workflow_overrides(
                profile=data["image_resolution_profile"],
                multiple=data["image_resolution_multiple"],
                width=image_profile_width,
                height=image_profile_height,
            )
            sync_consistency_locks(project)
            episodes = project.episodes.prefetch_related("shots").order_by("number")
            if data["episode_ids"]:
                requested_episode_ids = set(data["episode_ids"])
                episodes = episodes.filter(pk__in=requested_episode_ids)
                found_episode_ids = set(episodes.values_list("pk", flat=True))
                missing_episode_ids = requested_episode_ids - found_episode_ids
                if missing_episode_ids:
                    raise ValidationError({
                        "episode_ids": (
                            "以下剧集不属于该项目: "
                            f"{', '.join(sorted(map(str, missing_episode_ids)))}"
                        ),
                    })
            requested_shot_ids = set(data["shot_ids"])
            if requested_shot_ids:
                scoped_shots = Shot.objects.filter(
                    episode__project=project, pk__in=requested_shot_ids,
                )
                if data["episode_ids"]:
                    scoped_shots = scoped_shots.filter(episode_id__in=data["episode_ids"])
                found_shot_ids = set(scoped_shots.values_list("pk", flat=True))
                missing_shot_ids = requested_shot_ids - found_shot_ids
                if missing_shot_ids:
                    raise ValidationError({
                        "shot_ids": (
                            "以下分镜不属于该项目或所选剧集: "
                            f"{', '.join(sorted(map(str, missing_shot_ids)))}"
                        ),
                    })
                selected_episode_ids = scoped_shots.values_list("episode_id", flat=True)
                episodes = episodes.filter(pk__in=selected_episode_ids)
            episodes = list(episodes)
            whole_episode_shots: list[Shot] | None = None
            batch_id = None
            batch_fingerprint = None
            batch_size = 0
            batch_consistency_snapshot = None
            if whole_episode:
                # The serializer guarantees exactly one requested id; this
                # second check provides an ownership-aware error after lookup.
                if len(episodes) != 1:
                    raise ValidationError({"episode_ids": "整集生成目标剧集不存在或不属于该项目"})
                batch_episode = episodes[0]
                whole_episode_shots = load_shots([
                    str(value) for value in batch_episode.shots.order_by("number").values_list("id", flat=True)
                ])
                if not whole_episode_shots:
                    raise ValidationError("整集生成目标剧集至少需要一个分镜")
                batch_consistency_snapshot = capture_consistency_snapshot(project)
                batch_fingerprint = _episode_batch_fingerprint(
                    project=project,
                    episode=batch_episode,
                    shots=whole_episode_shots,
                    data=data,
                    consistency_snapshot=batch_consistency_snapshot,
                )
                existing_tasks = _episode_batch_tasks(
                    user=request.user, project=project,
                    idempotency_key=data["idempotency_key"],
                )
                if existing_tasks:
                    fingerprints = {
                        (task.capability_snapshot or {}).get("request_fingerprint")
                        for task in existing_tasks
                    }
                    if fingerprints != {batch_fingerprint}:
                        raise ValidationError({
                            "idempotency_key": (
                                "该 idempotency_key 已用于不同的整集生成请求；"
                                "修改分镜或参数后必须使用新的键"
                            ),
                        })
                    return Response(
                        _episode_batch_response(
                            project=project,
                            tasks=existing_tasks,
                            idempotency_key=data["idempotency_key"],
                            idempotent_replay=True,
                        ),
                        status=status.HTTP_200_OK,
                    )
                batch_id = str(uuid.uuid4())
                batch_size = len(whole_episode_shots)
                # Reserve the complete batch under the queue-capacity mutex.
                # No prompt/workflow/task is persisted if this preflight fails.
                _assert_queue_capacity(request.user, additional=batch_size)
            batch_index = 0
            for episode in episodes:
                if whole_episode:
                    shots = whole_episode_shots or []
                else:
                    episode_shots = episode.shots.order_by("number")
                    if requested_shot_ids:
                        episode_shots = episode_shots.filter(pk__in=requested_shot_ids)
                    shots = load_shots([str(item.id) for item in episode_shots])
                if not shots:
                    continue
                try:
                    groups = _production_groups(
                        episode,
                        shots,
                        data["multi_shot"],
                        force_single_shot=whole_episode,
                    )
                except PromptBuildError as exc:
                    raise ValidationError(str(exc)) from exc
                for group in groups:
                    first_shot = group[0]
                    output_kind = first_shot.media_type
                    matched_selection = selected_workflows.get(
                        (output_kind, first_shot.h3_mode),
                    )
                    if matched_selection:
                        used_workflow_selections.add((output_kind, first_shot.h3_mode))
                    template = _select_production_template(
                        templates=available_templates, preferred=preferred_template,
                        selected=(matched_selection or {}).get("template"),
                        output_kind=output_kind, h3_mode=first_shot.h3_mode,
                    )
                    try:
                        prompts = _create_prompt_artifacts(
                            request.user, project, episode, group,
                            prompt_type=output_kind, mode=first_shot.h3_mode,
                            multi_shot=len(group) > 1,
                            include_audio=(
                                first_shot.include_audio
                                if output_kind == PromptArtifact.PromptType.VIDEO else False
                            ),
                            duration_overrides={},
                            consistency_snapshot=(
                                batch_consistency_snapshot if whole_episode else None
                            ),
                            reuse_latest_unchanged=True,
                        )
                        prompt = prompts[0]
                        language_gate_payload = _prompt_language_gate_payload(
                            prompt,
                            episode_id=str(episode.id),
                            episode_number=episode.number,
                            shot_ids=[str(item.id) for item in group],
                        )
                        if language_gate_payload:
                            prompt_language_gate_reports.append(language_gate_payload)
                            if data["enqueue"] and language_gate_payload["would_block_enqueue"]:
                                raise ValidationError({"prompt_language_gate": language_gate_payload})
                        if output_kind == PromptArtifact.PromptType.IMAGE and template.model:
                            prompt.model = template.model
                            prompt.save(update_fields=("model", "updated_at"))
                        selected_overrides = dict(
                            image_workflow_overrides
                            if output_kind == PromptArtifact.PromptType.IMAGE
                            else video_workflow_overrides
                        )
                        if matched_selection:
                            selected_overrides.update(matched_selection["parameters"])
                        workflow_json, injected, content_hash = build_workflow(
                            template,
                            prompt,
                            project,
                            selected_overrides,
                        )
                    except (PromptBuildError, WorkflowBuildError) as exc:
                        raise ValidationError(str(exc)) from exc
                    generated_prompts.append(prompt)
                    workflow = GeneratedWorkflow.objects.create(
                        project=project, episode=episode, prompt_artifact=prompt, template=template,
                        created_by=request.user, workflow_json=workflow_json,
                        injected_values=injected, content_hash=content_hash,
                    )
                    workflows.append(workflow)
                    task = None
                    if data["enqueue"]:
                        capability_overrides = None
                        if whole_episode:
                            batch_index += 1
                            if len(group) != 1:
                                raise ValidationError(
                                    "整集生成内部契约错误：每个子任务必须只包含一个分镜"
                                )
                            child_shot = group[0]
                            capability_overrides = {
                                "batch_id": batch_id,
                                "batch_kind": "episode",
                                "batch_role": "child",
                                "batch_index": batch_index,
                                "batch_size": batch_size,
                                "idempotency_key": data["idempotency_key"],
                                "request_fingerprint": batch_fingerprint,
                                "episode_id": str(episode.id),
                                "episode_number": episode.number,
                                "episode_title": episode.title,
                                "shot_ids": [str(child_shot.id)],
                                "shot_id": str(child_shot.id),
                                "shot_number": child_shot.number,
                                "shot_title": child_shot.title,
                                "prompt_artifact_reused": bool(
                                    getattr(prompt, "_production_prompt_reused", False)
                                ),
                                **({
                                    "include_audio": child_shot.include_audio,
                                    "requested_duration_ms": child_shot.duration_ms,
                                } if output_kind == Shot.MediaType.VIDEO else {}),
                            }
                        else:
                            _assert_queue_capacity(request.user)
                        task = _create_generation_task(
                            user=request.user, workflow=workflow,
                            priority=data["priority"], max_attempts=data["max_attempts"],
                            capability_snapshot_overrides=capability_overrides,
                        )
                        tasks.append(task)
                    if whole_episode:
                        production_units.append(_production_unit_from_task(task))
                    else:
                        one_shot = group[0] if len(group) == 1 else None
                        production_units.append({
                            "episode_id": str(episode.id),
                            "episode_number": episode.number,
                            "episode_title": episode.title,
                            "shot_ids": [str(item.id) for item in group],
                            "shot_id": str(one_shot.id) if one_shot else None,
                            "shot_number": one_shot.number if one_shot else None,
                            "shot_order": one_shot.number if one_shot else None,
                            "shot_title": one_shot.title if one_shot else "",
                            "media_type": output_kind,
                            "h3_mode": first_shot.h3_mode if output_kind == Shot.MediaType.VIDEO else None,
                            "effective_conditioning_mode": injected.get("EFFECTIVE_CONDITIONING_MODE"),
                            "prompt_template_key": injected.get("PROMPT_TEMPLATE_KEY"),
                            "workflow_mode": injected.get("WORKFLOW_MODE"),
                            "source_workflow": injected.get("SOURCE_WORKFLOW"),
                            "workflow_parameters": {
                                key: injected.get(key, definition.get("default"))
                                for key, definition in (
                                    (template.input_schema or {}).get("user_parameters") or {}
                                ).items()
                                if isinstance(key, str) and isinstance(definition, dict)
                            },
                            "resolution_profile": injected["RESOLUTION_PROFILE"],
                            "resolution_multiple": injected["RESOLUTION_MULTIPLE"],
                            "width": injected.get("WIDTH", 0),
                            "height": injected.get("HEIGHT", 0),
                            **({
                                "include_audio": first_shot.include_audio,
                                "requested_duration_ms": sum(item.duration_ms for item in group),
                                "effective_duration_ms": injected.get(
                                    "EFFECTIVE_DURATION_MS", 0,
                                ),
                                "fps": injected.get("FPS", 0),
                            } if output_kind == Shot.MediaType.VIDEO else {}),
                            "template_id": str(template.id),
                            "template_slug": template.slug,
                            "prompt_artifact_id": str(prompt.id),
                            "prompt_artifact_reused": bool(
                                getattr(prompt, "_production_prompt_reused", False)
                            ),
                            "workflow_id": str(workflow.id),
                            "task_id": str(task.id) if task else None,
                        })
                target_status = (
                    Episode.Status.GENERATING if data["enqueue"] else Episode.Status.READY
                )
                if episode.status != target_status:
                    episode.status = target_status
                    episode.save(update_fields=("status", "updated_at"))
            unused_selections = set(selected_workflows) - used_workflow_selections
            if unused_selections:
                formatted = ", ".join(
                    f"{kind}/{mode}" for kind, mode in sorted(unused_selections)
                )
                raise ValidationError({
                    "workflow_selections": f"所选生产范围没有可应用的镜头: {formatted}",
                })
            if production_units and (data["enqueue"] or project.status == SeriesProject.Status.COMPLETED):
                project.status = SeriesProject.Status.ACTIVE
                project.completed_at = None
                project.save(update_fields=("status", "completed_at", "updated_at"))
        if whole_episode:
            response_payload = _episode_batch_response(
                project=project,
                tasks=tasks,
                idempotency_key=data["idempotency_key"],
                idempotent_replay=False,
            )
            response_payload["prompt_language_gate"] = prompt_language_gate_reports
            return Response(response_payload, status=status.HTTP_201_CREATED)
        return Response({
            "project_id": str(project.id),
            "episode_ids": [str(item.id) for item in episodes],
            "prompt_artifact_ids": [str(item.id) for item in generated_prompts],
            "workflow_ids": [str(item.id) for item in workflows],
            "task_ids": [str(item.id) for item in tasks],
            "counts": {"prompts": len(generated_prompts), "workflows": len(workflows), "tasks": len(tasks)},
            "production_units": production_units,
            "prompt_language_gate": prompt_language_gate_reports,
        }, status=status.HTTP_201_CREATED)


class ProjectChildViewSet(viewsets.ModelViewSet):
    permission_classes = [IsOwnerOrStaff]

    def owned(self, queryset):
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)


class CharacterBibleViewSet(ProjectChildViewSet):
    serializer_class = CharacterBibleSerializer
    filterset_fields = ("project", "is_locked", "stable_speaker_id")
    search_fields = ("name", "role", "visual_identity", "face_identity")

    def get_queryset(self):
        return self.owned(CharacterBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class SceneBibleViewSet(ProjectChildViewSet):
    serializer_class = SceneBibleSerializer
    filterset_fields = ("project", "is_locked", "time_of_day")
    search_fields = ("name", "location", "visual_identity")

    def get_queryset(self):
        return self.owned(SceneBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class PropBibleViewSet(ProjectChildViewSet):
    serializer_class = PropBibleSerializer
    filterset_fields = ("project", "is_locked")
    search_fields = ("name", "visual_identity", "distinctive_marks")

    def get_queryset(self):
        return self.owned(PropBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class ConsistencyLockViewSet(ProjectChildViewSet):
    serializer_class = ConsistencyLockSerializer
    filterset_fields = ("project", "scope", "target_id", "is_active")
    search_fields = ("lock_key",)

    def get_queryset(self):
        return self.owned(ConsistencyLock.objects.select_related("project"))

    def perform_update(self, serializer):
        serializer.save(version=serializer.instance.version + 1)


_SHOT_SCALAR_FIELDS = (
    "title", "narrative", "detailed_description", "camera", "action", "dialogue", "sound_effects", "music",
    "duration_ms", "media_type", "h3_mode", "include_audio", "scene", "reference_bindings",
    "continuity_notes", "metadata",
)


def _sync_episode_shots(*, episode: Episode, user, items: list[dict]):
    existing = list(Shot.objects.select_for_update().filter(episode=episode).prefetch_related(
        "characters", "props", "reference_assets",
    ))
    existing_by_id = {item.pk: item for item in existing}
    requested_ids = {item["id"] for item in items if item.get("id")}
    unknown_ids = requested_ids - set(existing_by_id)
    if unknown_ids:
        raise ValidationError({
            "shots": f"以下 id 不属于该剧集: {', '.join(sorted(map(str, unknown_ids)))}",
        })

    delete_ids = set(existing_by_id) - requested_ids
    deleted_count = len(delete_ids)
    if delete_ids:
        Shot.objects.filter(pk__in=delete_ids).delete()
    retained = [item for item in existing if item.pk in requested_ids]
    final_numbers = [item["number"] for item in items]
    temp_base = max([item.number for item in retained] + final_numbers + [0]) + len(retained) + 1000
    for offset, instance in enumerate(retained):
        Shot.objects.filter(pk=instance.pk).update(number=temp_base + offset)

    created_count = 0
    saved = []
    for data in items:
        instance = existing_by_id.get(data.get("id"))
        if instance is None:
            instance = Shot(episode=episode)
            created_count += 1
        instance.number = data["number"]
        for field in _SHOT_SCALAR_FIELDS:
            if field in data:
                setattr(instance, field, data[field])

        scene = data.get("scene", instance.scene if instance.pk else None)
        if scene and scene.project_id != episode.project_id:
            raise ValidationError({"shots": f"分镜 {instance.number} 的 scene 不属于该项目"})
        characters = list(data["characters"]) if "characters" in data else (
            list(instance.characters.all()) if instance.pk else []
        )
        props = list(data["props"]) if "props" in data else (
            list(instance.props.all()) if instance.pk else []
        )
        references = list(data["reference_assets"]) if "reference_assets" in data else (
            list(instance.reference_assets.all()) if instance.pk else []
        )
        if any(item.project_id != episode.project_id for item in characters):
            raise ValidationError({"shots": f"分镜 {instance.number} 含其他项目的人物"})
        if any(item.project_id != episode.project_id for item in props):
            raise ValidationError({"shots": f"分镜 {instance.number} 含其他项目的道具"})
        _validate_asset_access(user, references)
        dialogue = data.get("dialogue", instance.dialogue if instance.pk else [])
        speaker_ids = {item.stable_speaker_id for item in characters}
        unknown_speakers = {
            item.get("speaker_id") for item in dialogue
            if item.get("speaker_id") not in speaker_ids
        }
        if unknown_speakers:
            raise ValidationError({
                "shots": (
                    f"分镜 {instance.number} dialogue 含未绑定 speaker_id: "
                    f"{', '.join(sorted(str(item) for item in unknown_speakers))}"
                ),
            })
        if (
            instance.media_type == Shot.MediaType.VIDEO
            and not VIDEO_SHOT_MIN_DURATION_MS <= instance.duration_ms <= VIDEO_SHOT_MAX_DURATION_MS
        ):
            raise ValidationError({
                "shots": f"分镜 {instance.number} 的视频时长必须在 5000–15000ms",
            })
        if instance.media_type == Shot.MediaType.IMAGE:
            instance.include_audio = False
        try:
            instance.reference_bindings = normalize_shot_reference_bindings(
                mode=instance.h3_mode,
                bindings=data.get(
                    "reference_bindings", instance.reference_bindings if instance.pk else {},
                ),
                project=episode.project,
                characters=characters,
                scene=scene,
                props=props,
                shot_reference_assets=references,
                user=user,
            )
        except ReferenceBindingError as exc:
            raise ValidationError({
                "shots": f"分镜 {instance.number} reference_bindings: {exc}",
            }) from exc
        instance.save()
        if "characters" in data:
            instance.characters.set(characters)
        if "props" in data:
            instance.props.set(props)
        if "reference_assets" in data:
            instance.reference_assets.set(references)
        saved.append(instance)
    return saved, {"created": created_count, "updated": len(saved) - created_count, "deleted": deleted_count}


class EpisodeViewSet(viewsets.ModelViewSet):
    serializer_class = EpisodeSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "status", "number")
    search_fields = ("title", "synopsis", "script")
    ordering_fields = ("number", "created_at", "updated_at")

    def get_queryset(self):
        queryset = Episode.objects.select_related("project").annotate(
            shot_count=Count("shots", distinct=True),
            total_duration_ms=Coalesce(Sum("shots__duration_ms"), 0),
        ).order_by("project_id", "number")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)

    @extend_schema(request=EpisodeShotSyncSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="sync-shots")
    def sync_shots(self, request, pk=None):
        """Atomically replace an episode's ordered shot snapshot."""
        episode = self.get_object()
        serializer = EpisodeShotSyncSerializer(data=request.data, context={"episode": episode})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            episode = Episode.objects.select_for_update().select_related("project").get(pk=episode.pk)
            saved, counts = _sync_episode_shots(
                episode=episode, user=request.user, items=serializer.validated_data["shots"],
            )
        shots = Shot.objects.filter(pk__in=[item.pk for item in saved]).select_related("scene").prefetch_related(
            "characters", "props", "reference_assets",
        ).order_by("number")
        return Response({
            "episode_id": str(episode.id), "counts": counts,
            "shots": ShotSerializer(shots, many=True, context={"request": request}).data,
        })


class ShotViewSet(viewsets.ModelViewSet):
    serializer_class = ShotSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("episode", "episode__project", "media_type", "h3_mode", "include_audio")
    search_fields = ("title", "narrative", "action", "camera")
    ordering_fields = ("number", "duration_ms", "created_at", "updated_at")

    def get_queryset(self):
        queryset = Shot.objects.select_related("episode__project", "scene").prefetch_related(
            "characters", "props", "reference_assets"
        )
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(episode__project__owner=self.request.user)


def _append_translation_history(
    entry: PromptFieldTranslation, *, event: str = "", actor=None,
) -> None:
    history_item = {
        "revision": entry.revision,
        "source_text": entry.source_text,
        "translated_text": entry.translated_text,
        "status": entry.status,
        "provider": entry.provider,
        "model": entry.model,
        "error_message": entry.error_message,
        "confirmed_at": entry.confirmed_at.isoformat() if entry.confirmed_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }
    if event:
        history_item["event"] = event
    if actor is not None:
        history_item["confirmed_by"] = str(actor.pk)
    entry.revision_history = [*(entry.revision_history or []), history_item]


_PROMOTABLE_SHOT_SCALAR_FIELDS = {
    "shot.narrative": "narrative",
    "shot.detailed_description": "detailed_description",
    # The current storyboard's single action/visual description is
    # shot.narrative.  Legacy shot.action candidates stay audit-only: promoting
    # both would create two competing authoritative fields.
    "shot.camera": "camera",
    "shot.continuity_notes": "continuity_notes",
    "shot.sound_effects": "sound_effects",
    "shot.music": "music",
}
_PROMOTABLE_DIALOGUE_DELIVERY_RE = re.compile(
    r"^shot\.dialogue\.([0-9]+)\.delivery$"
)


class TranslationPromotionConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "translation_promotion_conflict"


def _promotion_target(field_key: str) -> tuple[str, int | None] | None:
    if field_key in _PROMOTABLE_SHOT_SCALAR_FIELDS:
        return _PROMOTABLE_SHOT_SCALAR_FIELDS[field_key], None
    match = _PROMOTABLE_DIALOGUE_DELIVERY_RE.fullmatch(field_key)
    if match:
        return "dialogue", int(match.group(1))
    return None


def _dialogue_identity(shot: Shot, index: int) -> dict:
    dialogue = shot.dialogue if isinstance(shot.dialogue, list) else []
    if index < 0 or index >= len(dialogue) or not isinstance(dialogue[index], dict):
        raise TranslationPromotionConflict({
            "code": "dialogue_index_changed", "shot_id": str(shot.id),
            "dialogue_index": index,
        })
    entry = dialogue[index]
    speaker_id = str(entry.get("speaker_id") or "")
    text = str(entry.get("text") or "")
    if not speaker_id or not text:
        raise TranslationPromotionConflict({
            "code": "dialogue_identity_invalid", "shot_id": str(shot.id),
            "dialogue_index": index,
        })
    return {
        "source_order": index,
        "speaker_id": speaker_id,
        "text_sha256": source_text_hash(text),
        "voiceover": bool(entry.get("voiceover")),
    }


def _promotion_field_payload(
    entry: PromptFieldTranslation, value: str,
) -> dict:
    target = _promotion_target(entry.field_key)
    dialogue_index = target[1] if target else None
    return {
        "field_key": entry.field_key,
        "value": value,
        "dialogue_index": dialogue_index,
    }


def _confirm_and_promote_translation(
    *, entry: PromptFieldTranslation, expected_revision: int,
    candidate: str, user, require_confirmed: bool = False,
) -> tuple[PromptFieldTranslation, dict | None, Shot | None]:
    """Confirm one candidate and atomically promote supported shot prose.

    The pre-promotion Chinese source remains in revision_history.  Once the
    English candidate is written to Shot, the translation row is rebased to
    that authoritative value so a successful confirmation does not immediately
    report source_changed.  Revoke/retranslation only changes review state and
    intentionally never rolls the formal Shot field back implicitly.
    """

    try:
        ensure_no_canonical_target_collisions(entry.project)
    except CanonicalTargetCollisionError as exc:
        raise TranslationPromotionConflict({
            "code": exc.code,
            "collisions": exc.collisions,
        }) from exc

    target = _promotion_target(entry.field_key)
    shot = None
    if target and entry.scope_type == PromptFieldTranslation.ScopeType.SHOT:
        try:
            shot = Shot.objects.select_for_update().get(
                pk=entry.scope_id, episode__project=entry.project,
            )
        except Shot.DoesNotExist as exc:
            raise TranslationPromotionConflict({
                "code": "shot_not_found",
                "entry_id": str(entry.id),
            }) from exc
        if target[0] == "dialogue":
            current_identity = _dialogue_identity(shot, int(target[1]))
            stored_identity = (entry.runtime_metadata or {}).get("dialogue_identity")
            if stored_identity:
                if stored_identity != current_identity:
                    raise TranslationPromotionConflict({
                        "code": "dialogue_identity_changed",
                        "entry_id": str(entry.id), "field_key": entry.field_key,
                    })
            elif len(shot.dialogue or []) != 1:
                # Legacy single-line rows are unambiguous and can be safely
                # adopted once. Multi-line rows require the identity snapshot
                # written when the candidate was generated.
                raise TranslationPromotionConflict({
                    "code": "dialogue_identity_missing",
                    "entry_id": str(entry.id), "field_key": entry.field_key,
                })

    try:
        current_source = resolve_current_source(
            project=entry.project,
            scope_type=entry.scope_type,
            scope_id=entry.scope_id,
            field_key=entry.field_key,
        )
    except TranslationValidationError as exc:
        raise TranslationPromotionConflict({
            "code": "source_unavailable", "entry_id": str(entry.id),
            "message": str(exc),
        }) from exc
    existing_promotion = (entry.runtime_metadata or {}).get("promotion") or {}
    transport_retry = bool(
        target
        and existing_promotion.get("promoted")
        and expected_revision == existing_promotion.get("applied_from_revision")
        and current_source == candidate
        and entry.translated_text == candidate
        and entry.status == PromptFieldTranslation.Status.CONFIRMED
        and source_text_hash(current_source) == entry.source_hash
    )
    if entry.revision != expected_revision and not transport_retry:
        raise TranslationPromotionConflict({
            "code": "revision_conflict",
            "entry_id": str(entry.id),
            "expected_revision": expected_revision,
            "current_revision": entry.revision,
        })
    if transport_retry:
        return entry, _promotion_field_payload(entry, candidate), shot
    if require_confirmed and entry.status != PromptFieldTranslation.Status.CONFIRMED:
        raise TranslationPromotionConflict({
            "code": "entry_not_confirmed",
            "entry_id": str(entry.id),
            "status": entry.status,
        })
    if source_text_hash(current_source) != entry.source_hash:
        raise TranslationPromotionConflict({
            "code": "source_changed", "entry_id": str(entry.id),
            "field_key": entry.field_key,
        })
    if not candidate:
        raise ValidationError({"translation": "没有可确认的译文"})
    try:
        candidate = validate_manual_translation(current_source, candidate)
    except TranslationValidationError as exc:
        raise ValidationError({"translation": str(exc)}) from exc
    if not translation_entry_context_current(
        entry, project=entry.project, current_source=current_source,
        translated_text=candidate,
    ):
        raise TranslationPromotionConflict({
            "code": "context_changed", "entry_id": str(entry.id),
            "field_key": entry.field_key,
        })

    already_promoted = bool(
        target
        and existing_promotion.get("promoted")
        and current_source == candidate
        and entry.translated_text == candidate
        and entry.status == PromptFieldTranslation.Status.CONFIRMED
    )
    if already_promoted:
        return entry, _promotion_field_payload(entry, candidate), shot

    _append_translation_history(
        entry, event="translation_promoted", actor=user,
    )
    entry.revision += 1
    now = timezone.now()
    promoted_field = None
    if target and shot is not None:
        field_name, dialogue_index = target
        shot_changed = False
        if field_name == "dialogue":
            dialogue = [dict(item) for item in (shot.dialogue or [])]
            if dialogue_index is None or dialogue_index >= len(dialogue):
                raise TranslationPromotionConflict({
                    "code": "dialogue_index_changed",
                    "entry_id": str(entry.id), "field_key": entry.field_key,
                })
            if str(dialogue[dialogue_index].get("delivery") or "") != candidate:
                dialogue[dialogue_index]["delivery"] = candidate
                shot.dialogue = dialogue
                shot.save(update_fields=("dialogue", "updated_at"))
                shot_changed = True
        elif str(getattr(shot, field_name) or "") != candidate:
            setattr(shot, field_name, candidate)
            shot.save(update_fields=(field_name, "updated_at"))
            shot_changed = True
        if not shot_changed:
            shot.refresh_from_db(fields=("updated_at",))

        first_promoted_at = str(
            existing_promotion.get("first_promoted_at") or now.isoformat()
        )
        first_source_hash = str(
            existing_promotion.get("original_source_hash") or entry.source_hash
        )
        entry.source_text = candidate
        entry.source_hash = source_text_hash(candidate)
        context = build_translation_context(
            entry.project, scope_type=entry.scope_type,
            scope_id=entry.scope_id, field_key=entry.field_key,
            source_text=candidate,
        )
        entry.runtime_metadata = {
            **(entry.runtime_metadata or {}),
            "translation_context_hash": translation_context_hash(context),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            "promotion": {
                "promoted": True,
                "event": "translation_promoted",
                "promoted_by": str(user.pk),
                "shot_id": str(shot.id),
                "field_key": entry.field_key,
                "dialogue_index": dialogue_index,
                "original_source_hash": first_source_hash,
                "original_source_text_sha256": first_source_hash,
                "promoted_value_sha256": source_text_hash(candidate),
                "applied_from_revision": existing_promotion.get(
                    "applied_from_revision", entry.revision - 1,
                ),
                "first_promoted_at": first_promoted_at,
                "last_confirmed_at": now.isoformat(),
                "shot_updated_at": shot.updated_at.isoformat(),
            },
        }
        if field_name == "dialogue":
            entry.runtime_metadata["dialogue_identity"] = _dialogue_identity(
                shot, int(dialogue_index),
            )
        promoted_field = _promotion_field_payload(entry, candidate)

    entry.translated_text = candidate
    entry.status = PromptFieldTranslation.Status.CONFIRMED
    entry.confirmed_by = user
    entry.confirmed_at = now
    entry.error_message = ""
    entry.save()
    return entry, promoted_field, shot


class PromptFieldTranslationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PromptFieldTranslationSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "scope_type", "scope_id", "field_key", "status")
    ordering_fields = ("created_at", "updated_at", "revision", "field_key")

    def get_queryset(self):
        queryset = PromptFieldTranslation.objects.select_related(
            "project", "confirmed_by",
        )
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(
            project__owner=self.request.user,
        )

    @extend_schema(request=PromptTranslationRetranslateSerializer, responses=PromptFieldTranslationSerializer)
    @action(detail=True, methods=["post"], url_path="retranslate")
    def retranslate(self, request, pk=None):
        entry = self.get_object()
        serializer = PromptTranslationRetranslateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data["provider"]
        try:
            current_source = resolve_current_source(
                project=entry.project,
                scope_type=entry.scope_type,
                scope_id=entry.scope_id,
                field_key=entry.field_key,
            )
        except TranslationValidationError as exc:
            raise ValidationError({"source_text": str(exc)}) from exc
        requested_source = data.get("source_text", current_source)
        if requested_source != current_source:
            raise ValidationError({
                "source_text": "source_text 已过期；请刷新页面后重新翻译",
            })
        if provider == LOCAL_QWEN_PROVIDER:
            _ensure_translation_resources_available()
        item = {
            "scope_type": entry.scope_type,
            "scope_id": entry.scope_id,
            "field_key": entry.field_key,
            "source_text": current_source,
        }
        try:
            result = translate_text_once(
                current_source,
                source_lang=data["source_lang"],
                target_lang=data["target_lang"],
                field_key=entry.field_key,
                label=entry.field_key.replace(".", " / "),
                context=_translation_context(entry.project, item),
                attempt=min(entry.revision + 1, 100),
                provider=provider,
            )
        except PromptTranslationResourceBusy as exc:
            raise TranslationResourceBusy(str(exc)) from exc
        except PromptTranslationError as exc:
            result = exc
        saved = _save_translation_candidate(
            project=entry.project, request_user=request.user, item=item, result=result,
            attempted_provider=provider,
        )
        return Response(PromptFieldTranslationSerializer(saved).data)

    @extend_schema(request=PromptTranslationConfirmSerializer, responses=PromptFieldTranslationSerializer)
    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        serializer = PromptTranslationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            entry = self.get_queryset().select_for_update().get(pk=pk)
            self.check_object_permissions(request, entry)
            candidate = serializer.validated_data.get("translation", entry.translated_text)
            entry, _, _ = _confirm_and_promote_translation(
                entry=entry,
                expected_revision=serializer.validated_data["expected_revision"],
                candidate=candidate,
                user=request.user,
            )
        return Response(PromptFieldTranslationSerializer(entry).data)

    @extend_schema(
        request=PromptTranslationPromoteShotSerializer,
        responses={200: PromptTranslationPromoteShotResponseSerializer},
        description=(
            "Atomically promote explicitly selected, already-confirmed English shot director "
            "fields into one authoritative Shot. Dialogue text/title and Bible fields are never changed."
        ),
    )
    @action(detail=False, methods=["post"], url_path="promote-shot")
    def promote_shot(self, request):
        serializer = PromptTranslationPromoteShotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        selections = data["entries"]
        selection_by_id = {
            str(item["entry_id"]): item for item in selections
        }
        with transaction.atomic():
            rows = list(
                self.get_queryset().select_for_update().filter(
                    pk__in=[item["entry_id"] for item in selections],
                )
            )
            by_id = {str(entry.id): entry for entry in rows}
            if set(by_id) != set(selection_by_id):
                raise TranslationPromotionConflict({
                    "code": "entry_not_found",
                    "missing_entry_ids": sorted(set(selection_by_id) - set(by_id)),
                })
            ordered_entries = [by_id[str(item["entry_id"])] for item in selections]
            shot_id = str(data["shot_id"])
            invalid = [
                str(entry.id) for entry in ordered_entries
                if entry.scope_type != PromptFieldTranslation.ScopeType.SHOT
                or str(entry.scope_id) != shot_id
                or _promotion_target(entry.field_key) is None
            ]
            if invalid:
                raise TranslationPromotionConflict({
                    "code": "entry_scope_mismatch",
                    "entry_ids": invalid,
                    "shot_id": shot_id,
                })
            promoted_fields = []
            saved_entries = []
            locked_shot = None
            for entry in ordered_entries:
                selection = selection_by_id[str(entry.id)]
                saved, promoted, promoted_shot = _confirm_and_promote_translation(
                    entry=entry,
                    expected_revision=selection["expected_revision"],
                    candidate=entry.translated_text,
                    user=request.user,
                    require_confirmed=True,
                )
                saved_entries.append(saved)
                if promoted:
                    promoted_fields.append(promoted)
                locked_shot = promoted_shot or locked_shot
            shot_patch = {}
            if data["repair_legacy_negative_prompt"]:
                if locked_shot is None:
                    shot_queryset = Shot.objects.select_for_update().select_related(
                        "episode__project",
                    )
                    if not request.user.is_staff:
                        shot_queryset = shot_queryset.filter(
                            episode__project__owner=request.user,
                        )
                    try:
                        locked_shot = shot_queryset.get(pk=data["shot_id"])
                    except Shot.DoesNotExist as exc:
                        raise TranslationPromotionConflict({
                            "code": "shot_not_found", "shot_id": shot_id,
                        }) from exc
                try:
                    shot_patch = repair_legacy_negative_prompt(
                        shot=locked_shot, user=request.user,
                    )
                except ValueError as exc:
                    raise TranslationPromotionConflict({
                        "code": str(exc), "shot_id": shot_id,
                    }) from exc
        return Response({
            "shot_id": shot_id,
            "promoted_fields": promoted_fields,
            "entries": PromptFieldTranslationSerializer(saved_entries, many=True).data,
            "shot_patch": shot_patch,
        })

    @action(detail=True, methods=["post"], url_path="request-retranslation")
    def request_retranslation(self, request, pk=None):
        with transaction.atomic():
            entry = self.get_queryset().select_for_update().get(pk=pk)
            self.check_object_permissions(request, entry)
            _append_translation_history(entry)
            entry.revision += 1
            entry.status = PromptFieldTranslation.Status.NEEDS_RETRANSLATION
            entry.confirmed_by = None
            entry.confirmed_at = None
            entry.error_message = ""
            entry.save()
        return Response(PromptFieldTranslationSerializer(entry).data)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        with transaction.atomic():
            entry = self.get_queryset().select_for_update().get(pk=pk)
            self.check_object_permissions(request, entry)
            _append_translation_history(entry)
            entry.revision += 1
            entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
            entry.confirmed_by = None
            entry.confirmed_at = None
            entry.error_message = ""
            entry.save()
        return Response(PromptFieldTranslationSerializer(entry).data)


def _negative_prompt(project: SeriesProject, shots: list[Shot]) -> str:
    return build_negative_prompt(project, shots)


def _prompt_artifact_matches_current_input(
    artifact: PromptArtifact,
    *,
    input_fingerprint: str,
    shot_ids: list[str],
    consistency_snapshot: dict,
    reference_bindings: list[dict],
    negative_prompt: str,
) -> bool:
    """Fail closed unless a stored artifact is the exact current compilation.

    ``content_hash`` protects the actual prompt text, while the versioned input
    fingerprint protects every source field and compiler/optimizer setting that
    produced it.  Both are required: a matching source snapshot must never make
    a subsequently edited artifact eligible for production reuse.
    """

    request_options = artifact.request_options or {}
    payload = artifact.structured_payload or {}
    try:
        fingerprint_version = int(request_options.get("input_fingerprint_version", 0))
        payload_fingerprint_version = int(payload.get("input_fingerprint_version", 0))
        compiler_version = int(payload.get("prompt_compiler_version", 0))
    except (TypeError, ValueError):
        return False
    return bool(
        fingerprint_version == PROMPT_INPUT_FINGERPRINT_VERSION
        and payload_fingerprint_version == PROMPT_INPUT_FINGERPRINT_VERSION
        and compiler_version == PROMPT_COMPILER_VERSION
        and request_options.get("input_fingerprint") == input_fingerprint
        and payload.get("input_fingerprint") == input_fingerprint
        and [str(value) for value in request_options.get("shot_ids", [])] == shot_ids
        and artifact.consistency_snapshot == consistency_snapshot
        and (payload.get("reference_bindings") or []) == reference_bindings
        and artifact.negative_prompt == negative_prompt
        and artifact.content_hash
        == hashlib.sha256(artifact.prompt_text.encode("utf-8")).hexdigest()
    )


def _create_prompt_artifacts(
    user, project: SeriesProject, episode: Episode | None, shots: list[Shot], *, prompt_type: str,
    mode: str, multi_shot: bool, include_audio: bool, duration_overrides: dict[str, int],
    overall_soundscape: str = "", non_diegetic_music: str = "", extra_direction: str = "",
    max_group_duration_ms: int | None = None,
    single_shot_reference_bindings: dict | None = None,
    single_shot_reference_assets: list[Asset] | None = None,
    consistency_snapshot: dict | None = None,
    reuse_latest_unchanged: bool = False,
) -> list[PromptArtifact]:
    if multi_shot and prompt_type == PromptArtifact.PromptType.VIDEO:
        if max_group_duration_ms:
            groups, current, current_duration = [], [], 0
            for shot in shots:
                duration = int(duration_overrides.get(str(shot.id), shot.duration_ms))
                if duration > max_group_duration_ms:
                    raise PromptBuildError(f"分镜 {shot.number} 超过单次 H3 安全时长 {max_group_duration_ms}ms")
                if current and current_duration + duration > max_group_duration_ms:
                    groups.append(current)
                    current, current_duration = [], 0
                current.append(shot)
                current_duration += duration
            if current:
                groups.append(current)
        else:
            groups = [shots]
    else:
        groups = [[shot] for shot in shots]
    snapshot = (
        consistency_snapshot
        if consistency_snapshot is not None
        else capture_consistency_snapshot(project)
    )
    artifacts = []
    for group in groups:
        target_shot = None if len(group) > 1 else group[0]
        target_episode = episode or group[0].episode
        shot_ids = [str(item.id) for item in group]
        options = H3PromptOptions(
            mode=mode, include_audio=include_audio, language=project.language,
            duration_overrides=duration_overrides, overall_soundscape=overall_soundscape,
            non_diegetic_music=non_diegetic_music, extra_direction=extra_direction,
            single_shot_reference_bindings=single_shot_reference_bindings,
            single_shot_reference_assets=single_shot_reference_assets,
            consistency_snapshot=snapshot,
        )
        if prompt_type == PromptArtifact.PromptType.VIDEO:
            try:
                reference_bindings = resolve_group_reference_bindings(
                    project=project,
                    shots=group,
                    mode=mode,
                    snapshot=snapshot,
                    single_shot_bindings=single_shot_reference_bindings,
                    single_shot_reference_assets=single_shot_reference_assets,
                )
            except ReferenceBindingError as exc:
                raise PromptBuildError(str(exc)) from exc
        else:
            reference_bindings = []
        fingerprint = prompt_input_fingerprint(
            project,
            group,
            options,
            prompt_type=prompt_type,
            consistency_snapshot=snapshot,
            reference_bindings=reference_bindings,
        )
        negative_prompt = _negative_prompt(project, group)
        if reuse_latest_unchanged:
            latest = PromptArtifact.objects.filter(
                project=project,
                episode=target_episode,
                shot=target_shot,
                prompt_type=prompt_type,
                request_options__shot_ids=shot_ids,
            ).order_by("-revision", "-created_at").first()
            if latest and _prompt_artifact_matches_current_input(
                latest,
                input_fingerprint=fingerprint,
                shot_ids=shot_ids,
                consistency_snapshot=snapshot,
                reference_bindings=reference_bindings,
                negative_prompt=negative_prompt,
            ):
                latest._production_prompt_reused = True
                artifacts.append(latest)
                continue
        if prompt_type == PromptArtifact.PromptType.IMAGE:
            result = build_consistent_image_prompt(
                project,
                group[0],
                extra_direction,
                consistency_snapshot=snapshot,
            )
        else:
            result = build_h3_video_prompt(project, group, options)
        structured_payload = {
            **result.payload,
            "input_fingerprint_version": PROMPT_INPUT_FINGERPRINT_VERSION,
            "input_fingerprint": fingerprint,
        }
        last_revision = PromptArtifact.objects.filter(
            project=project, episode=target_episode, shot=target_shot, prompt_type=prompt_type,
        ).aggregate(value=Max("revision"))["value"] or 0
        artifact = PromptArtifact.objects.create(
            project=project, episode=target_episode, shot=target_shot, created_by=user,
            prompt_type=prompt_type,
            model=str(result.payload.get("model") or project.target_model or "minimax-h3"),
            h3_mode=mode,
            revision=last_revision + 1, is_multi_shot=len(group) > 1,
            include_audio=include_audio if prompt_type == PromptArtifact.PromptType.VIDEO else False,
            total_duration_ms=result.total_duration_ms, prompt_text=result.text,
            negative_prompt=negative_prompt, structured_payload=structured_payload,
            consistency_snapshot=result.consistency_snapshot,
            request_options={
                "shot_ids": shot_ids,
                "duration_overrides": duration_overrides,
                "overall_soundscape": overall_soundscape,
                "non_diegetic_music": non_diegetic_music,
                "extra_direction": extra_direction,
                "reference_bindings_override": single_shot_reference_bindings,
                "reference_asset_ids_override": [
                    str(item.id) for item in (single_shot_reference_assets or [])
                ],
                "input_fingerprint_version": PROMPT_INPUT_FINGERPRINT_VERSION,
                "input_fingerprint": fingerprint,
            },
            content_hash=result.content_hash,
        )
        artifact._production_prompt_reused = False
        artifacts.append(artifact)
    return artifacts


def _partition_h3_shots(shots: list[Shot]) -> list[list[Shot]]:
    """Partition one ordered, homogeneous video run into safe 5–15s jobs."""
    if not shots:
        return []
    size = len(shots)
    best: list[list[tuple[int, int]] | None] = [None] * (size + 1)
    best[0] = []
    for start in range(size):
        if best[start] is None:
            continue
        duration = 0
        for end in range(start, size):
            duration += shots[end].duration_ms
            if duration > 15_000:
                break
            if duration < 5_000:
                continue
            candidate = [*best[start], (start, end + 1)]
            current = best[end + 1]
            if current is None or len(candidate) < len(current):
                best[end + 1] = candidate
    if best[size] is None:
        numbers = ", ".join(str(item.number) for item in shots)
        raise PromptBuildError(
            f"视频分镜 [{numbers}] 无法按连续顺序划分为 5000–15000ms 的 H3 任务；"
            "请调整分镜时长或模式/音频分组"
        )
    return [shots[start:end] for start, end in best[size]]


def _production_groups(
    episode: Episode,
    shots: list[Shot],
    default_multi_shot: bool,
    *,
    force_single_shot: bool = False,
):
    # A whole-episode request is represented by an ordered batch of independently
    # traceable child tasks.  Its per-shot contract must win over the persisted
    # episode editor preference, which is still authoritative for normal builds.
    multi_shot = False if force_single_shot else bool(
        (episode.prompt_settings or {}).get("multi_shot", default_multi_shot)
    )
    groups: list[list[Shot]] = []
    run: list[Shot] = []
    run_key = None

    def flush_run():
        nonlocal run, run_key
        if run:
            groups.extend(_partition_h3_shots(run) if multi_shot else [[item] for item in run])
        run, run_key = [], None

    for shot in shots:
        if shot.media_type == Shot.MediaType.IMAGE:
            flush_run()
            groups.append([shot])
            continue
        # First/last-frame inputs are shot-bound controls in the editor. Never
        # merge two such shots and risk dropping or crossing their frame slots.
        if shot.h3_mode in {Shot.H3Mode.I2VA, Shot.H3Mode.FL2VA, Shot.H3Mode.L2VA}:
            flush_run()
            groups.append([shot])
            continue
        binding_signature = (
            json.dumps(shot.reference_bindings or {}, ensure_ascii=False, sort_keys=True)
            if shot.h3_mode == Shot.H3Mode.REF2VA else ""
        )
        key = (shot.media_type, shot.h3_mode, shot.include_audio, binding_signature)
        if not multi_shot:
            flush_run()
            groups.append([shot])
        elif run_key is None or run_key == key:
            run.append(shot)
            run_key = key
        else:
            flush_run()
            run.append(shot)
            run_key = key
    flush_run()
    return groups


def _select_production_template(
    *, templates: list[WorkflowTemplate], preferred: WorkflowTemplate | None,
    selected: WorkflowTemplate | None = None, output_kind: str, h3_mode: str,
) -> WorkflowTemplate:
    def compatible(template):
        if template.provider != "comfyui" or template.output_kind != output_kind:
            return False
        modes = (template.input_schema or {}).get("h3_modes")
        return output_kind != WorkflowTemplate.OutputKind.VIDEO or not modes or h3_mode in modes

    if preferred:
        if compatible(preferred):
            return preferred
        modes = (preferred.input_schema or {}).get("h3_modes") or []
        raise ValidationError(
            f"显式选择的工作流 {preferred.name} 与当前 {output_kind}/{h3_mode} 不兼容；"
            f"允许模式: {', '.join(modes) or '未声明'}。系统不会静默改用其他模板。"
        )
    if selected:
        if compatible(selected):
            return selected
        modes = (selected.input_schema or {}).get("h3_modes") or []
        raise ValidationError(
            f"为 {output_kind}/{h3_mode} 选择的工作流 {selected.name} 不兼容；"
            f"允许模式: {', '.join(modes) or '未声明'}。系统不会静默降级。"
        )
    candidates = [item for item in templates if compatible(item)]
    if not candidates:
        if output_kind == WorkflowTemplate.OutputKind.IMAGE:
            raise ValidationError(
                "项目包含图片分镜，但没有 active 且已配置的 ComfyUI 图片 WorkflowTemplate；"
                "系统不会把图片伪装成视频或跳过。请先导入并 live 校验真实图片工作流。"
            )
        raise ValidationError(f"没有 active ComfyUI 视频模板支持 H3 模式 {h3_mode}")
    candidates.sort(key=lambda item: (
        not bool((item.input_schema or {}).get("h3_modes")),
        not bool((item.input_schema or {}).get("is_default")),
        item.owner_id is not None,
        -item.version,
        item.slug,
    ))
    return candidates[0]


class PromptArtifactViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PromptArtifactSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "shot", "prompt_type", "h3_mode", "is_multi_shot")
    search_fields = ("prompt_text", "content_hash")
    ordering_fields = ("created_at", "revision", "total_duration_ms")

    def get_queryset(self):
        queryset = PromptArtifact.objects.select_related("project", "episode", "shot", "created_by")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)

    @extend_schema(request=PromptGenerateSerializer, responses={201: PromptArtifactSerializer(many=True)})
    @action(
        detail=False, methods=["post"], url_path="generate",
        pagination_class=None, filter_backends=[],
    )
    def generate(self, request):
        serializer = PromptGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            project = _owned_projects(SeriesProject.objects.select_related("style"), request.user).get(pk=data["project_id"])
        except SeriesProject.DoesNotExist as exc:
            raise NotFound("连续剧项目不存在") from exc
        episode = None
        if data.get("episode_id"):
            try:
                episode = project.episodes.get(pk=data["episode_id"])
            except Episode.DoesNotExist as exc:
                raise ValidationError("episode_id 不属于该项目") from exc
        if data.get("shot_ids"):
            shots = load_shots([str(value) for value in data["shot_ids"]])
        else:
            shots = load_shots([str(item.id) for item in episode.shots.order_by("number")])
        if len(shots) != len(data.get("shot_ids", shots)) or any(shot.episode.project_id != project.id for shot in shots):
            raise ValidationError("存在无效或不属于该项目的 shot_id")
        if episode and any(shot.episode_id != episode.id for shot in shots):
            raise ValidationError("所有分镜必须属于 episode_id 指定的剧集")
        if data["multi_shot"] and len({shot.episode_id for shot in shots}) != 1:
            raise ValidationError("多分镜提示词不能跨剧集")
        has_reference_override = (
            "reference_bindings" in data or "reference_asset_ids" in data
        )
        if has_reference_override and len(shots) != 1:
            raise ValidationError({
                "reference_bindings": "临时参考素材绑定只允许单个分镜；多分镜请先保存每个 Shot 的绑定",
            })
        reference_assets_override = None
        reference_bindings_override = None
        if has_reference_override:
            reference_bindings_override = data.get("reference_bindings", {})
            reference_ids = list(data.get("reference_asset_ids", []))
            reference_assets_override = list(Asset.objects.filter(pk__in=reference_ids, is_active=True))
            if len(reference_assets_override) != len(reference_ids):
                raise ValidationError({"reference_asset_ids": "包含不存在或已停用的素材"})
            _validate_asset_access(request.user, reference_assets_override)
        overrides = {str(item["shot_id"]): item["duration_ms"] for item in data["shot_durations"]}
        if set(overrides) - {str(shot.id) for shot in shots}:
            raise ValidationError("shot_durations 包含未选择的分镜")
        total_duration = data.get("total_duration_ms")
        if total_duration is not None:
            if total_duration < 100 * len(shots):
                raise ValidationError("total_duration_ms 必须至少为分镜数 × 100ms")
            if overrides:
                if set(overrides) != {str(shot.id) for shot in shots} or sum(overrides.values()) != total_duration:
                    raise ValidationError("同时设置总时长时，必须提供全部分镜时长且总和精确相等")
            else:
                original_total = sum(shot.duration_ms for shot in shots)
                remaining = total_duration
                for index, shot in enumerate(shots):
                    remaining_count = len(shots) - index - 1
                    desired = round(total_duration * shot.duration_ms / original_total)
                    value = remaining if not remaining_count else min(
                        max(100, desired), remaining - (100 * remaining_count)
                    )
                    overrides[str(shot.id)] = value
                    remaining -= value
                if remaining != 0 or min(overrides.values()) < 100:
                    raise ValidationError("无法在每个分镜至少 100ms 的条件下分配总时长")
        try:
            with transaction.atomic():
                project = SeriesProject.objects.select_for_update().select_related("style").get(pk=project.pk)
                sync_consistency_locks(project)
                artifacts = _create_prompt_artifacts(
                    request.user, project, episode, shots,
                    prompt_type=data["prompt_type"], mode=data["h3_mode"],
                    multi_shot=data["multi_shot"], include_audio=data["include_audio"],
                    duration_overrides=overrides, overall_soundscape=data["overall_soundscape"],
                    non_diegetic_music=data["non_diegetic_music"], extra_direction=data["extra_direction"],
                    single_shot_reference_bindings=reference_bindings_override,
                    single_shot_reference_assets=reference_assets_override,
                )
        except PromptBuildError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(artifacts, many=True).data, status=status.HTTP_201_CREATED)


class WorkflowTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = WorkflowTemplateSerializer
    permission_classes = [IsStaffOrReadOnly]
    filterset_fields = ("provider", "output_kind", "model", "is_active", "is_system")
    search_fields = ("name", "slug", "description")
    ordering_fields = ("name", "version", "created_at")

    def get_queryset(self):
        return _owned_or_global(WorkflowTemplate.objects.select_related("owner"), self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, is_system=False)

    @extend_schema(responses=ProductionWorkflowTemplateSerializer(many=True))
    @action(
        detail=False, methods=["get"], url_path="production-options",
        pagination_class=None, filter_backends=[],
    )
    def production_options(self, request):
        templates = _executable_templates(
            self.get_queryset().filter(
                provider="comfyui", output_kind=WorkflowTemplate.OutputKind.VIDEO,
                is_active=True,
            ),
            request.user,
        ).order_by("name", "-version")
        return Response(ProductionWorkflowTemplateSerializer(templates, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @action(detail=True, methods=["get"], url_path="validate-comfyui")
    def validate_comfyui(self, request, pk=None):
        template = self.get_object()
        try:
            client = ComfyUIClient()
            object_info = client.object_info()
        except ComfyUIError as exc:
            return Response({"reachable": False, "valid": False, "error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        node_types = {
            node.get("class_type") for node in template.definition.values()
            if isinstance(node, dict) and isinstance(node.get("class_type"), str)
        }
        dynamic = sorted(value for value in node_types if "${" in value)
        missing = sorted(value for value in node_types if "${" not in value and value not in object_info)
        required_models = {
            "checkpoints": set(), "diffusion_models": set(), "text_encoders": set(),
            "vae": set(), "loras": set(),
        }
        for node in template.definition.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            if inputs.get("ckpt_name"):
                checkpoint = inputs["ckpt_name"]
                if checkpoint == "${MODEL}":
                    checkpoint = template.model
                if checkpoint:
                    required_models["checkpoints"].add(checkpoint)
            if inputs.get("unet_name"):
                required_models["diffusion_models"].add(inputs["unet_name"])
            if inputs.get("clip_name"):
                required_models["text_encoders"].add(inputs["clip_name"])
            if inputs.get("vae_name"):
                required_models["vae"].add(inputs["vae_name"])
            if inputs.get("lora_name"):
                required_models["loras"].add(inputs["lora_name"])
        missing_models = {}
        resolved_models = {}
        try:
            for folder, required in required_models.items():
                if not required:
                    continue
                available = {value.casefold(): value for value in client.models(folder)}
                missing_models[folder] = sorted(value for value in required if value.casefold() not in available)
                resolved_models[folder] = {
                    value: available.get(value.casefold()) for value in sorted(required)
                }
        except ComfyUIError as exc:
            return Response({"reachable": True, "valid": False, "error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        missing_models = {key: value for key, value in missing_models.items() if value}
        return Response({
            "reachable": True, "valid": not missing and not dynamic and not missing_models,
            "missing_node_types": missing, "dynamic_node_types": dynamic,
            "checked_node_types": sorted(node_types),
            "missing_models": missing_models, "resolved_models": resolved_models,
        })


class GeneratedWorkflowViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = GeneratedWorkflowSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "prompt_artifact", "template")
    search_fields = ("content_hash",)
    ordering_fields = ("created_at", "updated_at")

    def get_queryset(self):
        queryset = GeneratedWorkflow.objects.select_related("project", "episode", "prompt_artifact", "template")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=WorkflowGenerateSerializer, responses={201: GeneratedWorkflowSerializer})
    @action(
        detail=False, methods=["post"], url_path="generate",
        pagination_class=None, filter_backends=[],
    )
    def generate(self, request):
        serializer = WorkflowGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            prompt = self._owned_prompt(request.user).select_related("project", "episode").get(pk=data["prompt_artifact_id"])
        except PromptArtifact.DoesNotExist as exc:
            raise NotFound("提示词版本不存在") from exc
        try:
            template = _executable_templates(WorkflowTemplate.objects.all(), request.user).get(pk=data["template_id"])
        except WorkflowTemplate.DoesNotExist as exc:
            raise NotFound("工作流模板不存在") from exc
        if template.output_kind != prompt.prompt_type:
            raise ValidationError("模板输出类型与提示词类型不一致")
        # Creating a workflow is a preview/planning operation. Surface the
        # current production gate decision, but do not block until GPU work is
        # actually enqueued through /tasks/ or build-production.
        language_gate_payload = _prompt_language_gate_payload(
            prompt,
            episode_id=str(prompt.episode_id) if prompt.episode_id else None,
            shot_ids=list((prompt.request_options or {}).get("shot_ids") or []),
        )
        if prompt.prompt_type == PromptArtifact.PromptType.IMAGE and template.model:
            prompt.model = template.model
            prompt.save(update_fields=("model", "updated_at"))
        try:
            workflow_json, injected, content_hash = build_workflow(template, prompt, prompt.project, data["overrides"])
        except WorkflowBuildError as exc:
            raise ValidationError(str(exc)) from exc
        workflow = GeneratedWorkflow.objects.create(
            project=prompt.project, episode=prompt.episode, prompt_artifact=prompt,
            template=template, created_by=request.user, workflow_json=workflow_json,
            injected_values=injected, content_hash=content_hash,
        )
        response_data = self.get_serializer(workflow).data
        if language_gate_payload:
            response_data["prompt_language_gate"] = language_gate_payload
        return Response(response_data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _owned_prompt(user):
        queryset = PromptArtifact.objects.all()
        return queryset if user.is_staff else queryset.filter(project__owner=user)


class GenerationTaskViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = GenerationTaskSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "kind", "status", "generated_workflow")
    search_fields = ("comfy_prompt_id", "error_message")
    ordering_fields = ("priority", "queued_at", "started_at", "finished_at", "updated_at")

    def get_queryset(self):
        queryset = GenerationTask.objects.select_related(
            "project",
            "episode",
            "generated_workflow__template",
            "generated_workflow__prompt_artifact__episode",
            "generated_workflow__prompt_artifact__shot",
            "owner",
        )
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        queryset = queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)
        batch_id = self.request.query_params.get("batch_id")
        if batch_id:
            queryset = queryset.filter(capability_snapshot__batch_id=batch_id).order_by(
                "capability_snapshot__batch_index", "queued_at", "id",
            )
        shot_id = self.request.query_params.get("shot_id")
        if shot_id:
            try:
                parsed_shot_id = uuid.UUID(shot_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValidationError({"shot_id": "shot_id 必须是有效 UUID"}) from exc
            queryset = queryset.filter(
                Q(capability_snapshot__shot_id=str(parsed_shot_id))
                | Q(generated_workflow__prompt_artifact__shot_id=parsed_shot_id)
            )
        return queryset

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = GenerationTaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        queryset = GeneratedWorkflow.objects.select_related("project", "episode", "prompt_artifact", "template")
        if not request.user.is_staff:
            queryset = queryset.filter(project__owner=request.user)
        try:
            workflow = queryset.get(pk=data["generated_workflow_id"])
        except GeneratedWorkflow.DoesNotExist as exc:
            raise NotFound("生成工作流不存在") from exc
        if not _can_execute_template(workflow.template, request.user):
            raise ValidationError("The workflow template is not executable by this user.")
        _require_prompt_language_gate_for_enqueue(
            workflow.prompt_artifact,
            workflow_id=str(workflow.id),
            episode_id=str(workflow.episode_id) if workflow.episode_id else None,
            shot_ids=list((workflow.prompt_artifact.request_options or {}).get("shot_ids") or []),
        )
        with transaction.atomic():
            _assert_queue_capacity(request.user)
            task = _create_generation_task(
                user=request.user, workflow=workflow,
                priority=data["priority"], max_attempts=data["max_attempts"],
            )
        return Response(self.get_serializer(task).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        responses={204: None},
        description=(
            "Delete only a terminal GenerationTask history record. "
            "Generated workflows, prompt artifacts, templates, and physical output media are retained. "
            "Tasks referenced by a successful assembly are retained to preserve its source audit trail."
        ),
    )
    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            try:
                task = self.get_queryset().select_for_update().get(pk=kwargs["pk"])
            except GenerationTask.DoesNotExist as exc:
                raise NotFound("生成任务不存在") from exc
            self.check_object_permissions(request, task)
            terminal_statuses = {
                GenerationTask.Status.SUCCEEDED,
                GenerationTask.Status.FAILED,
                GenerationTask.Status.CANCELLED,
            }
            if task.status not in terminal_statuses:
                raise ValidationError(
                    "仅能删除已成功、已失败或已取消的终态任务；"
                    "排队中、运行中或取消中任务不可删除。"
                )
            if task_is_release_evidence(task):
                raise ValidationError("该任务已进入封存或历史发行清单，不能删除。")
            self._assert_not_successful_assembly_source(task)
            self._assert_successful_video_source_is_replaceable(task)
            task.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _assert_not_successful_assembly_source(task: GenerationTask) -> None:
        referenced_by_successful_assembly = AssemblyJob.objects.select_for_update().filter(
            source_tasks=task,
            status=AssemblyJob.Status.SUCCEEDED,
        ).exists()
        if referenced_by_successful_assembly:
            raise ValidationError(
                "该任务已被成功组装成片引用，删除会破坏成片来源审计链。"
                "请保留该任务记录；如需更换镜头，请先生成替代镜头并重新组装。"
            )

    @staticmethod
    def _recorded_video_output(task: GenerationTask) -> bool:
        for item in task.outputs or []:
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            if item.get("kind") in {"videos", "gifs"}:
                return True
            if str(item["filename"]).casefold().endswith((".mp4", ".mov", ".mkv", ".webm")):
                return True
        return False

    @staticmethod
    def _covered_video_shot_ids(task: GenerationTask) -> set[str]:
        prompt = task.generated_workflow.prompt_artifact
        values = (prompt.request_options or {}).get("shot_ids", [])
        if isinstance(values, list) and values:
            covered = {str(value) for value in values}
        elif prompt.shot_id:
            covered = {str(prompt.shot_id)}
        else:
            return set()
        return {
            str(value)
            for value in Shot.objects.filter(
                episode_id=task.episode_id,
                media_type=Shot.MediaType.VIDEO,
                pk__in=covered,
            ).values_list("id", flat=True)
        }

    def _assert_successful_video_source_is_replaceable(self, task: GenerationTask) -> None:
        if (
            task.status != GenerationTask.Status.SUCCEEDED
            or task.kind != GenerationTask.Kind.VIDEO
            or not task.episode_id
            or not self._recorded_video_output(task)
        ):
            return
        required_shot_ids = self._covered_video_shot_ids(task)
        if not required_shot_ids:
            return
        alternatives = list(
            GenerationTask.objects.select_for_update().filter(
                owner=task.owner,
                episode_id=task.episode_id,
                kind=GenerationTask.Kind.VIDEO,
                status=GenerationTask.Status.SUCCEEDED,
            ).exclude(pk=task.pk).select_related(
                "generated_workflow__prompt_artifact",
            )
        )
        replacement_shot_ids: set[str] = set()
        for candidate in alternatives:
            if self._recorded_video_output(candidate):
                replacement_shot_ids.update(self._covered_video_shot_ids(candidate))
        unprotected = required_shot_ids - replacement_shot_ids
        if not unprotected:
            return
        shot_numbers = list(
            Shot.objects.filter(pk__in=unprotected).order_by("number").values_list("number", flat=True)
        )
        formatted = "、".join(f"镜头 {number:02d}" for number in shot_numbers)
        raise ValidationError(
            f"该任务仍是 {formatted} 唯一可组装的成功视频产物，不能删除。"
            "请先重新生成对应镜头并确认新任务成功。"
        )

    @extend_schema(
        responses={200: GenerationTaskInputPromptSerializer},
        description=(
            "Return the persisted prompt text actually injected into this task's workflow, "
            "without returning the full injected values or workflow graph."
        ),
    )
    @action(detail=True, methods=["get"], url_path="input-prompt")
    def input_prompt(self, request, pk=None):
        task = self.get_object()
        payload = GenerationTaskSerializer.build_input_prompt_payload(task)
        return Response(GenerationTaskInputPromptSerializer(payload).data)

    @extend_schema(
        request=TaskOutputPromoteSerializer,
        responses={200: AssetSerializer, 201: AssetSerializer},
        description=(
            "Copy one successful generated image into the owner's reusable asset library. "
            "The ComfyUI master remains unchanged; repeated calls are idempotent."
        ),
    )
    @action(detail=True, methods=["post"], url_path="promote-output-to-asset")
    def promote_output_to_asset(self, request, pk=None):
        request_serializer = TaskOutputPromoteSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        output_index = request_serializer.validated_data["output_index"]
        requested_name = request_serializer.validated_data.get("name", "").strip()
        reference_category = request_serializer.validated_data.get("reference_category") or ""
        requested_entity_id = request_serializer.validated_data.get("entity_id")
        requested_target = {
            "reference_category": reference_category,
            "entity_id": str(requested_entity_id) if requested_entity_id else "",
        }
        asset_kind_label = {
            Asset.Kind.CHARACTER: "人物",
            Asset.Kind.SCENE: "场景",
            Asset.Kind.PROP: "道具",
        }.get(reference_category, "")

        with transaction.atomic():
            try:
                task = self.get_queryset().select_for_update().select_related(
                    "project__style", "generated_workflow__prompt_artifact__shot",
                ).get(pk=pk)
            except GenerationTask.DoesNotExist as exc:
                raise NotFound("任务不存在") from exc
            if task.status != GenerationTask.Status.SUCCEEDED:
                raise ValidationError({"output_index": "只有已完成任务的图片产物可以保存为素材"})
            if not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
                raise ValidationError({"output_index": "产物序号不存在"})
            output = task.outputs[output_index]
            if classify_task_output(output) != "image":
                raise ValidationError({"output_index": "当前产物不是图片，不能保存为图片素材"})
            entity = None
            if reference_category:
                entity_model = {
                    Asset.Kind.CHARACTER: CharacterBible,
                    Asset.Kind.SCENE: SceneBible,
                    Asset.Kind.PROP: PropBible,
                }[reference_category]
                entity = entity_model.objects.select_for_update().filter(
                    pk=requested_entity_id,
                    project=task.project,
                ).first()
                if entity is None:
                    raise ValidationError({
                        "entity_id": "所选分类实体不存在或不属于当前任务项目",
                    })

            existing_id = output.get("promoted_asset_id") if isinstance(output, dict) else None
            stored_target = output.get("promoted_asset_target") if isinstance(output, dict) else None
            if isinstance(stored_target, dict):
                normalized_stored_target = {
                    "reference_category": str(stored_target.get("reference_category") or ""),
                    "entity_id": str(stored_target.get("entity_id") or ""),
                }
                if normalized_stored_target != requested_target:
                    raise ValidationError({
                        "reference_category": (
                            "该图片产物已按另一种素材目标保存；为保持引用稳定，"
                            "不能把同一次保存悄悄改成其他分类或实体"
                        ),
                    })
            if existing_id:
                existing = Asset.objects.filter(pk=existing_id, owner=task.owner).first()
                if existing:
                    if not isinstance(stored_target, dict):
                        generated_from = (existing.metadata or {}).get("generated_from") or {}
                        legacy_target = {
                            "reference_category": str(
                                (existing.metadata or {}).get("reference_category")
                                or generated_from.get("reference_category")
                                or (existing.kind if existing.kind in {
                                    Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP,
                                } else "")
                            ),
                            "entity_id": str(
                                (existing.metadata or {}).get("reference_entity_id")
                                or generated_from.get("entity_id")
                                or ""
                            ),
                        }
                        if legacy_target != requested_target:
                            raise ValidationError({
                                "reference_category": (
                                    "该图片产物已保存为不同的素材目标；"
                                    "如需另一用途，请保留现有素材并重新生成一张图片"
                                ),
                            })
                    if entity is not None:
                        entity.reference_assets.add(existing)
                    return Response(AssetSerializer(existing, context={"request": request}).data)

            filename = output.get("filename") if isinstance(output, dict) else None
            subfolder = output.get("subfolder", "") if isinstance(output, dict) else ""
            if not isinstance(filename, str) or not isinstance(subfolder, str):
                raise ValidationError({"output_index": "图片产物路径记录不完整"})
            configured_root = str(settings.COMFYUI_OUTPUT_DIR or "").strip()
            if not configured_root:
                raise ValidationError({"output_index": "ComfyUI 输出目录未配置"})
            allowed_root = Path(configured_root).resolve()
            source_path = (allowed_root / subfolder / filename).resolve()
            if not source_path.is_relative_to(allowed_root) or not source_path.is_file():
                raise ValidationError({"output_index": "图片母版不存在或超出输出目录"})
            if not isinstance(output.get("provenance"), dict):
                task.outputs = persist_task_provenance(
                    task, list(task.outputs), generated_at=task.finished_at,
                )
                task.save(update_fields=("outputs", "updated_at"))
                output = task.outputs[output_index]
            try:
                manifest = verified_task_manifest(task, output_index)
            except (OSError, ProvenanceError, ValueError) as exc:
                raise ValidationError({"output_index": f"图片来源校验失败：{exc}"}) from exc

            source_shot = task.generated_workflow.prompt_artifact.shot
            display_name = requested_name or (
                f"{source_shot.title} · 生成定帧" if source_shot and source_shot.title
                else f"生成定帧 {str(task.id)[:8]}-{output_index + 1}"
            )
            suffix = source_path.suffix.lower() or ".png"
            asset = Asset(
                owner=task.owner,
                style=task.project.style,
                kind=reference_category or Asset.Kind.OUTPUT_IMAGE,
                name=display_name[:160],
                description=(
                    f"由本项目已完成图片任务生成，并绑定为 {entity.name} 的{asset_kind_label}分类参考图。"
                    if entity is not None
                    else "由本项目已完成图片任务生成，可在后续分镜中选择为完整首帧或尾帧锚点。"
                ),
                prompt_hint="Use this generated frame as a strict visual continuity anchor.",
                mime_type=mimetypes.guess_type(source_path.name)[0] or "image/png",
                checksum=str((manifest.get("artifact") or {}).get("sha256") or sha256_file(source_path)),
                metadata={
                    "generated_from": {
                        "task_id": str(task.id),
                        "output_index": output_index,
                        "project_id": str(task.project_id),
                        "episode_id": str(task.episode_id) if task.episode_id else None,
                        "shot_id": str(source_shot.id) if source_shot else None,
                        "artifact_sha256": str((manifest.get("artifact") or {}).get("sha256") or ""),
                        "manifest_sha256": str((manifest.get("verification") or {}).get("manifest_sha256") or ""),
                        "reference_category": reference_category or None,
                        "entity_id": str(entity.id) if entity is not None else None,
                    },
                    "reference_category": reference_category or None,
                    "reference_entity_id": str(entity.id) if entity is not None else None,
                },
            )
            try:
                with source_path.open("rb") as source_stream:
                    asset.file.save(
                        f"generated-{task.id}-{output_index}{suffix}",
                        File(source_stream),
                        save=False,
                    )
                asset.save()
            except OSError as exc:
                raise ValidationError({"output_index": f"复制图片到素材库失败：{exc}"}) from exc

            copied_path = Path(asset.file.path)
            copied_hash = sha256_file(copied_path)
            if copied_hash != asset.checksum:
                # The database transaction can roll back the Asset row, but
                # FileField storage is external to the transaction. Remove the
                # just-created copy explicitly so a failed integrity check does
                # not leave an orphaned tenant file behind.
                asset.file.delete(save=False)
                asset.delete()
                raise ValidationError({"output_index": "复制后的素材哈希与生成母版不一致"})
            if entity is not None:
                entity.reference_assets.add(asset)
            outputs = [dict(item) if isinstance(item, dict) else item for item in task.outputs]
            outputs[output_index]["promoted_asset_id"] = str(asset.id)
            outputs[output_index]["promoted_asset_target"] = requested_target
            task.outputs = outputs
            task.save(update_fields=("outputs", "updated_at"))

        return Response(
            AssetSerializer(asset, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {"output_index": {"type": "integer", "minimum": 0}},
                "required": ["output_index"],
            }
        },
        responses={200: GenerationTaskSerializer},
        description=(
            "Idempotently create a managed public distribution copy for one successful "
            "video/image output. The ComfyUI production master is never modified."
        ),
    )
    @action(detail=True, methods=["post"], url_path="publish-output")
    def publish_output(self, request, pk=None):
        task = self.get_object()
        output_index = request.data.get("output_index")
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            raise ValidationError({"output_index": "output_index must be a non-negative integer"})
        try:
            prepare_task_distribution(task, output_index)
        except DistributionError as exc:
            raise ValidationError({"distribution": str(exc)}) from exc
        task.refresh_from_db()
        return Response(self.get_serializer(task).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="workflow_format",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["api", "ui"],
                description=(
                    "Export format. api (default) preserves the exact persisted ComfyUI API prompt; "
                    "ui creates an importable ComfyUI canvas using the connected server's object_info schema."
                ),
            ),
            OpenApiParameter(
                name="download",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Return the selected workflow format as a JSON attachment.",
            ),
        ],
        responses={200: ComfyUIWorkflowExportSerializer},
        description=(
            "Inspect or download the task's ComfyUI workflow. The default API format is the exact "
            "persisted prompt graph for backward compatibility. workflow_format=ui creates a real canvas JSON "
            "that can be opened in ComfyUI. This endpoint is read-only and never submits work."
        ),
    )
    @action(detail=True, methods=["get"], url_path="comfyui-workflow")
    def comfyui_workflow(self, request, pk=None):
        task = self.get_object()
        generated_workflow = task.generated_workflow
        # Do not use the generic ``format`` query key: Django REST Framework
        # reserves it for renderer negotiation before this action is invoked.
        requested_format = str(request.query_params.get("workflow_format", "api")).strip().casefold()
        if requested_format in {"", "api", "comfyui_api"}:
            export_format = "comfyui_api"
        elif requested_format in {"ui", "canvas", "comfyui_ui"}:
            export_format = "comfyui_ui"
        else:
            raise ValidationError({
                "workflow_format": (
                    "workflow_format 仅支持 api/comfyui_api 或 ui/canvas/comfyui_ui。"
                )
            })
        api_workflow, missing_tokens = _resolved_task_api_workflow(task)
        if export_format == "comfyui_ui":
            try:
                object_info = ComfyUIClient().object_info()
            except ComfyUIError as exc:
                return Response(
                    {
                        "detail": (
                            "无法读取 ComfyUI /object_info，暂时不能生成画布格式；"
                            "API 图仍可用默认格式导出。"
                        ),
                        "error": str(exc),
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                workflow = api_graph_to_ui_workflow(
                    api_workflow,
                    object_info,
                    workflow_id=generated_workflow.id,
                    metadata={
                        "task_id": str(task.id),
                        "template_slug": generated_workflow.template.slug,
                        "template_version": task.workflow_version,
                    },
                )
            except ComfyUICanvasExportError as exc:
                raise ValidationError({"workflow": str(exc)}) from exc
            filename = f"autoprompt-task-{task.id}-comfyui-ui.json"
        else:
            workflow = api_workflow
            filename = f"autoprompt-task-{task.id}-comfyui-api.json"

        if str(request.query_params.get("download", "")).casefold() in {"1", "true", "yes"}:
            response = HttpResponse(
                json.dumps(workflow, ensure_ascii=False, indent=2),
                content_type="application/json; charset=utf-8",
            )
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response

        comfyui_url = _safe_comfyui_browser_url()
        staged_placeholder_count = _count_staged_asset_tokens(api_workflow)
        graph_valid = (
            is_comfyui_ui_workflow(workflow)
            if export_format == "comfyui_ui"
            else _is_comfyui_api_graph(workflow)
        )
        import_ready = graph_valid and staged_placeholder_count == 0
        open_reason = (
            "ComfyUI 画布 JSON 已就绪，可下载后直接拖入或从 ComfyUI 工作流菜单打开。"
            if import_ready and export_format == "comfyui_ui"
            else "工作流 API JSON 已就绪，可用于 API 调试或队列提交；如需查看画布请选择 ui 格式。"
            if import_ready
            else "任务参考素材尚未全部安全上传并暂存到 ComfyUI；请等待任务进入 prepared/submitted 后刷新。"
            if staged_placeholder_count
            else "工作流内容不是有效的所选 ComfyUI 导出格式。"
        )
        api_download_url = request.build_absolute_uri(f"{request.path}?download=true")
        ui_download_url = request.build_absolute_uri(
            f"{request.path}?workflow_format=ui&download=true"
        )
        payload = {
            "task_id": task.id,
            "generated_workflow_id": generated_workflow.id,
            "format": export_format,
            "filename": filename,
            "content_hash": generated_workflow.content_hash,
            "template_slug": generated_workflow.template.slug,
            "template_version": task.workflow_version,
            "workflow": workflow,
            "import_ready": import_ready,
            "open_reason": open_reason,
            "contains_staged_placeholders": staged_placeholder_count > 0,
            "staged_placeholder_count": staged_placeholder_count,
            "comfyui_url": comfyui_url,
            "safe_to_open": bool(comfyui_url),
            "direct_open_supported": False,
            "direct_open_url": None,
            "open_mode": "download_import",
            "download_url": ui_download_url if export_format == "comfyui_ui" else api_download_url,
            "available_formats": ["comfyui_api", "comfyui_ui"],
            "api_download_url": api_download_url,
            "ui_download_url": ui_download_url,
        }
        serializer = ComfyUIWorkflowExportSerializer(payload)
        response = Response(serializer.data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @extend_schema(
        request=None,
        responses={200: ComfyUIWorkflowSyncSerializer},
        description=(
            "Explicitly save the task's importable UI canvas under "
            "workflows/autoprompt in the connected ComfyUI user library. "
            "This does not queue or execute the workflow."
        ),
    )
    @action(detail=True, methods=["post"], url_path="sync-comfyui-workflow")
    def sync_comfyui_workflow(self, request, pk=None):
        task = self.get_object()
        api_workflow, missing_tokens = _resolved_task_api_workflow(task)
        if missing_tokens:
            raise ValidationError({
                "workflow": (
                    f"任务仍有 {len(missing_tokens)} 个参考素材尚未暂存到 ComfyUI；"
                    "请等待任务完成准备或提交后再同步。"
                )
            })

        client = ComfyUIClient()
        try:
            object_info = client.object_info()
            canvas = api_graph_to_ui_workflow(
                api_workflow,
                object_info,
                workflow_id=task.generated_workflow_id,
                metadata={
                    "task_id": str(task.id),
                    "template_slug": task.generated_workflow.template.slug,
                    "template_version": task.workflow_version,
                },
            )
            if not is_comfyui_ui_workflow(canvas):
                raise ComfyUICanvasExportError("转换结果不是有效的 ComfyUI UI 画布")
            filename = f"autoprompt-task-{task.id}-comfyui-ui.json"
            saved = client.save_autoprompt_workflow(canvas, filename=filename)
        except (ComfyUIError, ComfyUICanvasExportError) as exc:
            return Response(
                {
                    "detail": "工作流尚未同步到 ComfyUI；任务本身和执行队列未被修改。",
                    "error": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        comfyui_url = _safe_comfyui_browser_url()
        payload = {
            "task_id": task.id,
            "generated_workflow_id": task.generated_workflow_id,
            "synced": True,
            "workflow_path": saved["workflow_path"],
            "filename": filename,
            "comfyui_url": comfyui_url,
            "open_reason": (
                f"已保存到 ComfyUI 工作流库：{saved['workflow_path']}。"
                "打开或刷新 ComfyUI 后，可在 workflows/autoprompt 中查看；平台不会自动运行它。"
            ),
        }
        serializer = ComfyUIWorkflowSyncSerializer(payload)
        response = Response(serializer.data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        task = self.get_object()
        if task.status in {GenerationTask.Status.SUCCEEDED, GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}:
            raise ValidationError("任务已结束，不能取消")
        if task.status in {
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        } and task.comfy_prompt_id:
            if task.status == GenerationTask.Status.RUNNING:
                task.status = GenerationTask.Status.CANCEL_REQUESTED
                task.heartbeat_at = timezone.now()
                task.remote_state = GenerationTask.RemoteState.CANCELLING
                task.remote_error = ""
                task.save(update_fields=(
                    "status", "heartbeat_at", "remote_state", "remote_error", "updated_at",
                ))
            client = ComfyUIClient()
            cancel_warning = ""
            try:
                client.cancel(task.comfy_prompt_id)
            except ComfyUIError as exc:
                cancel_warning = str(exc)
            try:
                remote_history = client.history(task.comfy_prompt_id)
                still_queued = client.is_queued(task.comfy_prompt_id)
            except ComfyUIError as exc:
                task.remote_error = cancel_warning or str(exc)
                task.save(update_fields=("remote_error", "updated_at"))
                return Response({
                    "status": task.status,
                    "warning": task.remote_error,
                }, status=status.HTTP_202_ACCEPTED)
            if remote_history is None and still_queued:
                task.remote_error = cancel_warning
                task.save(update_fields=("remote_error", "updated_at"))
                return Response({
                    "status": task.status,
                    "warning": cancel_warning or "ComfyUI 已收到取消请求，正在等待远端任务退出",
                }, status=status.HTTP_202_ACCEPTED)
        task.status = GenerationTask.Status.CANCELLED
        task.finished_at = timezone.now()
        task.heartbeat_at = task.finished_at
        task.progress = 0
        task.remote_state = GenerationTask.RemoteState.CANCELLED
        task.remote_error = ""
        if task.submission_token:
            task.submission_state = GenerationTask.SubmissionState.TERMINAL_CANCELLED
        task.save(update_fields=(
            "status", "finished_at", "heartbeat_at", "progress", "remote_state",
            "remote_error", "submission_state", "updated_at",
        ))
        return Response(self.get_serializer(task).data)

    @extend_schema(request=None, responses={201: GenerationTaskSerializer})
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        source = self.get_object()
        with transaction.atomic():
            task = self.get_queryset().select_for_update().get(pk=source.pk)
            if not _can_execute_template(task.generated_workflow.template, request.user):
                raise ValidationError("The workflow template is not executable by this user.")
            if task.status not in {GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}:
                raise ValidationError("Only failed or cancelled tasks can be retried.")
            if task.outputs:
                raise ValidationError("A task with successful outputs cannot be retried.")
            existing_retry = GenerationTask.objects.filter(
                owner=task.owner,
                capability_snapshot__retry_of=str(task.id),
            ).order_by("queued_at").first()
            if existing_retry:
                raise ValidationError({
                    "retry_task_id": str(existing_retry.id),
                    "detail": "This failed task has already been retried.",
                })
            _require_prompt_language_gate_for_enqueue(
                task.generated_workflow.prompt_artifact,
                workflow_id=str(task.generated_workflow_id),
                retry_of=str(task.id),
                episode_id=str(task.episode_id) if task.episode_id else None,
                shot_ids=list(
                    (task.generated_workflow.prompt_artifact.request_options or {}).get("shot_ids")
                    or []
                ),
            )
            if (
                task.submission_token
                and task.submission_state not in {
                    GenerationTask.SubmissionState.TERMINAL_FAILED,
                    GenerationTask.SubmissionState.TERMINAL_CANCELLED,
                }
            ):
                try:
                    client = ComfyUIClient()
                    client.health()
                    remote_history = client.history(task.submission_token)
                    remote_queued = client.is_queued(task.submission_token)
                except ComfyUIError as exc:
                    raise ValidationError(
                        "The previous submission token cannot be reconciled while ComfyUI is unhealthy; "
                        "retry is blocked to prevent duplicate GPU work. " + str(exc)
                    ) from exc
                if remote_history is not None:
                    remote_status = remote_history.get("status", {}) if isinstance(remote_history, dict) else {}
                    if (
                        remote_status.get("status_str") == "error"
                        or remote_status.get("completed") is False
                    ):
                        task.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
                        task.remote_state = GenerationTask.RemoteState.FAILED
                        task.remote_error = "Reconciled a definitive remote execution failure before retry"
                    else:
                        task.remote_state = GenerationTask.RemoteState.SUCCEEDED
                        task.remote_error = "Remote history contains a completed result; recover outputs instead of retrying"
                        task.save(update_fields=("remote_state", "remote_error", "updated_at"))
                        raise ValidationError(
                            "The previous ComfyUI prompt already has terminal output; retry is blocked "
                            "to prevent duplicate GPU work."
                        )
                elif remote_queued:
                    raise ValidationError(
                        "The previous ComfyUI prompt is still queued or running; "
                        "retry is blocked to prevent duplicate GPU work."
                    )
                else:
                    # A healthy, restarted ComfyUI that reports the token in
                    # neither queue nor history proves the old in-memory job is
                    # gone.  This is the safe recovery path after a sticky OOM.
                    task.submission_state = GenerationTask.SubmissionState.TERMINAL_CANCELLED
                    task.remote_state = GenerationTask.RemoteState.MISSING
                    task.remote_error = "Remote prompt absent after successful ComfyUI reconciliation"
                task.heartbeat_at = timezone.now()
                task.save(update_fields=(
                    "submission_state", "heartbeat_at", "remote_state", "remote_error", "updated_at",
                ))
            _assert_queue_capacity(request.user)
            retry_task = _clone_generation_task_for_retry(task)
        return Response(self.get_serializer(retry_task).data, status=status.HTTP_201_CREATED)


class AssemblyJobViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AssemblyJobSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "scope", "status", "dry_run")
    ordering_fields = ("created_at", "started_at", "finished_at")

    def get_queryset(self):
        queryset = AssemblyJob.objects.select_related("project", "episode", "owner").prefetch_related("source_tasks")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    @extend_schema(request=AssembleEpisodeSerializer, responses={201: AssemblyJobSerializer})
    @action(detail=False, methods=["post"], url_path="episode")
    def assemble_episode(self, request):
        serializer = AssembleEpisodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        queryset = Episode.objects.select_related("project")
        if not request.user.is_staff:
            queryset = queryset.filter(project__owner=request.user)
        try:
            episode = queryset.get(pk=data["episode_id"])
        except Episode.DoesNotExist as exc:
            raise NotFound("剧集不存在") from exc
        return self._assemble(
            request, project=episode.project, episode=episode,
            dry_run=data["dry_run"], output_name=data["output_name"],
        )

    @extend_schema(request=AssembleSeriesSerializer, responses={201: AssemblyJobSerializer})
    @action(detail=False, methods=["post"], url_path="series")
    def assemble_series(self, request):
        serializer = AssembleSeriesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            project = _owned_projects(SeriesProject.objects.all(), request.user).get(pk=data["project_id"])
        except SeriesProject.DoesNotExist as exc:
            raise NotFound("连续剧项目不存在") from exc
        return self._assemble(
            request, project=project, episode=None,
            dry_run=data["dry_run"], output_name=data["output_name"],
        )

    def _assemble(self, request, **kwargs):
        try:
            job = create_assembly(owner=request.user, **kwargs)
        except AssemblyError as exc:
            if exc.job:
                return Response({
                    "error": str(exc),
                    "assembly": self.get_serializer(exc.job).data,
                }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(job).data, status=status.HTTP_201_CREATED)


class ReleaseBundleViewSet(viewsets.ModelViewSet):
    serializer_class = ReleaseBundleSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "status")
    ordering_fields = ("version", "created_at", "sealed_at", "released_at")

    def get_queryset(self):
        queryset = ReleaseBundle.objects.select_related("project", "owner", "created_by")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    @extend_schema(request=ReleaseBundleCreateSerializer, responses={201: ReleaseBundleSerializer})
    def create(self, request, *args, **kwargs):
        payload = ReleaseBundleCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            project = _owned_projects(SeriesProject.objects.all(), request.user).get(
                pk=data["project_id"],
            )
        except SeriesProject.DoesNotExist as exc:
            raise NotFound("连续剧项目不存在") from exc
        bundle = create_release_draft(
            project=project,
            owner=project.owner,
            created_by=request.user,
            title=data.get("title", ""),
            notes=data.get("notes", ""),
        )
        if "selection" in data:
            try:
                bundle.selection = normalize_release_selection(data["selection"])
            except ReleaseBundleError as exc:
                bundle.delete()
                raise ValidationError({"selection": str(exc), "details": exc.details}) from exc
            bundle.save(update_fields=("selection", "updated_at"))
        return Response(self.get_serializer(bundle).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        with transaction.atomic():
            bundle = self.get_queryset().select_for_update().get(pk=self.get_object().pk)
            if bundle.status != ReleaseBundle.Status.DRAFT:
                raise ValidationError("已封存或已发行的发行清单不可修改")
            serializer = self.get_serializer(bundle, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            if "selection" in serializer.validated_data:
                try:
                    serializer.validated_data["selection"] = normalize_release_selection(
                        serializer.validated_data["selection"],
                    )
                except ReleaseBundleError as exc:
                    raise ValidationError({"selection": str(exc), "details": exc.details}) from exc
            serializer.save()
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            bundle = self.get_queryset().select_for_update().get(pk=self.get_object().pk)
            if bundle.status != ReleaseBundle.Status.DRAFT:
                raise ValidationError("只有草稿发行候选可以删除")
            bundle.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=ReleaseBundleProposeSerializer, responses=dict)
    @action(detail=False, methods=["post"], url_path="propose")
    def propose(self, request):
        payload = ReleaseBundleProposeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            project = _owned_projects(SeriesProject.objects.all(), request.user).get(
                pk=payload.validated_data["project_id"],
            )
        except SeriesProject.DoesNotExist as exc:
            raise NotFound("连续剧项目不存在") from exc
        return Response({"project_id": str(project.id), "selection": propose_release_selection(project)})

    @extend_schema(request=None, responses={200: ReleaseBundleSerializer})
    @action(detail=True, methods=["post"], url_path="seal")
    def seal(self, request, pk=None):
        bundle = self.get_object()
        try:
            bundle = seal_release_bundle(bundle)
        except ReleaseBundleError as exc:
            raise ValidationError({"error": str(exc), "details": exc.details}) from exc
        return Response(self.get_serializer(bundle).data)

    @extend_schema(request=None, responses={200: ReleaseBundleSerializer})
    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        if not request.user.is_staff:
            raise PermissionDenied("只有 staff 可以确认公开发行")
        bundle = self.get_object()
        try:
            bundle = mark_release_bundle_released(bundle)
        except ReleaseBundleError as exc:
            raise ValidationError({"error": str(exc), "details": exc.details}) from exc
        return Response(self.get_serializer(bundle).data)

    @extend_schema(responses=dict)
    @action(detail=True, methods=["get"], url_path="verify")
    def verify(self, request, pk=None):
        try:
            document = live_verified_release_bundle(self.get_object())
        except ReleaseBundleError as exc:
            raise ValidationError({"error": str(exc), "details": exc.details}) from exc
        return Response(document)

class ComfyUIHealthView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        try:
            client = ComfyUIClient()
        except ComfyUIError as exc:
            return Response({
                "reachable": False,
                "degraded": False,
                "system_stats": None,
                "error": str(exc),
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            system_stats = client.health()
        except ComfyUIError as health_exc:
            try:
                queue = client.queue()
            except ComfyUIError as queue_exc:
                return Response({
                    "reachable": False,
                    "degraded": False,
                    "system_stats": None,
                    "error": str(health_exc),
                    "queue_error": str(queue_exc),
                }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            return Response({
                "reachable": True,
                "degraded": True,
                "system_stats": None,
                "error": str(health_exc),
                "queue_stats": {
                    "running": len(queue.get("queue_running") or []),
                    "pending": len(queue.get("queue_pending") or []),
                },
            })
        return Response({
            "reachable": True,
            "degraded": False,
            "system_stats": system_stats,
        })
