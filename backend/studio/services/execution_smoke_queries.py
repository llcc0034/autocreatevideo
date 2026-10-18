"""Read original Smoke tasks for one exact execution version, without I/O.

History is a narrow task projection, not a replacement evidence store. Only
the verified latest completed combination result is the evidence summary;
task completion and configuration publication are not production admission.
"""
from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_smoke_evidence import instance_smoke_identity, read_instance_smoke_facts
from studio.services.security_boundaries import security_boundary_resolution_cache


def instance_smoke_history(version, *, status=None):
    model = WorkflowSmokeAttempt if version.workflow_version_id else ModelDeploymentSmokeAttempt
    queryset = model.objects.filter(scope="instance", execution_instance_version_id=version.pk)
    if status is not None:
        if status not in dict(model.Status.choices):
            raise ValidationError({"status": "此验收执行器没有该任务状态"})
        queryset = queryset.filter(status=status)
    return queryset.order_by("-created_at", "-id").values(
        "id", "status", "created_at", "updated_at", "submission_started_at", "finished_at", "evidence_expires_at",
        "target_sha256", "error_code", "estimated_cost", "actual_cost", "cost_currency",
        "execution_preparations__id", "execution_preparations__status", "execution_preparations__revision",
        "execution_preparations__purpose", "execution_preparations__execution_instance_version_id",
        "execution_preparations__request_sha256", "execution_preparations__reason_code",
    )


def project_instance_smoke_history(version, rows):
    kind = "workflow_smoke" if version.workflow_version_id else "deployment_smoke"
    result = []
    for row in rows:
        if (row["execution_preparations__id"] is None
                or row["execution_preparations__purpose"] != "instance_smoke"
                or row["execution_preparations__execution_instance_version_id"] != version.pk):
            raise ValidationError("组合验收任务缺少一致的准备关联，不能补造或隐藏记录")
        result.append({"attempt_id": row["id"], "attempt_kind": kind,
            **{key: row[key] for key in ("status", "created_at", "updated_at", "submission_started_at", "finished_at",
                "evidence_expires_at", "target_sha256", "error_code", "estimated_cost", "actual_cost", "cost_currency")},
            "preparation_id": row["execution_preparations__id"], "preparation_status": row["execution_preparations__status"],
            "preparation_revision": row["execution_preparations__revision"],
            "request_sha256": row["execution_preparations__request_sha256"],
            "preparation_reason_code": row["execution_preparations__reason_code"]})
    return result


@security_boundary_resolution_cache()
def instance_smoke_summary(version):
    definition = verify_stored_execution_definition(stored_execution_contract(version.pk))
    identity = instance_smoke_identity(version.pk, definition)
    facts = read_instance_smoke_facts(version, definition)
    now, policies = timezone.now(), read_execution_policies()
    fact = facts[0] if facts else None  # Reader validates latest completion, never last success.
    state = "missing" if fact is None else "failed" if fact.status == "failed" else "fresh" if fact.expires_at > now else "expired"
    configuration = definition.value["configuration"]
    return {"identity": asdict(identity), "read_at": now, "evidence_state": state,
        "latest_completed_attempt_id": fact.attempt_id if fact else None,
        "finished_at": fact.finished_at if fact else None, "expires_at": fact.expires_at if fact else None,
        "deployment_id": configuration["deployment_id"], "workflow_version_id": configuration["workflow_version_id"],
        "prompt_mode": configuration["prompt_mode"], "prompt_profile_version_id": configuration["prompt_profile_version_id"],
        "policies": policies.configuration.value}
