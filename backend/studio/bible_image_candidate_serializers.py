from __future__ import annotations

from rest_framework import serializers


class BibleImageAssetSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()
    checksum = serializers.CharField(allow_blank=True)
    preview_url = serializers.CharField(allow_blank=True, required=False, default="")


class BibleImageCandidateHistorySerializer(serializers.Serializer):
    artifact_id = serializers.UUIDField()
    production_run_id = serializers.UUIDField()
    stage_run_id = serializers.UUIDField()
    generation_attempt_id = serializers.UUIDField(allow_null=True)
    generation_task_id = serializers.UUIDField(allow_null=True)
    prompt_artifact_id = serializers.UUIDField(allow_null=True)
    workflow_version_id = serializers.UUIDField(allow_null=True)
    model_profile_version_id = serializers.UUIDField(allow_null=True)
    model_identifier = serializers.CharField(allow_blank=True)
    prompt_profile_version_id = serializers.UUIDField(allow_null=True)
    prompt_profile_binding_key = serializers.CharField(allow_blank=True)
    prompt_profile_contract_sha256 = serializers.CharField(allow_blank=True)
    source_snapshot_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$", allow_blank=True,
    )
    source_current = serializers.BooleanField()
    integrity_status = serializers.ChoiceField(choices=("valid", "invalid"))
    integrity_error_code = serializers.CharField(allow_blank=True)
    variant_group = serializers.CharField()
    variant_index = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(
        choices=("available", "selected", "rejected", "invalid", "legacy_missing"),
    )
    binding_role = serializers.ChoiceField(choices=("anchor", "reference", "unbound"))
    materialized_asset = BibleImageAssetSummarySerializer(allow_null=True)
    preview_url = serializers.CharField(allow_blank=True)
    download_url = serializers.CharField(allow_blank=True)
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    size_bytes = serializers.IntegerField(min_value=1)
    mime_type = serializers.CharField()
    created_at = serializers.DateTimeField()


class BibleImageCandidateProjectionSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()
    entity_name = serializers.CharField()
    anchor_asset = BibleImageAssetSummarySerializer(allow_null=True)
    reference_assets = BibleImageAssetSummarySerializer(many=True)
    latest_candidate = BibleImageCandidateHistorySerializer(allow_null=True)
    candidate_history = BibleImageCandidateHistorySerializer(many=True)


class BibleImageCandidateCountsSerializer(serializers.Serializer):
    characters = serializers.IntegerField(min_value=0)
    scenes = serializers.IntegerField(min_value=0)
    props = serializers.IntegerField(min_value=0)
    total = serializers.IntegerField(min_value=0)
    with_candidate = serializers.IntegerField(min_value=0)


class BibleImageCandidatesResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    candidates = BibleImageCandidateProjectionSerializer(many=True)
    counts = BibleImageCandidateCountsSerializer()


class BibleImageCandidateErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()


class BibleImageCandidateErrorEnvelopeBodySerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = BibleImageCandidateErrorSerializer()


class BibleImageCandidateErrorResponseSerializer(serializers.Serializer):
    error = BibleImageCandidateErrorEnvelopeBodySerializer()
