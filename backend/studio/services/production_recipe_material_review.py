"""Project exact reused and generated Run results into the original review.

No library search, new selection, Asset copy or generation fact is created.
Original fulfillment/selection/registration readers own all result validation.
"""
from copy import deepcopy
from pathlib import Path

from django.core.exceptions import ValidationError

from studio.services.production_recipe_material_fulfillment import (
    material_fulfillment_reference, read_recipe_material_fulfillment_choices, read_recipe_material_fulfillment,
)


def require_complete_recipe_material_results(stage, canonical):
    """Completion requires every frozen appearance, including units never prepared."""
    from studio.services.production_recipe_material_units import frozen_recipe_material_units
    from studio.services.production_run_plans import get_run_execution_plan
    units, _ = frozen_recipe_material_units(stage, get_run_execution_plan(stage.production_run))
    results = canonical.get("material_fulfillments", []) + canonical.get("material_generations", [])
    if (canonical.get("generation_stage_id") != str(stage.pk) or not units
            or len(results) != len(units)
            or {(row["unit_key"], row["requirement_key"]) for row in results}
            != {(row["unit_key"], row["requirement_key"]) for row in units}):
        raise ValidationError("本轮冻结素材需求尚未全部选择并登记为确切版本")
    from studio.services.production_recipe_material_selection import (
        _context, read_recipe_material_selection, recipe_material_reselection_pending,
    )
    for result in canonical.get("material_generations", []):
        key = result["unit_key"]
        if recipe_material_reselection_pending(stage=stage, context=_context(stage, key),
                previous=read_recipe_material_selection(stage=stage, unit_key=key)):
            raise ValidationError("明确追加候选后须重新选择或确认保留原图，不能复用旧决定完成阶段")


def read_recipe_material_generations(stage):
    """Read original selected/registered appearance results, without choosing.

    A candidate preview may precede selection/registration; a completed stage
    may not. Entity-only generation is handled by its existing review contract.
    """
    from studio.models import ExecutionPreparation, ProductionMaterialFulfillment
    from studio.services.asset_references import read_generated_material
    from studio.services.production_recipe_material_selection import read_recipe_material_selection

    keys = (ExecutionPreparation.objects.filter(stage_run=stage, unit_key__startswith="appearance:")
        .order_by("unit_key").values_list("unit_key", flat=True).distinct())
    # Absence is an indexed identity check, not another full M3/media read for
    # every generated unit. Existing choices still use the original validator.
    fulfilled = set(ProductionMaterialFulfillment.objects.filter(stage_run=stage).values_list("unit_key", flat=True))
    results = []
    for key in keys:
        fulfillment = read_recipe_material_fulfillment(stage=stage, unit_key=key) if key in fulfilled else None
        choice = read_recipe_material_selection(stage=stage, unit_key=key)
        if fulfillment is not None:
            if choice is not None:
                raise ValidationError("同一单元同时存在成图履约与生成选择，不能自动择一")
            # Failed requests remain evidence, not missing successful results.
            # The fulfillment reader rechecks all failures and actual bytes.
            continue
        version = read_generated_material(artifact=choice["artifact"]) if choice else None
        if version is None:
            if stage.status == "succeeded":
                raise ValidationError("已完成素材阶段缺少原生成选择或确切登记版本")
            continue
        results.append({**choice, "version": version, "asset": version.asset})
    return results


def material_generation_reference(choice):
    version, decision = choice["version"], choice["decision"]
    return {"unit_key": choice["unit"]["unit_key"], "requirement_key": choice["unit"]["requirement_key"],
        "decision_id": str(decision.pk), "artifact_id": str(choice["artifact"].pk),
        "reference_version_id": str(version.pk), "generated_from_id": str(version.generated_from_id) if version.generated_from_id else None,
        "source": "generated"}


def material_review_display(choice, reference):
    """Frozen display projection of the original choice, never a new selection."""
    asset = choice["asset"]
    return {"asset_id": str(asset.pk), "asset_name": asset.name,
        "checksum": asset.checksum, "mime_type": asset.mime_type,
        "appearance_role": choice["unit"]["appearance_role"], "reference": reference}


