"""Exact adopted production media as execution input, never a latest fallback.

The root business adapter supplies the real Run and eligible upstream stages.
This reader verifies the explicitly chosen Decision/Artifact and bytes; it does
not create an Asset, approve a stage, select a candidate or execute a model.
"""
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ArtifactSelectionDecision, GeneratedArtifact, ProductionRun, StageRun
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.generated_artifacts import generated_artifact_storage_owner_id, verified_generated_artifact_media
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.provenance import ProvenanceError
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class SelectedExecutionMedia:
    artifact: GeneratedArtifact
    media_facts: dict
    selection: dict
    path: Path


def _uuid(value, label):
    if not isinstance(value, str):
        raise ValidationError({label: "必须提供明确的正式 UUID"})
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValidationError({label: "必须提供明确的正式 UUID"}) from exc


def selected_output_item_key(*, input_key, decision_id, artifact_id, purpose):
    """Distinct uses survive repeated bytes/decisions and different purposes."""
    if not isinstance(input_key, str) or not input_key or purpose is not None and not isinstance(purpose, str):
        raise ValidationError("已采用产物使用必须有明确输入和用途")
    return "selected:" + json_contract({"input_key": input_key,
        "decision_id": _uuid(decision_id, "decision_id"), "artifact_id": _uuid(artifact_id, "artifact_id"),
        "purpose": purpose}, max_bytes=execution_configuration_max_bytes()).sha256


def _selected_execution_artifact(*, decision_id, artifact_id, owner, production_run_id, eligible_stage_ids):
    """Caller holds a transaction. Shared identity checks for media and text."""
    decision_id, artifact_id = _uuid(decision_id, "decision_id"), _uuid(artifact_id, "artifact_id")
    if not getattr(owner, "is_active", False) or not getattr(owner, "pk", None):
        raise PermissionDenied("需要有效的业务所有者")
    run_id = _uuid(production_run_id, "production_run_id")
    if not isinstance(eligible_stage_ids, (set, frozenset, list, tuple)):
        raise ValidationError("必须由正式运行计划明确提供可用上游阶段")
    eligible = {_uuid(str(value), "eligible_stage_ids") for value in eligible_stage_ids}
    run = ProductionRun.objects.select_for_update().select_related("project").filter(pk=run_id).first()
    if run is None or run.project.owner_id != owner.pk:
        raise PermissionDenied("上游产物不属于本次授权的制作运行")
    decision = ArtifactSelectionDecision.objects.filter(pk=decision_id, artifact_id=artifact_id).first()
    if decision is None or str(decision.stage_run_id) not in eligible:
        raise ValidationError("所选决定/产物不匹配，或不是运行计划允许的上游阶段")
    stage = StageRun.objects.select_for_update().filter(pk=decision.stage_run_id, production_run=run).first()
    if stage is None:
        raise ValidationError("所选决定不属于本轮上游步骤")
    manual_media = run.plan_source == "manual" and stage.stage_key == "media_generation"
    if manual_media:
        from studio.services.production_manual_media_review import manual_media_review_detail
        review = manual_media_review_detail(run)
        if review["approval"]["status"] != "current":
            raise ValidationError("人工媒体选片尚未审核，或选片、上游内容及审核依据已变化，请重新核对")
    elif stage.status != StageRun.Status.SUCCEEDED:
        raise ValidationError("上游阶段尚未完成，不能将候选或未批准输出当成已采用结果")
    artifact = GeneratedArtifact.objects.select_for_update(of=("self",)).select_related(
        "production_run__project", "generation_attempt", "generation_task", "asset",
    ).filter(pk=artifact_id).first()
    if (artifact is None or artifact.production_run_id != run.pk or artifact.stage_run_id != stage.pk
            or artifact.status != GeneratedArtifact.Status.SELECTED or artifact.variant_group != decision.variant_group):
        raise ValidationError("产物已不再是本次运行的有效采用结果")
    latest = ArtifactSelectionDecision.objects.filter(stage_run=stage, variant_group=decision.variant_group).order_by("-revision").first()
    if latest is None or latest.pk != decision.pk:
        raise ValidationError("选片决定已更新，请明确选择当前决定，不能静默替换来源")
    if artifact.generation_attempt_id and artifact.generation_attempt.stage_run_id != stage.pk:
        raise ValidationError("产物的真实生成尝试与上游阶段不一致")
    adopted = isinstance(artifact.manifest_metadata, dict) and artifact.manifest_metadata.get("source") == "adopted"
    if artifact.kind != GeneratedArtifact.Kind.ASSEMBLY and not adopted and not (artifact.generation_attempt_id or artifact.generation_task_id):
        raise ValidationError("上游生成产物缺少真实生成任务")
    for task in (artifact.generation_attempt, artifact.generation_task):
        if task is not None and task.status != "succeeded":
            raise ValidationError("上游生成任务没有明确成功，不能消费其产物")
    if not isinstance(artifact.manifest_metadata, dict):
        raise ValidationError("上游产物 manifest 必须为明确对象")
    verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
        size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
        max_bytes=execution_configuration_max_bytes())
    return run, stage, artifact, decision


