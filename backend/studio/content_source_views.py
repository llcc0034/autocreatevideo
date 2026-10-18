"""Staff-only source authoring. Reading this registry never reads business content."""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from studio.models import ContentSourceVersion
from studio.pagination import StandardPageNumberPagination
from studio.services.content_sources import verified_source_contract, source_reader_options, write_source


class SourceReaderSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()
    group_label = serializers.CharField()
    description = serializers.CharField()
    record_location = serializers.CharField()
    execution_usage = serializers.CharField()
    reader = serializers.DictField()


class ContentSourceVersionSerializer(serializers.ModelSerializer):
    reader = serializers.SerializerMethodField(help_text="本来源版本已保存并核验摘要的读取合同；不是当前作者目录的重新投影。")

    def get_reader(self, obj) -> dict:
        try:
            return verified_source_contract(obj).value["reader"]
        except DjangoValidationError as exc:
            raise ValidationError({"reader": "来源版本合同无法核验，请停止使用并检查；不会回退到当前目录。"}) from exc

    class Meta:
        model = ContentSourceVersion
        fields = ("id", "source_key", "version", "name", "description", "reader_selector", "reader", "status",
            "data_structure_version", "revision", "contract_sha256", "contract_size_bytes", "published_at", "published_by", "created_at", "updated_at")
        read_only_fields = fields


class ContentSourceSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentSourceVersion
        fields = ("id", "source_key", "version", "name", "reader_selector", "status", "data_structure_version", "revision")
        read_only_fields = fields


class SourceWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=1000, allow_blank=True, default="")
    reader_selector = serializers.CharField(max_length=80)
    data_structure_version = serializers.UUIDField(help_text="已发布的确切结构版本；不自动选择最新版，不另写业务Schema。")

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("包含不可编辑的字段")
        return attrs


class SourceUpdateSerializer(SourceWriteSerializer):
    expected_revision = serializers.IntegerField(min_value=1)


class SourceActionSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if set(self.initial_data) != {"expected_revision"}:
            raise serializers.ValidationError("操作只接受当前修订号，不可同时改写来源")
        return attrs


class SourceVersionPagination(StandardPageNumberPagination):
    def get_page_size(self, request):
        size = super().get_page_size(request)
        return min(size, 200 if "page_size" in request.query_params else 50)


class ContentSourceVersionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = ContentSourceVersion.objects.all()
    serializer_class = ContentSourceVersionSerializer
    permission_classes = (IsAdminUser,)
    pagination_class = SourceVersionPagination
    filterset_fields = ("status", "source_key", "reader_selector")
    search_fields = ("name",)

    def get_serializer_class(self):
        return ContentSourceSummarySerializer if self.action == "list" else self.serializer_class

    def get_queryset(self):
        query = super().get_queryset()
        if self.action == "list":
            return query.only(*ContentSourceSummarySerializer.Meta.fields)
        return query.select_related("data_structure_version")

    def _write(self, *, data=None, source_id=None, expected_revision=None, action="update"):
        try:
            return write_source(actor=self.request.user, data=data, source_id=source_id,
                expected_revision=expected_revision, action=action)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc

    @extend_schema(request=SourceWriteSerializer, responses={201: ContentSourceVersionSerializer})
    def create(self, request):
        payload = SourceWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        source = self._write(data=payload.validated_data)
        return Response(self.get_serializer(source).data, status=201)

    @extend_schema(request=SourceUpdateSerializer, responses=ContentSourceVersionSerializer)
    def partial_update(self, request, pk=None):
        source = self.get_object()
        payload = SourceUpdateSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if "expected_revision" not in data:
            raise ValidationError({"expected_revision": "请提供当前修订号"})
        source = self._write(source_id=source.pk, expected_revision=data.pop("expected_revision"), data=data)
        return Response(self.get_serializer(source).data)

    def _action(self, request, action_name):
        source = self.get_object()
        payload = SourceActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        source = self._write(source_id=source.pk, action=action_name, **payload.validated_data)
        return Response(self.get_serializer(source).data, status=201 if action_name == "successor" else 200)

    @extend_schema(request=SourceActionSerializer, responses=ContentSourceVersionSerializer)
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        return self._action(request, "publish")

    @extend_schema(request=SourceActionSerializer, responses={201: ContentSourceVersionSerializer})
    @action(detail=True, methods=("post",))
    def successor(self, request, pk=None):
        return self._action(request, "successor")

    @extend_schema(request=SourceActionSerializer, responses=ContentSourceVersionSerializer)
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        return self._action(request, "retire")

    @extend_schema(responses=SourceReaderSerializer(many=True))
    @action(detail=False, methods=("get",), pagination_class=None, filter_backends=[])
    def readers(self, request):
        return Response(SourceReaderSerializer(source_reader_options(), many=True).data)
