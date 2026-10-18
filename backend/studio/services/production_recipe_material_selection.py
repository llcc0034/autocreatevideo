"""Select appearance candidates on the original artifact decision ledger.

The original stage consumes its role-specific choice and exposes exact primary
dependencies, without Bible adoption or Asset creation. The public comparison
uses this same writer and checks its observed decision under the Run lock.
"""
from hashlib import sha256

from django.core.exceptions import PermissionDenied, ValidationError

from studio.models import ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.execution_production_attempts import current_primary_attempts
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.generated_artifacts import _artifact_limit, _record_artifact_selection
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_material_units import frozen_recipe_material_units
from studio.services.production_recipe_media_preparations import (
    read_recipe_media_candidate, recipe_media_stage_context, require_current_recipe_material_requirement,
)
from studio.services.production_recipe_routing import read_recipe_candidate_route, recipe_routing


def _context(stage, unit_key):
    plan, declaration = recipe_media_stage_context(stage, None)
    units, requirements = frozen_recipe_material_units(stage, plan)
    matches = [row for row in units if row["unit_key"] == unit_key]
    if len(matches) != 1:
        raise ValidationError("选择目标不在本轮冻结的外观需求中")
    unit = matches[0]
    target = next(row for row in requirements["targets"] if row["requirement_key"] == unit["requirement_key"])
    group = f"stage:{stage.pk}:unit:{sha256(unit_key.encode()).hexdigest()}"
    return plan, declaration, unit, target, group


def _candidate(artifact, *, stage, context, current):
    plan, declaration, unit, target, group = context
    if (artifact.production_run_id != stage.production_run_id or artifact.stage_run_id != stage.pk
            or artifact.variant_group != group or artifact.kind != "image" or not artifact.generation_attempt_id):
        raise ValidationError("所选图片不是本轮外观单元的生成候选")
    media = read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(stage.production_run))
    preparation = artifact.generation_attempt.preparation
    scope, number = read_recipe_media_candidate(preparation, stage=stage, plan=plan, declaration=declaration)
    generated = read_media_generation_target(preparation).value
    if (preparation.unit_key != unit["unit_key"] or scope != unit["scope"]
            or media["unit_key"] != unit["unit_key"] or generated["setting"] != target["setting"]
            or generated["appearance"]["role"] != unit["appearance_role"]):
        raise ValidationError("候选与本轮确切外观设定不一致")
    if current:
        choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=unit["unit_key"], candidate_number=number)
        attempts = list(current_primary_attempts(stage=stage, preparation_ids=[preparation.pk]).values_list("pk", flat=True))
        if (choice.preparation is None or choice.preparation.pk != preparation.pk
                or attempts != [artifact.generation_attempt_id]):
            raise ValidationError("所选候选不是当前路线的唯一主调用结果")
        require_current_recipe_material_requirement(preparation, stage=stage)
    return preparation, number, media


def read_recipe_material_selection(*, stage, unit_key, decision_id=None, require_current=True):
    """Return an exact existing selection, never infer it from Asset/latest image.

    Internal callers own visibility and hold the original Run lock when consuming
    this as new-input authority. An explicit historical decision can be read without
    reapplying today's choice or M3 approval; it does not grant new dispatch rights.
    """
    if not require_current and decision_id is None:
        raise ValidationError("历史素材选择必须给出确切决定，不能猜测最新记录")
    context = _context(stage, unit_key)
    group = context[-1]
    rows = ArtifactSelectionDecision.objects.filter(stage_run=stage, variant_group=group).select_related(
        "artifact__generation_attempt__preparation")
    decision = rows.filter(pk=decision_id).first() if decision_id is not None else rows.order_by("-revision").first()
    if decision is None:
        if decision_id is not None:
            raise ValidationError("确切素材选择不存在或不属于本轮外观单元")
        if GeneratedArtifact.objects.filter(stage_run=stage, variant_group=group, status="selected").exists():
            raise ValidationError("已选候选缺少原选择记录，不能自动补造")
        return None
    if ((decision.selection_mode == "manual" and not decision.selected_by_id)
            or (decision.selection_mode == "automatic" and decision.selected_by_id)
            or decision.selection_mode not in {"manual", "automatic"}):
        raise ValidationError("素材选择缺少真实的人工或自动来源")
    if require_current:
        selected = list(GeneratedArtifact.objects.filter(stage_run=stage, variant_group=group,
            status="selected").values_list("pk", flat=True))
        if (rows.order_by("-revision").values_list("pk", flat=True).first() != decision.pk
                or selected != [decision.artifact_id]):
            raise ValidationError("素材选择已改变或与候选状态不一致，不能静默换图")
    preparation, number, media = _candidate(decision.artifact, stage=stage, context=context, current=require_current)
    return {"decision": decision, "artifact": decision.artifact, "preparation": preparation,
        "candidate_number": number, "media": media, "unit": context[2], "target": context[3]}


