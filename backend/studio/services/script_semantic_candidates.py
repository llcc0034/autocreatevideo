"""Structured script results in the original GeneratedArtifact registry.

Shared M2 candidate component used by the original Artifact detail and review.
Native instance authoring/admission is a separate integration boundary.
The original worker/recovery transaction owns its locks, claim, retained response
and settlement. The public helpers read that call's frozen output themselves.
No parallel candidate table, result copy, selection or legacy-format fallback.
"""
from copy import deepcopy
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ScriptRevision, ScriptSource
from studio.services.business_outputs import verify_output_reference
from studio.services.execution_production_candidates import (
    new_production_text_candidate, read_production_text_candidate_envelope, save_production_text_candidate,
)
from studio.services.execution_structured_text import structured_text_warning_messages
from studio.services.script_revision_structures import create_structured_script_source, read_script_revision_structure
from studio.services.script_semantic_requests import SCRIPT_SEMANTIC_OPERATIONS, read_script_semantic_attempt_output
from studio.services.security_boundaries import security_boundary_resolution_cache


@transaction.atomic
@security_boundary_resolution_cache()
def save_script_semantic_candidate(attempt, stage, run, *, response_text, max_bytes, recovery=None):
    """Bind the candidate to the original frozen request before any source write."""
    output = read_script_semantic_attempt_output(attempt)
    return _save_script_semantic_candidate(attempt, stage, run, response_text=response_text,
        expected_output=output, max_bytes=max_bytes, recovery=recovery)


@transaction.atomic
def _save_script_semantic_candidate(attempt, stage, run, *, response_text, expected_output, max_bytes, recovery=None):
    """Commit source, revision, detail and original Artifact, or none of them.

    Caller has verified the frozen request and owns the Run/Stage/Attempt locks.
    Return the original result digest for that caller's success settlement.
    A duplicate attempt is rejected by the original Artifact uniqueness rules;
    its new rows roll back, without replacing the existing candidate.
    """
    preparation = attempt.preparation
    if (attempt.operation not in SCRIPT_SEMANTIC_OPERATIONS or attempt.role != "primary"
            or attempt.stage_run_id != stage.pk or stage.production_run_id != run.pk
            or preparation.stage_run_id != stage.pk or preparation.purpose != "production"
            or preparation.request_frozen_at is None or len(attempt.request_sha256) != 64
            or attempt.request_sha256 != preparation.request_sha256
            or attempt.execution_instance_version_id != preparation.execution_instance_version_id):
        raise ValidationError("结构化剧本候选必须属于同一运行、阶段和冻结主调用")
    output = verify_output_reference(expected_output)
    source, revision, detail = create_structured_script_source(project=run.project, user=run.owner,
        content=response_text, output=output, max_bytes=max_bytes,
        source_type=ScriptSource.SourceType.PASTE if recovery else ScriptSource.SourceType.AI_GENERATED)
    revision.origin_generation_attempt = attempt
    revision.save(update_fields=("origin_generation_attempt", "updated_at"))
    # Only a typed identity and checksum: raw text and full projections stay with
    # their original ScriptRevision/ScriptRevisionStructure owners.
    artifact, metadata, result = new_production_text_candidate(attempt, stage, run,
        result={"schema_version": 1, "script_revision_id": str(revision.pk),
            "script_structure_sha256": detail["document_sha256"]}, max_bytes=max_bytes, recovery=recovery)
    if recovery is not None and revision.content_sha256 != metadata["human_response_recovery"]["corrected_sha256"]:
        raise ValidationError("结构化候选不是已确认的补全文本")
    metadata.update(script_source_id=str(source.pk), script_revision_id=str(revision.pk))
    artifact.kind = "script"
    artifact.object_key = f"script-revisions/{revision.pk}"
    artifact.mime_type, artifact.size_bytes, artifact.sha256 = (
        "text/plain; charset=utf-8", revision.content_size_bytes, revision.content_sha256)
    save_production_text_candidate(artifact, metadata, max_bytes=max_bytes)
    return result.sha256


@security_boundary_resolution_cache()
def read_script_semantic_candidate(artifact, *, max_bytes, for_audit=False):
    """Verify the actual frozen request and read its saved candidate detail."""
    envelope = read_production_text_candidate_envelope(artifact, max_bytes=max_bytes, for_audit=for_audit)
    if envelope is None:
        raise ValidationError("结构化剧本候选缺少原生成来源")
    output = read_script_semantic_attempt_output(envelope[0])
    return _read_script_semantic_candidate(artifact, expected_output=output, max_bytes=max_bytes,
        for_audit=for_audit, envelope=envelope)


def _read_script_semantic_candidate(artifact, *, expected_output, max_bytes, for_audit=False, envelope=None):
    """Read exact saved detail, not a replay of parsing or current configuration.

    Caller owns visibility and passes the output from the verified original
    request. This component does not select or declare downstream readiness.
    """
    output = verify_output_reference(expected_output)
    if envelope is None:
        envelope = read_production_text_candidate_envelope(artifact, max_bytes=max_bytes, for_audit=for_audit)
    if envelope is None:
        raise ValidationError("结构化剧本候选缺少原生成来源")
    attempt, metadata, result = envelope
    pointer = result.value
    if (attempt.operation not in SCRIPT_SEMANTIC_OPERATIONS or not isinstance(pointer, dict)
            or set(pointer) != {"schema_version", "script_revision_id", "script_structure_sha256"}
            or type(pointer["schema_version"]) is not int or pointer["schema_version"] != 1
            or pointer["script_revision_id"] != metadata["script_revision_id"]
            or artifact.manifest_key != f"run:{artifact.production_run_id}:prepared-text:{attempt.pk}"
            or artifact.variant_group != f"stage:{artifact.stage_run_id}:{sha256(attempt.unit_key.encode()).hexdigest()}"
            or artifact.variant_index != attempt.attempt_number):
        raise ValidationError("结构化候选缺少确切修订引用或与原调用身份不一致")
    if not ScriptRevision.objects.filter(pk=metadata["script_revision_id"], source_id=metadata["script_source_id"],
            origin_generation_attempt_id=attempt.pk).exists():
        raise ValidationError("结构化候选与原剧本修订的调用来源不一致")
    detail = read_script_revision_structure(revision_id=metadata["script_revision_id"],
        project_id=artifact.production_run.project_id, max_bytes=max_bytes)
    document, text = detail["document"], detail["original_text"]
    if (document["output"] != output or detail["document_sha256"] != pointer["script_structure_sha256"]
            or artifact.sha256 != document["revision"]["content_sha256"]
            or artifact.size_bytes != document["revision"]["content_size_bytes"]
            or artifact.object_key != f'script-revisions/{metadata["script_revision_id"]}'
            or artifact.mime_type != "text/plain; charset=utf-8"):
        raise ValidationError("结构化候选的原文、详情或确切输出合同已漂移")
    recovery = metadata.get("human_response_recovery")
    if recovery is not None and sha256(text.encode("utf-8")).hexdigest() != recovery["corrected_sha256"]:
        raise ValidationError("结构化候选不是已确认的补全文本")
    projection = deepcopy(document["projection"])
    candidate = projection.pop("source_value")
    return {"operation": attempt.operation, "preparation_id": str(attempt.preparation_id),
        "result_sha256": result.sha256, "revision": deepcopy(document["revision"]),
        "output_contract": deepcopy(output), "projection": projection, "candidate": candidate,
        "json_schema": deepcopy(detail["json_schema"]),
        "warnings": structured_text_warning_messages(projection["warnings"]),
        "text": text, "human_response_recovery": deepcopy(recovery)}
