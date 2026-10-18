from __future__ import annotations

from studio.project_translation_views import ProjectTranslationRequestsMixin
from studio.consistency_serializers import ConsistencySnapshotSerializer
from studio.shot_source_serializers import ShotSourceFieldsSerializer
from studio.shot_field_serializers import (ShotFieldRevisionDetailSerializer,
    ShotFieldRevisionWriteSerializer, ShotFieldRevisionWriteResultSerializer)

import json
import hashlib
import logging
import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
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
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters import rest_framework as filters

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    GeneratedArtifact,
    GenerationTask,
    PromptFieldTranslation,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    StyleCategory,
    WorkflowTemplate,
)
from studio.services.execution_asset_references import asset_has_active_execution_reference
from studio.services.registry_reference_lock import registry_reference_writer
from studio.permissions import IsOwnerOrStaff, IsOwnerOrStaffOrGlobalReadOnly, IsStaffOrReadOnly
from studio.media_signing import classify_task_output, load_signed_media_token, signed_media_token
from studio.throttling import DatabaseScopedRateThrottle
from studio.serializers import (
    APIValidationErrorEnvelopeSerializer,
    AssetSerializer,
    WorkflowSmokeAssetCandidateSerializer,
    AssemblyJobSerializer,
    CharacterBibleSerializer,
    CanonicalGlossaryConfirmSerializer,
    CanonicalGlossaryConfirmResponseSerializer,
    ComfyUIWorkflowExportSerializer,
    ConsistencyLockSerializer,
    EpisodeSerializer,
    GenerationTaskInputPromptSerializer,
    GenerationTaskObservationSerializer,
    GenerationTaskListSerializer,
    GenerationTaskSerializer,
    PromptFieldTranslationSerializer,
    PromptTranslationListQuerySerializer,
    PromptTranslationConflictErrorResponseSerializer,
    PromptTranslationManualBatchRequestSerializer,
    PromptTranslationConfirmSerializer,
    NextEpisodeSerializer,
    ProjectDeleteErrorResponseSerializer,
    PropBibleSerializer,
    RegisterSerializer,
    ReleaseBundleSerializer,
    SceneBibleSerializer,
    SeriesProjectSerializer,
    ShotSerializer,
    StyleSerializer,
    StyleCategorySerializer,
    StyleReferenceSerializer,
    StyleCatalogSerializer,
    UserSerializer,
    FormalBibleAssetImmutableConflict,
    _is_formal_bible_reference_asset,
    WorkflowTemplateSerializer,
)
from studio.services.comfyui import ComfyUIClient, ComfyUIError
from studio.services.generated_artifacts import (
    generated_artifact_storage_owner_id,
    verified_local_media_facts,
)
from studio.services.smoke_assets import (
    ManagedSmokeAssetBudget,
    managed_smoke_asset_eligibility,
)
from studio.services.asset_visibility import exclude_workflow_smoke_fixture_assets
from studio.pagination import WorkflowSmokeCandidatePagination
from studio.services.configuration import (
    ConfigurationResolutionError,
    project_creation_policy,
    resolve_configuration,
)
from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
)
from studio.services.consistency import (
    capture_consistency_snapshot,
    sync_consistency_locks,
)
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
    append_translation_history,
    CanonicalTargetCollisionError,
    TranslationValidationError,
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    ensure_no_canonical_target_collisions,
    missing_translation_context_mappings,
    resolve_current_source,
    source_text_hash,
    translation_entry_context_current,
    translation_context_hash,
    validate_manual_translation,
)
from studio.services.canonical_glossary import confirm_canonical_candidate, is_canonical_name
from studio.services.script_bible import (
    ScriptBibleProvenanceProtectedError,
    validate_untrusted_script_bible_metadata,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
)
from studio.services.provenance import (
    verified_assembly_manifest,
    verified_task_manifest,
    verified_file_sha256,
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
        def delete_unshared_file():
            # Multiple Asset identities may share the same managed object key.
            if not Asset.objects.filter(file=name).exists():
                storage.delete(name)
        transaction.on_commit(delete_unshared_file, robust=True)


def _asset_structured_reference_labels(asset: Asset) -> list[str]:
    labels: list[str] = []
    reference_count = asset.reference_versions.count()
    if reference_count:
        labels.append(f"{reference_count}条主图/变体版本关联（在素材库关系视图解除）")
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
    if asset.shot_reference_uses.exists():
        labels.append("分镜参考")
    if asset.script_sources.exists():
        labels.append("项目剧本来源")
    if AssetGenerationJob.objects.filter(output_asset=asset).exists():
        labels.append("素材生成审计")
    return labels


def _asset_reference_conflict_message(asset: Asset) -> str:
    if asset_is_release_evidence(asset):
        return "该素材已进入封存或历史发行清单，不能修改类别或删除；可停用但必须保留审计证据。"
    labels = _asset_structured_reference_labels(asset)
    if labels:
        return f"该素材已用于{'、'.join(labels)}，请先解除引用后再修改类别或删除。"
    try:
        if asset_has_active_execution_reference(asset):
            return "该素材正被未结束的执行准备、生成任务或烟测使用，请结束相关执行后再修改类别或删除。"
    except DjangoValidationError as exc:
        return "；".join(exc.messages)
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
        or asset.shot_reference_uses.exists()
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
            str(selection.get("conditioning_mode") or ""),
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
    from studio.services.asset_provenance import asset_snapshot_sha256

    current_hash = asset_snapshot_sha256([asset])
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


def _iter_file_range(handle, start: int, length: int, chunk_size: int = 64 * 1024):
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                raise OSError("Media was truncated while streaming its declared byte range")
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _range_file_response(path: Path, request, *, content_type: str, as_attachment: bool):
    """Serve one RFC 7233 byte range so browser video seeking stays efficient."""

    handle = path.open("rb")
    try:
        response = _opened_file_response(
            handle, request, filename=path.name,
            content_type=content_type, as_attachment=as_attachment,
        )
        if response.streaming:
            # Also close an unconsumed Range response: generator.close() alone
            # does not enter the finally block of a generator never started.
            response._resource_closers.append(handle.close)
        else:
            handle.close()
        return response
    except BaseException:
        handle.close()
        raise


def _opened_file_response(handle, request, *, filename: str, content_type: str, as_attachment: bool):
    # The response length and streamed bytes belong to the same open file, not
    # a pathname reopened later when the server starts consuming the response.
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(0)

    range_header = str(request.headers.get("Range", "")).strip()
    if not range_header:
        response = FileResponse(
            handle, content_type=content_type,
            as_attachment=as_attachment, filename=filename,
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
        _iter_file_range(handle, start, length), status=206, content_type=content_type,
    )
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(length)
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Disposition"] = content_disposition_header(as_attachment, filename)
    return response


class SignedMediaView(APIView):
    """Short-lived signed access for browser img/video elements without Bearer headers."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Browser media elements cannot attach the authenticated API Bearer token.
    # A valid short-lived, object-bound signature remains mandatory, while an
    # independent database-configured scope prevents a thumbnail grid from
    # consuming the ordinary anonymous API allowance.
    throttle_classes = [DatabaseScopedRateThrottle]
    throttle_scope = "signed_media"

    @extend_schema(exclude=True)
    def get(self, request, token: str):
        provenance_url = ""
        verified_manifest = None
        verified_content_type = ""
        generated_asset = False
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
                generated_asset = AssetGenerationJob.objects.filter(
                    output_asset=asset,
                ).exists()
                if generated_asset:
                    if not re.fullmatch(r"[0-9a-f]{64}", str(asset.checksum or "")):
                        raise ValueError("generated asset checksum is unavailable")
                    if verified_file_sha256(path) != asset.checksum:
                        raise ValueError("generated asset bytes do not match their lineage")
                    verified_content_type = str(asset.mime_type or "")
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
                # GET verifies the original writer's result. It cannot repair
                # missing provenance or turn legacy output rows into success.
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
                    "production_run__project", "generation_task",
                ).get(
                    pk=object_id,
                    status__in=(
                        GeneratedArtifact.Status.AVAILABLE,
                        GeneratedArtifact.Status.SELECTED,
                    ),
                    storage_backend__in=("managed_local", "local_media"),
                )
                storage_owner_id = generated_artifact_storage_owner_id(artifact)
                if str(owner_id) != str(storage_owner_id):
                    raise ValueError("signed artifact tenant owner mismatch")
                if artifact.kind not in {
                    GeneratedArtifact.Kind.IMAGE,
                    GeneratedArtifact.Kind.VIDEO,
                    GeneratedArtifact.Kind.AUDIO,
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
                    "video" if artifact.kind == GeneratedArtifact.Kind.ASSEMBLY else artifact.kind
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
                    purpose in {"asset", "assembly", "task_distribution", "generated_artifact"}
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
        if generated_asset or purpose in {"assembly", "task_output", "task_distribution", "generated_artifact"}:
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


@extend_schema_view(
    list=extend_schema(parameters=[OpenApiParameter("scope", str, enum=["personal", "global"])]),
    destroy=extend_schema(responses={204: None, 400: APIValidationErrorEnvelopeSerializer, 409: APIValidationErrorEnvelopeSerializer}),
)
class StyleViewSet(viewsets.ModelViewSet):
    serializer_class = StyleSerializer
    permission_classes = [IsOwnerOrStaffOrGlobalReadOnly]
    filterset_fields = ("category", "is_active", "is_system")
    search_fields = ("name", "slug", "description", "visual_prompt")
    ordering_fields = ("name", "created_at", "updated_at")

    @extend_schema(responses=StyleCatalogSerializer)
    @action(detail=False, methods=["get"], filter_backends=[])
    def catalog(self, request):
        return Response({
            "can_manage_categories": bool(request.user.is_staff),
            "scopes": [{"value": key, "display_name": label} for key, label in StyleSerializer().fields["scope"].choices.items()
                if key == "personal" or request.user.is_staff],
        })

    def get_queryset(self):
        # Independent subqueries avoid multiplying project, asset and job rows.
        from studio.models import AssetGenerationJob
        counts = {}
        for key, model in (("asset_count", Asset), ("project_count", SeriesProject), ("generation_job_count", AssetGenerationJob)):
            count = model.objects.filter(style_id=OuterRef("pk")).order_by().values("style_id").annotate(n=Count("pk")).values("n")
            counts[key] = Coalesce(Subquery(count, output_field=IntegerField()), 0)
        queryset = Style.objects.select_related("category").annotate(**counts).order_by("name", "pk")
        scope = self.request.query_params.get("scope")
        if scope == "global":
            queryset = queryset.filter(owner__isnull=True)
        elif scope == "personal":
            queryset = queryset.filter(owner=self.request.user)
        return _owned_or_global(queryset, self.request.user)

    @transaction.atomic
    def perform_create(self, serializer):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        scope = serializer.validated_data.pop("scope", "personal")
        self._lock_category(serializer)
        instance = serializer.save(
            owner=None if scope == "global" else self.request.user,
            is_system=False, slug=uuid.uuid4().hex,
        )
        record_platform_lifecycle_event(instance, actor=self.request.user, action="create", reason_code="style.create", after_summary=lifecycle_summary(instance))
        serializer.instance = self.get_queryset().get(pk=instance.pk)

    @transaction.atomic
    def perform_update(self, serializer):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        serializer.instance = self.get_queryset().select_for_update(of=("self",)).get(pk=serializer.instance.pk)
        self._lock_category(serializer)
        before = lifecycle_summary(serializer.instance)
        instance = serializer.save()
        record_platform_lifecycle_event(instance, actor=self.request.user, action="update", reason_code="style.update", before_summary=before, after_summary=lifecycle_summary(instance))

    @transaction.atomic
    def perform_destroy(self, instance):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        instance = self.get_queryset().select_for_update(of=("self",)).get(pk=instance.pk)
        if instance.asset_count or instance.project_count or instance.generation_job_count:
            raise ValidationError({"detail": "此风格仍被项目、素材或生成任务引用，不能删除。请使用停用；已有引用会保留。"}, code="style_in_use")
        record_platform_lifecycle_event(instance, actor=self.request.user, action="delete", reason_code="style.delete", before_summary=lifecycle_summary(instance))
        instance.delete()

    def _lock_category(self, serializer):
        category = serializer.validated_data.get("category")
        if category:
            category = StyleCategory.objects.select_for_update().filter(pk=category.pk).first()
            if category is None:
                raise ValidationError({"category": "此题材分类已被删除，请重新选择。"})
            if not category.is_active and (not serializer.instance or serializer.instance.category_id != category.key):
                raise ValidationError({"category": "此题材分类已停用，不能用于新关联。"})
            serializer.validated_data["category"] = category

    @extend_schema(parameters=[OpenApiParameter("kind", str, enum=["projects", "assets", "jobs"], required=True)], responses=StyleReferenceSerializer(many=True))
    @action(detail=True, methods=["get"], filter_backends=[])
    def references(self, request, pk=None):
        """Paginated current references, restricted to the viewer's visibility."""
        instance = self.get_object()
        kind = request.query_params.get("kind")
        if kind not in {"projects", "assets", "jobs"}:
            raise ValidationError({"kind": "请选择 projects、assets 或 jobs。"})
        relation, name_field = {
            "projects": (instance.projects, "title"),
            "assets": (instance.assets, "name"),
            "jobs": (instance.asset_generation_jobs, "id"),
        }[kind]
        rows = _owned_or_global(relation.all(), request.user).order_by("pk").values("id", name_field)
        page = self.paginate_queryset(rows)
        data = [{"id": row["id"], "name": str(row[name_field]), "kind": kind} for row in page]
        return self.get_paginated_response(StyleReferenceSerializer(data, many=True).data)

@extend_schema_view(destroy=extend_schema(responses={204: None, 400: APIValidationErrorEnvelopeSerializer}))
class StyleCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = StyleCategorySerializer
    permission_classes = [IsStaffOrReadOnly]
    queryset = StyleCategory.objects.annotate(style_count=Count("styles")).order_by("sort_order", "name", "pk")
    filterset_fields = ("is_active",)
    search_fields = ("name", "description")
    ordering_fields = ("sort_order", "name", "created_at")

    @transaction.atomic
    def perform_create(self, serializer):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        instance = serializer.save(key=uuid.uuid4().hex)
        record_platform_lifecycle_event(instance, actor=self.request.user, action="create", reason_code="style_category.create", after_summary=lifecycle_summary(instance))
        serializer.instance = self.get_queryset().get(pk=instance.pk)

    @transaction.atomic
    def perform_update(self, serializer):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        serializer.instance = StyleCategory.objects.select_for_update().get(pk=serializer.instance.pk)
        before = lifecycle_summary(serializer.instance)
        instance = serializer.save()
        record_platform_lifecycle_event(instance, actor=self.request.user, action="update", reason_code="style_category.update", before_summary=before, after_summary=lifecycle_summary(instance))
        serializer.instance = self.get_queryset().get(pk=instance.pk)

    @transaction.atomic
    def perform_destroy(self, instance):
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        instance = StyleCategory.objects.select_for_update().get(pk=instance.pk)
        if instance.styles.exists():
            raise ValidationError({"detail": "此题材分类仍被风格引用，不能删除。可停用，或先调整风格分类。"}, code="style_category_in_use")
        record_platform_lifecycle_event(instance, actor=self.request.user, action="delete", reason_code="style_category.delete", before_summary=lifecycle_summary(instance))
        instance.delete()


from studio.maintenance_serializers import AssetBatchDeleteInput, AssetBatchDeleteResult


class AssetViewSet(viewsets.ModelViewSet):
    serializer_class = AssetSerializer
    permission_classes = [IsOwnerOrStaffOrGlobalReadOnly]
    filterset_fields = ("style", "kind", "is_active")
    search_fields = ("name", "description", "prompt_hint", "source_info")
    ordering_fields = ("name", "kind", "created_at", "updated_at")

    def get_throttles(self):
        if getattr(self, "action", None) in {"create", "update", "partial_update"}:
            self.throttle_scope = "asset_upload"
        return super().get_throttles()

    @extend_schema(
        description=(
            "读取当前用户或全局的普通生产素材。"
            "metadata.fixture_scope=workflow_smoke 的技术烟测素材不会进入此列表；"
            "工作流版本验收页只通过 workflow-smoke-candidates action 读取它们。"
        ),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = _owned_or_global(
            Asset.objects.select_related("style", "owner"),
            self.request.user,
        )
        return exclude_workflow_smoke_fixture_assets(queryset)

    @extend_schema(
        responses={
            200: WorkflowSmokeAssetCandidateSerializer(many=True),
            403: APIValidationErrorEnvelopeSerializer,
        },
        description=(
            "平台管理员专用的 workflow smoke 素材候选。"
            "这里会保留普通生产素材列表刻意隐藏的 workflow_smoke 技术 fixture；"
            "服务端在有界分页内实时复核受管路径、媒体类型和 SHA-256；不返回本地路径。"
        ),
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="workflow-smoke-candidates",
        permission_classes=[IsAdminUser],
        pagination_class=WorkflowSmokeCandidatePagination,
    )
    def workflow_smoke_candidates(self, request):
        # Deliberately bypass the normal Asset queryset: technical fixtures are
        # hidden from production/library selectors but remain valid smoke inputs.
        queryset = Asset.objects.filter(owner__isnull=True).order_by("name", "pk")
        page = self.paginate_queryset(queryset)
        if page is None:  # pragma: no cover - action always has a paginator
            page = []
        budget = ManagedSmokeAssetBudget()
        rows = []
        for asset in page:
            eligible, reason = managed_smoke_asset_eligibility(asset, budget=budget)
            checksum = str(asset.checksum or "").strip().casefold()
            metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
            try:
                duration_ms = int(metadata.get("duration_ms"))
            except (TypeError, ValueError):
                duration_ms = None
            try:
                source_fps = float(
                    metadata.get("source_fps", metadata.get("fps")),
                )
            except (TypeError, ValueError):
                source_fps = None
            rows.append({
                "id": asset.id,
                "name": asset.name,
                "kind": str(asset.kind or ""),
                "mime_type": str(asset.mime_type or ""),
                "checksum": checksum if re.fullmatch(r"[0-9a-f]{64}", checksum) else "",
                "duration_ms": duration_ms,
                "source_fps": source_fps,
                "has_audio": metadata.get("has_audio") is True,
                "smoke_eligible": eligible,
                "smoke_ineligible_reason": reason,
            })
        return self.get_paginated_response(
            WorkflowSmokeAssetCandidateSerializer(rows, many=True).data,
        )

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

    @registry_reference_writer
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

    def _delete_assets(self, request, asset_ids):
        """Single and batch deletion share permission/reference/file semantics.

        Validate the entire selection before deleting anything. A failed batch
        never leaves an apparently failed request with partially deleted assets.
        """
        with transaction.atomic():
            cover_project_ids = list(
                SeriesProject.objects.filter(cover_asset_id__in=asset_ids)
                .order_by("pk").values_list("pk", flat=True)
            )
            if cover_project_ids:
                list(
                    SeriesProject.objects.select_for_update()
                    .filter(pk__in=cover_project_ids).order_by("pk")
                )
            assets = list(self.get_queryset().select_for_update().filter(pk__in=asset_ids).order_by("pk"))
            if len(assets) != len(asset_ids):
                raise NotFound("部分素材不存在或无权访问；未删除任何素材。")
            for asset in assets:
                self.check_object_permissions(request, asset)
                if _is_formal_bible_reference_asset(asset):
                    raise FormalBibleAssetImmutableConflict()
                if asset.owner_id is None and not request.user.is_superuser:
                    raise ValidationError("只有超级管理员可删除全局素材。")
                if not _is_dedicated_project_cover(asset):
                    conflict = _asset_reference_conflict_message(asset)
                    if conflict:
                        raise ValidationError(f"{asset.name}：{conflict}")
            for asset in assets:
                _delete_file_after_commit(asset)
                asset.delete()

    @registry_reference_writer
    def destroy(self, request, *args, **kwargs):
        self._delete_assets(request, [kwargs["pk"]])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=AssetBatchDeleteInput, responses={200: AssetBatchDeleteResult})
    @action(detail=False, methods=["post"], url_path="batch-delete")
    @registry_reference_writer
    def batch_delete(self, request):
        serializer = AssetBatchDeleteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["asset_ids"]
        self._delete_assets(request, ids)
        return Response({"deleted_ids": [str(pk) for pk in ids], "deleted_count": len(ids)})


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


def _create_manual_translation_candidate(*, project: SeriesProject, request_user, item: dict) -> PromptFieldTranslation:
    """Create an explicitly blank review candidate; never mimic a model result."""
    scope_id = _translation_scope_id(project, item)
    source = item["source_text"]
    with transaction.atomic():
        project = _lock_translation_project(project, request_user)
        entry = PromptFieldTranslation.objects.select_for_update().filter(
            project=project, scope_type=item["scope_type"], scope_id=scope_id, field_key=item["field_key"],
        ).first()
        if entry is None:
            entry = PromptFieldTranslation(project=project, scope_type=item["scope_type"],
                scope_id=scope_id, field_key=item["field_key"], revision=1)
        else:
            append_translation_history(entry)
            entry.revision += 1
        identity_metadata = {}
        target = _dialogue_delivery_index(item["field_key"])
        if item["scope_type"] == PromptFieldTranslation.ScopeType.SHOT and target is not None:
            shot = Shot.objects.select_for_update().get(pk=scope_id, episode__project=project)
            identity_metadata["dialogue_identity"] = _dialogue_identity(shot, target)
        entry.source_text, entry.source_hash = source, source_text_hash(source)
        entry.translated_text = ""
        entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
        entry.provider, entry.model, entry.error_message = "manual_review", "", ""
        entry.runtime_metadata = {**identity_metadata, "submission_semantics": "no_submission", "manual_candidate": True,
            "translation_context_hash": translation_context_hash(_translation_context(project, item)),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION}
        entry.origin_generation_attempt = None
        entry.confirmed_by, entry.confirmed_at = None, None
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
        self.default_code = str(code or self.default_code)
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


@extend_schema_view(list=extend_schema(parameters=[OpenApiParameter(
    name="owned_only", type=bool, location=OpenApiParameter.QUERY, required=False, default=False,
    description="为 true 时只返回当前账号拥有的项目，供执行输入来源选择；false 或省略沿原管理权限范围。只接受 true/false。",
)]))
class SeriesProjectViewSet(ProjectTranslationRequestsMixin, viewsets.ModelViewSet):
    serializer_class = SeriesProjectSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_fields = ("status", "style", "target_model")
    search_fields = ("title", "logline", "master_script")
    ordering_fields = ("title", "created_at", "updated_at")

    def get_queryset(self):
        owned_only = self.request.query_params.get("owned_only", "false") if self.action == "list" else "false"
        if owned_only not in {"true", "false"}:
            raise ValidationError({"owned_only": "必须为 true 或 false"})
        if self.action in {"translation_options", "translation_requests", "create_translation_request", "translation_sources", "translation_candidate", "adopt_translation_candidate"}:
            return _owned_projects(SeriesProject.objects.only("id", "owner_id"), self.request.user)
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
        ).order_by("-updated_at", "-id")
        if owned_only == "true":
            queryset = queryset.filter(owner=self.request.user)
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
    @registry_reference_writer
    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            try:
                project = _owned_projects(
                    SeriesProject.objects.all(), request.user,
                ).select_for_update().get(pk=kwargs["pk"])
            except SeriesProject.DoesNotExist as exc:
                raise NotFound("连续剧项目不存在") from exc
            self.check_object_permissions(request, project)
            translation_jobs = AssetGenerationJob.objects.filter(
                Q(translation_request__project=project)
                | Q(parent_preparation__asset_job__translation_request__project=project)
            ).select_for_update(of=("self",)).only("id", "status", "claim_token")
            active_translation_jobs = sum(job.status not in {"succeeded", "failed", "cancelled"}
                or job.claim_token is not None for job in translation_jobs)
            if active_translation_jobs:
                raise ProjectDeleteConflict({"detail": "项目翻译或其辅助调用尚未终结；请在原任务中取消或核对远端状态后再删除。",
                    "active_translation_jobs": active_translation_jobs})
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
            active_generation_statuses = {
                GenerationTask.Status.QUEUED,
                GenerationTask.Status.RUNNING,
                GenerationTask.Status.CANCEL_REQUESTED,
            }
            terminal_submission_states = {
                GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
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
            formal_runs = list(
                project.production_runs.select_for_update().only("id")
            )
            if formal_runs:
                raise ProjectDeleteConflict(
                    {
                        "detail": (
                            "项目已有正式 ProductionRun 及不可变审计历史，禁止级联删除；"
                            "请将项目归档保留，或在专门的数据保留流程中处理。"
                        ),
                    },
                    code="protected_resource",
                )
            project.delete()
            _delete_unused_project_cover(cover_asset_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        parameters=[PromptTranslationListQuerySerializer],
        responses=PromptFieldTranslationSerializer(many=True),
    )
    @action(detail=True, methods=["get"], url_path="translations")
    def translations(self, request, pk=None):
        project = self.get_object()
        queryset = project.prompt_field_translations.all()
        query = PromptTranslationListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        episode_id = query.validated_data.get("episode")
        shot_id = query.validated_data.get("shot")
        translation_status = query.validated_data.get("status")
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
            _create_manual_translation_candidate(project=project, request_user=request.user, item=item)
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

    @extend_schema(request=None, responses={200: ConsistencyLockSerializer(many=True),
        409: APIValidationErrorEnvelopeSerializer})
    @action(detail=True, methods=["post"], url_path="sync-locks", pagination_class=None, filter_backends=[])
    def sync_locks(self, request, pk=None):
        project = self.get_object()
        locks = sync_consistency_locks(project)
        return Response(ConsistencyLockSerializer(locks, many=True, context={"request": request}).data)

    @extend_schema(responses={200: ConsistencySnapshotSerializer, 409: APIValidationErrorEnvelopeSerializer})
    @action(detail=True, methods=["get"], url_path="continuity")
    def continuity(self, request, pk=None):
        return Response(capture_consistency_snapshot(self.get_object()))

    @action(detail=True, methods=["post"], url_path="next-episode")
    def next_episode(self, request, pk=None):
        project = self.get_object()
        serializer = NextEpisodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        next_number = (project.episodes.aggregate(value=Max("number"))["value"] or 0) + 1
        try:
            creation_policy = project_creation_policy(project=project)
        except ConfigurationResolutionError as exc:
            raise ValidationError({
                "configuration": {
                    "code": exc.code,
                    "detail": str(exc),
                    "missing_keys": list(exc.missing_keys),
                },
            }) from exc
        episode = Episode.objects.create(
            project=project,
            number=next_number,
            prompt_settings={
                "include_audio": creation_policy["default_include_audio"],
            },
            **serializer.validated_data,
        )
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
            if requested_project_id != current_project_id:
                from studio.models import BibleEntityIdentity, ScriptBibleEntityBinding
                kind = {CharacterBible: "character", SceneBible: "scene", PropBible: "prop"}[model]
                if (locked.image_binding_review_id or locked.variants.exists()
                        or BibleEntityIdentity.objects.filter(**{f"{kind}_id": locked.pk}).exists()
                        or ScriptBibleEntityBinding.objects.filter(**{f"{kind}_id": locked.pk}).exists()):
                    raise ValidationError({"project": "已有稳定声明key、设定、图片审核或剧本来源绑定的 Bible 不可跨项目移动；不会改写来源。"})
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
    search_fields = ("name", "role", "face_identity")

    def get_queryset(self):
        return self.owned(CharacterBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class SceneBibleViewSet(BibleProjectChildViewSet):
    serializer_class = SceneBibleSerializer
    filterset_fields = ("project", "is_locked", "time_of_day")
    search_fields = ("name", "location")

    def get_queryset(self):
        return self.owned(SceneBible.objects.select_related("project", "anchor_asset").prefetch_related("reference_assets"))


class PropBibleViewSet(BibleProjectChildViewSet):
    serializer_class = PropBibleSerializer
    filterset_fields = ("project", "is_locked")
    search_fields = ("name", "distinctive_marks")

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
    filterset_fields = ("episode", "episode__project", "media_type", "conditioning_mode", "include_audio")
    search_fields = ("title",)
    ordering_fields = ("number", "duration_ms", "created_at", "updated_at")

    def get_queryset(self):
        queryset = Shot.objects.select_related("episode__project", "scene").prefetch_related(
            "character_relations", "props"
        )
        if self.action in {"source_fields", "field_revisions"}:
            queryset = Shot.objects.select_related("episode__project__owner").only(
                "id", "episode_id", "episode__project_id", "episode__project__owner_id",
                "episode__project__owner__id",
            )
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(episode__project__owner=self.request.user)

    @extend_schema(
        responses={200: ShotSourceFieldsSerializer, 409: APIValidationErrorEnvelopeSerializer},
        description="Read exact original structured fields bound to this shot, on demand. No reparse, current registry lookup, field composition, editing or execution. Missing or damaged source is explicit, never an empty success.",
    )
    @action(detail=True, methods=["get"], url_path="source-fields")
    def source_fields(self, request, pk=None):
        from studio.exceptions import api_error_response
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.script_storyboard_fields import read_shot_source_fields

        shot = self.get_object()
        try:
            payload = read_shot_source_fields(shot_id=shot.pk, project_id=shot.episode.project_id,
                max_bytes=execution_configuration_max_bytes())
        except DjangoValidationError as exc:
            response = api_error_response(status_code=409,
                code=getattr(exc, "code", None) or "storyboard_source_invalid",
                details={"source_fields": exc.messages})
        else:
            response = Response(ShotSourceFieldsSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


    @extend_schema(methods=["GET"], parameters=[OpenApiParameter("revision_id", OpenApiTypes.UUID,
        description="Read this exact revision of this shot; omit for current manual revision (or original fields).")],
        responses={200: ShotFieldRevisionDetailSerializer, 400: APIValidationErrorEnvelopeSerializer,
            409: APIValidationErrorEnvelopeSerializer})
    @extend_schema(methods=["POST"], request=ShotFieldRevisionWriteSerializer,
        responses={200: ShotFieldRevisionWriteResultSerializer, 201: ShotFieldRevisionWriteResultSerializer,
            400: APIValidationErrorEnvelopeSerializer, 409: APIValidationErrorEnvelopeSerializer},
        description="Save one independent field as a new manual revision, or explicitly restore its original value. No script rematerialization, review/adoption, composition or model call. Exact duplicate requests replay their original revision, not the latest revision.")
    @action(detail=True, methods=["get", "post"], url_path="field-revisions", parser_classes=[JSONParser])
    def field_revisions(self, request, pk=None):
        from studio.exceptions import api_error_response
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.shot_field_revisions import read_shot_field_revision, revise_shot_field

        shot = self.get_object()
        common = {"shot_id": shot.pk, "project_id": shot.episode.project_id,
            "max_bytes": execution_configuration_max_bytes()}
        try:
            if request.method == "POST":
                serializer = ShotFieldRevisionWriteSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                detail, replayed = revise_shot_field(**common, user=request.user, payload=request.data)
                response = Response(ShotFieldRevisionWriteResultSerializer(
                    {"detail": detail, "replayed": replayed}).data, status=200 if replayed else 201)
            else:
                selected = request.query_params.get("revision_id")
                if selected is not None:
                    selected = serializers.UUIDField().run_validation(selected)
                detail = read_shot_field_revision(**common, revision_id=selected)
                response = Response(ShotFieldRevisionDetailSerializer(detail).data)
        except DjangoValidationError as exc:
            conflict = request.method == "GET" or getattr(exc, "code", "") in {
                "shot_field_revision_conflict", "storyboard_source_conflict", "storyboard_source_missing"}
            response = api_error_response(status_code=409 if conflict else 400,
                code=getattr(exc, "code", None) or "shot_field_revision_invalid",
                details={"fields": exc.messages})
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class TranslationReviewConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "translation_review_conflict"

    def __init__(self, detail=None, code=None):
        super().__init__(detail, code)
        # Keep the allow-listed conflict payload typed. APIException otherwise
        # coerces booleans and revision counters to ErrorDetail strings.
        if detail is not None:
            self.detail = detail


def _dialogue_delivery_index(field_key: str) -> int | None:
    match = re.fullmatch(r"shot\.dialogue\.([0-9]+)\.delivery", field_key)
    return int(match.group(1)) if match else None


def _dialogue_identity(shot: Shot, index: int) -> dict:
    dialogue = shot.dialogue if isinstance(shot.dialogue, list) else []
    if index < 0 or index >= len(dialogue) or not isinstance(dialogue[index], dict):
        raise TranslationReviewConflict({
            "code": "dialogue_index_changed", "shot_id": str(shot.id),
            "dialogue_index": index,
        })
    entry = dialogue[index]
    speaker_id = str(entry.get("speaker_id") or "")
    text = str(entry.get("text") or "")
    if not speaker_id or not text:
        raise TranslationReviewConflict({
            "code": "dialogue_identity_invalid", "shot_id": str(shot.id),
            "dialogue_index": index,
        })
    return {
        "source_order": index,
        "speaker_id": speaker_id,
        "text_sha256": source_text_hash(text),
        "voiceover": bool(entry.get("voiceover")),
    }


def _confirm_translation(
    *, entry: PromptFieldTranslation, expected_revision: int, candidate: str, user,
) -> PromptFieldTranslation:
    """Confirm only the reviewed translation; never write or rebase authored source."""
    if is_canonical_name(entry.scope_type, entry.field_key):
        raise ValidationError("名称请使用明确的“确认并写入术语表”操作；普通译文确认不会写入术语")
    try:
        ensure_no_canonical_target_collisions(entry.project)
    except CanonicalTargetCollisionError as exc:
        raise TranslationReviewConflict({"code": exc.code, "collisions": exc.collisions}) from exc

    try:
        current_source = resolve_current_source(
            project=entry.project,
            scope_type=entry.scope_type,
            scope_id=entry.scope_id,
            field_key=entry.field_key,
        )
    except TranslationValidationError as exc:
        raise TranslationReviewConflict({
            "code": "source_unavailable", "entry_id": str(entry.id),
            "message": str(exc),
        }) from exc

    if entry.revision != expected_revision:
        raise TranslationReviewConflict({
            "code": "revision_conflict", "entry_id": str(entry.id),
            "expected_revision": expected_revision, "current_revision": entry.revision,
        })

    dialogue_index = _dialogue_delivery_index(entry.field_key)
    if dialogue_index is not None and entry.scope_type == PromptFieldTranslation.ScopeType.SHOT:
        shot = Shot.objects.select_for_update().get(pk=entry.scope_id, episode__project=entry.project)
        current_identity = _dialogue_identity(shot, dialogue_index)
        stored_identity = (entry.runtime_metadata or {}).get("dialogue_identity")
        if stored_identity != current_identity:
            raise TranslationReviewConflict({
                "code": "dialogue_identity_changed" if stored_identity else "dialogue_identity_missing",
                "entry_id": str(entry.id), "field_key": entry.field_key,
            })

    if source_text_hash(current_source) != entry.source_hash:
        raise TranslationReviewConflict({
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
        missing_mappings = missing_translation_context_mappings(
            entry.project, current_context, candidate,
        )
        if missing_mappings:
            raise TranslationReviewConflict({
                "code": "canonical_target_missing",
                "detail": "修改后的英文译文缺少当前视觉与术语规则要求保留的 canonical 名称。",
                "missing_mappings": missing_mappings,
            })
    if not translation_entry_context_current(
        entry, project=entry.project, current_source=current_source,
        translated_text=candidate,
    ):
        raise TranslationReviewConflict({
            "code": "context_changed", "entry_id": str(entry.id),
            "field_key": entry.field_key,
        })


    append_translation_history(entry, event="translation_confirmed", actor=user)
    entry.revision += 1
    entry.translated_text = candidate
    entry.status = PromptFieldTranslation.Status.CONFIRMED
    entry.confirmed_by = user
    entry.confirmed_at = timezone.now()
    entry.error_message = ""
    entry.save()
    return entry


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
            entry = _confirm_translation(
                entry=entry,
                expected_revision=serializer.validated_data["expected_revision"],
                candidate=candidate,
                user=request.user,
            )
        return Response(PromptFieldTranslationSerializer(entry).data)

    @extend_schema(request=CanonicalGlossaryConfirmSerializer, responses=CanonicalGlossaryConfirmResponseSerializer,
        description="Explicitly confirm a reviewed Bible name and write it to the project glossary atomically. Requires the observed candidate revision and canonical value. No model invocation.")
    @action(detail=True, methods=["post"], url_path="confirm-canonical")
    def confirm_canonical(self, request, pk=None):
        serializer = CanonicalGlossaryConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visible_entry = self.get_object()
        with transaction.atomic():
            project = _lock_translation_project(visible_entry.project, request.user)
            entry = self.get_queryset().select_for_update().get(pk=visible_entry.pk, project=project)
            self.check_object_permissions(request, entry)
            entry.project = project
            try:
                entry = confirm_canonical_candidate(project=project, entry=entry, user=request.user,
                    candidate=serializer.validated_data["translation"],
                    expected_revision=serializer.validated_data["expected_revision"],
                    expected_project_updated_at=serializer.validated_data["expected_project_updated_at"],
                    expected_canonical=serializer.validated_data["expected_canonical"])
            except TranslationValidationError as exc:
                if exc.code == "translation_source_conflict":
                    raise TranslationReviewConflict({"code": exc.code, "detail": str(exc)}) from exc
                raise ValidationError({"code": exc.code, "detail": str(exc)}) from exc
            except CanonicalTargetCollisionError as exc:
                raise TranslationReviewConflict({"code": exc.code, "collisions": exc.collisions}) from exc
            except ConfigurationResolutionError as exc:
                raise ValidationError({"code": exc.code, "detail": str(exc)}) from exc
            except DjangoValidationError as exc:
                raise ValidationError(exc.messages) from exc
            return Response(CanonicalGlossaryConfirmResponseSerializer({
                "entry": entry, "project_id": project.pk, "source_text": entry.source_text,
                "canonical_english": entry.translated_text,
                "previous_canonical": entry.runtime_metadata["canonical_adoption"]["previous_value"],
                "updated_at": entry.runtime_metadata["canonical_adoption"]["project_updated_at"],
            }).data)

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
            append_translation_history(entry)
            entry.revision += 1
            entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
            entry.confirmed_by = None
            entry.confirmed_at = None
            entry.error_message = ""
            entry.save()
        return Response(PromptFieldTranslationSerializer(entry).data)


class WorkflowTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkflowTemplateSerializer
    permission_classes = [IsStaffOrReadOnly]
    filterset_fields = ("provider", "output_kind", "model", "is_active", "is_system")
    search_fields = ("name", "slug", "description")
    ordering_fields = ("name", "version", "created_at")

    def get_queryset(self):
        return _owned_or_global(WorkflowTemplate.objects.select_related("owner"), self.request.user)


class GenerationTaskFilter(filters.FilterSet):
    production_run_id = filters.UUIDFilter(field_name="platform_attempts__stage_run__production_run_id")
    stage_run_id = filters.UUIDFilter(field_name="platform_attempts__stage_run_id")
    preparation_id = filters.UUIDFilter(field_name="platform_attempts__preparation_id")
    execution_instance_version_id = filters.UUIDFilter(field_name="platform_attempts__execution_instance_version_id")
    unit_key = filters.CharFilter(field_name="platform_attempts__unit_key")

    class Meta:
        model = GenerationTask
        fields = ("project", "episode", "kind", "status", "production_run_id", "stage_run_id",
            "preparation_id", "execution_instance_version_id", "unit_key")


class GenerationTaskViewSet(viewsets.ReadOnlyModelViewSet):
    # ProductionRun is the sole public mutation path. Tasks are diagnostics only.
    http_method_names = ["get", "head", "options"]
    serializer_class = GenerationTaskSerializer
    permission_classes = [IsOwnerOrStaff]
    filterset_class = GenerationTaskFilter
    search_fields = ("comfy_prompt_id", "error_message")
    ordering_fields = ("priority", "queued_at", "started_at", "finished_at", "updated_at")

    @classmethod
    def get_extra_actions(cls):
        allowed = {"input_prompt", "comfyui_workflow", "observations"}
        return [action_method for action_method in super().get_extra_actions() if action_method.__name__ in allowed]

    def get_serializer_class(self):
        if self.action == "list":
            return GenerationTaskListSerializer
        return GenerationTaskSerializer

    def get_queryset(self):
        if self.action == "list":
            allowed = set(GenerationTaskFilter.base_filters) | {"page", "page_size", "search", "ordering", "format"}
            if unknown := set(self.request.query_params) - allowed:
                raise ValidationError({"filters": f"不支持的任务筛选字段：{', '.join(sorted(unknown))}"})
        queryset = GenerationTask.objects.all()
        if not getattr(self.request.user, "is_authenticated", False):
            return queryset.none()
        if not self.request.user.is_staff:
            queryset = queryset.filter(owner=self.request.user)
        if self.action == "observations":
            return queryset.only("id", "owner_id")
        if self.action in {"comfyui_workflow", "input_prompt"}:
            return queryset.defer("outputs")
        return queryset.select_related("project", "episode")

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(responses=GenerationTaskObservationSerializer(many=True),
        description="Paginated actual worker checks. No graph, credentials or frozen-input copy. A passed check is not generation acceptance.")
    @action(detail=True, methods=["get"], filter_backends=[])
    def observations(self, request, pk=None):
        if unknown := set(request.query_params) - {"page", "page_size", "format"}:
            raise ValidationError({"filters": f"不支持的检查记录筛选字段：{', '.join(sorted(unknown))}"})
        task = self.get_object()
        page = self.paginate_queryset(task.observations.all())
        response = self.get_paginated_response(GenerationTaskObservationSerializer(page, many=True).data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    @extend_schema(
        responses={200: GenerationTaskInputPromptSerializer},
        description=(
            "Return this actual preparation's frozen prompt channels, source mode and reference labels. "
            "No strategy rerun, rewriting, legacy PromptArtifact or full graph. A frozen request does not prove remote acceptance."
        ),
    )
    @action(detail=True, methods=["get"], url_path="input-prompt")
    def input_prompt(self, request, pk=None):
        task = self.get_object()
        from django.core.exceptions import ValidationError as DjangoValidationError
        from studio.services.execution_workflow_inspection import inspect_prepared_task_prompt
        try:
            payload = inspect_prepared_task_prompt(task)
        except DjangoValidationError as exc:
            raise ValidationError({"input_prompt": exc.messages}) from exc
        response = Response(GenerationTaskInputPromptSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response



    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="workflow_format",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["api", "ui"],
                description=(
                    "api (default) reads this call's frozen preparation and verified upload receipts. "
                    "ui derives a canvas using only this call's exact target node catalog."
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
            "Read the actual preparation-backed graph. Missing upload receipts are shown explicitly; corrupt "
            "facts fail instead of falling back to old workflows. No upload, compile, submission or database write. "
            "UI conversion reads the exact target's catalog; API export does not contact a provider."
        ),
    )
    @action(detail=True, methods=["get"], url_path="comfyui-workflow")
    def comfyui_workflow(self, request, pk=None):
        task = self.get_object()
        from django.core.exceptions import ValidationError as DjangoValidationError
        from studio.services.execution_workflow_inspection import inspect_prepared_task_workflow
        from studio.services.json_contracts import json_contract
        from studio.services.security_boundaries import boundary_int
        from studio.services.task_queue import comfy_client_for_task
        # Do not use the generic ``format`` query key: Django REST Framework
        # reserves it for renderer negotiation before this action is invoked.
        if any(len(values) != 1 for key, values in request.query_params.lists()) or set(request.query_params) - {"workflow_format", "download"}:
            raise ValidationError("仅支持单次提供workflow_format与download")
        requested_format = request.query_params.get("workflow_format", "api")
        if requested_format == "api":
            export_format = "comfyui_api"
        elif requested_format == "ui":
            export_format = "comfyui_ui"
        else:
            raise ValidationError({
                "workflow_format": (
                    "workflow_format 仅支持 api 或 ui。"
                )
            })
        download = request.query_params.get("download", "false")
        if download not in {"true", "false"}:
            raise ValidationError({"download": "download 仅支持 true 或 false"})
        try:
            snapshot = inspect_prepared_task_workflow(task)
        except DjangoValidationError as exc:
            raise ValidationError({"workflow": exc.messages}) from exc
        api_workflow = snapshot["workflow"]
        comfyui_url = ""
        if export_format == "comfyui_ui":
            try:
                client = comfy_client_for_task(task)
                object_info = client.object_info()
                comfyui_url = client.base_url.rstrip("/") + "/"
            except ComfyUIError as exc:
                return Response(
                    {
                        "detail": (
                            "无法读取 ComfyUI /object_info，暂时不能生成画布格式；"
                            "API 图仍可用默认格式导出。"
                        ),
                        "code": "workflow_target_unavailable",
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                workflow = api_graph_to_ui_workflow(
                    api_workflow,
                    object_info,
                    workflow_id=snapshot["preparation_id"],
                    metadata={
                        "task_id": str(task.id),
                        "preparation_id": snapshot["preparation_id"],
                        "workflow_version_id": snapshot["workflow_version_id"],
                        "request_sha256": snapshot["request_sha256"],
                    },
                )
            except ComfyUICanvasExportError as exc:
                raise ValidationError({"workflow": str(exc)}) from exc
            filename = f"autoprompt-task-{task.id}-comfyui-ui.json"
        else:
            workflow = api_workflow
            filename = f"autoprompt-task-{task.id}-comfyui-api.json"

        if download == "true":
            response = HttpResponse(
                json.dumps(workflow, ensure_ascii=False, indent=2),
                content_type="application/json; charset=utf-8",
            )
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response

        staged_placeholder_count = snapshot["staged_placeholder_count"]
        graph_valid = (
            is_comfyui_ui_workflow(workflow)
            if export_format == "comfyui_ui"
            else True  # The shared frozen transport reader has validated it.
        )
        import_ready = graph_valid and staged_placeholder_count == 0
        open_reason = (
            "ComfyUI 画布 JSON 已就绪，可下载后直接拖入或从 ComfyUI 工作流菜单打开。"
            if import_ready and export_format == "comfyui_ui"
            else "工作流 API JSON 已就绪，可用于 API 调试或队列提交；如需查看画布请选择 ui 格式。"
            if import_ready
            else "素材暂存回执尚未齐全；当前仅展示冻结准备图，不能当作实际已提交图执行。"
            if staged_placeholder_count
            else "工作流内容不是有效的所选 ComfyUI 导出格式。"
        )
        api_download_url = request.build_absolute_uri(f"{request.path}?download=true")
        ui_download_url = request.build_absolute_uri(
            f"{request.path}?workflow_format=ui&download=true"
        )
        payload = {
            **snapshot,
            "format": export_format,
            "filename": filename,
            "content_hash": json_contract(workflow,
                max_bytes=boundary_int("security.workflow.max_graph_bytes")).sha256,
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
        queryset = AssemblyJob.objects.annotate(
            _source_media_count=Count("source_items"),
            _model_source_count=Count("source_items", filter=Q(
                source_items__generated_artifact__generation_attempt__isnull=False,
            )))
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
        queryset = ReleaseBundle.objects.all()
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
