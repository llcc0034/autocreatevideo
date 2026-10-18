"""Adopt an exact script's Bible declarations, then explicitly materialize them.

Saved semantic reads never reparse text, create AI tasks or confirm Bible rows.
Only the second, reviewed action calls the original Bible writer.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ApprovalDecision, ProductionRun, SeriesProject, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_manual_commands import record_manual_approval
from studio.services.production_manual_scripts import manual_script_states, read_manual_script_structure
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.script_bible_materialization import freeze_script_reference_bible
from studio.services.bible_content_review import approve_bible_content, bible_review_inventory


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _freeze_source(run):
    adopted = read_manual_script_structure(run)
    return freeze_script_reference_bible(revision=run.script_revision, project_id=run.project_id,
        expected_structure_source=adopted["source"])


def _stages(run, plan):
    if run.plan_source != "manual" or run.project.owner_id != run.owner_id:
        raise ValidationError("此入口只读取无配方制作的 Bible 采用与审核")
    ordinals = {row["key"]: row["ordinal"] for row in plan.value["stage_catalog"]["value"]["stages"]}
    stages = {row.stage_key: row for row in StageRun.objects.filter(production_run=run,
        stage_key__in=("bible_materialize", "bible_review")).order_by()}
    if (set(stages) != {"bible_materialize", "bible_review"}
            or any(row.mode != "manual" or row.ordinal != ordinals.get(key) for key, row in stages.items())
            or stages["bible_materialize"].ordinal >= stages["bible_review"].ordinal):
        raise ValidationError("本轮缺少与冻结目录一致的 Bible 来源/审核步骤")
    return stages["bible_materialize"], stages["bible_review"]


def _decision(stage, kind):
    decision = stage.approval_decisions.only("id", "revision", "stage_run_id", "actor", "decision",
        "decided_by_id", "output_snapshot_sha256").order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            _conflict("Bible 步骤缺少正式人工决策，不能显示为已完成")
        return {"revision": 0, "decision_id": None, "snapshot_sha256": None}, None
    frozen = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if (frozen.schema_version != 3 or not isinstance(frozen.value, dict) or frozen.value.get("kind") != kind
            or decision.actor != "human" or decision.decision != "approve" or not decision.decided_by_id
            or stage.status != "succeeded" or decision.output_snapshot_sha256 != frozen.sha256):
        _conflict("Bible 步骤的人工决策与候选快照不一致")
    return {"revision": decision.revision, "decision_id": str(decision.pk), "snapshot_sha256": frozen.sha256}, frozen.value


def _source(run, stage, current):
    state, value = _decision(stage, "manual_bible_source")
    if value is None:
        return {**state, "status": "not_adopted", "content": None, "candidate_sha256": None}, None
    frozen = value.get("candidates")
    if (set(value) != {"schema_version", "kind", "content", "candidates", "script_decision_id"} or not isinstance(frozen, dict)
            or frozen.get("kind") != "script_reference_bible" or not isinstance(value["content"], dict)
            or frozen.get("schema_version") != 3 or not isinstance(frozen.get("structure_source"), dict)
            or frozen["structure_source"].get("script") != value["content"]):
        _conflict("Bible 来源缺少明确的已确认剧本及候选集合")
    try:
        basis_id = UUID(value["script_decision_id"])
    except (ValueError, TypeError, AttributeError):
        _conflict("Bible 来源缺少原剧本采用决策")
    if not ApprovalDecision.objects.filter(pk=basis_id, stage_run__production_run=run,
            stage_run__stage_key__in=("script_input", "script_review"), actor="human", decision="approve",
            decided_by__isnull=False).exists():
        _conflict("Bible 来源未对应本轮原剧本采用决策")
    current_source = value["content"] == current and str(basis_id) == str(run.script_adoption_decision_id)
    if current_source:
        verified = _freeze_source(run)
        if verified != frozen:
            _conflict("Bible 候选与实际已确认剧本不一致，不能自动重建")
    return {**state, "status": "current" if current_source else "changed", "content": value["content"],
        "candidate_sha256": frozen["declaration_sha256"]}, frozen


def _approval(stage, source, baseline):
    state, value = _decision(stage, "manual_bible_review")
    if value is None:
        return {**state, "status": "not_reviewed"}
    if (set(value) != {"schema_version", "kind", "source_decision_id", "source_snapshot_sha256",
            "content", "materialization", "project_bible", "project_bible_sha256"}
            or not isinstance(value["materialization"], dict)
            or json_contract(value["project_bible"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["project_bible_sha256"]):
        _conflict("Bible 审核缺少正式写入结果与审核后内容，不能推断为已通过")
    matches = (source["status"] == "current" and value["source_decision_id"] == source["decision_id"]
        and value["source_snapshot_sha256"] == source["snapshot_sha256"] and value["content"] == source["content"]
        and value["project_bible_sha256"] == baseline.sha256)
    return {**state, "status": "current" if matches else "changed"}


@transaction.atomic
def manual_bible_review_detail(run):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    plan = get_run_execution_plan(run)
    source_stage, review = _stages(run, plan)
    current = manual_script_states(run)["current_script"]
    source, frozen = _source(run, source_stage, current)
    project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    baseline, candidates, settings = bible_review_inventory(run=run, frozen=frozen)
    return {"plan_source": "manual", "run_id": str(run.pk), "run_plan_sha256": plan.sha256, "current_script": current,
        "source": source, "project_bible": baseline.value, "project_bible_sha256": baseline.sha256, "candidates": candidates,
        "settings": settings, "approval": _approval(review, source, baseline)}


def perform_manual_bible(*, run, stage, plan, request, actor):
    source_stage, review = _stages(run, plan)
    payload = request["payload"]
    if (request["scope"] != {"kind": "project", "id": str(run.project_id)}
            or request["unit_key"] != f"project:{run.project_id}"):
        raise ValidationError("Bible 来源和审核必须定位本轮项目")
    current = manual_script_states(run)["current_script"]
    if current is None:
        raise ValidationError("请先明确采用本轮已确认剧本")
    if payload.get("expected_current_script") != current:
        _conflict("本轮已采用剧本已变化，请重新读取")
    comment = normalize_optional_audit_comment(payload.get("comment"))
    source, frozen = _source(run, source_stage, current)
    if stage.pk == source_stage.pk:
        if set(payload) != {"expected_current_script", "comment"}:
            raise ValidationError("采用 Bible 来源只接受当前剧本引用和说明")
        if request["expected_selection_revision"] != source["revision"]:
            _conflict("Bible 来源采用记录已变化")
        require_replaceable_content_dependents(run=run, stage=stage)
        frozen = _freeze_source(run)
        return record_manual_approval(run, stage, value={"schema_version": 3, "kind": "manual_bible_source",
            "content": current, "script_decision_id": str(run.script_adoption_decision_id), "candidates": frozen},
            revision=source["revision"], comment=comment, actor=actor)
    if stage.pk != review.pk or set(payload) != {"expected_current_script", "expected_source_snapshot_sha256",
            "expected_project_bible_sha256", "reviewed_candidate_ids", "reviewed_setting_keys", "reviewed_project_bible", "comment"}:
        raise ValidationError("Bible 审核需要明确来源、项目现状与逐项已核对候选")
    if payload["reviewed_project_bible"] is not True:
        raise ValidationError("请核对项目现有的完整创作设定，再确认 Bible 审核")
    if frozen is None or source["status"] != "current" or payload["expected_source_snapshot_sha256"] != source["snapshot_sha256"]:
        _conflict("Bible 来源尚未采用或已变化，请先重新核对来源")
    project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    baseline, _, _ = bible_review_inventory(run=run, frozen=frozen)
    approval = _approval(review, source, baseline)
    if (request["expected_selection_revision"] != approval["revision"]
            or payload["expected_project_bible_sha256"] != baseline.sha256):
        _conflict("Bible 审核记录或项目内容已变化，请重新读取后确认")
    require_replaceable_content_dependents(run=run, stage=stage)
    materialization = approve_bible_content(run=run, actor=actor, frozen=frozen,
        expected_project_bible_sha256=payload["expected_project_bible_sha256"],
        reviewed_candidate_ids=payload["reviewed_candidate_ids"], reviewed_setting_keys=payload["reviewed_setting_keys"],
        reviewed_project_bible=payload["reviewed_project_bible"], comment=comment)
    updated, _, _ = bible_review_inventory(run=run)
    return record_manual_approval(run, stage, value={"schema_version": 3, "kind": "manual_bible_review",
        "source_decision_id": source["decision_id"], "source_snapshot_sha256": source["snapshot_sha256"],
        "content": current, "materialization": materialization, "project_bible": updated.value,
        "project_bible_sha256": updated.sha256}, revision=approval["revision"], comment=comment, actor=actor)


@transaction.atomic
def read_manual_reviewed_bible(*, run, eligible_stage_ids, decision_id=None):
    """Use the original manual approval, not a fresh unreviewed project copy."""
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    review = StageRun.objects.filter(production_run=run, stage_key="bible_review",
        pk__in=eligible_stage_ids).first()
    if review is None:
        raise ValidationError("当前 Bible 不在本次允许的前置审核中", code="reference_review_required")
    state = manual_bible_review_detail(run)
    approval = state["approval"]
    if approval["status"] != "current":
        raise ValidationError("Bible 尚未审核或审核依据已变化，请先核对", code="reference_review_changed")
    if decision_id is not None and decision_id != approval["decision_id"]:
        raise ValidationError("指定的 Bible 审核并非本轮当前决策", code="reference_review_invalid")
    frozen = verify_json_contract(review.output_snapshot, schema_version=review.output_snapshot_schema_version,
        size_bytes=review.output_snapshot_size_bytes, sha256=review.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if frozen.sha256 != approval["snapshot_sha256"]:
        raise ValidationError("Bible 审核决定与内容快照不一致", code="reference_review_invalid")
    return {"canonical": deepcopy(frozen.value["project_bible"]), "review": {
        "stage_run_id": str(review.pk), "decision_id": approval["decision_id"],
        "revision": approval["revision"], "snapshot_sha256": frozen.sha256}}
