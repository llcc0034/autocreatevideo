"""Associate an explicit Asset with one exact frozen recipe requirement.

Association and explicit run selection are separate operations. Selection only
references the original version; it creates no task, candidate or approval.
The formal HTTP gateway reuses these operations; source authoring and its
waiting/resume UI are separate from recording an explicit result choice.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import AssetReferenceSeries, AssetReferenceVersion, ExecutionPreparation, ProductionMaterialFulfillment
from studio.services.asset_references import attach_asset, _series_for_write
from studio.services.execution_preparation_owners import locked_preparation_owners
from studio.services.production_recipe_material_units import frozen_recipe_material_units
from studio.services.production_recipe_media_preparations import _material_requirement, recipe_media_stage_context
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.json_contracts import json_contract
from studio.services.media_generation_targets import read_media_fulfillment_version
from studio.services.production_recipe_material_sources import recipe_material_source, material_source_version_origins


def _requirement_context(stage, plan, unit_key, requirement_key=None, *, require_current=True):
    units, requirements = frozen_recipe_material_units(stage, plan)
    matches = [unit for unit in units if unit["unit_key"] == unit_key
        and (requirement_key is None or unit["requirement_key"] == requirement_key)]
    if len(matches) != 1:
        raise ValidationError("素材关联必须指向本轮冻结的确切外观和设定需求")
    unit = matches[0]
    target = next(row for row in requirements["targets"] if row["requirement_key"] == unit["requirement_key"])
    if require_current:
        expected = {"schema_version": 1, "run_plan_sha256": requirements["run_plan_sha256"],
            "bible_review": requirements["bible_review"], "target": target}
        current = _material_requirement(stage, unit["scope"], appearance_target={
            "role": unit["appearance_role"], "setting_revision_id": target["setting"]["setting_revision_id"]})
        if current != expected:
            raise ValidationError("本轮素材需求审核已改变，不能把已有图关联为旧需求的结果")
    return unit, target


def _validate_series(stage, unit, target, series):
    if (series.owner_id != stage.production_run.owner_id or series.role != unit["appearance_role"]
            or str(series.bible_variant_id) != target["setting"]["appearance_id"]):
        raise ValidationError("所选素材系列不属于本轮所有者及确切外观设定")
    if unit["appearance_role"] == "variant" and not AssetReferenceSeries.objects.filter(
            pk=series.primary_series_id, owner_id=stage.production_run.owner_id, role="primary",
            bible_variant_id=target["primary_dependency"]["appearance_id"]).exists():
        raise ValidationError("变体系列未关联本需求指定的主图外观系列")


def _version_target(version):
    return read_media_fulfillment_version(version, expected_media_type="image")


def _target_digest(target):
    # Digest only: never copy the Asset, file, setting body or a generation input.
    return json_contract(target.value, max_bytes=4096).sha256


def _has_prepared_dependents(stage, plan, unit_key):
    units, _ = frozen_recipe_material_units(stage, plan)
    dependents = [unit["unit_key"] for unit in units if unit["primary_dependency"] is not None
        and unit["primary_dependency"]["unit_key"] == unit_key]
    return ExecutionPreparation.objects.filter(stage_run=stage, unit_key__in=dependents).exists()


def _unsubmitted_blocked_preparation(preparation, fulfillment):
    """Prove a blocked candidate and its auxiliaries have never made a call.

    A committed fulfillment permits reading the preparations it atomically
    stopped; an unrelated cancellation is never authority for a new choice.
    """
    from django.db.models import Q
    from studio.models import GenerationAttempt
    from studio.services.execution_auxiliary_requests import production_field_unit, read_production_field_context
    from studio.services.execution_preparations import read_execution_preparation_detail

    detail = read_execution_preparation_detail(preparation)
    units = [production_field_unit(preparation.pk, row["sha256"])
        for row in detail.resolved_inputs["field_requests"]]
    children = list(ExecutionPreparation.objects.filter(stage_run_id=preparation.stage_run_id,
        unit_key__in=units).order_by("id"))
    for child in children:
        context = read_production_field_context(child)
        if context is None or context[0].pk != preparation.pk:
            raise ValidationError("辅助准备归属不一致，不能接管本轮素材需求")
    rows = [preparation, *children]
    if any(row.claim_token is not None or row.claim_until is not None
            or row.request_frozen_at is not None or row.request_sha256 for row in rows):
        return False
    if GenerationAttempt.objects.filter(Q(preparation_id__in=[row.pk for row in rows])
            | Q(parent_preparation=preparation)).exists():
        return False
    if fulfillment is not None:
        return (fulfillment.selection_mode == "manual" and all(row.status == "cancelled"
            and row.cancelled_by_id == fulfillment.selected_by_id and row.cancelled_at is not None
            and row.cancelled_at <= fulfillment.created_at for row in rows))
    return preparation.status == "blocked" and all(row.status in {
        "preparing", "awaiting_input", "awaiting_review", "blocked"} for row in children)


def _require_replaceable_preparations(stage, plan, declaration, unit):
    """No preparation, proven zero-call blocker, or all calls explicitly failed.

    Read the original immutable requests and current retry leaves. A missing
    call without a proven blocker, success or unknown result is not an empty result.
    Fulfillment and dispatch hold the same Run lock; no second recovery fact is
    stored, and historical failed requests remain unchanged and inspectable.
    """
    from studio.services.execution_production_attempts import current_primary_attempts, retry_check_queryset
    from studio.services.production_execution_retry import material_failure_can_be_skipped
    from studio.services.production_recipe_media_preparations import read_recipe_media_candidate
    from studio.services.production_recipe_routing import read_recipe_candidate_route, recipe_routing

    preparations = list(ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit["unit_key"]))
    if not preparations:
        return []
    if recipe_material_source(declaration, unit["appearance_role"]) != "generate":
        raise ValidationError("已有/上传素材单元不能同时存在生成准备")
    replaced = {row.supersedes_id for row in preparations if row.supersedes_id is not None}
    if replaced:
        # Only the explicit, verified no-main-call replacement chain may be omitted.
        # Other cancelled/missing calls and prior fallback failures still count.
        candidates = {read_recipe_media_candidate(row, stage=stage, plan=plan, declaration=declaration)[1]
            for row in preparations}
        for number in candidates:
            read_recipe_candidate_route(stage=stage, plan=plan, declaration=declaration,
                unit_key=unit["unit_key"], candidate_number=number)
        preparations = [row for row in preparations if row.pk not in replaced]
    calls = list(retry_check_queryset(current_primary_attempts(stage=stage,
        preparation_ids=[row.pk for row in preparations])))
    called = {row.preparation_id for row in calls}
    fulfillment = ProductionMaterialFulfillment.objects.filter(stage_run=stage, unit_key=unit["unit_key"]).first()
    unsubmitted = [row for row in preparations if row.pk not in called]
    if (len(calls) != len(called) or any(not material_failure_can_be_skipped(row) for row in calls)
            or any(not _unsubmitted_blocked_preparation(row, fulfillment) for row in unsubmitted)):
        raise ValidationError("已有生成准备；仅未冻结且主/辅助均无调用的受阻准备，或全部调用明确失败且无产物，才可改用成图；不能覆盖在途、结果未知、成功或独立取消的调用")
    candidates = {read_recipe_media_candidate(row, stage=stage, plan=plan, declaration=declaration)[1]
        for row in preparations}
    count = recipe_routing(declaration, appearance_role=unit["appearance_role"])["variant_count"]
    if not set(range(1, count + 1)).issubset(candidates):
        raise ValidationError("本单元仍有未创建的配置候选，不能视为全部失败")
    return unsubmitted if fulfillment is None else []


def _manual_stage_available(stage):
    run = stage.production_run
    from studio.services.production_execution_recovery import MANUAL_RECOVERY
    if (stage.status == "blocked" and run.status == "awaiting_input" and run.current_stage_key == stage.stage_key
            and run.blocked_code == stage.reason_code and stage.reason_code in MANUAL_RECOVERY):
        return True
    return stage.status == "running" and (run.status == "running" or (
        run.status == "awaiting_input" and run.current_stage_key == stage.stage_key
        and run.blocked_code == stage.reason_code == "waiting_material_recovery"))


def material_fulfillment_receipt(row):
    """Return the committed identity only, without advancing or rereading media."""
    version = row.reference_version
    return {"id": str(row.pk), "reference_version_id": str(version.pk),
        "series_id": str(version.series_id), "asset_id": str(version.asset_id), "source": version.source,
        "target_sha256": row.target_sha256, "selection_mode": row.selection_mode, "selected_by_id": row.selected_by_id,
        "created_at": row.created_at}


def recipe_material_fulfillment_context(*, stage_run_id, unit_key, actor):
    """Inspect one frozen requirement on demand; never search or choose an Asset.

    This is a point-in-time check, not a reservation against the scheduler. The
    existing writer rechecks the same Run/unit under lock on every submission.
    """
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        run = stage.production_run
        if not run.owner.is_active:
            raise PermissionDenied("运行所有者已停用，不能选择素材")
        plan, declaration = recipe_media_stage_context(stage, None)
        unit, target = _requirement_context(stage, plan, unit_key)
        choice = _read_material_fulfillment(stage, plan, unit, target)
        reason = ""
        if choice is not None:
            reason = "此需求已有明确素材选择；本入口不覆盖选择或改写历史。"
        elif not _manual_stage_available(stage):
            reason = "只有运行中的配方素材阶段可以选择本轮需求素材。"
        elif _has_prepared_dependents(stage, plan, unit_key):
            reason = "已有变体准备冻结了主图依据；不能补选另一条主图来源。"
        else:
            try:
                _require_replaceable_preparations(stage, plan, declaration, unit)
            except ValidationError as exc:
                reason = "；".join(exc.messages)
        return {"stage_run_id": str(stage.pk), "unit_key": unit_key,
            "source_mode": recipe_material_source(declaration, unit["appearance_role"]),
            "run_plan_sha256": plan.sha256, "requirement_key": unit["requirement_key"],
            "appearance_id": target["setting"]["appearance_id"],
            "setting_revision_id": target["setting"]["setting_revision_id"],
            "appearance_role": unit["appearance_role"], "reference_name": target["reference_name"],
            "primary_appearance_id": (target["primary_dependency"]["appearance_id"]
                if target["primary_dependency"] is not None else None),
            "can_select": not reason, "reason": reason,
            "selection": material_fulfillment_receipt(choice["fulfillment"]) if choice else None}


def read_recipe_material_fulfillment(*, stage, unit_key, require_current=True):
    """Execution-time read of an exact choice; absence never searches the library.

    Internal callers own access and the Run lock. Historical reads can bypass
    today's M3 approval, but still verify the chosen version and actual file.
    No read backfills a selection or changes a stage's status.
    """
    plan, _ = recipe_media_stage_context(stage, None)
    unit, target = _requirement_context(stage, plan, unit_key, require_current=require_current)
    return _read_material_fulfillment(stage, plan, unit, target)


def _read_material_fulfillment(stage, plan, unit, target):
    row = ProductionMaterialFulfillment.objects.filter(stage_run=stage, unit_key=unit["unit_key"]).select_related(
        "reference_version__series", "reference_version__setting_revision").first()
    if row is None:
        return None
    if row.run_plan_sha256 != plan.sha256 or row.requirement_key != unit["requirement_key"]:
        raise ValidationError("素材选择不匹配本轮冻结计划或确切需求")
    row.stage_run = stage
    row.clean()
    version = row.reference_version
    _validate_series(stage, unit, target, version.series)
    _, declaration = recipe_media_stage_context(stage, plan.sha256)
    _require_replaceable_preparations(stage, plan, declaration, unit)
    resolved = _version_target(version)
    if resolved.value["setting"] != target["setting"] or _target_digest(resolved) != row.target_sha256:
        raise ValidationError("已选素材版本、来源或文件与选择时不一致，不能静默替换")
    return {"fulfillment": row, "version": version, "asset": resolved.asset, "unit": unit, "target": target}


def read_recipe_material_fulfillment_choices(*, stage):
    """Validate all explicit stage choices, never select.

    The dispatcher, review and formal result manifest share this indexed reader.
    A missing/corrupt/out-of-scope choice is an error, not a generation fallback.
    """
    result = []
    unit_keys = (ProductionMaterialFulfillment.objects.filter(stage_run=stage).order_by("unit_key")
        .values_list("unit_key", flat=True))
    for unit_key in unit_keys:
        try:
            choice = read_recipe_material_fulfillment(stage=stage, unit_key=unit_key)
        except APIValidationError as exc:
            raise ValidationError(str(exc.detail)) from exc
        if choice is None:
            raise ValidationError("本轮素材选择已缺失，不能静默转为生成")
        result.append(choice)
    return result


def material_fulfillment_reference(choice):
    row, version = choice["fulfillment"], choice["version"]
    return {"fulfillment_id": str(row.pk), "unit_key": row.unit_key,
        "requirement_key": row.requirement_key, "reference_version_id": str(version.pk),
        "target_sha256": row.target_sha256, "source": version.source}


def read_recipe_material_fulfillments(*, stage):
    return [material_fulfillment_reference(choice)
        for choice in read_recipe_material_fulfillment_choices(stage=stage)]


def _record_fulfillment(*, stage, plan, unit, version, resolved, selection_mode, actor):
    from studio.services.execution_production_cancellation import cancel_production_preparation
    _, declaration = recipe_media_stage_context(stage, plan.sha256)
    unsubmitted = _require_replaceable_preparations(stage, plan, declaration, unit)
    if unsubmitted and (selection_mode != "manual" or actor is None):
        raise ValidationError("未提交受阻准备须由本次明确人工选材接管，不能自动替换")
    for preparation in unsubmitted:
        cancel_production_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=actor)
    row = ProductionMaterialFulfillment(stage_run=stage, unit_key=unit["unit_key"],
        requirement_key=unit["requirement_key"], run_plan_sha256=plan.sha256, reference_version=version,
        target_sha256=_target_digest(resolved), selection_mode=selection_mode, selected_by=actor)
    row.full_clean()
    row.save()
    run = stage.production_run
    if run.status == "awaiting_input" and _manual_stage_available(stage):
        # Release only this recovery pause. The original durable running-Run
        # sweep resumes after commit; this POST never calls a model or advances
        # stages. Other unresolved units retain their own original blockers.
        run.status, run.blocked_code, run.blocked_reason = "running", "", ""
        stage.status, stage.reason_code, stage.reason, stage.finished_at = "running", "", "", None
        stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "updated_at"))
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
    return row


@registry_reference_writer
def fulfill_frozen_recipe_material_sources(*, stage):
    """Original dispatcher only: consume frozen identities, never search again.

    Invalid/unavailable automatic sources leave the unit open for the existing
    manual action. They do not dispatch a model, invent an actor or new version.
    This operation is not an approval or a public automatic-selection endpoint.
    """
    from studio.models import AssetReferenceVersion
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.production_recipe_material_sources import recipe_material_source_snapshot
    plan, declaration = recipe_media_stage_context(stage, None)
    if "unique_version" not in declaration.get("material_selection", {}).values():
        return {}
    _, requirements = frozen_recipe_material_units(stage, plan)
    sources = recipe_material_source_snapshot(stage, declaration, requirements["targets"])
    if not sources:
        return {}
    failures = {}
    with locked_preparation_owner_rows([("stage_run_id", stage.pk)]) as owners:
        stage = owners[("stage_run_id", str(stage.pk))]
        if not stage.production_run.owner.is_active:
            raise PermissionDenied("运行所有者已停用，不能自动选择素材")
        if stage.status != "running" or stage.production_run.status != "running":
            raise ValidationError("仅运行中的素材阶段可自动履约")
        plan, declaration = recipe_media_stage_context(stage, plan.sha256)
        _, requirements = frozen_recipe_material_units(stage, plan)
        sources = recipe_material_source_snapshot(stage, declaration, requirements["targets"])
        for unit_key, source in sources.items():
            unit, target = _requirement_context(stage, plan, unit_key)
            if _read_material_fulfillment(stage, plan, unit, target) is not None:
                continue  # Original manual/automatic choice always wins; never relabel it.
            if (ExecutionPreparation.objects.filter(stage_run=stage, unit_key=unit_key).exists()
                    or _has_prepared_dependents(stage, plan, unit_key)):
                raise ValidationError("自动履约不能覆盖生成准备或已冻结的主图依赖")
            if source["status"] != "unique":
                failures[unit_key] = source["status"]
                continue
            version = AssetReferenceVersion.objects.filter(pk=source["version_id"]).select_related(
                "series", "setting_revision").first()
            try:
                if version is None or version.source not in material_source_version_origins(
                        recipe_material_source(declaration, unit["appearance_role"])):
                    raise ValidationError("冻结的素材版本缺失或来源不符")
                _validate_series(stage, unit, target, version.series)
                resolved = _version_target(version)
                if resolved.value["setting"] != target["setting"]:
                    raise ValidationError("冻结素材不属于本需求的确切设定")
            except (ValidationError, APIValidationError):
                failures[unit_key] = "unavailable"
                continue
            _record_fulfillment(stage=stage, plan=plan, unit=unit, version=version,
                resolved=resolved, selection_mode="automatic", actor=None)
    return failures


@registry_reference_writer
def attach_recipe_material_asset(*, stage_run_id, expected_run_plan_sha256, unit_key,
        requirement_key, series_id, asset_id, source, actor):
    """Validate original M3 authority, then reuse the single Asset/version writer.

    A library association does not imply that a Run selected it. No job,
    preparation, candidate, approval or generated-from evidence is created.
    The whole operation uses the existing registry -> Run -> series lock order.
    """
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        _, declaration, unit, target = _manual_material_context(
            stage, expected_run_plan_sha256, unit_key, requirement_key)
        mode = recipe_material_source(declaration, unit["appearance_role"])
        if mode != "generate" and source != mode:
            raise ValidationError({"source": "所选成图来源与本轮冻结配方不一致"})
        # Staff access to both roots is not permission to cross their owners.
        # The caller chooses the series explicitly; do not infer latest/first.
        series = _series_for_write(actor, series_id)
        _validate_series(stage, unit, target, series)
        return attach_asset(actor=actor, series_id=series.pk, asset_id=asset_id, source=source,
            setting_revision_id=target["setting"]["setting_revision_id"], expected_media_type="image")


def _manual_material_context(stage, expected_run_plan_sha256, unit_key, requirement_key):
    if (not isinstance(expected_run_plan_sha256, str) or len(expected_run_plan_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_run_plan_sha256)):
        raise ValidationError("选材需要本次确认的执行计划摘要")
    run = stage.production_run
    if not run.owner.is_active:
        raise PermissionDenied("运行所有者已停用，不能选择素材")
    if not _manual_stage_available(stage) or stage.stage_key != "bible_image_generation":
        raise ValidationError("只有运行中的配方素材阶段可以选择本轮需求素材")
    plan, declaration = recipe_media_stage_context(stage, expected_run_plan_sha256)
    unit, target = _requirement_context(stage, plan, unit_key, requirement_key)
    _require_replaceable_preparations(stage, plan, declaration, unit)
    return plan, declaration, unit, target


@registry_reference_writer
def select_recipe_material_asset(*, stage_run_id, expected_run_plan_sha256, unit_key,
        requirement_key, actor, series_id=None, asset_id=None, source=None, reference_version_id=None):
    """Choose an original version, or atomically associate an explicit Asset.

    Run + unit is the natural idempotency key. An exact replay verifies the
    original choice; a changed request conflicts, never overwrites it. Reselect
    and dependent reprepare remain the separately scoped M4-06 operation.
    """
    association = (series_id, asset_id, source)
    if ((reference_version_id is not None and any(value is not None for value in association))
            or (reference_version_id is None and any(value is None for value in association))):
        raise ValidationError("确切版本与Asset关联是互斥的选择方式")
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        existing = ProductionMaterialFulfillment.objects.filter(stage_run=stage, unit_key=unit_key).select_related(
            "reference_version").first()
        if reference_version_id is not None:
            plan, declaration, unit, target = _manual_material_context(
                stage, expected_run_plan_sha256, unit_key, requirement_key)
            if existing is not None:
                if str(existing.reference_version_id) != str(reference_version_id):
                    raise ValidationError("此单元已有不同的素材选择；不能覆盖或另建履约事实")
                read_recipe_material_fulfillment(stage=stage, unit_key=unit_key)
                return existing, False  # Preserve original manual/automatic actor.
            if _has_prepared_dependents(stage, plan, unit_key):
                raise ValidationError("已有变体准备冻结了主图依据；不能补选另一条主图来源")
            version = AssetReferenceVersion.objects.filter(pk=reference_version_id).select_related(
                "series", "setting_revision").first()
            if version is None:
                raise ValidationError("所选确切素材版本不存在")
            _validate_series(stage, unit, target, version.series)
            mode = recipe_material_source(declaration, unit["appearance_role"])
            if version.source not in material_source_version_origins("existing" if mode == "generate" else mode):
                raise ValidationError("所选原版本来源不符合本轮配方策略")
            resolved = _version_target(version)
            if resolved.value["setting"] != target["setting"]:
                raise ValidationError("素材版本不属于本需求的确切设定修订")
            return _record_fulfillment(stage=stage, plan=plan, unit=unit, version=version,
                resolved=resolved, selection_mode="manual", actor=actor), True
        if existing is not None:
            version = existing.reference_version
            if (existing.run_plan_sha256 != expected_run_plan_sha256 or existing.requirement_key != requirement_key
                    or str(version.series_id) != str(series_id) or str(version.asset_id) != str(asset_id)
                    or version.source != source):
                raise ValidationError("此单元已有不同的素材选择；不能覆盖或另建履约事实")
        else:
            plan, _ = recipe_media_stage_context(stage, expected_run_plan_sha256)
            if _has_prepared_dependents(stage, plan, unit_key):
                raise ValidationError("已有变体准备冻结了主图依据；不能补选另一条主图来源")
        # Also on replay: original owner/status/M3 and actual media validation.
        version, _ = attach_recipe_material_asset(stage_run_id=stage.pk,
            expected_run_plan_sha256=expected_run_plan_sha256, unit_key=unit_key, requirement_key=requirement_key,
            series_id=series_id, asset_id=asset_id, source=source, actor=actor)
        if existing is not None:
            result = read_recipe_material_fulfillment(stage=stage, unit_key=unit_key)
            if result["version"].pk != version.pk:
                raise ValidationError("原素材关联不再唯一匹配选择版本")
            return existing, False
        plan, _ = recipe_media_stage_context(stage, expected_run_plan_sha256)
        unit, _ = _requirement_context(stage, plan, unit_key, requirement_key)
        row = _record_fulfillment(stage=stage, plan=plan, unit=unit, version=version,
            resolved=_version_target(version), selection_mode="manual", actor=actor)
        return row, True
