from __future__ import annotations

from drf_spectacular.extensions import OpenApiSerializerExtension
from rest_framework import serializers

from studio.models import DeferredGenerationRequest


class DeferredGenerationRequestCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    source_task_id = serializers.UUIDField()
    target_shot_id = serializers.UUIDField()
    frame_slot = serializers.ChoiceField(
        choices=DeferredGenerationRequest.FrameSlot.values,
    )
    idempotency_key = serializers.CharField(
        min_length=1,
        max_length=120,
        trim_whitespace=False,
    )
    priority = serializers.IntegerField(min_value=-100, max_value=100, default=0)
    max_attempts = serializers.IntegerField(min_value=1, max_value=10, default=3)
    extended_generation_confirmed = serializers.BooleanField(default=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("请求体必须是 JSON 对象。")
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({
                key: "不支持的字段；output_index、模板、模型、工作流和参考图均由服务端解析。"
                for key in unknown
            })
        return super().to_internal_value(data)

    def validate_idempotency_key(self, value: str) -> str:
        if value != value.strip() or any(char in value for char in "\r\n\t"):
            raise serializers.ValidationError("idempotency_key 首尾不能有空白，也不能包含控制字符。")
        return value


class DeferredGenerationCancelSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if data in (None, b"", ""):
            data = {}
        if not isinstance(data, dict):
            raise serializers.ValidationError("请求体必须为空 JSON 对象。")
        if data:
            raise serializers.ValidationError({
                key: "取消请求不接受任何字段。" for key in sorted(data)
            })
        return {}


class DeferredGenerationRequestListSerializer(serializers.ModelSerializer):
    project_id = serializers.UUIDField(source="project.id")
    source_task_id = serializers.UUIDField(source="source_task.id")
    target_shot_id = serializers.UUIDField(source="target_shot.id")
    target_template_id = serializers.UUIDField(source="target_template.id")
    derived_asset_id = serializers.UUIDField(source="derived_asset.id", allow_null=True)
    materialized_task_id = serializers.UUIDField(source="materialized_task.id", allow_null=True)
    materialized_prompt_artifact_id = serializers.SerializerMethodField()
    materialized_workflow_id = serializers.SerializerMethodField()
    cancel_allowed = serializers.SerializerMethodField()
    retry_allowed = serializers.SerializerMethodField()

    class Meta:
        model = DeferredGenerationRequest
        fields = (
            "id",
            "project_id",
            "source_task_id",
            "source_output_role",
            "target_shot_id",
            "frame_slot",
            "target_template_id",
            "target_template_hash",
            "target_workflow_overrides",
            "idempotency_key",
            "request_fingerprint",
            "target_input_fingerprint",
            "status",
            "resolved_source_output_index",
            "source_artifact_sha256",
            "source_manifest_sha256",
            "derived_asset_id",
            "materialized_task_id",
            "materialized_prompt_artifact_id",
            "materialized_workflow_id",
            "priority",
            "attempts",
            "max_attempts",
            "next_attempt_at",
            "extended_generation_confirmed",
            "cancel_allowed",
            "retry_allowed",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_materialized_prompt_artifact_id(self, obj) -> str | None:
        task = obj.materialized_task
        if not task or not task.generated_workflow_id:
            return None
        return str(task.generated_workflow.prompt_artifact_id)

    def get_materialized_workflow_id(self, obj) -> str | None:
        task = obj.materialized_task
        if not task:
            return None
        return str(task.generated_workflow_id)

    def get_cancel_allowed(self, obj) -> bool:
        return (
            obj.status in {
                DeferredGenerationRequest.Status.PREPARING_PROMPT,
                DeferredGenerationRequest.Status.WAITING_UPSTREAM,
            }
            and not obj.claim_token
        )

    def get_retry_allowed(self, obj) -> bool:
        return False


class DeferredGenerationRequestDetailSerializer(DeferredGenerationRequestListSerializer):
    class Meta(DeferredGenerationRequestListSerializer.Meta):
        fields = DeferredGenerationRequestListSerializer.Meta.fields + (
            "provisional_prompt_text",
            "provisional_negative_prompt",
            "provisional_prompt_sha256",
        )
        read_only_fields = fields


class DeferredGenerationErrorDetailsSerializer(serializers.Serializer):
    detail = serializers.CharField()
    idempotency_key = serializers.CharField(required=False)
    deferred_generation_id = serializers.UUIDField(required=False)


class DeferredGenerationErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = DeferredGenerationErrorDetailsSerializer()


class DeferredGenerationErrorResponseSerializer(serializers.Serializer):
    error = DeferredGenerationErrorSerializer()


class _ClosedDeferredGenerationObjectSchemaExtension(OpenApiSerializerExtension):
    def map_serializer(self, auto_schema, direction):
        schema = auto_schema._map_serializer(
            self.target,
            direction,
            bypass_extensions=True,
        )
        schema["additionalProperties"] = False
        return schema


class DeferredGenerationRequestCreateSchemaExtension(_ClosedDeferredGenerationObjectSchemaExtension):
    target_class = DeferredGenerationRequestCreateSerializer


class DeferredGenerationCancelSchemaExtension(_ClosedDeferredGenerationObjectSchemaExtension):
    target_class = DeferredGenerationCancelSerializer
