"""Original manual Run command over declared shot uses; selection is not review.

No recipe defaults, media generation, request-slot allocation or auto advance.
The Run's frozen action catalog grants this manual operation, never a caller's
claimed override permission. Receipts identify the original shot, not a second
copy of mutable selection state or a fabricated material approval.
"""
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ProductionRun
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_manual_storyboard import _adoption, _content
from studio.services.production_manual_storyboard_review import _review_stage
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_shot_reference_editability import shot_reference_execution_editability
from studio.services.shot_reference_choices import apply_shot_reference_choices, read_shot_reference_choices
from studio.services.shot_reference_sources import _reference_project


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _editable(run, stage, shot_id, adoption, plan):
    row = next(item for item in plan.value["stage_catalog"]["value"]["stages"] if item["key"] == stage.stage_key)
    if "select" not in row["manual_actions"]:
        return False, "action_unavailable", "本轮冻结计划未声明选材动作；不会升级或改写旧计划。"
    if run.status not in {"awaiting_input", "running"}:
        return False, "run_closed", "本轮已停止或结束，不修改其素材选择。"
    if adoption["status"] != "current":
        return False, "storyboard_not_current", "请先在原分镜步骤采用当前内容，再选择其已声明的素材用途。"
    return shot_reference_execution_editability(run, stage, shot_id)


def _state(*, run, plan, actor, shot_id):
    _reference_project(actor, run.project_id, lock=True)
    source, stage = _review_stage(run, plan)
    unit = resolve_production_unit(run, {"kind": "shot", "id": str(shot_id)})
    content = _content(run, plan)
    adoption = _adoption(source, content)
    baseline = read_shot_reference_choices(actor=actor, project_id=run.project_id, shot_id=unit["scope"]["id"])
    allowed, code, reason = _editable(run, stage, baseline.shot_id, adoption, plan)
    return stage, adoption, baseline, {"allowed": allowed, "code": code, "reason": reason}


@transaction.atomic
def manual_shot_reference_detail(*, run_id, actor, shot_id):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run = ProductionRun.objects.select_for_update().only("id", "owner_id", "project_id", "plan_source",
        "recipe_version_id", "status", "script_revision_id", "script_adoption_decision_id").get(pk=run_id)
    if run.owner_id != actor.pk and not actor.is_staff:
        raise PermissionDenied("无权读取此制作运行")
    plan = get_run_execution_plan(run)
    stage, adoption, baseline, editable = _state(run=run, plan=plan, actor=actor, shot_id=shot_id)
    return {"run_id": str(run.pk), "stage_key": stage.stage_key, "run_plan_sha256": plan.sha256,
        "shot_id": baseline.shot_id, "storyboard_adoption_revision": adoption["revision"],
        "storyboard_adoption_sha256": adoption["snapshot_sha256"], "uses_sha256": baseline.sha256,
        "state": baseline.value, "editability": editable}


def select_manual_shot_references(*, run, stage, plan, request, actor):
    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("镜头选材必须在原人工命令事务内执行")
    if request["scope"]["kind"] != "shot" or request["unit_key"] != f'shot:{request["scope"]["id"]}':
        raise ValidationError("选材必须明确定位本轮单一镜头，不能改变项目或其他镜头")
    payload = request["payload"]
    if set(payload) != {"expected_storyboard_adoption_sha256", "expected_uses_sha256", "choices"}:
        raise ValidationError("选材仅提交原分镜采用摘要、用途基线摘要和明确选择，不接受审核或策略覆盖")
    current_stage, adoption, baseline, editable = _state(run=run, plan=plan, actor=actor, shot_id=request["scope"]["id"])
    if stage.pk != current_stage.pk:
        raise ValidationError("选材必须使用原分镜参考步骤")
    if (request["expected_selection_revision"] != adoption["revision"]
            or payload["expected_storyboard_adoption_sha256"] != adoption["snapshot_sha256"]):
        _conflict("分镜采用依据已变化，请重新读取后选择")
    if not editable["allowed"]:
        raise ValidationError(editable["reason"], code=editable["code"])
    if payload["expected_uses_sha256"] != baseline.sha256:
        _conflict("本镜用途或选择已变化，请重新读取；不覆盖新选择")
    apply_shot_reference_choices(actor=actor, project_id=run.project_id, shot_id=baseline.shot_id,
        expected_uses_sha256=payload["expected_uses_sha256"], choices=payload["choices"])
    return ManualActionResult("shot_reference_choices", UUID(baseline.shot_id))
