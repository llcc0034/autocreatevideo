"""Read-only projection of a shot's bound original structured fields."""
from rest_framework import serializers


class ShotStructuredSourceSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    structure_id = serializers.UUIDField(help_text="Original ScriptRevisionStructure identity, not today's selected revision.")
    stage_id = serializers.UUIDField()
    episode_number = serializers.IntegerField(min_value=1)
    shot_number = serializers.IntegerField(min_value=1)
    document_sha256 = serializers.CharField()
    materialization_sha256 = serializers.CharField()


class ShotSourceFieldSerializer(serializers.Serializer):
    structure_version_id = serializers.UUIDField()
    node_key = serializers.CharField()
    field_definition = serializers.JSONField(help_text="Exact frozen field identity, definition version and metadata; never the current registry.")
    source_path = serializers.CharField()
    value_path = serializers.ListField(child=serializers.JSONField(), help_text="Original string keys and integer array indexes.")
    present = serializers.BooleanField()
    source_present = serializers.BooleanField()
    default_paths = serializers.ListField(child=serializers.CharField())
    value = serializers.JSONField(required=False, allow_null=True, help_text="Exact original parsed value. Omitted when present=false; null, empty text, zero and false are preserved.")

    def to_representation(self, instance):
        result = super().to_representation(instance)
        # DRF's allow_null fills an absent attribute with None on output. The
        # source reader deliberately omits missing values; keep that distinction.
        if "value" not in instance:
            result.pop("value", None)
        return result


class ShotSourceFieldsSerializer(serializers.Serializer):
    source = ShotStructuredSourceSerializer()
    fields = ShotSourceFieldSerializer(many=True)
