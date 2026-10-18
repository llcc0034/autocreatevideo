"""Read exact selected values through their original, verified output protocol.

No new content store, current-candidate guess, or adoption occurs here. Generic
values come from the frozen result contract; script projections retain their
business review. Neither source masquerades as an authored literal.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ApprovalDecision, StageRun
from studio.services.execution_production_candidates import read_production_text_candidate
from studio.services.execution_selected_outputs import _selected_execution_artifact, _selection_reference


@transaction.atomic
def resolve_selected_execution_text(source, *, spec, owner, project_id, production_run_id, eligible_stage_ids):
    from studio.services.execution_input_resolution import _context_field
    from studio.services.production_runs import _verify_stage_snapshot

    fields = {"kind", "decision_id", "artifact_id", "output_key", "scope"}
    if set(source) not in (fields, fields | {"stage_key"}) or source["kind"] != "selected_output":
        raise ValidationError("已采用文本必须明确提供决定、产物、目录输出及项目范围")
    run, stage, artifact, decision = _selected_execution_artifact(decision_id=source["decision_id"],
        artifact_id=source["artifact_id"], owner=owner, production_run_id=production_run_id,
        eligible_stage_ids=eligible_stage_ids)
    from studio.services.execution_text_output_catalog import plan_text_output_options
    from studio.services.production_run_plans import get_run_execution_plan
    options = [row for row in plan_text_output_options(get_run_execution_plan(run))
        if row["output_key"] == source["output_key"] and row["stage_key"] == stage.stage_key]
    if len(options) != 1 or options[0]["kind"] != spec["kind"]:
        raise ValidationError("已采用文本的输出标识或类型不在受控目录中")
    option = options[0]
    structured = option.get("reader") == "structured_text"
    if set(source) != fields | ({"stage_key"} if structured else set()) or (
            structured and source["stage_key"] != stage.stage_key):
        raise ValidationError("已采用文本必须绑定确切上游步骤身份")
    if (stage.stage_key != option["stage_key"] or artifact.role != option["artifact_role"]
            or source["scope"] != {"kind": "project", "id": str(run.project_id)}
            or str(project_id) != str(run.project_id)):
        raise ValidationError("已采用文本与本轮项目、上游职责或目录输出不一致")
    detail = read_production_text_candidate(artifact)
    if structured:
        candidate = detail.get("candidate") if isinstance(detail, dict) else None
        if (not isinstance(candidate, dict) or "value" not in candidate or any(
                candidate.get(key) != option[key] for key in
                ("output_version_id", "output_contract_sha256", "structure_version_id"))):
            raise ValidationError("已采用通用文本与上游步骤的确切输出合同不一致")
        snapshot = _verify_stage_snapshot(stage, "output_snapshot")
        candidates = snapshot.get("review_payload")
        if (snapshot.get("stage_key") != stage.stage_key
                or snapshot.get("output_reference") != f"stage-candidates:{stage.pk}"
                or not isinstance(candidates, dict) or candidates.get("kind") != "execution_candidates"
                or candidates.get("stage_key") != stage.stage_key):
            raise ValidationError("已采用通用文本缺少完成步骤的正式候选快照")
        selected = candidates.get("selection")
        if not isinstance(selected, dict) or selected.get("decision_id") != str(decision.pk) or selected.get("artifact_id") != str(artifact.pk):
            raise ValidationError("已采用通用文本与完成步骤的选择快照不一致")
        return candidate["value"], {**_selection_reference(run, stage, artifact, decision),
            "output_key": option["output_key"], "scope": source["scope"], "artifact_sha256": artifact.sha256,
            **{key: option[key] for key in ("output_version_id", "output_contract_sha256", "structure_version_id")}}
    if detail is None or detail["text"] is None:
        raise ValidationError("已采用文本缺少真实的成功候选")
    review = StageRun.objects.select_for_update().filter(production_run=run, stage_key="script_review",
        status=StageRun.Status.SUCCEEDED, ordinal__gt=stage.ordinal).first()
    if review is None:
        raise ValidationError("上游剧本尚无已完成的正式审核")
    snapshot = _verify_stage_snapshot(review, "output_snapshot")
    evidence = snapshot.get("review_payload")
    approval = ApprovalDecision.objects.filter(stage_run=review).order_by("-revision").first()
    if (not isinstance(evidence, dict) or evidence.get("kind") != "script_review_decision"
            or evidence.get("source_stage_id") != str(stage.pk)
            or evidence.get("source_snapshot_sha256") != stage.output_snapshot_sha256
            or evidence.get("artifact_id") != str(artifact.pk) or evidence.get("artifact_sha256") != artifact.sha256
            or evidence.get("script_revision_id") != str(run.script_revision_id)
            or evidence.get("script_revision_id") != artifact.manifest_metadata["script_revision_id"]
            or evidence.get("script_content_sha256") != artifact.sha256
            or approval is None or approval.decision != "approve"
            or approval.output_snapshot_sha256 != review.output_snapshot_sha256):
        raise ValidationError("所选剧本与正式审核采用记录不一致，不能用候选或当前母稿代替")
    _verify_stage_snapshot(stage, "output_snapshot")
    value, context = _context_field({"kind": "context_field", "selector_key": option["context_selector"],
        "scope": source["scope"], "content_revision_id": evidence["script_revision_id"]},
        spec=spec, owner=owner, project_id=project_id, production_run_id=production_run_id,
        eligible_stage_ids=eligible_stage_ids, context_cache={}, production_scope={"kind": "project", "id": str(run.project_id)})
    return value, {**_selection_reference(run, stage, artifact, decision), "output_key": option["output_key"],
        "scope": source["scope"], "artifact_sha256": artifact.sha256,
        "approval_id": str(approval.pk), "approval_stage_id": str(review.pk),
        "approval_snapshot_sha256": review.output_snapshot_sha256, "context": context}
