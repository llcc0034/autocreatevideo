"""Explicit JSON parsing on original script revisions; no model or adoption."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from studio.exceptions import api_error_response
from studio.models import BusinessOutputVersion, ScriptRevisionStructure
from studio.services.business_outputs import output_reference
from studio.services.configuration import ConfigurationResolutionError
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.script_revision_structures import read_script_revision_structure, save_script_revision_structure
from studio.services.script_semantics import SEMANTICS_KEY
from studio.services.security_boundaries import security_boundary_resolution_cache


class ScriptStructureInputSerializer(serializers.Serializer):
    output_version_id = serializers.UUIDField(help_text="结构化结果使用的确切已发布剧本语义输出版本；不自动选择最新版本。")

    def validate(self, attrs):
        if set(self.initial_data) != {"output_version_id"}:
            raise serializers.ValidationError("只接受明确的 output_version_id；正文来自本修订，不接受替换正文。")
        return attrs


class ScriptStructureDetailSerializer(serializers.Serializer):
    document = serializers.JSONField(help_text="已保存的修订引用、输出快照、结构化值、业务投影和来源证据；不重新解析。")
    document_sha256 = serializers.CharField()
    json_schema = serializers.JSONField(help_text="由本详情冻结的结构快照编译，供共用字段表单使用；不取当前版本。")
    can_revise = serializers.BooleanField(help_text="是否可沿原公共修订入口另存字段草稿；不表示已确认或采用。")


class ScriptStructureResponseSerializer(serializers.Serializer):
    structure = ScriptStructureDetailSerializer(allow_null=True, help_text="null仅表示尚未保存结构化详情，不表示读取失败或有效JSON。")


class ScriptStructureOutputOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessOutputVersion
        fields = ("id", "output_key", "version", "name", "description")
        read_only_fields = fields


class ScriptRevisionStructureActions:
    @extend_schema(parameters=[OpenApiParameter("search", str)],
        responses=ScriptStructureOutputOptionSerializer(many=True))
    @action(detail=False, methods=("get",), url_path="structure-options", filter_backends=())
    def structure_options(self, request):
        rows = BusinessOutputVersion.objects.filter(status="published", parser_key=SEMANTICS_KEY).only(
            "id", "output_key", "version", "name", "description").order_by("name", "output_key", "-version", "id")
        search = request.query_params.get("search", "").strip()
        if search:
            rows = rows.filter(Q(name__icontains=search) | Q(output_key__icontains=search))
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(ScriptStructureOutputOptionSerializer(page, many=True).data)

    @extend_schema(methods=("GET",), responses=ScriptStructureResponseSerializer)
    @extend_schema(methods=("POST",), request=ScriptStructureInputSerializer,
        responses=ScriptStructureResponseSerializer)
    @action(detail=True, methods=("get", "post"))
    @security_boundary_resolution_cache()
    def structure(self, request, pk=None):
        revision = self.get_object()  # Original source-owner visibility for both reads and writes.
        try:
            if request.method == "POST":
                data = ScriptStructureInputSerializer(data=request.data)
                data.is_valid(raise_exception=True)
                output = BusinessOutputVersion.objects.select_related("data_structure_version").filter(
                    pk=data.validated_data["output_version_id"], status="published", parser_key=SEMANTICS_KEY).first()
                if output is None:
                    raise ValidationError({"output_version_id": "请选择可用的已发布剧本语义输出版本。"})
                detail = save_script_revision_structure(revision_id=revision.pk,
                    project_id=revision.source.project_id, output=output_reference(output, require_published=True),
                    max_bytes=execution_configuration_max_bytes())
            else:
                if not ScriptRevisionStructure.objects.filter(revision_id=revision.pk).exists():
                    return Response({"structure": None})
                detail = read_script_revision_structure(revision_id=revision.pk,
                    project_id=revision.source.project_id, max_bytes=execution_configuration_max_bytes())
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        except ConfigurationResolutionError as exc:
            return api_error_response(status_code=409, code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)})
        return Response({"structure": ScriptStructureDetailSerializer(detail).data})
