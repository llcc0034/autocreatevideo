from __future__ import annotations

import hashlib
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    GeneratedArtifact,
    GenerationAttempt,
    ProductionRun,
    ScriptRevision,
    StageRun,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.script_sources import confirm_script_revision


REVIEW_PAYLOAD_KINDS = frozenset({
    "generated_script_preview",
    "structured_script_preview",
})
def _text_sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _verified_snapshot(stage: StageRun) -> dict[str, Any]:
    value = verify_json_contract(
        stage.output_snapshot,
        schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes,
        sha256=stage.output_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    ).value
    if not isinstance(value, dict):
        raise ValidationError("剧本候选阶段输出快照必须是对象")
    return value


def _frozen_review_candidate(
    run: ProductionRun,
    review_stage: StageRun,
) -> tuple[StageRun | None, dict[str, Any] | None]:
    upstream = list(
        run.stages.select_for_update().filter(
            ordinal__lt=review_stage.ordinal,
            status=StageRun.Status.SUCCEEDED,
            stage_key__in=("script_generate", "script_structure"),
        ).order_by("-ordinal")
    )
    for source_stage in upstream:
        snapshot = _verified_snapshot(source_stage)
        payload = snapshot.get("review_payload")
        if not isinstance(payload, dict):
            payload = snapshot
        if payload.get("kind") in REVIEW_PAYLOAD_KINDS:
            return source_stage, payload
    return None, None


def _metadata_field_evidence(
    *,
    requested: bool,
    base_available: bool,
    base_value: str,
    before_value: str,
    candidate_value: str,
) -> tuple[str, dict[str, Any]]:
    status = "not_adopted"
    reason = "not_requested"
    after_value = before_value
    if requested:
        if not base_available:
            reason = "base_snapshot_missing"
        elif before_value != base_value:
            # A human edit after run creation always wins, including when it
            # happens to equal the candidate. The service does not claim that
            # value as its own write.
            reason = "concurrent_edit"
        else:
            status = "adopted"
            reason = "already_equal" if before_value == candidate_value else "frozen_candidate"
            after_value = candidate_value
    return after_value, {
        "status": status,
        "reason": reason,
        "base_sha256": _text_sha256(base_value) if base_available else "",
        "before_sha256": _text_sha256(before_value),
        "candidate_sha256": _text_sha256(candidate_value),
        "after_sha256": _text_sha256(after_value),
    }


