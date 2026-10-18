"""Explicit unexecuted variant replacement on the original preparation chain.

No remote cancellation, dispatch, hidden selection, or rewriting frozen inputs.
The caller owns the Run lock and the surrounding Asset file/registry transaction.
"""
from django.core.exceptions import ValidationError
from django.db.models import Q

from studio.models import ExecutionPreparation, GenerationAttempt, GeneratedArtifact
from studio.services.execution_preparations import read_execution_preparation_detail


def _auxiliary_dependencies(preparation, detail):
    """All original batches/attempts, including prior retries, not just latest."""
    from studio.services.execution_auxiliary_requests import production_field_unit, read_production_field_context
    from studio.services.execution_task_links import validate_execution_task_links
    units = [production_field_unit(preparation.pk, row["sha256"])
        for row in detail.resolved_inputs["field_requests"]]
    children = list(ExecutionPreparation.objects.filter(stage_run_id=preparation.stage_run_id,
        unit_key__in=units).order_by("id"))
    for child in children:
        context = read_production_field_context(child)
        if context is None or context[0].pk != preparation.pk:
            raise ValidationError("辅助准备不属于此变体，不能猜测重新准备范围")
    ids = {child.pk for child in children}
    attempts = list(GenerationAttempt.objects.filter(Q(preparation_id__in={preparation.pk, *ids})
        | Q(parent_preparation=preparation)).order_by("id"))
    seen = set()
    for attempt in attempts:
        if (attempt.preparation_id not in ids or attempt.parent_preparation_id != preparation.pk
                or attempt.role != "auxiliary" or attempt.preparation_id in seen):
            raise ValidationError("只能重新准备未执行主调用的变体；已有主调用或辅助归属不一致")
        validate_execution_task_links(attempt)
        seen.add(attempt.preparation_id)
    return children, attempts


def _queued_before_submission(attempt):
    return (attempt.status == "queued" and attempt.claim_token is None
        and attempt.submission_started_at is None and not attempt.provider_request_id
        and not attempt.ignore_late_result and attempt.cancel_requested_at is None
        and attempt.actual_cost is None and attempt.generation_task_id is None)


def _require_settled_auxiliaries(children, attempts, *, stopped):
    called = {attempt.preparation_id for attempt in attempts}
    for child in children:
        if (child.claim_token is not None or child.claim_until is not None
                or (child.status == "dispatched" and child.pk not in called)
                or (child.pk in called and child.status not in {"dispatched", "cancelled"})
                or (stopped and child.pk not in called and child.status != "cancelled")):
            raise ValidationError("辅助准备仍被占用或派发事实不完整，不能重新准备")
    for attempt in attempts:
        if (attempt.claim_token is not None or attempt.error_code == "uncertain_remote_response"
                or (attempt.status not in {"succeeded", "failed", "cancelled", "awaiting_review"}
                    and not (not stopped and _queued_before_submission(attempt)))):
            raise ValidationError("辅助调用仍在途或结果未知；待原调用明确结束后重新读取影响，不取消或重发")


def require_unexecuted_preparation(preparation, *, stopped=False, auxiliaries=None):
    detail = read_execution_preparation_detail(preparation)
    if (not preparation.stage_run_id or preparation.purpose != "production"
            or preparation.claim_token is not None or preparation.claim_until is not None
            or preparation.status in ({"dispatched"} if stopped else {"dispatched", "cancelled"})
            or (stopped and preparation.status != "cancelled")):
        raise ValidationError("只能重新准备未执行且未被工作进程占用的本轮准备")
    children, attempts = auxiliaries if auxiliaries is not None else _auxiliary_dependencies(preparation, detail)
    _require_settled_auxiliaries(children, attempts, stopped=stopped)
    return detail


def require_unexecuted_variant(preparation, *, stopped=False, auxiliaries=None):
    from studio.services.media_generation_targets import read_media_generation_target
    detail = require_unexecuted_preparation(preparation, stopped=stopped, auxiliaries=auxiliaries)
    if read_media_generation_target(preparation, detail=detail).value["appearance"]["role"] != "variant":
        raise ValidationError("只能重新准备本轮变体")
    return detail


