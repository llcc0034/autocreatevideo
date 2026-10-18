"""Closed public field actions; origins and approvals are server-owned."""
from django.core.validators import ProhibitNullCharactersValidator
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.models import ExecutionFieldSelection, ExecutionFieldValue, ExecutionPreparation, GenerationAttempt


class ExecutionFieldCreateSerializer(ExecutionAuthoringSerializer):
    preparation_id = serializers.UUIDField()
    unit_key = serializers.CharField(max_length=191)
    field_key = serializers.CharField(max_length=100)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    revision = serializers.IntegerField(min_value=1)
    expected_preparation_revision = serializers.IntegerField(min_value=1)


class ExecutionPreparationContinueSerializer(ExecutionAuthoringSerializer):
    expected_preparation_revision = serializers.IntegerField(min_value=1)


class ExecutionResourceConfirmSerializer(ExecutionPreparationContinueSerializer):
    fingerprint = serializers.RegexField(r"^[a-f0-9]{64}$")


class ExecutionMediaArchiveRecoverSerializer(ExecutionPreparationContinueSerializer):
    task_id = serializers.UUIDField()
    attempt_id = serializers.UUIDField()
    task_updated_at = serializers.DateTimeField()
    receipt_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class ExecutionMediaArchiveRecoverySerializer(serializers.Serializer):
    task_id = serializers.UUIDField()
    attempt_id = serializers.UUIDField()
    task_updated_at = serializers.DateTimeField()
    receipt_sha256 = serializers.CharField(allow_blank=True)
    can_recover = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


class ExecutionMediaUncertainRetryRequestSerializer(ExecutionPreparationContinueSerializer):
    task_id = serializers.UUIDField()
    attempt_id = serializers.UUIDField()
    task_updated_at = serializers.DateTimeField()
    request_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    acknowledged = serializers.BooleanField()


class ExecutionMediaUncertainRetrySerializer(serializers.Serializer):
    task_id = serializers.UUIDField()
    attempt_id = serializers.UUIDField()
    task_updated_at = serializers.DateTimeField()
    request_sha256 = serializers.CharField()
    can_retry = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


class ExecutionTextResponseCompleteSerializer(ExecutionPreparationContinueSerializer):
    review_id = serializers.UUIDField()
    original_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    text = serializers.CharField(trim_whitespace=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # DRF installs this Django validator directly; CharField error_messages
        # does not control its translated message. Keep the same validation/code.
        for validator in self.fields["text"].validators:
            if isinstance(validator, ProhibitNullCharactersValidator):
                validator.message = "正文包含空字符（NUL）；请移除该控制字符后再保存人工修订。"

    def validate_text(self, value):
        from studio.services.security_boundaries import boundary_int
        if len(value.encode("utf-8")) > boundary_int("security.snapshot.text_max_bytes"):
            raise serializers.ValidationError("人工补全内容超过当前文本存储安全边界")
        return value


class ExecutionFieldBatchRetrySerializer(ExecutionPreparationContinueSerializer):
    field_batch_key = serializers.RegexField(r"^[a-f0-9]{64}$")
    attempt_number = serializers.IntegerField(min_value=2)
    acknowledged_uncertain_task_id = serializers.UUIDField(required=False,
        help_text="当批次要求风险确认时，明确接受原调用结果或费用仍未知、可能重复计费；保留原记录及预留，新增同批次尝试。必须为当前原调用ID。")


class ExecutionAuxiliaryBatchSerializer(serializers.Serializer):
    field_batch_key = serializers.CharField()
    purpose = serializers.ChoiceField(choices=("generate_fields", "translate_fields"))
    field_keys = serializers.ListField(child=serializers.CharField())
    attempt_number = serializers.IntegerField(min_value=0)
    task_id = serializers.UUIDField(allow_null=True)
    preparation_id = serializers.UUIDField(allow_null=True)
    status = serializers.CharField()
    reason = serializers.CharField(allow_blank=True)
    can_retry = serializers.BooleanField()
    requires_uncertain_acknowledgement = serializers.BooleanField()


class ExecutionFieldReviewSerializer(ExecutionAuthoringSerializer):
    expected_text_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    expected_source_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    expected_preparation_revision = serializers.IntegerField(min_value=1)


class ExecutionFieldConfirmSerializer(ExecutionFieldReviewSerializer):
    select_for_execution = serializers.BooleanField(default=False)
    expected_selection_revision = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    replace = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if not attrs["select_for_execution"] and (attrs["replace"] or attrs["expected_selection_revision"] is not None):
            raise serializers.ValidationError("只确认、不采用时不能同时提交替换采用的参数")
        return attrs


class StagePreparationQuerySerializer(serializers.Serializer):
    stage_run_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=("primary", "auxiliary"), required=False)
    unit_key = serializers.CharField(max_length=191, required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def to_internal_value(self, data):
        unknown = set(data) - self.fields.keys()
        if unknown:
            raise serializers.ValidationError({"fields": "包含不支持的查询参数：" + ", ".join(sorted(unknown))})
        return super().to_internal_value(data)

    def validate(self, attrs):
        if "unit_key" in attrs and "kind" not in attrs:
            raise serializers.ValidationError("读取同一制作对象的历史必须指定准备分类")
        return attrs


class StagePreparationGroupQuerySerializer(StagePreparationQuerySerializer):
    kind = serializers.ChoiceField(choices=("primary", "auxiliary"))

    def validate(self, attrs):
        if "unit_key" in attrs:
            raise serializers.ValidationError("分组索引不接受历史筛选；请从准备列表读取组内历史")
        return attrs


class ExecutionPreparationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionPreparation
        fields = ("id", "stage_run", "asset_job", "deployment_smoke", "workflow_smoke", "execution_instance_version",
            "purpose", "unit_key", "request_key", "revision", "status", "intent_sha256", "request_sha256",
            "request_frozen_at", "next_check_at", "reason_code", "reason", "evaluation_revision", "evaluated_at", "created_by", "created_at", "updated_at")
        read_only_fields = fields


class PreparationParentPresentationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    label = serializers.CharField()
    status = serializers.CharField()
    unit_key = serializers.CharField()


class PreparationTaskPresentationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=GenerationAttempt.Status.choices)
    error_code = serializers.CharField(allow_blank=True)
    attempt_number = serializers.IntegerField(min_value=1)


