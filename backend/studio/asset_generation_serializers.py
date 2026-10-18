from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from studio.execution_instance_serializers import ExecutionAuthoringSerializer, ExecutionRequestSerializer, ExecutionSubjectFactSerializer
from studio.execution_preparation_serializers import ExecutionPreparationSerializer

from studio.models import (
    AssetGenerationJob,
    AssetGenerationJobResolution,
    ModelExecutionInstanceVersion,
    Style,
)
from studio.serializers import AssetSerializer
from studio.services.content_contracts import normalize_audit_note


class AssetGenerationResolutionSerializer(serializers.ModelSerializer):
    # Public resolution endpoints expose human decisions, never system events.
    outcome = serializers.ChoiceField(choices=AssetGenerationJobResolution.OPERATOR_CHOICES, read_only=True)
    decided_by = serializers.PrimaryKeyRelatedField(read_only=True)
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


class AssetGenerationRetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetGenerationJob
        fields = ("id", "status")
        read_only_fields = fields


class AssetGenerationJobSerializer(serializers.ModelSerializer):
    retries = AssetGenerationRetrySerializer(many=True, read_only=True)
    execution_instance_name = serializers.CharField(source="execution_instance_version.instance.name", read_only=True)
    execution_version = serializers.IntegerField(source="execution_instance_version.version", read_only=True)
    preparation = serializers.SerializerMethodField()
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
            "id", "retry_of", "retries", "job_type", "media_type", "image_category", "name",
            "description", "style", "execution_instance_version", "execution_instance_name", "execution_version", "preparation",
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

    @extend_schema_field(ExecutionPreparationSerializer)
    def get_preparation(self, obj):
        rows = list(obj.execution_preparations.all())
        if len(rows) != 1 or rows[0].execution_instance_version_id != obj.execution_instance_version_id:
            raise serializers.ValidationError("素材任务的执行准备关联缺失或不一致")
        return ExecutionPreparationSerializer(rows[0]).data

    @extend_schema_field(AssetSerializer(allow_null=True))
    def get_output_asset(self, obj) -> dict | None:
        if obj.output_asset_id is None:
            return None
        return AssetSerializer(obj.output_asset, context=self.context).data

    @extend_schema_field(AssetGenerationResolutionSerializer(allow_null=True))
    def get_latest_resolution(self, obj) -> dict | None:
        rows = [row for row in obj.resolutions.all()
                if row.outcome in AssetGenerationJobResolution.OPERATOR_OUTCOMES]
        if not rows:
            return None
        return AssetGenerationResolutionSerializer(rows[-1]).data


class AssetInitialFieldSerializer(ExecutionAuthoringSerializer):
    unit_key = serializers.CharField(max_length=191)
    field_key = serializers.CharField(max_length=100)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)


class AssetAppearanceInputSerializer(ExecutionAuthoringSerializer):
    input_key = serializers.CharField(max_length=191, trim_whitespace=False)
    item_key = serializers.CharField(max_length=191, trim_whitespace=False)


class AssetAppearanceDifferenceSerializer(ExecutionAuthoringSerializer):
    kind = serializers.ChoiceField(choices=("manual_channel", "logical_input", "reference_field", "structured_field"))
    key = serializers.CharField(max_length=191, trim_whitespace=False)
    field_key = serializers.CharField(max_length=191, required=False, trim_whitespace=False)
    unit_key = serializers.CharField(max_length=191, required=False, trim_whitespace=False)


class AssetAppearanceSerializer(ExecutionAuthoringSerializer):
    role = serializers.ChoiceField(choices=("primary", "variant"))
    setting_revision_id = serializers.UUIDField(allow_null=True)
    primary_version_id = serializers.UUIDField(allow_null=True)
    primary_input = AssetAppearanceInputSerializer(allow_null=True)
    difference_input = AssetAppearanceDifferenceSerializer(required=False)

    def validate(self, attrs):
        from studio.services.media_generation_appearances import normalize_media_appearance_request
        for key in ("setting_revision_id", "primary_version_id"):
            if attrs[key] is not None:
                attrs[key] = str(attrs[key])
        try:
            return normalize_media_appearance_request(attrs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict["appearance_target"]) from exc


