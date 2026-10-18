from rest_framework import serializers
from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.services.production_stage_input_amendments import POLICIES, SOURCE_POLICIES


class RecipeMappingPreviewInputSerializer(ExecutionAuthoringSerializer):
    stage_run_id = serializers.UUIDField()
    shot_id = serializers.UUIDField()
    candidate_number = serializers.IntegerField(min_value=1)
    expected_run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")


class RecipeMappingValueInputSerializer(serializers.Serializer):
    key = serializers.CharField()
    kind = serializers.ChoiceField(choices=("text", "json"))
    value = serializers.JSONField(allow_null=True)
    provenance = serializers.DictField()


class RecipeMappingPreviewSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=("inputs_validated",))
    run_plan_sha256 = serializers.CharField()
    definition_sha256 = serializers.CharField()
    stage_run_id = serializers.UUIDField()
    shot_id = serializers.UUIDField()
    candidate_number = serializers.IntegerField(min_value=1)
    route_index = serializers.IntegerField(min_value=0)
    execution_instance_version_id = serializers.UUIDField()
    review = serializers.JSONField(allow_null=True)
    excluded_reference_keys = serializers.ListField(child=serializers.CharField())
    inputs = serializers.ListField(child=serializers.DictField())
    value_inputs = RecipeMappingValueInputSerializer(many=True)
    notice = serializers.CharField()
    input_amendment = serializers.DictField(allow_null=True)
    effective_input_sources = serializers.DictField()


class StageInputSourceChangeSerializer(ExecutionAuthoringSerializer):
    route_index = serializers.IntegerField(min_value=0)
    input_key = serializers.CharField(max_length=191)
    source = serializers.DictField()


class StageInputAmendmentInputSerializer(ExecutionAuthoringSerializer):
    stage_run_id = serializers.UUIDField()
    expected_run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    action = serializers.ChoiceField(choices=("preview", "apply"))
    policy = serializers.ChoiceField(choices=POLICIES)
    source_changes = StageInputSourceChangeSerializer(many=True, required=False, allow_empty=False)
    expected_amendment_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", required=False)

    def validate(self, attrs):
        if (attrs["policy"] in SOURCE_POLICIES) != ("source_changes" in attrs):
            raise serializers.ValidationError({"source_changes": "明确来源修订必须提供修改项；对白定向规则不接受自定义来源"})
        if attrs["action"] == "apply" and "expected_amendment_sha256" not in attrs:
            raise serializers.ValidationError({"expected_amendment_sha256": "请先预览并确认精确修订内容"})
        return attrs


class StageInputAmendmentSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("proposed", "applied"))
    sha256 = serializers.CharField()
    evidence = serializers.DictField()
    id = serializers.UUIDField(allow_null=True)
    created_by = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField(allow_null=True)
    notice = serializers.CharField()
