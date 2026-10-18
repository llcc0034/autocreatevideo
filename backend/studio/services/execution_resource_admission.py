"""Current hard resource facts, separate from combination Smoke and capacity.

Only local database/configuration/credential checks; no probe or provider I/O.
An expired Smoke is a policy decision, not a missing file or disabled host.
"""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import HostCapabilitySnapshot, ModelArtifact, ModelExecutionInstanceVersion
from studio.services.execution_instances import ExecutionResourceSelection, execution_configuration_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError, driver_for, validate_deployment_execution_contract
from studio.services.runtime_platform import verified_artifact_availability, verify_host_snapshot


def read_execution_resource_state(version):
    """Caller supplies a freshly loaded, integrity-checked exact version."""
    if not isinstance(version, (ModelExecutionInstanceVersion, ExecutionResourceSelection)):
        raise ValidationError("资源准入需要精确执行版本或服务端资源选择")
    resource_smoke = isinstance(version, ExecutionResourceSelection)
    deployment, blockers = version.deployment, []
    model, provider = deployment.profile_version, deployment.provider

    def block(code, message):
        blockers.append({"code": code, "message": message})

    if not resource_smoke and (not version.instance.is_active or version.status != "published"):
        block("execution_instance_inactive", "执行实例已停用或版本未发布")
    if deployment.status != "active" or model.status != "published" or not provider.is_active:
        block("execution_resource_inactive", "模型部署、模型版本或服务连接不可用")
    if version.prompt_profile_version_id and version.prompt_profile_version.status != "published":
        block("execution_profile_inactive", "精确提示词策略版本已退役或未发布")
    workflow_states = {"validated", "published"} if resource_smoke else {"published"}
    if version.workflow_version_id and version.workflow_version.status not in workflow_states:
        block("execution_workflow_inactive", "精确工作流版本已退役或未发布")
    if resource_smoke and version.workflow_version_id:
        from studio.services.workflow_versions import verify_workflow_live_validation
        try:
            verify_workflow_live_validation(version.workflow_version, deployment)
        except ValidationError:
            block("execution_live_validation_invalid", "基础工作流的在线校验缺失或已变化，请先重新Live Validate")
    try:
        validate_deployment_execution_contract(deployment)
        driver_for(provider).validate_execution_credentials(deployment)
    except (ValidationError, ProviderDriverError):
        # Do not serialize exceptions from a credential/config backend. Their
        # raw text may contain internal addresses or credential service facts.
        block("execution_transport_unavailable", "当前执行连接或受管凭据不可用，请检查服务连接和部署配置")

    host, snapshot = deployment.runtime_host, None
    if host is not None:
        if host.status != "active":
            block("execution_host_unavailable", "绑定运行主机已停用或不可用")
        snapshot = HostCapabilitySnapshot.objects.filter(host_id=host.pk).order_by("-captured_at", "-revision").first()
        if snapshot is None:
            block("execution_host_evidence_missing", "绑定运行主机缺少探测事实")
        else:
            verify_host_snapshot(snapshot)
            if snapshot.probe_status == "failed":
                block("execution_host_probe_failed", "运行主机最近一次探测失败")
            if snapshot.expires_at is None or snapshot.expires_at <= timezone.now():
                block("execution_host_evidence_stale", "运行主机探测事实已过期或未声明有效期，请重新探测")

    artifact_facts = []
    if version.workflow_version_id:
        requirements = version.workflow_version.model_requirements.filter(is_required=True).select_related("artifact")
        artifacts = []
        for requirement in requirements:
            artifact = requirement.artifact
            if artifact is None or artifact.sha256 != requirement.expected_sha256 or artifact.filename != requirement.expected_filename:
                block("execution_model_requirement_invalid", "工作流必需模型文件未绑定或与冻结文件要求不一致")
            elif artifact.pk not in {row.pk for row in artifacts}:
                artifacts.append(artifact)
    else:
        artifacts = list(model.artifacts.all())
    for artifact in artifacts:
        fact = {"artifact_id": str(artifact.pk), "status": artifact.status, "availability_id": None}
        if artifact.profile_version_id != model.pk or artifact.status != ModelArtifact.Status.AVAILABLE:
            block("execution_model_file_unavailable", "必需模型文件不属于此模型版本或尚未验证可用")
        elif snapshot is None:
            block("execution_model_file_evidence_missing", "必需模型文件缺少精确运行主机的字节校验证据")
        else:
            try:
                observation = verified_artifact_availability(snapshot=snapshot, deployment=deployment, artifact=artifact)
                fact["availability_id"] = str(observation.pk)
            except ValidationError:
                block("execution_model_file_evidence_invalid", "模型文件缺少当前运行主机上一致的大小和摘要证据")
        artifact_facts.append(fact)
    from studio.services.execution_capacity_signatures import capacity_runtime_facts
    return json_contract({"schema_version": 1, "deployment_id": str(deployment.pk),
        "model_version_id": str(model.pk), "provider_id": str(provider.pk),
        "runtime_host_id": str(host.pk) if host else None,
        "host_snapshot_id": str(snapshot.pk) if snapshot else None,
        "model_files": artifact_facts, "blockers": blockers,
        "capacity_runtime": capacity_runtime_facts(version, snapshot)}, max_bytes=execution_configuration_max_bytes())
