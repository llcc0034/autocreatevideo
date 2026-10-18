"""Human takeover of exact uses, inside the original Run command transaction.

Frozen recipe/input grant source and replacement permissions. The existing
command receipt proves replay; selected uses remain the sole mutable facts.
This adapter neither approves a storyboard nor resumes or dispatches a call.
"""
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ProductionRun, StageRun
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_recipe_shot_references import (
    recipe_shot_reference_catalog, recipe_storyboard_reference_source, validate_recipe_shot_reference_sources,
)
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_shot_reference_editability import shot_reference_execution_editability
from studio.services.shot_reference_choices import _choices, apply_shot_reference_choices, read_shot_reference_choices
from studio.services.shot_reference_sources import _reference_project


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _state(*, run, plan, actor, shot_id):
    run.project = _reference_project(actor, run.project_id, lock=True)
    if run.plan_source != "recipe" or run.recipe_version_id is None:
        raise ValidationError("配方选材需要原配方运行，不借用人工采用事实")
    stage = StageRun.objects.select_for_update().filter(production_run=run, stage_key="storyboard_review", mode="review").first()
    if stage is None:
        raise ValidationError("本轮缺少原配方分镜参考步骤")
    stage.production_run = run
    configured, source_sha, shot_ids = recipe_storyboard_reference_source(run, stage)
    if configured is None:
        raise ValidationError("本轮未明确配置逐镜选材来源，不能补造来源或改选权限")
    unit = resolve_production_unit(run, {"kind": "shot", "id": str(shot_id)})
    if unit["scope"]["id"] not in shot_ids:
        raise ValidationError("镜头不在本轮已冻结分镜来源内，不能选材")
    baseline = read_shot_reference_choices(actor=actor, project_id=run.project_id, shot_id=unit["scope"]["id"])
    catalog = recipe_shot_reference_catalog(run, stage, configured)
    row = next(item for item in plan.value["stage_catalog"]["value"]["stages"] if item["key"] == stage.stage_key)
    if "select" not in row["manual_actions"]:
        allowed, code, reason = False, "action_unavailable", "本轮冻结计划未声明选材动作。"
    elif stage.status not in {"running", "awaiting_approval"} or run.current_stage_key != stage.stage_key:
        allowed, code, reason = False, "review_not_current", "本轮不在待处理的分镜参考步骤，不修改其选择。"
    else:
        allowed, code, reason = shot_reference_execution_editability(run, stage, baseline.shot_id)
    return stage, configured, source_sha, baseline, catalog, {"allowed": allowed, "code": code, "reason": reason}


@transaction.atomic
def recipe_shot_reference_detail(*, run_id, actor, shot_id):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run = ProductionRun.objects.select_for_update().only("id", "owner_id", "project_id", "plan_source",
        "recipe_version_id", "status", "current_stage_key", "config_snapshot", "config_snapshot_schema_version",
        "config_snapshot_size_bytes", "config_snapshot_sha256").get(pk=run_id)
    if run.owner_id != actor.pk and not actor.is_staff:
        raise PermissionDenied("无权读取此制作运行")
    plan = get_run_execution_plan(run)
    stage, configured, source_sha, baseline, catalog, editable = _state(run=run, plan=plan, actor=actor, shot_id=shot_id)
    return {"run_id": str(run.pk), "stage_key": stage.stage_key, "run_plan_sha256": plan.sha256,
        "shot_id": baseline.shot_id, "source_snapshot_sha256": source_sha,
        "selection_rule": configured, "upstream_candidates": catalog,
        "uses_sha256": baseline.sha256, "state": baseline.value, "editability": editable}


def select_recipe_shot_references(*, run, stage, plan, request, actor):
    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("镜头选材必须在原命令事务内执行")
    if (request["scope"]["kind"] != "shot" or request["unit_key"] != f'shot:{request["scope"]["id"]}'
            or request["expected_selection_revision"] is not None):
        raise ValidationError("配方选材必须定位本轮单镜且修订为空，不伪造人工分镜采用修订")
    payload = request["payload"]
    if set(payload) != {"expected_source_snapshot_sha256", "expected_uses_sha256", "choices"}:
        raise ValidationError("配方选材仅提交分镜来源摘要、用途基线及选择，不接受来源或改选权限覆盖")
    current, configured, source_sha, baseline, catalog, editable = _state(
        run=run, plan=plan, actor=actor, shot_id=request["scope"]["id"])
    if stage.pk != current.pk:
        raise ValidationError("选材必须使用本轮原分镜参考步骤")
    if payload["expected_source_snapshot_sha256"] != source_sha or payload["expected_uses_sha256"] != baseline.sha256:
        _conflict("分镜来源、用途或选择已变化，请重新读取；不覆盖新选择")
    if not editable["allowed"]:
        raise ValidationError(editable["reason"], code=editable["code"])
    choices = _choices(payload["choices"])
    uses = {UUID(row["id"]): row for row in baseline.value["uses"]}
    if set(choices) - set(uses):
        raise ValidationError("所选用途不属于本镜头")
    rule = configured["rule"]
    for use_id, choice in choices.items():
        original = uses[use_id]
        selected = bool(original["source_mode"] or original["reference_version_id"] or original["asset_id"] or original["asset_sha256"])
        changed = any(original[key] != (str(value) if isinstance(value, UUID) else value) for key, value in choice.items())
        if selected and changed and not rule["allow_override"]:
            raise ValidationError("本轮冻结规则不允许替换已有选择；未选择用途仍可首次选材")
    validate_recipe_shot_reference_sources(stage=current, configured=configured, catalog=catalog,
        choices=choices.values(), shot_id=baseline.shot_id)
    apply_shot_reference_choices(actor=actor, project_id=run.project_id, shot_id=baseline.shot_id,
        expected_uses_sha256=baseline.sha256, choices=payload["choices"])
    return ManualActionResult("shot_reference_choices", UUID(baseline.shot_id))
