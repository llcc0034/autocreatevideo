"""Advance Recipe media through the existing Preparation and original queue."""
from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import ExecutionPreparation, GeneratedArtifact
from studio.services.execution_production_attempts import current_primary_attempts
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.generated_artifacts import _artifact_limit
from studio.services.json_contracts import json_contract
from studio.services.production_execution_targets import production_target_queryset
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.bible_material_requirements import read_run_bible_material_requirements
from studio.services.production_recipe_material_units import project_recipe_material_units
from studio.services.production_recipe_material_sources import recipe_material_source
from studio.services.production_recipe_media_preparations import (prepare_recipe_media_unit, recipe_media_stage_context,
    read_recipe_media_candidate, require_current_recipe_material_requirement)
from studio.services.production_recipe_routing import recipe_routing, read_recipe_candidate_route, require_recipe_stage_route


def is_recipe_media_stage(stage):
    return (stage.production_run.plan_source == "recipe" and stage.mode == "existing_pipeline"
        and stage.stage_key in {"media_generation", "bible_image_generation"})


def recipe_media_stage_contract(stage):
    # Validate the effective declaration (including any immutable amendment),
    # but freeze the identity of the ORIGINAL RunPlan declaration. A bounded
    # input amendment is a separate audited fact, not a replacement RunPlan.
    # Preparations bind its ID/SHA and effective mapping; stage snapshots keep
    # validating the same plan and target set before and after that amendment.
    plan, _ = recipe_media_stage_context(stage, None)
    declaration = next(row for row in plan.value["recipe"]["definition"]["stages"] if row["key"] == stage.stage_key)
    return json_contract({"schema_version": 1, "stage_key": stage.stage_key, "run_plan_sha256": plan.sha256,
        "stage_declaration_sha256": json_contract(declaration, max_bytes=execution_configuration_max_bytes()).sha256},
        max_bytes=execution_configuration_max_bytes())


def recipe_media_input_contract(stage):
    from studio.services.production_runs import _effective_generation_task_limit
    value = recipe_media_stage_contract(stage).value
    _, declaration = recipe_media_stage_context(stage, None)
    limit = _effective_generation_task_limit(stage.production_run)
    units = []
    if stage.stage_key == "bible_image_generation":
        requirements = read_run_bible_material_requirements(run=stage.production_run,
            eligible_stage_ids=execution_source_stage_ids(stage))
        targets = requirements.value["targets"]
        counts = {role: (recipe_routing(declaration, appearance_role=role)["variant_count"]
            if recipe_material_source(declaration, role) == "generate" else 0)
            for role in {target["appearance_role"] for target in targets}}
        if sum(counts[target["appearance_role"]] for target in targets) > limit:
            raise ValidationError("素材需求乘以候选数超过本轮任务数量边界，请调整范围或配置")
        value["material_requirements"] = requirements.value
        units = project_recipe_material_units(requirements.value)
        from studio.services.production_recipe_material_sources import recipe_material_source_snapshot
        sources = recipe_material_source_snapshot(stage, declaration, targets)
        if sources:
            value["material_source_versions"] = sources
    else:
        variants = declaration["routing"]["variant_count"]
        _, rows = production_target_queryset(stage.production_run, "shot")
        # The catalog queryset is already scoped and ordered. This boundary is
        # a finite stage snapshot, not a new list endpoint or a media scanner.
        for row in rows[:limit + 1]:
            units.append({"kind": "shot", "id": str(row.pk)})
            if len(units) * variants > limit:
                raise ValidationError("制作单元乘以候选数超过本轮任务数量边界，请调整范围或配置")
    if not units:
        raise ValidationError("本轮没有明确的制作对象，不能把空生成阶段标为成功")
    value["units"] = units
    return json_contract(value, max_bytes=execution_configuration_max_bytes())


