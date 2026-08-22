from __future__ import annotations

import unicodedata
import re

from drf_spectacular.extensions import OpenApiSerializerExtension
from rest_framework import serializers

from studio.models import GenerationTask
from studio.services.prompt_quality_gate import BIBLE_IMAGE_CANDIDATE_QUALITY_POLICY

PROMPT_OVERRIDE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def _validate_plain_text(value: str, *, field_name: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise serializers.ValidationError(f"{field_name} 不能包含控制字符或换行。")
    return value


class BibleImageCandidateRequestSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(
        min_length=1, max_length=120, trim_whitespace=False,
    )
    extra_direction = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2_000,
        trim_whitespace=False,
    )
    prompt_overrides = serializers.DictField(
        child=serializers.CharField(
            allow_blank=True, max_length=4_000, trim_whitespace=False,
        ),
        required=False,
        default=dict,
        help_text="本次生成使用的提示词来源字段覆盖值；只影响当前候选并写入冻结来源。",
    )
    seed = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=9_223_372_036_854_775_807,
    )
    priority = serializers.IntegerField(min_value=-100, max_value=100, default=0)
    max_attempts = serializers.IntegerField(min_value=1, max_value=10, default=3)
    extended_generation_confirmed = serializers.BooleanField(
        default=False,
        help_text="队列超过推荐值时显式继续；不提高 worker 并发。",
    )

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("请求体必须是 JSON 对象。")
        allowed = set(self.fields)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise serializers.ValidationError({
                key: "不支持的字段；模板、模型和工作流均由服务端固定选择。"
                for key in unknown
            })
        return super().to_internal_value(data)

    def validate_idempotency_key(self, value: str) -> str:
        _validate_plain_text(value, field_name="idempotency_key")
        if value != value.strip():
            raise serializers.ValidationError("idempotency_key 首尾不能包含空白。")
        return value

    def validate_extra_direction(self, value: str) -> str:
        return _validate_plain_text(value, field_name="extra_direction")

    def validate_prompt_overrides(self, value: dict) -> dict:
        if len(value) > 80:
            raise serializers.ValidationError("prompt_overrides 最多 80 个字段。")
        normalized = {}
        for key, text in value.items():
            if not isinstance(key, str) or not key.strip():
                raise serializers.ValidationError("prompt_overrides 字段名必须是非空字符串。")
            if not PROMPT_OVERRIDE_KEY_RE.fullmatch(key):
                raise serializers.ValidationError(f"不支持的提示词字段名：{key}")
            normalized[key] = _validate_plain_text(
                str(text),
                field_name=f"prompt_overrides.{key}",
            )
        return normalized


class BibleImagePromptConfigSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=1)
    style_prompt = serializers.CharField(allow_blank=True)
    style_negative_prompt = serializers.CharField(allow_blank=True)
    composition_rules = serializers.DictField(child=serializers.CharField(allow_blank=True))
    negative_rules = serializers.DictField(child=serializers.CharField(allow_blank=True))
    quality_suffix = serializers.CharField(allow_blank=True)


class BibleImagePromptInputSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    value = serializers.CharField(allow_blank=True)
    role = serializers.ChoiceField(choices=("positive", "negative"))
    source = serializers.CharField()
    editable = serializers.BooleanField()


class BibleImagePromptInputsSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=1)
    positive = BibleImagePromptInputSerializer(many=True)
    negative = BibleImagePromptInputSerializer(many=True)
    final_prompt_text = serializers.CharField(allow_blank=True)
    negative_prompt = serializers.CharField(allow_blank=True)


class BibleImageAssetSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()
    checksum = serializers.CharField(allow_blank=True)
    preview_url = serializers.CharField(allow_blank=True)


class BibleImageQualityIssueSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[a-z][a-z0-9_]{0,99}$")
    severity = serializers.ChoiceField(choices=("info", "warning", "error"))
    path = serializers.CharField()
    message = serializers.CharField()


