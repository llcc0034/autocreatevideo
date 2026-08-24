from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import Episode, SeriesProject, Shot, ShotDetailDraftBatch
from studio.serializers import (
    APIValidationErrorEnvelopeSerializer,
    ShotSerializer,
    ShotDetailDraftBatchItemSerializer,
    ShotDetailDraftBatchSerializer,
)
from studio.services.shot_versions import (
    ShotVersionResolutionError,
    resolve_episode_shot_version,
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
ShotDetailBatchListResponse = inline_serializer(
    name="PaginatedShotDetailDraftBatchList",
    fields={
        "count": serializers.IntegerField(min_value=0),
        "next": serializers.URLField(allow_null=True),
        "previous": serializers.URLField(allow_null=True),
        "results": ShotDetailDraftBatchSerializer(many=True),
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




class ProjectShotDetailDraftBatchListView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="episode",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="所选剧集 UUID。必须与 shot_version_key 同时提供。",
            ),
            OpenApiParameter(
                name="shot_version_key",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                description=(
                    "当前明确选择的分镜版本标识。只返回该版本的可归属批次，"
                    "不会回退到同剧集的其他版本。"
                ),
            ),
        ],
        responses={
            200: ShotDetailBatchListResponse,
            400: APIValidationErrorEnvelopeSerializer,
        },
    )
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        episode_id = str(request.query_params.get("episode") or "").strip()
        shot_version_key = str(
            request.query_params.get("shot_version_key") or ""
        ).strip()
        if not episode_id or not shot_version_key:
            raise ValidationError({
                "episode": "episode 与 shot_version_key 必须同时提供",
                "shot_version_key": "必须明确选择分镜版本",
            })
        episode = get_object_or_404(Episode, pk=episode_id, project=project)
        try:
            resolved_version = resolve_episode_shot_version(
                episode, shot_version_key,
            )
        except ShotVersionResolutionError as exc:
            raise ValidationError({"shot_version_key": str(exc)}) from exc
        queryset = project.shot_detail_batches.select_related(
            "episode", "created_by",
        ).filter(
            episode=episode,
            shot_version_key=resolved_version.key,
            version_scope_status=ShotDetailDraftBatch.VersionScopeStatus.RESOLVED,
        ).order_by("-created_at")
        paginator = ShotDetailBatchPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(
            ShotDetailDraftBatchSerializer(page, many=True).data,
        )


class ShotDetailDraftBatchDetailView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

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
