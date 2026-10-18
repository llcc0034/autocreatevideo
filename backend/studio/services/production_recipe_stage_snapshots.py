"""Freeze non-model Recipe stages against the same RunPlan and configuration.

Model stages retain their specific Preparation adapters. This module does not
resolve deployments, merge model defaults, select media or approve a stage.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.services.configuration import resolve_configuration_from_snapshot
from studio.services.assembly_contracts import validate_assembly_sources
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.production_recipe_preparations import recipe_stage_context
from studio.services.production_recipe_contracts import recipe_stage_kind
from studio.services.production_stage_catalog import stage_requires_execution_route
from studio.services.security_boundaries import boundary_int


def _identity(stage):
    plan, declaration = recipe_stage_context(stage)
    if stage_requires_execution_route(recipe_stage_kind(declaration), stage.mode):
        raise ValidationError("模型调用阶段必须使用对应执行准备适配器，不能退回普通阶段快照")
    limit = boundary_int("security.snapshot.stage_max_bytes")
    return {"schema_version": 2, "stage_key": stage.stage_key, "run_plan_sha256": plan.sha256,
        "stage_declaration_sha256": json_contract(declaration, max_bytes=limit).sha256}, limit


def recipe_non_model_config_contract(stage):
    value, limit = _identity(stage)
    run = stage.production_run
    verified = verify_json_contract(run.config_snapshot, schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes, sha256=run.config_snapshot_sha256, max_bytes=limit)
    # Business configuration comes from the frozen Run, not current revisions.
    # Revalidate today's configured ceilings without replacing any frozen
    # value/provenance with the current security revision's metadata.
    resolve_configuration_from_snapshot(verified.value)
    value.update(run_config_sha256=verified.sha256, base=verified.value)
    if stage.stage_key == "assembly":
        from studio.services.assembly import AssemblyError, resolve_assembly_configuration
        from studio.services.assembly_contracts import validate_assembly_configuration
        from studio.services.production_recipe_assembly_output import apply_recipe_assembly_output
        _, declaration = recipe_stage_context(stage)
        saved_mapping = None
        if stage.config_snapshot_sha256:
            saved = verify_json_contract(stage.config_snapshot, schema_version=stage.config_snapshot_schema_version,
                size_bytes=stage.config_snapshot_size_bytes, sha256=stage.config_snapshot_sha256, max_bytes=limit)
            if not isinstance(saved.value, dict):
                raise ValidationError("装配冻结配置必须是完整对象")
            configuration = validate_assembly_configuration(saved.value.get("assembly_configuration"), max_bytes=limit)
            if "assembly_output" in declaration:
                if not isinstance(saved.value.get("assembly_output"), dict):
                    raise ValidationError("装配缺少已冻结的作者映射，不能补当前版本")
                saved_mapping = saved.value["assembly_output"]
        else:
            # Business policy comes from the original Run; geometry and the local
            # font are captured once when this stage is first prepared.
            try:
                configuration = resolve_assembly_configuration(run.project, frozen_values=verified.value["values"])
            except AssemblyError as exc:
                raise ValidationError(str(exc)) from exc
        if "assembly_output" in declaration:
            projected, mapping = apply_recipe_assembly_output(declaration, configuration,
                max_bytes=limit, frozen=saved_mapping, stage_run=stage)
            if stage.config_snapshot_sha256 and projected != configuration:
                raise ValidationError("冻结装配参数与原作者映射不一致，不能静默覆盖")
            configuration = projected
            value["assembly_output"] = mapping
        value["assembly_configuration"] = configuration
        for author_key in ("assembly_timing", "assembly_media"):
            if author_key not in declaration:
                continue
            from studio.services.production_recipe_assembly_output import recipe_assembly_output_projection
            frozen_author = None
            if stage.config_snapshot_sha256:
                frozen_author = saved.value.get(author_key)
                if not isinstance(frozen_author, dict):
                    raise ValidationError("装配缺少已冻结的时序或片段作者映射，不能补当前版本")
            value[author_key] = recipe_assembly_output_projection(declaration, key=author_key,
                max_bytes=limit, frozen=frozen_author, stage_run=stage)
    if stage.stage_key == "delivery":
        from studio.services.production_recipe_assembly_output import recipe_assembly_output_projection
        _, declaration = recipe_stage_context(stage)
        if "delivery_metadata" in declaration:
            frozen = None
            if stage.config_snapshot_sha256:
                saved = verify_json_contract(stage.config_snapshot, schema_version=stage.config_snapshot_schema_version,
                    size_bytes=stage.config_snapshot_size_bytes, sha256=stage.config_snapshot_sha256, max_bytes=limit)
                frozen = saved.value.get("delivery_metadata")
                if not isinstance(frozen, dict):
                    raise ValidationError("交付缺少已冻结的字段来源，不能补当前版本")
            value["delivery_metadata"] = recipe_assembly_output_projection(declaration, key="delivery_metadata",
                max_bytes=limit, frozen=frozen, stage_run=stage)
    return json_contract(value, max_bytes=limit)


def recipe_delivery_metadata(stage):
    """Read the original validated stage contract for sealing, never new author values."""
    if stage.stage_key != "delivery" or not stage.config_snapshot_sha256:
        raise ValidationError("交付必须先冻结配置及字段来源")
    expected = recipe_non_model_config_contract(stage)
    saved = verify_json_contract(stage.config_snapshot, schema_version=stage.config_snapshot_schema_version,
        size_bytes=stage.config_snapshot_size_bytes, sha256=stage.config_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if saved.value != expected.value:
        raise ValidationError("交付字段与原冻结计划不一致")
    return saved.value.get("delivery_metadata")


def recipe_assembly_configuration(stage):
    """Read the same prepared configuration for initial enqueue and retry."""
    if stage.stage_key != "assembly" or not stage.config_snapshot_sha256:
        raise ValidationError("装配必须先冻结本轮配置，不能入队时读取当前值")
    expected = recipe_non_model_config_contract(stage)
    verified = verify_json_contract(stage.config_snapshot, schema_version=stage.config_snapshot_schema_version,
        size_bytes=stage.config_snapshot_size_bytes, sha256=stage.config_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if verified.value != expected.value:
        raise ValidationError("装配配置与本轮计划或原配置不一致")
    return verified.value["assembly_configuration"]


@transaction.atomic
def _assembly_source_snapshot(stage, *, lock=True):
    """Freeze adopted identities and authored timing, not new media decisions.

    The original selection reader verifies origin/selection/media evidence.
    Project locking follows the caller's Run lock and the storyboard writers.
    No alternate file, last task, or current configuration supplies missing data.
    """
    from django.db.models import OuterRef, Subquery, UUIDField
    from studio.models import ArtifactSelectionDecision, GeneratedArtifact, SeriesProject, Shot
    from studio.services.assembly import AssemblyError
    from studio.services.production_runs import _selected_assembly_sources, _selected_episodes
    from studio.services.shot_timing import validate_sequential_episode_timing

    run = stage.production_run
    if lock:
        SeriesProject.objects.select_for_update().only("id").get(pk=run.project_id)
    try:
        references = _selected_assembly_sources(run)
    except AssemblyError as exc:
        raise ValidationError(str(exc)) from exc
    episodes = _selected_episodes(run)
    shots = Shot.objects.filter(episode__in=episodes).order_by("number", "id").values(
        "id", "episode_id", "number", "duration_ms", "media_type", "episode_start_ms", "episode_end_ms")
    grouped = {str(episode.pk): [] for episode in episodes}
    latest = ArtifactSelectionDecision.objects.filter(stage_run_id=OuterRef("stage_run_id"),
        variant_group=OuterRef("variant_group")).order_by("-revision")
    artifacts = {str(row["id"]): row for row in GeneratedArtifact.objects.filter(
        pk__in=[ref["generated_artifact_id"] for ref in references.values()], production_run=run).annotate(
            decision_id=Subquery(latest.values("id")[:1]),
            decision_revision=Subquery(latest.values("revision")[:1]),
            decision_artifact_id=Subquery(latest.values("artifact_id")[:1], output_field=UUIDField())).values(
                "id", "sha256", "manifest_metadata_sha256", "decision_id", "decision_revision", "decision_artifact_id")}
    for shot in shots:
        reference = references.get(str(shot["id"]))
        artifact = artifacts.get(reference["generated_artifact_id"]) if reference else None
        if artifact is None or artifact["decision_artifact_id"] != artifact["id"]:
            raise ValidationError("装配来源与正式采用决定不一致，不能冻结")
        grouped[str(shot["episode_id"])].append({
            "shot_id": str(shot["id"]), "number": shot["number"], "duration_ms": shot["duration_ms"],
            "media_kind": shot["media_type"], "episode_start_ms": shot["episode_start_ms"],
            "episode_end_ms": shot["episode_end_ms"], "source": {
                **reference, "sha256": artifact["sha256"], "manifest_sha256": artifact["manifest_metadata_sha256"],
                "decision_id": str(artifact["decision_id"]), "decision_revision": artifact["decision_revision"]}})
    result = []
    for episode in episodes:
        rows = grouped[str(episode.pk)]
        if not rows:
            raise ValidationError("装配剧集缺少明确的逐镜来源")
        try:
            validate_sequential_episode_timing(rows)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        result.append({"episode_id": str(episode.pk), "number": episode.number, "shots": rows})
    return validate_assembly_sources({"schema_version": 1, "episodes": result},
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))


def recipe_assembly_sources(stage, *, lock=True):
    """First enqueue and explicit retry consume one original input snapshot.

    Changes are a conflict, not consent to replace an earlier failed job's input.
    No caller may synthesize a missing historical assembly source contract.
    """
    if stage.stage_key != "assembly" or not stage.input_snapshot_sha256:
        raise ValidationError("装配必须先冻结逐镜来源和时序，不能入队时读取当前选片")
    saved = verify_json_contract(stage.input_snapshot, schema_version=stage.input_snapshot_schema_version,
        size_bytes=stage.input_snapshot_size_bytes, sha256=stage.input_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if not isinstance(saved.value, dict) or "assembly_sources" not in saved.value:
        raise ValidationError("装配缺少已冻结逐镜来源，不能为旧阶段补造")
    sources = validate_assembly_sources(saved.value["assembly_sources"],
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    expected = recipe_non_model_input_contract(stage, lock=lock)
    if saved.value != expected.value:
        raise ValidationError("已冻结装配来源、采用修订、顺序或时长已变化；本轮不能换源重试，请新建制作轮次")
    return sources


def recipe_non_model_input_contract(stage, *, lock=True):
    value, limit = _identity(stage)
    config = recipe_non_model_config_contract(stage)
    verified = verify_json_contract(stage.config_snapshot, schema_version=stage.config_snapshot_schema_version,
        size_bytes=stage.config_snapshot_size_bytes, sha256=stage.config_snapshot_sha256, max_bytes=limit)
    if verified.value != config.value:
        raise ValidationError("阶段配置与本轮计划或冻结运行配置不一致")
    plan, declaration = recipe_stage_context(stage)
    declarations = plan.value["recipe"]["definition"]["stages"]
    expected_keys = [row["key"] for row in declarations[:declarations.index(declaration)]]
    sources = list(stage.production_run.stages.filter(ordinal__lt=stage.ordinal).order_by("ordinal"))
    if [source.stage_key for source in sources] != expected_keys:
        raise ValidationError("本轮冻结计划的前置阶段不完整，不能通过读取补造或跳过")
    upstream = []
    for source in sources:
        if source.status != "succeeded":
            raise ValidationError("前置阶段尚未完成，不能冻结后续阶段输入")
        recipe_stage_context(source)
        output = verify_json_contract(source.output_snapshot, schema_version=source.output_snapshot_schema_version,
            size_bytes=source.output_snapshot_size_bytes, sha256=source.output_snapshot_sha256, max_bytes=limit)
        upstream.append({"stage_id": str(source.pk), "stage_key": source.stage_key,
            "status": source.status, "output_snapshot_sha256": output.sha256})
    value.update(config_snapshot_sha256=config.sha256, upstream=upstream)
    if stage.stage_key == "assembly":
        value["assembly_sources"] = _assembly_source_snapshot(stage, lock=lock)
        if "assembly_timing" in declaration:
            from studio.services.assembly_contracts import assert_assembly_timing_matches
            assert_assembly_timing_matches(config.value["assembly_timing"]["projection"]["output"], value["assembly_sources"])
        if "assembly_media" in declaration:
            from studio.services.assembly_contracts import assert_assembly_media_matches
            assert_assembly_media_matches(config.value["assembly_media"]["projection"]["output"], value["assembly_sources"])
    if "shot_reference_selection" in declaration:
        from studio.services.production_recipe_shot_references import shot_reference_source_snapshot
        value["shot_reference_selection"] = shot_reference_source_snapshot(declaration, upstream)
    if stage.stage_key == "storyboard" and stage.mode == "deterministic":
        from studio.services.production_storyboard_materialization import read_storyboard_materialization_plan
        proposed = read_storyboard_materialization_plan(run=stage.production_run, stage=stage)
        value["materialization"] = {"contract": proposed.contract.value, "sha256": proposed.contract.sha256}
    return json_contract(value, max_bytes=limit)
