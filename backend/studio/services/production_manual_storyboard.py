"""Adopt authored shots or a confirmed script, never infer an execution route."""
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ApprovalDecision, ProductionRun, SeriesProject, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_manual_commands import record_manual_approval
from studio.services.production_manual_scripts import manual_script_states
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_storyboard_content import storyboard_content
from studio.services.production_storyboard_materialization import (
    materialize_declared_storyboard, read_storyboard_materialization_plan,
)
from studio.services.configuration import ConfigurationResolutionError
from studio.services.bible_variants import BibleVariantConflict


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _stage(run, plan):
    if run.plan_source != "manual" or run.project.owner_id != run.owner_id:
        raise ValidationError("此入口仅用于人工制作的明确分镜采用")
    try:
        stage = StageRun.objects.get(production_run=run, stage_key="storyboard")
    except StageRun.DoesNotExist as exc:
        raise ValidationError("本轮缺少正式分镜步骤，不能补造") from exc
    row = next((item for item in plan.value["stage_catalog"]["value"]["stages"] if item["key"] == stage.stage_key), None)
    if stage.mode != "manual" or row is None or stage.ordinal != row["ordinal"]:
        raise ValidationError("分镜步骤与本轮冻结目录不一致")
    return stage


def _content(run, plan):
    # Use the original RunPlan-scoped episode resolver, not all current project
    # episodes when the user chose an explicit subset.
    from studio.services.production_runs import StageBlocked, _selected_episodes
    try:
        episodes = _selected_episodes(run)
    except StageBlocked as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    script = manual_script_states(run)
    content = {"schema_version": 3, "run_plan_sha256": plan.sha256,
        "script": script["current_script"], "script_decision_id": script["current_script_decision_id"],
        **storyboard_content(project_id=run.project_id, episodes=episodes, include_references=False)}
    return json_contract(content, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _adoption(stage, current):
    decision = stage.approval_decisions.only("id", "stage_run_id", "revision", "decision", "actor",
        "decided_by_id", "output_snapshot_sha256").order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            _conflict("分镜采用状态缺少正式决策")
        return {"revision": 0, "decision_id": None, "snapshot_sha256": None,
            "content_sha256": None, "status": "not_adopted", "source": None}
    saved = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = saved.value
    if (saved.schema_version != 5 or not isinstance(value, dict)
            or set(value) != {"schema_version", "kind", "content", "content_sha256", "source"}
            or value["kind"] != "manual_storyboard_adoption" or stage.status != "succeeded"
            or decision.actor != "human" or decision.decision != "approve" or not decision.decided_by_id
            or decision.output_snapshot_sha256 != saved.sha256
            or not isinstance(value["content"], dict) or value["content"].get("schema_version") != 3
            or json_contract(value["content"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["content_sha256"]):
        _conflict("分镜采用决策与正式内容快照不一致")
    content = value["content"]
    if "script_decision_id" not in content or "script" not in content:
        _conflict("分镜缺少明确剧本采用依据")
    if content["script"] is None:
        if content["script_decision_id"] is not None:
            _conflict("未采用剧本的分镜不能伪造剧本依据")
    else:
        try:
            basis_id = UUID(content["script_decision_id"])
        except (ValueError, TypeError, AttributeError):
            _conflict("分镜缺少原剧本采用决策")
        if not ApprovalDecision.objects.filter(pk=basis_id, stage_run__production_run_id=stage.production_run_id,
                stage_run__stage_key__in=("script_input", "script_review"), actor="human", decision="approve",
                decided_by__isnull=False).exists():
            _conflict("分镜未对应本轮原剧本采用决策")
    source = value["source"]
    if not isinstance(source, dict) or source.get("kind") not in {"existing_storyboard", "confirmed_script"}:
        _conflict("分镜采用来源缺失")
    if source["kind"] == "existing_storyboard":
        if set(source) != {"kind"}:
            _conflict("已有分镜采用来源包含未知字段")
    elif (set(source) != {"kind", "contract", "sha256"}
            or not isinstance(source["contract"], dict) or source["contract"].get("schema_version") != 3
            or not isinstance(source["contract"].get("source_basis"), dict)
            or source["contract"]["source_basis"].get("script") != content["script"]
            or source["contract"]["source_basis"].get("script_decision_id") != content["script_decision_id"]
            or source["contract"]["source_basis"].get("run_plan_sha256") != content["run_plan_sha256"]
            or json_contract(source["contract"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != source["sha256"]):
        _conflict("结构化分镜的冻结来源或参数不一致")
    return {"revision": decision.revision, "decision_id": str(decision.pk), "snapshot_sha256": saved.sha256,
        "content_sha256": value["content_sha256"], "source": source,
        "status": "current" if current.sha256 == value["content_sha256"] else "changed"}


def _materialization(run, stage, plan, content):
    """Read a proposed exact source; absence blocks only this input method."""
    reason = ""
    if plan.value["scope"]["kind"] != "project":
        reason = "本轮只包含指定剧集；不能把新建剧集静默加入冻结范围。"
    elif run.project.episodes.exists():
        reason = "项目已有剧集或分镜，请查看并采用现有内容；不会自动覆盖或追加拆稿。"
    elif content.value["script"] is None:
        reason = "请先在剧本步骤明确采用一个已确认的剧本修订。"
    if reason:
        return {"ready": False, "reason": reason, "contract": None, "sha256": None}
    try:
        contract = read_storyboard_materialization_plan(run=run, stage=stage).contract
    except (ValidationError, ConfigurationResolutionError, BibleVariantConflict, ValueError) as exc:
        return {"ready": False, "reason": f"无法从已采用结构化剧本创建分镜：{exc}", "contract": None, "sha256": None}
    return {"ready": True, "reason": "", "contract": contract.value, "sha256": contract.sha256}


@transaction.atomic
def manual_storyboard_detail(run):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    plan = get_run_execution_plan(run)
    stage = _stage(run, plan)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    content = _content(run, plan)
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256, "content": content.value,
        "content_sha256": content.sha256, "adoption": _adoption(stage, content),
        "materialization": _materialization(run, stage, plan, content)}


def adopt_manual_storyboard(*, run, stage, plan, request, actor):
    expected = _stage(run, plan)
    payload = request["payload"]
    if (stage.pk != expected.pk or request["scope"] != {"kind": "project", "id": str(run.project_id)}
            or request["unit_key"] != f"project:{run.project_id}"):
        raise ValidationError("分镜采用必须定位本轮冻结选集，而不是单独猜测一个镜头")
    source = payload.get("source")
    if not isinstance(source, dict) or source.get("kind") not in {"existing_storyboard", "confirmed_script"}:
        raise ValidationError("请明确选择已有分镜或本轮已确认剧本来源")
    from_script = source["kind"] == "confirmed_script"
    keys = {"expected_content_sha256", "source", "comment"} | (set() if from_script else {"reviewed_shot_ids"})
    if set(payload) != keys or set(source) != ({"kind", "expected_materialization_sha256"} if from_script else {"kind"}):
        raise ValidationError("请提交与所选内容来源对应的完整采用请求")
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    content = _content(run, plan)
    state = _adoption(stage, content)
    if request["expected_selection_revision"] != state["revision"] or payload["expected_content_sha256"] != content.sha256:
        _conflict("分镜内容、剧本依据或采用修订已变化，请重新读取")
    require_replaceable_content_dependents(run=run, stage=stage)
    if from_script:
        proposed = _materialization(run, stage, plan, content)
        if not proposed["ready"]:
            raise ValidationError(proposed["reason"])
        if source["expected_materialization_sha256"] != proposed["sha256"]:
            _conflict("采用的剧本、参数或角色场景道具已变化，请重新读取后确认")
        materialize_declared_storyboard(run=run, stage=stage, expected_contract_sha256=proposed["sha256"])
        source = {"kind": "confirmed_script", "contract": proposed["contract"], "sha256": proposed["sha256"]}
        content = _content(run, plan)
    else:
        episodes = content.value["episodes"]
        if not episodes or any(not episode["shots"] for episode in episodes):
            raise ValidationError("所选范围存在空剧集或尚无分镜，请先在原分镜管理中补齐")
        ids = [shot["id"] for episode in episodes for shot in episode["shots"]]
        checked = payload["reviewed_shot_ids"]
        if (not isinstance(checked, list) or any(not isinstance(item, str) for item in checked)
                or sorted(checked) != sorted(ids)):
            raise ValidationError("请逐项核对本轮选集中的全部镜头，不接受遗漏或重复确认")
    return record_manual_approval(run, stage, value={"schema_version": 5, "kind": "manual_storyboard_adoption",
        "content": content.value, "content_sha256": content.sha256, "source": source}, revision=state["revision"],
        comment=normalize_optional_audit_comment(payload["comment"]), actor=actor)
