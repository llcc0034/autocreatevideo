from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers

from studio.models import Episode, EpisodeCreativeDraft, SeriesProject
from studio.serializers import (
    CreativeDraftApplySerializer,
    CreativeDraftConfirmSerializer,
    CreativeDraftGenerateSerializer,
    CreativeDraftPatchSerializer,
    CreativeDraftRegenerateSerializer,
    EpisodeCreativeDraftSerializer,
    EpisodeSerializer,
    ShotSerializer,
)
from studio.services.creative_drafts import (
    CreativeContextConflict,
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRevisionConflict,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
    append_creative_review_acknowledgement,
    append_revision_history,
    apply_creative_draft,
    create_creative_draft,
    create_failed_creative_draft,
    creative_context_hash,
    creative_review_issues,
    validate_creative_draft_data,
)


class CreativeConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "creative_draft_conflict"


class CreativeBusy(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "creative_resource_busy"


CreativeDraftApplyResponse = inline_serializer(
    name="CreativeDraftApplyResponse",
    fields={
        "draft": EpisodeCreativeDraftSerializer(),
        "episode": EpisodeSerializer(),
        "shots": ShotSerializer(many=True),
        "applied_shot_ids": serializers.ListField(child=serializers.UUIDField()),
        "translations_written": serializers.IntegerField(min_value=0),
    },
)


def _owned_project(user, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.select_related("style")
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    return get_object_or_404(queryset, pk=project_id)


def _owned_episode(project: SeriesProject, episode_id) -> Episode:
    return get_object_or_404(
        Episode.objects.select_related("project", "project__style"),
        pk=episode_id, project=project,
    )


def _raise_creative_error(exc: CreativeDraftError):
    if isinstance(exc, CreativeResourceBusy):
        raise CreativeBusy(str(exc)) from exc
    if isinstance(exc, (CreativeContextConflict, CreativeRevisionConflict)):
        raise CreativeConflict(str(exc)) from exc
    if isinstance(exc, CreativeRuntimeUnavailable):
        error = APIException(str(exc))
        error.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        error.default_code = "creative_runtime_unavailable"
        raise error from exc
    if isinstance(exc, CreativeValidationError):
        raise ValidationError({
            "code": exc.code, "detail": str(exc), "errors": exc.errors,
        }) from exc
    error = APIException(str(exc))
    error.status_code = status.HTTP_502_BAD_GATEWAY
    error.default_code = "creative_runtime_failed"
    raise error from exc


class ProjectCreativeDraftListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=EpisodeCreativeDraftSerializer(many=True))
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        queryset = project.creative_drafts.select_related(
            "project", "project__style", "episode", "parent_draft",
            "confirmed_by", "applied_by",
        )
        episode_id = request.query_params.get("episode")
        if episode_id:
            episode = _owned_episode(project, episode_id)
            queryset = queryset.filter(episode=episode)
        return Response(EpisodeCreativeDraftSerializer(queryset, many=True).data)


class ProjectCreativeDraftGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=CreativeDraftGenerateSerializer,
        responses={201: EpisodeCreativeDraftSerializer, 502: dict},
    )
    def post(self, request, project_id):
        project = _owned_project(request.user, project_id)
        serializer = CreativeDraftGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data["provider"]
        episode = _owned_episode(project, data["episode_id"])
        try:
            draft = create_creative_draft(
                project=project, episode=episode, user=request.user,
                target_shot_count=data["target_shot_count"],
                creativity=data["creativity"], variation_note=data["variation_note"],
                provider=provider,
            )
        except (CreativeResourceBusy, CreativeContextConflict, CreativeValidationError,
                CreativeRuntimeUnavailable) as exc:
            _raise_creative_error(exc)
        except CreativeDraftError as exc:
            # Runtime failures remain inspectable/retryable, while still
            # returning a truthful gateway error to the caller.
            failed = create_failed_creative_draft(
                project=project, episode=episode, user=request.user,
                target_shot_count=data["target_shot_count"],
                creativity=data["creativity"], variation_note=data["variation_note"],
                error=str(exc), error_code=exc.code,
                runtime_metadata=exc.runtime_metadata,
                attempt_context_snapshot=exc.attempt_context_snapshot,
                attempt_context_hash=exc.attempt_context_hash,
                source_drift_during_attempt=exc.source_drift_during_attempt,
                provider=provider,
            )
            return Response({
                "detail": str(exc),
                "code": exc.code,
                "draft": EpisodeCreativeDraftSerializer(failed).data,
            }, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            EpisodeCreativeDraftSerializer(draft).data,
            status=status.HTTP_201_CREATED,
        )


class EpisodeCreativeDraftViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = EpisodeCreativeDraftSerializer
    filterset_fields = ("project", "episode", "status", "generation_no")
    ordering_fields = ("created_at", "updated_at", "generation_no", "revision")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EpisodeCreativeDraft.objects.none()
        queryset = EpisodeCreativeDraft.objects.select_related(
            "project", "project__style", "episode", "parent_draft",
            "confirmed_by", "applied_by",
        )
        return queryset if self.request.user.is_staff else queryset.filter(
            project__owner=self.request.user,
        )

    @extend_schema(
        request=CreativeDraftPatchSerializer,
        responses=EpisodeCreativeDraftSerializer,
    )
    def partial_update(self, request, *args, **kwargs):
        input_serializer = CreativeDraftPatchSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        with transaction.atomic():
            try:
                draft = self.get_queryset().select_for_update().get(pk=kwargs["pk"])
            except EpisodeCreativeDraft.DoesNotExist as exc:
                raise NotFound("创作草稿不存在") from exc
            if draft.revision != data["expected_revision"]:
                raise CreativeConflict("草稿版本已变化，请刷新后再编辑")
            if draft.status not in {
                EpisodeCreativeDraft.Status.DRAFT,
                EpisodeCreativeDraft.Status.FAILED,
            }:
                raise ValidationError("只有待审核或校验失败草稿可以人工编辑")
            project = SeriesProject.objects.select_for_update().select_related("style").get(
                pk=draft.project_id,
            )
            episode = Episode.objects.select_for_update().get(pk=draft.episode_id)
            if creative_context_hash(project, episode) != draft.context_hash:
                raise CreativeConflict("创作上下文已变化，请重新生成")
            normalized, validation = validate_creative_draft_data(
                project, episode, data["draft_data"],
                target_shot_count=len(data["draft_data"]["shots"]),
            )
            append_revision_history(draft)
            draft.revision += 1
            draft.draft_data = normalized
            draft.validation = validation
            draft.warnings = validation["warnings"]
            draft.status = (
                EpisodeCreativeDraft.Status.DRAFT
                if validation["status"] in {"passed", "ready", "needs_review"}
                else EpisodeCreativeDraft.Status.FAILED
            )
            draft.error_message = (
                "" if validation["status"] in {"passed", "ready", "needs_review"}
                else "；".join(validation["errors"])[:10000]
            )
            draft.confirmed_by = None
            draft.confirmed_at = None
            draft.save()
        return Response(EpisodeCreativeDraftSerializer(draft).data)

    @extend_schema(
        request=CreativeDraftRegenerateSerializer,
        responses={201: EpisodeCreativeDraftSerializer},
    )
    @action(detail=True, methods=["post"], url_path="regenerate")
    def regenerate(self, request, pk=None):
        current = self.get_object()
        serializer = CreativeDraftRegenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data.get("provider", current.provider or "local_qwen")
        if current.revision != data["expected_revision"]:
            raise CreativeConflict("草稿版本已变化，请刷新后再重新生成")
        if current.status not in {
            EpisodeCreativeDraft.Status.DRAFT,
            EpisodeCreativeDraft.Status.CONFIRMED,
            EpisodeCreativeDraft.Status.FAILED,
        }:
            raise ValidationError("已应用或已被替代的草稿不能重新生成")
        current_count = len((current.draft_data or {}).get("shots", []))
        target_count = data.get("target_shot_count", current_count or current.input_settings.get("target_shot_count", 4))
        shot_keys = data.get("shot_keys")
        if shot_keys:
            available = {
                item.get("client_key") for item in (current.draft_data or {}).get("shots", [])
            }
            if not set(shot_keys) <= available:
                raise ValidationError({"shot_keys": "包含不存在的草稿镜头"})
            if not data["preserve_locked_content"]:
                raise ValidationError({
                    "preserve_locked_content": "逐镜重新生成必须保留未选中的镜头",
                })
            if target_count != current_count:
                raise ValidationError({
                    "target_shot_count": "逐镜重新生成时镜头总数必须保持不变",
                })
        try:
            draft = create_creative_draft(
                project=current.project, episode=current.episode, user=request.user,
                target_shot_count=target_count,
                creativity=data.get("creativity", current.creativity),
                variation_note=data.get("variation_note", current.variation_note),
                parent=current, merge_from=current if shot_keys else None,
                shot_keys=shot_keys, expected_parent_revision=data["expected_revision"],
                provider=provider,
            )
        except (
            CreativeResourceBusy, CreativeContextConflict, CreativeRevisionConflict,
            CreativeValidationError, CreativeRuntimeUnavailable,
        ) as exc:
            _raise_creative_error(exc)
        except CreativeDraftError as exc:
            try:
                failed = create_failed_creative_draft(
                    project=current.project, episode=current.episode, user=request.user,
                    target_shot_count=target_count,
                    creativity=data.get("creativity", current.creativity),
                    variation_note=data.get("variation_note", current.variation_note),
                    error=str(exc), error_code=exc.code,
                    runtime_metadata=exc.runtime_metadata, parent=current,
                    attempt_context_snapshot=exc.attempt_context_snapshot,
                    attempt_context_hash=exc.attempt_context_hash,
                    source_drift_during_attempt=exc.source_drift_during_attempt,
                    expected_parent_revision=data["expected_revision"],
                    provider=provider,
                )
            except CreativeDraftError as save_exc:
                _raise_creative_error(save_exc)
            return Response({
                "detail": str(exc), "code": exc.code,
                "draft": EpisodeCreativeDraftSerializer(failed).data,
            }, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            EpisodeCreativeDraftSerializer(draft).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=CreativeDraftConfirmSerializer,
        responses=EpisodeCreativeDraftSerializer,
    )
    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        serializer = CreativeDraftConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            try:
                draft = self.get_queryset().select_for_update().get(pk=pk)
            except EpisodeCreativeDraft.DoesNotExist as exc:
                raise NotFound("创作草稿不存在") from exc
            if draft.revision != serializer.validated_data["expected_revision"]:
                raise CreativeConflict("草稿版本已变化，请刷新后再确认")
            if draft.status != EpisodeCreativeDraft.Status.DRAFT:
                raise ValidationError("只有待审核草稿可以确认")
            project = SeriesProject.objects.select_for_update().select_related("style").get(
                pk=draft.project_id,
            )
            episode = Episode.objects.select_for_update().get(pk=draft.episode_id)
            if creative_context_hash(project, episode) != draft.context_hash:
                raise CreativeConflict("项目、Bible、剧集或正式镜头已变化，请重新生成")
            normalized, validation = validate_creative_draft_data(
                project, episode, draft.draft_data,
                target_shot_count=len((draft.draft_data or {}).get("shots", [])),
            )
            if validation["status"] not in {"passed", "ready", "needs_review"}:
                raise ValidationError({"validation": validation})
            if creative_review_issues(validation) and not serializer.validated_data[
                "review_acknowledged"
            ]:
                raise ValidationError({
                    "review_acknowledged": "请先阅读草稿问题清单并明确确认风险",
                    "review_summary": validation.get("summary", {}),
                })
            append_revision_history(draft)
            draft.revision += 1
            draft.draft_data = normalized
            draft.validation = validation
            draft.warnings = validation.get("warnings", [])
            draft.status = EpisodeCreativeDraft.Status.CONFIRMED
            draft.confirmed_by = request.user
            draft.confirmed_at = timezone.now()
            draft.error_message = ""
            if creative_review_issues(validation):
                append_creative_review_acknowledgement(
                    draft, user=request.user, action="confirm",
                )
            draft.save()
        return Response(EpisodeCreativeDraftSerializer(draft).data)

    @extend_schema(
        request=CreativeDraftApplySerializer,
        responses=CreativeDraftApplyResponse,
    )
    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, pk=None):
        draft = self.get_object()
        serializer = CreativeDraftApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            episode, shots, translation_count = apply_creative_draft(
                draft=draft, user=request.user, language_mode=data["language_mode"],
                placement=data["placement"], shot_keys=data.get("shot_keys"),
                expected_revision=data["expected_revision"],
                review_acknowledged=data["review_acknowledged"],
            )
        except CreativeDraftError as exc:
            _raise_creative_error(exc)
        draft.refresh_from_db()
        return Response({
            "draft": EpisodeCreativeDraftSerializer(draft).data,
            "episode": EpisodeSerializer(episode, context={"request": request}).data,
            "shots": ShotSerializer(
                shots, many=True, context={"request": request},
            ).data,
            "applied_shot_ids": [str(item.id) for item in shots],
            "translations_written": translation_count,
        })
