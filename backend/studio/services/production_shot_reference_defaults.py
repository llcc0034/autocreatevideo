"""Read-only exact-setting match for a recipe's declared upstream material.

The original stage adapter must authorize the frozen default/override policy
and re-read this plan before applying choices through shot_reference_choices.
This is not a new command, approval, scheduler or execution-purpose mapper.
No source stage, latest version, missing setting or existing choice is inferred.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import AssetReferenceVersion, ProductionRun, StageRun
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_material_references import read_upstream_material_catalog, read_upstream_material_result
from studio.services.shot_reference_choices import ShotReferenceChoiceState, read_shot_reference_choices
from studio.services.shot_reference_sources import _reference_project, _resolve_rows, _use_rows


@dataclass(frozen=True)
class UpstreamShotReferencePlan:
    state: ShotReferenceChoiceState
    _items: tuple

    @property
    def items(self):
        return deepcopy(list(self._items))

    @property
    def choices(self):
        return [row["choice"] for row in self.items if row["status"] == "available"]

    @property
    def ready(self):
        return all(row["status"] in {"retained", "available"} for row in self._items)


@transaction.atomic
def plan_upstream_shot_reference_choices(*, stage_run_id, actor, shot_id, source_stage_id,
        expected_source_output_sha256):
    """Inspect only this shot, matching exact setting revisions in original outputs.

    A missing/ambiguous match is an explicit manual-takeover reason. A corrupt
    source, selection or file is an error, never a search of another source.
    Existing choices are validated and retained, including invalid choices
    failing explicitly rather than being replaced by an upstream default.
    """
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run_id = StageRun.objects.values_list("production_run_id", flat=True).get(pk=stage_run_id)
    run = ProductionRun.objects.select_for_update().only("id", "owner_id", "project_id", "plan_source").get(pk=run_id)
    if run.owner_id != actor.pk and not actor.is_staff:
        raise PermissionDenied("无权读取此制作运行")
    if run.plan_source != "recipe":
        raise ValidationError("上游默认选材计划需要原配方运行，不推断人工运行策略")
    project = _reference_project(actor, run.project_id, lock=True)
    run.project = project
    stage = StageRun.objects.only("id", "production_run_id", "stage_key", "ordinal", "mode").get(
        pk=stage_run_id, production_run_id=run.pk)
    stage.production_run = run
    unit = resolve_production_unit(run, {"kind": "shot", "id": str(shot_id)})
    shot_id = unit["scope"]["id"]
    state = read_shot_reference_choices(actor=actor, project_id=project.pk, shot_id=shot_id)
    rows = list(_use_rows([shot_id]))
    catalog = read_upstream_material_catalog(stage=stage, source_stage_id=str(source_stage_id),
        expected_output_sha256=expected_source_output_sha256)
    # One narrow indexed metadata query, not one setting lookup per use or a
    # library scan. File/choice integrity is delegated to the original readers.
    version_ids = {UUID(item["reference"]["reference_version_id"]) for item in catalog}
    versions = dict(AssetReferenceVersion.objects.filter(pk__in=version_ids, series__owner_id=run.owner_id)
        .order_by().values_list("id", "setting_revision_id"))
    if set(versions) != version_ids:
        raise ValidationError("上游目录的素材版本已缺失或不属于运行所有者")
    by_setting = {}
    for item in catalog:
        setting_id = versions[UUID(item["reference"]["reference_version_id"])]
        by_setting.setdefault(setting_id, []).append(item)
    items, prospective, resolved = [], [], {}
    for row in rows:
        item = {"use_id": str(row["id"]), "use_key": row["use_key"], "status": "retained",
            "reason": "保留本镜已有明确选择，不由默认规则覆盖。", "choice": None, "source": None}
        if (row["reference_version_id"] or row["asset_id"] or row["source_mode"] or row["asset_sha256"]):
            prospective.append(row)
        elif row["setting_revision_id"] is None:
            item.update(status="manual", reason="本用途未声明确切设定，需明确选材；不按名字或主图推断。")
        else:
            candidates = by_setting.get(row["setting_revision_id"], [])
            if len(candidates) != 1:
                item.update(status="ambiguous" if candidates else "missing",
                    reason="本轮上游存在多个同设定结果，需明确选择。" if candidates else "本轮上游没有此确切设定的素材结果，需明确选择。")
            else:
                source = candidates[0]
                version_id = UUID(source["reference"]["reference_version_id"])
                if version_id not in resolved:
                    resolved[version_id] = read_upstream_material_result(stage=stage, source=source)
                chosen = resolved[version_id]
                # Preserve acquisition provenance: a reused generated Asset
                # fulfilled in this round is still a reuse, not a new generation.
                mode = "generated" if source["field"] == "material_generations" else "existing"
                choice = {"use_id": str(row["id"]), "source_mode": mode,
                    "reference_version_id": str(chosen["version"].pk), "asset_id": None,
                    "asset_sha256": chosen["asset"].checksum}
                item.update(status="available", reason="本轮上游有唯一确切设定结果，待原命令按冻结策略应用。",
                    choice=choice, source=source)
                prospective.append({**row, "source_mode": mode, "reference_version_id": version_id,
                    "asset_id": None, "asset_sha256": choice["asset_sha256"]})
        items.append(item)
    # Batch validation reuses M5's single exact-source reader and deduplicates
    # repeated versions/files. This never marks pending uses as resolved.
    _resolve_rows(project, [shot_id], prospective)
    return UpstreamShotReferencePlan(state, tuple(items))


@transaction.atomic
def plan_configured_shot_reference_choices(*, stage_run_id, actor, shot_id):
    """Read automatic suggestions using only the original frozen recipe/input.

    No caller-supplied source, strategy or override flag can authorize a choice.
    Manual selection remains a separate original-command operation.
    """
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run_id = StageRun.objects.values_list("production_run_id", flat=True).get(pk=stage_run_id)
    run = ProductionRun.objects.select_for_update().only(
        "id", "owner_id", "project_id", "plan_source", "config_snapshot",
        "config_snapshot_schema_version", "config_snapshot_size_bytes", "config_snapshot_sha256").get(pk=run_id)
    if run.owner_id != actor.pk and not actor.is_staff:
        raise PermissionDenied("无权读取此制作运行")
    stage = StageRun.objects.only("id", "production_run_id", "stage_key", "mode", "ordinal",
        "config_snapshot", "config_snapshot_schema_version", "config_snapshot_size_bytes", "config_snapshot_sha256",
        "input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes", "input_snapshot_sha256"
    ).get(pk=stage_run_id, production_run=run)
    stage.production_run = run
    choice = read_configured_shot_reference_selection(stage)
    if not choice or choice["rule"]["selection"] != "unique_setting":
        raise ValidationError("本轮未明确配置自动确切设定选材，不能自动替代人工选择")
    return plan_upstream_shot_reference_choices(stage_run_id=stage.pk, actor=actor, shot_id=shot_id,
        source_stage_id=choice["upstream"]["stage_run_id"],
        expected_source_output_sha256=choice["upstream"]["output_snapshot_sha256"])


def read_configured_shot_reference_selection(stage):
    """Recheck the original Stage input; callers own authorization and locks."""
    from studio.services.production_recipe_stage_snapshots import recipe_non_model_input_contract
    from studio.services.production_runs import _verify_stage_snapshot
    if stage.production_run.plan_source != "recipe" or stage.stage_key != "storyboard_review":
        raise ValidationError("配方选材仅使用本轮分镜参考步骤")
    saved = _verify_stage_snapshot(stage, "input_snapshot")
    if recipe_non_model_input_contract(stage).value != saved:
        raise ValidationError("分镜选材输入或上游已改变，不能用新结果替代原冻结来源")
    return saved.get("shot_reference_selection")
