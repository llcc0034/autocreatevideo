from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from studio.deferred_generation_serializers import (
    DeferredGenerationRequestDetailSerializer,
    DeferredGenerationRequestListSerializer,
)
from studio.models import DeferredGenerationRequest


class DeferredGenerationRequestViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Read-only diagnostics for historical deferred-generation chains."""

    permission_classes = [IsAuthenticated]
    serializer_class = DeferredGenerationRequestListSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = DeferredGenerationRequest.objects.select_related(
            "project",
            "source_task",
            "target_shot",
            "target_workflow_version__template",
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
        return DeferredGenerationRequestListSerializer
