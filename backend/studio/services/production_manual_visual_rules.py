"""Explicit manual film-rule review using the original project writer."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ApprovalDecision
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_manual_text_candidates import manual_text_candidate
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.production_visual_rules import approve_verified_visual_rules_artifact, VISUAL_RULES_REVIEW_SCHEMA_VERSION
from studio.services.production_reviewed_visual_rules import visual_rules_review_state


def approve_manual_visual_rules(*, run, stage, plan, request, actor):
    if (request["scope"] != {"kind": "project", "id": str(run.project_id)}
            or request["unit_key"] != f"project:{run.project_id}"):
        raise ValidationError("全剧影视规则审核必须定位本轮项目")
    payload = request["payload"]
    if set(payload) != {"text_candidate", "candidate_overrides", "comment"}:
        raise ValidationError("请提交已阅读候选、三项审核值和说明；输入与采用基线由原准备核验")
    state = visual_rules_review_state(run, stage)
    if request["expected_selection_revision"] != state["revision"]:
        raise ValidationError("本轮影视规则审核修订已变化，请重新读取", code="manual_selection_conflict")
    artifact, detail = manual_text_candidate(run=run, stage=stage, plan=plan,
        selection=payload["text_candidate"], unit_key=request["unit_key"])
    require_replaceable_content_dependents(run=run, stage=stage)
    comment = normalize_optional_audit_comment(payload["comment"])
    evidence = approve_verified_visual_rules_artifact(run=run, artifact=artifact, detail=detail,
        reviewed_rules=payload["candidate_overrides"], user=actor)
    assign_json_contract(stage, "output_snapshot", evidence, schema_version=VISUAL_RULES_REVIEW_SCHEMA_VERSION, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    stage.status, stage.reason_code, stage.reason = "succeeded", "", ""
    stage.started_at = stage.started_at or timezone.now()
    stage.finished_at = timezone.now()
    stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
        "output_snapshot_sha256", "status", "reason_code", "reason", "started_at", "finished_at", "updated_at"))
    decision = ApprovalDecision.objects.create(stage_run=stage, revision=state["revision"] + 1,
        decision="approve", actor="human", decided_by=actor, comment=comment, output_snapshot_sha256=stage.output_snapshot_sha256)
    stage.output_reference = f"approval:{decision.pk}"
    stage.save(update_fields=("output_reference", "updated_at"))
    run.status = "awaiting_input"
    run.save(update_fields=("status", "updated_at"))
    return ManualActionResult("approval_decision", decision.pk)
