"""Apply configured exact-use defaults in the original recipe review transaction.

No Bible-primary inference, source/slot guessing, generation or human approval.
All shots are planned before any choices are written; unresolved uses wait for
the original manual takeover command. The review owns the resulting evidence.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ProductionRun, Shot, StageRun
from studio.services.production_recipe_shot_references import (
    RecipeShotReferenceSourceConflict, recipe_shot_reference_catalog, recipe_storyboard_reference_source,
    validate_recipe_shot_reference_sources,
)
from studio.services.production_reference_mapping import reference_use_records
from studio.services.production_shot_reference_defaults import (
    plan_configured_shot_reference_choices, read_configured_shot_reference_selection,
)
from studio.services.production_shot_reference_editability import shot_reference_execution_editability
from studio.services.shot_reference_choices import apply_shot_reference_choices, read_shot_reference_choices
from studio.services.shot_reference_sources import _reference_project, _use_rows, resolve_shot_reference_sources


@transaction.atomic
def read_recipe_storyboard_reference_selection(run):
    """Review the original exact uses without choosing or writing approval.

    Both automatic review and the human review adapter must use this boundary.
    A selection hash binds resolved versions/files, not a stale creative snapshot
    or a second set of legacy entity-anchor bindings.
    """
    from studio.services.production_runs import _sha256
    from studio.services.production_recipe_media_preparations import recipe_reference_selection_validator

    run = ProductionRun.objects.select_for_update().select_related("owner").get(pk=run.pk)
    if not run.owner.is_active:
        raise PermissionDenied("运行所有者已失效，不能核对素材")
    _reference_project(run.owner, run.project_id, lock=True)
    review = run.stages.select_for_update().filter(stage_key="storyboard_review").first()
    if review is None:
        raise ValidationError("本轮没有分镜参考审核步骤")
    if run.plan_source != "recipe" or review.mode != "review":
        raise ValidationError("素材核对仅使用原配方分镜参考步骤")
    review.production_run = run
    configured, source_sha, shot_ids = recipe_storyboard_reference_source(run, review)
    shots = {str(shot.pk): shot for shot in Shot.objects.select_for_update().filter(
        pk__in=shot_ids, episode__project_id=run.project_id).order_by("pk")}
    if set(shots) != set(shot_ids):
        raise ValidationError("本轮分镜镜头已缺失或不属于本项目")
    # Read all current choices under the shared project/shot locks. Missing or
    # corrupt selections fail explicitly, never importing a Bible default.
    choices = list(_use_rows(shot_ids).select_for_update())
    resolved = resolve_shot_reference_sources(actor=run.owner, project_id=run.project_id, shot_ids=shot_ids).value
    validate_recipe_shot_reference_sources(stage=review, configured=configured,
        catalog=recipe_shot_reference_catalog(run, review, configured), choices=choices)
    media_stage = run.stages.select_related("production_run__project", "production_run__owner").filter(stage_key="media_generation").first()
    if media_stage is None:
        raise ValidationError("参考审核缺少本轮明确的媒体执行阶段")
    validate_selection = recipe_reference_selection_validator(media_stage)
    for row in resolved["shots"]:
        validate_selection(shot=shots[row["shot_id"]], records=reference_use_records(row["uses"]))
    return {"source_snapshot_sha256": source_sha, "selection_sha256": _sha256(resolved),
        "shot_count": len(shot_ids), "selection": resolved}


@transaction.atomic
def recipe_storyboard_reference_review(run, *, actor):
    """Explicit current human review read, not a list/poll or a choosing action."""
    from studio.services.production_runs import _is_unattended_run

    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    _reference_project(actor, run.project_id, lock=True)
    if (run.plan_source != "recipe" or _is_unattended_run(run)
            or run.status != ProductionRun.Status.AWAITING_APPROVAL
            or run.current_stage_key != "storyboard_review"
            or not run.stages.filter(stage_key="storyboard_review", mode="review",
                status=StageRun.Status.AWAITING_APPROVAL).exists()):
        raise ValidationError("仅当前待人工分镜参考审核可读取此确认合同；自动接管仍沿明确继续")
    result = read_recipe_storyboard_reference_selection(run)
    from studio.services.production_runs import _verify_stage_snapshot
    from studio.services.storyboard_review_display import storyboard_review_display
    source = run.stages.only("output_snapshot", "output_snapshot_schema_version",
        "output_snapshot_size_bytes", "output_snapshot_sha256", "stage_key").get(stage_key="storyboard", status="succeeded")
    result["display"] = storyboard_review_display(project_id=run.project_id,
        canonical=_verify_stage_snapshot(source, "output_snapshot")["canonical"])
    return result


def assert_recipe_storyboard_reference_selection(run, *, actor,
        expected_source_snapshot_sha256, expected_selection_sha256):
    """Called under the original approval transaction; never writes choices."""
    from studio.services.production_runs import ProductionRunConflict

    if not expected_source_snapshot_sha256 or not expected_selection_sha256:
        raise ValidationError("批准分镜参考前必须明确提交所见来源和完整选择摘要")
    reviewed = recipe_storyboard_reference_review(run, actor=actor)
    if (reviewed["source_snapshot_sha256"] != expected_source_snapshot_sha256
            or reviewed["selection_sha256"] != expected_selection_sha256):
        raise ProductionRunConflict({"detail": "分镜来源或确切素材选择已变化，请重新读取并核对；本次未批准也未改选。"},
            code="storyboard_reference_selection_changed")
    return reviewed


@transaction.atomic
def approve_recipe_storyboard_references(run):
    if not run.stages.filter(stage_key="storyboard_review", mode="review", status="running").exists():
        raise ValidationError("自动参考审核仅在原配方运行中的分镜参考步骤执行")
    reviewed = apply_recipe_storyboard_reference_choices(run)
    if reviewed is None:
        return None
    return {"schema_version": 1, "kind": "unattended_storyboard_reference_policy",
        **{key: reviewed[key] for key in ("source_snapshot_sha256", "selection_sha256", "shot_count")}}


@transaction.atomic
def apply_recipe_storyboard_reference_choices(run):
    """Apply the frozen selection rule, independently of the review policy.

    Only the scheduler or explicit Continue invokes this writer. Reads and
    approvals never fill missing choices, and this does not approve a stage.
    """
    run = ProductionRun.objects.select_for_update().select_related("owner").get(pk=run.pk)
    if not run.owner.is_active:
        raise PermissionDenied("运行所有者已失效，不能自动选择素材")
    _reference_project(run.owner, run.project_id, lock=True)
    review = run.stages.select_for_update().get(stage_key="storyboard_review")
    if (run.plan_source != "recipe" or review.mode != "review"
            or review.status not in {"running", "awaiting_approval"}
            or run.current_stage_key != "storyboard_review"
            or run.status not in {"running", "awaiting_approval", "awaiting_input"}):
        raise ValidationError("自动选材仅在原配方分镜参考步骤执行")
    configured, _source_sha, shot_ids = recipe_storyboard_reference_source(run, review)
    shots = {str(shot.pk): shot for shot in Shot.objects.select_for_update().filter(
        pk__in=shot_ids, episode__project_id=run.project_id).order_by("pk")}
    if set(shots) != set(shot_ids):
        raise ValidationError("本轮分镜镜头已缺失或不属于本项目")
    plans, pending = [], []
    for shot_id in shot_ids:
        if configured and configured["rule"]["selection"] == "unique_setting":
            planned = plan_configured_shot_reference_choices(stage_run_id=review.pk, actor=run.owner, shot_id=shot_id)
            plans.append(planned)
            pending.extend(f'镜头{shots[shot_id].number}/{row["use_key"]}：{row["reason"]}'
                for row in planned.items if row["status"] not in {"available", "retained"})
        else:
            state = read_shot_reference_choices(actor=run.owner, project_id=run.project_id, shot_id=shot_id)
            pending.extend(f'镜头{shots[shot_id].number}/{row["use_key"]}：需按本轮明确来源人工选材。'
                for row in state.value["uses"] if not row["source_mode"])
    if pending:
        review.reason_code = "waiting_shot_reference_selection"
        review.reason = ("本轮逐镜素材尚未明确，等待人工接管；不调用模型或记为审核成功。 " + "；".join(pending))[:2000]
        review.save(update_fields=("reason_code", "reason", "updated_at"))
        return None
    try:
        # A retained choice can be valid media yet conflict with this round's
        # usage rule. Roll back every new default before exposing takeover;
        # never silently relabel it or leave earlier shots partially selected.
        with transaction.atomic():
            for planned in plans:
                if not planned.choices:
                    continue
                allowed, code, reason = shot_reference_execution_editability(run, review, planned.state.shot_id)
                if not allowed:
                    raise ValidationError(reason, code=code)
                apply_shot_reference_choices(actor=run.owner, project_id=run.project_id, shot_id=planned.state.shot_id,
                    expected_uses_sha256=planned.state.sha256, choices=planned.choices)
            return read_recipe_storyboard_reference_selection(run)
    except RecipeShotReferenceSourceConflict as exc:
        if not configured["rule"]["allow_override"]:
            raise ValidationError("已有选择与本轮来源冲突，且冻结配方不允许替换；不能转为人工改选。") from exc
        allowed, code, reason = shot_reference_execution_editability(run, review, exc.shot_id)
        if not allowed:
            raise ValidationError(reason, code=code) from exc
        review.reason_code = "waiting_shot_reference_selection"
        review.reason = ("本轮保留了原明确选择，需人工核对使用来源；不调用模型或记为审核成功。 "
            + "；".join(exc.messages))[:2000]
        review.save(update_fields=("reason_code", "reason", "updated_at"))
        return None


def prepare_required_storyboard_reference_choices(run, review):
    """Optional automatic selection before a required human review; no approval."""
    configured = read_configured_shot_reference_selection(review)
    if configured and configured["rule"]["selection"] == "unique_setting":
        selected = apply_recipe_storyboard_reference_choices(run)
        if selected is None:
            review.refresh_from_db(fields=("reason_code", "reason"))
            return review.reason_code, review.reason
    return "stage_approval_required", f"阶段 {review.stage_key} 需要人工审批。"
