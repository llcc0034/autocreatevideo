"""Structure configuration only; this API never creates business task values."""
from collections.abc import Mapping

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from studio.models import DataStructureVersion
from studio.services.data_structures import NODE_FIELDS, write_data_structure


class StructureNodesField(serializers.JSONField):
    def get_attribute(self, instance):
        return instance

    def to_representation(self, structure):
        nodes = list(structure.nodes.all())
        keys = {node.pk: node.node_key for node in nodes}
        if any(node.parent_id is not None and node.parent_id not in keys for node in nodes):
            raise serializers.ValidationError("结构包含不属于本版本的父节点，请检查配置数据")
        return [{
            **{name: getattr(node, name) for name in NODE_FIELDS},
            "parent_key": keys[node.parent_id] if node.parent_id is not None else None,
            "field_definition_version": str(node.field_definition_version_id) if node.field_definition_version_id else None,
        } for node in nodes]


class DataStructureVersionSerializer(serializers.ModelSerializer):
    nodes = StructureNodesField(required=False)
    source_version = serializers.PrimaryKeyRelatedField(
        queryset=DataStructureVersion.objects.all(), write_only=True, required=False,
    )

    class Meta:
        model = DataStructureVersion
        fields = ("id", "structure_key", "version", "name", "description", "status", "nodes", "source_version", "created_at", "updated_at")
        read_only_fields = ("id", "structure_key", "version", "status", "created_at", "updated_at")
        validators = []

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unsupported = set(data) - {"name", "description", "nodes", "source_version"}
            if unsupported:
                raise serializers.ValidationError({key: "此项不属于结构可编辑内容" for key in sorted(unsupported)})
        return super().to_internal_value(data)

    def _write(self, data, instance=None):
        source = data.pop("source_version", None)
        try:
            return write_data_structure(content=data, version_id=instance.pk if instance else None,
                source_version_id=source.pk if source else None)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc

    def create(self, validated_data):
        return self._write(validated_data)

    def update(self, instance, validated_data):
        return self._write(validated_data, instance)


class DataStructureSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = DataStructureVersion
        fields = ("id", "structure_key", "version", "name", "status", "created_at", "updated_at")
        read_only_fields = fields


class DataStructurePreviewSerializer(serializers.Serializer):
    valid = serializers.BooleanField()
    json_schema = serializers.JSONField(allow_null=True)
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class DataStructureAuthoringOptionsSerializer(serializers.Serializer):
    node_defaults = serializers.JSONField()
    statuses = serializers.ListField(child=serializers.DictField(child=serializers.CharField()))
    constraint_schemas = serializers.DictField(child=serializers.JSONField())
    value_schemas = serializers.DictField(child=serializers.JSONField())


class StructureValueRequestSerializer(serializers.Serializer):
    value = serializers.JSONField(allow_null=True)

    def to_internal_value(self, data):
        if isinstance(data, Mapping) and set(data) - {"value"}:
            raise serializers.ValidationError({"non_field_errors": ["值预览仅接受显式 value，不修改结构或创建任务"]})
        return super().to_internal_value(data)


class StructureDefaultSourceSerializer(serializers.Serializer):
    path = serializers.CharField()
    node_key = serializers.CharField()
    structure_version_id = serializers.UUIDField()
    field_definition_version_id = serializers.UUIDField()
    source = serializers.ChoiceField(choices=("configured_default",))
    value = serializers.JSONField(allow_null=True)


class StructureValuePreviewSerializer(serializers.Serializer):
    preview_only = serializers.BooleanField()
    structure_version_id = serializers.UUIDField()
    valid = serializers.BooleanField()
    value = serializers.JSONField(allow_null=True)
    defaults = StructureDefaultSourceSerializer(many=True)
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class StructurePathOptionsSerializer(serializers.Serializer):
    structure_version_id = serializers.UUIDField()
    valid = serializers.BooleanField()
    options = serializers.ListField(child=serializers.JSONField())
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class StructureMappingRequestSerializer(serializers.Serializer):
    source_structure_version = serializers.PrimaryKeyRelatedField(queryset=DataStructureVersion.objects.all())
    source_text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    mapping = serializers.JSONField()

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            if set(data) - {"source_structure_version", "source_text", "mapping"}:
                raise serializers.ValidationError({"non_field_errors": ["映射预览不接受配置保存或执行操作"]})
            if "source_text" in data and not isinstance(data["source_text"], str):
                raise serializers.ValidationError({"source_text": "请输入原始JSON文本，不进行隐式转换"})
        return super().to_internal_value(data)


class StructureMappingAuthoringOptionsSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    nodes = serializers.ListField(child=serializers.DictField(child=serializers.CharField()))
    scopes = serializers.ListField(child=serializers.DictField(child=serializers.CharField()))


class StructureMappingPreviewSerializer(serializers.Serializer):
    preview_only = serializers.BooleanField()
    valid = serializers.BooleanField()
    parsed = serializers.BooleanField()
    source_structure_version_id = serializers.UUIDField()
    target_structure_version_id = serializers.UUIDField()
    raw_value = serializers.JSONField(allow_null=True)
    source_value = serializers.JSONField(allow_null=True)
    value = serializers.JSONField(allow_null=True)
    defaults = StructureDefaultSourceSerializer(many=True)
    provenance = serializers.ListField(child=serializers.JSONField())
    warnings = serializers.ListField(child=serializers.JSONField())
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))


class InputSourceOptionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()
    kinds = serializers.ListField(child=serializers.CharField())
    reader = serializers.CharField(allow_null=True)
    available = serializers.BooleanField()
    requires = serializers.ListField(child=serializers.CharField())
    description = serializers.CharField()


class InputSourceCatalogSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    sources = InputSourceOptionSerializer(many=True)
    orders = serializers.ListField(child=serializers.DictField(child=serializers.CharField()))
    configuration_owner = serializers.CharField()
    description = serializers.CharField()


class MediaSourcePreviewRequestSerializer(serializers.Serializer):
    policy = serializers.JSONField()
    selection = serializers.JSONField(allow_null=True)
    override = serializers.JSONField(required=False, allow_null=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - {"policy", "selection", "override"}:
            raise serializers.ValidationError({"non_field_errors": ["只接受来源策略、明确选择及可选本次改选；不接受执行或运行上下文"]})
        return super().to_internal_value(data)


class MediaSourcePreviewSerializer(serializers.Serializer):
    preview_only = serializers.BooleanField()
    valid = serializers.BooleanField()
    resolved = serializers.JSONField(allow_null=True)
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))