class PreparationOutcomeSerializer(serializers.Serializer):
    message = serializers.CharField()
    artifact_count = serializers.IntegerField(min_value=0)
    selected_count = serializers.IntegerField(min_value=0)


class PreparationPresentationSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("primary", "auxiliary"))
    label = serializers.CharField()
    operation = serializers.CharField(allow_blank=True)
    parent = PreparationParentPresentationSerializer(allow_null=True)
    task = PreparationTaskPresentationSerializer(allow_null=True)


class StageExecutionPreparationSerializer(ExecutionPreparationSerializer):
    presentation = PreparationPresentationSerializer(read_only=True)
    outcome = PreparationOutcomeSerializer(read_only=True)

    class Meta(ExecutionPreparationSerializer.Meta):
        fields = ExecutionPreparationSerializer.Meta.fields + ("presentation", "outcome")
        read_only_fields = fields


class StagePreparationGroupSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    label = serializers.CharField()
    version_count = serializers.IntegerField(min_value=1)
    latest = StageExecutionPreparationSerializer()


class ExecutionFieldSelectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionFieldSelection
        fields = ("id", "preparation", "unit_key", "field_key", "field_value", "revision", "selected_by", "selected_at")
        read_only_fields = fields


class ExecutionCapacitySampleSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    observed_at = serializers.DateTimeField()
    elapsed_ms = serializers.IntegerField(min_value=0)
    signature_sha256 = serializers.CharField(allow_null=True)
    dimensions_complete = serializers.BooleanField()
    known_dimensions = serializers.JSONField()
    missing_dimensions = serializers.ListField(child=serializers.CharField())


class ExecutionPreparationFieldSlotSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    field_key = serializers.CharField()
    field_label = serializers.CharField(allow_blank=True)
    latest_value_id = serializers.UUIDField(allow_null=True)
    effective_language = serializers.CharField(allow_blank=True)
    next_revision = serializers.IntegerField(min_value=1)
    field_contract = serializers.JSONField()
    source_language = serializers.CharField(allow_blank=True)
    target_language = serializers.CharField(allow_blank=True)


class ExecutionFieldReviewSummarySerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("manual", "policy"))
    label = serializers.CharField()
    description = serializers.CharField()


class ExecutionResponseDiagnosticItemSerializer(serializers.Serializer):
    label = serializers.CharField()
    value = serializers.CharField()


class ExecutionResponseDiagnosticSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("recorded", "unverifiable"))
    task_id = serializers.UUIDField(allow_null=True)
    observed_at = serializers.DateTimeField(allow_null=True)
    summary = serializers.CharField()
    details = ExecutionResponseDiagnosticItemSerializer(many=True)


class ExecutionPreparationCancellationSerializer(serializers.Serializer):
    allowed = serializers.BooleanField()
    reason = serializers.CharField()
    cancelled_at = serializers.DateTimeField(allow_null=True)
    cancelled_by = serializers.UUIDField(allow_null=True)


class ExecutionResourceIssueSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class ExecutionResourceReviewSerializer(serializers.Serializer):
    fingerprint = serializers.RegexField(r"^[a-f0-9]{64}$")
    policy_current = serializers.BooleanField()
    issues = ExecutionResourceIssueSerializer(many=True)
    projected_calls = serializers.IntegerField(min_value=0)
    projected_total = serializers.CharField(allow_null=True)
    currency = serializers.CharField(allow_blank=True)
    confirmed = serializers.BooleanField()
    confirmed_at = serializers.DateTimeField(allow_null=True)


class ExecutionResponseFieldSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    field_key = serializers.CharField()
    label = serializers.CharField()
    text = serializers.CharField(allow_blank=True)


class ExecutionFieldWarningSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class ExecutionTextResponseReviewSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    attempt_id = serializers.UUIDField(allow_null=True)
    asset_job_id = serializers.UUIDField(allow_null=True)
    corrected_text = serializers.CharField(allow_null=True)
    corrected_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", allow_null=True)
    confirmed_by = serializers.CharField(allow_null=True)
    original_text = serializers.CharField(allow_blank=True)
    original_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    diagnostics = serializers.JSONField()
    editable = serializers.BooleanField()
    readonly_reason = serializers.CharField(allow_blank=True)
    confirmed_at = serializers.DateTimeField(allow_null=True)
    fields = ExecutionResponseFieldSerializer(many=True)
    notices = ExecutionFieldWarningSerializer(many=True)


class ExecutionPreparationDetailSerializer(ExecutionPreparationSerializer):
    media_archive_recovery = ExecutionMediaArchiveRecoverySerializer(allow_null=True, read_only=True)
    media_uncertain_retry = ExecutionMediaUncertainRetrySerializer(allow_null=True, read_only=True)
    outcome = PreparationOutcomeSerializer(allow_null=True, read_only=True)
    presentation = PreparationPresentationSerializer(allow_null=True, read_only=True)
    resource_review = ExecutionResourceReviewSerializer(allow_null=True, read_only=True)
    text_response_review = ExecutionTextResponseReviewSerializer(allow_null=True, read_only=True)
    cancellation = ExecutionPreparationCancellationSerializer(allow_null=True, read_only=True)
    response_diagnostic = ExecutionResponseDiagnosticSerializer(allow_null=True, read_only=True)
    auxiliary_batches = ExecutionAuxiliaryBatchSerializer(many=True, read_only=True)
    intent = serializers.JSONField()
    resolved_inputs = serializers.JSONField()
    review_contract = serializers.JSONField()
    review_summary = ExecutionFieldReviewSummarySerializer(read_only=True)
    prepared_request = serializers.JSONField(allow_null=True)
    evaluation = serializers.JSONField(allow_null=True)
    field_selections = ExecutionFieldSelectionSerializer(many=True, read_only=True)
    capacity_sample = ExecutionCapacitySampleSerializer(allow_null=True, read_only=True)
    field_slots = ExecutionPreparationFieldSlotSerializer(many=True, read_only=True)

    class Meta(ExecutionPreparationSerializer.Meta):
        fields = ExecutionPreparationSerializer.Meta.fields + ("presentation", "outcome", "intent", "resolved_inputs", "review_contract", "review_summary", "prepared_request", "evaluation", "field_selections", "capacity_sample", "field_slots", "auxiliary_batches", "response_diagnostic", "cancellation", "resource_review", "text_response_review", "media_archive_recovery", "media_uncertain_retry")
        read_only_fields = fields


class ExecutionFieldSummarySerializer(serializers.ModelSerializer):
    approval_label = serializers.SerializerMethodField()

    def get_approval_label(self, obj) -> str:
        return obj.get_approval_kind_display() if obj.status == "confirmed" else obj.get_status_display()

    class Meta:
        model = ExecutionFieldValue
        fields = ("id", "preparation", "unit_key", "field_key", "revision", "source_sha256", "field_contract_sha256",
            "text_sha256", "origin_kind", "origin_attempt", "origin_asset_job", "status", "approval_kind", "approval_label",
            "reviewed_by", "reviewed_at", "approval_policy_sha256", "created_by", "created_at", "updated_at")
        read_only_fields = fields


class ExecutionFieldDetailSerializer(ExecutionFieldSummarySerializer):
    warnings = serializers.SerializerMethodField()

    @extend_schema_field(ExecutionFieldWarningSerializer(many=True))
    def get_warnings(self, value):
        from studio.services.execution_preparation_fields import _field_context, _validate_text
        from studio.services.execution_text_response_review import field_warning_messages
        return field_warning_messages(_validate_text(_field_context(value.preparation, value.unit_key, value.field_key), value.text))

    class Meta(ExecutionFieldSummarySerializer.Meta):
        fields = ExecutionFieldSummarySerializer.Meta.fields + ("text", "warnings")
        read_only_fields = fields


class ExecutionFieldActionResultSerializer(serializers.Serializer):
    field = ExecutionFieldDetailSerializer()
    preparation = ExecutionPreparationSerializer()
    selection = ExecutionFieldSelectionSerializer(allow_null=True)
