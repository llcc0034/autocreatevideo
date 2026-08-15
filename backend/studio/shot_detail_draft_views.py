from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import Episode, SeriesProject, Shot, ShotDetailDraft
from studio.serializers import (
    ShotDetailDraftApplySerializer,
    ShotDetailDraftConfirmSerializer,
    ShotDetailDraftGenerateSerializer,
    ShotDetailDraftPatchSerializer,
    ShotDetailDraftRegenerateSerializer,
    ShotDetailDraftSerializer,
    ShotSerializer,
)
from studio.services.creative_drafts import (
    CreativeContextConflict,
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRevisionConflict,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
)
from studio.services.shot_detail_drafts import (
    apply_shot_detail_draft,
    confirm_shot_detail_draft,
    create_failed_shot_detail_draft,
    create_shot_detail_draft,
    patch_shot_detail_draft,
    shot_detail_provider_label,
)


class ShotDetailConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "shot_detail_draft_conflict"


class ShotDetailBusy(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "creative_resource_busy"


ShotDetailApplyResponse = inline_serializer(
    name="ShotDetailApplyResponse",
    fields={
        "draft": ShotDetailDraftSerializer(),
        "applied_shot": ShotSerializer(),
        "translation_written": serializers.BooleanField(),
    },
)


def _owned_project(user, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.select_related("style")
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    return get_object_or_404(queryset, pk=project_id)


def _owned_shot(project: SeriesProject, shot_id) -> Shot:
    return get_object_or_404(
        Shot.objects.select_related("episode", "episode__project", "scene").prefetch_related(
            "characters", "props",
        ),
        pk=shot_id, episode__project=project,
    )


def _raise_error(exc: CreativeDraftError, *, provider: str | None = None):
    if isinstance(exc, CreativeResourceBusy):
        raise ShotDetailBusy(str(exc)) from exc
    if isinstance(exc, (CreativeContextConflict, CreativeRevisionConflict)):
        raise ShotDetailConflict(str(exc)) from exc
    if isinstance(exc, CreativeRuntimeUnavailable):
        error = APIException(str(exc))
        error.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        error.default_code = "shot_detail_runtime_unavailable"
        raise error from exc
    if isinstance(exc, CreativeValidationError):
        safe_messages = {
            "shot_detail_review_acknowledgement_required": "请先阅读高细节草稿的人工核验项并明确确认风险。",
            "shot_detail_requires_edit": "高细节草稿仍有必须人工修复的内容，修正后再应用。",
        }
        raise ValidationError({
            "code": exc.code,
            "detail": safe_messages.get(exc.code, "高细节候选暂不能继续，请检查可编辑内容后重试。"),
        }) from exc
    selected = provider or (exc.runtime_metadata or {}).get("provider") or "local_qwen"
    error = APIException(
        f"{shot_detail_provider_label(selected)} 未返回可审核的高细节候选；正式镜头未修改。",
    )
    error.status_code = status.HTTP_502_BAD_GATEWAY
    error.default_code = "shot_detail_runtime_failed"
    raise error from exc


class ProjectShotDetailDraftListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ShotDetailDraftSerializer(many=True))
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        queryset = project.shot_detail_drafts.select_related(
            "project", "project__style", "episode", "shot", "parent_draft",
            "confirmed_by", "applied_by",
        )
        shot_id = request.query_params.get("shot")
        episode_id = request.query_params.get("episode")
        if shot_id:
            queryset = queryset.filter(shot=_owned_shot(project, shot_id))
        if episode_id:
            episode = get_object_or_404(Episode, pk=episode_id, project=project)
            queryset = queryset.filter(episode=episode)
        return Response(ShotDetailDraftSerializer(queryset, many=True).data)


class ProjectShotDetailDraftGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ShotDetailDraftGenerateSerializer,
        responses={201: ShotDetailDraftSerializer, 502: dict},
    )
    def post(self, request, project_id):
        project = _owned_project(request.user, project_id)
        serializer = ShotDetailDraftGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data["provider"]
        shot = _owned_shot(project, data["shot_id"])
        try:
            draft = create_shot_detail_draft(
                project=project, episode=shot.episode, shot=shot, user=request.user,
                variation_note=data["variation_note"],
                provider=provider,
            )
        except (
            CreativeResourceBusy, CreativeContextConflict, CreativeRevisionConflict,
            CreativeValidationError, CreativeRuntimeUnavailable,
        ) as exc:
            _raise_error(exc, provider=provider)
        except CreativeDraftError as exc:
            failed = create_failed_shot_detail_draft(
                project=project, episode=shot.episode, shot=shot, user=request.user,
                variation_note=data["variation_note"], error=str(exc), error_code=exc.code,
                runtime_metadata=exc.runtime_metadata,
                attempt_context_snapshot=exc.attempt_context_snapshot,
                attempt_context_hash=exc.attempt_context_hash,
                source_drift_during_attempt=exc.source_drift_during_attempt,
                provider=provider,
            )
            if failed.status == ShotDetailDraft.Status.DRAFT:
                return Response(
                    ShotDetailDraftSerializer(failed).data,
                    status=status.HTTP_201_CREATED,
                )
            return Response({
                "detail": (
                    f"{shot_detail_provider_label(provider)} 未返回可审核的高细节候选；"
                    "正式镜头未修改。"
                ),
                "code": exc.code,
                "draft": ShotDetailDraftSerializer(failed).data,
            }, status=status.HTTP_502_BAD_GATEWAY)
        return Response(ShotDetailDraftSerializer(draft).data, status=status.HTTP_201_CREATED)


class ShotDetailDraftViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ShotDetailDraftSerializer
    filterset_fields = ("project", "episode", "shot", "status", "generation_no")
    ordering_fields = ("created_at", "updated_at", "generation_no", "revision")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ShotDetailDraft.objects.none()
        queryset = ShotDetailDraft.objects.select_related(
            "project", "project__style", "episode", "shot", "parent_draft",
            "confirmed_by", "applied_by",
        )
        return queryset if self.request.user.is_staff else queryset.filter(project__owner=self.request.user)

    @extend_schema(request=ShotDetailDraftPatchSerializer, responses=ShotDetailDraftSerializer)
    def partial_update(self, request, *args, **kwargs):
        input_serializer = ShotDetailDraftPatchSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        current = self.get_object()
        data = input_serializer.validated_data
        try:
            draft = patch_shot_detail_draft(
                draft=current, expected_revision=data["expected_revision"], draft_data=data["draft_data"],
            )
        except CreativeDraftError as exc:
            _raise_error(exc)
        return Response(ShotDetailDraftSerializer(draft).data)

    @extend_schema(request=ShotDetailDraftRegenerateSerializer, responses={201: ShotDetailDraftSerializer})
    @action(detail=True, methods=["post"], url_path="regenerate")
    def regenerate(self, request, pk=None):
        current = self.get_object()
        serializer = ShotDetailDraftRegenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = data.get("provider", current.provider or "local_qwen")
        if current.revision != data["expected_revision"]:
            raise ShotDetailConflict("高细节草稿版本已变化，请刷新后重新生成")
        if current.status not in {
            ShotDetailDraft.Status.DRAFT, ShotDetailDraft.Status.CONFIRMED, ShotDetailDraft.Status.FAILED,
        }:
            raise ValidationError("已应用或已被替代的高细节草稿不能重新生成")
        try:
            draft = create_shot_detail_draft(
                project=current.project, episode=current.episode, shot=current.shot,
                user=request.user, variation_note=data["variation_note"], parent=current,
                expected_parent_revision=data["expected_revision"],
                provider=provider,
            )
        except (
            CreativeResourceBusy, CreativeContextConflict, CreativeRevisionConflict,
            CreativeValidationError, CreativeRuntimeUnavailable,
        ) as exc:
            _raise_error(exc, provider=provider)
        except CreativeDraftError as exc:
            try:
                failed = create_failed_shot_detail_draft(
                    project=current.project, episode=current.episode, shot=current.shot,
                    user=request.user, variation_note=data["variation_note"], error=str(exc),
                    error_code=exc.code, runtime_metadata=exc.runtime_metadata, parent=current,
                    attempt_context_snapshot=exc.attempt_context_snapshot,
                    attempt_context_hash=exc.attempt_context_hash,
                    source_drift_during_attempt=exc.source_drift_during_attempt,
                    expected_parent_revision=data["expected_revision"],
                    provider=provider,
                )
            except CreativeDraftError as save_exc:
                _raise_error(save_exc, provider=provider)
            if failed.status == ShotDetailDraft.Status.DRAFT:
                return Response(
                    ShotDetailDraftSerializer(failed).data,
                    status=status.HTTP_201_CREATED,
                )
            return Response({
                "detail": (
                    f"{shot_detail_provider_label(provider)} 未返回可审核的高细节候选；"
                    "正式镜头未修改。"
                ),
                "code": exc.code,
                "draft": ShotDetailDraftSerializer(failed).data,
            }, status=status.HTTP_502_BAD_GATEWAY)
        return Response(ShotDetailDraftSerializer(draft).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ShotDetailDraftConfirmSerializer, responses=ShotDetailDraftSerializer)
    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        serializer = ShotDetailDraftConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            draft = confirm_shot_detail_draft(
                draft=self.get_object(), user=request.user,
                expected_revision=serializer.validated_data["expected_revision"],
                review_acknowledged=serializer.validated_data["review_acknowledged"],
            )
        except CreativeDraftError as exc:
            _raise_error(exc)
        return Response(ShotDetailDraftSerializer(draft).data)

    @extend_schema(request=ShotDetailDraftApplySerializer, responses=ShotDetailApplyResponse)
    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, pk=None):
        serializer = ShotDetailDraftApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            draft, shot, translation_written = apply_shot_detail_draft(
                draft=self.get_object(), user=request.user,
                language_mode=data["language_mode"], expected_revision=data["expected_revision"],
                review_acknowledged=data["review_acknowledged"],
            )
        except CreativeDraftError as exc:
            _raise_error(exc)
        return Response({
            "draft": ShotDetailDraftSerializer(draft).data,
            "applied_shot": ShotSerializer(shot, context={"request": request}).data,
            "translation_written": translation_written,
        })
