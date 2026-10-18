"""Original deployment capability snapshots reference prepared resource results.

The snapshot is a read model for the original result, not a second certificate.
Its configuration columns describe the observation at completion; admission
checks the frozen target/definition on the referenced Preparation instead.
"""
import re

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils import timezone

from studio.models import ExecutionPreparation, ModelDeploymentCapabilitySnapshot, ModelDeploymentSmokeAttempt
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
from studio.services.execution_smoke_evidence import read_prepared_smoke_fact
from studio.services.json_contracts import assign_json_contract, verify_json_contract


def resource_deployment_smoke_projection(attempt):
    if (not isinstance(attempt, ModelDeploymentSmokeAttempt) or attempt.scope != "resource"
            or attempt.execution_instance_version_id is not None):
        raise ValidationError("基础部署证据只能引用原资源准备，不接受实例组合证据")
    definition = read_resource_smoke_definition(attempt)
    fact = read_prepared_smoke_fact(attempt)
    prep = ExecutionPreparation.objects.get(deployment_smoke=attempt)
    evidence = attempt.result["evidence"]
    if fact.status not in {"succeeded", "failed"}:
        raise ValidationError("未确定或取消的任务不能记为通过或失败快照")
    if fact.status == "succeeded":
        if evidence.get("execution_submitted") is not True or attempt.submission_started_at is None:
            raise ValidationError("基础部署通过证据缺少实际提交事实")
        if definition.value["target"]["driver_type"] == "async_rest":
            artifacts = evidence.get("artifact_facts")
            if (not isinstance(artifacts, list) or not artifacts
                    or any(not isinstance(row, dict) or not isinstance(row.get("sha256"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) for row in artifacts)):
                raise ValidationError("基础媒体烟测缺少已核验的产物事实")
        elif (not isinstance(evidence.get("response_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence["response_sha256"])
                or type(evidence.get("response_size_bytes")) is not int or evidence["response_size_bytes"] <= 0
                or not isinstance(evidence.get("result"), dict)):
            raise ValidationError("基础文本烟测缺少已核验的响应事实")
    return {"schema_version": 2, "scope": "resource_smoke", "smoke_attempt_id": str(attempt.pk),
        "preparation_id": str(prep.pk), "result_sha256": attempt.result_sha256,
        "request_sha256": prep.request_sha256, "definition_sha256": definition.sha256,
        "operation": fact.identity.operation, "mode": fact.identity.mode,
        "prompt_mode": definition.value["configuration"]["prompt_mode"],
        "prompt_profile": definition.value["resources"].get("prompt_profile")}


def project_completed_deployment_smoke(attempt, deployment):
    """Original worker holds the Deployment/Attempt locks; no public writer."""
    if attempt.scope != "resource" or attempt.status not in {"succeeded", "failed"}:
        return
    if attempt.deployment_id != deployment.pk or attempt.final_snapshot_id is not None:
        raise ValidationError("基础结果已投影或部署身份不一致，不能重复创建证据")
    evidence = resource_deployment_smoke_projection(attempt)
    from studio.services.provider_registry import credential_binding_sha256
    # This is an observed configuration record, not a replacement of the saved
    # execution target. A later config edit cannot make the old request fresh.
    snapshot = ModelDeploymentCapabilitySnapshot(deployment=deployment,
        revision=(deployment.capability_snapshots.aggregate(value=Max("revision"))["value"] or 0) + 1,
        probe_kind="smoke", status="passed" if attempt.status == "succeeded" else "failed",
        target_sha256=attempt.target_sha256, provider_config_sha256=deployment.provider.driver_config_sha256,
        deployment_config_sha256=deployment.runtime_config_sha256, credential_binding_sha256=credential_binding_sha256(deployment),
        prompt_profile_version_id=attempt.prompt_profile_version_id, prompt_profile_contract_sha256=attempt.prompt_profile_contract_sha256,
        captured_at=attempt.finished_at, expires_at=attempt.evidence_expires_at or attempt.finished_at)
    assign_json_contract(snapshot, "operations", [attempt.operation], max_bytes=execution_configuration_max_bytes())
    assign_json_contract(snapshot, "evidence", evidence, schema_version=2, max_bytes=execution_configuration_max_bytes())
    snapshot.save()
    attempt.final_snapshot = snapshot
    attempt.save(update_fields=("final_snapshot", "updated_at"))


def read_resource_deployment_snapshot(snapshot, *, current=False):
    """Verify one exact projection; no old snapshot/Binding parser or fallback."""
    if snapshot.probe_kind != "smoke" or snapshot.evidence_schema_version != 2:
        raise ValidationError("基础烟测快照不符合当前准备协议，请重新验收")
    evidence = verify_json_contract(snapshot.evidence, schema_version=2, size_bytes=snapshot.evidence_size_bytes,
        sha256=snapshot.evidence_sha256, max_bytes=execution_configuration_max_bytes()).value
    operations = verify_json_contract(snapshot.operations, schema_version=snapshot.operations_schema_version,
        size_bytes=snapshot.operations_size_bytes, sha256=snapshot.operations_sha256,
        max_bytes=execution_configuration_max_bytes()).value
    try:
        attempt = snapshot.smoke_attempt
    except ModelDeploymentSmokeAttempt.DoesNotExist as exc:
        raise ValidationError("基础快照没有对应的原烟测任务") from exc
    expected = resource_deployment_smoke_projection(attempt)
    if (evidence != expected or operations != [attempt.operation] or snapshot.deployment_id != attempt.deployment_id
            or snapshot.target_sha256 != attempt.target_sha256
            or snapshot.status != ("passed" if attempt.status == "succeeded" else "failed")
            or snapshot.captured_at != attempt.finished_at or snapshot.expires_at != (attempt.evidence_expires_at or attempt.finished_at)
            or snapshot.prompt_profile_version_id != attempt.prompt_profile_version_id
            or snapshot.prompt_profile_contract_sha256 != attempt.prompt_profile_contract_sha256):
        raise ValidationError("基础快照与原准备、结果或到期时间不一致")
    if current:
        read_resource_smoke_definition(attempt, current=True)
    return attempt


def resource_smoke_profile_summary(attempt):
    """Display the exact selected version, without looking for a Binding."""
    definition = read_resource_smoke_definition(attempt)
    profile = definition.value["resources"].get("prompt_profile")
    if profile is None:
        return None
    row = attempt.prompt_profile_version
    if row is None or str(row.pk) != profile["id"]:
        raise ValidationError("基础烟测所选策略版本已不存在")
    return {**profile, "name": row.name, "version": row.version,
        "execution_mode": definition.value["configuration"]["mode"]}


def current_resource_smoke(deployment, operation):
    """Resource recommendation only; never certify an execution combination.

    Select the original latest completed outcome before checking success or
    expiry. A newer failure/uncertainty cannot reveal an earlier passing row.
    Configuration drift and damaged facts are explicit validation failures.
    """
    row = ModelDeploymentSmokeAttempt.objects.filter(deployment=deployment, scope="resource", operation=operation,
        status__in=("succeeded", "failed", "uncertain"), finished_at__isnull=False).order_by("-finished_at", "-id").values(
            "id", "status", "evidence_expires_at", "final_snapshot_id").first()
    if row is None or row["status"] != "succeeded":
        return None
    if row["evidence_expires_at"] is None or row["final_snapshot_id"] is None:
        raise ValidationError("基础烟测通过记录缺少原结果或有效期，不能推荐为可用资源")
    try:
        snapshot = ModelDeploymentCapabilitySnapshot.objects.get(pk=row["final_snapshot_id"])
    except ModelDeploymentCapabilitySnapshot.DoesNotExist as exc:
        raise ValidationError("基础烟测原结果已不存在，不能推荐为可用资源") from exc
    attempt = read_resource_deployment_snapshot(snapshot, current=True)
    if attempt.pk != row["id"] or attempt.operation != operation or attempt.deployment_id != deployment.pk:
        raise ValidationError("基础烟测结果与所查能力不一致，不能采用其他任务的通过记录")
    return attempt if attempt.evidence_expires_at > timezone.now() else None