def _selection_reference(run, stage, artifact, decision):
    return {
        "kind": "selected_output", "production_run_id": str(run.pk), "stage_run_id": str(stage.pk),
        "stage_key": stage.stage_key, "output_key": artifact.role, "variant_group": artifact.variant_group,
        "decision_id": str(decision.pk), "decision_revision": decision.revision,
        "artifact_id": str(artifact.pk), "manifest_sha256": artifact.manifest_metadata_sha256,
        "selection_mode": decision.selection_mode,
        "selected_by": str(decision.selected_by_id) if decision.selected_by_id is not None else None,
        "selected_at": decision.created_at.isoformat(),
    }


@transaction.atomic
@security_boundary_resolution_cache()
def resolve_selected_execution_media(*, decision_id, artifact_id, owner, production_run_id,
        eligible_stage_ids, expected_kind, max_media_bytes):
    """Run -> stage -> artifact locks; caller must use the common root lock order.

    eligible_stage_ids comes from the validated Run plan, never an HTTP bypass.
    Reading an old preparation does not call this function again.
    """
    run, stage, artifact, decision = _selected_execution_artifact(decision_id=decision_id,
        artifact_id=artifact_id, owner=owner, production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids)
    kind = "video" if artifact.kind == GeneratedArtifact.Kind.ASSEMBLY else artifact.kind
    if kind != expected_kind:
        raise ValidationError("上游产物类型与实例输入槽不一致")
    if run.plan_source == "manual" and stage.stage_key == "media_generation":
        from studio.services.execution_production_candidates import read_production_media_candidate
        verified = read_production_media_candidate(artifact, max_media_bytes=max_media_bytes)
        return SelectedExecutionMedia(artifact, verified["media_facts"], _selection_reference(run, stage, artifact, decision), verified["path"])
    try:
        # Current managed writers freeze tenant_owner_id. Do not use historical
        # actor-owned directory exceptions to admit new instance inputs.
        if (artifact.storage_backend == "managed_local" and artifact.generation_task_id is None
                and artifact.manifest_metadata.get("tenant_owner_id") != str(owner.pk)):
            raise ProvenanceError("上游产物缺少当前租户存储事实")
        if generated_artifact_storage_owner_id(artifact) != str(owner.pk):
            raise ProvenanceError("上游产物存储租户与业务所有者不一致")
        path, media = verified_generated_artifact_media(artifact, max_bytes=max_media_bytes, include_observed_media=True)
    except (ProvenanceError, OSError, ValueError) as exc:
        raise ValidationError({"selected_output": str(exc)}) from exc
    if artifact.asset_id and (artifact.asset.owner_id != owner.pk or artifact.asset.checksum != artifact.sha256):
        raise ValidationError("产物关联素材的租户或内容摘要不一致")
    return SelectedExecutionMedia(artifact, media, _selection_reference(run, stage, artifact, decision), path)