class AssetGenerationJobCreateSerializer(ExecutionRequestSerializer):
    execution_instance_version_id = serializers.UUIDField()
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    job_type = serializers.ChoiceField(choices=("asset", "prompt"))
    media_type = serializers.ChoiceField(choices=("image", "video", "audio", "script"))
    inputs = serializers.DictField(help_text="按精确实例输入合同提交。媒体items来自asset或selected_output；配对音频也可明确使用video_audio与paired_to，引用本次父视频原音，不提交伪音频Asset或文件路径。")
    parameters = serializers.DictField()
    manual_channels = serializers.DictField(child=serializers.CharField(allow_blank=True, trim_whitespace=False))
    subject_facts = ExecutionSubjectFactSerializer(many=True)
    source_language = serializers.CharField(max_length=40, allow_blank=True)
    target_language = serializers.CharField(max_length=40, allow_blank=True)
    field_values = AssetInitialFieldSerializer(many=True)
    style_id = serializers.PrimaryKeyRelatedField(source="style", queryset=Style.objects.all(), required=False, allow_null=True)
    image_category = serializers.ChoiceField(choices=("character", "scene", "prop", "reference"), allow_blank=True, default="")
    name = serializers.CharField(allow_blank=True, max_length=160, default="", trim_whitespace=False)
    description = serializers.CharField(allow_blank=True, default="", trim_whitespace=False)
    allow_external_calls = serializers.BooleanField(default=False)
    appearance_target = AssetAppearanceSerializer(required=False, allow_null=True,
        help_text="可选主图/变体声明；设定可空。变体必须明确确切主图版本、原图片输入位置和原差异来源；准备只核对并冻结，不自动注入。生成成功后按声明登记素材系列版本，不代表审核采用。")

    def validate(self, attrs):
        if attrs.get("appearance_target") is not None and (attrs["job_type"] != "asset" or attrs["media_type"] != "image"):
            raise serializers.ValidationError({"appearance_target": "只有图片素材任务可声明外观目标"})
        return attrs

    def validate_style_id(self, value):
        if value and not value.is_active:
            raise serializers.ValidationError("此风格已停用，不能用于新生成；已有项目和素材引用会保留。")
        return value


class AssetGenerationResolutionInputSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=AssetGenerationJobResolution.OPERATOR_CHOICES,
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
    job_type = serializers.ChoiceField(choices=("asset", "prompt"))
    media_type = serializers.ChoiceField(choices=("image", "video", "audio", "script"))
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def to_internal_value(self, data):
        unknown = set(data) - self.fields.keys()
        if unknown:
            raise serializers.ValidationError({"query": "包含未声明的选项查询参数"})
        return super().to_internal_value(data)


class AssetGenerationOptionSerializer(serializers.ModelSerializer):
    execution_instance_version_id = serializers.UUIDField(source="pk", read_only=True)
    key = serializers.CharField(source="instance.key", read_only=True)
    name = serializers.CharField(source="instance.name", read_only=True)
    definition_sha256 = serializers.CharField(source="contract.sha256", read_only=True)

    class Meta:
        model = ModelExecutionInstanceVersion
        fields = ("execution_instance_version_id", "key", "name", "version", "operation", "mode", "prompt_mode", "definition_sha256")
        read_only_fields = fields


class AssetGenerationOptionsResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = AssetGenerationOptionSerializer(many=True)


class AssetAppearanceOptionsQuerySerializer(AssetGenerationOptionsQuerySerializer):
    job_type = None
    media_type = None
    role = serializers.ChoiceField(choices=("primary", "variant"))
    search = serializers.CharField(max_length=160, required=False, allow_blank=True)


class AssetAppearanceOptionSerializer(serializers.Serializer):
    setting_revision_id = serializers.UUIDField(source="id")
    appearance_id = serializers.UUIDField(source="variant_id")
    project_id = serializers.UUIDField(source="variant__project_id")
    project_title = serializers.CharField(source="variant__project__title")
    entity_name = serializers.CharField()
    name = serializers.CharField()
    reference_name = serializers.CharField()
    revision = serializers.IntegerField()


class AssetAppearanceOptionsResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = AssetAppearanceOptionSerializer(many=True)
