"""Original project text stages consume RunPlan and independent candidates.

No provider I/O, alternate queue or low-level resource route.
Candidate bodies remain on their original Artifact/ScriptRevision details.
"""
from django.core.exceptions import ValidationError

from studio.models import GeneratedArtifact, GenerationAttempt
from studio.services.execution_production_attempts import current_primary_attempts
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import recipe_stage_kind
from studio.services.production_recipe_preparations import prepare_recipe_project_stage, recipe_project_stage_context, recipe_stage_context
from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS, PROJECT_TEXT_REVIEW_SOURCES
from studio.services.production_recipe_routing import require_recipe_stage_route


def is_project_text_stage(stage):
    if stage.mode != "ai":
        return False
    _, declaration = recipe_stage_context(stage)
    return STAGE_EXECUTION_OPERATIONS.get(recipe_stage_kind(declaration)) in PRODUCTION_TEXT_OPERATIONS


def project_text_stage_contract(stage):
    plan, declaration = recipe_stage_context(stage)
    # Validate the effective amendment above, but preserve the original stage
    # identity. The preparation separately freezes the amendment ID and digest.
    declaration = plan.value["recipe"]["definition"]["stages"][stage.ordinal - 1]
    return json_contract({"schema_version": 1, "stage_key": stage.stage_key,
        "run_plan_sha256": plan.sha256, "stage_declaration_sha256": json_contract(
            declaration, max_bytes=execution_configuration_max_bytes()).sha256},
        max_bytes=execution_configuration_max_bytes())


def is_project_text_review(stage):
    return stage.mode == "review" and stage.stage_key in PROJECT_TEXT_REVIEW_SOURCES


def text_review_source(stage):
    """Resolve the unique declared producer, never the latest successful row."""
    plan, declaration = recipe_stage_context(stage)
    declarations = plan.value["recipe"]["definition"]["stages"]
    keys = [row["key"] for row in declarations[:declarations.index(declaration)]
        if row["key"] in PROJECT_TEXT_REVIEW_SOURCES[stage.stage_key] and row["mode"] == "ai"]
    if len(keys) != 1:
        raise ValidationError("文本审核必须对应唯一的已声明生成阶段，不能推断采用哪个候选")
    source = stage.production_run.stages.filter(stage_key=keys[0], ordinal__lt=stage.ordinal).first()
    if source is None or source.status != "succeeded":
        raise ValidationError("文本审核的生成阶段尚未完成")
    return source


def project_text_input_contract(stage):
    value = project_text_stage_contract(stage).value
    if is_project_text_review(stage):
        source = text_review_source(stage)
        value["candidate_source"] = {"stage_id": str(source.pk), "stage_key": source.stage_key,
            "output_snapshot_sha256": source.output_snapshot_sha256}
    return json_contract(value, max_bytes=execution_configuration_max_bytes())


def project_text_candidate_manifest(stage, preparation_ids):
    """Bounded narrow manifest, not a copy of results or an approval fact."""
    plan, declaration = recipe_project_stage_context(stage)
    operation = STAGE_EXECUTION_OPERATIONS[recipe_stage_kind(declaration)]
    attempts = list(current_primary_attempts(stage=stage, preparation_ids=preparation_ids)
        .values("id", "preparation_id", "unit_key", "operation", "status", "ignore_late_result", "request_sha256", "result_sha256")
        .order_by("preparation_id", "id"))
    if (len(attempts) != len(preparation_ids) or {row["preparation_id"] for row in attempts} != set(preparation_ids)
            or any(row["status"] != "succeeded" or row["ignore_late_result"] or not row["result_sha256"]
                or row["operation"] != operation
                or row["unit_key"] != f"project:{stage.production_run.project_id}" for row in attempts)):
        raise ValidationError("候选主调用尚未全部成功或与本阶段准备不一致")
    artifacts = list(GeneratedArtifact.objects.filter(stage_run=stage, production_run_id=stage.production_run_id,
        generation_attempt_id__in=[row["id"] for row in attempts]).values(
            "id", "generation_attempt_id", "status", "kind", "role", "size_bytes", "sha256", "manifest_metadata_sha256")
        .order_by("generation_attempt_id", "id"))
    by_attempt = {row["generation_attempt_id"]: row for row in artifacts}
    if (len(artifacts) != len(attempts) or len(by_attempt) != len(attempts)
            or any(row["status"] not in {"available", "selected"} or not row["sha256"]
                or not row["manifest_metadata_sha256"] or row["size_bytes"] is None for row in artifacts)):
        raise ValidationError("成功主调用缺少唯一的有效候选产物，不能把阶段标为生成完成")
    members = [{"preparation_id": str(row["preparation_id"]), "attempt_id": str(row["id"]),
        "request_sha256": row["request_sha256"], "result_sha256": row["result_sha256"],
        "artifact_id": str(by_attempt[row["id"]]["id"]), "artifact_sha256": by_attempt[row["id"]]["sha256"],
        "artifact_metadata_sha256": by_attempt[row["id"]]["manifest_metadata_sha256"]} for row in attempts]
    manifest = json_contract({"schema_version": 1, "members": members}, max_bytes=execution_configuration_max_bytes())
    return {"kind": "execution_candidates", "stage_key": stage.stage_key,
        "unit_key": f"project:{stage.production_run.project_id}", "candidate_count": len(members),
        "manifest_sha256": manifest.sha256, "run_plan_sha256": plan.sha256}


