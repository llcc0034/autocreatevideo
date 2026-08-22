from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from studio.services.series_concept_drafts import (
    SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
    SERIES_CONCEPT_MAX_IDEA_CHARS,
    SERIES_CONCEPT_MAX_STYLE_HINT_CHARS,
    SERIES_CONCEPT_TEMPLATE_ID,
)


class SeriesConceptDraftRequestSerializer(serializers.Serializer):
    idea = serializers.CharField(
        trim_whitespace=True,
        max_length=SERIES_CONCEPT_MAX_IDEA_CHARS,
    )
    provider = serializers.ChoiceField(choices=("local_qwen", "deepseek_api"))
    episode_count = serializers.IntegerField(
        min_value=1, default=3,
    )
    shots_per_episode = serializers.IntegerField(
        min_value=1, default=3,
    )
    episode_duration_seconds = serializers.IntegerField(
        min_value=1,
        default=settings.SERIES_CONCEPT_DEFAULT_EPISODE_DURATION_SECONDS,
    )
    style_hint = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=SERIES_CONCEPT_MAX_STYLE_HINT_CHARS,
        default="",
    )
    attempt = serializers.IntegerField(min_value=1, max_value=100, default=1)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({
                    key: "未知字段" for key in unknown
                })
        return super().to_internal_value(data)

    def validate(self, attrs):
        required_duration_ms = (
            attrs["shots_per_episode"] * settings.VIDEO_SHOT_MIN_DURATION_MS
        )
        if required_duration_ms > attrs["episode_duration_seconds"] * 1_000:
            raise serializers.ValidationError({
                "episode_duration_seconds": (
                    "单集时长不足以容纳当前镜头数；按生产最短镜头时长至少需要 "
                    f"{(required_duration_ms + 999) // 1_000} 秒。"
                ),
            })
        return attrs


class SeriesConceptBibleEntitySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    description = serializers.CharField(max_length=500)
    visual_identity_en = serializers.CharField(max_length=500)


class SeriesConceptBibleSerializer(serializers.Serializer):
    characters = SeriesConceptBibleEntitySerializer(many=True)
    scenes = SeriesConceptBibleEntitySerializer(many=True)
    props = SeriesConceptBibleEntitySerializer(many=True)


class SeriesConceptCandidateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=60)
    logline = serializers.CharField(max_length=500)
    master_script = serializers.CharField()
    bible = SeriesConceptBibleSerializer()


class SeriesConceptFormatSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(
        min_value=SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
        max_value=SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
    )
    template_id = serializers.ChoiceField(choices=(SERIES_CONCEPT_TEMPLATE_ID,))
    compatible = serializers.BooleanField()
    episode_count = serializers.IntegerField(min_value=1)
    shot_count = serializers.IntegerField(min_value=1)
    episode_duration_seconds = serializers.IntegerField(min_value=1)
    total_duration_ms = serializers.IntegerField(min_value=1)
    shot_durations_ms = serializers.ListField(
        child=serializers.ListField(
            child=serializers.IntegerField(
                min_value=settings.VIDEO_SHOT_MIN_DURATION_MS,
                max_value=settings.VIDEO_SHOT_MAX_DURATION_MS,
            ),
            allow_empty=False,
        ),
        allow_empty=False,
    )


class SeriesConceptResourceEstimateSerializer(serializers.Serializer):
    total_shots = serializers.IntegerField(min_value=1)
    estimated_input_chars = serializers.IntegerField(min_value=1)
    estimated_output_tokens = serializers.IntegerField(min_value=1)
    configured_output_token_hint = serializers.IntegerField(min_value=1)
    output_hint_utilization_percent = serializers.IntegerField(min_value=1)


class SeriesConceptRuntimeSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=("local_qwen", "deepseek_api"))
    model = serializers.CharField()
    transport = serializers.ChoiceField(choices=("local_process", "remote_api"))
    elapsed_ms = serializers.IntegerField(min_value=0, allow_null=True)
    review_required = serializers.BooleanField()
    persisted = serializers.BooleanField()