def require_replacement_parent(parent, *, owner_ref, unit_key, request_key, requested):
    from studio.services.production_recipe_routing import candidate_request_key
    parent = ExecutionPreparation.objects.get(pk=parent.pk)
    if "configuration_replacement" in requested:
        from studio.services.execution_configuration_replacement import verify_replacement_parent
        detail = verify_replacement_parent(parent, requested)
    else:
        detail = require_unexecuted_variant(parent, stopped=True)
    original = detail.intent["request"]["requested"]
    if (owner_ref != ("stage_run_id", parent.stage_run_id) and
            owner_ref != ("stage_run_id", str(parent.stage_run_id))):
        raise ValidationError("替代准备必须属于同一原阶段")
    if (parent.unit_key != unit_key or requested.get("supersedes_preparation_id") != str(parent.pk)
            or request_key != candidate_request_key(original["route_index"], original["candidate_number"], supersedes_id=parent.pk)
            or any(requested.get(key) != original.get(key) for key in (
                "stage_key", "scope", "run_plan_sha256", "route_index", "candidate_number", "input_amendment"))):
        raise ValidationError("重新准备不能改变本轮单元、路线、候选或冻结计划")


def current_replacement_leaf(rows):
    """Resolve explicit links, never the newest timestamp or highest UUID."""
    by_id = {row.pk: row for row in rows}
    roots = [row for row in rows if row.supersedes_id is None]
    children = {}
    for row in rows:
        if row.supersedes_id is not None:
            if row.supersedes_id not in by_id or row.supersedes_id in children:
                raise ValidationError("候选替代链缺少确切原准备或出现分叉")
            parent = by_id[row.supersedes_id]
            detail = read_execution_preparation_detail(row)
            require_replacement_parent(parent, owner_ref=("stage_run_id", row.stage_run_id),
                unit_key=row.unit_key, request_key=row.request_key, requested=detail.intent["request"]["requested"])
            children[parent.pk] = row
    if len(roots) != 1:
        raise ValidationError("同一候选路线缺少唯一原准备")
    current, visited = roots[0], set()
    while current.pk not in visited:
        visited.add(current.pk)
        if current.pk not in children:
            if visited != set(by_id):
                raise ValidationError("候选替代链存在未连接的准备")
            return current
        current = children[current.pk]
    raise ValidationError("候选替代链存在循环")


