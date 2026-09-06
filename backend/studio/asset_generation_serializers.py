from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from studio.models import (
    AssetGenerationJob,
    AssetGenerationJobResolution,
    ModelDeployment,
    Style,
    WorkflowTemplateVersion,
)
from studio.serializers import AssetSerializer
from studio.platform_serializers import WorkflowExecutionInputSerializer, WorkflowExecutionInputSchemaSerializer
from studio.services.media_request_limits import media_prompt_absolute_max_chars
from studio.services.content_contracts import normalize_audit_note


@extend_schema_field(WorkflowExecutionInputSerializer)
class WorkflowExecutionInputJSONField(serializers.JSONField):
    """Document the shared input shape while preserving service-side strict validation."""


class AssetGenerationResolutionSerializer(serializers.ModelSerializer):
    job_status = serializers.CharField(source="job.status", read_only=True)

    class Meta:
        model = AssetGenerationJobResolution
        fields = (
            "id", "job", "job_status", "revision", "outcome",
            "request_sha256", "provider_request_id_sha256", "evidence",
            "evidence_schema_version", "evidence_size_bytes", "evidence_sha256",
            "note", "decided_by", "created_at",
        )
        read_only_fields = fields


class AssetGenerationJobSerializer(serializers.ModelSerializer):
    model_deployment_name = serializers.CharField(
        source="model_deployment.name",
        read_only=True,
    )
    output_asset = serializers.SerializerMethodField()
    latest_resolution = serializers.SerializerMethodField()
    workflow_name = serializers.CharField(
        source="workflow_template_version.template.name",
        read_only=True,
        allow_null=True,
    )
    workflow_version = serializers.IntegerField(
        source="workflow_template_version.version",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = AssetGenerationJob
        fields = (
            "id", "job_type", "media_type", "image_category", "name",
            "description", "prompt", "negative_prompt", "parameters", "style",
            "execution_kind", "model_deployment", "model_deployment_name",
            "workflow_template_version", "workflow_name", "workflow_version",
            "output_asset", "status",
            "operation", "is_external", "allow_external_calls", "budget_limit",
            "estimated_cost", "actual_cost", "cost_currency", "result_text",
            "result_text_size_bytes", "result_text_sha256", "remote_status",
            "provider_request_id_sha256", "poll_count", "error_code", "error_message",
            "ignore_late_result", "submission_started_at", "cancel_requested_at",
            "cancel_submitted_at", "started_at", "submitted_at", "finished_at",
            "latest_resolution", "created_at", "updated_at",
        )
        read_only_fields = fields

    @extend_schema_field(AssetSerializer(allow_null=True))
    def get_output_asset(self, obj) -> dict | None:
        if obj.output_asset_id is None:
            return None
        return AssetSerializer(obj.output_asset, context=self.context).data

    @extend_schema_field(AssetGenerationResolutionSerializer(allow_null=True))
    def get_latest_resolution(self, obj) -> dict | None:
        rows = list(obj.resolutions.all())
        if not rows:
            return None
        return AssetGenerationResolutionSerializer(rows[-1]).data


class AssetGenerationJobCreateSerializer(serializers.Serializer):
    job_type = serializers.ChoiceField(
        choices=AssetGenerationJob.JobType.choices,
        default=AssetGenerationJob.JobType.ASSET,
    )
    media_type = serializers.ChoiceField(choices=AssetGenerationJob.MediaType.choices)
    execution_kind = serializers.ChoiceField(
        choices=AssetGenerationJob.ExecutionKind.choices,
        default=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
    )
    model_deployment_id = serializers.PrimaryKeyRelatedField(
        source="model_deployment",
        queryset=ModelDeployment.objects.select_related("profile_version__provider"),
        required=False,
    )
    workflow_template_version_id = serializers.PrimaryKeyRelatedField(
        source="workflow_template_version",
        queryset=WorkflowTemplateVersion.objects.select_related(
            "template", "validated_deployment__profile_version__provider",
        ),
        required=False,
    )
    style_id = serializers.PrimaryKeyRelatedField(
        source="style",
        queryset=Style.objects.all(),
        required=False,
        allow_null=True,
    )
    image_category = serializers.ChoiceField(
        choices=("character", "scene", "prop", "reference"),
        required=False,
        allow_blank=True,
        default="",
    )
    name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    description = serializers.CharField(required=False, allow_blank=True)
    prompt = serializers.CharField()
    negative_prompt = serializers.CharField(
        required=False,
        allow_blank=True,
    )
    parameters = serializers.JSONField(required=False, default=dict)
    workflow_inputs = WorkflowExecutionInputJSONField(required=False, default=dict)
    allow_external_calls = serializers.BooleanField(required=False, default=False)
    budget_limit = serializers.DecimalField(
        max_digits=14,
        decimal_places=6,
        required=False,
        allow_null=True,
        min_value=0,
    )

    def validate_parameters(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("必须是对象")
        return value

    def validate_workflow_inputs(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("必须是对象")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        prompt_limit = media_prompt_absolute_max_chars()
        if len(attrs.get("prompt") or "") > prompt_limit:
            raise serializers.ValidationError({
                "prompt": f"不能超过当前安全边界 {prompt_limit} 字符",
            })
        if len(attrs.get("negative_prompt") or "") > prompt_limit:
            raise serializers.ValidationError({
                "negative_prompt": f"不能超过当前安全边界 {prompt_limit} 字符",
            })
        execution_kind = attrs.get(
            "execution_kind",
            AssetGenerationJob.ExecutionKind.DEPLOYMENT,
        )
        deployment = attrs.get("model_deployment")
        workflow = attrs.get("workflow_template_version")
        if execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT:
            if deployment is None or workflow is not None:
                raise serializers.ValidationError(
                    "deployment 方案必须且只能提供 model_deployment_id",
                )
            if attrs.get("workflow_inputs"):
                raise serializers.ValidationError({
                    "workflow_inputs": "模型部署方案不接受工作流输入",
                })
        elif workflow is None or deployment is not None:
            raise serializers.ValidationError(
                "comfy_workflow 方案必须且只能提供 workflow_template_version_id",
            )
        elif attrs.get("parameters"):
            raise serializers.ValidationError({
                "parameters": "ComfyUI 工作流参数统一放入 workflow_inputs.values",
            })
        return attrs


class AssetGenerationResolutionInputSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=AssetGenerationJobResolution.Outcome.choices,
    )
    note = serializers.CharField(trim_whitespace=True)
    provider_request_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=191,
        default="",
        write_only=True,
    )
    actual_cost = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=14,
        decimal_places=6,
        min_value=0,
    )
    cost_currency = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=12,
        default="",
    )

    def validate_note(self, value):
        try:
            return normalize_audit_note(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict["note"]) from exc


