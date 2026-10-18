"""Configured selection of exact generic-text candidates on original Run facts.

This writer never calls a provider or materializes script/Bible business objects.
Ordering is the frozen candidate number, not completion time or artifact index.
"""
from hashlib import sha256

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ArtifactSelectionDecision, GeneratedArtifact, ProductionRun, StageRun
from studio.services.execution_production_attempts import current_primary_attempts
from studio.services.execution_production_candidates import read_production_text_candidate
from studio.services.generated_artifacts import _record_artifact_selection
from studio.services.production_recipe_contracts import recipe_stage_kind
from studio.services.production_recipe_preparations import recipe_project_stage_context
from studio.services.production_recipe_routing import read_recipe_candidate_route, recipe_routing, verify_recipe_preparation_route
from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS


def is_structured_text_declaration(declaration):
    return STAGE_EXECUTION_OPERATIONS.get(recipe_stage_kind(declaration)) == "text.structured"


def _verify_candidate(artifact, *, stage, plan, declaration, group):
    if (artifact.production_run_id != stage.production_run_id or artifact.stage_run_id != stage.pk
            or artifact.variant_group != group):
        raise ValidationError("通用文本候选必须属于本轮明确步骤和候选组")
    detail = read_production_text_candidate(artifact)
    if detail is None or detail["operation"] != "text.structured":
        raise ValidationError("此步骤只能选择通用结构化文本产物")
    preparation = artifact.generation_attempt.preparation
    if preparation.unit_key != f"project:{stage.production_run.project_id}" or artifact.generation_attempt.unit_key != preparation.unit_key:
        raise ValidationError("文本候选不属于本步骤的项目单元")
    _, number = verify_recipe_preparation_route(preparation, stage=stage, plan=plan, declaration=declaration)
    if number > recipe_routing(declaration)["variant_count"]:
        raise ValidationError("候选序号不属于冻结候选数量")
    return number


@transaction.atomic
def choose_structured_text_candidate(stage, *, actor=None, artifact_id=None):
    """Return the chosen Decision, or None while no configured choice is ready.

Automatic rechecks preserve any existing decision. Explicit manual changes are
allowed before stage completion; completed output references are not rewritten.
"""
    if stage is None:
        raise ValidationError("候选缺少所属步骤")
    run = ProductionRun.objects.select_for_update().select_related("project").get(pk=stage.production_run_id)
    stage = StageRun.objects.select_for_update().select_related("production_run").get(pk=stage.pk, production_run=run)
    plan, declaration = recipe_project_stage_context(stage)
    if not is_structured_text_declaration(declaration):
        raise ValidationError("此选择入口不处理剧本或影视规则候选")
    manual = artifact_id is not None
    if manual:
        if not actor or not actor.is_active or (actor.pk != run.project.owner_id and not actor.is_staff):
            raise PermissionDenied("只能选择当前用户项目的候选")
    elif actor is not None:
        raise ValidationError("自动选择不能填写人工操作者")
    group = f"stage:{stage.pk}:{sha256(f'project:{run.project_id}'.encode()).hexdigest()}"
    previous = ArtifactSelectionDecision.objects.filter(stage_run=stage, variant_group=group).select_related(
        "artifact__generation_attempt__preparation").order_by("-revision").first()
    if previous and (not manual or str(previous.artifact_id) == str(artifact_id)):
        _verify_candidate(previous.artifact, stage=stage, plan=plan, declaration=declaration, group=group)
        if previous.artifact.status != "selected":
            raise ValidationError("已有选择与产物状态不一致，不能静默重选")
        return previous
    if not manual and recipe_routing(declaration)["evaluator"] is None:
        return None
    if run.status in {"succeeded", "failed", "cancelled", "cancel_requested"} or stage.status in {"succeeded", "failed", "cancelled"}:
        raise ValidationError("已结束的运行或步骤不能新增或改写选择")
    candidates = []
    routing = recipe_routing(declaration)
    for number in range(1, routing["variant_count"] + 1):
        choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=f"project:{run.project_id}", candidate_number=number)
        if choice.preparation is None:
            continue
        attempts = list(current_primary_attempts(stage=stage, preparation_ids=[choice.preparation.pk]))
        if len(attempts) > 1:
            raise ValidationError("候选准备存在多个当前主调用")
        if not attempts or attempts[0].status != "succeeded" or attempts[0].ignore_late_result:
            continue
        artifacts = list(GeneratedArtifact.objects.filter(generation_attempt=attempts[0]).select_related(
            "generation_attempt__preparation"))
        if len(artifacts) != 1:
            raise ValidationError("成功文本主调用缺少唯一产物")
        artifact = artifacts[0]
        if artifact.status not in {"available", "selected"} or (manual and str(artifact.pk) != str(artifact_id)):
            continue
        if _verify_candidate(artifact, stage=stage, plan=plan, declaration=declaration, group=group) != number:
            raise ValidationError("候选序号与本次步骤不一致")
        candidates.append(artifact)
        break
    if manual:
        choices = [row for row in candidates if str(row.pk) == str(artifact_id)]
        if len(choices) != 1:
            raise ValidationError("所选产物不是本步骤当前路线的有效成功候选")
        selected = choices[0]
    elif candidates:
        selected = candidates[0]
    else:
        return None
    return _record_artifact_selection(selected, selected_by=actor if manual else None,
        selection_mode="manual" if manual else "automatic")
