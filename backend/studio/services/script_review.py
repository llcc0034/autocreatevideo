"""Explicit adoption of a verified original text candidate.

The original approval transaction owns the Run/Stage locks and decision.
No generation result is installed as the Run's script before this action.
"""
from hashlib import sha256

from django.core.exceptions import ValidationError

from studio.models import ScriptRevision, SeriesProject
from studio.services.execution_production_candidates import reviewed_text_candidate
from studio.services.generated_artifacts import select_artifact
from studio.services.script_sources import confirm_script_revision
from studio.services.production_script_baselines import read_script_adoption_baseline


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def approve_frozen_script_candidate(*, run, review_stage, user,
        text_candidate, expected_source_snapshot_sha256, script_content=None):
    source, artifact, detail = reviewed_text_candidate(run=run, review_stage=review_stage,
        selection=text_candidate, expected_source_snapshot_sha256=expected_source_snapshot_sha256)
    evidence = apply_verified_script_candidate(run=run, artifact=artifact, detail=detail,
        user=user, script_content=script_content)
    evidence["source_snapshot_sha256"] = source.output_snapshot_sha256
    return evidence


def apply_verified_script_candidate(*, run, artifact, detail, user, script_content=None):
    """Original confirmation/selection writer shared by explicit review roots.

    Caller owns Run/Stage locks, verifies the candidate and writes its original
    ApprovalDecision in the same transaction. This is not a new action endpoint.
    """
    from studio.services.production_runs import ProductionRunConflict

    if (artifact.production_run_id != run.pk or not artifact.generation_attempt_id
            or str(artifact.generation_attempt.preparation_id) != detail["preparation_id"]):
        raise ValidationError("剧本采用必须对应本轮已核验候选")
    if detail["operation"] not in {"script.generate", "script.structure"}:
        raise ValidationError("剧本审核不能采用其他职责的产物")
    if detail.get("adoption_block_reason"):
        raise ValidationError(detail["adoption_block_reason"])
    project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    prepared_baseline = read_script_adoption_baseline(artifact.generation_attempt.preparation)
    baseline = prepared_baseline["fields"]
    current_revision_id = str(run.script_revision_id) if run.script_revision_id else None
    if current_revision_id != prepared_baseline["script_revision_id"]:
        raise ProductionRunConflict({"detail": "本轮已采用的剧本修订在准备后变化，请核对后重新制作。"},
            code="script_candidate_source_drift")
    if _digest(project.master_script) != baseline["master_script"]:
        raise ProductionRunConflict({"detail": "项目母稿在候选生成后已修改；不能覆盖，请核对后重新制作。"},
            code="script_candidate_source_drift")
    revision = ScriptRevision.objects.select_for_update().get(
        pk=artifact.manifest_metadata["script_revision_id"])
    if revision.status != ScriptRevision.Status.PREVIEW:
        raise ValidationError("候选剧本已经被确认或失效，请重新读取审核状态")
    if detail.get("output_contract") is not None:
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.script_semantic_candidates import read_script_semantic_candidate

        # The public reader selected this contract from the frozen request.
        # Recheck the saved original before adoption, without parsing again.
        verified = read_script_semantic_candidate(artifact, max_bytes=execution_configuration_max_bytes())
        if any(detail.get(key) != verified[key] for key in (
                "operation", "preparation_id", "result_sha256", "revision", "output_contract", "text")):
            raise ValidationError("结构化剧本候选详情已变化，请重新读取后采用")
    else:
        raise ValidationError("剧本采用缺少原请求的确切结构化输出合同")
    original_revision = revision
    if script_content is not None:
        from studio.services.script_sources import create_script_revision
        from studio.services.script_revision_structures import save_script_revision_structure

        if not isinstance(script_content, str) or not script_content.strip() or detail.get("output_contract") is None:
            raise ValidationError({"script_content": "字段编辑须提交原候选确切合同下的完整JSON文本。"})
        if script_content != revision.content:
            # Human content is a new revision, not a second model result. The
            # enclosing approval snapshot records the original candidate link.
            revision = create_script_revision(source=revision.source, user=user,
                content=script_content, allow_generated=True)
            save_script_revision_structure(revision_id=revision.pk, project_id=run.project_id,
                output=detail["output_contract"], max_bytes=execution_configuration_max_bytes())
    revision = confirm_script_revision(revision, user=user, allow_generated=True)
    from studio.services.production_run_plans import confirmed_script_reference
    from studio.services.script_revision_structures import read_confirmed_script_structure

    reference = confirmed_script_reference(run.project_id, {"kind": "script_revision",
        "id": str(revision.pk), "expected_sha256": revision.content_sha256})
    structure_source = read_confirmed_script_structure(project_id=run.project_id,
        reference=reference, max_bytes=execution_configuration_max_bytes())["source"]
    project.master_script = revision.content
    project.save(update_fields=("master_script", "updated_at"))
    select_artifact(artifact, user=user)
    run.project, run.script_revision = project, revision
    run.save(update_fields=("script_revision", "updated_at"))
    return {"schema_version": 1, "kind": "script_review_decision",
        "source_stage_id": str(artifact.stage_run_id), "source_stage_key": artifact.stage_run.stage_key,
        "artifact_id": str(artifact.pk), "artifact_sha256": artifact.sha256,
        "preparation_id": detail["preparation_id"], "result_sha256": detail["result_sha256"],
        "script_revision_id": str(revision.pk), "script_content_sha256": revision.content_sha256,
        "structure_source": structure_source,
        "candidate_kind": artifact.role,
        **({"edited_from_revision_id": str(original_revision.pk)} if revision.pk != original_revision.pk else {})}
