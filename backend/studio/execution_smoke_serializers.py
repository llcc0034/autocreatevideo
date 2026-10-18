"""Closed fixture requests and original-attempt response identities."""
from rest_framework import serializers

from studio.execution_instance_serializers import ExecutionAuthoringSerializer, ExecutionRequestSerializer, ExecutionSubjectFactSerializer, ExecutionSubjectCategoryOptionSerializer
from studio.execution_preparation_serializers import ExecutionPreparationSerializer
from studio.asset_generation_serializers import AssetAppearanceSerializer


class ExecutionSmokeFixtureSerializer(ExecutionRequestSerializer):
    inputs = serializers.DictField(help_text="按精确实例输入合同提交。文字literal可附带input-suggestion返回的reference来源凭据，value仍为人工确认正文。媒体items来自asset或selected_output；配对音频也可明确使用video_audio与paired_to，引用本次父视频原音，不提交伪音频Asset或文件路径。")
    parameters = serializers.DictField()
    manual_channels = serializers.DictField(child=serializers.CharField(allow_blank=True, trim_whitespace=False))
    subject_facts = ExecutionSubjectFactSerializer(many=True)
    source_language = serializers.CharField(max_length=40, allow_blank=True)
    target_language = serializers.CharField(max_length=40, allow_blank=True)


class ExecutionInstanceSmokeFixtureSerializer(ExecutionSmokeFixtureSerializer):
    appearance_target = AssetAppearanceSerializer(required=False, allow_null=True,
        help_text="仅复用准备时保留原主图/变体声明，须与原请求完全一致；不可遗漏、更改或在独立验收中新增。")

    def to_representation(self, instance):
        result = super().to_representation(instance)
        # Nullable optional fields must not invent a declaration on old inputs.
        if "appearance_target" not in instance:
            result.pop("appearance_target", None)
        return result


class ResourceSmokeSelectionSerializer(ExecutionAuthoringSerializer):
    output_version_id = serializers.UUIDField(required=False, allow_null=True, help_text="能力需要业务输出时，明确选择已发布确切版本；不自动选择最新版本。")
    operation = serializers.CharField(max_length=100)
    mode = serializers.CharField(max_length=100, allow_blank=True)
    prompt_mode = serializers.ChoiceField(choices=("profile", "manual", "none"))
    prompt_profile_version_id = serializers.UUIDField(allow_null=True)

    def validate(self, data):
        if (data["prompt_mode"] == "profile") != (data["prompt_profile_version_id"] is not None):
            raise serializers.ValidationError("策略模式需要一个精确策略版本；人工或无提示词模式不能附带策略")
        return data


class ResourceSmokeContractSerializer(serializers.Serializer):
    subject_categories = ExecutionSubjectCategoryOptionSerializer(many=True, read_only=True)
    language_form = serializers.DictField(read_only=True, help_text="精确策略的语言选项与source/target依赖说明，不是自动翻译或输出语言覆盖")
    input_forms = serializers.DictField(child=serializers.DictField(), read_only=True)
    scope = serializers.ChoiceField(choices=("resource_smoke",))
    version_id = serializers.UUIDField(allow_null=True)
    schema_version = serializers.IntegerField()
    size_bytes = serializers.IntegerField()
    sha256 = serializers.CharField()
    target_fingerprint = serializers.CharField()
    definition = serializers.DictField()
    context_fields = serializers.ListField(child=serializers.DictField())


class ExecutionFieldSmokeSourceQuerySerializer(ExecutionAuthoringSerializer):
    profile_version_id = serializers.UUIDField()


class ExecutionFieldSmokeItemSerializer(ExecutionRequestSerializer):
    field_key = serializers.CharField(max_length=100)
    source_text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    source_language = serializers.CharField(max_length=40, allow_blank=True)
    target_language = serializers.CharField(max_length=40, allow_blank=True)
    context_facts = serializers.DictField()
    protected_tokens = serializers.ListField(child=serializers.CharField(trim_whitespace=False))


class ExecutionFieldSmokeSourceCreateSerializer(ExecutionRequestSerializer):
    profile_version_id = serializers.UUIDField()
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    expected_profile_contract_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    items = ExecutionFieldSmokeItemSerializer(many=True)


class ExecutionFieldSmokeSourceSerializer(serializers.Serializer):
    execution_instance_version_id = serializers.UUIDField()
    definition_sha256 = serializers.CharField()
    input_key = serializers.CharField()
    profile_version_id = serializers.UUIDField()
    profile_contract_sha256 = serializers.CharField()
    rules = serializers.ListField(child=serializers.DictField())
    source = serializers.DictField(required=False)


class ResourceFieldSmokeSourceQuerySerializer(ExecutionRequestSerializer):
    selection = ResourceSmokeSelectionSerializer()
    profile_version_id = serializers.UUIDField()
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", required=False)
    expected_profile_contract_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", required=False)
    items = ExecutionFieldSmokeItemSerializer(many=True, required=False)

    def validate(self, data):
        batch = {"expected_definition_sha256", "expected_profile_contract_sha256", "items"}
        if batch.intersection(data) and not batch.issubset(data):
            raise serializers.ValidationError("组装字段测试输入需要同时提供定义摘要、策略摘要及测试内容")
        return data


class ResourceFieldSmokeSourceSerializer(ExecutionFieldSmokeSourceSerializer):
    execution_instance_version_id = serializers.UUIDField(allow_null=True)


class ExecutionSmokeInitialFieldSerializer(ExecutionAuthoringSerializer):
    unit_key = serializers.CharField(max_length=191)
    field_key = serializers.CharField(max_length=100)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)