def advance_recipe_media_stage(run, stage):
    from studio.services.production_runs import StageBlocked, _verify_stage_snapshot
    from studio.services.execution_production_candidates import read_production_media_candidate
    if stage.production_run_id != run.pk or not is_recipe_media_stage(stage):
        raise ValidationError("媒体阶段与原制作运行不一致")
    if not run.owner.is_active:
        raise StageBlocked("execution_owner_unavailable", "运行所有者已停用，不能开始模型调用。")
    plan, declaration = recipe_media_stage_context(stage, None)
    current = recipe_media_input_contract(stage)
    if _verify_stage_snapshot(stage, "input_snapshot") != current.value:
        raise ValidationError("制作对象范围已改变，请明确复核，不能悄悄新增或跳过镜头")
    targets = current.value.get("material_requirements", {}).get("targets", [])
    units = current.value["units"]
    fulfilled = set()
    source_failures = {}
    if targets:
        from studio.services.production_recipe_material_fulfillment import (
            read_recipe_material_fulfillments, fulfill_frozen_recipe_material_sources,
        )
        source_failures = fulfill_frozen_recipe_material_sources(stage=stage)
        # This indexed identity query is not another source selector. Verify
        # every explicit choice using its original reader before preparing ANY
        # unfulfilled unit; a corrupt reused image must never fall into generation.
        fulfilled = {row["unit_key"] for row in read_recipe_material_fulfillments(stage=stage)}
        if fulfilled - {unit["unit_key"] for unit in units}:
            raise ValidationError("素材选择含有本轮冻结需求以外的单元，不能跳过或自动替换")
        units = [unit for unit in units if unit["unit_key"] not in fulfilled]
    waiting_sources = [unit for unit in units if targets
        and recipe_material_source(declaration, unit["appearance_role"]) != "generate"]
    if waiting_sources and ExecutionPreparation.objects.filter(stage_run=stage,
            unit_key__in=[unit["unit_key"] for unit in waiting_sources]).exists():
        raise ValidationError("已有/上传成图单元存在生成准备，不能忽略或重发原调用")
    units = [unit for unit in units if unit not in waiting_sources]
    if not targets or any(unit["appearance_role"] == "primary" for unit in units):
        require_recipe_stage_route(stage, declaration)
    waiting_dependencies = 0
    if targets and any(unit["appearance_role"] == "variant" for unit in units):
        from studio.services.production_recipe_contracts import recipe_execution_contract
        from studio.services.execution_definitions import verify_stored_execution_definition
        from studio.services.production_recipe_material_inputs import recipe_material_binding
        from studio.services.production_recipe_material_registration import read_registered_recipe_primary_dependency
        require_recipe_stage_route(stage, declaration, appearance_role="variant")
        # Validate every possible variant route before dispatching any primary.
        for route in recipe_routing(declaration, appearance_role="variant")["routes"]:
            if recipe_material_binding(verify_stored_execution_definition(recipe_execution_contract(route)), route["input_sources"]) is None:
                raise ValidationError("变体路线尚未配置本轮主图及当前设定差异来源")
        ready = []
        for unit in units:
            try:
                dependency = (read_registered_recipe_primary_dependency(stage=stage, unit_key=unit["unit_key"])
                    if unit["appearance_role"] == "variant" else True)
            except APIValidationError as exc:
                raise ValidationError(str(exc.detail)) from exc
            if dependency is None:
                waiting_dependencies += 1
            else:
                ready.append(unit)
        units = ready
    # Resolve every candidate first. A missing mapping must not dispatch an
    # earlier unit before the stage has discovered its incomplete input set.
    preparations = [prepare_recipe_media_unit(stage_run_id=stage.pk, expected_run_plan_sha256=plan.sha256,
        scope=unit["scope"] if targets else unit, candidate_number=candidate, actor=run.owner,
        **({"material_unit_key": unit["unit_key"]} if targets and unit["appearance_role"] == "variant" else {}))
        for unit in units for candidate in range(1, recipe_routing(declaration,
            appearance_role=unit["appearance_role"] if targets else "primary")["variant_count"] + 1)]
    unit_keys = {row.unit_key for row in preparations}
    roles = {unit["unit_key"]: unit["appearance_role"] for unit in units} if targets else {}
    extra = list(ExecutionPreparation.objects.filter(stage_run=stage, unit_key__in=unit_keys)
        .exclude(pk__in=[row.pk for row in preparations]).order_by("unit_key", "request_key"))
    added = {}
    for preparation in extra:
        scope, number = read_recipe_media_candidate(preparation, stage=stage, plan=plan, declaration=declaration)
        choice = read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
            unit_key=preparation.unit_key, candidate_number=number)
        if choice.preparation is None:
            raise ValidationError("候选缺少明确选路准备")
        if number > recipe_routing(declaration, appearance_role=roles.get(preparation.unit_key, "primary"))["variant_count"]:
            added[(preparation.unit_key, number)] = (scope, choice.preparation)
        # Earlier failed routes are verified by the contiguous event chain;
        # they are evidence, not additional candidates to dispatch or adopt.
    for (_, number), (scope, preparation) in added.items():
        from studio.services.execution_preparations import read_execution_preparation_detail
        intent = read_execution_preparation_detail(preparation).intent["request"]["requested"]
        preparations.append(prepare_recipe_media_unit(stage_run_id=stage.pk, expected_run_plan_sha256=plan.sha256,
            scope=scope, candidate_number=number, generation_request=intent["generation_request"], actor=run.owner))
    if targets:
        for preparation in preparations:
            require_current_recipe_material_requirement(preparation, stage=stage,
                requirements=current.value["material_requirements"])
    stopped = sum(row.status == "cancelled" for row in preparations)
    if stopped:
        raise StageBlocked("execution_preparations_cancelled",
            f"{stopped} 个候选的执行准备已停止；本阶段不能继续等待或重新提交这些准备。请查看原任务记录。")
    preparations = [advance_execution_preparation(preparation_id=preparation.pk,
        expected_preparation_revision=preparation.revision, actor=run.owner) for preparation in preparations]
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
        .values("id", "preparation_id", "status", "ignore_late_result").order_by("preparation_id", "id"))
    if len({row["preparation_id"] for row in attempts}) != len(attempts):
        raise ValidationError("同一执行准备出现多个主调用，不能自动选最新结果")
    failures = [row for row in attempts if row["status"] in {"failed", "cancelled"} or row["ignore_late_result"]]
    if failures and targets:
        from studio.services.execution_production_attempts import attempt_has_artifacts, retry_check_queryset
        from studio.services.production_runs import _definitive_media_failure
        # A confirmed failed peer is not a failed material target when another
        # candidate can fulfill it. Unknown/cancelled/partially archived results
        # remain unresolved; a successful peer is not permission to discard them.
        failed_rows = retry_check_queryset(current_primary_attempts(stage=stage, preparation_ids=ids)
            .filter(pk__in=[row["id"] for row in failures]))
        if any(row.ignore_late_result or attempt_has_artifacts(row)
                or not _definitive_media_failure(row)[0] for row in failed_rows):
            raise StageBlocked("execution_candidates_incomplete", "素材候选包含取消、结果未知或未完成归档的调用；请核对原任务，不自动重发或忽略。")
    elif failures:
        raise StageBlocked("execution_candidates_incomplete", f"{len(failures)} 个候选未成功；请核对原任务后明确重试，不自动换实例或采用其他结果。")
    # Primary units may finish and register while dependent variants still wait.
    # Account for every configured call, but only successful calls own artifacts.
    pending, waiting_selection, completed, artifact_count, failed_units = [], 0, set(), 0, 0
    for unit_key in sorted(unit_keys):
        unit_ids = {row.pk for row in preparations if row.unit_key == unit_key}
        unit_attempts = [row for row in attempts if row["preparation_id"] in unit_ids]
        if len(unit_attempts) != len(unit_ids) or any(row["status"] not in {"succeeded", "failed"} for row in unit_attempts):
            continue
        successful = [row["id"] for row in unit_attempts if row["status"] == "succeeded"]
        if not successful:
            failed_units += 1
            continue
        artifacts = list(GeneratedArtifact.objects.filter(production_run=run, stage_run=stage,
            generation_attempt_id__in=successful).select_related("production_run__project", "stage_run"))
        if len(artifacts) != len(successful) or len({row.generation_attempt_id for row in artifacts}) != len(successful):
            raise ValidationError("成功调用未形成完整且唯一的候选产物，阶段不能完成")
        for artifact in artifacts:
            read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(run))
        artifact_count += len(artifacts)
        completed.add(unit_key)
        if targets:
            from studio.services.production_recipe_material_selection import choose_recipe_material_candidate
            from studio.services.asset_references import read_generated_material
            decision = choose_recipe_material_candidate(stage_run_id=stage.pk, unit_key=unit_key)
            if decision is None:
                waiting_selection += 1
                continue
            try:
                version = read_generated_material(artifact=decision.artifact)
            except APIValidationError as exc:
                raise ValidationError(str(exc.detail)) from exc
            if version is None:
                pending.append((unit_key, decision.pk))
    if pending:
        from studio.services.production_recipe_material_registration import schedule_recipe_material_registrations
        schedule_recipe_material_registrations(stage_run_id=stage.pk, selections=pending)
        stage.reason_code = "waiting_material_registration"
        stage.reason = f"{len(pending)} 个已选素材等待登记为确切版本；不等于Bible已审核采用。"
        return None
    if failed_units:
        stage.reason_code = "waiting_material_recovery"
        stage.reason = (f"{failed_units} 个素材单元的候选已明确失败，等待人工恢复；"
            "可在本轮需求中选择已有图或上传成图，也可查看原任务及重试影响后明确重试；不自动重发或把空结果标为完成。")
        if waiting_selection:
            stage.reason += f"另有 {waiting_selection} 个单元有成功候选待选择，不在此次失败重试范围。"
        if waiting_dependencies:
            stage.reason += f"另有 {waiting_dependencies} 个变体等待本轮确切主图，尚未派发。"
        return None
    if waiting_selection:
        stage.reason_code = "waiting_material_selection"
        stage.reason = f"{waiting_selection} 个素材单元有成功候选待人工选择；失败调用保留原记录，不自动重发或伪造人审。"
        return None
    if waiting_sources:
        stage.reason_code = "waiting_material_source"
        stage.reason = f"{len(waiting_sources)} 个素材需求等待按配方选择已有成图或上传；不会自动改走生成。"
        if source_failures:
            labels = {"missing": "无确切版本", "ambiguous": "存在多个确切版本", "unavailable": "冻结版本或文件不可用"}
            stage.reason += "自动选材待人工接管：" + "、".join(
                f"{sum(value == status for value in source_failures.values())} 项{label}"
                for status, label in labels.items() if status in source_failures.values()) + "；请在本轮素材需求中明确选材。"
        if waiting_dependencies:
            stage.reason += f"另有 {waiting_dependencies} 个变体等待本轮确切主图。"
        return None
    if completed == unit_keys and not waiting_dependencies:
        # Reuse is a real fulfillment, not a fabricated generation artifact.
        if fulfilled:
            generated = f"generated-artifacts:{artifact_count};" if artifact_count else ""
            return f"{generated}material-fulfillments:{len(fulfilled)}"
        return f"generated-artifacts:{artifact_count}"
    if waiting_dependencies and completed == unit_keys:
        stage.reason_code = "waiting_material_dependency"
        stage.reason = f"{waiting_dependencies} 个变体等待本轮确切主图版本，登记后由原执行器继续。"
        return None
    waiting = sum(row.status in {"awaiting_input", "awaiting_review", "blocked"} for row in preparations)
    stage.reason_code = ("execution_preparation_review_required" if any(row.status == "awaiting_review" for row in preparations)
        else "waiting_execution_preparation" if waiting else "waiting_external_task")
    stage.reason = (f"{waiting} 个候选等待输入、字段审核或准入；请查看原执行准备。" if waiting else
        f"正在等待 {len(ids)} 个候选的原任务；完成后仍需明确审核采用。")
    if waiting_dependencies:
        stage.reason += f"另有 {waiting_dependencies} 个变体等待本轮确切主图登记，尚未派发。"
    recovery = active_primary_retry_feedback(stage, ids)
    if recovery and not waiting:
        stage.reason_code, stage.reason = recovery
    return None
