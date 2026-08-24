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
from django.db import transaction
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import (
    Asset,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    DeferredGenerationRequest,
    Episode,
    GeneratedArtifact,
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
from studio.permissions import IsOwnerOrStaff, IsOwnerOrStaffOrGlobalReadOnly, IsStaffOrReadOnly
from studio.media_signing import classify_task_output, load_signed_media_token, signed_media_token
from studio.serializers import (
    AssetSerializer,
    AssemblyJobSerializer,
    CharacterBibleSerializer,
    CanonicalGlossaryEnsureSerializer,
    CanonicalGlossaryEnsureResponseSerializer,
    ComfyUIWorkflowExportSerializer,
    ConsistencyLockSerializer,
    EpisodeSerializer,
    GeneratedWorkflowSerializer,
    GenerationTaskInputPromptSerializer,
    GenerationTaskSerializer,
    PromptFieldTranslationSerializer,
    PromptTranslationConflictErrorResponseSerializer,
    PromptTranslationBatchRequestSerializer,
    PromptTranslationManualBatchRequestSerializer,
    PromptTranslationConfirmSerializer,
    PromptTranslationPromoteShotResponseSerializer,
    PromptTranslationPromoteShotSerializer,
    PromptTranslationRetranslateSerializer,
    NextEpisodeSerializer,
    ProjectDeleteErrorResponseSerializer,
    PromptArtifactSerializer,
    PropBibleSerializer,
    RegisterSerializer,
    ReleaseBundleSerializer,
    SceneBibleSerializer,
    ScriptBibleCandidatesResponseSerializer,
    ScriptBibleErrorEnvelopeSerializer,
    SeriesProjectSerializer,
    ShotSerializer,
    StyleSerializer,
    UserSerializer,
    WorkflowTemplateSerializer,
)
from studio.services.comfyui import ComfyUIClient, ComfyUIError
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.configuration import resolve_configuration
from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
)
from studio.services.h3_prompts import (
    PROMPT_COMPILER_VERSION,
    PROMPT_INPUT_FINGERPRINT_VERSION,
    capture_consistency_snapshot,
    sync_consistency_locks,
)
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.production_pipeline import (
    ProductionIdempotencyConflict,
    compile_prompt_input,
    materialize_compiled_prompt,
)
from studio.services.production_batch_slots import (
    build_production_batch_slots,
)
from studio.services.shot_prompt_recovery import repair_legacy_negative_prompt
from studio.services.shot_versions import (
    RESERVED_SHOT_VERSION_METADATA_KEYS,
    ShotVersionResolutionError,
    legacy_shot_version_key,
    normalize_requested_shot_version_key,
    shot_version_key as canonical_shot_version_key,
    shot_version_label,
    shot_version_number,
)
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    PromptTranslationError,
    TranslationResult,
    TranslationValidationError,
    TRANSLATION_CONTEXT_VERSION,
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
from studio.services.script_breakdown import (
    ConfirmedScriptBibleAliases,
)
from studio.services.script_bible import (
    SCRIPT_BIBLE_SCHEMA_VERSION,
    ScriptBibleParseError,
    ScriptBibleProvenanceProtectedError,
    classify_script_bible_candidates,
    parse_script_bible,
    validate_untrusted_script_bible_metadata,
)
from studio.services.resolution import H3_RESOLUTION_MULTIPLE_DEFAULT
from studio.services.reference_bindings import (
    ReferenceBindingError,
    normalize_shot_reference_bindings,
)
from studio.services.provenance import (
    assembly_manifest_path,
    persist_assembly_provenance,
    persist_task_provenance,
    verified_assembly_manifest,
    verified_task_manifest,
)
from studio.services.distribution import (
    distribution_artifact_path,
    verified_distribution_manifest,
)
from studio.services.release_bundles import (
    ReleaseBundleError,
    asset_is_release_evidence,
    live_verified_release_bundle,
)


logger = logging.getLogger(__name__)



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

    try:
        from studio.services.comfyui import default_comfyui_runtime_config
        raw_url = str(default_comfyui_runtime_config().get("base_url") or "").strip()
    except ComfyUIError:
        return ""
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




