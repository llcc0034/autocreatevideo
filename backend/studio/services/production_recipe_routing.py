"""RunPlan route choices backed by original preparations and StageRouteEvent.

Readers never select the latest successful result. Writers hold Run/Stage locks;
only a definitively failed, settled primary call permits ordered fallback.
"""
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.execution_production_attempts import current_primary_attempts, require_retryable_attempt, require_retryable_failure
from studio.services.production_recipe_routing_contract import validate_recipe_routing_shape


def recipe_routing(declaration, *, appearance_role="primary"):
    from studio.services.production_recipe_contracts import recipe_stage_routings
    recipe_stage_routings(declaration)
    if appearance_role not in {"primary", "variant"} or (appearance_role == "variant"
            and declaration["key"] != "bible_image_generation"):
        raise ValidationError("此阶段不支持所选素材角色")
    key = "variant_routing" if appearance_role == "variant" else "routing"
    return validate_recipe_routing_shape(declaration.get(key), path=key)


def initial_recipe_route_index(stage, declaration, *, appearance_role="primary"):
    routing = recipe_routing(declaration, appearance_role=appearance_role)
    if routing["mode"] != "manual_choice":
        return 0
    index = stage.selected_variant_route_index if appearance_role == "variant" else stage.selected_route_index
    if index is None:
        raise ValidationError("请先明确选择本阶段的执行实例路线。", code="route_selection_required")
    if type(index) is not int or not 0 <= index < len(routing["routes"]):
        raise ValidationError("人工选择不在本轮冻结路线范围内")
    return index


def candidate_request_key(index, number, *, supersedes_id=None):
    return f"route:{index}:candidate:{number}" + (f":replacement:{supersedes_id}" if supersedes_id else "")


def recipe_unit_role(stage, plan, unit_key):
    if stage.stage_key != "bible_image_generation":
        return "primary"
    from studio.services.production_recipe_material_units import frozen_recipe_material_units
    units, _ = frozen_recipe_material_units(stage, plan)
    matches = [unit for unit in units if unit["unit_key"] == unit_key]
    if len(matches) != 1:
        raise ValidationError("候选单元不在本轮冻结的外观需求中")
    return matches[0]["appearance_role"]


def require_recipe_stage_route(stage, declaration, *, appearance_role="primary"):
    """Translate the same validation to the original stage's human-action gate."""
    try:
        return initial_recipe_route_index(stage, declaration, appearance_role=appearance_role)
    except ValidationError as exc:
        if getattr(exc, "code", None) != "route_selection_required":
            raise
        from studio.services.production_runs import StageBlocked
        raise StageBlocked(exc.code, exc.messages[0], awaiting_approval=True) from exc


def _event_unit(unit_key, number):
    return f"{unit_key}:candidate:{number}"


