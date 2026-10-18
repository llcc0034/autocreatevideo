"""Explicit per-shot source rules, separate from approval and model purposes.

The recipe owns the rule; the original Stage input owns its upstream identity.
This module never selects media, overwrites a use, or creates review evidence.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import AssetReferenceVersion
from studio.services.json_contracts import json_contract
from studio.services.production_material_references import read_upstream_material_catalog, read_upstream_material_result


class RecipeShotReferenceSourceConflict(ValidationError):
    """A valid saved choice is outside this round's explicit source rule."""

    def __init__(self, message, *, shot_id):
        super().__init__(message, code="shot_reference_source_conflict")
        self.shot_id = str(shot_id)


SHOT_REFERENCE_SOURCE_OPTIONS = (
    {"value": "upstream", "label": "本轮上游素材结果", "upstream_stage_key": "bible_image_generation",
     "selection_options": [{"value": "manual", "label": "人工选择"},
         {"value": "unique_setting", "label": "自动匹配唯一确切设定，否则人工接管"}]},
    {"value": "existing", "label": "已有素材库", "upstream_stage_key": None,
     "selection_options": [{"value": "manual", "label": "人工选择"}]},
    {"value": "upload", "label": "本地上传素材", "upstream_stage_key": None,
     "selection_options": [{"value": "manual", "label": "人工选择"}]},
)


def validate_shot_reference_selection(stage, prior_stages):
    if "shot_reference_selection" not in stage:
        return
    rule = stage["shot_reference_selection"]
    path = "shot_reference_selection"
    if (stage.get("key") != "storyboard_review" or stage.get("mode") != "review"
            or not isinstance(rule, dict)
            or set(rule) != {"source", "source_stage_key", "selection", "allow_override"}):
        raise ValidationError({path: "仅分镜参考步骤可配置完整的来源、上游、选择方式和允许改选规则"})
    option = next((row for row in SHOT_REFERENCE_SOURCE_OPTIONS if row["value"] == rule["source"]), None)
    if option is None or not any(row["value"] == rule["selection"] for row in option["selection_options"]):
        raise ValidationError({path: "请选择已声明的素材来源及其选择方式；不推断自动库内选材或模型用途"})
    if type(rule["allow_override"]) is not bool:
        raise ValidationError({path + ".allow_override": "必须明确是否允许替换本镜已有选择"})
    source_key = option["upstream_stage_key"]
    if source_key is None:
        valid = rule["source_stage_key"] is None
    else:
        valid = rule["source_stage_key"] == source_key and any(
            row["key"] == source_key and row["mode"] == "existing_pipeline" for row in prior_stages)
    if not valid:
        raise ValidationError({path + ".source_stage_key": "上游必须是本配方之前的素材结果阶段；已有/上传来源不携带闲置上游"})


def shot_reference_source_snapshot(declaration, upstream):
    """Bind a validated rule to the original completed-stage reference, not Assets."""
    rule = declaration["shot_reference_selection"]
    source = None
    if rule["source"] == "upstream":
        matches = [row for row in upstream if row["stage_key"] == rule["source_stage_key"]]
        if len(matches) != 1:
            raise ValidationError("分镜选材缺少唯一明确的本轮上游，不能按名字或最近结果补齐")
        source = {"stage_run_id": matches[0]["stage_id"],
            "output_snapshot_sha256": matches[0]["output_snapshot_sha256"]}
    return {"rule": deepcopy(rule), "upstream": source}


