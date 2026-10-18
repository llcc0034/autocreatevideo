"""Explicit manual review of authored shots and their existing reference facts.

This does not choose an execution instance, allocate its slots, or generate media.
Those checks belong to the actual preparation, after this content review.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ApprovalDecision, Asset, ProductionRun, SeriesProject, Shot, ShotCharacterRelation, ShotReferenceUse, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_manual_commands import record_manual_approval
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.production_manual_storyboard import _stage, _content, _adoption
from studio.services.production_reference_mapping import reference_source_options, reference_use_records
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.shot_reference_sources import resolve_shot_reference_sources


def _review_stage(run, plan):
    source = _stage(run, plan)
    stage = StageRun.objects.filter(production_run=run, stage_key="storyboard_review").first()
    expected = next((row for row in plan.value["stage_catalog"]["value"]["stages"]
        if row["key"] == "storyboard_review"), None)
    if stage is None or expected is None or stage.mode != "manual" or stage.ordinal != expected["ordinal"] or stage.ordinal <= source.ordinal:
        raise ValidationError("本轮缺少与冻结目录一致的逐镜参考审核步骤")
    return source, stage


def _approval(stage, proposal, plan):
    decision = stage.approval_decisions.order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            raise ValidationError("逐镜审核状态缺少原人工决定")
        return {"revision": 0, "decision_id": None, "snapshot_sha256": None, "status": "not_reviewed"}, None
    saved = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = saved.value
    if (saved.schema_version != 1 or not isinstance(value, dict)
            or set(value) != {"schema_version", "kind", "stage_key", "formal_object_type", "canonical",
                "canonical_sha256", "manual_basis", "assets"}
            or value["kind"] != "manual_storyboard_review" or value["stage_key"] != stage.stage_key
            or value["formal_object_type"] != "storyboard" or not isinstance(value["canonical"], dict)
            or not isinstance(value["manual_basis"], dict) or set(value["manual_basis"]) != {
                "run_plan_sha256", "storyboard_adoption", "bible_review", "bible_image_review"}
            or not isinstance(value["manual_basis"]["storyboard_adoption"], dict) or not isinstance(value["assets"], list)
            or json_contract(value["canonical"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["canonical_sha256"]
            or stage.status != "succeeded" or decision.actor != "human" or decision.decision != "approve"
            or not decision.decided_by_id or decision.output_snapshot_sha256 != saved.sha256):
        raise ValidationError("逐镜审核决定与原内容快照不一致")
    if value["manual_basis"]["run_plan_sha256"] != plan.sha256:
        raise ValidationError("逐镜审核并非本轮冻结计划")
    for name, source_stage in (("storyboard_adoption", "storyboard"), ("bible_review", "bible_review"),
            ("bible_image_review", "bible_image_review")):
        ref = value["manual_basis"][name]
        if ref is None and name != "storyboard_adoption":
            continue
        expected = {"decision_id", "revision", "snapshot_sha256"} | ({"stage_run_id"} if name == "bible_review" else set())
        if not isinstance(ref, dict) or set(ref) != expected or type(ref["revision"]) is not int or ref["revision"] <= 0:
            raise ValidationError("逐镜审核缺少完整上游决定依据")
        try:
            decision_id = UUID(ref["decision_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("逐镜审核的上游决定身份无效") from exc
        source = ApprovalDecision.objects.filter(pk=decision_id, stage_run__production_run_id=stage.production_run_id,
            stage_run__stage_key=source_stage, revision=ref["revision"], output_snapshot_sha256=ref["snapshot_sha256"],
            actor="human", decision="approve", decided_by__isnull=False).only("stage_run_id").first()
        if source is None or (name == "bible_review" and str(source.stage_run_id) != ref["stage_run_id"]):
            raise ValidationError("逐镜审核的上游决定不属于本轮原审核事实")
    return {"revision": decision.revision, "decision_id": str(decision.pk), "snapshot_sha256": saved.sha256,
        "status": "current" if proposal is not None and proposal.sha256 == saved.sha256 else "changed"}, saved


def _proposal(run, plan, source, stage):
    from studio.services.production_runs import _canonical_storyboard, _lock_relation_rows
    from studio.services.production_reviewed_content import read_reviewed_bible

    content = _content(run, plan)
    adopted = _adoption(source, content)
    if adopted["status"] != "current":
        raise ValidationError("请先核对并采用本轮分镜内容；旧采用已变化时不能直接审核参考素材")
    ids = [shot["id"] for episode in content.value["episodes"] for shot in episode["shots"]]
    if not ids or any(not episode["shots"] for episode in content.value["episodes"]):
        raise ValidationError("本轮分镜存在空集合，请先补齐并采用")
    shots = list(Shot.objects.select_for_update().filter(pk__in=ids).select_related("episode", "scene")
        .prefetch_related("props", "character_relations").order_by("episode__number", "number", "id"))
    if len(shots) != len(ids):
        raise ValidationError("本轮所选镜头已缺失")
    for shot in shots:
        _lock_relation_rows(shot.props)
    list(ShotCharacterRelation.objects.select_for_update().filter(shot_id__in=ids).order_by("shot_id", "role", "position").values_list("pk", flat=True))
    list(ShotReferenceUse.objects.select_for_update().filter(shot_id__in=ids).order_by("shot_id", "position").values_list("pk", flat=True))
    resolved = resolve_shot_reference_sources(actor=run.owner, project_id=run.project_id, shot_ids=ids).value
    references = [{"shot_id": row["shot_id"], "items": reference_use_records(row["uses"])} for row in resolved["shots"]]
    records = [item for row in references for item in row["items"]]
    used_ids = {row["source"]["asset_id"] for row in records}
    assets = list(Asset.objects.select_for_update().filter(pk__in=used_ids).order_by("id"))
    if {str(asset.pk) for asset in assets} != used_ids:
        raise ValidationError("镜头参考素材已不存在")
    for asset in assets:
        if asset.owner_id not in (None, run.owner_id) or not asset.is_active:
            raise ValidationError("镜头包含停用或其他用户的素材，请在原分镜页面处理")
    basis = {"run_plan_sha256": plan.sha256, "storyboard_adoption": {
        key: adopted[key] for key in ("decision_id", "revision", "snapshot_sha256")},
        "bible_review": None, "bible_image_review": None}
    if (any(shot.scene_id or shot.props.all() or shot.character_relations.all() for shot in shots)
            or any(use["setting"] is not None for row in resolved["shots"] for use in row["uses"])):
        eligible = list(run.stages.filter(ordinal__lt=stage.ordinal).values_list("pk", flat=True))
        bible = read_reviewed_bible(run=run, eligible_stage_ids=eligible)
        basis["bible_review"] = bible["review"]
    # Exact saved uses are reviewed here. An explicitly selected upload/library
    # image is not a Bible-pool choice and must not acquire a fabricated image
    # approval. The original Bible content dependency above remains explicit.
    # Re-read after relation locks; no reference, image or creative field is written.
    canonical = _canonical_storyboard(run)
    if _content(run, plan).sha256 != content.sha256:
        raise ValidationError("审核期间分镜创作内容发生变化，请重新读取")
    return json_contract({"schema_version": 1, "kind": "manual_storyboard_review", "stage_key": stage.stage_key,
        "formal_object_type": "storyboard", "canonical": canonical,
        "canonical_sha256": json_contract(canonical, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256,
        "manual_basis": basis, "assets": [{"id": str(asset.pk), "name": asset.name, "kind": asset.kind,
            "mime_type": asset.mime_type, "checksum": asset.checksum} for asset in assets]}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES), references


def _state(run):
    plan = get_run_execution_plan(run)
    source, stage = _review_stage(run, plan)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    reason, proposal, references = "", None, []
    try:
        proposal, references = _proposal(run, plan, source, stage)
    except ValidationError as exc:
        reason = "；".join(exc.messages)
    approval, saved = _approval(stage, proposal, plan)
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256, "ready": proposal is not None,
        "reason": reason, "contract": proposal.value if proposal else None,
        "references": references,
        "source_options": reference_source_options(),
        "contract_sha256": proposal.sha256 if proposal else None, "approval": approval}, stage, saved


@transaction.atomic
def manual_storyboard_review_detail(run):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    result = _state(run)[0]
    from studio.services.storyboard_review_display import storyboard_review_display
    result["display"] = storyboard_review_display(project_id=run.project_id,
        canonical=result["contract"]["canonical"]) if result["ready"] else None
    return result


def approve_manual_storyboard_references(*, run, stage, plan, request, actor):
    payload = request["payload"]
    if (request["scope"] != {"kind": "project", "id": str(run.project_id)} or request["unit_key"] != f"project:{run.project_id}"
            or set(payload) != {"expected_contract_sha256", "reviewed_shot_ids", "comment"}):
        raise ValidationError("逐镜参考审核必须提交本轮完整核对清单、摘要与说明")
    state, expected, _ = _state(run)
    if stage.pk != expected.pk or not state["ready"]:
        raise ValidationError(state["reason"] or "本轮审核步骤不一致")
    if request["expected_selection_revision"] != state["approval"]["revision"] or payload["expected_contract_sha256"] != state["contract_sha256"]:
        raise ValidationError("镜头、素材或上游审核已变化，请重新核对", code="manual_selection_conflict")
    ids = [shot["id"] for episode in state["contract"]["canonical"]["episodes"] for shot in episode["shots"]]
    checked = payload["reviewed_shot_ids"]
    if not isinstance(checked, list) or any(not isinstance(pk, str) for pk in checked) or sorted(checked) != sorted(ids):
        raise ValidationError("必须逐镜核对本轮全部参考素材，不能遗漏或重复")
    require_replaceable_content_dependents(run=run, stage=stage)
    return record_manual_approval(run, stage, value=state["contract"], revision=state["approval"]["revision"],
        comment=normalize_optional_audit_comment(payload["comment"]), actor=actor)


@transaction.atomic
def read_manual_reviewed_storyboard(*, run, eligible_stage_ids, decision_id=None):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if not StageRun.objects.filter(production_run=run, stage_key="storyboard_review", pk__in=eligible_stage_ids).exists():
        raise ValidationError("当前逐镜审核不在本次允许的前置步骤中", code="reference_review_required")
    state, stage, saved = _state(run)
    approval = state["approval"]
    if approval["status"] != "current" or saved is None:
        raise ValidationError(state["reason"] or "逐镜参考清单尚未审核或已变化", code="reference_review_changed")
    if decision_id is not None and decision_id != approval["decision_id"]:
        raise ValidationError("指定逐镜审核并非当前决定", code="reference_review_invalid")
    return {"canonical": deepcopy(saved.value["canonical"]), "review": {"stage_run_id": str(stage.pk),
        "decision_id": approval["decision_id"], "revision": approval["revision"], "snapshot_sha256": saved.sha256}}
