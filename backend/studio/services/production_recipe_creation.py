"""Atomic Recipe root initialization for the original production creator.

The public creator validates new requests' complete executability. This initializer
owns one transaction for the actual Run, Plan, configuration and declared stages;
after commit it wakes the existing stage executor, without certifying acceptance
or introducing another provider dispatch path.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, json_contract
from studio.services.production_creation_inputs import validate_creation_inputs, validate_recipe_creation_mappings, verified_creation_inputs, verify_recipe_stage_rows
from studio.services.production_run_configuration import prepare_run_configuration, freeze_run_configuration
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.registry_reference_lock import registry_reference_writer


def _creation_source(stages, *, script_revision, values, episode_ids):
    inputs = [(row["key"], row["mode"]) for row in stages if (row["key"], row["mode"]) in {
        ("script_input", "confirmed_revision"), ("script_input", "manual"), ("script_generate", "ai")}]
    if len(inputs) != 1:
        raise ValidationError("配方必须只有一个明确的内容起点")
    counts = [values[key] for key in ("episode_count", "shots_per_episode", "episode_duration_seconds")]
    if inputs[0] == ("script_generate", "ai"):
        if script_revision is not None or episode_ids or not values["idea"].strip() or any(value is None for value in counts):
            raise ValidationError("AI 剧本需要本轮创意、集数、每集镜数和时长；不能混入已有剧本或选集")
    elif inputs[0] == ("script_input", "confirmed_revision"):
        if script_revision is None or episode_ids or values["idea"] or values["style_hint"]:
            raise ValidationError("导入制作需要明确已确认剧本，不同时接收创意或已有选集")
        if any(value is not None for value in counts) and (
                not all(value is not None for value in counts) or not any(
                    row["key"] == "script_structure" and row["mode"] == "ai" for row in stages)):
            raise ValidationError("集数、镜数和时长必须完整提供，且配方需要声明剧本结构化阶段")
    elif (script_revision is not None or not episode_ids or values["idea"] or values["style_hint"]
            or any(value is not None for value in counts)):
        raise ValidationError("从已有分集制作必须明确选择剧集，不混入剧本生成或结构化计划")
    return ({"kind": "episodes", "episode_ids": episode_ids} if episode_ids else {"kind": "project"}), (
        {"kind": "script_revision", "id": str(script_revision.pk), "expected_sha256": script_revision.content_sha256}
        if script_revision is not None else None)


@registry_reference_writer
@transaction.atomic
def create_recipe_run_root(*, user, project, recipe, script_revision, idempotency_key, allow_external_calls,
        config_overrides, idea, style_hint, episode_count,
        shots_per_episode, episode_duration_seconds, episode_ids, unattended_confirmed, project_source_confirmations=None):
    from studio.services.production_runs import _recipe_stages, _snapshot_config_overrides, ProductionRunConflict
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied("需要有效登录身份")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 120:
        raise ValidationError({"idempotency_key": "长度必须为 1 到 120"})
    if type(unattended_confirmed) is not bool or not isinstance(config_overrides, dict):
        raise ValidationError("必须明确提供布尔授权和配置对象")
    if not isinstance(episode_ids, list):
        raise ValidationError("已有剧集范围必须是明确列表")
    episode_ids = sorted(str(value) for value in episode_ids)
    if len(episode_ids) != len(set(episode_ids)):
        raise ValidationError("不能重复选择同一剧集")
    values = validate_creation_inputs({"idea": idea, "style_hint": style_hint, "episode_count": episode_count,
        "shots_per_episode": shots_per_episode, "episode_duration_seconds": episode_duration_seconds})
    from studio.services.production_project_snapshots import normalize_project_source_confirmations, validate_recipe_project_confirmations
    confirmations = normalize_project_source_confirmations([] if project_source_confirmations is None else project_source_confirmations)
    type(user).objects.select_for_update().only("id").get(pk=user.pk)
    project = SeriesProject.objects.select_for_update().get(pk=project.pk)
    if project.owner_id != user.pk and not user.is_staff:
        raise PermissionDenied("无权为此项目创建制作运行")
    # Request identity precedes mutable resource/source validation on replay.
    # Do not reread or resave current Recipe/config to recreate an accepted run.
    intent = json_contract({"schema_version": 4, "plan_source": "recipe", "project_id": str(project.pk),
        "recipe_version_id": str(recipe.pk), "script_revision_id": str(script_revision.pk) if script_revision else None,
        "episode_ids": episode_ids, "creation_inputs": values, "unattended_confirmed": unattended_confirmed,
        "project_source_confirmations": confirmations,
        "allow_external_calls": allow_external_calls, "config_overrides": _snapshot_config_overrides(config_overrides)},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    previous = ProductionRun.objects.select_for_update().filter(owner=user, idempotency_key=idempotency_key.strip()).first()
    if previous is not None:
        if previous.request_sha256 != intent.sha256 or previous.plan_source != "recipe":
            raise ProductionRunConflict({"idempotency_key": "该键已用于不同制作运行请求"})
        verified_creation_inputs(previous)
        plan = get_run_execution_plan(previous)
        try:
            verify_recipe_stage_rows(previous, plan)
        except ValidationError as exc:
            raise ProductionRunConflict({"run_plan": exc.messages}) from exc
        return previous, False
    recipe = ProductionRecipeVersion.objects.select_for_update().get(pk=recipe.pk)
    if recipe.status != "published" or recipe.definition_schema_version != 3:
        raise ValidationError("只能创建已发布 schema3 精确配方的运行")
    stages = _recipe_stages(recipe)
    scope, initial = _creation_source(stages, script_revision=script_revision, values=values, episode_ids=episode_ids)
    if any(row["key"] == "storyboard" and row["mode"] == "deterministic" for row in stages):
        from studio.services.production_storyboard_materialization import validate_automatic_storyboard_target
        validate_automatic_storyboard_target(project)
    validate_recipe_creation_mappings(stages, values)
    validate_recipe_project_confirmations(stages, confirmations, owner=user, project_id=project.pk)
    if unattended_confirmed != (recipe.definition["interaction_mode"] == "unattended"):
        raise ValidationError("无人值守必须明确确认；辅助制作不接收无人值守授权")
    base = resolve_configuration(project=project)
    if allow_external_calls is None:
        allow_external_calls = False
    merged, _, _, external = prepare_run_configuration(base_config=base, config_overrides=config_overrides,
        allow_external_calls=allow_external_calls)
    run = ProductionRun(owner=user, project=project, plan_source="recipe", recipe_version=recipe,
        script_revision=script_revision, idempotency_key=idempotency_key.strip(), request_sha256=intent.sha256,
        status="queued", allow_external_calls=external)
    for field in ("config_snapshot", "input_snapshot"):
        assign_json_contract(run, field, {"schema_version": 1}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    run.save()
    catalog = production_stage_catalog()
    plan = freeze_run_execution_plan(run=run, scope=scope, initial_content=initial, delivery={"kind": "episode_masters"},
        stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=user)
    assign_json_contract(run, "input_snapshot", {"schema_version": 4, "request_intent": intent.value,
        "run_plan_sha256": plan.definition_sha256}, schema_version=4, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    run.full_clean()
    run.save(update_fields=("input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at"))
    freeze_run_configuration(run, overrides=merged, actor=user)
    rows = []
    for ordinal, declaration in enumerate(stages, 1):
        row = StageRun(production_run=run, stage_key=declaration["key"], mode=declaration["mode"], ordinal=ordinal,
            approval_required=declaration["approval"] == "required", decision_policy=declaration.get("decision_policy", ""),
            status="pending", idempotency_key=f"{run.pk}:{declaration['key']}")
        if declaration["approval"] == "policy":
            row.decision_policy_sha256 = json_contract({"schema_version": 1,
                "interaction_mode": plan.definition["recipe"]["definition"]["interaction_mode"],
                "stage_key": declaration["key"], "decision_policy": declaration["decision_policy"],
                "recipe_definition_sha256": plan.definition["recipe"]["sha256"]}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256
        # Route rules live only in RunPlan. These original fields remain
        # uninitialized until the stage adapter freezes its own actual inputs.
        for field in ("routing", "routing_decision", "output_snapshot"):
            assign_json_contract(row, field, {"schema_version": 2, "run_plan_sha256": plan.definition_sha256},
                schema_version=2, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        rows.append(row)
    StageRun.objects.bulk_create(rows)
    from studio.services.production_run_auto_advance import schedule_recipe_run_advance
    schedule_recipe_run_advance(run.pk)
    return run, True
