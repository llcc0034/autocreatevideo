import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView

from studio.models import (
    GenerationTask,
    SeriesProject,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    project_creation_policy,
    resolve_configuration,
)
from studio.services.json_contracts import canonical_json_bytes
from studio.services.execution_catalog_readiness import configured_video_conditioning_modes
from studio.services.production_pipeline import queue_capacity_advisories
from studio.services.security_boundaries import boundary_resolution


QueueCapacityAdvisoryResponse = inline_serializer(
    name="QueueCapacityAdvisory",
    fields={
        "schema_version": serializers.IntegerField(min_value=1),
        "code": serializers.ChoiceField(choices=(
            "recommended_max_pending_exceeded",
            "recommended_max_active_per_user_exceeded",
        )),
        "severity": serializers.ChoiceField(choices=("warning",)),
        "scope": serializers.ChoiceField(choices=("platform", "user")),
        "measured": serializers.IntegerField(min_value=0),
        "recommended_maximum": serializers.IntegerField(min_value=1),
    },
)


ProductionEffectiveLimitsResponse = inline_serializer(
    name="ProductionEffectiveLimits",
    fields={
        "max_variants": serializers.IntegerField(min_value=1),
        "max_routing_routes": serializers.IntegerField(min_value=1),
        "max_episodes": serializers.IntegerField(min_value=1),
        "max_shots_per_episode": serializers.IntegerField(min_value=1),
        "max_total_shots": serializers.IntegerField(min_value=1),
        "max_generation_tasks": serializers.IntegerField(min_value=1),
        "max_reference_bindings": serializers.IntegerField(min_value=1),
        "max_bible_candidates_per_entity": serializers.IntegerField(min_value=1),
        "max_bible_reference_assets_per_entity": serializers.IntegerField(
            min_value=1,
        ),
        "max_bible_regeneration_entities_per_request": serializers.IntegerField(
            min_value=1,
        ),
        "dialogue_max_entries": serializers.IntegerField(min_value=1),
        "dialogue_speaker_id_max_characters": serializers.IntegerField(
            min_value=1,
        ),
        "dialogue_text_max_characters": serializers.IntegerField(min_value=1),
        "dialogue_delivery_max_characters": serializers.IntegerField(
            min_value=1,
        ),
        "execution_monitoring_max_node_type_overrides": serializers.IntegerField(
            min_value=1,
        ),
        "execution_monitoring_max_node_overrides": serializers.IntegerField(
            min_value=1,
        ),
        "execution_monitoring_max_idle_timeout_seconds": serializers.IntegerField(
            min_value=1,
        ),
        "audit_note_min_characters": serializers.IntegerField(min_value=1),
        "audit_note_max_characters": serializers.IntegerField(min_value=1),
    },
)


ProductionCreationDefaultsResponse = inline_serializer(
    name="ProductionCreationDefaults",
    fields={
        "episode_count": serializers.IntegerField(min_value=1),
        "shots_per_episode": serializers.IntegerField(min_value=1),
        "episode_duration_seconds": serializers.IntegerField(min_value=1),
        "fps": serializers.IntegerField(min_value=1),
        "width": serializers.IntegerField(min_value=1),
        "height": serializers.IntegerField(min_value=1),
        "default_language": serializers.CharField(),
        "default_aspect_ratio": serializers.CharField(),
        "aspect_ratio_options": serializers.ListField(
            child=serializers.DictField(),
        ),
        "default_shot_media_type": serializers.ChoiceField(
            choices=("video", "image"),
        ),
        "default_include_audio": serializers.BooleanField(),
    },
)


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
        "active_task_count": serializers.IntegerField(min_value=0),
        "user_active_task_count": serializers.IntegerField(min_value=0),
        "queue_guidance_enforcement": serializers.ChoiceField(choices=("advisory",)),
        "queue_advisories": serializers.ListField(
            child=QueueCapacityAdvisoryResponse,
        ),
        "business_volume_limited": serializers.BooleanField(),
        "configuration_public_sha256": serializers.RegexField(r"^[0-9a-f]{64}$"),
        "configuration_sources": serializers.DictField(
            child=serializers.CharField(),
        ),
        "effective_limits": ProductionEffectiveLimitsResponse,
        "creation_defaults": ProductionCreationDefaultsResponse,
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


