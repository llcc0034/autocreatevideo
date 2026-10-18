"""Resource health probes and shared media limits.

Smoke execution belongs to the original Preparation worker and result writer;
this module does not build or invoke operation-specific business samples.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import ModelDeployment, ModelDeploymentCapabilitySnapshot, ProviderProfile
from studio.services.async_media_contract import deployment_target_sha256, validate_async_model_profile
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError, credential_binding_sha256, driver_for
from studio.services.security_boundaries import boundary_int, boundary_number


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")




def async_media_smoke_max_poll_seconds() -> int:
    return boundary_int("security.provider.smoke_max_poll_seconds")


def async_media_smoke_max_poll_interval_seconds() -> float:
    return boundary_number(
        "security.provider.smoke_max_poll_interval_seconds",
        minimum=0.001,
    )


def _smoke_artifact_max_bytes(output_kind: str) -> int:
    key = (
        "security.provider.smoke_image_max_bytes"
        if output_kind == "image"
        else "security.provider.smoke_video_max_bytes"
    )
    return boundary_int(key)


def smoke_artifact_max_bytes(output_kind: str) -> int:
    return _smoke_artifact_max_bytes(output_kind)




@transaction.atomic
def record_deployment_capability_snapshot(
    deployment: ModelDeployment,
    *,
    probe_kind: str,
    status: str,
    operations: list[str],
    evidence: dict,
    expected_target_sha256: str | None = None,
) -> ModelDeploymentCapabilitySnapshot:
    if probe_kind != ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH:
        raise ValidationError("此入口仅记录健康探测；烟测证据必须来自原始执行准备与真实结果")
    if evidence.get("execution_submitted") is not False or evidence.get("prompt_profile"):
        raise ValidationError("健康探测不能声明模型调用或提示词策略烟测")
    locked = ModelDeployment.objects.select_for_update().get(pk=deployment.pk)
    current_target_sha256 = deployment_target_sha256(locked)
    if expected_target_sha256 and current_target_sha256 != expected_target_sha256:
        status = ModelDeploymentCapabilitySnapshot.Status.FAILED
        evidence = {
            **evidence,
            "schema_version": 1,
            "execution_submitted": bool(evidence.get("execution_submitted")),
            "error_code": "target_changed_during_probe",
            "probed_target_sha256": expected_target_sha256,
            "current_target_sha256": current_target_sha256,
        }
    revision = (
        locked.capability_snapshots.aggregate(value=Max("revision"))["value"] or 0
    ) + 1
    operations_contract = json_contract(
        operations,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    evidence_contract = json_contract(
        evidence,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    from studio.services.provider_registry import deployment_capability_ttl_seconds
    ttl = deployment_capability_ttl_seconds(locked)
    captured = timezone.now()
    return ModelDeploymentCapabilitySnapshot.objects.create(
        deployment=locked,
        revision=revision,
        probe_kind=probe_kind,
        status=status,
        target_sha256=current_target_sha256,
        provider_config_sha256=locked.provider.driver_config_sha256,
        deployment_config_sha256=locked.runtime_config_sha256,
        credential_binding_sha256=credential_binding_sha256(locked),
        operations=operations_contract.value,
        operations_schema_version=operations_contract.schema_version,
        operations_size_bytes=operations_contract.size_bytes,
        operations_sha256=operations_contract.sha256,
        evidence=evidence_contract.value,
        evidence_schema_version=evidence_contract.schema_version,
        evidence_size_bytes=evidence_contract.size_bytes,
        evidence_sha256=evidence_contract.sha256,
        captured_at=captured,
        expires_at=captured + timedelta(seconds=ttl),
    )


def probe_async_deployment(
    deployment: ModelDeployment,
) -> ModelDeploymentCapabilitySnapshot:
    deployment = ModelDeployment.objects.select_related("profile_version__provider").get(pk=deployment.pk)
    expected_target_sha256 = deployment_target_sha256(deployment)
    if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("该 probe 仅支持 async_rest deployment")
    operations = validate_async_model_profile(deployment.profile_version)
    driver = driver_for(deployment.provider)
    try:
        health = driver.invoke(deployment, "health", {"schema_version": 1})
        evidence = {
            "schema_version": 1,
            "execution_submitted": False,
            "health_response_sha256": str(health.get("response_sha256") or ""),
        }
        status = ModelDeploymentCapabilitySnapshot.Status.PASSED
    except ProviderDriverError as exc:
        evidence = {
            "schema_version": 1,
            "execution_submitted": False,
            "error_code": exc.code,
        }
        status = ModelDeploymentCapabilitySnapshot.Status.FAILED
    return record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
        status=status,
        operations=operations,
        evidence=evidence,
        expected_target_sha256=expected_target_sha256,
    )
