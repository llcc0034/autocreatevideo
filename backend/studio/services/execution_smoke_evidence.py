"""Prepared evidence belongs to the existing Smoke attempt and its exact scope.

Only original worker completion may call the writer. There is no public
accept-result endpoint, new queue, current-profile lookup or business-success
promotion here. Readers check saved request identity, not today's target.
"""
from dataclasses import asdict
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import ExecutionPreparation, ModelDeployment, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt, WorkflowTemplateVersion
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.execution_admission import ExecutionEvidenceIdentity, ExecutionSmokeFact
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_task_links import validate_smoke_preparation_link
from studio.services.json_contracts import JsonContract, assign_json_contract, verify_json_contract
from studio.services.provider_registry import deployment_capability_ttl_seconds


def _kind(model):
    if model is ModelDeploymentSmokeAttempt:
        return "deployment_smoke_id"
    if model is WorkflowSmokeAttempt:
        return "workflow_smoke_id"
    raise ValidationError("组合证据必须来自正式部署或工作流烟测")


def instance_smoke_identity(version_id, definition):
    if version_id is None or definition.value["version_id"] != str(version_id):
        raise ValidationError("组合证据需要真实且一致的执行实例版本身份")
    return ExecutionEvidenceIdentity(str(version_id), definition.sha256, definition.value["target_fingerprint"],
        definition.value["configuration"]["operation"], definition.value["configuration"]["mode"], "instance_smoke")


def read_prepared_smoke_context(attempt, preparation):
    validate_smoke_preparation_link(attempt, preparation.execution_instance_version_id)
    purpose = "resource_smoke" if attempt.scope == "resource" else "instance_smoke"
    if preparation.purpose != purpose or getattr(preparation, _kind(type(attempt))) != attempt.pk:
        raise ValidationError("证据与验收准备的范围或来源不一致")
    detail, definition, transport = read_frozen_execution_request(preparation)
    if attempt.target_sha256 != definition.value["target_fingerprint"]:
        raise ValidationError("组合验收目标与冻结执行定义不一致")
    if isinstance(attempt, ModelDeploymentSmokeAttempt) and attempt.request_sha256 != preparation.request_sha256:
        raise ValidationError("组合验收的请求摘要与准备不一致")
    from studio.services.execution_auxiliary_requests import read_preparation_operation_field_request
    batch = read_preparation_operation_field_request(preparation, definition, detail)
    if transport.value["field_request_sha256"] != (batch.sha256 if batch else None):
        raise ValidationError("验收请求的字段批次与当时声明的夹具不一致")
    identity = (ExecutionEvidenceIdentity(None, definition.sha256, definition.value["target_fingerprint"],
        definition.value["configuration"]["operation"], definition.value["configuration"]["mode"], purpose)
        if purpose == "resource_smoke" else instance_smoke_identity(preparation.execution_instance_version_id, definition))
    return detail, identity


