from rest_framework import serializers

from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.services.execution_field_bulk_review import BULK_FIELD_REVIEW_LIMIT


class ExecutionBulkFieldPreviewRequestSerializer(ExecutionAuthoringSerializer):
    preparation_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=BULK_FIELD_REVIEW_LIMIT)


class ExecutionBulkFieldReceiptSerializer(ExecutionAuthoringSerializer):
    preparation_id = serializers.UUIDField()
    expected_preparation_revision = serializers.IntegerField(min_value=1)
    preview_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class ExecutionBulkFieldConfirmRequestSerializer(ExecutionAuthoringSerializer):
    items = ExecutionBulkFieldReceiptSerializer(many=True, allow_empty=False, max_length=BULK_FIELD_REVIEW_LIMIT)


class ExecutionBulkFieldSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    field_key = serializers.CharField()
    label = serializers.CharField()
    state = serializers.ChoiceField(choices=("selected", "candidate", "blocked"))
    reason = serializers.CharField(allow_blank=True)
    value_id = serializers.UUIDField(allow_null=True)
    revision = serializers.IntegerField(allow_null=True)
    text = serializers.CharField(allow_blank=True)
    warnings = serializers.ListField(child=serializers.CharField())


class ExecutionBulkFieldPreviewItemSerializer(ExecutionBulkFieldReceiptSerializer):
    label = serializers.CharField()
    fields = ExecutionBulkFieldSerializer(many=True)
    ready = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


class ExecutionBulkFieldPreviewSerializer(serializers.Serializer):
    items = ExecutionBulkFieldPreviewItemSerializer(many=True)


class ExecutionBulkFieldResultItemSerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)


class ExecutionBulkFieldResultSerializer(serializers.Serializer):
    confirmed_field_count = serializers.IntegerField(min_value=1)
    items = ExecutionBulkFieldResultItemSerializer(many=True)
