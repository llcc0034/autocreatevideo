"""Stable select-artifact receipts around the existing recipe selection writer.

The API authorizes access before calling this service and serializes the result
with ProductionManualActionResultSerializer. A receipt identifies a historical
decision; current selection must be read separately from the original Run.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError

from studio.models import ArtifactSelectionDecision, GeneratedArtifact, ProductionAssemblyLink, ProductionRun, StageRun
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract
from studio.services.production_manual_commands import (
    ManualActionResult, replay_manual_command, store_manual_command_result,
)
from studio.services.registry_reference_lock import registry_reference_write_guard


def _result(receipt):
    return {"id": receipt.pk, "action": receipt.action, "created_at": receipt.created_at,
        "result_kind": receipt.result_kind, "target_id": receipt.target_id}


def select_run_artifact_command(run, artifact, *, actor, request_key, notes="", material_selection=None):
    """Run lock -> exact replay -> original writer -> receipt -> one commit.

All accepted request fields, including notes and the material impact baseline,
are hashed with the operation, Run and actor. New keys represent new commands;
the original text/material writers retain their same-candidate semantics.
No automatic policy caller is routed through this human command wrapper.
"""
    from studio.services.production_runs import ProductionRunConflict, _StoredAssetFileGuard, select_run_artifact

    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    # The guard must outlive the transaction, including receipt save/commit.
    with _StoredAssetFileGuard(), registry_reference_write_guard():
        run = ProductionRun.objects.select_for_update().only(
            "id", "owner_id", "project_id", "plan_source", "status",
        ).get(pk=run.pk)
        if run.owner_id != actor.pk and not actor.is_staff:
            raise PermissionDenied("无权操作此制作运行")
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 191:
            raise ValidationError("选片必须提供长度为1至191的稳定请求键")
        if not isinstance(notes, str) or (material_selection is not None and not isinstance(material_selection, dict)):
            raise ValidationError("选片备注必须为文本，素材选择基线必须为对象")
        material_selection = deepcopy(material_selection)
        if material_selection is not None and isinstance(material_selection.get("expected_decision_id"), UUID):
            material_selection["expected_decision_id"] = str(material_selection["expected_decision_id"])
        requested = json_contract({"schema_version": 1, "operation": "select-artifact", "run_id": str(run.pk),
            "actor_id": str(actor.pk), "body": {"artifact_id": str(artifact.pk), "notes": notes,
                "material_selection": deepcopy(material_selection)}}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        try:
            previous = replay_manual_command(run=run, request_key=request_key,
                request_sha256=requested.sha256, actor=actor)
        except ValidationError as exc:
            if getattr(exc, "code", None) == "run_plan_source_conflict":
                raise ProductionRunConflict(exc.messages, code=exc.code) from exc
            raise
        if previous is not None:
            return _result(previous)
        if run.plan_source != "recipe":
            raise ValidationError("无配方制作请使用对应步骤的人工动作", code="run_plan_source_conflict")
        if run.status in {"succeeded", "failed", "cancelled", "cancel_requested"}:
            raise ValidationError("已结束或正在取消的运行不能新增选片命令")
        if ProductionAssemblyLink.objects.filter(production_run=run).exists():
            raise ValidationError("已提交装配并冻结选片；更换内容请新建制作轮次")
        artifact = GeneratedArtifact.objects.select_related("stage_run").filter(
            pk=artifact.pk, production_run=run).first()
        if artifact is None or artifact.stage_run_id is None:
            raise ValidationError("选片候选必须属于本轮的确切步骤")
        stage = StageRun.objects.select_for_update().get(pk=artifact.stage_run_id, production_run=run)
        selected = select_run_artifact(run, artifact, user=actor,
            material_selection=requested.value["body"]["material_selection"])
        # The Run lock covers every selection writer. This is the exact
        # decision returned/created by this call, never a later unlocked read.
        decision = ArtifactSelectionDecision.objects.filter(stage_run=stage,
            variant_group=selected.variant_group).order_by("-revision").first()
        if decision is None or decision.artifact_id != selected.pk:
            raise ValidationError("选片未返回本步骤的确切决定，不能记录成功回执")
        receipt = store_manual_command_result(run=run, stage=stage, action="select", request_key=request_key,
            request_sha256=requested.sha256, actor=actor, result=ManualActionResult("artifact_selection", decision.pk))
        return _result(receipt)
