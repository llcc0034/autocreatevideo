from __future__ import annotations

import json
import hashlib
import logging
import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema
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
    DeferredGenerationRequest,
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
    ShotDetailDraftBatchItem,
    Style,
    WorkflowTemplate,
)
from studio.bible_image_candidate_serializers import BibleImageCandidateErrorResponseSerializer
from studio.permissions import IsOwnerOrStaff, IsOwnerOrStaffOrGlobalReadOnly, IsStaffOrReadOnly
from studio.media_signing import classify_task_output, load_signed_media_token, signed_media_token
from studio.serializers import (
    APIValidationErrorEnvelopeSerializer,
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
    GenerationTaskRetrySerializer,
    GenerationTaskSerializer,
    PromptFieldTranslationSerializer,
    PromptTranslationConflictErrorResponseSerializer,
    PromptTranslationBatchRequestSerializer,
    PromptTranslationConfirmSerializer,
    PromptTranslationPromoteShotResponseSerializer,
    PromptTranslationPromoteShotSerializer,
    PromptTranslationRetranslateSerializer,
    TaskOutputPromoteSerializer,
    NextEpisodeSerializer,
    ProductionBatchResumeResponseSerializer,
    ProductionBatchCancelResponseSerializer,
    ProductionBatchDeleteResponseSerializer,
    ProductionBuildSerializer,
    ProductionBuildResponseSerializer,
    ProductionIdempotencyConflictErrorResponseSerializer,
    ProductionQueueConfirmationErrorResponseSerializer,
    ProductionWorkflowTemplateSerializer,
    ProjectBibleSyncSerializer,
    ProjectDeleteErrorResponseSerializer,
    PromptArtifactSerializer,
    PromptGenerateSerializer,
    PropBibleSerializer,
    RegisterSerializer,
    ReleaseBundleCreateSerializer,
    ReleaseBundleProposeSerializer,
    ReleaseBundleSerializer,
    SceneBibleSerializer,
    ScriptBibleApplyRequestSerializer,
    ScriptBibleApplyResponseSerializer,
    ScriptBibleCandidatesResponseSerializer,
    ScriptBibleErrorEnvelopeSerializer,
    ScriptBreakdownSerializer,
    ScriptBreakdownResponseSerializer,
    ScriptRebuildConflictResponseSerializer,
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
    PROMPT_COMPILER_VERSION,
    PROMPT_INPUT_FINGERPRINT_VERSION,
    PromptBuildError,
    capture_consistency_snapshot,
    load_shots,
    sync_consistency_locks,
)
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.bible_image_candidates import (
    BibleImageCandidateAPIError,
    assert_bible_image_candidate_source_current,
    bible_image_candidate_promotion_metadata,
    validated_frozen_bible_target,
)
from studio.services.production_pipeline import (
    BIBLE_IMAGE_CANDIDATE_PURPOSE,
    BibleImageCandidateReservedOperation,
    ProductionIdempotencyConflict,
    assert_queue_capacity,
    can_execute_template,
    clone_generation_task_for_retry,
    compile_prompt_input,
    executable_templates,
    materialize_compiled_prompt,
    materialize_generated_workflow,
    materialize_generation_task,
)
from studio.services.production_batch_slots import (
    build_production_batch_slots,
    effective_batch_snapshot_for_task,
    production_batch_task_queryset,
)
from studio.services.deferred_generation import (
    active_first_frame_deferred_for,
    cascade_cancel_downstream_generation,
    ensure_auto_first_frame_deferred,
    latest_viable_video_task_for_shot,
    previous_video_shot_for,
    target_requires_deferred_first_frame,
)
from studio.services.shot_prompt_recovery import repair_legacy_negative_prompt
from studio.services.shot_versions import (
    RESERVED_SHOT_VERSION_METADATA_KEYS,
    ShotVersionResolutionError,
    legacy_shot_version_key,
    normalize_requested_shot_version_key,
    resolve_episode_shot_version,
    shot_version_key as canonical_shot_version_key,
    shot_version_label,
    shot_version_number,
)
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
    missing_relevant_canonical_identity_mappings,
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
from studio.services.workflow import WorkflowBuildError
from studio.services.script_breakdown import (
    ConfirmedScriptBibleAliases,
    ScriptRebuildConflict,
    breakdown_script,
    discard_rebuildable_production,
)
from studio.services.script_bible import (
    SCRIPT_BIBLE_SCHEMA_VERSION,
    ScriptBibleParseError,
    ScriptBibleProvenanceProtectedError,
    classify_script_bible_candidates,
    parse_script_bible,
    script_bible_reviewed_content_sha256,
    validate_untrusted_script_bible_metadata,
    validate_script_bible_ir,
)
from studio.services.resolution import (
    H3_RESOLUTION_MULTIPLE_DEFAULT,
    ResolutionProfileError,
    resolve_production_resolution,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    normalize_shot_reference_bindings,
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


logger = logging.getLogger(__name__)


class _PromotionFileCommitGuard:
    """Remove a newly copied file unless the surrounding DB transaction commits."""

    def __init__(self):
        self._storage = None
        self._name = ""

    def __enter__(self):
        return self

    def track(self, field_file) -> None:
        self._storage = field_file.storage
        self._name = str(field_file.name or "")

    def __exit__(self, exc_type, exc, traceback):
        # ``transaction.atomic`` is the inner context manager, so database
        # errors raised by a write or by the outermost commit reach this guard.
        # A successful nested atomic block may still sit inside Django's test
        # transaction; treating the absence of ``on_commit`` as failure would
        # therefore delete a valid file.
        if exc_type is not None and self._storage is not None and self._name:
            try:
                self._storage.delete(self._name)
            except Exception as cleanup_exc:  # pragma: no cover - storage backend dependent
                logger.warning(
                    "Failed to clean rolled-back promoted asset file",
                    extra={"error_type": type(cleanup_exc).__name__},
                )
        return False

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


def _is_dedicated_project_cover(asset: Asset) -> bool:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    return (
        asset.owner_id is not None
        and asset.kind == Asset.Kind.REFERENCE
        and metadata.get("usage") == "project_cover"
        and bool(asset.file)
    )


def _delete_file_after_commit(asset: Asset) -> None:
    if not asset.file:
        return
    storage = asset.file.storage
    name = asset.file.name
    if name:
        transaction.on_commit(
            lambda storage=storage, name=name: storage.delete(name),
            robust=True,
        )


def _asset_structured_reference_labels(asset: Asset) -> list[str]:
    labels: list[str] = []
    if asset.cover_projects.exists():
        labels.append("连续剧封面")
    bible_models = (
        (CharacterBible, "人物 Bible"),
        (SceneBible, "场景 Bible"),
        (PropBible, "道具 Bible"),
    )
    for model, label in bible_models:
        if model.objects.filter(
            Q(anchor_asset_id=asset.id) | Q(reference_assets=asset),
        ).exists():
            labels.append(label)
    if Shot.objects.filter(reference_assets=asset).exists():
        labels.append("分镜参考")
    return labels


def _json_contains_asset_id(value, asset_id: str) -> bool:
    if isinstance(value, dict):
        return any(
            (key in {"asset_id", "audio_asset_id", "paired_video_asset_id", "id"} and str(item) == asset_id)
            or _json_contains_asset_id(item, asset_id)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_json_contains_asset_id(item, asset_id) for item in value)
    return False


def _asset_has_active_task_reference(asset: Asset) -> bool:
    active_statuses = (
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )
    asset_id = str(asset.id)
    tasks = GenerationTask.objects.filter(
        owner_id=asset.owner_id,
        status__in=active_statuses,
    ).select_related("generated_workflow__prompt_artifact")
    for task in tasks:
        prompt = task.generated_workflow.prompt_artifact
        if any(
            _json_contains_asset_id(value, asset_id)
            for value in (
                task.staged_inputs,
                task.capability_snapshot,
                prompt.structured_payload,
                prompt.consistency_snapshot,
                prompt.request_options,
            )
        ):
            return True
    return False


def _asset_reference_conflict_message(asset: Asset) -> str:
    if asset_is_release_evidence(asset):
        return "该素材已进入封存或历史发行清单，不能修改类别或删除；可停用但必须保留审计证据。"
    labels = _asset_structured_reference_labels(asset)
    if labels:
        return f"该素材已用于{'、'.join(labels)}，请先解除引用后再修改类别或删除。"
    if _asset_has_active_task_reference(asset):
        return "该素材正被排队、运行或取消中的生成任务使用，请等待任务终结后再修改类别或删除。"
    return ""


def _delete_unused_project_cover(asset_id) -> None:
    if not asset_id:
        return
    asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
    if not asset or not _is_dedicated_project_cover(asset):
        return
    if (
        asset.cover_projects.exists()
        or CharacterBible.objects.filter(
            Q(anchor_asset_id=asset.id) | Q(reference_assets=asset),
        ).exists()
        or SceneBible.objects.filter(
            Q(anchor_asset_id=asset.id) | Q(reference_assets=asset),
        ).exists()
        or PropBible.objects.filter(
            Q(anchor_asset_id=asset.id) | Q(reference_assets=asset),
        ).exists()
        or Shot.objects.filter(reference_assets=asset).exists()
        or asset_is_release_evidence(asset)
    ):
        return
    _delete_file_after_commit(asset)
    asset.delete()


def _production_workflow_selection_snapshot(
    data: dict, *, canonical: bool = True,
) -> list[dict]:
    selections = [
        {
            **selection,
            "template_id": str(selection["template_id"]),
        }
        for selection in data.get("workflow_selections", [])
    ]
    if canonical:
        selections.sort(key=lambda selection: (
            str(selection.get("output_kind") or ""),
            str(selection.get("h3_mode") or ""),
            str(selection.get("template_id") or ""),
        ))
    return selections


def _canonical_fingerprint_value(value):
    """Sort unordered snapshot collections into one deterministic JSON value."""
    if isinstance(value, dict):
        return {
            key: _canonical_fingerprint_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        normalized = [_canonical_fingerprint_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ),
        )
    return value


def _episode_batch_fingerprint(
    *, project: SeriesProject, episode: Episode, shots: list[Shot], data: dict,
    consistency_snapshot: dict, canonical: bool = True,
    canonical_consistency: bool | None = None,
) -> str:
    """Fingerprint the requested batch and the ordered source-shot snapshot.

    The project row is locked before this is called, so the fingerprint and
    any tasks created from it describe one consistent editor snapshot.
    """
    if canonical_consistency is None:
        canonical_consistency = canonical
    payload = {
        "version": 2,
        "project_id": str(project.id),
        "episode_id": str(episode.id),
        "shot_version_key": data["shot_version_key"],
        "batch_shot_ids": [str(shot.id) for shot in shots],
        "episode": {
            "number": episode.number,
            "title": episode.title,
            "synopsis": episode.synopsis,
            "script": episode.script,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings,
        },
        "consistency_snapshot": (
            _canonical_fingerprint_value(consistency_snapshot)
            if canonical_consistency else consistency_snapshot
        ),
        "whole_episode": True,
        "template_id": str(data["template_id"]) if data.get("template_id") else None,
        "priority": data["priority"],
        "max_attempts": data["max_attempts"],
        "resolution_profile": data["resolution_profile"],
        "resolution_multiple": data["resolution_multiple"],
        "image_resolution_profile": data["image_resolution_profile"],
        "image_resolution_multiple": data["image_resolution_multiple"],
        "workflow_overrides": data["workflow_overrides"],
        "workflow_selections": _production_workflow_selection_snapshot(
            data, canonical=canonical,
        ),
        "shots": [
            {
                "id": str(shot.id),
                "number": shot_version_number(shot),
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
                "character_ids": (
                    sorted(str(item.id) for item in shot.characters.all())
                    if canonical else [str(item.id) for item in shot.characters.all()]
                ),
                "prop_ids": (
                    sorted(str(item.id) for item in shot.props.all())
                    if canonical else [str(item.id) for item in shot.props.all()]
                ),
                "reference_asset_ids": (
                    sorted(str(item.id) for item in shot.reference_assets.all())
                    if canonical else [str(item.id) for item in shot.reference_assets.all()]
                ),
                "reference_bindings": shot.reference_bindings or {},
                "requires_first_frame_continuity": shot.requires_first_frame_continuity,
                "requires_last_frame_continuity": shot.requires_last_frame_continuity,
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


def _series_batch_fingerprint(
    *, project: SeriesProject, episodes: list[Episode],
    production_groups_by_episode: dict[uuid.UUID, tuple[list[Shot], list[list[Shot]]]],
    planned_units: list[dict], data: dict, consistency_snapshot: dict,
) -> str:
    """Fingerprint one resolved multi-episode/selection production plan.

    ``planned_units`` contains the exact template and effective workflow
    parameters selected for every child. Together with the ordered editor
    snapshot below, this prevents an idempotency replay from silently adopting
    a later template/default or a changed episode/shot fact.
    """
    workflow_selections = _production_workflow_selection_snapshot(data)
    payload = {
        "version": 1,
        "scope": "series",
        "project": {
            "id": str(project.id),
            "title": project.title,
            "logline": project.logline,
            "master_script": project.master_script,
            "language": project.language,
            "target_model": project.target_model,
            "aspect_ratio": project.aspect_ratio,
            "width": project.width,
            "height": project.height,
            "fps": project.fps,
            "default_shot_duration_ms": project.default_shot_duration_ms,
            "continuity_seed": project.continuity_seed,
            "continuity_summary": project.continuity_summary,
            "production_settings": project.production_settings,
        },
        "consistency_snapshot": _canonical_fingerprint_value(consistency_snapshot),
        "request": {
            "whole_episode": False,
            "multi_shot": data["multi_shot"],
            "template_id": str(data["template_id"]) if data.get("template_id") else None,
            "priority": data["priority"],
            "max_attempts": data["max_attempts"],
            "resolution_profile": data["resolution_profile"],
            "resolution_multiple": data["resolution_multiple"],
            "image_resolution_profile": data["image_resolution_profile"],
            "image_resolution_multiple": data["image_resolution_multiple"],
            "workflow_overrides": data["workflow_overrides"],
            "workflow_selections": workflow_selections,
        },
        "episodes": [],
        "production_units": planned_units,
    }
    for episode in episodes:
        shots, groups = production_groups_by_episode.get(episode.id, ([], []))
        payload["episodes"].append({
            "id": str(episode.id),
            "number": episode.number,
            "title": episode.title,
            "synopsis": episode.synopsis,
            "script": episode.script,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings,
            "groups": [[str(shot.id) for shot in group] for group in groups],
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
                    "character_ids": sorted(
                        str(item.id) for item in shot.characters.all()
                    ),
                    "prop_ids": sorted(str(item.id) for item in shot.props.all()),
                    "reference_asset_ids": sorted(
                        str(item.id) for item in shot.reference_assets.all()
                    ),
                    "reference_bindings": shot.reference_bindings or {},
                    "requires_first_frame_continuity": shot.requires_first_frame_continuity,
                    "requires_last_frame_continuity": shot.requires_last_frame_continuity,
                    "continuity_notes": shot.continuity_notes,
                    "metadata": shot.metadata,
                }
                for shot in shots
            ],
        })
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _production_batch_tasks(
    *, user, project: SeriesProject, idempotency_key: str,
) -> list[GenerationTask]:
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
        "shot_version_key": capability.get("shot_version_key"),
        "shot_version_label": capability.get("shot_version_label", ""),
        "batch_shot_ids": [
            str(value) for value in capability.get("batch_shot_ids", [])
        ],
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


def _production_unit_from_deferred_request(
    *,
    episode: Episode,
    target_shot: Shot,
    deferred_request: DeferredGenerationRequest,
    source_task: GenerationTask | None,
    batch_id: str | None,
    batch_kind: str | None,
    batch_index: int | None,
    batch_size: int,
    batch_shot_ids: list[str] | None = None,
) -> dict:
    return {
        "batch_id": batch_id,
        "batch_kind": batch_kind or None,
        "batch_index": batch_index,
        "batch_size": batch_size,
        "shot_version_key": canonical_shot_version_key(target_shot),
        "shot_version_label": shot_version_label(target_shot),
        "batch_shot_ids": list(batch_shot_ids or []),
        "episode_id": str(episode.id),
        "episode_number": episode.number,
        "episode_title": episode.title,
        "shot_ids": [str(target_shot.id)],
        "shot_id": str(target_shot.id),
        "shot_number": shot_version_number(target_shot),
        "shot_order": shot_version_number(target_shot),
        "shot_title": target_shot.title,
        "media_type": target_shot.media_type,
        "h3_mode": target_shot.h3_mode,
        "effective_conditioning_mode": None,
        "prompt_template_key": None,
        "workflow_mode": "deferred_first_frame_continuity",
        "source_workflow": None,
        "workflow_parameters": {},
        "resolution_profile": None,
        "resolution_multiple": None,
        "width": None,
        "height": None,
        "include_audio": target_shot.include_audio,
        "requested_duration_ms": target_shot.duration_ms,
        "effective_duration_ms": 0,
        "fps": None,
        "template_id": str(deferred_request.target_template_id),
        "template_slug": "",
        "prompt_artifact_id": None,
        "prompt_artifact_reused": False,
        "workflow_id": None,
        "task_id": None,
        "task_status": None,
        "deferred_generation_request_id": str(deferred_request.id),
        "deferred_generation_status": deferred_request.status,
        "deferred_source_task_id": str(source_task.id) if source_task else str(deferred_request.source_task_id),
        "deferred_frame_slot": deferred_request.frame_slot,
    }


def _production_unit_waiting_for_deferred_source(
    *,
    episode: Episode,
    target_shot: Shot,
    upstream_request: DeferredGenerationRequest,
    batch_id: str | None,
    batch_kind: str | None,
    batch_index: int | None,
    batch_size: int,
    batch_shot_ids: list[str] | None = None,
) -> dict:
    return {
        "batch_id": batch_id,
        "batch_kind": batch_kind or None,
        "batch_index": batch_index,
        "batch_size": batch_size,
        "shot_version_key": canonical_shot_version_key(target_shot),
        "shot_version_label": shot_version_label(target_shot),
        "batch_shot_ids": list(batch_shot_ids or []),
        "episode_id": str(episode.id),
        "episode_number": episode.number,
        "episode_title": episode.title,
        "shot_ids": [str(target_shot.id)],
        "shot_id": str(target_shot.id),
        "shot_number": shot_version_number(target_shot),
        "shot_order": shot_version_number(target_shot),
        "shot_title": target_shot.title,
        "media_type": target_shot.media_type,
        "h3_mode": target_shot.h3_mode,
        "workflow_mode": "waiting_for_deferred_first_frame_source",
        "task_id": None,
        "task_status": None,
        "deferred_generation_request_id": None,
        "deferred_generation_status": "waiting_for_upstream_deferred",
        "deferred_upstream_request_id": str(upstream_request.id),
        "deferred_frame_slot": DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
    }


def _production_unit_waiting_for_deferred_chain(
    *,
    episode: Episode,
    target_shot: Shot,
    upstream_shot: Shot,
    root_request: DeferredGenerationRequest | None,
    batch_id: str | None,
    batch_kind: str | None,
    batch_index: int | None,
    batch_size: int,
    batch_shot_ids: list[str] | None = None,
) -> dict:
    return {
        "batch_id": batch_id,
        "batch_kind": batch_kind or None,
        "batch_index": batch_index,
        "batch_size": batch_size,
        "shot_version_key": canonical_shot_version_key(target_shot),
        "shot_version_label": shot_version_label(target_shot),
        "batch_shot_ids": list(batch_shot_ids or []),
        "episode_id": str(episode.id),
        "episode_number": episode.number,
        "episode_title": episode.title,
        "shot_ids": [str(target_shot.id)],
        "shot_id": str(target_shot.id),
        "shot_number": shot_version_number(target_shot),
        "shot_order": shot_version_number(target_shot),
        "shot_title": target_shot.title,
        "media_type": target_shot.media_type,
        "h3_mode": target_shot.h3_mode,
        "workflow_mode": "waiting_for_deferred_first_frame_chain",
        "task_id": None,
        "task_status": None,
        "deferred_generation_request_id": None,
        "deferred_generation_status": "waiting_for_upstream_deferred_chain",
        "deferred_upstream_shot_id": str(upstream_shot.id),
        "deferred_upstream_shot_number": shot_version_number(upstream_shot),
        "deferred_upstream_request_id": str(root_request.id) if root_request else None,
        "deferred_frame_slot": DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
    }


def _episode_batch_response(
    *, project: SeriesProject, tasks: list[GenerationTask], idempotency_key: str,
    idempotent_replay: bool,
) -> dict:
    if not tasks:
        raise ProductionIdempotencyConflict({
            "detail": "整集生成批次没有可复用的子任务，请使用新的 idempotency_key。",
            "idempotency_key": idempotency_key,
        })
    first = tasks[0].capability_snapshot or {}
    batch_size = int(first.get("batch_size", 0))
    indices = [
        int((task.capability_snapshot or {}).get("batch_index", 0)) for task in tasks
    ]
    if (
        batch_size < len(tasks)
        or any(index < 1 or index > batch_size for index in indices)
        or len(set(indices)) != len(indices)
    ):
        raise ProductionIdempotencyConflict({
            "detail": "检测到不完整的整集生成批次，请使用新的 idempotency_key 重新提交。",
            "idempotency_key": idempotency_key,
        })
    if len(tasks) < batch_size:
        batch_id = first.get("batch_id")
        deferred_exists = bool(
            batch_id
            and DeferredGenerationRequest.objects.filter(
                project=project,
                frame_slot=DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
                status__in=DeferredGenerationRequest.ACTIVE_STATUSES,
            ).filter(
                Q(source_task__capability_snapshot__batch_id=batch_id)
                | Q(
                    source_task__deferred_generation_request__source_task__capability_snapshot__batch_id=batch_id
                )
                | Q(materialized_task__capability_snapshot__batch_id=batch_id)
            ).exists()
        )
        if not deferred_exists:
            raise ProductionIdempotencyConflict({
                "detail": "检测到不完整的整集生成批次，请使用新的 idempotency_key 重新提交。",
                "idempotency_key": idempotency_key,
            })
    units = [_production_unit_from_task(task) for task in tasks]
    batch_slots = build_production_batch_slots(tasks[0], known_tasks=tasks)
    return {
        "project_id": str(project.id),
        "whole_episode": True,
        "shot_version_key": first.get("shot_version_key", ""),
        "shot_version_label": first.get("shot_version_label", ""),
        "batch_shot_ids": [
            str(value) for value in first.get("batch_shot_ids", [])
        ],
        "batch_id": first.get("batch_id"),
        "batch_kind": "episode",
        "batch_size": batch_size,
        "batch_slots": batch_slots,
        "extended_generation_confirmed": bool(
            first.get("extended_generation_confirmed", False)
        ),
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


def _series_batch_response(
    *, project: SeriesProject, tasks: list[GenerationTask], idempotency_key: str,
    idempotent_replay: bool,
) -> dict:
    if not tasks:
        raise ProductionIdempotencyConflict({
            "detail": "幂等生产批次没有可复用的子任务，请使用新的 idempotency_key。",
            "idempotency_key": idempotency_key,
        })
    first = tasks[0].capability_snapshot or {}
    batch_size = int(first.get("batch_size", 0))
    batch_id = first.get("batch_id")
    batch_kind = first.get("batch_kind")
    fingerprint = first.get("request_fingerprint")
    indices = [
        int((task.capability_snapshot or {}).get("batch_index", 0)) for task in tasks
    ]
    complete = (
        batch_kind in {"selection", "series"}
        and batch_id
        and fingerprint
        and batch_size == len(tasks)
        and indices == list(range(1, batch_size + 1))
        and all(
            (task.capability_snapshot or {}).get("batch_id") == batch_id
            and (task.capability_snapshot or {}).get("batch_kind") == batch_kind
            and (task.capability_snapshot or {}).get("idempotency_key") == idempotency_key
            and (task.capability_snapshot or {}).get("request_fingerprint") == fingerprint
            for task in tasks
        )
    )
    if not complete:
        raise ProductionIdempotencyConflict({
            "detail": "检测到不完整或混合的幂等生产批次，请使用新的 idempotency_key。",
            "idempotency_key": idempotency_key,
        })
    units = [_production_unit_from_task(task) for task in tasks]
    batch_slots = build_production_batch_slots(tasks[0], known_tasks=tasks)
    episode_ids = list(dict.fromkeys(
        str(unit["episode_id"]) for unit in units if unit.get("episode_id")
    ))
    prompt_language_gate_reports = []
    for task, unit in zip(tasks, units, strict=True):
        report = _prompt_language_gate_payload(
            task.generated_workflow.prompt_artifact,
            episode_id=unit.get("episode_id"),
            episode_number=unit.get("episode_number"),
            shot_ids=unit.get("shot_ids", []),
        )
        if report:
            prompt_language_gate_reports.append(report)
    return {
        "project_id": str(project.id),
        "whole_episode": False,
        "batch_id": batch_id,
        "batch_kind": batch_kind,
        "batch_size": batch_size,
        "batch_slots": batch_slots,
        "extended_generation_confirmed": bool(
            first.get("extended_generation_confirmed", False)
        ),
        "idempotency_key": idempotency_key,
        "idempotent_replay": idempotent_replay,
        "episode_ids": episode_ids,
        "prompt_artifact_ids": [unit["prompt_artifact_id"] for unit in units],
        "workflow_ids": [unit["workflow_id"] for unit in units],
        "task_ids": [unit["task_id"] for unit in units],
        "counts": {"prompts": len(tasks), "workflows": len(tasks), "tasks": len(tasks)},
        "production_units": units,
        "tasks": GenerationTaskSerializer(tasks, many=True).data,
        "prompt_language_gate": prompt_language_gate_reports,
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
    if any(_is_dedicated_project_cover(asset) for asset in assets):
        raise ValidationError("连续剧封面是项目展示专用素材，不能用于设定集、分镜或生成输入。")
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


def _without_deleted_binding_entities(
    bindings: object,
    *,
    field: str,
    deleted_ids: set[str],
) -> tuple[object, bool]:
    if not isinstance(bindings, dict):
        return bindings, False
    result = dict(bindings)
    if field == "scene":
        scene = result.get("scene")
        if isinstance(scene, dict) and str(scene.get("entity_id") or "") in deleted_ids:
            result.pop("scene", None)
            return result, True
        return bindings, False
    rows = result.get(field)
    if not isinstance(rows, list):
        return bindings, False
    kept = [
        row for row in rows
        if not (
            isinstance(row, dict)
            and str(row.get("entity_id") or "") in deleted_ids
        )
    ]
    if len(kept) == len(rows):
        return bindings, False
    if kept:
        result[field] = kept
    else:
        result.pop(field, None)
    return result, True


def _prune_deleted_bible_dependencies(
    *,
    project: SeriesProject,
    key: str,
    delete_ids: set,
) -> None:
    if not delete_ids:
        return
    deleted_id_strings = {str(value) for value in delete_ids}
    binding_field = {
        "characters": "characters", "scenes": "scene", "props": "props",
    }[key]
    translation_scope = {
        "characters": PromptFieldTranslation.ScopeType.CHARACTER,
        "scenes": PromptFieldTranslation.ScopeType.SCENE,
        "props": PromptFieldTranslation.ScopeType.PROP,
    }[key]
    lock_scope = {
        "characters": ConsistencyLock.Scope.CHARACTER,
        "scenes": ConsistencyLock.Scope.SCENE,
        "props": ConsistencyLock.Scope.PROP,
    }[key]

    for shot in Shot.objects.select_for_update().filter(episode__project=project):
        changed_fields = []
        cleaned, changed = _without_deleted_binding_entities(
            shot.reference_bindings,
            field=binding_field,
            deleted_ids=deleted_id_strings,
        )
        if changed:
            shot.reference_bindings = cleaned
            changed_fields.append("reference_bindings")
        metadata = dict(shot.metadata or {})
        cleaned_metadata_bindings, metadata_changed = _without_deleted_binding_entities(
            metadata.get("reference_bindings"),
            field=binding_field,
            deleted_ids=deleted_id_strings,
        )
        if metadata_changed:
            if cleaned_metadata_bindings:
                metadata["reference_bindings"] = cleaned_metadata_bindings
            else:
                metadata.pop("reference_bindings", None)
            shot.metadata = metadata
            changed_fields.append("metadata")
        if changed_fields:
            shot.save(update_fields=(*changed_fields, "updated_at"))

    PromptFieldTranslation.objects.filter(
        project=project,
        scope_type=translation_scope,
        scope_id__in=delete_ids,
    ).delete()
    ConsistencyLock.objects.filter(
        project=project,
        scope=lock_scope,
        target_id__in=delete_ids,
        is_active=True,
    ).update(is_active=False)


def _sync_bible_collection(*, project, user, key: str, items: list[dict]):
    model = {"characters": CharacterBible, "scenes": SceneBible, "props": PropBible}[key]
    existing = list(model.objects.select_for_update().filter(project=project).prefetch_related("reference_assets"))
    existing_by_id = {item.pk: item for item in existing}
    requested_ids = {item["id"] for item in items if item.get("id")}
    unknown_ids = requested_ids - set(existing_by_id)
    if unknown_ids:
        raise ValidationError({key: f"以下 id 不属于该项目: {', '.join(sorted(map(str, unknown_ids)))}"})

    for data in items:
        if "metadata" not in data:
            continue
        instance = existing_by_id.get(data.get("id"))
        previous_metadata = getattr(instance, "metadata", {}) if instance else {}
        try:
            validate_untrusted_script_bible_metadata(
                previous_metadata,
                data["metadata"],
            )
        except ScriptBibleProvenanceProtectedError as exc:
            raise ValidationError({"metadata": str(exc)}) from exc

    delete_ids = set(existing_by_id) - requested_ids
    deleted_count = len(delete_ids)
    if delete_ids:
        _prune_deleted_bible_dependencies(
            project=project,
            key=key,
            delete_ids=delete_ids,
        )
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
        if "anchor_asset" in data:
            anchor = data["anchor_asset"]
            anchor = _locked_assets_in_order([anchor])[0] if anchor else None
            _validate_bible_asset_access(
                user, [anchor] if anchor else [], expected_kind=expected_kind,
            )
            instance.anchor_asset = anchor
        provenance_assets = []
        if instance.anchor_asset:
            provenance_assets.extend(_locked_assets_in_order([instance.anchor_asset]))
        if references_supplied:
            provenance_assets.extend(
                asset for asset in references
                if all(asset.pk != current.pk for current in provenance_assets)
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
            metadata = serializer.validated_data.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("usage") == "project_cover":
                try:
                    SeriesProject.objects.select_for_update().get(
                        pk=metadata.get("project_id"), owner=self.request.user,
                    )
                except (SeriesProject.DoesNotExist, ValueError) as exc:
                    raise ValidationError({
                        "metadata": "连续剧项目已不存在，请返回项目列表刷新后重试。",
                    }) from exc
            User.objects.select_for_update().get(pk=self.request.user.pk)
            serializer.enforce_tenant_quota()
            serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.owner_id is None:
            raise ValidationError(
                "全局内置素材为只读；请通过受审计的素材 manifest/import 流程维护。"
            )
        with transaction.atomic():
            try:
                asset = self.get_queryset().select_for_update().get(pk=serializer.instance.pk)
            except Asset.DoesNotExist as exc:
                raise NotFound("素材不存在") from exc
            self.check_object_permissions(self.request, asset)
            serializer.instance = asset
            requested_kind = serializer.validated_data.get("kind", asset.kind)
            if requested_kind != asset.kind:
                conflict = _asset_reference_conflict_message(asset)
                if conflict:
                    raise ValidationError({"kind": conflict})
            if _is_dedicated_project_cover(asset):
                metadata = asset.metadata or {}
                try:
                    SeriesProject.objects.select_for_update().get(
                        pk=metadata.get("project_id"), owner=asset.owner,
                    )
                except (SeriesProject.DoesNotExist, ValueError) as exc:
                    raise ValidationError({
                        "metadata": "封面所属连续剧项目已不存在，不能继续修改该素材。",
                    }) from exc
            User.objects.select_for_update().get(pk=self.request.user.pk)
            serializer.enforce_tenant_quota()
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            try:
                cover_project_ids = list(
                    SeriesProject.objects.filter(cover_asset_id=kwargs["pk"])
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
                if cover_project_ids:
                    list(
                        SeriesProject.objects.select_for_update()
                        .filter(pk__in=cover_project_ids)
                        .order_by("pk")
                    )
                asset = self.get_queryset().select_for_update().get(pk=kwargs["pk"])
            except Asset.DoesNotExist as exc:
                raise NotFound("素材不存在") from exc
            self.check_object_permissions(request, asset)
            if asset.owner_id is None:
                raise ValidationError(
                    "全局内置素材为只读；请通过受审计的素材 manifest/import 流程维护。"
                )
            if not _is_dedicated_project_cover(asset):
                conflict = _asset_reference_conflict_message(asset)
                if conflict:
                    raise ValidationError(conflict)
            if asset.derived_deferred_generation_requests.exists():
                raise ValidationError(
                    "该素材是延迟首尾帧连续生成的审计帧证据，不能删除。"
                )
            _delete_file_after_commit(asset)
            asset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _translation_scope_id(project: SeriesProject, item: dict):
    return project.id if item["scope_type"] == PromptFieldTranslation.ScopeType.PROJECT else item["scope_id"]


def _lock_translation_project(project: SeriesProject, user) -> SeriesProject:
    queryset = SeriesProject.objects.select_for_update()
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    try:
        return queryset.get(pk=project.pk)
    except SeriesProject.DoesNotExist as exc:
        raise PermissionDenied("无权修改该项目的翻译候选") from exc


def _save_translation_candidate(
    *, project: SeriesProject, request_user, item: dict,
    result: TranslationResult | PromptTranslationError,
    attempted_provider: str = LOCAL_QWEN_PROVIDER,
) -> PromptFieldTranslation:
    scope_id = _translation_scope_id(project, item)
    source = item["source_text"]
    source_hash = source_text_hash(source)
    with transaction.atomic():
        project = _lock_translation_project(project, request_user)
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


class ProjectVersionConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "project_version_conflict"

    def __init__(self, *, expected: str | None, current: str | None):
        detail = {
            "detail": "连续剧项目已被其他操作更新；请刷新项目后再保存，避免用陈旧快照覆盖最新 Bible。",
            "expected_updated_at": expected,
            "current_updated_at": current,
        }
        super().__init__(detail=detail, code=self.default_code)
        self.detail = detail


def _assert_project_expected_updated_at(
    project: SeriesProject,
    expected_updated_at,
) -> None:
    if expected_updated_at is None:
        return
    expected = expected_updated_at
    current = project.updated_at
    if expected != current:
        raise ProjectVersionConflict(
            expected=expected.isoformat() if expected else None,
            current=current.isoformat() if current else None,
        )


class ProjectDeleteConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "project_has_active_work"

    def __init__(self, detail=None, code=None):
        super().__init__(detail=detail, code=code)
        if isinstance(detail, dict):
            for field in (
                "active_generation_tasks",
                "unresolved_remote_tasks",
                "active_assembly_jobs",
            ):
                if field in detail:
                    self.detail[field] = int(detail[field])


class ScriptRebuildConfirmationRequired(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "script_rebuild_confirmation_required"

    def __init__(self, detail):
        super().__init__(detail=detail)
        self.detail = detail


class ScriptRebuildBlocked(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "script_rebuild_blocked"

    def __init__(self, detail):
        super().__init__(detail=detail)
        self.detail = detail


class ScriptBibleEndpointError(APIException):
    def __init__(self, *, status_code: int, code: str, detail: dict):
        self.status_code = status_code
        self.default_code = code
        super().__init__(detail=detail, code=code)
        self.detail = detail


def _parse_script_bible_or_error(script: str):
    try:
        return parse_script_bible(script)
    except ScriptBibleParseError as exc:
        diagnostic = dict(exc.diagnostics[0])
        raise ScriptBibleEndpointError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=exc.code,
            detail={
                "detail": str(exc),
                "line": exc.line,
                "diagnostic": diagnostic,
            },
        ) from exc


def _locked_script_bible_rows(project: SeriesProject, *, lock: bool) -> dict[str, list]:
    rows = {
        "characters": CharacterBible.objects.filter(project=project).order_by("id"),
        "scenes": SceneBible.objects.filter(project=project).order_by("id"),
        "props": PropBible.objects.filter(project=project).order_by("id"),
    }
    if lock:
        rows = {key: queryset.select_for_update() for key, queryset in rows.items()}
    return {key: list(queryset) for key, queryset in rows.items()}


def _classified_script_bible_candidates(parsed, rows: dict[str, list]) -> list[dict]:
    return classify_script_bible_candidates(
        parsed.candidates,
        characters=rows["characters"],
        scenes=rows["scenes"],
        props=rows["props"],
    )


def _confirmed_script_bible_aliases(
    candidates: list[dict],
    rows: dict[str, list],
) -> ConfirmedScriptBibleAliases:
    """Build declaration-name aliases only from verified confirmed provenance."""

    collection_by_type = {
        "character": "characters", "scene": "scenes", "prop": "props",
    }
    rows_by_collection = {
        collection: {str(row.pk): row for row in values}
        for collection, values in rows.items()
    }
    aliases = {"characters": {}, "scenes": {}, "props": {}}
    for candidate in candidates:
        if not candidate["confirmed"] or candidate["conflict_code"]:
            continue
        collection = collection_by_type[candidate["entity_type"]]
        row = rows_by_collection[collection].get(
            str(candidate.get("matched_bible_id") or ""),
        )
        if row is None:
            raise ScriptBibleEndpointError(
                status_code=status.HTTP_409_CONFLICT,
                code="script_bible_provenance_conflict",
                detail={
                    "detail": (
                        "已确认候选指向的 Bible 已变化；当前数据未作任何修改，"
                        "请刷新候选后重试。"
                    ),
                },
            )
        aliases[collection][candidate["name"]] = row
    return ConfirmedScriptBibleAliases(**aliases)


def _stable_script_bible_slug(
    candidate: dict,
    *,
    used_slugs: set[str],
) -> str:
    prefix = {
        "character": "character", "scene": "scene", "prop": "prop",
    }[candidate["entity_type"]]
    base = slugify(candidate["name"]) or prefix
    for salt in range(1_000):
        digest_source = (
            candidate["candidate_id"] if salt == 0
            else f"{candidate['candidate_id']}:{salt}"
        )
        suffix = hashlib.sha256(digest_source.encode("ascii")).hexdigest()[:12]
        value = f"{base[:67]}-{suffix}"[:80]
        if value not in used_slugs:
            used_slugs.add(value)
            return value
    raise ScriptBibleEndpointError(
        status_code=status.HTTP_409_CONFLICT,
        code="script_bible_slug_conflict",
        detail={"detail": "无法为候选分配唯一稳定 slug；当前数据未作任何修改。"},
    )


class SeriesProjectViewSet(viewsets.ModelViewSet):
    serializer_class = SeriesProjectSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("status", "style", "target_model")
    search_fields = ("title", "logline", "master_script")
    ordering_fields = ("title", "created_at", "updated_at")

    def get_queryset(self):
        project_duration = (
            Shot.objects.filter(episode__project_id=OuterRef("pk"))
            .values("episode__project_id")
            .annotate(total=Sum("duration_ms"))
            .values("total")[:1]
        )
        queryset = SeriesProject.objects.select_related("style", "owner", "cover_asset").annotate(
            episode_count=Count("episodes", distinct=True),
            completed_episode_count=Count(
                "episodes",
                filter=Q(episodes__status=Episode.Status.COMPLETED),
                distinct=True,
            ),
            total_duration_ms=Coalesce(
                Subquery(project_duration, output_field=IntegerField()),
                0,
            ),
            character_count=Count("characters", distinct=True),
            scene_count=Count("scenes", distinct=True),
            prop_count=Count("props", distinct=True),
        ).order_by("-updated_at")
        return _owned_projects(queryset, self.request.user)

    def perform_create(self, serializer):
        serializer.validated_data.pop("expected_updated_at", None)
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        with transaction.atomic():
            project = SeriesProject.objects.select_for_update().get(
                pk=serializer.instance.pk,
            )
            self.check_object_permissions(self.request, project)
            expected_updated_at = serializer.validated_data.pop(
                "expected_updated_at", None,
            )
            _assert_project_expected_updated_at(project, expected_updated_at)
            serializer.instance = project
            old_cover_id = project.cover_asset_id
            new_cover = serializer.validated_data.get("cover_asset")
            if new_cover is not None:
                try:
                    locked_cover = Asset.objects.select_for_update().get(
                        pk=new_cover.pk,
                    )
                except Asset.DoesNotExist as exc:
                    raise ValidationError({"cover_asset": "封面素材已被删除，请重新上传。"}) from exc
                serializer.validated_data["cover_asset"] = serializer.validate_cover_asset(
                    locked_cover,
                )
            project = serializer.save()
            if old_cover_id and old_cover_id != project.cover_asset_id:
                _delete_unused_project_cover(old_cover_id)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        project = self.get_queryset().get(pk=serializer.instance.pk)
        response_serializer = self.get_serializer(project)
        headers = self.get_success_headers(response_serializer.data)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @extend_schema(
        responses={
            204: None,
            404: ProjectDeleteErrorResponseSerializer,
            409: ProjectDeleteErrorResponseSerializer,
        },
    )
    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            try:
                project = _owned_projects(
                    SeriesProject.objects.all(), request.user,
                ).select_for_update().get(pk=kwargs["pk"])
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            self.check_object_permissions(request, project)
            cover_asset_id = project.cover_asset_id
            # Lock child work records before evaluating the delete guard. This
            # prevents a recovery worker from changing a failed unresolved
            # task back to queued between the safety check and the cascade.
            generation_tasks = list(
                project.generation_tasks.select_for_update().only(
                    "status", "submission_token", "submission_state", "remote_state",
                )
            )
            assembly_jobs = list(
                project.assembly_jobs.select_for_update().only("status")
            )
            deferred_count = DeferredGenerationRequest.objects.select_for_update().filter(
                project=project,
            ).count()
            active_generation_statuses = {
                GenerationTask.Status.QUEUED,
                GenerationTask.Status.RUNNING,
                GenerationTask.Status.CANCEL_REQUESTED,
            }
            terminal_submission_states = {
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            }
            active_generation_tasks = sum(
                task.status in active_generation_statuses
                for task in generation_tasks
            )
            unresolved_remote_tasks = sum(
                bool(task.submission_token)
                and task.status != GenerationTask.Status.SUCCEEDED
                and task.submission_state not in terminal_submission_states
                and task.remote_state != GenerationTask.RemoteState.SUCCEEDED
                for task in generation_tasks
            )
            active_assembly_statuses = {
                AssemblyJob.Status.QUEUED,
                AssemblyJob.Status.RUNNING,
            }
            active_assembly_jobs = sum(
                job.status in active_assembly_statuses
                for job in assembly_jobs
            )
            if active_generation_tasks or unresolved_remote_tasks or active_assembly_jobs:
                raise ProjectDeleteConflict({
                    "detail": (
                        "项目仍有排队、运行、取消中或远端状态未终结的生产任务，不能删除；"
                        "请等待任务及远端提交均进入终态后重试。"
                    ),
                    "active_generation_tasks": active_generation_tasks,
                    "unresolved_remote_tasks": unresolved_remote_tasks,
                    "active_assembly_jobs": active_assembly_jobs,
                })
            if deferred_count:
                raise ProjectDeleteConflict({
                    "detail": (
                        "项目存在延迟首尾帧连续生成请求；为保留 source/task/frame 审计链，"
                        "请先处理或保留项目，不执行级联删除。"
                    ),
                    "active_generation_tasks": active_generation_tasks,
                    "unresolved_remote_tasks": unresolved_remote_tasks,
                    "active_assembly_jobs": active_assembly_jobs,
                })
            # GenerationTask intentionally protects its materialized workflow.
            # All tasks are safe terminal rows at this point, so delete those
            # project-owned rows first and let the project cascade collect the
            # workflows. Any later PROTECT failure rolls this transaction back.
            GenerationTask.objects.filter(
                pk__in=[task.pk for task in generation_tasks],
            ).delete()
            project.delete()
            _delete_unused_project_cover(cover_asset_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

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

    @extend_schema(
        request=None,
        responses={
            200: ScriptBibleCandidatesResponseSerializer,
            400: ScriptBibleErrorEnvelopeSerializer,
        },
    )
    @action(detail=True, methods=["get"], url_path="script-bible-candidates")
    def script_bible_candidates(self, request, pk=None):
        """Derive review-only Bible candidates from the current full master script."""

        project = self.get_object()
        parsed = _parse_script_bible_or_error(project.master_script)
        rows = _locked_script_bible_rows(project, lock=False)
        candidates = _classified_script_bible_candidates(parsed, rows)
        response_serializer = ScriptBibleCandidatesResponseSerializer(data={
            "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
            "project_id": str(project.pk),
            "source_fingerprint": parsed.source_fingerprint,
            "has_block": parsed.has_block,
            "candidates": candidates,
            "diagnostics": list(parsed.diagnostics),
        })
        response_serializer.is_valid(raise_exception=True)
        return Response(response_serializer.data)

    @extend_schema(
        request=ScriptBibleApplyRequestSerializer,
        responses={
            200: ScriptBibleApplyResponseSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            409: ScriptBibleErrorEnvelopeSerializer,
        },
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="script-bible-candidates/apply",
    )
    def apply_script_bible_candidates(self, request, pk=None):
        """Atomically add only explicitly reviewed candidates; never snapshot-delete."""

        visible_project = self.get_object()
        input_serializer = ScriptBibleApplyRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        try:
            with transaction.atomic():
                project = SeriesProject.objects.select_for_update().get(
                    pk=visible_project.pk,
                )
                self.check_object_permissions(request, project)
                parsed = _parse_script_bible_or_error(project.master_script)
                if parsed.source_fingerprint != data["source_fingerprint"]:
                    raise ScriptBibleEndpointError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="script_bible_candidates_stale",
                        detail={
                            "detail": "完整剧本已变化；当前数据未作任何修改，请刷新候选后重试。",
                            "source_fingerprint": data["source_fingerprint"],
                            "current_source_fingerprint": parsed.source_fingerprint,
                        },
                    )
                rows = _locked_script_bible_rows(project, lock=True)
                candidates = _classified_script_bible_candidates(parsed, rows)
                candidates_by_id = {
                    candidate["candidate_id"]: candidate for candidate in candidates
                }
                selected_ids = [
                    item["candidate_id"] for item in data["selections"]
                ]
                unknown = [
                    candidate_id for candidate_id in selected_ids
                    if candidate_id not in candidates_by_id
                ]
                if unknown:
                    raise ScriptBibleEndpointError(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code="script_bible_selection_invalid",
                        detail={
                            "detail": "选择中包含不属于当前完整剧本的候选；当前数据未作任何修改。",
                        },
                    )
                edited_ir = {"characters": [], "scenes": [], "props": []}
                collection_by_type = {
                    "character": "characters", "scene": "scenes", "prop": "props",
                }
                ordered_selections: list[tuple[dict, str]] = []
                for selection in data["selections"]:
                    original = candidates_by_id[selection["candidate_id"]]
                    collection = collection_by_type[original["entity_type"]]
                    edited_ir[collection].append({
                        "name": selection["name"],
                        "description": selection["description"],
                        "visual_identity_en": selection["visual_identity_en"],
                    })
                    ordered_selections.append((original, collection))
                try:
                    validated_edits = validate_script_bible_ir(edited_ir)
                except ScriptBibleParseError as exc:
                    raise ScriptBibleEndpointError(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code=exc.code,
                        detail={
                            "detail": str(exc),
                            "line": exc.line,
                            "diagnostic": dict(exc.diagnostics[0]),
                        },
                    ) from exc
                offsets = {"characters": 0, "scenes": 0, "props": 0}
                edited_candidates: list[dict] = []
                for original, collection in ordered_selections:
                    edited = validated_edits[collection][offsets[collection]]
                    offsets[collection] += 1
                    edited_candidates.append({
                        **original,
                        "name": edited["name"],
                        "description": edited["description"],
                        "visual_identity_en": edited["visual_identity_en"],
                    })
                selected = classify_script_bible_candidates(
                    edited_candidates,
                    characters=rows["characters"],
                    scenes=rows["scenes"],
                    props=rows["props"],
                )
                for candidate in selected:
                    if (
                        candidate["confirmed"]
                        and candidate.get("_reviewed_content_sha256")
                        != script_bible_reviewed_content_sha256(candidate)
                    ):
                        candidate["existing"] = False
                        candidate["confirmed"] = False
                        candidate["conflict_code"] = (
                            "script_bible_reviewed_content_conflict"
                        )
                conflicts = [item for item in selected if item["conflict_code"]]
                if conflicts:
                    raise ScriptBibleEndpointError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="script_bible_apply_conflict",
                        detail={
                            "detail": "候选与现有 Bible 的类型或内容冲突；本批次未写入任何数据。",
                            "source_fingerprint": parsed.source_fingerprint,
                            "conflict_count": len(conflicts),
                            "candidates": conflicts,
                        },
                    )

                model_by_type = {
                    "character": CharacterBible,
                    "scene": SceneBible,
                    "prop": PropBible,
                }
                rows_by_id = {
                    str(row.pk): row
                    for values in rows.values()
                    for row in values
                }
                used_slugs = {
                    entity_type: {
                        row.slug for row in rows[collection]
                    }
                    for entity_type, collection in (
                        ("character", "characters"),
                        ("scene", "scenes"),
                        ("prop", "props"),
                    )
                }
                speaker_numbers = [
                    int(row.stable_speaker_id[1:])
                    for row in rows["characters"]
                    if re.fullmatch(r"S[1-9][0-9]*", row.stable_speaker_id)
                ]
                next_speaker_number = max(speaker_numbers, default=0) + 1
                created = {"characters": [], "scenes": [], "props": []}
                already_exists = {"characters": [], "scenes": [], "props": []}
                for candidate in selected:
                    collection = collection_by_type[candidate["entity_type"]]
                    reviewed_provenance = {
                        "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
                        "candidate_id": candidate["candidate_id"],
                        "declaration_fingerprint": candidate[
                            "declaration_fingerprint"
                        ],
                        "source_fingerprint": parsed.source_fingerprint,
                        "reviewed_snapshot": {
                            "name": candidate["name"],
                            "description": candidate["description"],
                            "visual_identity_en": candidate[
                                "visual_identity_en"
                            ],
                        },
                        "reviewed_content_sha256": (
                            script_bible_reviewed_content_sha256(candidate)
                        ),
                    }
                    if candidate["existing"]:
                        bible = rows_by_id[candidate["matched_bible_id"]]
                        metadata = dict(bible.metadata or {})
                        if not isinstance(metadata.get("script_bible_v1"), dict):
                            metadata["script_bible_v1"] = reviewed_provenance
                            bible.metadata = metadata
                            bible.save(update_fields=("metadata", "updated_at"))
                        already_exists[collection].append(bible)
                        continue
                    kwargs = {
                        "project": project,
                        "slug": _stable_script_bible_slug(
                            candidate,
                            used_slugs=used_slugs[candidate["entity_type"]],
                        ),
                        "name": candidate["name"],
                        "description": candidate["description"],
                        "visual_identity": candidate["visual_identity_en"],
                        "metadata": {
                            "script_bible_v1": reviewed_provenance,
                        },
                        "is_locked": True,
                    }
                    if candidate["entity_type"] == "character":
                        kwargs["stable_speaker_id"] = f"S{next_speaker_number}"
                        next_speaker_number += 1
                    bible = model_by_type[candidate["entity_type"]].objects.create(
                        **kwargs,
                    )
                    created[collection].append(bible)
                sync_consistency_locks(project)
                project.save(update_fields=("updated_at",))
        except IntegrityError as exc:
            raise ScriptBibleEndpointError(
                status_code=status.HTTP_409_CONFLICT,
                code="script_bible_apply_conflict",
                detail={
                    "detail": "并发写入导致 Bible 唯一键冲突；本批次未写入任何数据。",
                },
            ) from exc

        created_count = sum(len(rows) for rows in created.values())
        existing_count = sum(len(rows) for rows in already_exists.values())
        response_serializer = ScriptBibleApplyResponseSerializer(
            instance={
                "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
                "project_id": str(project.pk),
                "source_fingerprint": parsed.source_fingerprint,
                "updated_at": project.updated_at,
                "created": created,
                "already_exists": already_exists,
                "counts": {
                    "requested": len(selected),
                    "created": created_count,
                    "already_exists": existing_count,
                },
            },
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(request=ProjectBibleSyncSerializer, responses=dict)
    @action(detail=True, methods=["post"], url_path="sync-bibles")
    def sync_bibles(self, request, pk=None):
        """Apply authoritative character/scene/prop snapshots atomically."""
        project = self.get_object()
        serializer = ProjectBibleSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        expected_updated_at = data.pop("expected_updated_at", None)
        with transaction.atomic():
            project = SeriesProject.objects.select_for_update().get(pk=project.pk)
            self.check_object_permissions(request, project)
            _assert_project_expected_updated_at(project, expected_updated_at)
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
            project.save(update_fields=("updated_at",))
        return Response({
            "project_id": str(project.id),
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
            "counts": counts,
            **results,
        })

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

    @extend_schema(
        request=ScriptBreakdownSerializer,
        responses={
            201: ScriptBreakdownResponseSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            409: PolymorphicProxySerializer(
                component_name="ScriptBreakdownConflictResponse",
                serializers=[
                    ScriptRebuildConflictResponseSerializer,
                    ScriptBibleErrorEnvelopeSerializer,
                ],
                resource_type_field_name=None,
            ),
        },
    )
    @action(detail=True, methods=["post"], url_path="breakdown-script")
    def breakdown_script(self, request, pk=None):
        project = self.get_object()
        serializer = ScriptBreakdownSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        explicit_script = data["script"]
        if project.episodes.exists() and not data["replace_existing"]:
            raise ValidationError({"replace_existing": "项目已有剧集；若要按完整剧本重建，请显式设为 true"})
        try:
            with transaction.atomic():
                project = SeriesProject.objects.select_for_update().get(pk=project.pk)
                if project.episodes.exists() and not data["replace_existing"]:
                    raise ValidationError({"replace_existing": "并发请求期间项目已创建剧集；请刷新后重试"})
                script = (
                    explicit_script
                    if explicit_script.strip()
                    else project.master_script
                )
                if not script.strip():
                    raise ValidationError({
                        "script": "请求 script 与项目 master_script 均为空",
                    })
                parsed_script = _parse_script_bible_or_error(script)
                bible_rows = _locked_script_bible_rows(project, lock=True)
                classified_candidates = _classified_script_bible_candidates(
                    parsed_script, bible_rows,
                )
                confirmed_aliases = _confirmed_script_bible_aliases(
                    classified_candidates, bible_rows,
                )
                unconfirmed = [
                    item for item in classified_candidates if not item["confirmed"]
                ]
                if unconfirmed and not data["continue_without_script_bible"]:
                    raise ScriptBibleEndpointError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="script_bible_confirmation_required",
                        detail={
                            "detail": (
                                "完整剧本包含尚未确认应用的 SCRIPT_BIBLE_V1 实体；"
                                "请先审核并应用候选，或显式确认跳过。"
                            ),
                            "source_fingerprint": parsed_script.source_fingerprint,
                            "pending_count": len(unconfirmed),
                            "conflict_count": sum(
                                bool(item["conflict_code"]) for item in unconfirmed
                            ),
                            "candidates": unconfirmed,
                        },
                    )
                if data["replace_existing"]:
                    cleared = discard_rebuildable_production(
                        project,
                        discard_existing_production=data[
                            "discard_existing_production"
                        ],
                    )
                else:
                    cleared = {
                        "episodes": 0,
                        "shots": 0,
                        "generation_tasks": 0,
                        "assembly_jobs": 0,
                        "prompt_artifacts": 0,
                        "generated_workflows": 0,
                        "creative_drafts": 0,
                        "shot_detail_drafts": 0,
                        "shot_detail_batches": 0,
                        "shot_detail_batch_items": 0,
                        "prompt_field_translations": 0,
                        "draft_release_bundles": 0,
                        "physical_outputs_retained": True,
                    }
                if explicit_script.strip() and explicit_script != project.master_script:
                    project.master_script = explicit_script
                    project.save(update_fields=("master_script", "updated_at"))
                result = breakdown_script(
                    project, script,
                    episode_count_hint=data["episode_count_hint"],
                    default_shot_duration_ms=data["default_shot_duration_ms"],
                    default_first_frame_continuity=data["default_first_frame_continuity"],
                    default_last_frame_continuity=data["default_last_frame_continuity"],
                    multi_shot=data["multi_shot"],
                    include_audio=data["include_audio"],
                    confirmed_bible_aliases=confirmed_aliases,
                )
                if unconfirmed and data["continue_without_script_bible"]:
                    result.warnings.insert(
                        0,
                        f"已按人工确认跳过 {len(unconfirmed)} 个未应用的 SCRIPT_BIBLE_V1 实体；"
                        "本次只拆解正文，未自动创建或修改 Bible。",
                    )
                sync_consistency_locks(project)
        except ScriptRebuildConflict as exc:
            if exc.code == "script_rebuild_confirmation_required":
                raise ScriptRebuildConfirmationRequired(exc.details) from exc
            raise ScriptRebuildBlocked(exc.details) from exc
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
            "cleared": cleared,
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

    @extend_schema(
        request=ProductionBuildSerializer,
        responses={
            200: ProductionBuildResponseSerializer,
            201: ProductionBuildResponseSerializer,
            409: PolymorphicProxySerializer(
                component_name="ProductionBuildConflictErrorResponse",
                serializers=[
                    ProductionQueueConfirmationErrorResponseSerializer,
                    ProductionIdempotencyConflictErrorResponseSerializer,
                ],
                resource_type_field_name=None,
            ),
        },
    )
    @action(detail=True, methods=["post"], url_path="build-production")
    def build_production(self, request, pk=None):
        project = self.get_object()
        input_serializer = ProductionBuildSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        whole_episode = data["whole_episode"]
        # Template visibility is broader than execution.  Non-staff may read
        # their legacy entries, but may submit only reviewed global templates.
        available_templates = list(executable_templates(
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

            def production_template_and_overrides_for_shot(first_shot: Shot):
                output_kind = first_shot.media_type
                selection_key = (output_kind, first_shot.h3_mode)
                matched_selection = selected_workflows.get(selection_key)
                template = _select_production_template(
                    templates=available_templates,
                    preferred=preferred_template,
                    selected=(matched_selection or {}).get("template"),
                    output_kind=output_kind,
                    h3_mode=first_shot.h3_mode,
                )
                effective_overrides = dict(
                    image_workflow_overrides
                    if output_kind == PromptArtifact.PromptType.IMAGE
                    else video_workflow_overrides
                )
                if matched_selection:
                    effective_overrides.update(matched_selection["parameters"])
                return selection_key, matched_selection, template, effective_overrides

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
            whole_episode_version_key = ""
            whole_episode_version_label = ""
            whole_episode_batch_shot_ids: list[str] = []
            batch_id = None
            batch_fingerprint = None
            batch_size = 0
            batch_kind = ""
            batch_consistency_snapshot = None
            production_groups_by_episode: dict[uuid.UUID, tuple[list[Shot], list[list[Shot]]]] = {}
            resolved_idempotent_groups: dict[tuple[str, ...], dict] = {}
            deferred_start_shot_ids: set[str] = set()
            created_task_by_shot_id: dict[str, GenerationTask] = {}
            deferred_request_by_target_shot_id: dict[str, DeferredGenerationRequest] = {}
            deferred_chain_source_shot_ids: set[str] = set()
            deferred_chain_root_request_by_shot_id: dict[str, DeferredGenerationRequest] = {}
            deferred_requests: list[DeferredGenerationRequest] = []

            def is_deferred_start_group(group: list[Shot]) -> bool:
                return (
                    data["enqueue"]
                    and len(group) == 1
                    and target_requires_deferred_first_frame(group[0])
                )

            def compute_deferred_start_shots(
                groups_by_episode: dict[uuid.UUID, tuple[list[Shot], list[list[Shot]]]],
            ) -> set[str]:
                planned: set[str] = set()
                source_available_or_planned: set[str] = set()
                for episode in episodes:
                    _, groups_for_episode = groups_by_episode.get(episode.id, ([], []))
                    for group_for_plan in groups_for_episode:
                        if len(group_for_plan) != 1:
                            for item in group_for_plan:
                                if item.media_type == Shot.MediaType.VIDEO:
                                    source_available_or_planned.add(str(item.id))
                            continue
                        shot_for_plan = group_for_plan[0]
                        if target_requires_deferred_first_frame(shot_for_plan):
                            previous = previous_video_shot_for(shot_for_plan)
                            source_ready = bool(previous) and (
                                str(previous.id) in source_available_or_planned
                                or latest_viable_video_task_for_shot(
                                    user=request.user,
                                    project=project,
                                    shot=previous,
                                )
                                is not None
                                or active_first_frame_deferred_for(
                                    previous,
                                    request_key=data["idempotency_key"],
                                ) is not None
                            )
                            if source_ready:
                                planned.add(str(shot_for_plan.id))
                                source_available_or_planned.add(str(shot_for_plan.id))
                                continue
                        if shot_for_plan.media_type == Shot.MediaType.VIDEO:
                            source_available_or_planned.add(str(shot_for_plan.id))
                return planned
            if whole_episode:
                # The serializer guarantees exactly one requested id; this
                # second check provides an ownership-aware error after lookup.
                if len(episodes) != 1:
                    raise ValidationError({"episode_ids": "整集生成目标剧集不存在或不属于该项目"})
                batch_episode = Episode.objects.select_for_update().get(
                    pk=episodes[0].pk,
                    project=project,
                )
                episodes = [batch_episode]
                try:
                    resolved_version = resolve_episode_shot_version(
                        batch_episode,
                        data["shot_version_key"],
                        for_update=True,
                    )
                except ShotVersionResolutionError as exc:
                    raise ValidationError({"shot_version_key": str(exc)}) from exc
                whole_episode_version_key = resolved_version.key
                whole_episode_version_label = resolved_version.label
                whole_episode_batch_shot_ids = [
                    str(shot.id) for shot in resolved_version.shots
                ]
                whole_episode_shots = load_shots(whole_episode_batch_shot_ids)
                if not whole_episode_shots:
                    raise ValidationError("所选分镜版本至少需要一个分镜")
                try:
                    whole_episode_groups = _production_groups(
                        batch_episode,
                        whole_episode_shots,
                        data["multi_shot"],
                        force_single_shot=True,
                    )
                except PromptBuildError as exc:
                    raise ValidationError(str(exc)) from exc
                production_groups_by_episode[batch_episode.id] = (
                    whole_episode_shots,
                    whole_episode_groups,
                )
                deferred_start_shot_ids = compute_deferred_start_shots(
                    production_groups_by_episode,
                )
                batch_consistency_snapshot = capture_consistency_snapshot(project)
                batch_fingerprint = _episode_batch_fingerprint(
                    project=project,
                    episode=batch_episode,
                    shots=whole_episode_shots,
                    data=data,
                    consistency_snapshot=batch_consistency_snapshot,
                )
                existing_tasks = _production_batch_tasks(
                    user=request.user, project=project,
                    idempotency_key=data["idempotency_key"],
                )
                if existing_tasks:
                    fingerprints = {
                        (task.capability_snapshot or {}).get("request_fingerprint")
                        for task in existing_tasks
                    }
                    accepted_fingerprints = {batch_fingerprint}
                    # Accept batches created during either earlier fingerprint
                    # generation: relation/workflow canonicalization landed
                    # before consistency-list canonicalization. New children
                    # always store the fully deterministic fingerprint above.
                    accepted_fingerprints.add(_episode_batch_fingerprint(
                        project=project,
                        episode=batch_episode,
                        shots=whole_episode_shots,
                        data=data,
                        consistency_snapshot=batch_consistency_snapshot,
                        canonical=True,
                        canonical_consistency=False,
                    ))
                    accepted_fingerprints.add(_episode_batch_fingerprint(
                        project=project,
                        episode=batch_episode,
                        shots=whole_episode_shots,
                        data=data,
                        consistency_snapshot=batch_consistency_snapshot,
                        canonical=False,
                    ))
                    if (
                        len(fingerprints) != 1
                        or not fingerprints <= accepted_fingerprints
                    ):
                        raise ProductionIdempotencyConflict({
                            "detail": (
                                "该 idempotency_key 已用于不同的整集生成请求；"
                                "修改分镜或参数后必须使用新的键"
                            ),
                            "idempotency_key": data["idempotency_key"],
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
                batch_kind = "episode"
                batch_size = len(whole_episode_groups)
                immediate_task_count = sum(
                    1
                    for group in whole_episode_groups
                    if str(group[0].id) not in deferred_start_shot_ids
                )
                # Reserve the complete batch under the queue-capacity mutex.
                # No prompt/workflow/task is persisted if this preflight fails.
                assert_queue_capacity(
                    request.user,
                    additional=immediate_task_count,
                    extended_generation_confirmed=data["extended_generation_confirmed"],
                )
            else:
                for episode in episodes:
                    episode_shots = episode.shots.order_by("number")
                    if requested_shot_ids:
                        episode_shots = episode_shots.filter(pk__in=requested_shot_ids)
                    shots = load_shots([str(item.id) for item in episode_shots])
                    if not shots:
                        production_groups_by_episode[episode.id] = ([], [])
                        continue
                    try:
                        groups = _production_groups(
                            episode,
                            shots,
                            data["multi_shot"],
                            force_single_shot=False,
                        )
                    except PromptBuildError as exc:
                        raise ValidationError(str(exc)) from exc
                    production_groups_by_episode[episode.id] = (shots, groups)
                deferred_start_shot_ids = compute_deferred_start_shots(
                    production_groups_by_episode,
                )
                batch_size = sum(
                    1
                    for _, groups in production_groups_by_episode.values()
                    for group in groups
                    if str(group[0].id) not in deferred_start_shot_ids
                )
                idempotency_key = data["idempotency_key"]
                if data["enqueue"] and batch_size > 1 and not idempotency_key:
                    raise ValidationError({
                        "idempotency_key": (
                            "一次创建多个生产任务时必须提供非空 idempotency_key，"
                            "用于安全重放并避免重复排队"
                        ),
                    })
                if idempotency_key and not batch_size and not deferred_start_shot_ids:
                    raise ValidationError({
                        "idempotency_key": "所选生产范围没有可创建的任务，不能建立幂等批次",
                    })
                if data["enqueue"] and batch_size:
                    if idempotency_key:
                        # A keyed selection is always represented as a complete
                        # auditable batch, including the one-child case.
                        batch_id = str(uuid.uuid4())
                        batch_kind = "series" if len(episodes) > 1 else "selection"
                        batch_consistency_snapshot = capture_consistency_snapshot(project)
                        planned_units = []
                        planned_workflow_selections = set()
                        for episode in episodes:
                            _, groups = production_groups_by_episode.get(
                                episode.id, ([], []),
                            )
                            for group in groups:
                                first_shot = group[0]
                                (
                                    selection_key,
                                    matched_selection,
                                    template,
                                    effective_overrides,
                                ) = production_template_and_overrides_for_shot(first_shot)
                                if matched_selection:
                                    planned_workflow_selections.add(selection_key)
                                if str(first_shot.id) in deferred_start_shot_ids:
                                    planned_units.append({
                                        "episode_id": str(episode.id),
                                        "shot_ids": [str(first_shot.id)],
                                        "output_kind": first_shot.media_type,
                                        "h3_mode": first_shot.h3_mode,
                                        "template": {
                                            "id": str(template.id),
                                            "slug": template.slug,
                                            "version": template.version,
                                            "provider": template.provider,
                                            "model": template.model,
                                            "definition": template.definition,
                                            "input_schema": template.input_schema,
                                        },
                                        "workflow_overrides": effective_overrides,
                                        "deferred_generation": {
                                            "frame_slot": DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
                                            "reason": "requires_first_frame_continuity",
                                        },
                                    })
                                    continue
                                output_kind = first_shot.media_type
                                group_key = tuple(str(item.id) for item in group)
                                resolved_idempotent_groups[group_key] = {
                                    "template": template,
                                    "workflow_overrides": effective_overrides,
                                    "selection_key": (
                                        selection_key if matched_selection else None
                                    ),
                                }
                                planned_units.append({
                                    "episode_id": str(episode.id),
                                    "shot_ids": list(group_key),
                                    "output_kind": output_kind,
                                    "h3_mode": first_shot.h3_mode,
                                    "include_audio": (
                                        first_shot.include_audio
                                        if output_kind == PromptArtifact.PromptType.VIDEO
                                        else False
                                    ),
                                    "template": {
                                        "id": str(template.id),
                                        "slug": template.slug,
                                        "version": template.version,
                                        "provider": template.provider,
                                        "model": template.model,
                                        "definition": template.definition,
                                        "input_schema": template.input_schema,
                                    },
                                    "workflow_overrides": effective_overrides,
                                })
                        unused_selections = (
                            set(selected_workflows) - planned_workflow_selections
                        )
                        if unused_selections:
                            formatted = ", ".join(
                                f"{kind}/{mode}"
                                for kind, mode in sorted(unused_selections)
                            )
                            raise ValidationError({
                                "workflow_selections": (
                                    "所选生产范围没有可应用的镜头: " + formatted
                                ),
                            })
                        batch_fingerprint = _series_batch_fingerprint(
                            project=project,
                            episodes=episodes,
                            production_groups_by_episode=production_groups_by_episode,
                            planned_units=planned_units,
                            data=data,
                            consistency_snapshot=batch_consistency_snapshot,
                        )
                        existing_tasks = _production_batch_tasks(
                            user=request.user,
                            project=project,
                            idempotency_key=idempotency_key,
                        )
                        if existing_tasks:
                            fingerprints = {
                                (task.capability_snapshot or {}).get(
                                    "request_fingerprint"
                                )
                                for task in existing_tasks
                            }
                            if fingerprints != {batch_fingerprint}:
                                raise ProductionIdempotencyConflict({
                                    "detail": (
                                        "该 idempotency_key 已用于不同的生产请求；"
                                        "修改剧集、分镜或工作流参数后必须使用新的键"
                                    ),
                                    "idempotency_key": idempotency_key,
                                })
                            return Response(
                                _series_batch_response(
                                    project=project,
                                    tasks=existing_tasks,
                                    idempotency_key=idempotency_key,
                                    idempotent_replay=True,
                                ),
                                status=status.HTTP_200_OK,
                            )
                    assert_queue_capacity(
                        request.user,
                        additional=batch_size,
                        extended_generation_confirmed=data[
                            "extended_generation_confirmed"
                        ],
                    )
            batch_index = 0
            for episode in episodes:
                if whole_episode:
                    shots, groups = production_groups_by_episode.get(
                        episode.id,
                        (whole_episode_shots or [], []),
                    )
                else:
                    shots, groups = production_groups_by_episode.get(episode.id, ([], []))
                if not shots:
                    continue
                for group in groups:
                    first_shot = group[0]
                    group_batch_index = None
                    if (
                        batch_id
                        and (
                            whole_episode
                            or str(first_shot.id) not in deferred_start_shot_ids
                        )
                    ):
                        batch_index += 1
                        group_batch_index = batch_index
                    if str(first_shot.id) in deferred_start_shot_ids:
                        (
                            deferred_selection_key,
                            deferred_matched_selection,
                            deferred_template,
                            deferred_overrides,
                        ) = production_template_and_overrides_for_shot(first_shot)
                        if deferred_matched_selection:
                            used_workflow_selections.add(deferred_selection_key)
                        previous_shot = previous_video_shot_for(first_shot)
                        if previous_shot is None:
                            raise ValidationError({
                                "shot_ids": (
                                    f"镜头 {first_shot.number} 启用了首帧承接，但没有可用上一镜。"
                                ),
                            })
                        source_task = created_task_by_shot_id.get(str(previous_shot.id))
                        if source_task is None:
                            source_task = latest_viable_video_task_for_shot(
                                user=request.user,
                                project=project,
                                shot=previous_shot,
                            )
                        if source_task is not None:
                            try:
                                deferred_request, _created = ensure_auto_first_frame_deferred(
                                    user=request.user,
                                    project=project,
                                    source_task=source_task,
                                    target_shot=first_shot,
                                    priority=data["priority"],
                                    max_attempts=data["max_attempts"],
                                    extended_generation_confirmed=data[
                                        "extended_generation_confirmed"
                                    ],
                                    request_key=data["idempotency_key"],
                                    preferred_template=deferred_template,
                                    target_workflow_overrides=deferred_overrides,
                                )
                            except (PromptBuildError, WorkflowBuildError) as exc:
                                raise ValidationError(str(exc)) from exc
                            deferred_requests.append(deferred_request)
                            deferred_request_by_target_shot_id[str(first_shot.id)] = deferred_request
                            deferred_chain_source_shot_ids.add(str(first_shot.id))
                            deferred_chain_root_request_by_shot_id[str(first_shot.id)] = deferred_request
                            production_units.append(_production_unit_from_deferred_request(
                                episode=episode,
                                target_shot=first_shot,
                                deferred_request=deferred_request,
                                source_task=source_task,
                                batch_id=batch_id,
                                batch_kind=batch_kind,
                                batch_index=group_batch_index,
                                batch_size=batch_size,
                                batch_shot_ids=(
                                    whole_episode_batch_shot_ids if whole_episode else None
                                ),
                            ))
                            continue
                        upstream_deferred = (
                            deferred_request_by_target_shot_id.get(str(previous_shot.id))
                            or active_first_frame_deferred_for(
                                previous_shot,
                                request_key=data["idempotency_key"],
                            )
                        )
                        if upstream_deferred is not None:
                            deferred_chain_source_shot_ids.add(str(first_shot.id))
                            deferred_chain_root_request_by_shot_id[str(first_shot.id)] = upstream_deferred
                            production_units.append(_production_unit_waiting_for_deferred_source(
                                episode=episode,
                                target_shot=first_shot,
                                upstream_request=upstream_deferred,
                                batch_id=batch_id,
                                batch_kind=batch_kind,
                                batch_index=group_batch_index,
                                batch_size=batch_size,
                                batch_shot_ids=(
                                    whole_episode_batch_shot_ids if whole_episode else None
                                ),
                            ))
                            continue
                        if str(previous_shot.id) in deferred_chain_source_shot_ids:
                            root_request = deferred_chain_root_request_by_shot_id.get(
                                str(previous_shot.id),
                            )
                            deferred_chain_source_shot_ids.add(str(first_shot.id))
                            if root_request is not None:
                                deferred_chain_root_request_by_shot_id[str(first_shot.id)] = root_request
                            production_units.append(_production_unit_waiting_for_deferred_chain(
                                episode=episode,
                                target_shot=first_shot,
                                upstream_shot=previous_shot,
                                root_request=root_request,
                                batch_id=batch_id,
                                batch_kind=batch_kind,
                                batch_index=group_batch_index,
                                batch_size=batch_size,
                                batch_shot_ids=(
                                    whole_episode_batch_shot_ids if whole_episode else None
                                ),
                            ))
                            continue
                        raise ValidationError({
                            "shot_ids": (
                                f"镜头 {first_shot.number} 启用了首帧承接，但上一镜 "
                                f"{previous_shot.number} 尚无可用视频任务。请先生成上一镜，"
                                "或把上一镜与本镜一起提交。"
                            ),
                        })
                    if (
                        data["enqueue"]
                        and len(group) == 1
                        and first_shot.media_type == Shot.MediaType.VIDEO
                        and first_shot.requires_first_frame_continuity
                        and not target_requires_deferred_first_frame(first_shot)
                    ):
                        raise ValidationError({
                            "shot_ids": (
                                f"镜头 {first_shot.number} 启用了首帧承接，但当前 "
                                f"{first_shot.h3_mode} 模式不能接收首帧连续参考。"
                            ),
                        })
                    output_kind = first_shot.media_type
                    group_key = tuple(str(item.id) for item in group)
                    resolved_idempotent_group = resolved_idempotent_groups.get(group_key)
                    if resolved_idempotent_group:
                        template = resolved_idempotent_group["template"]
                        selection_key = resolved_idempotent_group["selection_key"]
                        if selection_key:
                            used_workflow_selections.add(selection_key)
                        matched_selection = selected_workflows.get(selection_key)
                    else:
                        selection_key = (output_kind, first_shot.h3_mode)
                        matched_selection = selected_workflows.get(selection_key)
                        if matched_selection:
                            used_workflow_selections.add(selection_key)
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
                            consistency_snapshot=batch_consistency_snapshot,
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
                        if resolved_idempotent_group:
                            selected_overrides = dict(
                                resolved_idempotent_group["workflow_overrides"]
                            )
                        else:
                            selected_overrides = dict(
                                image_workflow_overrides
                                if output_kind == PromptArtifact.PromptType.IMAGE
                                else video_workflow_overrides
                            )
                            if matched_selection:
                                selected_overrides.update(matched_selection["parameters"])
                        workflow = materialize_generated_workflow(
                            user=request.user,
                            template=template,
                            prompt=prompt,
                            overrides=selected_overrides,
                        )
                        injected = workflow.injected_values
                    except (PromptBuildError, WorkflowBuildError) as exc:
                        raise ValidationError(str(exc)) from exc
                    generated_prompts.append(prompt)
                    workflows.append(workflow)
                    task = None
                    if data["enqueue"]:
                        capability_overrides = {
                            "extended_generation_confirmed": bool(
                                data["extended_generation_confirmed"]
                            ),
                        }
                        if batch_id:
                            if whole_episode and len(group) != 1:
                                raise ValidationError(
                                    "整集生成内部契约错误：每个子任务必须只包含一个分镜"
                                )
                            child_shot = group[0] if len(group) == 1 else None
                            capability_overrides.update({
                                "batch_id": batch_id,
                                "batch_kind": batch_kind,
                                "batch_role": "child",
                                "batch_index": group_batch_index,
                                "batch_size": batch_size,
                                **({
                                    "shot_version_key": whole_episode_version_key,
                                    "shot_version_label": whole_episode_version_label,
                                    "batch_shot_ids": whole_episode_batch_shot_ids,
                                } if whole_episode else {}),
                                "episode_id": str(episode.id),
                                "episode_number": episode.number,
                                "episode_title": episode.title,
                                "shot_ids": [str(item.id) for item in group],
                                "shot_id": str(child_shot.id) if child_shot else None,
                                "shot_number": (
                                    shot_version_number(child_shot)
                                    if child_shot else None
                                ),
                                "shot_title": child_shot.title if child_shot else "",
                                "prompt_artifact_reused": bool(
                                    getattr(prompt, "_production_prompt_reused", False)
                                ),
                                **({
                                    "include_audio": child_shot.include_audio,
                                    "requested_duration_ms": child_shot.duration_ms,
                                } if child_shot and output_kind == Shot.MediaType.VIDEO else {}),
                                **({
                                    "idempotency_key": data["idempotency_key"],
                                    "request_fingerprint": batch_fingerprint,
                                } if data["idempotency_key"] else {}),
                            })
                        task = materialize_generation_task(
                            user=request.user, workflow=workflow,
                            priority=data["priority"], max_attempts=data["max_attempts"],
                            capability_snapshot_overrides=capability_overrides,
                        )
                        tasks.append(task)
                        if (
                            len(group) == 1
                            and output_kind == Shot.MediaType.VIDEO
                        ):
                            created_task_by_shot_id[str(group[0].id)] = task
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
                            "batch_id": batch_id,
                            "batch_kind": batch_kind or None,
                            "batch_index": group_batch_index,
                            "batch_size": batch_size if batch_id else None,
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
            if tasks:
                response_payload = _episode_batch_response(
                    project=project,
                    tasks=tasks,
                    idempotency_key=data["idempotency_key"],
                    idempotent_replay=False,
                )
                response_payload["production_units"] = production_units
            else:
                response_payload = {
                    "project_id": str(project.id),
                    "whole_episode": True,
                    "shot_version_key": whole_episode_version_key,
                    "shot_version_label": whole_episode_version_label,
                    "batch_shot_ids": whole_episode_batch_shot_ids,
                    "batch_id": batch_id,
                    "batch_kind": "episode",
                    "batch_size": batch_size,
                    "extended_generation_confirmed": data["extended_generation_confirmed"],
                    "idempotency_key": data["idempotency_key"],
                    "idempotent_replay": False,
                    "episode_ids": [str(item.id) for item in episodes],
                    "prompt_artifact_ids": [],
                    "workflow_ids": [],
                    "task_ids": [],
                    "counts": {"prompts": 0, "workflows": 0, "tasks": 0},
                    "production_units": production_units,
                    "tasks": [],
                }
            response_payload["deferred_generation_request_ids"] = [
                str(item.id) for item in deferred_requests
            ]
            if deferred_requests:
                response_payload["counts"]["deferred_generation_requests"] = len(deferred_requests)
            response_payload["prompt_language_gate"] = prompt_language_gate_reports
            return Response(response_payload, status=status.HTTP_201_CREATED)
        if data["idempotency_key"]:
            if tasks:
                response_payload = _series_batch_response(
                    project=project,
                    tasks=tasks,
                    idempotency_key=data["idempotency_key"],
                    idempotent_replay=False,
                )
                response_payload["production_units"] = production_units
            else:
                response_payload = {
                    "project_id": str(project.id),
                    "whole_episode": False,
                    "batch_id": batch_id,
                    "batch_kind": batch_kind or None,
                    "batch_size": batch_size,
                    "extended_generation_confirmed": data["extended_generation_confirmed"],
                    "idempotency_key": data["idempotency_key"],
                    "idempotent_replay": False,
                    "episode_ids": [str(item.id) for item in episodes],
                    "prompt_artifact_ids": [],
                    "workflow_ids": [],
                    "task_ids": [],
                    "counts": {"prompts": 0, "workflows": 0, "tasks": 0},
                    "production_units": production_units,
                    "tasks": [],
                    "prompt_language_gate": prompt_language_gate_reports,
                }
            response_payload["deferred_generation_request_ids"] = [
                str(item.id) for item in deferred_requests
            ]
            if deferred_requests:
                response_payload["counts"]["deferred_generation_requests"] = len(deferred_requests)
            return Response(response_payload, status=status.HTTP_201_CREATED)
        counts = {
            "prompts": len(generated_prompts),
            "workflows": len(workflows),
            "tasks": len(tasks),
        }
        if deferred_requests:
            counts["deferred_generation_requests"] = len(deferred_requests)
        return Response({
            "project_id": str(project.id),
            "episode_ids": [str(item.id) for item in episodes],
            "prompt_artifact_ids": [str(item.id) for item in generated_prompts],
            "workflow_ids": [str(item.id) for item in workflows],
            "task_ids": [str(item.id) for item in tasks],
            "deferred_generation_request_ids": [str(item.id) for item in deferred_requests],
            "batch_id": batch_id,
            "batch_kind": batch_kind or None,
            "batch_size": batch_size,
            "extended_generation_confirmed": data["extended_generation_confirmed"],
            "counts": counts,
            "production_units": production_units,
            "prompt_language_gate": prompt_language_gate_reports,
        }, status=status.HTTP_201_CREATED)


class ProjectChildViewSet(viewsets.ModelViewSet):
    permission_classes = [IsOwnerOrStaff]

    def owned(self, queryset):
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)


class BibleProjectChildViewSet(ProjectChildViewSet):
    """Serialize every ordinary Bible write behind the project lock."""

    @staticmethod
    def _validate_locked_metadata(previous_metadata, incoming_metadata):
        try:
            validate_untrusted_script_bible_metadata(
                previous_metadata,
                incoming_metadata,
            )
        except ScriptBibleProvenanceProtectedError as exc:
            raise ValidationError({"metadata": str(exc)}) from exc

    def perform_create(self, serializer):
        requested_project = serializer.validated_data["project"]
        with transaction.atomic():
            try:
                project = SeriesProject.objects.select_for_update().get(
                    pk=requested_project.pk,
                )
            except SeriesProject.DoesNotExist as exc:
                raise ValidationError({"project": "项目已不存在，请刷新后重试。"}) from exc
            self.check_object_permissions(self.request, project)
            if "metadata" in serializer.validated_data:
                self._validate_locked_metadata(
                    {}, serializer.validated_data["metadata"],
                )
            serializer.save(project=project)

    def perform_update(self, serializer):
        model = type(serializer.instance)
        requested_project = serializer.validated_data.get(
            "project", serializer.instance.project,
        )
        with transaction.atomic():
            current_project_id = model.objects.filter(
                pk=serializer.instance.pk,
            ).values_list("project_id", flat=True).first()
            if current_project_id is None:
                raise NotFound("Bible 已不存在。")
            requested_project_id = requested_project.pk
            locked_projects = {
                project.pk: project
                for project in SeriesProject.objects.select_for_update()
                .filter(pk__in={current_project_id, requested_project_id})
                .order_by("pk")
            }
            if len(locked_projects) != len({current_project_id, requested_project_id}):
                raise ValidationError({"project": "项目已不存在，请刷新后重试。"})
            try:
                locked = model.objects.select_for_update().select_related(
                    "project",
                ).get(pk=serializer.instance.pk)
            except model.DoesNotExist as exc:
                raise NotFound("Bible 已不存在。") from exc
            if locked.project_id != current_project_id:
                raise ValidationError({
                    "project": "Bible 所属项目已被并发修改，请刷新后重试。",
                })
            self.check_object_permissions(self.request, locked)
            self.check_object_permissions(
                self.request, locked_projects[requested_project_id],
            )
            serializer.instance = locked
            if "metadata" in serializer.validated_data:
                self._validate_locked_metadata(
                    locked.metadata,
                    serializer.validated_data["metadata"],
                )
            if "project" in serializer.validated_data:
                serializer.validated_data["project"] = locked_projects[
                    requested_project_id
                ]
            serializer.save()


class CharacterBibleViewSet(BibleProjectChildViewSet):
    serializer_class = CharacterBibleSerializer
    filterset_fields = ("project", "is_locked", "stable_speaker_id")
    search_fields = ("name", "role", "visual_identity", "face_identity")

    def get_queryset(self):
        return self.owned(CharacterBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class SceneBibleViewSet(BibleProjectChildViewSet):
    serializer_class = SceneBibleSerializer
    filterset_fields = ("project", "is_locked", "time_of_day")
    search_fields = ("name", "location", "visual_identity")

    def get_queryset(self):
        return self.owned(SceneBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class PropBibleViewSet(BibleProjectChildViewSet):
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
    "requires_first_frame_continuity", "requires_last_frame_continuity",
    "continuity_notes", "metadata",
)


def _shot_sync_version_key(shot: Shot) -> str:
    return canonical_shot_version_key(shot)


def _shot_sync_version_number(shot: Shot) -> int:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    try:
        number = int(metadata.get("creative_version_shot_number"))
    except (TypeError, ValueError):
        number = shot.number
    return number if number > 0 else shot.number


def _sync_episode_shots(
    *, episode: Episode, user, items: list[dict], requested_version_key: str,
):
    try:
        sync_version_key = normalize_requested_shot_version_key(
            requested_version_key,
        )
    except ShotVersionResolutionError as exc:
        raise ValidationError({"shot_version_key": str(exc)}) from exc
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
    scoped_existing = [
        shot for shot in existing
        if _shot_sync_version_key(shot) == sync_version_key
    ]
    scoped_existing_ids = {shot.pk for shot in scoped_existing}
    if existing and not scoped_existing:
        raise ValidationError({
            "shot_version_key": "所选分镜版本不存在或已被删除，请刷新后重试。",
        })
    foreign_requested_ids = requested_ids - scoped_existing_ids
    if foreign_requested_ids:
        raise ValidationError({
            "shots": "提交内容包含其他分镜版本的镜头；本次未保存任何修改。",
        })
    sync_version_template = (
        dict(scoped_existing[0].metadata or {}) if scoped_existing else {}
    )
    if not existing and sync_version_key != legacy_shot_version_key(episode.pk):
        raise ValidationError({
            "shot_version_key": "空剧集只能从确定性的初始分镜版本开始创建。",
        })
    for item in items:
        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError({"shots": "metadata 必须是 JSON 对象。"})
        submitted_key = str((metadata or {}).get("creative_version_key") or "").strip()
        if submitted_key and submitted_key != sync_version_key:
            raise ValidationError({
                "shots": "提交的分镜版本字段与当前选择不一致；请刷新后重试。",
            })

    delete_ids = scoped_existing_ids - requested_ids
    deleted_count = len(delete_ids)
    if delete_ids:
        protected_count = ShotDetailDraftBatchItem.objects.filter(
            shot_id__in=delete_ids,
        ).count()
        if protected_count:
            raise ValidationError({
                "shots": (
                    "所选版本已有整集高细节扩写历史，不能删除其中的正式镜头；"
                    "请基于当前内容生成新的分镜版本后再比较。"
                ),
            })
        Shot.objects.filter(pk__in=delete_ids).delete()
    reassign_physical_numbers = (
        sync_version_key == legacy_shot_version_key(episode.pk)
        and scoped_existing_ids == set(existing_by_id)
    )
    retained = [item for item in existing if item.pk in requested_ids]
    next_db_number = int(Shot.objects.filter(episode=episode).aggregate(value=Max("number"))["value"] or 0) + 1
    if reassign_physical_numbers:
        final_numbers = [item["number"] for item in items]
        temp_base = (
            max([item.number for item in retained] + final_numbers + [0])
            + len(retained)
            + 1000
        )
        for offset, retained_shot in enumerate(retained):
            Shot.objects.filter(pk=retained_shot.pk).update(
                number=temp_base + offset,
            )

    created_count = 0
    saved = []
    for data in items:
        instance = existing_by_id.get(data.get("id"))
        original_metadata = dict(instance.metadata or {}) if instance else {}
        if instance is None:
            instance = Shot(episode=episode)
            if reassign_physical_numbers:
                instance.number = data["number"]
            else:
                instance.number = next_db_number
                next_db_number += 1
            created_count += 1
        elif reassign_physical_numbers:
            instance.number = data["number"]
        for field in _SHOT_SCALAR_FIELDS:
            if field in data and field != "metadata":
                setattr(instance, field, data[field])
        submitted_metadata = data.get("metadata")
        metadata = dict(
            submitted_metadata
            if isinstance(submitted_metadata, dict)
            else original_metadata
        )
        for key in RESERVED_SHOT_VERSION_METADATA_KEYS:
            metadata.pop(key, None)
        authoritative_version_metadata = original_metadata or sync_version_template
        for key in RESERVED_SHOT_VERSION_METADATA_KEYS:
            if key in authoritative_version_metadata:
                metadata[key] = authoritative_version_metadata[key]
        metadata["creative_version_key"] = sync_version_key
        metadata["creative_version_label"] = str(
            sync_version_template.get("creative_version_label")
            or (
                "初始分镜"
                if sync_version_key == legacy_shot_version_key(episode.pk)
                else "分镜版本"
            )
        )
        metadata["creative_version_active"] = True
        metadata["creative_version_shot_number"] = data["number"]
        metadata["creative_version_shot_count"] = len(items)
        instance.metadata = metadata

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
                "shots": (
                    f"分镜 {instance.number} 的视频时长必须在 "
                    f"{VIDEO_SHOT_MIN_DURATION_MS}–{VIDEO_SHOT_MAX_DURATION_MS}ms"
                ),
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
    saved_ids = {item.pk for item in saved}
    for shot in Shot.objects.select_for_update().filter(episode=episode):
        metadata = dict(shot.metadata or {})
        key = _shot_sync_version_key(shot)
        desired_active = key == sync_version_key and shot.pk in saved_ids
        if (
            metadata.get("creative_version_active") is desired_active
            and (
                key != sync_version_key
                or metadata.get("creative_version_shot_count") == len(saved)
            )
        ):
            continue
        metadata["creative_version_active"] = desired_active
        if key == sync_version_key:
            metadata["creative_version_shot_count"] = len(saved)
        shot.metadata = metadata
        shot.save(update_fields=("metadata", "updated_at"))
    saved.sort(key=lambda shot: (_shot_sync_version_number(shot), shot.number))
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
                episode=episode,
                user=request.user,
                items=serializer.validated_data["shots"],
                requested_version_key=serializer.validated_data["shot_version_key"],
            )
        shots = list(Shot.objects.filter(pk__in=[item.pk for item in saved]).select_related("scene").prefetch_related(
            "characters", "props", "reference_assets",
        ))
        saved_order = {item.pk: index for index, item in enumerate(saved)}
        shots.sort(key=lambda item: saved_order.get(item.pk, 0))
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

    def __init__(self, detail=None, code=None):
        super().__init__(detail, code)
        # Keep the allow-listed conflict payload typed. APIException otherwise
        # coerces booleans and revision counters to ErrorDetail strings.
        if detail is not None:
            self.detail = detail


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
    current_context = build_translation_context(
        entry.project,
        scope_type=entry.scope_type,
        scope_id=entry.scope_id,
        field_key=entry.field_key,
        source_text=current_source,
    )
    stored_context_hash = str(
        (entry.runtime_metadata or {}).get("translation_context_hash") or ""
    )
    if stored_context_hash == translation_context_hash(current_context):
        missing_mappings = missing_relevant_canonical_identity_mappings(
            entry.project, current_source, candidate,
        )
        if missing_mappings:
            raise TranslationPromotionConflict({
                "code": "canonical_target_missing",
                "detail": "修改后的英文译文缺少当前视觉与术语规则要求保留的 canonical 名称。",
                "missing_mappings": missing_mappings,
            })
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
        entry,
        event="translation_promoted" if target and shot is not None else "translation_confirmed",
        actor=user,
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

    @extend_schema(
        request=PromptTranslationConfirmSerializer,
        responses={
            200: PromptFieldTranslationSerializer,
            409: PromptTranslationConflictErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        serializer = PromptTranslationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visible_entry = self.get_object()
        with transaction.atomic():
            project = _lock_translation_project(visible_entry.project, request.user)
            entry = self.get_queryset().select_for_update().get(
                pk=visible_entry.pk, project=project,
            )
            self.check_object_permissions(request, entry)
            entry.project = project
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
            shot_queryset = Shot.objects.select_related("episode__project")
            if not request.user.is_staff:
                shot_queryset = shot_queryset.filter(
                    episode__project__owner=request.user,
                )
            try:
                visible_shot = shot_queryset.get(pk=data["shot_id"])
            except Shot.DoesNotExist as exc:
                raise TranslationPromotionConflict({
                    "code": "shot_not_found", "shot_id": str(data["shot_id"]),
                }) from exc
            project = _lock_translation_project(
                visible_shot.episode.project, request.user,
            )
            rows = list(
                self.get_queryset().select_for_update().filter(
                    project=project,
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
                entry.project = project
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
        visible_entry = self.get_object()
        with transaction.atomic():
            project = _lock_translation_project(visible_entry.project, request.user)
            entry = self.get_queryset().select_for_update().get(
                pk=visible_entry.pk, project=project,
            )
            self.check_object_permissions(request, entry)
            entry.project = project
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
        visible_entry = self.get_object()
        with transaction.atomic():
            project = _lock_translation_project(visible_entry.project, request.user)
            entry = self.get_queryset().select_for_update().get(
                pk=visible_entry.pk, project=project,
            )
            self.check_object_permissions(request, entry)
            entry.project = project
            _append_translation_history(entry)
            entry.revision += 1
            entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
            entry.confirmed_by = None
            entry.confirmed_at = None
            entry.error_message = ""
            entry.save()
        return Response(PromptFieldTranslationSerializer(entry).data)


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
        compiled = compile_prompt_input(
            project=project,
            shots=group,
            prompt_type=prompt_type,
            mode=mode,
            include_audio=include_audio,
            duration_overrides=duration_overrides,
            overall_soundscape=overall_soundscape,
            non_diegetic_music=non_diegetic_music,
            extra_direction=extra_direction,
            single_shot_reference_bindings=single_shot_reference_bindings,
            single_shot_reference_assets=single_shot_reference_assets,
            consistency_snapshot=snapshot,
        )
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
                input_fingerprint=compiled.input_fingerprint,
                shot_ids=shot_ids,
                consistency_snapshot=snapshot,
                reference_bindings=compiled.reference_bindings,
                negative_prompt=compiled.negative_prompt,
            ):
                latest._production_prompt_reused = True
                artifacts.append(latest)
                continue
        artifact = materialize_compiled_prompt(
            compiled=compiled,
            created_by=user,
        )
        artifacts.append(artifact)
    return artifacts


def _partition_h3_shots(shots: list[Shot]) -> list[list[Shot]]:
    """Partition one ordered video run into configured production-duration jobs."""
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
            if duration > VIDEO_SHOT_MAX_DURATION_MS:
                break
            if duration < VIDEO_SHOT_MIN_DURATION_MS:
                continue
            candidate = [*best[start], (start, end + 1)]
            current = best[end + 1]
            if current is None or len(candidate) < len(current):
                best[end + 1] = candidate
    if best[size] is None:
        numbers = ", ".join(str(item.number) for item in shots)
        raise PromptBuildError(
            f"视频分镜 [{numbers}] 无法按连续顺序划分为 "
            f"{VIDEO_SHOT_MIN_DURATION_MS}–{VIDEO_SHOT_MAX_DURATION_MS}ms 的 H3 任务；"
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
        if shot.requires_first_frame_continuity or shot.requires_last_frame_continuity:
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
        templates = executable_templates(
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
        if (
            isinstance(prompt.request_options, dict)
            and prompt.request_options.get("generation_purpose")
            == BIBLE_IMAGE_CANDIDATE_PURPOSE
        ):
            raise BibleImageCandidateReservedOperation()
        try:
            template = executable_templates(WorkflowTemplate.objects.all(), request.user).get(pk=data["template_id"])
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
        try:
            workflow = materialize_generated_workflow(
                user=request.user,
                template=template,
                prompt=prompt,
                overrides=data["overrides"],
            )
        except WorkflowBuildError as exc:
            raise ValidationError(str(exc)) from exc
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
            queryset = queryset.filter(
                Q(capability_snapshot__batch_id=batch_id)
                | Q(deferred_generation_request__source_task__capability_snapshot__batch_id=batch_id)
                | Q(
                    deferred_generation_request__source_task__deferred_generation_request__source_task__capability_snapshot__batch_id=batch_id
                )
            ).order_by(
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

    @extend_schema(
        request=GenerationTaskCreateSerializer,
        responses={
            201: GenerationTaskSerializer,
            409: ProductionQueueConfirmationErrorResponseSerializer,
        },
    )
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
        if (
            isinstance(workflow.prompt_artifact.request_options, dict)
            and workflow.prompt_artifact.request_options.get("generation_purpose")
            == BIBLE_IMAGE_CANDIDATE_PURPOSE
        ):
            raise BibleImageCandidateReservedOperation()
        if not can_execute_template(workflow.template, request.user):
            raise ValidationError("The workflow template is not executable by this user.")
        _require_prompt_language_gate_for_enqueue(
            workflow.prompt_artifact,
            workflow_id=str(workflow.id),
            episode_id=str(workflow.episode_id) if workflow.episode_id else None,
            shot_ids=list((workflow.prompt_artifact.request_options or {}).get("shot_ids") or []),
        )
        with transaction.atomic():
            try:
                SeriesProject.objects.select_for_update().get(pk=workflow.project_id)
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            assert_queue_capacity(
                request.user,
                extended_generation_confirmed=data[
                    "extended_generation_confirmed"
                ],
            )
            task = materialize_generation_task(
                user=request.user, workflow=workflow,
                priority=data["priority"], max_attempts=data["max_attempts"],
                capability_snapshot_overrides={
                    "extended_generation_confirmed": bool(
                        data["extended_generation_confirmed"]
                    ),
                },
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
            if task.downstream_deferred_requests.exists():
                raise ValidationError("该任务是延迟首尾帧连续生成的上游来源，不能删除。")
            if hasattr(task, "deferred_generation_request"):
                raise ValidationError("该任务是延迟首尾帧连续生成物化出的下游任务，不能删除。")
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
        responses={
            200: AssetSerializer,
            201: AssetSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            404: BibleImageCandidateErrorResponseSerializer,
            409: BibleImageCandidateErrorResponseSerializer,
        },
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
        accept_stale_bible_image_candidate = bool(
            request_serializer.validated_data.get(
                "accept_stale_bible_image_candidate",
            )
        )
        reference_category = request_serializer.validated_data.get("reference_category") or ""
        requested_entity_id = request_serializer.validated_data.get("entity_id")

        with _PromotionFileCommitGuard() as promotion_file_guard, transaction.atomic():
            scoped_project_id = self.get_queryset().filter(pk=pk).values_list(
                "project_id", flat=True,
            ).first()
            if scoped_project_id is None:
                raise NotFound("任务不存在")
            locked_project = SeriesProject.objects.select_for_update().select_related(
                "style",
            ).get(pk=scoped_project_id)
            try:
                task = self.get_queryset().select_for_update().select_related(
                    "project__style", "generated_workflow__prompt_artifact__shot",
                ).get(pk=pk)
            except GenerationTask.DoesNotExist as exc:
                raise NotFound("任务不存在") from exc
            # Use the locked, freshly loaded project for every source-fingerprint
            # read below instead of the select_related snapshot on the task.
            task.project = locked_project
            if task.status != GenerationTask.Status.SUCCEEDED:
                raise ValidationError({"output_index": "只有已完成任务的图片产物可以保存为素材"})
            if not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
                raise ValidationError({"output_index": "产物序号不存在"})
            output = task.outputs[output_index]
            if classify_task_output(output) != "image":
                raise ValidationError({"output_index": "当前产物不是图片，不能保存为图片素材"})
            frozen_target = validated_frozen_bible_target(task)
            if frozen_target is not None:
                frozen_category = frozen_target["reference_category"]
                frozen_entity_id = frozen_target["entity_id"]
                if reference_category and (
                    reference_category != frozen_category
                    or str(requested_entity_id) != frozen_entity_id
                ):
                    raise BibleImageCandidateAPIError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="bible_image_candidate_target_conflict",
                        detail={
                            "detail": (
                                "该产物只能推广到任务创建时冻结的 Bible 实体；"
                                "不能在确认阶段改绑。"
                            ),
                        },
                    )
                reference_category = frozen_category
                requested_entity_id = uuid.UUID(frozen_entity_id)
            requested_target = {
                "reference_category": reference_category,
                "entity_id": str(requested_entity_id) if requested_entity_id else "",
            }
            asset_kind_label = {
                Asset.Kind.CHARACTER: "人物",
                Asset.Kind.SCENE: "场景",
                Asset.Kind.PROP: "道具",
            }.get(reference_category, "")
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
                    if frozen_target is not None:
                        raise BibleImageCandidateAPIError(
                            status_code=status.HTTP_409_CONFLICT,
                            code="bible_image_candidate_target_unavailable",
                            detail={
                                "detail": "任务冻结的 Bible 实体已不存在；系统不会改绑到其他实体。",
                            },
                        )
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
                    if frozen_target is not None:
                        raise BibleImageCandidateAPIError(
                            status_code=status.HTTP_409_CONFLICT,
                            code="bible_image_candidate_target_conflict",
                            detail={"detail": "已推广目标与任务冻结的 Bible 实体不一致。"},
                        )
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

            bible_candidate_source_status = None
            if frozen_target is not None:
                bible_candidate_source_status = assert_bible_image_candidate_source_current(
                    task=task,
                    entity=entity,
                    frozen_target=frozen_target,
                    allow_stale=accept_stale_bible_image_candidate,
                )

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
                if frozen_target is not None:
                    raise BibleImageCandidateAPIError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="bible_image_candidate_provenance_missing",
                        detail={
                            "detail": (
                                "实体图片候选产物缺少生成时封存的来源清单，"
                                "不能用当前配置现场补写后推广。"
                            ),
                        },
                    )
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
            confirmed_at = timezone.now()
            bible_candidate_metadata = (
                bible_image_candidate_promotion_metadata(
                    task=task,
                    output_index=output_index,
                    entity=entity,
                    manifest=manifest,
                    confirmed_by=request.user,
                    confirmed_at=confirmed_at,
                    source_status=bible_candidate_source_status,
                )
                if frozen_target is not None else None
            )
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
                        "manifest_sha256": str((manifest.get("integrity") or {}).get("payload_sha256") or ""),
                        "reference_category": reference_category or None,
                        "entity_id": str(entity.id) if entity is not None else None,
                        **({
                            "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
                            "source_fingerprint": frozen_target["source_fingerprint"],
                            "source_current": bool(
                                (bible_candidate_source_status or {}).get(
                                    "source_current",
                                    True,
                                )
                            ),
                            "current_source_fingerprint": str(
                                (bible_candidate_source_status or {}).get(
                                    "current_source_fingerprint",
                                    frozen_target["source_fingerprint"],
                                )
                            ),
                            "stale_source_accepted": not bool(
                                (bible_candidate_source_status or {}).get(
                                    "source_current",
                                    True,
                                )
                            ),
                            "request_fingerprint": frozen_target["request_fingerprint"],
                        } if frozen_target is not None else {}),
                    },
                    "reference_category": reference_category or None,
                    "reference_entity_id": str(entity.id) if entity is not None else None,
                    **({
                        "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
                        "bible_image_candidate": bible_candidate_metadata,
                    } if bible_candidate_metadata is not None else {}),
                },
            )
            try:
                with source_path.open("rb") as source_stream:
                    asset.file.save(
                        f"generated-{task.id}-{output_index}{suffix}",
                        File(source_stream),
                        save=False,
                    )
                promotion_file_guard.track(asset.file)
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
        downstream_cancellations = {
            "deferred_requests_cancelled": 0,
            "tasks_cancelled": 0,
            "tasks_cancel_requested": 0,
            "deferred_request_ids": [],
            "task_ids": [],
        }
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
            downstream_cancellations = cascade_cancel_downstream_generation(
                source_task=task,
                user=request.user,
            )
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
                    "downstream_cancellations": downstream_cancellations,
                }, status=status.HTTP_202_ACCEPTED)
            if remote_history is None and still_queued:
                task.remote_error = cancel_warning
                task.save(update_fields=("remote_error", "updated_at"))
                return Response({
                    "status": task.status,
                    "warning": cancel_warning or "ComfyUI 已收到取消请求，正在等待远端任务退出",
                    "downstream_cancellations": downstream_cancellations,
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
        downstream_cancellations = cascade_cancel_downstream_generation(
            source_task=task,
            user=request.user,
        )
        payload = self.get_serializer(task).data
        payload["downstream_cancellations"] = downstream_cancellations
        return Response(payload)

    @staticmethod
    def _batch_index(task: GenerationTask) -> int:
        try:
            return int(effective_batch_snapshot_for_task(task).get("batch_index", 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _validation_summary(exc: ValidationError) -> str:
        detail = exc.detail
        if isinstance(detail, dict):
            try:
                return json.dumps(detail, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return str(detail)
        if isinstance(detail, list):
            return "；".join(str(item) for item in detail)
        return str(detail)

    def _batch_tasks_locked(self, source: GenerationTask) -> tuple[dict, list[GenerationTask]]:
        source_snapshot = effective_batch_snapshot_for_task(source)
        batch_id = str(source_snapshot.get("batch_id") or "")
        if not batch_id:
            raise ValidationError({"batch_id": "该任务不属于可操作的生产批次。"})
        batch_tasks = list(
            production_batch_task_queryset(
                owner_id=source.owner_id,
                project_id=source.project_id,
                batch_id=batch_id,
            )
            .select_for_update()
            .select_related(
                "generated_workflow__template",
                "generated_workflow__prompt_artifact",
            )
        )
        batch_tasks = sorted(
            batch_tasks,
            key=lambda item: (
                self._batch_index(item),
                item.queued_at or item.created_at,
                item.created_at,
                str(item.id),
            ),
        )
        return source_snapshot, batch_tasks

    def _mark_batch_task_cancelled_locked(self, task: GenerationTask) -> str:
        task.refresh_from_db()
        if task.status in {
            GenerationTask.Status.SUCCEEDED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        }:
            return "skipped"
        now = timezone.now()
        terminal_submission_states = {
            GenerationTask.SubmissionState.TERMINAL_FAILED,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        }
        remote_token = task.submission_token or task.comfy_prompt_id
        needs_remote_cancel = bool(remote_token) and (
            task.status in {
                GenerationTask.Status.RUNNING,
                GenerationTask.Status.CANCEL_REQUESTED,
            }
            or task.submission_state not in terminal_submission_states
        )
        if needs_remote_cancel:
            task.status = GenerationTask.Status.CANCEL_REQUESTED
            task.heartbeat_at = now
            task.remote_state = GenerationTask.RemoteState.CANCELLING
            task.remote_error = "整集批次取消请求已记录，等待 worker/ComfyUI 确认。"
            task.error_message = "整集批次取消请求已记录。"
            task.save(update_fields=(
                "status", "heartbeat_at", "remote_state", "remote_error",
                "error_message", "updated_at",
            ))
            return "cancel_requested"
        task.status = GenerationTask.Status.CANCELLED
        task.finished_at = now
        task.heartbeat_at = now
        task.progress = 0
        task.remote_state = GenerationTask.RemoteState.CANCELLED
        task.remote_error = ""
        task.error_message = "整集批次已取消。"
        if task.submission_token:
            task.submission_state = GenerationTask.SubmissionState.TERMINAL_CANCELLED
        task.save(update_fields=(
            "status", "finished_at", "heartbeat_at", "progress", "remote_state",
            "remote_error", "error_message", "submission_state", "updated_at",
        ))
        return "cancelled"

    def _prepare_task_for_retry_locked(self, task: GenerationTask, user) -> None:
        if not can_execute_template(task.generated_workflow.template, user):
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

    @extend_schema(
        request=GenerationTaskRetrySerializer,
        responses={
            200: ProductionBatchResumeResponseSerializer,
            201: ProductionBatchResumeResponseSerializer,
            409: ProductionQueueConfirmationErrorResponseSerializer,
        },
        description=(
            "Resume the selected production batch by retrying only the latest "
            "failed/cancelled task for each batch index. Existing queued, "
            "running, succeeded, or already-retried work is not duplicated."
        ),
    )
    @action(detail=True, methods=["post"], url_path="resume-batch")
    def resume_batch(self, request, pk=None):
        request_serializer = GenerationTaskRetrySerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        retry_options = request_serializer.validated_data
        source = self.get_object()
        with transaction.atomic():
            try:
                SeriesProject.objects.select_for_update().get(pk=source.project_id)
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            source_snapshot, batch_tasks = self._batch_tasks_locked(source)
            batch_id = str(source_snapshot.get("batch_id") or "")
            latest_by_index: dict[str, GenerationTask] = {}
            for task in sorted(
                batch_tasks,
                key=lambda item: (
                    self._batch_index(item),
                    item.queued_at or item.created_at,
                    item.created_at,
                    str(item.id),
                ),
            ):
                snapshot = effective_batch_snapshot_for_task(task)
                key = str(
                    snapshot.get("batch_index")
                    or snapshot.get("shot_id")
                    or task.id
                )
                latest_by_index[key] = task
            retry_sources: list[GenerationTask] = []
            skipped: list[dict] = []
            for task in sorted(
                latest_by_index.values(),
                key=lambda item: (self._batch_index(item), item.queued_at or item.created_at, str(item.id)),
            ):
                batch_index = self._batch_index(task) or None
                if task.status == GenerationTask.Status.SUCCEEDED:
                    skipped.append({
                        "task_id": str(task.id),
                        "batch_index": batch_index,
                        "status": task.status,
                        "reason": "已成功，跳过。",
                    })
                    continue
                if task.status in {
                    GenerationTask.Status.QUEUED,
                    GenerationTask.Status.RUNNING,
                    GenerationTask.Status.CANCEL_REQUESTED,
                }:
                    skipped.append({
                        "task_id": str(task.id),
                        "batch_index": batch_index,
                        "status": task.status,
                        "reason": "仍在排队或运行中，跳过。",
                    })
                    continue
                try:
                    self._prepare_task_for_retry_locked(task, request.user)
                except ValidationError as exc:
                    skipped.append({
                        "task_id": str(task.id),
                        "batch_index": batch_index,
                        "status": task.status,
                        "reason": self._validation_summary(exc),
                    })
                    continue
                retry_sources.append(task)
            if retry_sources:
                assert_queue_capacity(
                    request.user,
                    additional=len(retry_sources),
                    extended_generation_confirmed=retry_options[
                        "extended_generation_confirmed"
                    ],
                )
            retried_tasks = [
                clone_generation_task_for_retry(
                    task,
                    user=request.user,
                    extended_generation_confirmed=retry_options[
                        "extended_generation_confirmed"
                    ],
                )
                for task in retry_sources
            ]
        response_payload = {
            "batch_id": batch_id,
            "batch_kind": str(source_snapshot.get("batch_kind") or ""),
            "scanned_task_count": len(batch_tasks),
            "retried_task_ids": [str(task.id) for task in retried_tasks],
            "skipped": skipped,
            "tasks": GenerationTaskSerializer(
                retried_tasks,
                many=True,
                context=self.get_serializer_context(),
            ).data,
        }
        response_status = (
            status.HTTP_201_CREATED
            if retried_tasks
            else status.HTTP_200_OK
        )
        return Response(response_payload, status=response_status)

    @extend_schema(
        request=None,
        responses={200: ProductionBatchCancelResponseSerializer},
        description=(
            "Cancel only the exact production batch identified by the selected "
            "task's batch_id. Other historical batches for the same episode or "
            "shot are not touched."
        ),
    )
    @action(detail=True, methods=["post"], url_path="cancel-batch")
    def cancel_batch(self, request, pk=None):
        source = self.get_object()
        with transaction.atomic():
            try:
                SeriesProject.objects.select_for_update().get(pk=source.project_id)
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            source_snapshot, batch_tasks = self._batch_tasks_locked(source)
            batch_id = str(source_snapshot.get("batch_id") or "")
            cancelled_task_ids: list[str] = []
            cancel_requested_task_ids: list[str] = []
            skipped: list[dict] = []
            downstream_summary = {
                "deferred_requests_cancelled": 0,
                "tasks_cancelled": 0,
                "tasks_cancel_requested": 0,
                "deferred_request_ids": [],
                "task_ids": [],
            }
            for task in batch_tasks:
                batch_index = self._batch_index(task) or None
                result = self._mark_batch_task_cancelled_locked(task)
                if result == "skipped":
                    skipped.append({
                        "task_id": str(task.id),
                        "batch_index": batch_index,
                        "status": task.status,
                        "reason": "任务已处于终态，跳过。",
                    })
                    continue
                if result == "cancel_requested":
                    cancel_requested_task_ids.append(str(task.id))
                else:
                    cancelled_task_ids.append(str(task.id))
                downstream = cascade_cancel_downstream_generation(
                    source_task=task,
                    user=request.user,
                )
                for key in (
                    "deferred_requests_cancelled",
                    "tasks_cancelled",
                    "tasks_cancel_requested",
                ):
                    downstream_summary[key] += int(downstream.get(key) or 0)
                for key in ("deferred_request_ids", "task_ids"):
                    downstream_summary[key].extend([
                        str(value) for value in downstream.get(key) or []
                    ])
            refreshed_tasks = list(
                production_batch_task_queryset(
                    owner_id=source.owner_id,
                    project_id=source.project_id,
                    batch_id=batch_id,
                )
            )
            refreshed_tasks = sorted(
                refreshed_tasks,
                key=lambda item: (
                    self._batch_index(item),
                    item.queued_at or item.created_at,
                    item.created_at,
                    str(item.id),
                ),
            )
        return Response({
            "batch_id": batch_id,
            "batch_kind": str(source_snapshot.get("batch_kind") or ""),
            "scanned_task_count": len(batch_tasks),
            "cancelled_task_ids": cancelled_task_ids,
            "cancel_requested_task_ids": cancel_requested_task_ids,
            "skipped": skipped,
            "downstream_cancellations": downstream_summary,
            "tasks": GenerationTaskSerializer(
                refreshed_tasks,
                many=True,
                context=self.get_serializer_context(),
            ).data,
        })

    @extend_schema(
        request=None,
        responses={
            200: ProductionBatchDeleteResponseSerializer,
            400: APIValidationErrorEnvelopeSerializer,
        },
        description=(
            "Delete the terminal history records for the exact production batch "
            "identified by the selected task's batch_id. The operation is atomic: "
            "if any task or deferred continuity request in that batch is still "
            "active, or any task is protected by release/assembly audit, nothing "
            "is deleted."
        ),
    )
    @action(detail=True, methods=["delete"], url_path="delete-batch")
    def delete_batch(self, request, pk=None):
        source = self.get_object()
        terminal_statuses = {
            GenerationTask.Status.SUCCEEDED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        }
        with transaction.atomic():
            try:
                SeriesProject.objects.select_for_update().get(pk=source.project_id)
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            source_snapshot, batch_tasks = self._batch_tasks_locked(source)
            batch_id = str(source_snapshot.get("batch_id") or "")
            active_tasks = [
                {
                    "task_id": str(task.id),
                    "batch_index": self._batch_index(task) or None,
                    "status": task.status,
                }
                for task in batch_tasks
                if task.status not in terminal_statuses
            ]
            if active_tasks:
                raise ValidationError({
                    "batch_id": batch_id,
                    "active_tasks": active_tasks,
                    "detail": (
                        "该整集批次仍包含排队、运行或取消中的任务；"
                        "请先取消当前批次或等待任务进入终态后再删除。"
                    ),
                })

            batch_task_ids = {task.id for task in batch_tasks}
            deferred_requests = list(
                DeferredGenerationRequest.objects.select_for_update()
                .filter(owner_id=source.owner_id, project_id=source.project_id)
                .filter(Q(source_task_id__in=batch_task_ids) | Q(materialized_task_id__in=batch_task_ids))
                .order_by("created_at", "id")
            )
            active_deferred = [
                {
                    "deferred_generation_request_id": str(item.id),
                    "status": item.status,
                }
                for item in deferred_requests
                if item.status in DeferredGenerationRequest.ACTIVE_STATUSES
            ]
            if active_deferred:
                raise ValidationError({
                    "batch_id": batch_id,
                    "active_deferred_generation_requests": active_deferred,
                    "detail": (
                        "该整集批次仍包含等待或处理中首尾帧连续请求；"
                        "请先取消当前批次或等待请求进入终态后再删除。"
                    ),
                })
            external_materialized = [
                str(item.materialized_task_id)
                for item in deferred_requests
                if item.materialized_task_id and item.materialized_task_id not in batch_task_ids
            ]
            if external_materialized:
                raise ValidationError({
                    "batch_id": batch_id,
                    "materialized_task_ids": external_materialized,
                    "detail": "该批次的延迟连续请求已物化到批次外任务，不能作为单一整集批次删除。",
                })

            for task in batch_tasks:
                if task_is_release_evidence(task):
                    raise ValidationError("该批次包含已进入封存或历史发行清单的任务，不能删除。")
                self._assert_not_successful_assembly_source(task)
                self._assert_successful_video_source_is_replaceable(task)

            deleted_deferred_ids = [str(item.id) for item in deferred_requests]
            for item in deferred_requests:
                item.delete()
            deleted_task_ids = [str(task.id) for task in batch_tasks]
            for task in batch_tasks:
                task.delete()

        return Response({
            "batch_id": batch_id,
            "batch_kind": str(source_snapshot.get("batch_kind") or ""),
            "scanned_task_count": len(batch_tasks),
            "deleted_task_ids": deleted_task_ids,
            "deleted_deferred_generation_request_ids": deleted_deferred_ids,
        })

    @extend_schema(
        request=GenerationTaskRetrySerializer,
        responses={
            201: GenerationTaskSerializer,
            409: ProductionQueueConfirmationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        request_serializer = GenerationTaskRetrySerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        retry_options = request_serializer.validated_data
        source = self.get_object()
        with transaction.atomic():
            try:
                SeriesProject.objects.select_for_update().get(pk=source.project_id)
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            task = self.get_queryset().select_for_update().get(pk=source.pk)
            self._prepare_task_for_retry_locked(task, request.user)
            assert_queue_capacity(
                request.user,
                extended_generation_confirmed=retry_options[
                    "extended_generation_confirmed"
                ],
            )
            retry_task = clone_generation_task_for_retry(
                task,
                user=request.user,
                extended_generation_confirmed=retry_options[
                    "extended_generation_confirmed"
                ],
            )
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