VideoConditioningCapabilityResponse = inline_serializer(
    name="VideoConditioningCapability",
    fields={
        "conditioning_modes": serializers.ListField(child=serializers.DictField()),
        "conditioning_contract_sha256": serializers.RegexField(r"^[0-9a-f]{64}$"),
    },
)


class VideoConditioningCapabilityView(APIView):
    """Strict actor-visible video catalog; not a dependency of production settings."""

    permission_classes = [IsAuthenticated]

    @staticmethod
    def _conditioning_capability(actor) -> tuple[list[dict], str]:
        modes = configured_video_conditioning_modes(actor)
        digest = hashlib.sha256(canonical_json_bytes({"schema_version": 1, "modes": modes})).hexdigest()
        return modes, digest

    @extend_schema(responses={200: VideoConditioningCapabilityResponse, 409: ProductionCapabilityErrorResponse})
    def get(self, request):
        try:
            modes, digest = self._conditioning_capability(request.user)
        except ValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {"conditioning_contract": exc.messages}
            return Response({"code": "production_conditioning_contract_invalid", "detail": str(detail),
                "missing_keys": []}, status=status.HTTP_409_CONFLICT)
        return Response({"conditioning_modes": modes, "conditioning_contract_sha256": digest})




def creation_defaults_from_policy(policy):
    """One projection shared by creation and full production capabilities."""
    return {
        **{key: policy[f"default_{key}"] for key in (
            "episode_count", "shots_per_episode", "episode_duration_seconds",
        )},
        **{key: policy[key] for key in (
            "fps", "width", "height", "default_language", "default_aspect_ratio",
            "aspect_ratio_options", "default_shot_media_type", "default_include_audio",
        )},
    }


