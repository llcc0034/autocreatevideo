"""Exact original material results shared by retained and upstream consumers.

These readers never choose the latest version, attach an Asset, generate media,
or turn a reference input into a fulfilled result. The caller authorizes the
project; upstream reads additionally enforce the original RunPlan topology.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("素材来源需要规范的原选择UUID，不能从Asset或名称推断")
    return value


def read_material_result_reference(*, project_id, field, reference, stage_id=None):
    from studio.models import ArtifactSelectionDecision, ProductionMaterialFulfillment
    from studio.services.asset_references import read_generated_material
    from studio.services.production_recipe_material_fulfillment import (
        material_fulfillment_reference, read_recipe_material_fulfillment,
    )
    from studio.services.production_recipe_material_selection import read_recipe_material_selection
    from studio.services.production_recipe_material_review import material_generation_reference

    if field not in ("material_generations", "material_fulfillments") or not isinstance(reference, dict):
        raise ValidationError("素材来源必须明确原生成选择或原履约引用")
    generated = field == "material_generations"
    model, key = (ArtifactSelectionDecision, "decision_id") if generated else (ProductionMaterialFulfillment, "fulfillment_id")
    rows = model.objects.filter(pk=_uuid(reference.get(key)),
        stage_run__production_run__project_id=project_id)
    if stage_id is not None:
        rows = rows.filter(stage_run_id=stage_id)
    row = rows.select_related("stage_run__production_run").first()
    if row is None:
        raise ValidationError("素材来源的原选择不存在或不属于指定项目/阶段")
    if generated:
        choice = read_recipe_material_selection(stage=row.stage_run, unit_key=reference.get("unit_key"),
            decision_id=row.pk, require_current=False)
        version = read_generated_material(artifact=choice["artifact"])
        if version is None:
            raise ValidationError("生成素材缺少原登记版本")
        choice = {**choice, "version": version, "asset": version.asset}
        actual = material_generation_reference(choice)
    else:
        choice = read_recipe_material_fulfillment(stage=row.stage_run, unit_key=row.unit_key, require_current=False)
        actual = material_fulfillment_reference(choice) if choice else None
    if actual != reference:
        raise ValidationError("素材来源的确切版本或选择回执已改变")
    return choice


def _upstream_material_output(stage, source_id, expected_sha256):
    from studio.models import StageRun
    from studio.services.production_execution_sources import execution_source_stage_ids
    from studio.services.production_runs import _verify_stage_snapshot, _sha256

    source_id = _uuid(source_id)
    if source_id not in execution_source_stage_ids(stage):
        raise ValidationError("素材来源不是本轮冻结计划中已完成的上游步骤")
    upstream = StageRun.objects.filter(pk=source_id, production_run_id=stage.production_run_id).only(
        "id", "status", "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
        "output_snapshot_sha256").first()
    if upstream is None:
        raise ValidationError("上游素材阶段已缺失")
    saved = _verify_stage_snapshot(upstream, "output_snapshot")
    canonical = saved.get("canonical")
    if (upstream.status != "succeeded" or upstream.output_snapshot_sha256 != expected_sha256
            or not isinstance(canonical, dict) or saved.get("canonical_sha256") != _sha256(canonical)):
        raise ValidationError("上游素材输出与冻结来源摘要不一致")
    return upstream, canonical


def read_upstream_material_catalog(*, stage, source_stage_id, expected_output_sha256):
    """List original exact result references, not current library candidates.

    This metadata read neither chooses nor proves file readiness. Every chosen
    reference must still pass read_upstream_material_result before use.
    """
    upstream, canonical = _upstream_material_output(stage, source_stage_id, expected_output_sha256)
    result, seen = [], set()
    for field, identity in (("material_generations", "decision_id"), ("material_fulfillments", "fulfillment_id")):
        references = canonical.get(field, [])
        if not isinstance(references, list):
            raise ValidationError("上游素材结果目录必须是明确数组")
        for reference in references:
            if not isinstance(reference, dict):
                raise ValidationError("上游素材结果缺少原选择引用")
            key = (field, _uuid(reference.get(identity)))
            _uuid(reference.get("reference_version_id"))
            if key in seen:
                raise ValidationError("上游素材结果存在重复选择，不能按顺序猜测")
            seen.add(key)
            result.append({"stage_run_id": str(upstream.pk), "output_snapshot_sha256": expected_output_sha256,
                "field": field, "reference": deepcopy(reference)})
    return result


def read_upstream_material_result(*, stage, source):
    """Verify a frozen source, not an authoring rule or an automatic selector.

    Only an exact completed upstream output can authorize this result. This
    read does not equate its setting with a downstream requirement; that match
    and the explicit policy remain the fulfillment caller's responsibility.
    """
    if (not isinstance(source, dict) or set(source) != {"stage_run_id", "output_snapshot_sha256", "field", "reference"}
            or source["field"] not in ("material_generations", "material_fulfillments")):
        raise ValidationError("上游素材来源必须包含确切阶段、输出摘要与原结果引用")
    upstream, canonical = _upstream_material_output(stage, source["stage_run_id"], source["output_snapshot_sha256"])
    references = canonical.get(source["field"], [])
    if (not isinstance(references, list) or references.count(source["reference"]) != 1):
        raise ValidationError("上游正式输出没有唯一的指定素材结果")
    return read_material_result_reference(project_id=stage.production_run.project_id, field=source["field"],
        reference=deepcopy(source["reference"]), stage_id=upstream.pk)
