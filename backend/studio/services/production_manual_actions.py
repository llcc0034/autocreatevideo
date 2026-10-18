"""Registered manual operations over original production facts, not a queue."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import ApprovalDecision, ProductionRun, StageRun
from studio.services.production_manual_change_impact import MANUAL_CONTENT_CHANGE_STAGES, require_replaceable_content_dependents
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract
from studio.services.production_manual_commands import execute_manual_command, ManualActionResult
from studio.services.production_run_plans import confirmed_script_reference, get_run_execution_plan
from studio.services.production_manual_preparations import prepare_manual_generation
from studio.services.production_manual_retry import retry_manual_primary
from studio.services.production_manual_media import adopt_manual_media
from studio.services.production_manual_media_review import approve_manual_media
from studio.services.production_manual_scripts import approve_manual_script, manual_script_states
from studio.services.production_manual_visual_rules import approve_manual_visual_rules
from studio.services.production_reviewed_visual_rules import visual_rules_review_state
from studio.services.production_manual_bible import perform_manual_bible
from studio.services.production_manual_storyboard import adopt_manual_storyboard
from studio.services.production_manual_bible_images import approve_manual_bible_images
from studio.services.production_manual_storyboard_review import approve_manual_storyboard_references
from studio.services.production_manual_shot_references import select_manual_shot_references
from studio.services.production_manual_assembly import assemble_manual_episode
from studio.services.production_manual_delivery import deliver_manual_run
from studio.services.production_stage_catalog import MANUAL_STAGE_ACTIONS, PROJECT_TEXT_REVIEW_SOURCES, STAGE_EXECUTION_OPERATIONS, STAGE_MEDIA_OUTPUT_KINDS, STAGE_MEDIA_SCOPE_KINDS


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _adopt_confirmed_script(*, run, stage, plan, request, actor):
    payload = request["payload"]
    if set(payload) != {"content", "comment"}:
        raise ValidationError("采用剧本必须明确提交内容引用和人工说明，不接受模型或配方参数")
    if request["scope"] != {"kind": "project", "id": str(run.project_id)} or request["unit_key"] != f"project:{run.project_id}":
        raise ValidationError("剧本采用必须定位当前项目，不能伪造单元或镜头范围")
    state = manual_script_states(run)["script_adoption"]
    if request["expected_selection_revision"] != state["revision"]:
        _conflict("剧本采用修订已变化，请重新读取后决定是否更换")
    require_replaceable_content_dependents(run=run, stage=stage)
    if payload["content"] is None:
        raise ValidationError("采用动作需要一个已确认的真实剧本，不能采用空值")
    content = confirmed_script_reference(str(run.project_id), payload["content"])
    comment = normalize_optional_audit_comment(payload["comment"])
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.script_revision_structures import freeze_script_input_structure

    source = freeze_script_input_structure(project_id=run.project_id, reference=content,
        max_bytes=execution_configuration_max_bytes())
    assign_json_contract(stage, "output_snapshot", {"schema_version": 4, "content": content,
        "structure_source": source}, schema_version=4, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    now = timezone.now()
    stage.status = StageRun.Status.SUCCEEDED
    stage.started_at = stage.started_at or now
    stage.finished_at = now
    stage.reason = ""
    stage.reason_code = ""
    stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256",
        "status", "started_at", "finished_at", "reason", "reason_code", "updated_at"))
    decision = ApprovalDecision.objects.create(stage_run=stage, revision=state["revision"] + 1, decision="approve",
        actor="human", decided_by=actor, comment=comment, output_snapshot_sha256=stage.output_snapshot_sha256)
    run.script_revision_id = content["id"]
    run.script_adoption_decision = decision
    run.started_at = run.started_at or now
    # Completing this action never approves or schedules the next stage.
    run.status = ProductionRun.Status.AWAITING_INPUT
    run.save(update_fields=("script_revision", "script_adoption_decision", "started_at", "status", "updated_at"))
    return ManualActionResult("approval_decision", decision.pk)


# Fixed platform operations, not per-model templates or user-provided code.
_ADAPTERS = {("script_input", "adopt"): _adopt_confirmed_script, ("media_generation", "adopt"): adopt_manual_media,
    ("artifact_selection", "approve"): approve_manual_media,
    ("script_review", "approve"): approve_manual_script,
    ("visual_rules_review", "approve"): approve_manual_visual_rules,
    ("bible_materialize", "adopt"): perform_manual_bible,
    ("bible_review", "approve"): perform_manual_bible,
    ("storyboard", "adopt"): adopt_manual_storyboard,
    ("storyboard_review", "approve"): approve_manual_storyboard_references,
    ("storyboard_review", "select"): select_manual_shot_references,
    ("bible_image_review", "approve"): approve_manual_bible_images,
    ("assembly", "assemble"): assemble_manual_episode,
    ("delivery", "deliver"): deliver_manual_run,
    **{(key, "generate"): prepare_manual_generation for key, actions in MANUAL_STAGE_ACTIONS.items() if "generate" in actions},
    **{(key, "retry"): retry_manual_primary for key, actions in MANUAL_STAGE_ACTIONS.items() if "retry" in actions}}


def _dispatch(*, asset_guard=None, **kwargs):
    request = kwargs["request"]
    if kwargs["run"].plan_source == "recipe" and (request["stage_key"], request["action"]) == ("storyboard_review", "select"):
        from studio.services.production_recipe_shot_reference_commands import select_recipe_shot_references
        return select_recipe_shot_references(**kwargs)
    adapter = _ADAPTERS.get((request["stage_key"], request["action"]))
    if adapter is None:
        raise ValidationError("此人工动作尚未接入原生产服务，未创建任务或成功回执", code="manual_action_not_ready")
    if adapter in (approve_manual_bible_images, assemble_manual_episode):
        return adapter(**kwargs, asset_guard=asset_guard)
    return adapter(**kwargs)


def perform_manual_action(*, run_id, request, actor):
    from studio.services.production_runs import _StoredAssetFileGuard
    # Outside the entire command transaction: a receipt or commit failure must
    # also remove new copies, not only failures inside the image writer.
    with _StoredAssetFileGuard() as guard:
        return execute_manual_command(run_id=run_id, request=request, actor=actor,
            execute=lambda **kwargs: _dispatch(asset_guard=guard, **kwargs))


@transaction.atomic
def manual_action_options(run):
    from studio.services.production_manual_activity import manual_execution_activity
    run = ProductionRun.objects.select_for_update().only("id", "plan_source", "project_id", "recipe_version_id",
        "owner_id", "script_revision_id", "script_adoption_decision_id").get(pk=run.pk)
    if run.plan_source != "manual":
        raise ValidationError("此运行不是无配方人工制作")
    plan = get_run_execution_plan(run)
    frozen_actions = {(row["key"], action) for row in plan.value["stage_catalog"]["value"]["stages"]
        for action in row["manual_actions"]}
    script_states = manual_script_states(run, include_identity=True)
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256,
        "execution_activity": manual_execution_activity(run),
        "content_change_stage_keys": list(MANUAL_CONTENT_CHANGE_STAGES),
        **script_states,
        "visual_rules_review": visual_rules_review_state(run),
        "actions": [{"stage_key": key, "action": action,
            **({"source_stage_keys": sorted(PROJECT_TEXT_REVIEW_SOURCES[key])} if action == "approve" and key in PROJECT_TEXT_REVIEW_SOURCES else {}),
            "operation": STAGE_EXECUTION_OPERATIONS.get(key) if action == "generate" else None,
            "output_kinds": list(STAGE_MEDIA_OUTPUT_KINDS.get(key, ())) if action == "generate" else [],
            "scope_kinds": ["shot"] if action == "select" else ["episode"] if action == "assemble" else list(STAGE_MEDIA_SCOPE_KINDS[key]) if key in STAGE_MEDIA_OUTPUT_KINDS else ["project"]
        } for key, action in _ADAPTERS if (key, action) in frozen_actions]}
