"""Bounded exact-successor selection; clients never submit replacement facts."""
from rest_framework import serializers
from studio.execution_instance_serializers import ExecutionAuthoringSerializer


class ConfigurationReplacementRequestSerializer(ExecutionAuthoringSerializer):
    version_id = serializers.UUIDField()
    expected_preparation_revision = serializers.IntegerField(min_value=1)


class ConfigurationReplacementApplySerializer(ConfigurationReplacementRequestSerializer):
    preview_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class ConfigurationSuccessorSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    version = serializers.IntegerField()
    name = serializers.CharField()
    profile_name = serializers.CharField()
    profile_version = serializers.IntegerField()
    definition_sha256 = serializers.CharField()


class ConfigurationReplacementOptionsSerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    revision = serializers.IntegerField()
    allowed = serializers.BooleanField()
    reason = serializers.CharField()
    replacement_id = serializers.UUIDField(allow_null=True)
    versions = ConfigurationSuccessorSerializer(many=True)


class ConfigurationChangeSerializer(serializers.Serializer):
    path = serializers.CharField()
    before = serializers.JSONField(allow_null=True)
    after = serializers.JSONField(allow_null=True)


class ConfigurationReplacementPreviewSerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    version_id = serializers.UUIDField()
    revision = serializers.IntegerField()
    preview_sha256 = serializers.CharField()
    changes = ConfigurationChangeSerializer(many=True)


class ConfigurationReplacementResultSerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    replacement_id = serializers.UUIDField()
    version_id = serializers.UUIDField()
