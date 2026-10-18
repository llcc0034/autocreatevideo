"""Exact raw JSON field revisions; never string-coerce authored values."""
from rest_framework import serializers

from studio.shot_source_serializers import ShotSourceFieldSerializer, ShotStructuredSourceSerializer


class ShotFieldRevisionReferenceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)
    previous_id = serializers.UUIDField(allow_null=True)
    source_sha256 = serializers.CharField()
    content_sha256 = serializers.CharField()
    created_by = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class ShotRevisedFieldSerializer(serializers.Serializer):
    original = ShotSourceFieldSerializer()
    schema = serializers.JSONField(help_text="Exact frozen node schema for the shared structured value editor.")
    overridden = serializers.BooleanField()
    present = serializers.BooleanField()
    value = serializers.JSONField(required=False, allow_null=True)

    def to_representation(self, instance):
        result = super().to_representation(instance)
        if "value" not in instance:
            result.pop("value", None)
        return result


class ShotFieldRevisionDetailSerializer(serializers.Serializer):
    source = ShotStructuredSourceSerializer()
    source_sha256 = serializers.CharField()
    revision = ShotFieldRevisionReferenceSerializer(allow_null=True)
    fields = ShotRevisedFieldSerializer(many=True)


class ShotFieldRevisionWriteSerializer(serializers.Serializer):
    request_id = serializers.UUIDField(help_text="Keep the exact same request for an unknown outcome retry.")
    source_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_revision_id = serializers.UUIDField(allow_null=True, help_text="Null only when no manual revision exists.")
    node_key = serializers.CharField(trim_whitespace=False)
    value_path = serializers.ListField(child=serializers.JSONField())
    operation = serializers.ChoiceField(choices=("set", "reset"))
    value = serializers.JSONField(required=False, allow_null=True, help_text="Required for set, forbidden for reset. Raw typed JSON, never trimmed or composed.")


class ShotFieldRevisionWriteResultSerializer(serializers.Serializer):
    detail = ShotFieldRevisionDetailSerializer()
    replayed = serializers.BooleanField()
