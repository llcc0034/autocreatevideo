from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from studio.services.task_distribution_recovery import (
    PHASE_FIELDS, distribution_phase, request_distribution_recovery, run_distribution_tasks,
)
from studio.task_distribution_serializers import TaskDistributionPhaseSerializer, TaskDistributionRecoveryInputSerializer


class TaskDistributionRecoveryMixin:
    @extend_schema(responses=TaskDistributionPhaseSerializer(many=True),
        description="Paginated original successful task publication phases for this Run. No filesystem scan or provider request. Readiness describes local processing, not a new generation or sealed release.")
    @action(detail=True, methods=("get",), url_path="distribution-recovery", filter_backends=[])
    def distribution_recovery(self, request, pk=None):
        if set(request.query_params) - {"page", "page_size", "format"} or any(len(values) != 1 for _, values in request.query_params.lists()):
            raise serializers.ValidationError("仅支持分页参数")
        run = self.get_object()
        rows = run_distribution_tasks(run).only(*PHASE_FIELDS).order_by("id")
        page = self.paginate_queryset(rows)
        response = self.get_paginated_response(TaskDistributionPhaseSerializer(
            [distribution_phase(task) for task in page], many=True).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=TaskDistributionRecoveryInputSerializer, responses={200: TaskDistributionPhaseSerializer, 202: TaskDistributionPhaseSerializer},
        description="Explicit compare-and-swap retry of local publication only. The same revision replays without another encode; an older revision conflicts. Uses the original generation worker, never model submission or release sealing.")
    @distribution_recovery.mapping.post
    def retry_distribution(self, request, pk=None):
        run = self.get_object()
        serializer = TaskDistributionRecoveryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            task, created = request_distribution_recovery(run_id=run.pk, actor=request.user, **serializer.validated_data)
        except DjangoValidationError as exc:
            if getattr(exc, "code", "") == "distribution_revision_conflict":
                return Response({"code": exc.code, "detail": exc.messages}, status=409)
            raise serializers.ValidationError(exc.messages) from exc
        response = Response(TaskDistributionPhaseSerializer(distribution_phase(task)).data, status=202 if created else 200)
        response["Cache-Control"] = "private, no-store"
        return response
