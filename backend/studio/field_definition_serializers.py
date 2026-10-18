"""Shared field-version authoring contract for the field-center integration."""
from collections.abc import Mapping
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from studio.models import FieldDefinitionVersion
from studio.services.field_definitions import (
    create_field_definition_version,
    update_field_definition_draft,
)


class FieldDefinitionVersionSerializer(serializers.ModelSerializer):
    preview_example = serializers.SerializerMethodField()

    @extend_schema_field(serializers.JSONField())
    def get_preview_example(self, obj):
        from studio.services.prompt_profile_preview_examples import prompt_profile_preview_example
        return prompt_profile_preview_example(obj)

    field_key = serializers.CharField(source="field_definition.key", read_only=True)
    source_version = serializers.PrimaryKeyRelatedField(
        queryset=FieldDefinitionVersion.objects.all(), required=False, write_only=True,
    )

    class Meta:
        model = FieldDefinitionVersion
        fields = (
            "id", "field_definition", "field_key", "version", "status", "label", "category",
            "description", "impact", "value_type", "unit", "value_options", "preview_example",
            "source_version", "created_at", "updated_at",
        )
        read_only_fields = ("id", "version", "status", "created_at", "updated_at")
        # Version numbers are allocated inside the identity lock, not supplied
        # by clients. The database enforces the identity/version unique pair.
        validators = []

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            return super().to_internal_value(data)
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: "此项不属于字段定义版本" for key in sorted(unknown)})
        return super().to_internal_value(data)

    def create(self, validated_data):
        definition = validated_data.pop("field_definition")
        source = validated_data.pop("source_version", None)
        try:
            return create_field_definition_version(
                field_definition_id=definition.pk,
                source_version_id=source.pk if source is not None else None,
                content=validated_data,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, "message_dict", exc.messages)) from exc

    def update(self, instance, validated_data):
        definition = validated_data.pop("field_definition", instance.field_definition)
        if definition.pk != instance.field_definition_id or "source_version" in validated_data:
            raise serializers.ValidationError("已有版本不能更换字段身份或来源版本")
        try:
            return update_field_definition_draft(version_id=instance.pk, content=validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, "message_dict", exc.messages)) from exc


class FieldDefinitionVersionSummarySerializer(serializers.ModelSerializer):
    """Version selection metadata; full meaning and enum contents are detail-only."""
    field_key = serializers.CharField(source="field_definition.key", read_only=True)

    class Meta:
        model = FieldDefinitionVersion
        fields = (
            "id", "field_definition", "field_key", "version", "status", "label",
            "category", "value_type", "unit", "created_at", "updated_at",
        )
        read_only_fields = fields


class FieldDefinitionChoiceSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class FieldDefinitionVersionOptionsSerializer(serializers.Serializer):
    value_types = FieldDefinitionChoiceSerializer(many=True)
    statuses = FieldDefinitionChoiceSerializer(many=True)