class BibleImageCanonicalEntitySerializer(serializers.Serializer):
    scope_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    scope_id = serializers.UUIDField()
    name = serializers.CharField()
    canonical_target = serializers.CharField()


class BibleImageCanonicalCollisionSerializer(serializers.Serializer):
    code = serializers.CharField()
    normalized_target = serializers.CharField()
    entities = BibleImageCanonicalEntitySerializer(many=True)


class BibleImageOptimizerSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("not_configured", "not_required"))
    enabled = serializers.BooleanField()
    provider = serializers.CharField()
    model = serializers.CharField(allow_blank=True)
    transport = serializers.ChoiceField(choices=("local",))
    failure_mode = serializers.ChoiceField(choices=("warn",))
    message = serializers.CharField()
    validation_errors = serializers.ListField(child=serializers.CharField())


class BibleImageProductionLanguageGateSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("advisory",))
    would_block_enqueue = serializers.BooleanField()
    message = serializers.CharField()


class BibleImageCandidateQualitySerializer(serializers.Serializer):
    policy_id = serializers.ChoiceField(
        choices=(BIBLE_IMAGE_CANDIDATE_QUALITY_POLICY,),
    )
    status = serializers.ChoiceField(choices=("ready", "needs_attention"))
    advisory_only = serializers.BooleanField()
    would_block_enqueue = serializers.BooleanField()
    prompt_character_count = serializers.IntegerField(min_value=0)
    prompt_word_count = serializers.IntegerField(min_value=0)
    prompt_word_count_is_approximate = serializers.BooleanField()
    visual_fallback_source = serializers.CharField()
    issues = BibleImageQualityIssueSerializer(many=True)
    warnings = serializers.ListField(child=serializers.CharField())
    integrity_errors = BibleImageCanonicalCollisionSerializer(many=True)
    fields_requiring_english_rewrite = serializers.ListField(
        child=serializers.CharField(),
    )
    hard_block_fields = serializers.ListField(child=serializers.CharField())
    source_fields_optimized = serializers.ListField(child=serializers.CharField())
    optimizer = BibleImageOptimizerSerializer()
    production_language_gate = BibleImageProductionLanguageGateSerializer()


class BibleImageOutputMediaSerializer(serializers.Serializer):
    index = serializers.IntegerField(min_value=0)
    kind = serializers.ChoiceField(choices=("image", "video", "audio"))
    filename = serializers.CharField()
    preview_url = serializers.CharField(allow_blank=True)
    download_url = serializers.CharField(allow_blank=True)
    ai_generated = serializers.BooleanField()
    provenance_url = serializers.CharField(allow_blank=True)
    artifact_sha256 = serializers.CharField(allow_blank=True)
    manifest_sha256 = serializers.CharField(allow_blank=True)
    distribution_status = serializers.CharField()
    distribution_error = serializers.CharField(allow_blank=True)
    public_provenance_url = serializers.CharField(allow_blank=True)
    public_artifact_sha256 = serializers.CharField(allow_blank=True)
    public_manifest_sha256 = serializers.CharField(allow_blank=True)
    public_label = serializers.CharField(allow_blank=True)
    public_version = serializers.CharField(allow_blank=True)
    publish_url = serializers.CharField(allow_blank=True)
    promoted_asset_id = serializers.CharField(allow_blank=True)
    promote_to_asset_url = serializers.CharField(allow_blank=True)


class BibleImageLatestCandidateSerializer(serializers.Serializer):
    prompt_artifact_id = serializers.UUIDField()
    generated_workflow_id = serializers.UUIDField(allow_null=True)
    generation_task_id = serializers.UUIDField(allow_null=True)
    task_status = serializers.ChoiceField(
        choices=(*GenerationTask.Status.values, "incomplete"),
    )
    created_at = serializers.DateTimeField()
    idempotency_key = serializers.CharField()
    request_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    source_current = serializers.BooleanField()
    quality = BibleImageCandidateQualitySerializer()
    prompt_inputs = BibleImagePromptInputsSerializer()
    output_media = BibleImageOutputMediaSerializer(many=True)
    promoted_assets = BibleImageAssetSummarySerializer(many=True)
    review_required = serializers.BooleanField()
    persisted_as_asset = serializers.BooleanField()


class BibleImageCandidateProjectionSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()
    entity_name = serializers.CharField()
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    current_prompt_inputs = BibleImagePromptInputsSerializer()
    anchor_asset = BibleImageAssetSummarySerializer(allow_null=True)
    reference_assets = BibleImageAssetSummarySerializer(many=True)
    latest_candidate = BibleImageLatestCandidateSerializer(allow_null=True)
    candidate_history = BibleImageLatestCandidateSerializer(many=True)


class BibleImageCandidateCountsSerializer(serializers.Serializer):
    characters = serializers.IntegerField(min_value=0)
    scenes = serializers.IntegerField(min_value=0)
    props = serializers.IntegerField(min_value=0)
    total = serializers.IntegerField(min_value=0)
    with_candidate = serializers.IntegerField(min_value=0)


class BibleImageCandidatesResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    prompt_config = BibleImagePromptConfigSerializer()
    candidates = BibleImageCandidateProjectionSerializer(many=True)
    counts = BibleImageCandidateCountsSerializer()


class BibleImageCandidateCreateResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    prompt_artifact_id = serializers.UUIDField()
    generated_workflow_id = serializers.UUIDField()
    generation_task_id = serializers.UUIDField()
    idempotent_replay = serializers.BooleanField()
    review_required = serializers.BooleanField()
    persisted_as_asset = serializers.BooleanField()
    candidate = BibleImageCandidateProjectionSerializer()


class BibleImageCandidateErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    idempotency_key = serializers.CharField(required=False)
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)
    current_source_fingerprint = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False,
    )
    requested_task_count = serializers.IntegerField(required=False, min_value=0)
    active_task_count = serializers.IntegerField(required=False, min_value=0)
    user_active_task_count = serializers.IntegerField(required=False, min_value=0)
    recommended_max_pending = serializers.IntegerField(required=False, min_value=0)
    recommended_max_active_per_user = serializers.IntegerField(required=False, min_value=0)
    max_concurrent = serializers.IntegerField(required=False, min_value=0)


class BibleImageCandidateErrorEnvelopeBodySerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = BibleImageCandidateErrorSerializer()


class BibleImageCandidateErrorResponseSerializer(serializers.Serializer):
    error = BibleImageCandidateErrorEnvelopeBodySerializer()


class BibleImageCandidateRequestSchemaExtension(OpenApiSerializerExtension):
    target_class = BibleImageCandidateRequestSerializer

    def map_serializer(self, auto_schema, direction):
        schema = auto_schema._map_serializer(
            self.target, direction, bypass_extensions=True,
        )
        schema["additionalProperties"] = False
        return schema


class _ClosedBibleImageObjectSchemaExtension(OpenApiSerializerExtension):
    def map_serializer(self, auto_schema, direction):
        schema = auto_schema._map_serializer(
            self.target, direction, bypass_extensions=True,
        )
        schema["additionalProperties"] = False
        return schema


class BibleImageQualityIssueSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageQualityIssueSerializer


class BibleImageCanonicalEntitySchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageCanonicalEntitySerializer


class BibleImageCanonicalCollisionSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageCanonicalCollisionSerializer


class BibleImageOptimizerSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageOptimizerSerializer


class BibleImageProductionLanguageGateSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageProductionLanguageGateSerializer


class BibleImageCandidateQualitySchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageCandidateQualitySerializer


class BibleImagePromptConfigSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImagePromptConfigSerializer


class BibleImagePromptInputSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImagePromptInputSerializer


class BibleImagePromptInputsSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImagePromptInputsSerializer


class BibleImageOutputMediaSchemaExtension(_ClosedBibleImageObjectSchemaExtension):
    target_class = BibleImageOutputMediaSerializer
