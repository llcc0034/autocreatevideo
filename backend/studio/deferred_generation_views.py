from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from studio.deferred_generation_serializers import (
    DeferredGenerationCancelSerializer,
    DeferredGenerationErrorResponseSerializer,
    DeferredGenerationRequestCreateSerializer,
    DeferredGenerationRequestDetailSerializer,
    DeferredGenerationRequestListSerializer,
)
from studio.models import DeferredGenerationRequest, GenerationTask, SeriesProject, Shot
from studio.serializers import APIValidationErrorEnvelopeSerializer
from studio.services.deferred_generation import (
    cancel_deferred_generation_request,
    create_deferred_generation_request,
)


class DeferredGenerationRequestViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = DeferredGenerationRequestListSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = DeferredGenerationRequest.objects.select_related(
            "project",
            "source_task",
            "target_shot",
            "target_template",
            "derived_asset",
            "materialized_task__generated_workflow",
        ).order_by("-priority", "created_at")
        user = self.request.user
        if not getattr(user, "is_authenticated", False):
            return queryset.none()
        if not user.is_staff:
            queryset = queryset.filter(owner=user)
        params = self.request.query_params
        filters = {
            "project_id": params.get("project"),
            "source_task_id": params.get("source_task"),
            "target_shot_id": params.get("target_shot"),
            "status": params.get("status"),
            "frame_slot": params.get("frame_slot"),
        }
        for key, value in filters.items():
            if value:
                queryset = queryset.filter(**{key: value})
        return queryset

    def get_serializer_class(self):
        if self.action == "retrieve":
            return DeferredGenerationRequestDetailSerializer
        if self.action == "create":
            return DeferredGenerationRequestCreateSerializer
        if self.action == "cancel":
            return DeferredGenerationCancelSerializer
        return DeferredGenerationRequestListSerializer

    @extend_schema(
        request=DeferredGenerationRequestCreateSerializer,
        responses={
            200: DeferredGenerationRequestDetailSerializer,
            201: DeferredGenerationRequestDetailSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            404: DeferredGenerationErrorResponseSerializer,
            409: DeferredGenerationErrorResponseSerializer,
            503: DeferredGenerationErrorResponseSerializer,
        },
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        try:
            project_qs = SeriesProject.objects.all()
            source_qs = GenerationTask.objects.all()
            shot_qs = Shot.objects.select_related("episode__project")
            if not user.is_staff:
                project_qs = project_qs.filter(owner=user)
                source_qs = source_qs.filter(owner=user)
                shot_qs = shot_qs.filter(episode__project__owner=user)
            project = project_qs.get(pk=data["project_id"])
            source_task = source_qs.get(pk=data["source_task_id"])
            target_shot = shot_qs.get(pk=data["target_shot_id"])
        except (SeriesProject.DoesNotExist, GenerationTask.DoesNotExist, Shot.DoesNotExist) as exc:
            raise NotFound("延迟生成请求引用的项目、任务或分镜不存在。") from exc

        deferred, created = create_deferred_generation_request(
            user=user,
            project=project,
            source_task=source_task,
            target_shot=target_shot,
            frame_slot=data["frame_slot"],
            idempotency_key=data["idempotency_key"],
            priority=data["priority"],
            max_attempts=data["max_attempts"],
            extended_generation_confirmed=data["extended_generation_confirmed"],
        )
        response_serializer = DeferredGenerationRequestDetailSerializer(
            deferred,
            context={"request": request},
        )
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=DeferredGenerationCancelSerializer,
        responses={
            200: DeferredGenerationRequestDetailSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            409: DeferredGenerationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        serializer = self.get_serializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        deferred = cancel_deferred_generation_request(
            request=self.get_object(),
            user=request.user,
        )
        return Response(
            DeferredGenerationRequestDetailSerializer(
                deferred,
                context={"request": request},
            ).data,
        )
