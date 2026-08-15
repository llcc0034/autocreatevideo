from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import Episode, SeriesProject, Shot, ShotDetailDraftBatch
from studio.serializers import (
    ShotSerializer,
    ShotDetailDraftBatchApplySerializer,
    ShotDetailDraftBatchCreateSerializer,
    ShotDetailDraftBatchItemSerializer,
    ShotDetailDraftBatchSerializer,
)
from studio.services.creative_drafts import (
    CreativeContextConflict,
    CreativeDraftError,
    CreativeRevisionConflict,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
)
from studio.services.shot_detail_batches import (
    ShotDetailBatchConfirmationRequired,
    ShotDetailBatchConflict,
    apply_shot_detail_batch,
    create_shot_detail_batch,
)


class ShotDetailBatchPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


ShotDetailBatchDetailResponse = inline_serializer(
    name="ShotDetailBatchDetailResponse",
    fields={
        "batch": ShotDetailDraftBatchSerializer(),
        "items": ShotDetailDraftBatchItemSerializer(many=True),
        "applied_shots": ShotSerializer(many=True),
        "translation_written": serializers.BooleanField(),
        "pagination": inline_serializer(
            name="ShotDetailBatchItemPagination",
            fields={
                "count": serializers.IntegerField(min_value=0),
                "next": serializers.URLField(allow_null=True),
                "previous": serializers.URLField(allow_null=True),
            },
        ),
    },
)
ShotDetailBatchApplyResponse = inline_serializer(
    name="ShotDetailBatchApplyResponse",
    fields={
        "batch": ShotDetailDraftBatchSerializer(),
        "items": ShotDetailDraftBatchItemSerializer(many=True),
        "applied_shots": ShotSerializer(many=True),
        "translation_written": serializers.BooleanField(),
    },
)


def _owned_project(user, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.select_related("style")
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    return get_object_or_404(queryset, pk=project_id)


def _owned_batch(user, batch_id) -> ShotDetailDraftBatch:
    queryset = ShotDetailDraftBatch.objects.select_related(
        "project", "episode", "created_by",
    )
    if not user.is_staff:
        queryset = queryset.filter(project__owner=user)
    return get_object_or_404(queryset, pk=batch_id)


def _raise_batch_error(exc: CreativeDraftError):
    if isinstance(exc, ShotDetailBatchConfirmationRequired):
        return Response({
            "code": exc.code,
            "detail": str(exc),
            "confirmation": exc.details,
        }, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, ShotDetailBatchConflict):
        return Response({"code": exc.code, "detail": str(exc)}, status=409)
    if isinstance(exc, (CreativeContextConflict, CreativeRevisionConflict)):
        return Response({"code": exc.code, "detail": str(exc)}, status=409)
    if isinstance(exc, CreativeRuntimeUnavailable):
        return Response({"code": exc.code, "detail": str(exc)}, status=503)
    if isinstance(exc, CreativeValidationError):
        return Response({"code": exc.code, "detail": str(exc)}, status=400)
    error = APIException("整集高细节扩写请求失败")
    error.status_code = status.HTTP_502_BAD_GATEWAY
    error.default_code = getattr(exc, "code", "shot_detail_batch_failed")
    raise error from exc


class ProjectShotDetailDraftBatchListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ShotDetailDraftBatchSerializer(many=True))
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        queryset = project.shot_detail_batches.select_related(
            "episode", "created_by",
        ).order_by("-created_at")
        episode_id = request.query_params.get("episode")
        if episode_id:
            queryset = queryset.filter(episode_id=episode_id)
        paginator = ShotDetailBatchPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(
            ShotDetailDraftBatchSerializer(page, many=True).data,
        )

    @extend_schema(
        request=ShotDetailDraftBatchCreateSerializer,
        responses={
            200: ShotDetailDraftBatchSerializer,
            202: ShotDetailDraftBatchSerializer,
            409: dict,
            503: dict,
        },
    )
    def post(self, request, project_id):
        project = _owned_project(request.user, project_id)
        serializer = ShotDetailDraftBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        episode = get_object_or_404(Episode, pk=data["episode_id"], project=project)
        try:
            batch, created = create_shot_detail_batch(
                project=project,
                episode=episode,
                user=request.user,
                request_id=data["request_id"],
                provider=data["provider"],
                variation_note=data["variation_note"],
                confirmation_acknowledged=data["confirmation_acknowledged"],
            )
        except CreativeDraftError as exc:
            return _raise_batch_error(exc)
        return Response(
            ShotDetailDraftBatchSerializer(batch).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class ShotDetailDraftBatchDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ShotDetailBatchDetailResponse)
    def get(self, request, batch_id):
        batch = _owned_batch(request.user, batch_id)
        queryset = batch.items.select_related(
            "shot", "draft", "draft__project", "draft__episode", "draft__shot",
        ).order_by("ordinal")
        paginator = ShotDetailBatchPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        applied_shots: list[Shot] = []
        translation_written = False
        if batch.apply_status == ShotDetailDraftBatch.ApplyStatus.COMPLETE:
            applied_shots = list(
                Shot.objects.filter(
                    detail_batch_items__batch=batch,
                    detail_batch_items__applied_at__isnull=False,
                ).distinct().order_by("number", "id")
            )
            translation_written = bool(applied_shots) and all(
                ((shot.metadata or {}).get("shot_detail_provenance") or {}).get(
                    "applied_language_mode",
                ) == "bilingual_confirmed"
                for shot in applied_shots
            )
        return Response({
            "batch": ShotDetailDraftBatchSerializer(batch).data,
            "items": ShotDetailDraftBatchItemSerializer(page, many=True).data,
            # A completed apply can outlive its HTTP response.  Returning the
            # committed shots here lets clients recover without reconstructing
            # formal state from the proposal payload.
            "applied_shots": ShotSerializer(
                applied_shots, many=True, context={"request": request},
            ).data,
            "translation_written": translation_written,
            "pagination": {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
            },
        })


class ShotDetailDraftBatchApplyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ShotDetailDraftBatchApplySerializer,
        responses={200: ShotDetailBatchApplyResponse, 400: dict, 409: dict},
    )
    def post(self, request, batch_id):
        batch = _owned_batch(request.user, batch_id)
        serializer = ShotDetailDraftBatchApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            applied_batch, plans = apply_shot_detail_batch(
                batch=batch,
                user=request.user,
                expected_batch_revision=data["expected_batch_revision"],
                items=data["items"],
                language_mode=data["language_mode"],
                review_acknowledged=data["review_acknowledged"],
            )
        except CreativeDraftError as exc:
            return _raise_batch_error(exc)
        items = applied_batch.items.select_related(
            "shot", "draft", "draft__project", "draft__episode", "draft__shot",
        ).order_by("ordinal")
        return Response({
            "batch": ShotDetailDraftBatchSerializer(applied_batch).data,
            "items": ShotDetailDraftBatchItemSerializer(items, many=True).data,
            # Return the committed database-backed shot values.  The client
            # must not reconstruct formal state from a proposal after apply.
            "applied_shots": ShotSerializer(
                [plan.shot for plan in plans], many=True, context={"request": request},
            ).data,
            "translation_written": data["language_mode"] == "bilingual_confirmed",
        })