def dependency_impact(*, stage, context):
    """Project current candidates from original routes; no second dependency ledger.

    Caller holds the Run lock, also used by preparation/attempt writers. A
    successful candidate is historical evidence, not authority to resubmit it.
    """
    from studio.services.production_recipe_material_selection import _dependent_preparations
    from studio.services.production_recipe_media_preparations import read_recipe_media_candidate
    from studio.services.production_recipe_routing import read_recipe_candidate_route
    from studio.services.execution_production_attempts import current_primary_attempts
    from studio.services.production_recipe_material_selection import _candidate, _context
    from studio.services.json_contracts import json_contract
    from studio.services.execution_instances import execution_configuration_max_bytes
    from rest_framework.exceptions import ValidationError as APIValidationError
    plan, declaration = context[:2]
    dependencies = _dependent_preparations(stage, context)
    names = {str(row["id"]): row["reference_name"] for row in dependencies}
    rows = list(ExecutionPreparation.objects.select_for_update(of=("self",)).filter(
        pk__in=names).select_related("contract").order_by("id"))
    result, current, auxiliary_evidence = [], [], []
    for row in rows:
        _, number = read_recipe_media_candidate(row, stage=stage, plan=plan, declaration=declaration)
        choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=row.unit_key, candidate_number=number)
        if choice.preparation is None:
            raise ValidationError("依赖候选缺少当前准备，不能猜测改选影响")
        if choice.preparation.pk != row.pk:
            continue  # Explicitly superseded route/replacement, not latest-by-time.
        current.append(row)
        try:
            attempts = list(current_primary_attempts(stage=stage, preparation_ids=[row.pk]))
            if (row.status == "dispatched" and row.claim_token is None and row.claim_until is None
                    and len(attempts) == 1 and attempts[0].status == "succeeded" and not attempts[0].ignore_late_result):
                artifacts = list(GeneratedArtifact.objects.filter(generation_attempt=attempts[0]).select_related(
                    "generation_attempt__preparation"))
                if len(artifacts) != 1:
                    raise ValidationError("已完成变体缺少唯一真实产物，不能作为可保留结果")
                _candidate(artifacts[0], stage=stage, context=_context(stage, row.unit_key), current=True)
                action, reason = "retain", "已生成：保留原主图依据和结果，不重新调用。"
            else:
                children, calls = _auxiliary_dependencies(row, read_execution_preparation_detail(row))
                # Include the precise child state even when the parent's revision
                # did not change (e.g. a worker claim or a failed response).
                auxiliary_evidence.append({"parent": str(row.pk),
                    "preparations": [{"id": str(child.pk), "revision": child.revision, "status": child.status,
                        "claim_token": str(child.claim_token), "claim_until": str(child.claim_until)} for child in children],
                    "attempts": [{"id": str(call.pk), "status": call.status, "claim_token": str(call.claim_token),
                        "submission_started_at": str(call.submission_started_at), "request_sha256": call.request_sha256,
                        "ignore_late_result": call.ignore_late_result, "error_code": call.error_code,
                        "provider_request_id": call.provider_request_id, "cancel_requested_at": str(call.cancel_requested_at)}
                        for call in calls]})
                require_unexecuted_variant(row, auxiliaries=(children, calls))
                action = "reprepare"
                reason = ("主调用尚未开始：停止旧准备及未提交辅助任务，按新主图重新准备；"
                    "已结束的辅助请求、结果及费用保留在旧准备，不移植到新请求。")
        except (ValidationError, APIValidationError) as exc:
            action, reason = "blocked", str(exc)
        result.append({"id": str(row.pk), "unit_key": row.unit_key, "reference_name": names[str(row.pk)],
            "status": row.status, "revision": row.revision, "action": action, "reason": reason})
    digest = json_contract({"dependencies": result, "auxiliaries": auxiliary_evidence},
        max_bytes=execution_configuration_max_bytes()).sha256
    return result, digest, current


def replacement_candidates(*, stage, context, expected_ids=None, expected_sha256=None):
    from studio.services.production_runs import ProductionRunConflict
    impact, digest, rows = dependency_impact(stage=stage, context=context)
    if expected_sha256 is not None:
        if expected_sha256 != digest:
            raise ProductionRunConflict({"detail": "变体准备状态或影响已改变，请重新读取后确认。"},
                code="material_dependencies_changed")
    elif (not isinstance(expected_ids, list) or not expected_ids or any(not isinstance(value, str) for value in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
            or {str(row.pk) for row in rows} != set(expected_ids)):
        raise ProductionRunConflict({"detail": "变体准备清单已改变，请重新读取改选影响。"}, code="material_dependencies_changed")
    for item in impact:
        if item["action"] == "blocked":
            raise ValidationError(item["reason"])
    return [row for row, item in zip(rows, impact) if item["action"] == "reprepare"], bool(rows)


def reprepare_variants(*, stage, preparations, actor):
    from studio.models import ProductionRun
    from studio.services.execution_production_cancellation import cancel_production_attempts, stop_production_preparations
    from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
    result = []
    for preparation in preparations:
        detail = read_execution_preparation_detail(preparation)
        children, attempts = _auxiliary_dependencies(preparation, detail)
        require_unexecuted_variant(preparation, auxiliaries=(children, attempts))
        intent = detail.intent["request"]["requested"]
        # Keep the old contract, parameters and any already frozen request.
        queued = [attempt for attempt in attempts if _queued_before_submission(attempt)]
        terminal = {attempt.preparation_id for attempt in attempts if attempt not in queued}
        stop_production_preparations([preparation, *(child for child in children if child.pk not in terminal)], actor=actor)
        cancel_production_attempts(run=ProductionRun.objects.get(pk=stage.production_run_id), attempts=queued, actor=actor)
        result.append(prepare_recipe_media_unit(stage_run_id=stage.pk,
            expected_run_plan_sha256=intent["run_plan_sha256"], scope=intent["scope"],
            candidate_number=intent["candidate_number"], actor=actor,
            material_unit_key=preparation.unit_key, supersedes=preparation))
    return result
