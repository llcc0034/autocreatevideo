"""Response-only contracts for the existing project continuity action."""
from rest_framework import serializers


class ConsistencyPrimarySettingSerializer(serializers.Serializer):
    appearance_id = serializers.UUIDField()
    setting_revision_id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)
    content_sha256 = serializers.CharField()
    identity_anchor = serializers.CharField(allow_blank=True)
    appearance_anchor = serializers.CharField(allow_blank=True)


class ConsistencyAssetSnapshotSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField(allow_blank=True)
    local_path = serializers.CharField(allow_blank=True)
    checksum = serializers.CharField(allow_blank=True)


class ConsistencyBibleSnapshotSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()
    primary_setting = ConsistencyPrimarySettingSerializer(allow_null=True)
    negative_constraints = serializers.CharField(allow_blank=True)
    anchor_asset = ConsistencyAssetSnapshotSerializer(allow_null=True)
    reference_assets = ConsistencyAssetSnapshotSerializer(many=True)


class ConsistencyCharacterSnapshotSerializer(ConsistencyBibleSnapshotSerializer):
    speaker_id = serializers.CharField()
    age_appearance = serializers.CharField(allow_blank=True)
    face_identity = serializers.CharField(allow_blank=True)
    wardrobe = serializers.CharField(allow_blank=True)
    voice_profile = serializers.CharField(allow_blank=True)
    personality = serializers.CharField(allow_blank=True)
    visual_reference_policy = serializers.JSONField()


class ConsistencySceneSnapshotSerializer(ConsistencyBibleSnapshotSerializer):
    location = serializers.CharField(allow_blank=True)
    time_of_day = serializers.CharField(allow_blank=True)
    lighting = serializers.CharField(allow_blank=True)
    color_palette = serializers.CharField(allow_blank=True)
    ambient_sound = serializers.CharField(allow_blank=True)


class ConsistencyPropSnapshotSerializer(ConsistencyBibleSnapshotSerializer):
    material = serializers.CharField(allow_blank=True)
    distinctive_marks = serializers.CharField(allow_blank=True)
    continuity_state = serializers.CharField(allow_blank=True)


class ConsistencySnapshotLockSerializer(serializers.Serializer):
    scope = serializers.CharField()
    target_id = serializers.UUIDField(allow_null=True)
    key = serializers.CharField()
    value = serializers.JSONField()
    version = serializers.IntegerField(min_value=1)


class ConsistencySnapshotSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    language = serializers.CharField()
    continuity_seed = serializers.IntegerField()
    continuity_summary = serializers.CharField(allow_blank=True)
    model = serializers.CharField(allow_blank=True)
    prompt_settings = serializers.JSONField()
    style = serializers.JSONField(allow_null=True)
    characters = ConsistencyCharacterSnapshotSerializer(many=True)
    scenes = ConsistencySceneSnapshotSerializer(many=True)
    props = ConsistencyPropSnapshotSerializer(many=True)
    locks = ConsistencySnapshotLockSerializer(many=True)
