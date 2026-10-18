"""Closed, typed authoring payloads for the single execution registry."""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from studio.models import (
    BusinessOutputVersion,
    ExecutionAuxiliaryBinding, ExecutionInputSpec, ExecutionInputTarget, ExecutionParameterSpec,
    ModelDeployment, ModelExecutionInstance, ModelExecutionInstanceVersion, PromptProfileVersion, WorkflowTemplateVersion,
)
from studio.services.execution_instances import execution_configuration_max_bytes, execution_version_configuration
from studio.services.execution_input_catalog import ExecutionScopeKind, ExecutionSubjectKind, ExecutionSubjectCategory
from studio.services.json_contracts import json_contract
from studio.services.execution_request_storage import execution_request_max_bytes


class ExecutionAuthoringSerializer(serializers.Serializer):
    json_max_bytes = staticmethod(execution_configuration_max_bytes)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("必须提交 JSON 对象")
        unknown = set(data) - self.fields.keys()
        if unknown:
            raise serializers.ValidationError({"fields": "包含不支持的字段：" + ", ".join(sorted(unknown))})
        # Preserve exact revision/flag semantics before DRF can coerce strings
        # or booleans to integers and vice versa.
        for key, value in data.items():
            field = self.fields[key]
            if value is None and field.allow_null:
                continue
            if isinstance(field, serializers.IntegerField) and type(value) is not int:
                raise serializers.ValidationError({key: "必须是整数"})
            if isinstance(field, serializers.BooleanField) and type(value) is not bool:
                raise serializers.ValidationError({key: "必须是布尔值"})
        try:
            json_contract(data, max_bytes=self.json_max_bytes())
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return super().to_internal_value(data)


class ExecutionRequestSerializer(ExecutionAuthoringSerializer):
    """Same closed types, but runtime facts use the configured request policy."""
    json_max_bytes = staticmethod(execution_request_max_bytes)


class ExecutionSubjectFactSerializer(ExecutionRequestSerializer):
    id = serializers.UUIDField()
    category = serializers.ChoiceField(choices=ExecutionSubjectCategory.choices,
        help_text="主体语义类别，不是Bible身份；subject_ref.kind仍为external")
    name = serializers.CharField(trim_whitespace=False)
    description = serializers.CharField(allow_blank=True, trim_whitespace=False)

    def validate_id(self, value):
        return str(value)


class ExecutionSubjectCategoryOptionSerializer(serializers.Serializer):
    value = serializers.ChoiceField(choices=ExecutionSubjectCategory.choices)
    label = serializers.CharField()


class ExecutionInstanceCreateSerializer(ExecutionAuthoringSerializer):
    key = serializers.RegexField(r"^[a-z][a-z0-9_.-]{1,99}$", max_length=100)
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(allow_blank=True, default="")


class ExecutionInputSuggestionQuerySerializer(ExecutionAuthoringSerializer):
    input_key = serializers.CharField(max_length=100)
    suggestion_key = serializers.CharField(max_length=100)
    project_id = serializers.UUIDField(required=False)
    production_run_id = serializers.UUIDField(required=False)
    entity_kind = serializers.ChoiceField(choices=("character", "scene", "prop"), required=False)
    entity_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        context = {"production_run_id", "entity_kind", "entity_id"}
        if context.intersection(attrs) and not context.union({"project_id"}).issubset(attrs):
            raise serializers.ValidationError("Bible 预填需同时提供项目、制作轮及明确实体类型和ID")
        return attrs


class ExecutionInputSuggestionSerializer(serializers.Serializer):
    input_key = serializers.CharField()
    definition_sha256 = serializers.CharField()
    label = serializers.CharField()
    content = serializers.CharField()
    reference = serializers.CharField(help_text="服务端预览凭据，不是模型调用授权；可编辑副本随 literal 提交，项目事实合同随 project_snapshot 提交且不接受改写正文。")
    origin = serializers.DictField()


