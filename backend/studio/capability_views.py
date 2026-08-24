import hashlib

from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView

from studio.services.prompt_normalization import prompt_normalization_capability
from studio.models import GenerationTask, SeriesProject
from studio.services.configuration import ConfigurationResolutionError, resolve_configuration
from studio.services.json_contracts import canonical_json_bytes


ProductionCapabilityResponse = inline_serializer(
    name="ProductionCapability",
    fields={
        "recommended_max_pending": serializers.IntegerField(min_value=1),
        "recommended_max_active_per_user": serializers.IntegerField(min_value=1),
        "max_concurrent": serializers.IntegerField(min_value=1),
        "configured_concurrency": serializers.IntegerField(min_value=1),
        "deployment_max_concurrent": serializers.IntegerField(min_value=1),
        "task_timeout_seconds": serializers.FloatField(min_value=0),
        "configured_task_timeout_seconds": serializers.FloatField(min_value=0),
        "task_idle_timeout_seconds": serializers.FloatField(min_value=0),
        "video_shot_min_duration_ms": serializers.IntegerField(min_value=1),
        "video_shot_max_duration_ms": serializers.IntegerField(min_value=1),
        "script_breakdown_default_shot_duration_ms": serializers.IntegerField(min_value=1),
        "script_breakdown_default_first_frame_continuity": serializers.BooleanField(),
        "script_breakdown_default_last_frame_continuity": serializers.BooleanField(),
        "script_breakdown_auto_split_trigger_chars": serializers.IntegerField(min_value=1),
        "script_breakdown_auto_split_target_chars": serializers.IntegerField(min_value=1),
        "active_task_count": serializers.IntegerField(min_value=0),
        "user_active_task_count": serializers.IntegerField(min_value=0),
        "confirmation_required": serializers.BooleanField(),
        "business_volume_limited": serializers.BooleanField(),
        "configuration_public_sha256": serializers.RegexField(r"^[0-9a-f]{64}$"),
        "configuration_sources": serializers.DictField(
            child=serializers.CharField(),
        ),
    },
)


ProductionCapabilityErrorResponse = inline_serializer(
    name="ProductionCapabilityError",
    fields={
        "code": serializers.CharField(),
        "detail": serializers.CharField(),
        "missing_keys": serializers.ListField(child=serializers.CharField()),
    },
)


PromptNormalizationDeploymentResponse = inline_serializer(
    name="PromptNormalizationDeployment",
    many=True,
    fields={
        "model_deployment_id": serializers.UUIDField(),
        "deployment_key": serializers.CharField(),
        "label": serializers.CharField(),
        "provider_key": serializers.CharField(),
        "provider_label": serializers.CharField(),
        "provider_driver": serializers.CharField(),
        "model_profile_version_id": serializers.UUIDField(),
        "model_identifier": serializers.CharField(),
        "capability": serializers.CharField(),
        "state": serializers.ChoiceField(choices=(
            "ready", "smoke_test_required", "unavailable",
        )),
        "ready": serializers.BooleanField(),
        "reason_code": serializers.CharField(),
        "transport": serializers.ChoiceField(choices=("local_endpoint", "remote_api")),
        "data_egress": serializers.BooleanField(),
        "data_egress_notice": serializers.CharField(allow_blank=True),
        "estimated_cost": serializers.DecimalField(
            max_digits=14, decimal_places=6, allow_null=True,
        ),
        "cost_currency": serializers.CharField(allow_blank=True),
        "cost_basis": serializers.CharField(),
        "smoke_snapshot_id": serializers.UUIDField(allow_null=True),
        "smoke_expires_at": serializers.DateTimeField(allow_null=True),
    },
)


PromptNormalizationCapabilityResponse = inline_serializer(
    name="PromptNormalizationCapability",
    fields={
        "schema_version": serializers.IntegerField(min_value=2),
        "capability_key": serializers.CharField(),
        "review_required": serializers.BooleanField(),
        "default_model_deployment_id": serializers.UUIDField(allow_null=True),
        "configuration_public_sha256": serializers.RegexField(r"^[0-9a-f]{64}$"),
        "deployments": PromptNormalizationDeploymentResponse,
    },
)