def recipe_storyboard_reference_source(run, review):
    """One creative-source boundary for automatic and human recipe selection.

    Callers hold Run/Project locks. This reads the original frozen input and
    completed storyboard, never refreezes either or resolves selected files.
    """
    from studio.models import StageRun
    from studio.services.configuration import redact_sensitive_bindings
    from studio.services.json_contracts import json_contract
    from studio.services.production_runs import (
        _assert_storyboard_review_source, _stage_snapshot_max_bytes, _verify_stage_snapshot)
    from studio.services.production_shot_reference_defaults import read_configured_shot_reference_selection

    configured = read_configured_shot_reference_selection(review)
    source = run.stages.select_for_update().filter(stage_key="storyboard", status=StageRun.Status.SUCCEEDED).first()
    if source is None:
        raise ValidationError("配方分镜选材缺少本轮已完成的分镜来源")
    snapshot = _verify_stage_snapshot(source, "output_snapshot")
    source_sha = json_contract(redact_sensitive_bindings(snapshot), max_bytes=_stage_snapshot_max_bytes()).sha256
    _assert_storyboard_review_source(run, expected_source_snapshot_sha256=source_sha)
    shot_ids = [row["id"] for episode in snapshot["canonical"]["episodes"] for row in episode["shots"]]
    if not shot_ids or len(shot_ids) != len(set(shot_ids)):
        raise ValidationError("配方分镜选材必须有本轮明确且不重复的镜头")
    return configured, source_sha, shot_ids


def recipe_shot_reference_catalog(run, stage, configured):
    """Metadata only; selecting/reviewing must verify original results and files."""
    if not configured or configured["rule"]["source"] != "upstream":
        return []
    upstream = configured["upstream"]
    entries = read_upstream_material_catalog(stage=stage, source_stage_id=upstream["stage_run_id"],
        expected_output_sha256=upstream["output_snapshot_sha256"])
    ids = {entry["reference"]["reference_version_id"] for entry in entries}
    versions = {str(row["id"]): row for row in AssetReferenceVersion.objects.filter(
        pk__in=ids, series__owner_id=run.owner_id).order_by().values(
            "id", "version", "setting_revision_id", "asset_id", "asset__checksum", "asset__name", "asset__mime_type")}
    if set(versions) != ids:
        raise ValidationError("本轮上游的素材版本已缺失或不属于运行所有者")
    candidates = []
    for entry in entries:
        version = versions[entry["reference"]["reference_version_id"]]
        candidates.append({"source": entry,
            "source_mode": "generated" if entry["field"] == "material_generations" else "existing",
            "reference_version_id": entry["reference"]["reference_version_id"], "version": version["version"],
            "setting_revision_id": str(version["setting_revision_id"]) if version["setting_revision_id"] else None,
            "asset_id": str(version["asset_id"]), "asset_sha256": version["asset__checksum"],
            "name": version["asset__name"], "mime_type": version["asset__mime_type"]})
    return candidates


def validate_recipe_shot_reference_sources(*, stage, configured, catalog, choices, shot_id=None):
    """One frozen-source check for human writes and review of saved choices.

    This does not choose/replace anything, allocate slots or assert approval.
    Version acquisition provenance is not rewritten to the round's usage mode.
    """
    if not configured:
        return
    rule = configured["rule"]
    checked = set()
    for choice in choices:
        if rule["source"] == "upstream":
            matches = [row for row in catalog if row["reference_version_id"] == str(choice["reference_version_id"])
                and row["source_mode"] == choice["source_mode"] and row["asset_sha256"] == choice["asset_sha256"]]
            if choice["asset_id"] is not None or not matches:
                raise RecipeShotReferenceSourceConflict(
                    "已有选择与本轮冻结上游的确切素材版本或使用来源不一致；请按本轮规则明确改选，不自动覆盖原选择。",
                    shot_id=choice.get("shot_id", shot_id))
            for match in matches:
                identity = json_contract(match["source"]).sha256
                if identity in checked:
                    continue
                resolved = read_upstream_material_result(stage=stage, source=match["source"])
                if str(resolved["version"].pk) != str(choice["reference_version_id"]) or resolved["asset"].checksum != choice["asset_sha256"]:
                    raise ValidationError("上游原结果与明确选择不一致")
                checked.add(identity)
        elif choice["source_mode"] != rule["source"]:
            raise RecipeShotReferenceSourceConflict("所选素材来源不符合本轮冻结来源规则；请明确改选。",
                shot_id=choice.get("shot_id", shot_id))
