from rest_framework import serializers

from studio.asset_generation_serializers import AssetInitialFieldSerializer, AssetGenerationOptionsResponseSerializer
from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.execution_smoke_serializers import ExecutionSmokeFixtureSerializer
from studio.models import AssetGenerationJob, PromptFieldTranslation


class ProjectTranslationEntrySerializer(ExecutionAuthoringSerializer):
    scope_type = serializers.ChoiceField(choices=PromptFieldTranslation.ScopeType.choices)
    scope_id = serializers.UUIDField()
    field_key = serializers.CharField(max_length=160)
    source_text = serializers.CharField(trim_whitespace=False)
    expected_entry_id = serializers.UUIDField(allow_null=True)
    expected_revision = serializers.IntegerField(min_value=0)


class ProjectTranslationCreateSerializer(ExecutionAuthoringSerializer):
    execution_instance_version_id = serializers.UUIDField()
    expected_definition_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    entries = ProjectTranslationEntrySerializer(many=True, allow_empty=False)
    source_lang = serializers.ChoiceField(choices=("Chinese",))
    target_lang = serializers.ChoiceField(choices=("English",))
    request = ExecutionSmokeFixtureSerializer()
    field_values = AssetInitialFieldSerializer(many=True)
    allow_external_calls = serializers.BooleanField()


class ProjectTranslationRequestSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    job_id = serializers.UUIDField()
    preparation_id = serializers.UUIDField()
    execution_instance_version_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=AssetGenerationJob.Status.choices)
    created_at = serializers.DateTimeField()
    source_count = serializers.IntegerField(min_value=0)


class ProjectTranslationSourceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    position = serializers.IntegerField(min_value=0)
    scope_type = serializers.ChoiceField(choices=PromptFieldTranslation.ScopeType.choices)
    scope_id = serializers.UUIDField()
    field_key = serializers.CharField()
    expected_revision = serializers.IntegerField(min_value=0)
    adopted_entry_id = serializers.UUIDField(allow_null=True)
    adopted_revision = serializers.IntegerField(allow_null=True)
    adopted_at = serializers.DateTimeField(allow_null=True)


class ProjectTranslationCandidateSerializer(serializers.Serializer):
    source = ProjectTranslationSourceSerializer()
    source_text = serializers.CharField()
    text = serializers.CharField()
    current = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


class ProjectTranslationAdoptSerializer(ExecutionAuthoringSerializer):
    pass


class ProjectTranslationOptionsSerializer(AssetGenerationOptionsResponseSerializer):
    source_lang = serializers.CharField()
    target_lang = serializers.CharField()
    max_entries = serializers.IntegerField(min_value=1)
