"""Workflow base acceptance is a verified reference to its original Smoke.

The root result and Preparation remain authoritative. The Workflow stores only
their identities for its lifecycle/read model, never another output snapshot.
Instance combination results cannot enter this projection.
"""
import hashlib
import re

from django.core.exceptions import ValidationError

from studio.models import ExecutionPreparation, WorkflowSmokeAttempt
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_smoke_evidence import read_prepared_smoke_fact
from studio.services.json_contracts import json_contract


def same_workflow_validation(attempt, version):
    return (attempt.workflow_version_id == version.pk
        and attempt.deployment_id == version.validated_deployment_id
        and attempt.graph_sha256 == version.graph_sha256
        and attempt.execution_contract_sha256 == version.execution_contract_sha256
        and attempt.validation_evidence_sha256 == version.validation_evidence_sha256)


def resource_workflow_smoke_projection(attempt, version):
    """Check saved identities, not today's config or a recomputed result."""
    if (not isinstance(attempt, WorkflowSmokeAttempt) or attempt.scope != "resource"
            or attempt.execution_instance_version_id is not None or not same_workflow_validation(attempt, version)):
        raise ValidationError("基础工作流证据不属于此次在线校验，不能用实例组合结果替代")
    from studio.services.workflow_versions import _evidence_digest
    validation = version.validation_evidence
    if (not isinstance(validation, dict) or _evidence_digest(validation) != attempt.validation_evidence_sha256
            or not isinstance(validation.get("execution_target"), dict)
            or _evidence_digest(validation["execution_target"]) != validation.get("execution_target_sha256")):
        raise ValidationError("基础工作流证据缺少当时完整的在线校验目标")
    fact = read_prepared_smoke_fact(attempt)
    preparation = ExecutionPreparation.objects.get(workflow_smoke=attempt)
    _, definition, transport = read_frozen_execution_request(preparation)
    if fact.identity.scope != "resource_smoke" or fact.identity.version_id is not None:
        raise ValidationError("基础工作流证据的范围或执行实例身份不一致")
    if fact.status == "succeeded":
        evidence = attempt.result["evidence"]
        compiled = transport.value["payload"]["compiled_workflow"]
        artifacts = evidence.get("artifact_facts")
        receipts = json_contract(attempt.staged_inputs, max_bytes=execution_configuration_max_bytes())
        if (evidence.get("execution_submitted") is not True or attempt.submission_started_at is None
                or attempt.provider_request_id != str(preparation.pk)
                or evidence.get("request_id_sha256") != hashlib.sha256(attempt.provider_request_id.encode()).hexdigest()
                or evidence.get("graph_sha256") != compiled["graph_sha256"]
                or evidence.get("staged_inputs_sha256") != receipts.sha256
                or type(evidence.get("staged_input_count")) is not int
                or evidence["staged_input_count"] != len(receipts.value)
                or not isinstance(artifacts, list) or len(artifacts) != 1
                or not isinstance(artifacts[0], dict)
                or artifacts[0].get("kind") != compiled["output_contract"]["primary"]["kind"]
                or not isinstance(artifacts[0].get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", artifacts[0]["sha256"])):
            raise ValidationError("基础工作流的提交、编译图、素材或产物证据不完整")
    return {"schema_version": 2, "scope": "resource_smoke", "smoke_attempt_id": str(attempt.pk),
        "preparation_id": str(preparation.pk), "result_sha256": attempt.result_sha256,
        "request_sha256": preparation.request_sha256, "definition_sha256": definition.sha256,
        "deployment_id": str(attempt.deployment_id), "graph_sha256": attempt.graph_sha256,
        "execution_contract_sha256": attempt.execution_contract_sha256,
        "validation_evidence_sha256": attempt.validation_evidence_sha256,
        "execution_target_sha256": validation["execution_target_sha256"],
        "prepared_target_sha256": fact.identity.target_fingerprint}


def project_completed_workflow_smoke(attempt, version):
    """Called only by original worker completion with Deployment/Workflow locks.

    A late result is historical evidence for its old validation generation; it
    must not overwrite a newer Live Validate's current state.
    """
    if attempt.scope != "resource" or not same_workflow_validation(attempt, version):
        return
    from studio.services.workflow_versions import _evidence_digest
    projection = resource_workflow_smoke_projection(attempt, version)
    version.smoke_test_state = "passed" if attempt.status == "succeeded" else "failed"
    version.smoke_test_message = ("真实基础烟测已完成并核验声明的产物；有效期见原任务。"
        if attempt.status == "succeeded" else f"基础烟测未通过（{attempt.status}），请查看原任务。")
    version.smoke_test_evidence, version.smoke_test_evidence_sha256 = projection, _evidence_digest(projection)
    version.save(update_fields=("smoke_test_state", "smoke_test_message", "smoke_test_evidence",
        "smoke_test_evidence_sha256", "updated_at"))
