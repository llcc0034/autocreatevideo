from __future__ import annotations

from rest_framework import serializers


class AssetBibleDraftRequestSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    output_language = serializers.ChoiceField(choices=("zh", "en"), default="zh")
    asset_id = serializers.UUIDField()
    asset_checksum = serializers.CharField(max_length=128, trim_whitespace=True)
    attempt = serializers.IntegerField(min_value=1, max_value=100, default=1)


class AssetBibleDraftFieldsSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(max_length=2_000, allow_blank=True)
    visual_identity = serializers.CharField(max_length=3_000, allow_blank=True)


class AssetBibleDraftSourceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=("character", "scene", "prop", "reference"))
    name = serializers.CharField()
    checksum = serializers.CharField()
    updated_at = serializers.DateTimeField()
    metadata_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    metadata_fields_used = serializers.ListField(child=serializers.CharField())


class AssetBibleDraftRuntimeSerializer(serializers.Serializer):
    provider = serializers.CharField()
    model = serializers.CharField()
    loaded_on_demand = serializers.BooleanField()
    unloaded_after_request = serializers.BooleanField()
    review_required = serializers.BooleanField()
    persisted = serializers.BooleanField()
    pixel_vision_supported = serializers.BooleanField()
    elapsed_ms = serializers.IntegerField(allow_null=True, required=False)


class AssetBibleDraftResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    output_language = serializers.ChoiceField(choices=("zh", "en"))
    evidence_basis = serializers.ChoiceField(choices=("asset_metadata",))
    reference_mode = serializers.ChoiceField(choices=("single_reference",))
    pixel_vision_used = serializers.BooleanField()
    source_metadata_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    draft = AssetBibleDraftFieldsSerializer()
    source_assets = AssetBibleDraftSourceSerializer(many=True)
    warnings = serializers.ListField(child=serializers.CharField())
    runtime = AssetBibleDraftRuntimeSerializer()
