from rest_framework import serializers

from studio.models import AssetReferenceSeries, AssetReferenceVersion
from studio.serializers import StrictSerializer


class AssetReferenceSeriesInput(StrictSerializer):
    name = serializers.CharField(max_length=160)
    primary_series_id = serializers.UUIDField(required=False, allow_null=True)
    bible_variant_id = serializers.UUIDField(required=False, allow_null=True)


class AssetReferenceSeriesCreate(AssetReferenceSeriesInput):
    role = serializers.ChoiceField(choices=AssetReferenceSeries.Role.choices)


class AssetReferenceVersionCreate(StrictSerializer):
    series_id = serializers.UUIDField()
    asset_id = serializers.UUIDField()
    source = serializers.ChoiceField(choices=(AssetReferenceVersion.Source.UPLOAD, AssetReferenceVersion.Source.EXISTING))
    setting_revision_id = serializers.UUIDField(required=False, allow_null=True)


class AssetReferencePageQuery(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)


class AssetReferenceSeriesQuery(AssetReferencePageQuery):
    role = serializers.ChoiceField(choices=AssetReferenceSeries.Role.choices, required=False)
    primary_series_id = serializers.UUIDField(required=False)
    bible_variant_id = serializers.UUIDField(required=False)
    search = serializers.CharField(max_length=160, required=False, allow_blank=True)
    media_type = serializers.ChoiceField(choices=("image", "video", "audio"), required=False)
    bible_linked = serializers.BooleanField(required=False)
    asset_id = serializers.UUIDField(required=False)


class AssetReferenceVersionQuery(AssetReferencePageQuery):
    series_id = serializers.UUIDField(required=True)


class AssetReferenceBibleQuery(AssetReferencePageQuery):
    search = serializers.CharField(max_length=160, required=False, allow_blank=True)
    id = serializers.UUIDField(required=False)


class AssetReferenceBibleOption(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    reference_name = serializers.CharField()
    project_id = serializers.UUIDField()
    project_title = serializers.CharField(source="project__title")
    character_name = serializers.CharField(source="character__name", allow_null=True)
    scene_name = serializers.CharField(source="scene__name", allow_null=True)
    prop_name = serializers.CharField(source="prop__name", allow_null=True)


class AssetReferenceRemovalImpact(serializers.Serializer):
    target_type = serializers.ChoiceField(choices=("series", "version"))
    id = serializers.UUIDField()
    name = serializers.CharField()
    version_count = serializers.IntegerField()
    asset_count = serializers.IntegerField()
    variant_count = serializers.IntegerField()
    dependent_version_count = serializers.IntegerField()
    production_use_count = serializers.IntegerField()
    can_remove = serializers.BooleanField()


class AssetReferenceSeriesSummary(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=AssetReferenceSeries.Role.choices)
    owner_id = serializers.IntegerField()
    primary_series_id = serializers.UUIDField(allow_null=True)
    bible_variant_id = serializers.UUIDField(allow_null=True)
    version_count = serializers.IntegerField()
    variant_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class AssetReferenceAssetSummary(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.CharField()
    mime_type = serializers.CharField()
    is_active = serializers.BooleanField()


class AssetReferenceArtifactOrigin(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    production_run_id = serializers.UUIDField(allow_null=True)
    generation_attempt_id = serializers.UUIDField(allow_null=True)


class AssetReferenceJobOrigin(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()


class AssetReferenceVersionSummary(serializers.Serializer):
    id = serializers.UUIDField()
    series_id = serializers.UUIDField()
    version = serializers.IntegerField()
    asset = AssetReferenceAssetSummary()
    source = serializers.ChoiceField(choices=AssetReferenceVersion.Source.choices)
    setting_revision_id = serializers.UUIDField(allow_null=True)
    generated_from_id = serializers.UUIDField(allow_null=True)
    origin_history_cleared = serializers.BooleanField()
    generated_artifact = AssetReferenceArtifactOrigin(allow_null=True)
    generation_job = AssetReferenceJobOrigin(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