def approve_frozen_script_candidate(
    *,
    run: ProductionRun,
    review_stage: StageRun,
    user,
    adopt_generated_metadata: bool,
) -> dict[str, Any]:
    """Apply only a frozen upstream candidate and return append-only evidence.

    The caller must run inside the same transaction that persists the
    ApprovalDecision. Project title/logline changes use compare-with-frozen-base
    semantics: concurrent human edits are preserved and recorded, not treated
    as a reason to overwrite or silently fail the script approval.
    """

    source_stage, payload = _frozen_review_candidate(run, review_stage)
    if adopt_generated_metadata and payload is None:
        raise ValidationError("当前 script_review 没有可采用的冻结 AI title/logline 候选")
    project = type(run.project).objects.select_for_update().get(pk=run.project_id)
    inputs = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    base_title_available = "project_base_title" in inputs
    base_logline_available = "project_base_logline" in inputs
    candidate_title = project.title
    candidate_logline = project.logline
    revision = run.script_revision
    artifact: GeneratedArtifact | None = None

    if payload is not None:
        revision_id = str(payload.get("script_revision_id") or "")
        if not revision_id or revision is None or revision_id != str(revision.id):
            raise ValidationError("冻结剧本候选与 ProductionRun 当前 revision 不一致")
        revision = ScriptRevision.objects.select_for_update().select_related("source").get(pk=revision.pk)
        if (
            revision.content_sha256 != payload.get("content_sha256")
            or revision.content_size_bytes != payload.get("content_size_bytes", revision.content_size_bytes)
        ):
            raise ValidationError("冻结剧本候选正文 hash 或大小已漂移")
        candidate_title = str(payload.get("title") or "")
        candidate_logline = str(payload.get("logline") or "")
        project_title_max = project._meta.get_field("title").max_length
        if not candidate_title or len(candidate_title) > project_title_max:
            raise ValidationError("冻结剧本候选 title/logline 无效")
        artifact = GeneratedArtifact.objects.select_for_update().filter(
            pk=payload.get("artifact_id"),
            production_run=run,
            kind=GeneratedArtifact.Kind.SCRIPT,
            sha256=revision.content_sha256,
        ).first()
        if artifact is None:
            raise ValidationError("冻结剧本候选缺少匹配的 GeneratedArtifact")
        manifest = verify_json_contract(
            artifact.manifest_metadata,
            schema_version=artifact.manifest_schema_version,
            size_bytes=artifact.manifest_metadata_size_bytes,
            sha256=artifact.manifest_metadata_sha256,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
        ).value
        if (
            not isinstance(manifest, dict)
            or manifest.get("script_revision_id") != str(revision.id)
        ):
            raise ValidationError("冻结剧本候选 artifact manifest 与 revision 不一致")
        attempt_id = payload.get("attempt_id")
        if attempt_id is not None:
            attempt = GenerationAttempt.objects.select_for_update().filter(
                pk=attempt_id,
                stage_run=source_stage,
                status=GenerationAttempt.Status.SUCCEEDED,
                result_sha256=payload.get("result_sha256"),
            ).first()
            if (
                attempt is None
                or revision.origin_generation_attempt_id != attempt.id
                or artifact.generation_attempt_id != attempt.id
                or artifact.stage_run_id != source_stage.id
                or manifest.get("generation_attempt_id") != str(attempt.id)
            ):
                raise ValidationError("冻结剧本候选 attempt/result provenance 无效")
            if manifest.get("operation") == "script.structure" and (
                manifest.get("result_sha256") != payload.get("result_sha256")
                or manifest.get("candidate_sha256") != payload.get("candidate_sha256")
                or manifest.get("source_revision_id") != payload.get("source_revision_id")
            ):
                raise ValidationError("结构化剧本 artifact provenance 已漂移")
        elif payload.get("submission_semantics") != "no_submission":
            raise ValidationError("无 attempt 的剧本候选必须明确标记 no_submission")
        elif artifact.generation_attempt_id is not None:
            raise ValidationError("no_submission 剧本候选不能绑定 generation attempt")

    if revision is None:
        raise ValidationError("剧本审批前缺少 ScriptRevision")

    if revision.status == ScriptRevision.Status.PREVIEW:
        if payload is None:
            raise ValidationError("preview ScriptRevision 缺少冻结的上游候选")
        current_master_sha256 = _text_sha256(project.master_script)
        frozen_master_sha256 = str(inputs.get("project_base_master_script_sha256") or "")
        if inputs.get("script_mode") == "ai":
            if project.master_script and project.master_script != revision.content:
                raise ValidationError("项目母稿已被另一条生产链更新；禁止并发覆盖")
        elif not frozen_master_sha256 or current_master_sha256 != frozen_master_sha256:
            raise ValidationError("项目母稿已被另一条生产链更新；禁止并发覆盖")
        revision = confirm_script_revision(revision, user=user, allow_generated=True)
        project.master_script = revision.content
    elif revision.status != ScriptRevision.Status.CONFIRMED:
        raise ValidationError("冻结剧本候选 revision 当前不可确认")

    if artifact is not None:
        if artifact.status not in {
            GeneratedArtifact.Status.AVAILABLE,
            GeneratedArtifact.Status.SELECTED,
        }:
            raise ValidationError("冻结剧本候选 artifact 当前不可选择")
        if artifact.status != GeneratedArtifact.Status.SELECTED:
            artifact.status = GeneratedArtifact.Status.SELECTED
            artifact.save(update_fields=("status", "updated_at"))

    next_title, title_evidence = _metadata_field_evidence(
        requested=bool(adopt_generated_metadata and payload is not None),
        base_available=base_title_available,
        base_value=str(inputs.get("project_base_title") or ""),
        before_value=project.title,
        candidate_value=candidate_title,
    )
    next_logline, logline_evidence = _metadata_field_evidence(
        requested=bool(adopt_generated_metadata and payload is not None),
        base_available=base_logline_available,
        base_value=str(inputs.get("project_base_logline") or ""),
        before_value=project.logline,
        candidate_value=candidate_logline,
    )
    project.title = next_title
    project.logline = next_logline
    update_fields = ["title", "logline", "updated_at"]
    if revision.status == ScriptRevision.Status.CONFIRMED and project.master_script == revision.content:
        update_fields.insert(2, "master_script")
    project.save(update_fields=tuple(update_fields))
    run.project = project
    run.script_revision = ScriptRevision.objects.get(pk=revision.pk)
    run.save(update_fields=("script_revision", "updated_at"))

    evidence = {
        "schema_version": 1,
        "kind": "script_review_decision",
        "source_stage_id": str(source_stage.id) if source_stage is not None else None,
        "source_stage_key": source_stage.stage_key if source_stage is not None else "script_input",
        "source_snapshot_sha256": source_stage.output_snapshot_sha256 if source_stage is not None else "",
        "script_revision_id": str(revision.id),
        "script_content_sha256": revision.content_sha256,
        "candidate_kind": str(payload.get("kind") or "confirmed_script") if payload else "confirmed_script",
        "candidate_sha256": str(payload.get("candidate_sha256") or "") if payload else "",
        "metadata_adoption": {
            "requested": bool(adopt_generated_metadata),
            "title": title_evidence,
            "logline": logline_evidence,
        },
    }
    return json_contract(
        evidence,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    ).value