def material_review_context(run, canonical):
    references = canonical.get("material_fulfillments", [])
    generations = canonical.get("material_generations", [])
    if not isinstance(references, list) or not isinstance(generations, list):
        raise ValidationError("素材履约审核需要确切版本清单")
    if not references and not generations:
        return {}
    if run.plan_source != "recipe":
        raise ValidationError("素材履约审核必须属于原配方运行及确切版本清单")
    stage = run.stages.filter(pk=canonical.get("generation_stage_id"),
        stage_key="bible_image_generation", status="succeeded").first()
    if stage is None:
        raise ValidationError("素材履约审核缺少本轮已完成的原素材阶段")
    stage.production_run = run
    choices = read_recipe_material_fulfillment_choices(stage=stage)
    if [material_fulfillment_reference(choice) for choice in choices] != references:
        raise ValidationError("素材履约审核清单与原选择不一致，不能补选或忽略版本")
    generated = read_recipe_material_generations(stage)
    if [material_generation_reference(choice) for choice in generated] != generations:
        raise ValidationError("素材生成审核清单与原选择或登记版本不一致")
    result = {}
    for field, pairs in (("material_fulfillments", zip(references, choices)),
            ("material_generations", zip(generations, generated))):
        for reference, choice in pairs:
            _add_material_fact(result, field, reference, choice)
    return result


def _add_material_fact(result, field, reference, choice):
    unit = choice["unit"]
    key = (unit["scope"]["kind"], unit["scope"]["id"])
    entry = result.setdefault(key, {"primary_asset_id": None, "assets": {}})
    asset = choice["asset"]
    asset_id = str(asset.pk)
    if unit["appearance_role"] == "primary":
        if entry["primary_asset_id"] is not None:
            raise ValidationError("同一实体存在多个主图履约，不能自动择一")
        entry["primary_asset_id"] = asset_id
    # The original reader verifies bytes and exact setting/registration, not
    # a guessed category or a new candidate. No file or provenance is copied.
    fact = entry["assets"].setdefault(asset_id, {
        "asset_id": asset_id, "kind": asset.kind, "mime_type": asset.mime_type,
        "size_bytes": Path(asset.file.path if asset.file else asset.local_path).stat().st_size,
        "checksum": asset.checksum, "material_fulfillments": [],
    })
    fact.setdefault(field, []).append(deepcopy(reference))


def normalize_material_review_selection(selection, context):
    """An explicit candidate means its already selected Asset, not a new choice."""
    expected = context.get((selection["entity_type"], selection["entity_id"]))
    if expected is None:
        return selection
    selection = deepcopy(selection)
    assets_by_artifact = {row["artifact_id"]: (asset_id, fact)
        for asset_id, fact in expected["assets"].items() for row in fact.get("material_generations", [])}
    retained = []
    for artifact_id in selection["selected_artifact_ids"]:
        if artifact_id not in assets_by_artifact:
            retained.append(artifact_id)
            continue
        asset_id, fact = assets_by_artifact[artifact_id]
        if any(row["asset_id"] == asset_id for row in selection["selected_library_assets"]):
            raise ValidationError("同一已选素材不能作为候选和素材库图片重复提交")
        selection["selected_library_assets"].append({"asset_id": asset_id, "expected_sha256": fact["checksum"]})
        if selection["anchor_artifact_id"] == artifact_id:
            selection["anchor_artifact_id"] = None
            selection["anchor_library_asset_id"] = asset_id
    selection["selected_artifact_ids"] = retained
    return selection


def validate_material_review_selection(selection, context):
    """Review may not silently replace a result already used by dependents."""
    expected = context.get((selection["entity_type"], selection["entity_id"]))
    if expected is None:
        return
    selected = {row["asset_id"] for row in selection["selected_library_assets"]}
    if not set(expected["assets"]) <= selected:
        raise ValidationError("审核必须保留本轮已履约的确切素材；改选须沿依赖变更流程处理")
    primary = expected["primary_asset_id"]
    if primary is not None and selection["anchor_library_asset_id"] != primary:
        raise ValidationError("审核不能替换本轮已履约主图或把变体当作主图")