def advance_recipe_project_text_stage(run, stage, *, human_initiated=False):
    """Native text awaits its business review; generic text uses its evaluator."""
    from studio.services.production_runs import StageBlocked, _save_stage_snapshot
    from studio.services.production_structured_text_selection import choose_structured_text_candidate, is_structured_text_declaration

    if stage.production_run_id != run.pk:
        raise ValidationError("阶段不属于当前运行")
    if not run.owner.is_active:
        raise StageBlocked("execution_owner_unavailable", "运行所有者已停用，不能开始新的模型调用。")
    plan, declaration = recipe_project_stage_context(stage)
    require_recipe_stage_route(stage, declaration)
    structured = is_structured_text_declaration(declaration)

    def selected_output():
        decision = choose_structured_text_candidate(stage)
        if decision is None:
            return None
        summary = project_text_candidate_manifest(stage, [decision.artifact.generation_attempt.preparation_id])
        summary["selection"] = {"decision_id": str(decision.pk), "artifact_id": str(decision.artifact_id),
            "selection_mode": decision.selection_mode,
            "selected_by": str(decision.selected_by_id) if decision.selected_by_id is not None else None}
        summary["requested_candidate_count"] = declaration["routing"]["variant_count"]
        _save_stage_snapshot(stage, "output_snapshot", summary)
        return f"stage-candidates:{stage.pk}"

    if structured:
        output = selected_output()
        if output is not None:
            return output
    preparations = []
    for candidate_number in range(1, declaration["routing"]["variant_count"] + 1):
        preparation = prepare_recipe_project_stage(stage_run_id=stage.pk, expected_run_plan_sha256=plan.sha256,
            candidate_number=candidate_number, actor=run.owner)
        preparation = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=run.owner, human_initiated=human_initiated)
        preparations.append(preparation)
    ids = [row.pk for row in preparations]
    from studio.services.production_execution_recovery import failed_primary_recovery, RETRY_WAIT, active_primary_retry_feedback
    recovery = failed_primary_recovery(stage, ids)
    if recovery:
        stage.reason_code, stage.reason = recovery
        if stage.reason_code != RETRY_WAIT:
            raise StageBlocked(stage.reason_code, stage.reason,
                awaiting_approval=stage.reason_code == "execution_resource_confirmation_required")
        return None
    attempts = list(current_primary_attempts(stage=stage, preparation_ids=ids)
        .values("preparation_id", "status", "ignore_late_result", "error_code").order_by("preparation_id", "id"))
    if len({row["preparation_id"] for row in attempts}) != len(attempts):
        raise ValidationError("同一准备对应多个主调用，不能自动选择最新结果")
    if structured:
        output = selected_output()
        if output is not None:
            return output
        if any(row["status"] == "succeeded" and not row["ignore_late_result"] for row in attempts):
            if declaration["routing"]["evaluator"] is None:
                stage.reason_code, stage.reason = "execution_candidate_selection_required", "已有成功候选；本步骤配置为人工选择，请明确选择后继续。其他候选的失败仍保留，不会自动重试。"
                return None
            if len(attempts) == len(ids) and all(row["status"] in {"succeeded", "failed", "cancelled"} for row in attempts):
                raise StageBlocked("execution_candidates_unavailable", "成功调用暂没有可采用产物；请核对候选状态，不会将已拒绝内容自动采用。")
    elif len(attempts) == len(ids) and all(row["status"] == "succeeded" and not row["ignore_late_result"] for row in attempts):
        summary = project_text_candidate_manifest(stage, ids)
        _save_stage_snapshot(stage, "output_snapshot", summary)
        return f"stage-candidates:{stage.pk}"
    if len(attempts) == len(ids) and all(row["status"] in {"succeeded", "failed", "cancelled"} for row in attempts):
        failed = sum(row["status"] != "succeeded" or row["ignore_late_result"] for row in attempts)
        raise StageBlocked("execution_candidates_incomplete", f"{len(ids)} 个候选中有 {failed} 个未成功；请核对原任务，不会自动重试或采用其他候选。")
    if any(row["status"] == "awaiting_review" for row in attempts):
        stage.reason_code, stage.reason = "execution_output_review_required", "响应格式、输出长度或服务容量需要人工处理。原输入及可用返回内容已保留，请进入对应执行准备查看具体原因；不会自动重跑。"
        return None
    waiting = [row for row in preparations if row.status in {"awaiting_input", "awaiting_review", "blocked", "cancelled"}]
    if waiting:
        stage.reason_code = "execution_preparation_review_required" if any(row.status == "awaiting_review" for row in waiting) else "waiting_execution_preparation"
        stage.reason = f"{len(waiting)} 个候选的执行准备尚未就绪，请查看对应准备的字段审核或准入原因。"
    else:
        stage.reason_code = "waiting_external_task"
        stage.reason = (f"正在生成 {len(ids)} 个独立候选；按本步骤配置自动或人工选择。" if structured
            else f"正在生成 {len(ids)} 个独立候选；生成完成后仍需审核采用。")
        recovery = active_primary_retry_feedback(stage, ids)
        if recovery:
            stage.reason_code, stage.reason = recovery
    return None
