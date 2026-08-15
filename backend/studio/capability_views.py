from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.services.h3_prompt_optimizer import get_h3_prompt_optimizer_capability
from studio.services.shot_detail_drafts import shot_detail_capability
from studio.services.prompt_translation import get_translation_capability
from studio.models import GenerationTask


H3PromptOptimizerCapabilityResponse = inline_serializer(
    name="H3PromptOptimizerCapability",
    fields={
        "enabled": serializers.BooleanField(),
        "configured": serializers.BooleanField(),
        "transport": serializers.ChoiceField(
            choices=("local", "remote", "unconfigured"),
        ),
        "model": serializers.CharField(allow_blank=True),
        "failure_mode": serializers.ChoiceField(choices=("warn", "block")),
        "language_gate": serializers.ChoiceField(choices=("warn", "block")),
    },
)


CreativeDraftProviderCapabilities = inline_serializer(
    name="CreativeDraftProviderCapability",
    many=True,
    fields={
        "id": serializers.ChoiceField(choices=("local_qwen", "deepseek_api")),
        "provider": serializers.ChoiceField(choices=("local_qwen", "deepseek_api")),
        "label": serializers.CharField(),
        "creative_generation_supported": serializers.BooleanField(),
        "enabled": serializers.BooleanField(),
        "configured": serializers.BooleanField(),
        "ready": serializers.BooleanField(),
        "temporarily_busy": serializers.BooleanField(required=False),
        "model": serializers.CharField(allow_blank=True),
        "transport": serializers.ChoiceField(choices=("local_process", "remote_api")),
        "data_egress": serializers.BooleanField(),
        "data_egress_notice": serializers.CharField(required=False, allow_blank=True),
        "notice": serializers.CharField(required=False, allow_blank=True),
        "model_downloaded": serializers.BooleanField(required=False),
        "model_verified": serializers.BooleanField(required=False),
        "device": serializers.CharField(required=False, allow_blank=True),
        "gpu_layers": serializers.IntegerField(required=False),
        "cuda_requested": serializers.BooleanField(required=False),
        "gpu_offload_supported": serializers.BooleanField(required=False),
        "gpu_context_smoke_passed": serializers.BooleanField(required=False),
        "gpu_ready": serializers.BooleanField(required=False),
        "runtime_ready": serializers.BooleanField(required=False),
        "runtime_version": serializers.CharField(required=False, allow_blank=True),
        "runtime_probe_error_code": serializers.CharField(required=False, allow_blank=True),
        "min_shots": serializers.IntegerField(min_value=1),
        "recommended_max_shots": serializers.IntegerField(min_value=1),
        "batch_shots": serializers.IntegerField(min_value=1),
        "max_dialogue_per_shot": serializers.IntegerField(min_value=1),
        "max_episode_title_chars": serializers.IntegerField(min_value=1),
        "max_dialogue_text_chars": serializers.IntegerField(min_value=1),
        "max_dialogue_delivery_chars": serializers.IntegerField(min_value=1),
        "max_dialogue_delivery_en_chars": serializers.IntegerField(min_value=1),
        "recommended_title_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_title_en_chars": serializers.IntegerField(min_value=1),
        "recommended_narrative_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_narrative_en_chars": serializers.IntegerField(min_value=1),
        "recommended_camera_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_camera_en_chars": serializers.IntegerField(min_value=1),
        "recommended_action_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_action_en_chars": serializers.IntegerField(min_value=1),
        "recommended_continuity_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_continuity_en_chars": serializers.IntegerField(min_value=1),
        "recommended_soundscape_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_soundscape_en_chars": serializers.IntegerField(min_value=1),
        "recommended_music_zh_chars": serializers.IntegerField(min_value=1),
        "recommended_music_en_chars": serializers.IntegerField(min_value=1),
        "max_camera_movements": serializers.IntegerField(min_value=1),
        "max_action_sequence_markers": serializers.IntegerField(min_value=1),
        "min_script_action_coverage_percent": serializers.IntegerField(
            min_value=1, max_value=100,
        ),
        "min_shot_duration_ms": serializers.IntegerField(min_value=1),
        "max_shot_duration_ms": serializers.IntegerField(min_value=1),
        "output_min_tokens": serializers.IntegerField(min_value=1),
        "output_base_tokens": serializers.IntegerField(min_value=1),
        "output_tokens_per_shot": serializers.IntegerField(min_value=1),
        "output_tokens_per_dialogue": serializers.IntegerField(min_value=1),
        "output_dialogue_char_threshold": serializers.IntegerField(min_value=0),
        "output_headroom_per_shot": serializers.IntegerField(min_value=0),
        "max_source_chars": serializers.IntegerField(min_value=1),
        "max_regeneration_context_chars": serializers.IntegerField(min_value=1),
        "max_input_chars": serializers.IntegerField(min_value=1),
        "baseline_max_new_tokens": serializers.IntegerField(min_value=1),
        "max_new_tokens": serializers.IntegerField(min_value=1),
        "max_output_tokens": serializers.IntegerField(min_value=1),
        "configured_max_new_tokens": serializers.IntegerField(min_value=1),
        "context_size": serializers.IntegerField(min_value=1, required=False),
        "min_context_size": serializers.IntegerField(min_value=1, required=False),
        "max_context_size": serializers.IntegerField(min_value=1, required=False),
        "reserved_input_tokens": serializers.IntegerField(min_value=1, required=False),
        "runtime_timeout_seconds": serializers.FloatField(min_value=0, required=False),
        "process_timeout_seconds": serializers.FloatField(min_value=0, required=False),
        "loaded_on_demand": serializers.BooleanField(required=False),
        "unloaded_after_request": serializers.BooleanField(required=False),
        "review_required": serializers.BooleanField(),
        "translation_review_reused": serializers.BooleanField(),
    },
)


