"""Staff authoring for versioned output contracts, never generated artifacts."""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from studio.models import BusinessOutputVersion
from studio.content_source_views import SourceVersionPagination
from studio.services.business_outputs import parser_contract, parser_options, write_output, verified_output_contract
from studio.services.production_visual_rules import visual_rules_output_editor_options
from studio.services.script_semantics import SEMANTICS_KEY, freeze_script_semantics, preview_script_semantics, script_semantic_options
from studio.services.visual_rules_output_contract import visual_rule_allowed_value_fields, visual_rule_field_source_options, visual_rule_response_key_policy
from studio.platform_serializers import ConfigurationErrorResponseSerializer, VisualRulesOutputPreviewInputSerializer, VisualRulesOutputPreviewResponseSerializer


class BusinessOutputSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        if "parser_contract" in self.Meta.fields:
            try:
                verified_output_contract(instance)
            except DjangoValidationError as exc:
                raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return super().to_representation(instance)

    class Meta:
        model = BusinessOutputVersion
        fields = ("id", "output_key", "version", "name", "description", "parser_key", "data_structure_version", "parser_contract", "quality_advisories", "status",
            "revision", "contract_sha256", "contract_size_bytes", "published_at", "published_by", "created_at", "updated_at")
        read_only_fields = fields


class BusinessOutputListSerializer(BusinessOutputSerializer):
    class Meta(BusinessOutputSerializer.Meta):
        fields = tuple(key for key in BusinessOutputSerializer.Meta.fields if key not in ("quality_advisories", "parser_contract"))
        read_only_fields = fields


class OutputWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=1000, allow_blank=True, default="")
    parser_key = serializers.CharField(max_length=80, help_text="parsers目录中的已实现协议；structured_json.v1使用共享结构，script_semantics.v1增加显式剧本语义映射，visual_rules.v2执行影视规则业务转换。")
    data_structure_version = serializers.UUIDField(help_text="模型原始响应使用的已发布确切结构版本；复用统一字段定义与使用规则，不接受内联Schema。")
    quality_advisories = serializers.DictField(default=dict, help_text="影视规则质量建议；structured_json.v1/script_semantics.v1仅接受空对象，其字段规则来自结构。")
    allowed_values = serializers.DictField(required=False,
        help_text="仅visual_rules.v2：已声明色彩字段的允许取值子集；省略保留，空对象清除限制，不接受空数组、未知字段或任意路径。structured_json.v1须省略此项。")
    field_sources = serializers.DictField(child=serializers.CharField(), required=False,
        help_text="完整选择三项基础响应来源：continuity_summary/continuity_points、color_design/color_script、negative_constraints/negative_prompt；可增加 camera_guidance 英文文字。所选字段都必须返回并审核。省略保留；选项、optional及转换规则见 parsers，不支持任意新字段或类型。")
    response_keys = serializers.DictField(child=serializers.CharField(trim_whitespace=False), required=False,
        help_text="本版本所选业务字段到响应JSON位置的完整映射，点分隔对象／数组层级、最多4层；数组用从0连续完整声明的整数下标，不留空或混用对象字段。不支持通配符、脚本、别名或外部读取。省略保留，空对象使用所选格式的顶层字段名；规则见 parsers.response_key_policy。")
    semantic_mapping = serializers.DictField(required=False, help_text="仅script_semantics.v1：共用schema_version=1映射，来源为所选响应结构，目标目录见parsers.script_semantics。新建必需，更新省略保留；不创建剧本或素材。")

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("包含不可编辑字段")
        if "expected_revision" in self.initial_data and type(self.initial_data["expected_revision"]) is not int:
            raise serializers.ValidationError("修订号必须为整数")
        return attrs


class OutputUpdateSerializer(OutputWriteSerializer):
    expected_revision = serializers.IntegerField(min_value=1)


class OutputActionSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if set(self.initial_data) != {"expected_revision"}:
            raise serializers.ValidationError("操作只接受当前修订号")
        if type(self.initial_data["expected_revision"]) is not int:
            raise serializers.ValidationError("修订号必须为整数")
        return attrs


class OutputParserOptionsSerializer(serializers.Serializer):
    parsers = serializers.ListField(child=serializers.DictField())
    parser = serializers.DictField()
    editor = serializers.DictField()
    allowed_value_fields = serializers.ListField(child=serializers.DictField())
    field_source_options = serializers.ListField(child=serializers.DictField())
    response_key_policy = serializers.DictField()
    script_semantics = serializers.DictField()


class StructuredOutputPreviewRequestSerializer(OutputActionSerializer):
    expected_contract_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    response_text = serializers.CharField(trim_whitespace=False, allow_blank=True)

    def validate(self, attrs):
        if (set(self.initial_data) != set(self.fields) or type(self.initial_data["expected_revision"]) is not int
                or not isinstance(self.initial_data["response_text"], str)):
            raise serializers.ValidationError("预览只接受当前修订号、合同摘要及样例JSON原文")
        return attrs


class StructuredOutputPreviewSerializer(serializers.Serializer):
    preview_only = serializers.BooleanField()
    output_version_id = serializers.UUIDField()
    output_revision = serializers.IntegerField()
    output_contract_sha256 = serializers.CharField()
    structure_version_id = serializers.UUIDField()
    json_schema = serializers.DictField()
    fields = serializers.ListField(child=serializers.DictField())
    parsed = serializers.BooleanField()
    valid = serializers.BooleanField()
    raw_value = serializers.JSONField(allow_null=True)
    value = serializers.JSONField(allow_null=True)
    defaults = serializers.ListField(child=serializers.DictField())
    warnings = serializers.ListField(child=serializers.DictField())
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class ScriptSemanticPreviewSerializer(serializers.Serializer):
    preview_only = serializers.BooleanField()
    validation_scope = serializers.CharField()
    output_version_id = serializers.UUIDField()
    output_revision = serializers.IntegerField()
    output_contract_sha256 = serializers.CharField()
    configuration_sha256 = serializers.CharField()
    source_structure_version_id = serializers.UUIDField()
    source_fields = serializers.ListField(child=serializers.DictField())
    parsed = serializers.BooleanField()
    valid = serializers.BooleanField()
    raw_value = serializers.JSONField(allow_null=True)
    source_value = serializers.JSONField(allow_null=True)
    value = serializers.JSONField(allow_null=True)
    defaults = serializers.ListField(child=serializers.DictField())
    provenance = serializers.ListField(child=serializers.DictField())
    warnings = serializers.ListField(child=serializers.DictField())
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class BusinessOutputVersionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = BusinessOutputVersion.objects.all()
    serializer_class = BusinessOutputSerializer
    permission_classes = (IsAdminUser,)
    pagination_class = SourceVersionPagination
    filterset_fields = ("status", "output_key", "parser_key")
    search_fields = ("name",)
    http_method_names = ("get", "post", "patch", "head", "options")

    @extend_schema(request=StructuredOutputPreviewRequestSerializer, responses={200: ScriptSemanticPreviewSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="preview-script-semantics")
    def preview_script(self, request, pk=None):
        output = self.get_object()
        incoming = StructuredOutputPreviewRequestSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        data = incoming.validated_data
        if output.revision != data["expected_revision"] or output.contract_sha256 != data["expected_contract_sha256"]:
            raise ValidationError("输出修订或合同已变化，请重新读取；预览不会改用当前版本")
        from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES
        from studio.services.security_boundaries import boundary_int
        try:
            contract = verified_output_contract(output)
            if output.parser_key != SEMANTICS_KEY:
                raise DjangoValidationError("请选择剧本语义映射输出协议")
            configuration = freeze_script_semantics(source_contract=contract.value["data_structure"],
                mapping=contract.value["parser"]["mapping"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            report = preview_script_semantics(configuration=configuration, response_text=data["response_text"],
                max_bytes=boundary_int("security.provider.max_response_bytes"))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response({**report, "output_version_id": str(output.pk), "output_revision": output.revision,
            "output_contract_sha256": contract.sha256}, headers={"Cache-Control": "private, no-store"})

    @extend_schema(request=StructuredOutputPreviewRequestSerializer, responses={200: StructuredOutputPreviewSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="preview-json")
    def preview_json(self, request, pk=None):
        output = self.get_object()
        incoming = StructuredOutputPreviewRequestSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        data = incoming.validated_data
        if output.revision != data["expected_revision"] or output.contract_sha256 != data["expected_contract_sha256"]:
            raise ValidationError("输出修订或合同已变化，请重新读取；预览不会改用当前版本")
        from studio.services.security_boundaries import boundary_int
        from studio.services.structured_output_parsing import parse_structured_output
        try:
            contract = verified_output_contract(output)
            result = parse_structured_output(reference={**contract.value, "sha256": contract.sha256},
                response_text=data["response_text"], max_bytes=boundary_int("security.provider.max_response_bytes"))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response({"preview_only": True, "output_revision": output.revision, **result},
            headers={"Cache-Control": "private, no-store"})

    def get_serializer_class(self):
        return BusinessOutputListSerializer if self.action == "list" else BusinessOutputSerializer

    @extend_schema(request=VisualRulesOutputPreviewInputSerializer, responses={200: VisualRulesOutputPreviewResponseSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer})
    @action(detail=False, methods=("post",), url_path="preview-visual-rules-output")
    def preview_visual_rules_output(self, request):
        from studio.services.visual_rules_output_preview import preview_visual_rules_output
        serializer = VisualRulesOutputPreviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = preview_visual_rules_output(**serializer.validated_data)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response(result)

    def get_queryset(self):
        result = super().get_queryset()
        return result.defer("quality_advisories", "parser_contract") if self.action == "list" else result.select_related("data_structure_version")

    def _write(self, **kwargs):
        try:
            return write_output(actor=self.request.user, **kwargs)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc

    @extend_schema(request=OutputWriteSerializer, responses={201: BusinessOutputSerializer})
    def create(self, request):
        payload = OutputWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(self.get_serializer(self._write(data=payload.validated_data)).data, status=201)

    @extend_schema(request=OutputUpdateSerializer, responses=BusinessOutputSerializer)
    def partial_update(self, request, pk=None):
        output = self.get_object()
        payload = OutputUpdateSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if "expected_revision" not in data:
            raise ValidationError({"expected_revision": "请提供当前修订号"})
        updated = self._write(output_id=output.pk, expected_revision=data.pop("expected_revision"), data=data)
        return Response(self.get_serializer(updated).data)

    def _action(self, request, name):
        output = self.get_object()
        payload = OutputActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        updated = self._write(output_id=output.pk, action=name, **payload.validated_data)
        return Response(self.get_serializer(updated).data, status=201 if name == "successor" else 200)

    @extend_schema(request=OutputActionSerializer, responses=BusinessOutputSerializer)
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        return self._action(request, "publish")

    @extend_schema(request=OutputActionSerializer, responses={201: BusinessOutputSerializer})
    @action(detail=True, methods=("post",))
    def successor(self, request, pk=None):
        return self._action(request, "successor")

    @extend_schema(request=OutputActionSerializer, responses=BusinessOutputSerializer)
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        return self._action(request, "retire")

    @extend_schema(responses=OutputParserOptionsSerializer)
    @action(detail=False, methods=("get",), pagination_class=None, filter_backends=[])
    def parsers(self, request):
        return Response({"parsers": parser_options(), "parser": parser_contract("visual_rules.v2"), "editor": visual_rules_output_editor_options(),
            "allowed_value_fields": visual_rule_allowed_value_fields(), "field_source_options": visual_rule_field_source_options(),
            "response_key_policy": visual_rule_response_key_policy(), "script_semantics": script_semantic_options()})
