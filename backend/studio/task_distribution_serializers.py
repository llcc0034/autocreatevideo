from rest_framework import serializers
from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.models import GenerationTask


class TaskDistributionRecoveryInputSerializer(ExecutionAuthoringSerializer):
    task_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=0)

    def to_internal_value(self, data):
        if isinstance(data, dict) and type(data.get("expected_revision")) is not int:
            raise serializers.ValidationError({"expected_revision": "必须提交实际整数修订"})
        return super().to_internal_value(data)


class TaskDistributionPhaseSerializer(serializers.Serializer):
    task_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=("image", "video"))
    state = serializers.ChoiceField(choices=GenerationTask.DistributionState.choices)
    revision = serializers.IntegerField(min_value=0)
    requested_at = serializers.DateTimeField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
