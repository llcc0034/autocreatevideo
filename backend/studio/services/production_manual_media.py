"""Explicit manual media adoption over original Artifact/Selection facts."""
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import ArtifactSelectionDecision, GeneratedArtifact
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.generated_artifacts import _artifact_limit, select_artifact
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_stage_catalog import STAGE_MEDIA_SCOPE_KINDS


def media_selection_state(*, run_id, stage_id, group):
    decision = ArtifactSelectionDecision.objects.filter(stage_run_id=stage_id,
        variant_group=group).order_by("-revision").first()
    selected = list(GeneratedArtifact.objects.filter(production_run_id=run_id,
        variant_group=group, status="selected").values_list("id", flat=True))
    if (decision is None and selected) or (decision is not None and selected != [decision.artifact_id]):
        raise ValidationError("媒体采用记录与当前候选状态不一致，不能补造选择结果")
    return decision


def manual_media_candidate_detail(artifact):
    run = artifact.production_run
    if (run is None or run.plan_source != "manual" or artifact.stage_run is None
            or artifact.stage_run.stage_key != "media_generation" or artifact.kind not in {"image", "video", "audio"}):
        return None
    verified = read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(run))
    decision = media_selection_state(run_id=run.pk, stage_id=artifact.stage_run_id, group=artifact.variant_group)
    unit = verified["production_unit"]
    return {"run_id": str(run.pk), "stage_key": artifact.stage_run.stage_key,
        "unit_key": verified["unit_key"], "scope": unit["scope"], "source_kind": verified["source_kind"],
        "source_asset_id": str(artifact.asset_id) if verified["source_kind"] == "asset" else None,
        "run_plan_sha256": unit["run_plan_sha256"], "preparation_id": verified["preparation_id"],
        "request_sha256": verified["request_sha256"], "selection_revision": decision.revision if decision else 0,
        "selected_artifact_id": str(decision.artifact_id) if decision else None,
        "selection_decision_id": str(decision.pk) if decision else None}


def adopt_manual_media(*, run, stage, plan, request, actor):
    """The command already holds Run then Stage locks and the plan CAS.

    Adopting a candidate does not complete/approve the stage or dispatch its
    dependants. Asset adoption and generated candidates use explicit sources;
    neither impersonates the other's execution or archive evidence.
    """
    payload = request["payload"]
    if set(payload) != {"source"} or not isinstance(payload["source"], dict):
        raise ValidationError("请明确选择媒体来源：本轮候选或素材库素材")
    source = payload["source"]
    if stage.stage_key != "media_generation" or stage.mode != "manual":
        raise ValidationError("当前步骤不是人工媒体制作")
    if request["scope"]["kind"] not in STAGE_MEDIA_SCOPE_KINDS[stage.stage_key]:
        raise ValidationError("所选制作对象不适用于当前媒体制作步骤")
    if source.get("kind") == "asset":
        from studio.services.production_adopted_media import adopt_asset_media
        return adopt_asset_media(run=run, stage=stage, plan=plan, request=request, actor=actor)
    if set(source) != {"kind", "artifact_id", "expected_sha256", "expected_manifest_sha256"} or source["kind"] != "artifact":
        raise ValidationError("采用媒体须提供候选ID、文件摘要和清单摘要，不接受模型或槽位覆盖")
    try:
        artifact_id = UUID(source["artifact_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("请选择正式媒体候选") from exc
    artifact = GeneratedArtifact.objects.select_for_update().filter(pk=artifact_id,
        production_run=run, stage_run=stage).first()
    if artifact is None or artifact.sha256 != source["expected_sha256"] or artifact.manifest_metadata_sha256 != source["expected_manifest_sha256"]:
        raise ValidationError("所选候选不属于本轮步骤或内容已变化，请重新读取")
    detail = manual_media_candidate_detail(artifact)
    if detail is None or detail["scope"] != request["scope"] or detail["unit_key"] != request["unit_key"]:
        raise ValidationError("候选与明确选择的制作对象不一致，不能改派到其他镜头")
    if request["expected_selection_revision"] != detail["selection_revision"]:
        raise ValidationError("媒体采用修订已变化，请重新读取后再选择", code="manual_selection_conflict")
    if detail["selected_artifact_id"] == str(artifact.pk):
        return ManualActionResult("artifact_selection", UUID(detail["selection_decision_id"]))
    select_artifact(artifact, user=actor)
    decision = media_selection_state(run_id=run.pk, stage_id=stage.pk, group=artifact.variant_group)
    return ManualActionResult("artifact_selection", decision.pk)