class ExecutionInstanceUpdateSerializer(ExecutionAuthoringSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    name = serializers.CharField(max_length=160, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    is_active = serializers.BooleanField(required=False)


class ExecutionInputSemanticSerializer(ExecutionAuthoringSerializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    allowed_purposes = serializers.ListField(child=serializers.CharField(max_length=100))
    allowed_subject_kinds = serializers.ListField(child=serializers.ChoiceField(choices=ExecutionSubjectKind.choices))
    allowed_scope_kinds = serializers.ListField(child=serializers.ChoiceField(choices=ExecutionScopeKind.choices))
    paired_with_input_key = serializers.CharField(max_length=100, allow_null=True)
    allow_repeated_asset = serializers.BooleanField()


class ExecutionInputTargetSerializer(ExecutionAuthoringSerializer):
    target_kind = serializers.ChoiceField(choices=ExecutionInputTarget.Kind.choices)
    target_key = serializers.CharField(max_length=191)
    is_primary = serializers.BooleanField()


class ExecutionReferenceField(serializers.UUIDField):
    def to_internal_value(self, data):
        return str(super().to_internal_value(data))


class ExecutionMediaSourcePolicySerializer(ExecutionAuthoringSerializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    allowed_sources = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    default_source = serializers.CharField()
    allow_override = serializers.BooleanField()
    order = serializers.ChoiceField(choices=("provided",))


class ExecutionInputSpecSerializer(ExecutionAuthoringSerializer):
    key = serializers.CharField(max_length=100)
    position = serializers.IntegerField(min_value=0)
    kind = serializers.ChoiceField(choices=ExecutionInputSpec.Kind.choices)
    required = serializers.BooleanField()
    min_items = serializers.IntegerField(min_value=0)
    max_items = serializers.IntegerField(min_value=0)
    semantic_contract = ExecutionInputSemanticSerializer()
    targets = ExecutionInputTargetSerializer(many=True)
    data_structure_version_id = ExecutionReferenceField(required=False, allow_null=True)
    media_source_policy = ExecutionMediaSourcePolicySerializer(required=False, allow_null=True)


class ExecutionParameterSpecSerializer(ExecutionAuthoringSerializer):
    key = serializers.CharField(max_length=100)
    value_type = serializers.ChoiceField(choices=ExecutionParameterSpec.ValueType.choices)
    minimum = serializers.JSONField(allow_null=True)
    maximum = serializers.JSONField(allow_null=True)
    enum_values = serializers.ListField(child=serializers.JSONField())
    step = serializers.JSONField(allow_null=True)
    remainder = serializers.JSONField(allow_null=True)
    policy = serializers.ChoiceField(choices=ExecutionParameterSpec.Policy.choices)
    default_value = serializers.JSONField(allow_null=True)
    locked_value = serializers.JSONField(allow_null=True)
    seed_policy = serializers.ChoiceField(choices=ExecutionParameterSpec.SeedPolicy.choices, allow_null=True)


class ExecutionAuxiliarySerializer(ExecutionAuthoringSerializer):
    purpose = serializers.ChoiceField(choices=ExecutionAuxiliaryBinding.Purpose.choices)
    target_version_id = ExecutionReferenceField()


class ExecutionVersionCreateSerializer(ExecutionAuthoringSerializer):
    output_version_id = serializers.PrimaryKeyRelatedField(source="output_version", queryset=BusinessOutputVersion.objects.all(), allow_null=True, required=False, default=None)
    instance_id = serializers.PrimaryKeyRelatedField(source="instance", queryset=ModelExecutionInstance.objects.all())
    expected_revision = serializers.IntegerField(min_value=1, help_text="执行实例的当前修订号")
    deployment_id = serializers.PrimaryKeyRelatedField(source="deployment", queryset=ModelDeployment.objects.select_related("profile_version__provider"))
    operation = serializers.CharField(max_length=100)
    mode = serializers.CharField(max_length=40, allow_blank=True)
    prompt_mode = serializers.ChoiceField(choices=ModelExecutionInstanceVersion.PromptMode.choices)
    prompt_profile_version_id = serializers.PrimaryKeyRelatedField(source="prompt_profile_version", queryset=PromptProfileVersion.objects.all(), allow_null=True)
    workflow_version_id = serializers.PrimaryKeyRelatedField(source="workflow_version", queryset=WorkflowTemplateVersion.objects.select_related("template"), allow_null=True)
    inputs = ExecutionInputSpecSerializer(many=True, required=False)
    parameters = ExecutionParameterSpecSerializer(many=True, required=False)
    auxiliaries = ExecutionAuxiliarySerializer(many=True, required=False)


class ExecutionVersionUpdateSerializer(ExecutionAuthoringSerializer):
    output_version_id = serializers.PrimaryKeyRelatedField(source="output_version", queryset=BusinessOutputVersion.objects.all(), allow_null=True, required=False)
    expected_revision = serializers.IntegerField(min_value=1, help_text="当前实例版本的修订号")
    deployment_id = serializers.PrimaryKeyRelatedField(source="deployment", queryset=ModelDeployment.objects.select_related("profile_version__provider"), required=False)
    operation = serializers.CharField(max_length=100, required=False)
    mode = serializers.CharField(max_length=40, allow_blank=True, required=False)
    prompt_mode = serializers.ChoiceField(choices=ModelExecutionInstanceVersion.PromptMode.choices, required=False)
    prompt_profile_version_id = serializers.PrimaryKeyRelatedField(source="prompt_profile_version", queryset=PromptProfileVersion.objects.all(), allow_null=True, required=False)
    workflow_version_id = serializers.PrimaryKeyRelatedField(source="workflow_version", queryset=WorkflowTemplateVersion.objects.select_related("template"), allow_null=True, required=False)
    inputs = ExecutionInputSpecSerializer(many=True, required=False)
    parameters = ExecutionParameterSpecSerializer(many=True, required=False)
    auxiliaries = ExecutionAuxiliarySerializer(many=True, required=False)


class ExecutionCloneSerializer(ExecutionAuthoringSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    expected_definition_sha256 = serializers.RegexField(
        r"^[a-f0-9]{64}$", allow_null=True,
        help_text="草稿提交 null；已发布/退役来源提交所查看的精确冻结合同 SHA",
    )


class ExecutionPublishSerializer(ExecutionAuthoringSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", help_text="刚核对的完整定义 SHA；发布不执行烟测")


class ExecutionRetireSerializer(ExecutionPublishSerializer):
    reason = serializers.CharField(max_length=191, trim_whitespace=False)


class ExecutionParameterMappingQuerySerializer(serializers.Serializer):
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class ExecutionParameterDestinationSerializer(serializers.Serializer):
    node_id = serializers.CharField(allow_null=True, read_only=True)
    field = serializers.CharField(read_only=True)


class ExecutionParameterMappingRowSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    destinations = ExecutionParameterDestinationSerializer(many=True, read_only=True)


class ExecutionParameterMappingsSerializer(serializers.Serializer):
    version_id = serializers.UUIDField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    definition_sha256 = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(choices=("verified", "unavailable", "not_recorded"), read_only=True)
    code = serializers.CharField(read_only=True)
    message = serializers.CharField(read_only=True)
    basis_kind = serializers.ChoiceField(choices=("workflow", "deployment", "none"), read_only=True)
    basis_id = serializers.UUIDField(allow_null=True, read_only=True)
    basis_sha256 = serializers.CharField(allow_null=True, read_only=True)
    rows = ExecutionParameterMappingRowSerializer(many=True, read_only=True)


class AppearancePrimaryInputOptionSerializer(serializers.Serializer):
    input_key = serializers.CharField(read_only=True)
    target_key = serializers.CharField(read_only=True)


class AppearanceDifferenceSourceOptionSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("manual_channel", "logical_input", "reference_field", "structured_field"), read_only=True)
    key = serializers.CharField(read_only=True)
    field_key = serializers.CharField(required=False,
        help_text="reference_field与structured_field提供精确策略字段键；参考单元身份须另从本次原输入选择，目录不提供业务正文")


class AppearanceInputOptionsSerializer(serializers.Serializer):
    primary_inputs = AppearancePrimaryInputOptionSerializer(many=True, read_only=True)
    difference_sources = AppearanceDifferenceSourceOptionSerializer(many=True, read_only=True)


class ExecutionInputContractSerializer(serializers.Serializer):
    appearance_inputs = AppearanceInputOptionsSerializer(read_only=True,
        help_text="由本份精确实例定义投影的外观输入目录；仅实际主图片槽与原通道/策略正文来源，不是变体提交开放、实际非空文本或可运行证明；不改变冻结定义摘要")
    subject_categories = ExecutionSubjectCategoryOptionSerializer(many=True, read_only=True)
    language_form = serializers.DictField(read_only=True,
        help_text="精确策略的语言填写说明：choices、allow_custom、source/target的required与fields、fixed_fields；仅解释语言上下文，不改变冻结定义或剧本输出语言")
    input_forms = serializers.DictField(child=serializers.DictField(), read_only=True,
        help_text="当前填写说明：输入键对应label、description、schemas、suggestions；引用与范例需显式预览采用，来自精确目标及原生协议，不改写冻结定义或放宽服务端校验")
    context_fields = serializers.ListField(child=serializers.DictField(), read_only=True,
        help_text="当前受控上下文选择目录，不是冻结实例定义或可提交许可")
    version_id = serializers.UUIDField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    frozen = serializers.BooleanField(read_only=True, help_text="已发布/退役版本返回冻结定义；不是可运行或烟测通过标志")
    schema_version = serializers.IntegerField(read_only=True)
    size_bytes = serializers.IntegerField(read_only=True)
    sha256 = serializers.CharField(read_only=True)
    target_fingerprint = serializers.CharField(read_only=True)
    definition = serializers.JSONField(read_only=True, help_text="有版本和摘要的完整派生定义；含配置、资源、输入、参数、通道、输出与脱敏目标身份，不接受客户端写入")


class ExecutionAuthoringOptionsSerializer(serializers.Serializer):
    version_id = serializers.UUIDField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    resources = serializers.JSONField(read_only=True)
    input_targets = serializers.JSONField(read_only=True, help_text="精确资源投影；semantic_group的reference_bindings按semantic_key保存已声明的category/input_slot，不按数组位置推测。")
    profile_inputs = serializers.JSONField(read_only=True)
    derived_input_keys = serializers.ListField(child=serializers.CharField(), read_only=True,
        help_text="从精确策略派生的本次取值及已登记项目来源输入标识，只读；派生本身不读取项目或上游结果。")
    input_purposes = serializers.JSONField(read_only=True)
    static_semantics = serializers.JSONField(read_only=True)
    parameters = serializers.JSONField(read_only=True)
    prompt_channels = serializers.JSONField(read_only=True)
    prompt_not_required = serializers.BooleanField(read_only=True)
    auxiliary_purposes = serializers.ListField(child=serializers.DictField(), read_only=True)
    identity_options = serializers.JSONField(read_only=True)


class ExecutionSelectionQuerySerializer(ExecutionAuthoringSerializer):
    deployment_id = serializers.PrimaryKeyRelatedField(source="deployment", queryset=ModelDeployment.objects.select_related("profile_version__provider"), required=False)
    workflow_version_id = serializers.PrimaryKeyRelatedField(source="workflow", queryset=WorkflowTemplateVersion.objects.select_related("template"), required=False)


class ExecutionChoiceSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class ExecutionOperationChoiceSerializer(ExecutionChoiceSerializer):
    output_parser_keys = serializers.ListField(child=serializers.CharField(), help_text="此能力支持的业务输出协议；非空时完整执行合同必须选择确切已发布输出版本。")


class ExecutionSelectionTargetSerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()
    model_profile_version_id = serializers.UUIDField()
    model_family = serializers.CharField(allow_null=True)
    driver = serializers.CharField()
    workflow_required = serializers.BooleanField()
    workflow = serializers.JSONField(allow_null=True)
    operations = ExecutionOperationChoiceSerializer(many=True)
    modes = ExecutionChoiceSerializer(many=True)


class ExecutionSelectionOptionsSerializer(serializers.Serializer):
    prompt_modes = ExecutionChoiceSerializer(many=True)
    version_statuses = ExecutionChoiceSerializer(many=True)
    input_kinds = ExecutionChoiceSerializer(many=True)
    target_kinds = ExecutionChoiceSerializer(many=True)
    parameter_types = ExecutionChoiceSerializer(many=True)
    parameter_policies = ExecutionChoiceSerializer(many=True)
    seed_policies = ExecutionChoiceSerializer(many=True)
    identity_options = serializers.JSONField()
    target = ExecutionSelectionTargetSerializer(allow_null=True, help_text="所选资源声明的能力/模式与只读模型和Projector身份；不是就绪或组合验收")


class ExecutionInstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelExecutionInstance
        fields = ("id", "key", "name", "description", "is_active", "revision", "created_at", "updated_at")
        read_only_fields = fields


class ExecutionVersionSerializer(serializers.ModelSerializer):
    output_version_id = serializers.UUIDField(read_only=True, allow_null=True)
    instance_id = serializers.UUIDField(read_only=True)
    deployment_id = serializers.UUIDField(read_only=True)
    workflow_version_id = serializers.UUIDField(read_only=True, allow_null=True)
    prompt_profile_version_id = serializers.UUIDField(read_only=True, allow_null=True)
    published_by_id = serializers.UUIDField(read_only=True, allow_null=True)
    instance_name = serializers.CharField(source="instance.name", read_only=True)
    prompt_operation = serializers.CharField(source="prompt_profile_version.operation", read_only=True, allow_null=True)
    prompt_profile_mode = serializers.CharField(source="prompt_profile_version.mode", read_only=True, allow_null=True)

    class Meta:
        model = ModelExecutionInstanceVersion
        fields = (
            "id", "instance_id", "instance_name", "version", "status", "revision",
            "deployment_id", "operation", "mode", "prompt_mode", "prompt_profile_version_id",
            "prompt_operation", "prompt_profile_mode", "workflow_version_id", "output_version_id",
            "published_at", "published_by_id", "created_at", "updated_at",
        )
        read_only_fields = fields


class ExecutionVersionConfigurationSerializer(serializers.Serializer):
    output_version_id = serializers.UUIDField(required=False)
    deployment_id = serializers.UUIDField()
    operation = serializers.CharField()
    mode = serializers.CharField(allow_blank=True)
    prompt_mode = serializers.ChoiceField(choices=ModelExecutionInstanceVersion.PromptMode.choices)
    prompt_profile_version_id = serializers.UUIDField(allow_null=True)
    workflow_version_id = serializers.UUIDField(allow_null=True)
    inputs = ExecutionInputSpecSerializer(many=True)
    parameters = ExecutionParameterSpecSerializer(many=True)
    auxiliaries = ExecutionAuxiliarySerializer(many=True)


class ExecutionVersionDetailSerializer(ExecutionVersionSerializer):
    configuration = serializers.SerializerMethodField()

    class Meta(ExecutionVersionSerializer.Meta):
        fields = ExecutionVersionSerializer.Meta.fields + ("configuration",)
        read_only_fields = fields

    @extend_schema_field(ExecutionVersionConfigurationSerializer)
    def get_configuration(self, obj):
        return execution_version_configuration(obj)