def recipe_primary_dependency_target(*, stage, unit_key):
    """One exact dependency check for generated and explicitly reused primaries."""
    plan, _, unit, target, _ = _context(stage, unit_key)
    if unit["appearance_role"] != "variant":
        raise ValidationError("只有变体单元可以读取主图依赖选择")
    units, _ = frozen_recipe_material_units(stage, plan)
    dependency = unit["primary_dependency"]
    matches = [row for row in units if row["unit_key"] == dependency["unit_key"]
        and row["requirement_key"] == dependency["requirement_key"] and row["appearance_role"] == "primary"
        and row["scope"] == unit["scope"]]
    if len(matches) != 1:
        raise ValidationError("本轮生成需求没有变体所需的确切主图修订，请明确素材来源，不能改用当前主图")
    return matches[0], target["primary_dependency"]


def read_recipe_primary_dependency(*, stage, unit_key):
    """Read the original generated-candidate decision, never infer a library choice."""
    primary_unit, expected_setting = recipe_primary_dependency_target(stage=stage, unit_key=unit_key)
    selection = read_recipe_material_selection(stage=stage, unit_key=primary_unit["unit_key"])
    if selection is not None and selection["target"]["setting"] != expected_setting:
        raise ValidationError("主图选择不匹配变体固定的设定依赖")
    return selection


def _manual_owner(stage, actor):
    run = stage.production_run
    if (not actor or not actor.is_authenticated or not actor.is_active
            or (not actor.is_staff and actor.pk != run.owner_id)):
        raise PermissionDenied("无权选择此制作运行的候选")
    if not run.owner.is_active:
        raise PermissionDenied("运行所有者已停用，不能选择素材")


def _dependent_preparations(stage, context):
    plan, _, unit, _, _ = context
    if unit["appearance_role"] != "primary":
        return []
    units, requirements = frozen_recipe_material_units(stage, plan)
    keys = [row["unit_key"] for row in units if row["primary_dependency"] is not None
        and row["primary_dependency"]["unit_key"] == unit["unit_key"]]
    names = {f'appearance:{row["setting"]["appearance_id"]}': row["reference_name"]
        for row in requirements["targets"]}
    # This is an impact projection, not another dependency ledger. Even a
    # cancelled preparation has frozen evidence; don't rewrite it on selection.
    return [{**row, "reference_name": names[row["unit_key"]]} for row in
        ExecutionPreparation.objects.filter(stage_run=stage, unit_key__in=keys)
        .order_by("created_at", "id").values("id", "unit_key", "status")]


DEPENDENCY_CHANGE_REASON = "已有变体准备冻结了原主图；请重新读取并确认具体改选影响，不能直接覆盖原依据。"


def recipe_material_reselection_pending(*, stage, context, previous):
    """An explicit append after a choice needs a new choice, not a late-result swap.

    Read the original action and preparation evidence. A later fallback route
    for an older action does not reopen selection just because it was created later.
    """
    if previous is None:
        return False
    events = set(stage.route_events.filter(event_type="bible_regeneration_preparing",
        created_at__gt=previous["decision"].created_at).values_list("unit_key", "code"))
    if not events:
        return False
    from studio.services.execution_preparations import read_execution_preparation_detail
    plan, declaration, unit = context[:3]
    for preparation in ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit["unit_key"]).select_related("contract"):
        read_recipe_media_candidate(preparation, stage=stage, plan=plan, declaration=declaration)
        request = read_execution_preparation_detail(preparation).intent["request"]["requested"].get("generation_request")
        if request and (f'bible-regenerate:{request["request_id"]}', request["request_sha256"]) in events:
            return True
    return False


