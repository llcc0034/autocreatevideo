"""Local, read-only identity of an execution target; not an admission verdict.

Read current database references rather than cached Django related objects.
The enclosing registry writer supplies the transaction for publication. This
service neither invokes a provider nor decrypts credentials, and only exposes
configuration/file digests in the derived identity.
"""
from django.core.exceptions import ValidationError

from studio.models import ModelArtifact, ModelDeployment, WorkflowTemplateVersion
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.provider_registry import (
    credential_binding_sha256, deployment_execution_config_contract,
)
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_versions import verify_workflow_version_integrity


_ARTIFACT_IDENTITY_FIELDS = (
    "role", "logical_name", "filename", "storage_uri", "inventory_folder",
    "inventory_node_type", "inventory_input_name", "sha256", "size_bytes",
)


@security_boundary_resolution_cache()
def execution_target_contract(*, deployment_id, workflow_version_id=None):
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider__credential", "credential",
    ).get(pk=deployment_id)
    artifacts = list(deployment.profile_version.artifacts.order_by("role", "logical_name").values(*_ARTIFACT_IDENTITY_FIELDS))
    workflow = None
    if workflow_version_id is not None:
        workflow = WorkflowTemplateVersion.objects.select_related(
            "model_profile_version", "provider",
        ).get(pk=workflow_version_id)
    return _execution_target_contract(deployment, artifacts, workflow=workflow)


@security_boundary_resolution_cache()
def execution_target_contracts(*, deployment_ids):
    """Read one page of current deployment identities without per-row queries.

    Invalid or concurrently removed resources remain explicit per-resource
    errors. No cached model objects or historical identity are accepted.
    """
    ids = set(deployment_ids)
    deployments = list(ModelDeployment.objects.filter(pk__in=ids).select_related(
        "profile_version__provider__credential", "credential",
    ))
    artifacts_by_model = {}
    for artifact in ModelArtifact.objects.filter(
        profile_version_id__in={row.profile_version_id for row in deployments},
    ).order_by("role", "logical_name").values("profile_version_id", *_ARTIFACT_IDENTITY_FIELDS):
        model_id = artifact.pop("profile_version_id")
        artifacts_by_model.setdefault(model_id, []).append(artifact)
    results = {str(pk): ValidationError("执行目标已不存在") for pk in ids}
    for deployment in deployments:
        try:
            result = _execution_target_contract(deployment, artifacts_by_model.get(deployment.profile_version_id, []))
        except (ValidationError, RuntimeError, TypeError, ValueError) as exc:
            result = exc
        results[str(deployment.pk)] = result
    return results


def _execution_target_contract(deployment, artifacts, *, workflow=None):
    """Shared identity compiler; callers load current rows before entering."""
    model = deployment.profile_version
    verified_model_profile_specification(model)
    configuration = deployment_execution_config_contract(deployment)
    # Paths/URIs are execution facts but must not leak into registry summaries.
    files = json_contract(artifacts, max_bytes=execution_configuration_max_bytes())
    workflow_identity = None
    if workflow is not None:
        if workflow.model_profile_version_id != model.pk or workflow.provider_id != model.provider_id:
            raise ValidationError({"workflow_version": "执行目标的工作流与精确模型或服务连接不一致"})
        verify_workflow_version_integrity(workflow)
        workflow_identity = {
            "id": str(workflow.pk), "execution_contract_sha256": workflow.execution_contract_sha256,
            "compiler_id": workflow.compiler_id, "compiler_version": workflow.compiler_version,
        }
    return json_contract({
        "schema_version": 1,
        "deployment_id": str(deployment.pk), "provider_id": str(model.provider_id),
        "driver_type": deployment.provider.driver_type,
        "runtime_host_id": str(deployment.runtime_host_id) if deployment.runtime_host_id else None,
        "configuration_sha256": configuration.sha256,
        "credential_reference_sha256": credential_binding_sha256(deployment),
        "model": {
            "id": str(model.pk), "specification_sha256": model.specification_sha256,
            "identifier": model.model_identifier, "modality": model.modality,
            "artifacts_sha256": files.sha256,
        },
        "workflow": workflow_identity,
    }, max_bytes=execution_configuration_max_bytes())