class PromptNormalizationCapabilityView(APIView):
    """Project-scoped, deployment-backed prompt.normalize capability."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: PromptNormalizationCapabilityResponse,
            409: ProductionCapabilityErrorResponse,
        },
    )
    def get(self, request, project_id):
        projects = SeriesProject.objects.all()
        if not request.user.is_staff:
            projects = projects.filter(owner=request.user)
        project = get_object_or_404(projects, pk=project_id)
        try:
            return Response(prompt_normalization_capability(project=project))
        except ConfigurationResolutionError as exc:
            return Response({
                "code": exc.code,
                "detail": str(exc),
                "missing_keys": list(exc.missing_keys),
            }, status=status.HTTP_409_CONFLICT)


class ProductionCapabilityView(APIView):
    """Expose queue guidance separately from business episode/shot volume."""

    permission_classes = [IsAuthenticated]

    CONFIG_KEYS = (
        "production.recommended_max_pending",
        "production.recommended_max_active_per_user",
        "production.default_concurrency",
        "production.task_timeout_seconds",
        "runtime.task_idle_timeout_seconds",
        "production.video_shot_min_duration_ms",
        "production.video_shot_max_duration_ms",
        "production.default_shot_duration_ms",
        "production.default_first_frame_continuity",
        "production.default_last_frame_continuity",
        "production.script_auto_split_trigger_chars",
        "production.script_auto_split_target_chars",
    )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "project_id",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="可选；返回该项目覆盖后的有效生产策略。",
            ),
        ],
        responses={
            200: ProductionCapabilityResponse,
            409: ProductionCapabilityErrorResponse,
        },
    )
    def get(self, request):
        project = None
        project_id = str(request.query_params.get("project_id") or "").strip()
        if project_id:
            projects = SeriesProject.objects.all()
            if not request.user.is_staff:
                projects = projects.filter(owner=request.user)
            project = get_object_or_404(projects, pk=project_id)
        try:
            resolved = resolve_configuration(
                project=project,
                required_keys=self.CONFIG_KEYS,
            )
        except ConfigurationResolutionError as exc:
            return Response({
                "code": exc.code,
                "detail": str(exc),
                "missing_keys": list(exc.missing_keys),
            }, status=status.HTTP_409_CONFLICT)
        values = resolved.values
        recommended_pending = int(values["production.recommended_max_pending"])
        recommended_user = int(values["production.recommended_max_active_per_user"])
        configured_concurrency = int(values["production.default_concurrency"])
        deployment_concurrency = int(settings.TASK_MAX_CONCURRENT)
        configured_timeout = float(values["production.task_timeout_seconds"])
        idle_timeout = float(values["runtime.task_idle_timeout_seconds"])
        minimum_duration = int(values["production.video_shot_min_duration_ms"])
        maximum_duration = int(values["production.video_shot_max_duration_ms"])
        default_duration = int(values["production.default_shot_duration_ms"])
        split_trigger = int(values["production.script_auto_split_trigger_chars"])
        split_target = int(values["production.script_auto_split_target_chars"])
        if recommended_user > recommended_pending:
            return Response({
                "code": "production_config_invalid",
                "detail": "单用户活跃任务建议阈值不能超过总待处理建议阈值",
                "missing_keys": [],
            }, status=status.HTTP_409_CONFLICT)
        if not minimum_duration <= default_duration <= maximum_duration:
            return Response({
                "code": "production_config_invalid",
                "detail": "默认镜头时长必须位于配置的视频镜头时长范围内",
                "missing_keys": [],
            }, status=status.HTTP_409_CONFLICT)
        if split_target > split_trigger:
            return Response({
                "code": "production_config_invalid",
                "detail": "自动分段目标字符数不能超过触发字符数",
                "missing_keys": [],
            }, status=status.HTTP_409_CONFLICT)
        public_sources = {
            key: str((resolved.sources.get(key) or {}).get("source") or "")
            for key in self.CONFIG_KEYS
        }
        public_sha = hashlib.sha256(canonical_json_bytes({
            "values": {key: values[key] for key in self.CONFIG_KEYS},
            "sources": public_sources,
            "deployment_limits": {
                "max_concurrent": deployment_concurrency,
            },
        })).hexdigest()
        active_statuses = (
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        )
        active = GenerationTask.objects.filter(status__in=active_statuses)
        return Response({
            "recommended_max_pending": recommended_pending,
            "recommended_max_active_per_user": recommended_user,
            "max_concurrent": min(configured_concurrency, deployment_concurrency),
            "configured_concurrency": configured_concurrency,
            "deployment_max_concurrent": deployment_concurrency,
            "task_timeout_seconds": configured_timeout,
            "configured_task_timeout_seconds": configured_timeout,
            "task_idle_timeout_seconds": idle_timeout,
            "video_shot_min_duration_ms": minimum_duration,
            "video_shot_max_duration_ms": maximum_duration,
            "script_breakdown_default_shot_duration_ms": default_duration,
            "script_breakdown_default_first_frame_continuity": bool(
                values["production.default_first_frame_continuity"]
            ),
            "script_breakdown_default_last_frame_continuity": bool(
                values["production.default_last_frame_continuity"]
            ),
            "script_breakdown_auto_split_trigger_chars": split_trigger,
            "script_breakdown_auto_split_target_chars": split_target,
            "active_task_count": active.count(),
            "user_active_task_count": active.filter(owner=request.user).count(),
            "confirmation_required": True,
            "business_volume_limited": False,
            "configuration_public_sha256": public_sha,
            "configuration_sources": public_sources,
        })