def recipe_material_selection_context(*, artifact, actor):
    """Authorized, on-demand read of one candidate and its change impact."""
    if not artifact.stage_run_id or not artifact.generation_attempt_id:
        raise ValidationError("所选图片缺少原阶段或主调用")
    with locked_preparation_owner_rows([("stage_run_id", artifact.stage_run_id)]) as owners:
        stage = owners[("stage_run_id", str(artifact.stage_run_id))]
        _manual_owner(stage, actor)
        if stage.production_run.plan_source != "recipe":
            raise ValidationError("无配方制作请使用对应步骤的人工动作")
        unit_key = artifact.generation_attempt.preparation.unit_key
        context = _context(stage, unit_key)
        _, number, _ = _candidate(artifact, stage=stage, context=context, current=True)
        previous = read_recipe_material_selection(stage=stage, unit_key=unit_key)
        from studio.services.production_recipe_material_repreparation import dependency_impact
        dependencies, digest, _ = dependency_impact(stage=stage, context=context)
        reason = ""
        if (previous and previous["artifact"].pk == artifact.pk
                and not recipe_material_reselection_pending(stage=stage, context=context, previous=previous)):
            reason = "此候选已是当前选择，无需重复提交。"
        elif stage.production_run.status != "running" or stage.status != "running":
            reason = "只有运行中的素材阶段可以新增或改变依赖选择。"
        elif any(row["action"] == "blocked" for row in dependencies):
            reason = "存在不能安全处理的变体依赖，请先处理下列原准备；不会取消、替换或重发在途及结果未知调用。"
        return {"artifact_id": str(artifact.pk), "stage_run_id": str(stage.pk), "unit_key": unit_key,
            "appearance_role": context[2]["appearance_role"], "reference_name": context[3]["reference_name"],
            "candidate_number": number,
            "expected_decision_id": str(previous["decision"].pk) if previous else None,
            "selected_artifact_id": str(previous["artifact"].pk) if previous else None,
            "can_select": not reason, "reason": reason, "dependent_preparations": dependencies,
            "dependencies_sha256": digest}


def choose_recipe_material_candidate(*, stage_run_id, unit_key, actor=None, artifact_id=None, observed_selection=None):
    """Internal role-aware choice; no new queue, file, adoption or human approval."""
    if observed_selection is not None and "expected_dependencies_sha256" in observed_selection:
        return change_recipe_primary_and_reprepare(stage_run_id=stage_run_id, unit_key=unit_key,
            actor=actor, artifact_id=artifact_id, expected_decision_id=observed_selection["expected_decision_id"],
            expected_dependencies_sha256=observed_selection["expected_dependencies_sha256"])
    return _choose_recipe_material_candidate(stage_run_id=stage_run_id, unit_key=unit_key, actor=actor,
        artifact_id=artifact_id, observed_selection=observed_selection)


def change_recipe_primary_and_reprepare(*, stage_run_id, unit_key, actor, artifact_id,
        expected_decision_id, expected_preparation_ids=None, expected_dependencies_sha256=None):
    """Original selection transaction, shared by public confirmation and internal tests.

    Main calls must not have started. Settled auxiliary evidence stays on the
    old preparation; only provably unsubmitted queue entries may be stopped.
    """
    from studio.services.production_runs import _StoredAssetFileGuard
    from studio.services.registry_reference_lock import registry_reference_write_guard
    with _StoredAssetFileGuard() as guard, registry_reference_write_guard():
        return _choose_recipe_material_candidate(stage_run_id=stage_run_id, unit_key=unit_key,
            actor=actor, artifact_id=artifact_id, observed_selection={"expected_decision_id": expected_decision_id},
            dependency_change={"expected_preparation_ids": expected_preparation_ids,
                "expected_dependencies_sha256": expected_dependencies_sha256, "asset_guard": guard})