CreativeDraftCapabilityResponse = inline_serializer(
    name="CreativeDraftCapability",
    fields={
        "creative_generation_supported": serializers.BooleanField(),
        "review_required": serializers.BooleanField(),
        "default_provider": serializers.ChoiceField(
            choices=("local_qwen", "deepseek_api"),
        ),
        "providers": CreativeDraftProviderCapabilities,
        "shot_high_detail_supported": serializers.BooleanField(),
        "shot_high_detail_schema_version": serializers.IntegerField(min_value=1),
        "shot_high_detail_policies": serializers.DictField(
            child=serializers.DictField(),
        ),
        "shot_high_detail_shared_resource_busy": serializers.BooleanField(),
        "shot_high_detail_review_required": serializers.BooleanField(),
    },
)


ProductionCapabilityResponse = inline_serializer(
    name="ProductionCapability",
    fields={
        "recommended_max_pending": serializers.IntegerField(min_value=1),
        "recommended_max_active_per_user": serializers.IntegerField(min_value=1),
        "max_concurrent": serializers.IntegerField(min_value=1),
        "task_timeout_seconds": serializers.FloatField(min_value=0),
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
    },
)


class H3PromptOptimizerCapabilityView(APIView):
    """Expose only the authenticated user's safe server capability snapshot."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=H3PromptOptimizerCapabilityResponse)
    def get(self, request):
        return Response(get_h3_prompt_optimizer_capability())


class PromptTranslationCapabilityView(APIView):
    """Safe snapshot of the disposable local translation runtime."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(get_translation_capability())


class CreativeDraftCapabilityView(APIView):
    """Safe snapshot of the disposable local structured-creation runtime."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=CreativeDraftCapabilityResponse)
    def get(self, request):
        return Response(shot_detail_capability())


class ProductionCapabilityView(APIView):
    """Expose queue guidance separately from business episode/shot volume."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ProductionCapabilityResponse)
    def get(self, request):
        active_statuses = (
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        )
        active = GenerationTask.objects.filter(status__in=active_statuses)
        return Response({
            "recommended_max_pending": settings.TASK_RECOMMENDED_MAX_PENDING,
            "recommended_max_active_per_user": (
                settings.TASK_RECOMMENDED_MAX_ACTIVE_PER_USER
            ),
            "max_concurrent": settings.TASK_MAX_CONCURRENT,
            "task_timeout_seconds": settings.TASK_GENERATION_TIMEOUT_SECONDS,
            "video_shot_min_duration_ms": settings.VIDEO_SHOT_MIN_DURATION_MS,
            "video_shot_max_duration_ms": settings.VIDEO_SHOT_MAX_DURATION_MS,
            "script_breakdown_default_shot_duration_ms": (
                settings.SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS
            ),
            "script_breakdown_default_first_frame_continuity": (
                settings.SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY
            ),
            "script_breakdown_default_last_frame_continuity": (
                settings.SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY
            ),
            "script_breakdown_auto_split_trigger_chars": (
                settings.SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS
            ),
            "script_breakdown_auto_split_target_chars": (
                settings.SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS
            ),
            "active_task_count": active.count(),
            "user_active_task_count": active.filter(owner=request.user).count(),
            "confirmation_required": True,
            "business_volume_limited": False,
        })