class ExecutionSmokeFieldSelectionSerializer(ExecutionAuthoringSerializer):
    source_preparation_id = serializers.CharField(max_length=36)
    field_value_id = serializers.CharField(max_length=36)
    unit_key = serializers.CharField(max_length=191)
    field_key = serializers.CharField(max_length=100)


class PreparedSmokeCreateSerializer(ExecutionRequestSerializer):
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    fixture = ExecutionSmokeFixtureSerializer()
    field_values = ExecutionSmokeInitialFieldSerializer(many=True)
    allow_external_calls = serializers.BooleanField()


class ResourceSmokeCreateSerializer(PreparedSmokeCreateSerializer):
    selection = ResourceSmokeSelectionSerializer()


class ReviewedSmokeContextSerializer(ExecutionAuthoringSerializer):
    run_id = serializers.UUIDField()
    stage_key = serializers.CharField(max_length=100)
    run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class SmokeSourceContextQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False)

    def to_internal_value(self, data):
        if set(data) - self.fields.keys():
            raise serializers.ValidationError({"query": "包含不支持的来源上下文查询参数"})
        return super().to_internal_value(data)


class SmokeSourceContextRowSerializer(ReviewedSmokeContextSerializer):
    project_id = serializers.UUIDField()
    project_title = serializers.CharField()
    run_created_at = serializers.DateTimeField()


class SmokeSourceContextPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = SmokeSourceContextRowSerializer(many=True)


class ExecutionSmokeCreateSerializer(PreparedSmokeCreateSerializer):
    fixture = ExecutionInstanceSmokeFixtureSerializer()
    reviewed_source_context = ReviewedSmokeContextSerializer(required=False)
    reuse_preparation_id = serializers.UUIDField(required=False)
    expected_reuse_revision = serializers.IntegerField(min_value=1, required=False)
    field_selections = ExecutionSmokeFieldSelectionSerializer(many=True, required=False)


class ExecutionSmokeCreatedSerializer(serializers.Serializer):
    attempt_kind = serializers.CharField()
    attempt_id = serializers.UUIDField()
    status = serializers.CharField()
    preparation = ExecutionPreparationSerializer()


class ExecutionSmokeReuseQuerySerializer(serializers.Serializer):
    expected_preparation_revision = serializers.IntegerField(min_value=1, max_value=2147483647)

    def to_internal_value(self, data):
        import re
        if set(data) != {"expected_preparation_revision"}:
            raise serializers.ValidationError({"query": "只接受明确的准备修订号"})
        if not re.fullmatch(r"[1-9][0-9]{0,9}", data.get("expected_preparation_revision", "")):
            raise serializers.ValidationError({"expected_preparation_revision": "请提供正整数修订号"})
        return super().to_internal_value(data)


class ExecutionSmokeReuseFixtureSerializer(serializers.Serializer):
    execution_instance_version_id = serializers.UUIDField()
    expected_definition_sha256 = serializers.CharField()
    reuse_preparation_id = serializers.UUIDField()
    expected_reuse_revision = serializers.IntegerField(min_value=1)
    fixture = ExecutionInstanceSmokeFixtureSerializer()
    field_selections = ExecutionSmokeFieldSelectionSerializer(many=True)


class ExecutionSmokeEvidenceQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=200, required=False)
    status = serializers.ChoiceField(choices=("preparing", "queued", "submitting", "submitted", "running",
        "cancel_requested", "succeeded", "failed", "cancelled", "uncertain"), required=False)

    def to_internal_value(self, data):
        if set(data) - self.fields.keys():
            raise serializers.ValidationError({"query": "包含未支持的验收查询参数"})
        return super().to_internal_value(data)


class ExecutionSmokeIdentitySerializer(serializers.Serializer):
    version_id = serializers.UUIDField()
    definition_sha256 = serializers.CharField()
    target_fingerprint = serializers.CharField()
    operation = serializers.CharField()
    mode = serializers.CharField(allow_blank=True)
    scope = serializers.ChoiceField(choices=("instance_smoke",))


class ExecutionSmokeSummarySerializer(serializers.Serializer):
    identity = ExecutionSmokeIdentitySerializer()
    read_at = serializers.DateTimeField()
    evidence_state = serializers.ChoiceField(choices=("missing", "failed", "fresh", "expired"))
    latest_completed_attempt_id = serializers.UUIDField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    deployment_id = serializers.UUIDField()
    workflow_version_id = serializers.UUIDField(allow_null=True)
    prompt_mode = serializers.ChoiceField(choices=("manual", "profile", "none"))
    prompt_profile_version_id = serializers.UUIDField(allow_null=True)
    policies = serializers.DictField()


class ExecutionSmokeHistorySerializer(serializers.Serializer):
    attempt_id = serializers.UUIDField()
    attempt_kind = serializers.ChoiceField(choices=("deployment_smoke", "workflow_smoke"))
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    submission_started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    evidence_expires_at = serializers.DateTimeField(allow_null=True)
    target_sha256 = serializers.CharField()
    error_code = serializers.CharField(allow_blank=True)
    estimated_cost = serializers.DecimalField(max_digits=14, decimal_places=6, allow_null=True)
    actual_cost = serializers.DecimalField(max_digits=14, decimal_places=6, allow_null=True)
    cost_currency = serializers.CharField(allow_blank=True)
    preparation_id = serializers.UUIDField()
    preparation_status = serializers.CharField()
    preparation_revision = serializers.IntegerField()
    preparation_reason_code = serializers.CharField(allow_blank=True)
    request_sha256 = serializers.CharField(allow_null=True)


class ExecutionSmokeEvidenceSerializer(serializers.Serializer):
    summary = ExecutionSmokeSummarySerializer()
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = ExecutionSmokeHistorySerializer(many=True)