def superseded_recipe_preparation_ids(stage):
    """Read the recorded fallback edge, not full prompts or an inferred latest route."""
    edges = []
    for code, index in stage.route_events.filter(event_type="route_fallback").values_list("code", "route_index"):
        try:
            preparation_id, attempt_id = (UUID(value) for value in code.split(":"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("回退记录缺少原准备及失败调用身份") from exc
        edges.append((attempt_id, preparation_id, index - 1))
    recorded = set(GenerationAttempt.objects.filter(pk__in=[edge[0] for edge in edges],
        stage_run=stage, role="primary", status="failed").values_list("pk", "preparation_id", "route_index"))
    for edge in edges:
        if edge not in recorded:
            raise ValidationError("回退记录与原失败路线不一致")
    return {edge[1] for edge in edges}


def verify_recipe_preparation_route(preparation, *, stage, plan, declaration, detail=None, _visited=frozenset()):
    from studio.services.production_stage_input_amendments import stage_input_amendment_identity
    if preparation.pk in _visited:
        raise ValidationError("候选替代链存在循环")
    detail = read_execution_preparation_detail(preparation, detail=detail)
    request = detail.intent["request"]
    requested = request["requested"]
    index, number = requested.get("route_index"), requested.get("candidate_number")
    routing = recipe_routing(declaration, appearance_role=recipe_unit_role(stage, plan, preparation.unit_key))
    identity = None
    if preparation.supersedes_id:
        from studio.services.execution_configuration_replacement import replacement_route_identity
        identity = replacement_route_identity(preparation, stage=stage, plan=plan, declaration=declaration,
            detail=detail, visited=_visited | {preparation.pk})
    if (type(index) is not int or not 0 <= index < len(routing["routes"])
            or type(number) is not int or number < 1 or preparation.stage_run_id != stage.pk
            or requested.get("run_plan_sha256") != plan.sha256 or requested.get("stage_key") != stage.stage_key
            or requested.get("input_amendment") != stage_input_amendment_identity(stage)
            or preparation.request_key != candidate_request_key(index, number, supersedes_id=preparation.supersedes_id)
            or requested.get("supersedes_preparation_id") != (str(preparation.supersedes_id) if preparation.supersedes_id else None)
            or str(preparation.execution_instance_version_id) != (identity[0] if identity else routing["routes"][index]["execution_instance_version_id"])
            or request.get("definition_sha256") != (identity[1] if identity else routing["routes"][index]["expected_definition_sha256"])
            or request.get("unit_key") != preparation.unit_key or request.get("request_key") != preparation.request_key):
        raise ValidationError("候选准备与本轮冻结路线不一致")
    return index, number


@dataclass(frozen=True)
class RecipeRouteChoice:
    index: int
    preparation: ExecutionPreparation | None = None
    failed_attempt: GenerationAttempt | None = None


def _candidate_keys(preparations, routing, candidate_number):
    """Do not turn an unrecognized request in this unit into an absent one.

    Compare the narrow key index with its original intent key projection; a
    well-formed key renamed to another candidate must not disappear either.
    Other units are out of scope; peer details are not this candidate's source.
    An invalid index cannot be attributed safely, so fail without repairing it.
    """
    result = []
    for key, frozen_key, parent_id in preparations.values_list("request_key", "contract__intent__request__request_key", "supersedes_id"):
        try:
            if key != frozen_key:
                raise ValueError
            parts = key.split(":")
            if len(parts) != (6 if parent_id else 4):
                raise ValueError
            index, number = int(parts[1]), int(parts[3])
            if (not 0 <= index < len(routing["routes"]) or number < 1
                    or candidate_request_key(index, number, supersedes_id=parent_id) != key):
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("本单元存在无法归属冻结路线或候选编号的准备，不能忽略后重新分配",
                code="execution_route_identity_invalid") from exc
        if number == candidate_number:
            result.append(key)
    return result


def read_recipe_candidate_route(*, stage, plan, declaration, unit_key, candidate_number):
    """Read the exact contiguous route chain; does not advance or write events."""
    role = recipe_unit_role(stage, plan, unit_key)
    routing = recipe_routing(declaration, appearance_role=role)
    if type(candidate_number) is not int or candidate_number < 1:
        raise ValidationError("需要明确的候选编号")
    initial = initial_recipe_route_index(stage, declaration, appearance_role=role)
    preparations = ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit_key).order_by()
    keys = _candidate_keys(preparations, routing, candidate_number)
    rows = list(preparations.filter(request_key__in=keys).select_related("contract"))
    chains = {}
    for preparation in rows:
        index, number = verify_recipe_preparation_route(preparation, stage=stage, plan=plan,
            declaration=declaration, detail=getattr(preparation, "contract", None))
        if number != candidate_number:
            raise ValidationError("候选准备的编号与本次读取不一致")
        chains.setdefault(index, []).append(preparation)
    from studio.services.production_recipe_material_repreparation import current_replacement_leaf
    by_index = {index: current_replacement_leaf(chain) for index, chain in chains.items()}
    if not by_index:
        return RecipeRouteChoice(initial)
    indexes = sorted(by_index)
    expected = list(range(initial, indexes[-1] + 1)) if routing["mode"] == "ordered_fallback" else [initial]
    if indexes != expected:
        raise ValidationError("候选准备缺少连续、明确的选路依据，不能按最新记录推断")
    for index in indexes[1:]:
        previous = by_index[index - 1]
        attempts = list(current_primary_attempts(stage=stage, preparation_ids=[previous.pk]))
        if len(attempts) != 1:
            raise ValidationError("回退前的原路线缺少唯一失败主调用")
        require_retryable_failure(attempts[0], automatic=True)
        if not stage.route_events.filter(unit_key=_event_unit(unit_key, candidate_number), route_index=index,
                event_type="route_fallback", code=f"{previous.pk}:{attempts[0].pk}").exists():
            raise ValidationError("回退准备缺少原失败调用的正式选路记录")
    return RecipeRouteChoice(indexes[-1], by_index[indexes[-1]])


def resolve_recipe_candidate_route(**context):
    """Called only by a locked producer; replay itself never retries a call."""
    choice = read_recipe_candidate_route(**context)
    stage, declaration = context["stage"], context["declaration"]
    routing = recipe_routing(declaration, appearance_role=recipe_unit_role(stage, context["plan"], context["unit_key"]))
    if (routing["mode"] != "ordered_fallback" or choice.preparation is None
            or stage.status != "running" or stage.production_run.status != "running"):
        return choice
    attempts = list(current_primary_attempts(stage=stage, preparation_ids=[choice.preparation.pk]))
    if len(attempts) > 1:
        raise ValidationError("同一准备存在多个主调用，不能决定回退")
    if not attempts or attempts[0].status != "failed":
        return choice
    from studio.services.production_execution_recovery import production_retry_decision, RETRY_EXHAUSTED
    # Exhaust the configured same-request budget before the declared fallback.
    # Unsafe failures go to human handling, never to another model.
    if production_retry_decision(attempts[0])["code"] != RETRY_EXHAUSTED:
        return choice
    # Known input/review/admission problems and ambiguous remote outcomes are
    # not a license to silently run another model. Keep their original error.
    require_retryable_attempt(attempts[0], automatic=True)
    if choice.index + 1 == len(routing["routes"]):
        return choice
    return RecipeRouteChoice(choice.index + 1, failed_attempt=attempts[0])


def record_recipe_route_preparation(*, stage, preparation, choice, candidate_number):
    """Within the same Run/Stage transaction as allocation; replays are no-ops."""
    from studio.services.production_runs import _mark_route_selected
    if choice.preparation is not None:
        return
    previous = choice.failed_attempt
    _mark_route_selected(stage, unit_key=_event_unit(preparation.unit_key, candidate_number), route_index=choice.index,
        event_type="route_fallback" if previous else "route_selected",
        reason=f"{previous.preparation_id}:{previous.pk}" if previous else str(preparation.pk))


def require_current_recipe_preparation(preparation, *, stage):
    """Reject submitting/retrying a superseded route, including auxiliary work."""
    from studio.services.production_recipe_preparations import recipe_stage_context
    plan, declaration = recipe_stage_context(stage)
    _, number = verify_recipe_preparation_route(preparation, stage=stage, plan=plan, declaration=declaration)
    choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
        unit_key=preparation.unit_key, candidate_number=number)
    if choice.preparation is None or choice.preparation.pk != preparation.pk:
        raise ValidationError("此候选已进入另一条明确记录的路线，不能重新提交原路线", code="execution_route_superseded")
    from studio.services.production_recipe_media_preparations import require_current_recipe_material_requirement
    require_current_recipe_material_requirement(preparation, stage=stage)
    if recipe_unit_role(stage, plan, preparation.unit_key) == "variant":
        from studio.services.production_recipe_material_inputs import require_current_recipe_material_binding
        require_current_recipe_material_binding(preparation, stage=stage)