@registry_reference_writer
def finish_prepared_smoke(attempt, claim_token, *, status, evidence, code="", message="", actual_cost=None, cost_currency="", capacity_request=None):
    """Commit an actual worker outcome under the original target/claim locks."""
    model, owner_key = type(attempt), _kind(type(attempt))
    if claim_token is None or status not in {"succeeded", "failed", "cancelled", "uncertain"} or not isinstance(evidence, JsonContract):
        raise ValidationError("组合验收完成需要有效任务租约、终态和核对后的结果")
    verified = verify_json_contract(evidence.value, schema_version=evidence.schema_version, size_bytes=evidence.size_bytes,
        sha256=evidence.sha256, max_bytes=execution_configuration_max_bytes())
    if verified.schema_version != 1 or not isinstance(verified.value, dict):
        raise ValidationError("验收结果需要当前对象合同")
    from studio.services.execution_asset_budget import _money
    import re
    amount = _money(actual_cost)
    if amount is not None and (not isinstance(cost_currency, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", cost_currency)):
        raise ValidationError("实际结算必须记录明确币种，不推断或换算")
    with transaction.atomic():
        deployment = ModelDeployment.objects.select_for_update().get(pk=attempt.deployment_id)
        workflow = (WorkflowTemplateVersion.objects.select_for_update().get(pk=attempt.workflow_version_id)
            if model is WorkflowSmokeAttempt else None)
        locked = model.objects.select_for_update().get(pk=attempt.pk)
        if locked.claim_token != claim_token:
            return locked  # Late worker cannot append or replace evidence.
        if locked.scope not in {"resource", "instance"} or locked.finished_at is not None:
            raise ValidationError("不能重写已有终态或未知范围的证据")
        if locked.scope == "resource" and capacity_request is not None:
            raise ValidationError("基础资源烟测不能写入执行实例的容量证据")
        pre_submit_failure = locked.status == "queued" and status in {"failed", "cancelled"} and locked.submission_started_at is None
        if locked.status not in {"submitting", "submitted", "running", "cancel_requested"} and not pre_submit_failure:
            raise ValidationError("验收尚未进入远端提交，不能生成组合结果")
        preparation = ExecutionPreparation.objects.select_for_update().get(**{owner_key: locked.pk})
        detail, identity = read_prepared_smoke_context(locked, preparation)
        if status != "uncertain" and (locked.status == "cancel_requested" or preparation.status == "cancelled"):
            status = "cancelled"
        now = timezone.now()
        expires_at = now + timedelta(seconds=deployment_capability_ttl_seconds(deployment)) if status == "succeeded" else None
        assign_json_contract(locked, "result", {"schema_version": 1, "scope": preparation.purpose,
            "attempt_id": str(locked.pk), "attempt_kind": owner_key.removesuffix("_id"),
            "preparation_id": str(preparation.pk), "request_sha256": detail.prepared_request_sha256,
            "identity": asdict(identity), "status": status, "finished_at": now.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None, "evidence": verified.value},
            max_bytes=execution_configuration_max_bytes())
        locked.status, locked.finished_at, locked.evidence_expires_at = status, now, expires_at
        if amount is not None:
            locked.actual_cost, locked.cost_currency = amount, cost_currency
        locked.error_code, locked.error_message = str(code)[:100], str(message)[:2000]
        locked.heartbeat_at, locked.next_poll_at = now, None
        locked.claim_token, locked.worker_id, locked.claimed_at = None, "", None
        locked.save(update_fields=("status", "finished_at", "evidence_expires_at", "error_code", "error_message",
            "result", "result_schema_version", "result_size_bytes", "result_sha256", "heartbeat_at", "next_poll_at",
            "claim_token", "worker_id", "claimed_at", "actual_cost", "cost_currency", "updated_at"))
        if locked.status == "succeeded" and capacity_request is not None:
            from studio.services.execution_capacity_observations import record_completed_capacity_observation
            record_completed_capacity_observation(locked, capacity_request)
        if workflow is not None and locked.scope == "resource":
            from studio.services.execution_workflow_resource_evidence import project_completed_workflow_smoke
            project_completed_workflow_smoke(locked, workflow)
        elif model is ModelDeploymentSmokeAttempt and locked.scope == "resource":
            from studio.services.execution_deployment_resource_evidence import project_completed_deployment_smoke
            project_completed_deployment_smoke(locked, deployment)
        return locked


def instance_smoke_candidates(version, identity):
    """Indexed, narrow first read. No JSON or unrelated history on the hot path."""
    model = WorkflowSmokeAttempt if version.workflow_version_id else ModelDeploymentSmokeAttempt
    return model.objects.filter(scope="instance", execution_instance_version_id=version.pk,
        target_sha256=identity.target_fingerprint, status__in=("succeeded", "failed"), finished_at__isnull=False
        ).order_by("-finished_at", "-id").values("id", "status", "finished_at", "evidence_expires_at")


def read_instance_smoke_facts(version, definition):
    """Latest completed result for this exact combination, not last success."""
    identity = instance_smoke_identity(version.pk, definition)
    candidate = instance_smoke_candidates(version, identity).first()
    if candidate is None:
        return ()
    model = WorkflowSmokeAttempt if version.workflow_version_id else ModelDeploymentSmokeAttempt
    attempt = model.objects.get(pk=candidate["id"])
    fact = read_prepared_smoke_fact(attempt)
    if (fact.identity != identity or fact.status != candidate["status"] or fact.finished_at != candidate["finished_at"]
            or fact.expires_at != candidate["evidence_expires_at"]):
        raise ValidationError("组合证据正文、索引或请求身份不一致，不能采用或回退旧通过结果")
    return (fact,)


def read_prepared_smoke_fact(attempt):
    """Verify the exact saved outcome; the caller must apply its own scope gate."""
    model = type(attempt)
    preparation = ExecutionPreparation.objects.get(**{_kind(model): attempt.pk})
    detail, saved_identity = read_prepared_smoke_context(attempt, preparation)
    if attempt.finished_at is None or attempt.status not in {"succeeded", "failed", "cancelled", "uncertain"}:
        raise ValidationError("尚未完成的验收不能提供结果证据")
    result = verify_json_contract(attempt.result, schema_version=attempt.result_schema_version, size_bytes=attempt.result_size_bytes,
        sha256=attempt.result_sha256, max_bytes=execution_configuration_max_bytes()).value
    expected = {"schema_version": 1, "scope": preparation.purpose, "attempt_id": str(attempt.pk),
        "attempt_kind": _kind(model).removesuffix("_id"), "preparation_id": str(preparation.pk),
        "request_sha256": detail.prepared_request_sha256, "identity": asdict(saved_identity), "status": attempt.status,
        "finished_at": attempt.finished_at.isoformat(),
        "expires_at": attempt.evidence_expires_at.isoformat() if attempt.evidence_expires_at else None}
    if (not isinstance(result, dict) or set(result) != {*expected, "evidence"}
            or any(result[key] != value for key, value in expected.items()) or not isinstance(result["evidence"], dict)
            or (attempt.status == "succeeded" and (attempt.evidence_expires_at is None or attempt.evidence_expires_at <= attempt.finished_at))
            or (attempt.status != "succeeded" and attempt.evidence_expires_at is not None)):
        raise ValidationError("验收证据正文、索引或请求身份不一致，不能采用或回退旧通过结果")
    return ExecutionSmokeFact(saved_identity, str(attempt.pk), attempt.status, attempt.finished_at, attempt.evidence_expires_at)