class SeriesConceptDraftResponseSerializer(serializers.Serializer):
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    candidate = SeriesConceptCandidateSerializer()
    format = SeriesConceptFormatSerializer()
    resource_estimate = SeriesConceptResourceEstimateSerializer()
    warnings = serializers.ListField(child=serializers.CharField())
    runtime = SeriesConceptRuntimeSerializer()

    def validate(self, attrs):
        format_data = attrs["format"]
        runtime = attrs["runtime"]
        estimate = attrs["resource_estimate"]
        durations = format_data["shot_durations_ms"]
        if (
            format_data["schema_version"] != SERIES_CONCEPT_DRAFT_SCHEMA_VERSION
            or format_data["template_id"] != SERIES_CONCEPT_TEMPLATE_ID
            or format_data["compatible"] is not True
            or runtime["review_required"] is not True
            or runtime["persisted"] is not False
        ):
            raise serializers.ValidationError("候选兼容性或人工审核标记无效。")
        if (
            len(durations) != format_data["episode_count"]
            or sum(len(row) for row in durations) != format_data["shot_count"]
            or format_data["shot_count"] % format_data["episode_count"] != 0
            or any(
                len(row)
                != format_data["shot_count"] // format_data["episode_count"]
                for row in durations
            )
            or any(
                sum(row) > format_data["episode_duration_seconds"] * 1_000
                for row in durations
            )
            or sum(sum(row) for row in durations) != format_data["total_duration_ms"]
        ):
            raise serializers.ValidationError("候选镜头时长与剧集资源摘要不一致。")
        expected_utilization = (
            estimate["estimated_output_tokens"] * 100
            + estimate["configured_output_token_hint"] - 1
        ) // estimate["configured_output_token_hint"]
        if (
            estimate["total_shots"] != format_data["shot_count"]
            or estimate["output_hint_utilization_percent"]
            != expected_utilization
        ):
            raise serializers.ValidationError("候选模型资源估算无效。")
        return attrs


class SeriesConceptDiagnosticUsageSerializer(serializers.Serializer):
    prompt_tokens = serializers.IntegerField(min_value=0, required=False)
    completion_tokens = serializers.IntegerField(min_value=0, required=False)
    total_tokens = serializers.IntegerField(min_value=0, required=False)


class SeriesConceptDiagnosticOutputSerializer(serializers.Serializer):
    character_count = serializers.IntegerField(min_value=0, required=False)
    utf8_byte_count = serializers.IntegerField(min_value=0, required=False)
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)
    redacted_json_shape = serializers.CharField(
        max_length=900, required=False, allow_blank=True,
    )
    json_decode_line = serializers.IntegerField(min_value=1, required=False)
    json_decode_column = serializers.IntegerField(min_value=1, required=False)
    json_decode_position = serializers.IntegerField(min_value=0, required=False)


class SeriesConceptDiagnosticDetailsSerializer(serializers.Serializer):
    attempts = serializers.IntegerField(min_value=0, required=False)
    elapsed_ms = serializers.IntegerField(min_value=0, required=False)
    http_status = serializers.IntegerField(min_value=100, max_value=599, required=False)
    incomplete = serializers.BooleanField(required=False)
    requested_max_tokens = serializers.IntegerField(min_value=1, required=False)
    configured_max_tokens = serializers.IntegerField(min_value=1, required=False)
    configured_timeout_seconds = serializers.FloatField(
        min_value=0, required=False,
    )
    validation_error_code = serializers.CharField(max_length=64, required=False)
    validation_path = serializers.CharField(max_length=160, required=False)
    repair_attempted = serializers.BooleanField(required=False)
    repair_succeeded = serializers.BooleanField(required=False)
    repair_reason_code = serializers.CharField(max_length=64, required=False)
    output = SeriesConceptDiagnosticOutputSerializer(required=False)


class SeriesConceptDiagnosticSerializer(serializers.Serializer):
    diagnostic_id = serializers.RegexField(r"^[0-9a-f]{32}$")
    provider = serializers.ChoiceField(
        choices=("local_qwen", "deepseek_api", "unknown"),
    )
    error_code = serializers.CharField(max_length=64)
    failure_phase = serializers.CharField(max_length=64)
    finish_reason = serializers.CharField(max_length=64, required=False)
    usage = SeriesConceptDiagnosticUsageSerializer(required=False)
    details_available = serializers.BooleanField()
    details = SeriesConceptDiagnosticDetailsSerializer()


class SeriesConceptDraftGenerationErrorDetailsSerializer(serializers.Serializer):
    detail = serializers.CharField()
    diagnostic = SeriesConceptDiagnosticSerializer()


class SeriesConceptDraftGenerationErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField(max_length=64)
    details = SeriesConceptDraftGenerationErrorDetailsSerializer()


class SeriesConceptDraftGenerationErrorEnvelopeSerializer(serializers.Serializer):
    error = SeriesConceptDraftGenerationErrorSerializer()