class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # Liveness intentionally has no database dependency. In particular, do
    # not run the database-backed global throttles before this view.
    throttle_classes = []

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
        verified_content_type = ""
        try:
            payload = load_signed_media_token(
                token,
                max_age=int(resolve_configuration(
                    required_keys=("artifact.signed_url_ttl_seconds",),
                ).values["artifact.signed_url_ttl_seconds"]),
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
            elif purpose == "generated_artifact":
                artifact = GeneratedArtifact.objects.select_related(
                    "production_run",
                ).get(
                    pk=object_id,
                    production_run__owner_id=owner_id,
                    status__in=(
                        GeneratedArtifact.Status.AVAILABLE,
                        GeneratedArtifact.Status.SELECTED,
                    ),
                    storage_backend__in=("managed_local", "local_media"),
                )
                if artifact.kind not in {
                    GeneratedArtifact.Kind.IMAGE,
                    GeneratedArtifact.Kind.VIDEO,
                    GeneratedArtifact.Kind.ASSEMBLY,
                }:
                    raise ValueError("artifact is not browser media")
                if not artifact.object_key or not artifact.sha256 or artifact.size_bytes is None:
                    raise ValueError("artifact integrity facts are incomplete")
                path = (media_root / artifact.object_key).resolve()
                if artifact.storage_backend == "managed_local":
                    allowed_root = (
                        media_root / "generated-artifacts" / str(owner_id)
                        / str(artifact.production_run.project_id)
                    ).resolve()
                else:
                    allowed_root = (
                        media_root / "assemblies" / str(owner_id)
                        / str(artifact.production_run.project_id)
                    ).resolve()
                expected_kind = (
                    "image" if artifact.kind == GeneratedArtifact.Kind.IMAGE else "video"
                )
                facts = verified_local_media_facts(
                    path,
                    expected_kind=expected_kind,
                    max_bytes=int(artifact.size_bytes),
                )
                if (
                    facts["size_bytes"] != artifact.size_bytes
                    or facts["sha256"] != artifact.sha256
                    or facts["mime_type"] != artifact.mime_type
                ):
                    raise ValueError("artifact bytes do not match the formal manifest")
                verified_content_type = facts["mime_type"]
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
            content_type=verified_content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            as_attachment=(
                (
                    purpose in {"assembly", "task_distribution", "generated_artifact"}
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
        if purpose in {"assembly", "task_output", "task_distribution", "generated_artifact"}:
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
        return super().get_throttles()

    @extend_schema(responses=dict)
    def get(self, request):
        enabled = bool(resolve_configuration(
            required_keys=("access.public_registration_enabled",),
        ).values["access.public_registration_enabled"])
        return Response({
            "public_registration_enabled": enabled,
        })

    @extend_schema(request=RegisterSerializer, responses={201: UserSerializer})
    def post(self, request):
        enabled = bool(resolve_configuration(
            required_keys=("access.public_registration_enabled",),
        ).values["access.public_registration_enabled"])
        if not enabled:
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
        if getattr(self, "action", None) in {"create", "update", "partial_update"}:
            self.throttle_scope = "asset_upload"
        return super().get_throttles()

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
    attempted_deployment_id,
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
            entry.provider = str(
                (getattr(result, "metadata", {}) or {}).get("provider")
                or attempted_deployment_id
            )[:80]
            entry.model = ""
            entry.error_message = str(result)
            entry.runtime_metadata = {
                **(getattr(result, "metadata", {}) or {}),
                "error_code": getattr(result, "code", "translation_failed"),
                "model_deployment_id": str(attempted_deployment_id),
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
                "last_attempt_model_deployment_id": str(attempted_deployment_id),
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
        deployment_id = data["model_deployment_id"]
        items = _validated_translation_items(project, data["entries"])
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
                model_deployment_id=deployment_id,
            )
        except PromptTranslationError as exc:
            results = [exc for _ in items]
        saved = [
            _save_translation_candidate(
                project=project, request_user=request.user, item=item, result=result,
                attempted_deployment_id=deployment_id,
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
            "runtime": next((
                dict(getattr(result, "metadata", {}) or {})
                for result in results
                if getattr(result, "metadata", None)
            ), {"model_deployment_id": str(deployment_id)}),
        })

    @extend_schema(
        request=PromptTranslationManualBatchRequestSerializer,
        responses={201: dict},
        description=(
            "Create review-only blank English candidates without invoking a model. "
            "The source remains authoritative and production still requires explicit confirmation."
        ),
    )
    @action(detail=True, methods=["post"], url_path="translations/manual")
    def create_manual_prompt_fields(self, request, pk=None):
        project = self.get_object()
        serializer = PromptTranslationManualBatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = _validated_translation_items(project, serializer.validated_data["entries"])
        saved = [
            _save_translation_candidate(
                project=project,
                request_user=request.user,
                item=item,
                result=TranslationResult(
                    text="",
                    provider="manual_review",
                    model="",
                    metadata={
                        "submission_semantics": "no_submission",
                        "manual_candidate": True,
                    },
                ),
                attempted_deployment_id="manual_review",
            )
            for item in items
        ]
        return Response({
            "entries": PromptFieldTranslationSerializer(saved, many=True).data,
            "summary": {
                "requested": len(items),
                "created": len(saved),
                "external_calls": 0,
            },
        }, status=status.HTTP_201_CREATED)

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
        "runtime_metadata": entry.runtime_metadata or {},
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
        deployment_id = data["model_deployment_id"]
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
                model_deployment_id=deployment_id,
            )
        except PromptTranslationError as exc:
            result = exc
        saved = _save_translation_candidate(
            project=entry.project, request_user=request.user, item=item, result=result,
            attempted_deployment_id=deployment_id,
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




class PromptArtifactViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_value_regex = r"[0-9a-fA-F-]{36}"
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


class WorkflowTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkflowTemplateSerializer
    permission_classes = [IsStaffOrReadOnly]
    filterset_fields = ("provider", "output_kind", "model", "is_active", "is_system")
    search_fields = ("name", "slug", "description")
    ordering_fields = ("name", "version", "created_at")

    def get_queryset(self):
        return _owned_or_global(WorkflowTemplate.objects.select_related("owner"), self.request.user)


class GeneratedWorkflowViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_value_regex = r"[0-9a-fA-F-]{36}"
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


    @staticmethod
    def _owned_prompt(user):
        queryset = PromptArtifact.objects.all()
        return queryset if user.is_staff else queryset.filter(project__owner=user)


class GenerationTaskViewSet(viewsets.ReadOnlyModelViewSet):
    # ProductionRun is the sole public mutation path. Tasks are diagnostics only.
    http_method_names = ["get", "head", "options"]
    serializer_class = GenerationTaskSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "kind", "status", "generated_workflow")
    search_fields = ("comfy_prompt_id", "error_message")
    ordering_fields = ("priority", "queued_at", "started_at", "finished_at", "updated_at")

    @classmethod
    def get_extra_actions(cls):
        allowed = {"input_prompt", "comfyui_workflow"}
        return [action_method for action_method in super().get_extra_actions() if action_method.__name__ in allowed]

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


class AssemblyJobViewSet(viewsets.ReadOnlyModelViewSet):
    http_method_names = ["get", "head", "options"]
    serializer_class = AssemblyJobSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "episode", "scope", "status", "dry_run")
    ordering_fields = ("created_at", "started_at", "finished_at")


    def get_queryset(self):
        queryset = AssemblyJob.objects.select_related("project", "episode", "owner").prefetch_related("source_tasks")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)





class ReleaseBundleViewSet(viewsets.ReadOnlyModelViewSet):
    http_method_names = ["get", "head", "options"]
    serializer_class = ReleaseBundleSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("project", "status")
    ordering_fields = ("version", "created_at", "sealed_at", "released_at")


    def get_queryset(self):
        queryset = ReleaseBundle.objects.select_related("project", "owner", "created_by")
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)







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