def _choose_recipe_material_candidate(*, stage_run_id, unit_key, actor=None, artifact_id=None,
        observed_selection=None, dependency_change=None):
    manual = artifact_id is not None
    if not manual and actor is not None:
        raise ValidationError("自动选择不能填写人工操作者")
    if manual and (not actor or not actor.is_authenticated or not actor.is_active):
        raise PermissionDenied("人工选择需要有效登录身份")
    with locked_preparation_owner_rows([("stage_run_id", stage_run_id)]) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        run = stage.production_run
        if manual:
            _manual_owner(stage, actor)
        if not run.owner.is_active:
            raise PermissionDenied("运行所有者已停用，不能选择素材")
        context = _context(stage, unit_key)
        plan, declaration, unit, _, group = context
        if dependency_change is not None and not manual:
            raise ValidationError("重新准备只能由明确的人工选择确认发起")
        previous = read_recipe_material_selection(stage=stage, unit_key=unit_key)
        reselection = recipe_material_reselection_pending(stage=stage, context=context, previous=previous)
        if reselection and not manual:
            return None  # The explicit manual append is not permission for automatic re-selection.
        if previous and not reselection and (not manual or str(previous["artifact"].pk) == str(artifact_id)):
            return previous["decision"]
        if manual and observed_selection is not None:
            from studio.services.production_runs import ProductionRunConflict
            expected = observed_selection["expected_decision_id"]
            actual = previous["decision"].pk if previous else None
            if (str(expected) if expected else None) != (str(actual) if actual else None):
                raise ProductionRunConflict({"detail": "当前选择已改变，请重新查看候选和影响后确认。"},
                    code="material_selection_changed")
        routing = recipe_routing(declaration, appearance_role=unit["appearance_role"])
        if not manual and routing["evaluator"] is None:
            return None
        if run.status != "running" or stage.status != "running":
            raise ValidationError("只有运行中的素材阶段可以新增或改变依赖选择")
        if manual:
            artifact = GeneratedArtifact.objects.filter(pk=artifact_id, stage_run=stage,
                variant_group=group).select_related("generation_attempt__preparation").first()
            if artifact is None:
                raise ValidationError("所选图片不是本轮外观单元的生成候选")
            _candidate(artifact, stage=stage, context=context, current=True)
            replacements = []
            affected = False
            if dependency_change is not None:
                from studio.services.production_recipe_material_repreparation import replacement_candidates
                replacements, affected = replacement_candidates(stage=stage, context=context,
                    expected_ids=dependency_change["expected_preparation_ids"],
                    expected_sha256=dependency_change["expected_dependencies_sha256"])
            elif _dependent_preparations(stage, context):
                from studio.services.production_runs import ProductionRunConflict
                raise ProductionRunConflict({"detail": DEPENDENCY_CHANGE_REASON}, code="material_dependency_change_required")
        else:
            # Preserve the existing evaluator's candidate-number ordering, not
            # completion time. Additional candidates still need original action
            # evidence; malformed keys are not treated as absent preparations.
            numbers = set(range(1, routing["variant_count"] + 1))
            for row in ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit_key).select_related("contract"):
                _, number = read_recipe_media_candidate(row, stage=stage, plan=plan, declaration=declaration)
                numbers.add(number)
            artifact = None
            for number in sorted(numbers):
                choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
                    unit_key=unit_key, candidate_number=number)
                if choice.preparation is None:
                    continue
                attempts = list(current_primary_attempts(stage=stage, preparation_ids=[choice.preparation.pk])
                    .only("id", "status", "ignore_late_result"))
                if len(attempts) > 1:
                    raise ValidationError("候选准备存在多个当前主调用")
                if not attempts or attempts[0].status != "succeeded" or attempts[0].ignore_late_result:
                    continue
                artifacts = list(GeneratedArtifact.objects.filter(generation_attempt=attempts[0]).select_related(
                    "generation_attempt__preparation"))
                if len(artifacts) != 1:
                    raise ValidationError("成功图片主调用缺少唯一候选产物")
                candidate = artifacts[0]
                if candidate.status not in {"available", "selected"}:
                    continue
                _candidate(candidate, stage=stage, context=context, current=True)
                artifact = candidate
                break
            if artifact is None:
                return None
        decision = _record_artifact_selection(artifact, selected_by=actor if manual else None,
            selection_mode="manual" if manual else "automatic")
        if dependency_change is not None and affected:
            from studio.services.production_recipe_material_registration import register_recipe_material_selection
            from studio.services.production_recipe_material_repreparation import reprepare_variants
            register_recipe_material_selection(stage_run_id=stage.pk, unit_key=unit_key, decision_id=decision.pk,
                actor=actor, asset_guard=dependency_change["asset_guard"])
            reprepare_variants(stage=stage, preparations=replacements, actor=actor)
        return decision