class AssetGenerationOptionsQuerySerializer(serializers.Serializer):
    job_type = serializers.ChoiceField(
        choices=AssetGenerationJob.JobType.choices,
        default=AssetGenerationJob.JobType.ASSET,
    )
    media_type = serializers.ChoiceField(choices=AssetGenerationJob.MediaType.choices)




class AssetGenerationRouteOptionSerializer(serializers.Serializer):
    target_kind = serializers.ChoiceField(choices=AssetGenerationJob.ExecutionKind.choices)
    target_id = serializers.UUIDField()
    key = serializers.CharField()
    name = serializers.CharField()
    model_deployment_id = serializers.UUIDField()
    model_deployment_name = serializers.CharField()
    model_identifier = serializers.CharField()
    operation = serializers.CharField()
    is_external = serializers.BooleanField()
    smoke_evidence_id = serializers.UUIDField()
    smoke_expires_at = serializers.DateTimeField(allow_null=True)
    request_schema = serializers.JSONField(allow_null=True)
    parameter_defaults = serializers.JSONField()
    workflow_input_schema = WorkflowExecutionInputSchemaSerializer(allow_null=True)
    accepts_negative_prompt = serializers.BooleanField()
    workflow_version_id = serializers.UUIDField(allow_null=True)
    workflow_name = serializers.CharField(allow_blank=True)
    workflow_version = serializers.IntegerField(allow_null=True)

class AssetGenerationOptionsResponseSerializer(serializers.Serializer):
    job_type = serializers.CharField()
    media_type = serializers.CharField()
    routes = AssetGenerationRouteOptionSerializer(many=True)
