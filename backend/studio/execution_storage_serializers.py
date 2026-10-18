"""Recorded metadata only. Complete prompt text retains its existing API."""
from rest_framework import serializers
from studio.execution_prompt_serializers import ExecutionPromptQuerySerializer


class ExecutionStorageMetricsQuerySerializer(ExecutionPromptQuerySerializer):
    pass


class ExecutionStorageMetricSerializer(serializers.Serializer):
    key = serializers.ChoiceField(choices=("intent", "resolved_inputs", "review_contract", "evaluation", "prepared_request"))
    label = serializers.CharField()
    state = serializers.ChoiceField(choices=("recorded", "not_generated"))
    basis = serializers.ChoiceField(choices=("canonical_json_utf8", "canonical_request_file_utf8"))
    schema_version = serializers.IntegerField(min_value=1, allow_null=True)
    size_bytes = serializers.IntegerField(min_value=2, allow_null=True)
    sha256 = serializers.CharField(allow_null=True)
    current_limit_bytes = serializers.IntegerField(min_value=1)
    over_current_limit_bytes = serializers.IntegerField(min_value=0, allow_null=True)


class ExecutionStorageMetricsSerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    preparation_revision = serializers.IntegerField(min_value=1)
    observed_at = serializers.DateTimeField()
    items = ExecutionStorageMetricSerializer(many=True)