class ProjectCreationCapabilityView(APIView):
    """Creation policy only; no execution catalog, task history or model calls."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={
        200: inline_serializer(name="ProjectCreationCapability", fields={
            "creation_defaults": ProductionCreationDefaultsResponse,
            "configuration_public_sha256": serializers.RegexField(r"^[0-9a-f]{64}$"),
        }),
        409: ProductionCapabilityErrorResponse,
    })
    def get(self, request):
        try:
            defaults = creation_defaults_from_policy(project_creation_policy())
        except ConfigurationResolutionError as exc:
            return Response({
                "code": exc.code, "detail": str(exc),
                "missing_keys": list(exc.missing_keys),
            }, status=status.HTTP_409_CONFLICT)
        return Response({
            "creation_defaults": defaults,
            "configuration_public_sha256": hashlib.sha256(
                canonical_json_bytes(defaults),
            ).hexdigest(),
        })


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
        "production.project_creation_policy",
        "production.default_resolution_width",
        "production.default_resolution_height",
        "production.default_include_audio",
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
            security_resolution = boundary_resolution((
                "security.production.max_variants",
                "security.production.max_routing_routes",
                "security.production.max_episodes",
                "security.production.max_shots_per_episode",
                "security.production.max_total_shots",
                "security.production.max_generation_tasks",
                "security.reference.max_bindings",
                "security.production.max_bible_candidates_per_entity",
                "security.production.max_bible_reference_assets_per_entity",
                "security.production.max_bible_regeneration_entities_per_request",
                "security.dialogue.max_entries",
                "security.dialogue.speaker_id_max_characters",
                "security.dialogue.text_max_characters",
                "security.dialogue.delivery_max_characters",
                "security.execution.max_type_overrides",
                "security.execution.max_node_overrides",
                "security.execution.max_idle_timeout_seconds",
                "security.prompt_normalization.batch_max_items",
                "security.prompt_normalization.advisory_max_items",
                "security.prompt_normalization.request_max_bytes",
                "security.prompt_normalization.text_max_characters",
                "security.prompt.max_usage_count",
                "security.audit_note.minimum_characters",
                "security.audit_note.maximum_characters",
            ))
            security_limits = security_resolution.values
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
        if not minimum_duration <= default_duration <= maximum_duration:
            return Response({
                "code": "production_config_invalid",
                "detail": "默认镜头时长必须位于配置的视频镜头时长范围内",
                "missing_keys": [],
            }, status=status.HTTP_409_CONFLICT)
        public_sources = {
            key: str((resolved.sources.get(key) or {}).get("source") or "")
            for key in self.CONFIG_KEYS
        }
        public_sources.update({
            key: str((security_resolution.sources.get(key) or {}).get("source") or "")
            for key in security_limits
        })
        effective_limits = {
            "max_variants": int(
                security_limits["security.production.max_variants"]
            ),
            "max_routing_routes": int(
                security_limits["security.production.max_routing_routes"]
            ),
            "max_episodes": int(
                security_limits["security.production.max_episodes"]
            ),
            "max_shots_per_episode": int(
                security_limits["security.production.max_shots_per_episode"]
            ),
            "max_total_shots": int(
                security_limits["security.production.max_total_shots"]
            ),
            "max_generation_tasks": int(
                security_limits["security.production.max_generation_tasks"]
            ),
            "max_reference_bindings": int(
                security_limits["security.reference.max_bindings"]
            ),
            "max_bible_candidates_per_entity": int(
                security_limits[
                    "security.production.max_bible_candidates_per_entity"
                ]
            ),
            "max_bible_reference_assets_per_entity": int(
                security_limits[
                    "security.production.max_bible_reference_assets_per_entity"
                ]
            ),
            "max_bible_regeneration_entities_per_request": int(
                security_limits[
                    "security.production.max_bible_regeneration_entities_per_request"
                ]
            ),
            "dialogue_max_entries": int(
                security_limits["security.dialogue.max_entries"]
            ),
            "dialogue_speaker_id_max_characters": int(
                security_limits[
                    "security.dialogue.speaker_id_max_characters"
                ]
            ),
            "dialogue_text_max_characters": int(
                security_limits["security.dialogue.text_max_characters"]
            ),
            "dialogue_delivery_max_characters": int(
                security_limits[
                    "security.dialogue.delivery_max_characters"
                ]
            ),
            "execution_monitoring_max_node_type_overrides": int(
                security_limits["security.execution.max_type_overrides"]
            ),
            "execution_monitoring_max_node_overrides": int(
                security_limits["security.execution.max_node_overrides"]
            ),
            "execution_monitoring_max_idle_timeout_seconds": int(
                security_limits[
                    "security.execution.max_idle_timeout_seconds"
                ]
            ),
            "audit_note_min_characters": int(
                security_limits["security.audit_note.minimum_characters"]
            ),
            "audit_note_max_characters": int(
                security_limits["security.audit_note.maximum_characters"]
            ),
        }
        if effective_limits["audit_note_min_characters"] > effective_limits[
            "audit_note_max_characters"
        ]:
            return Response({
                "code": "production_config_invalid",
                "detail": "审计说明的有效最小字符数不能超过最大字符数",
                "missing_keys": [],
            }, status=status.HTTP_409_CONFLICT)
        try:
            creation_policy = project_creation_policy(values=values)
            creation_defaults = creation_defaults_from_policy(creation_policy)
        except ConfigurationResolutionError as exc:
            return Response({
                "code": exc.code,
                "detail": str(exc),
                "missing_keys": list(exc.missing_keys),
            }, status=status.HTTP_409_CONFLICT)
        public_sha = hashlib.sha256(canonical_json_bytes({
            "values": {key: values[key] for key in self.CONFIG_KEYS},
            "sources": public_sources,
            "deployment_limits": {
                "max_concurrent": deployment_concurrency,
            },
            "effective_limits": effective_limits,
            "creation_defaults": creation_defaults,
        })).hexdigest()
        active_statuses = (
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        )
        active = GenerationTask.objects.filter(status__in=active_statuses)
        active_count = active.count()
        user_active_count = active.filter(owner=request.user).count()
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
            "active_task_count": active_count,
            "user_active_task_count": user_active_count,
            "queue_guidance_enforcement": "advisory",
            "queue_advisories": queue_capacity_advisories(
                active_task_count=active_count,
                user_active_task_count=user_active_count,
                requested_task_count=0,
                recommended_max_pending=recommended_pending,
                recommended_max_active_per_user=recommended_user,
            ),
            "business_volume_limited": False,
            "configuration_public_sha256": public_sha,
            "configuration_sources": public_sources,
            "effective_limits": effective_limits,
            "creation_defaults": creation_defaults,
        })
