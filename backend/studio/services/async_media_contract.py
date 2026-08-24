from __future__ import annotations

from copy import copy
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.provider_registry import (
    credential_binding_sha256,
    credential_material_sha256,
    driver_for,
)


ASYNC_MEDIA_OPERATIONS = frozenset({"image.text_to_image", "video.text_to_video"})
STANDARD_MEDIA_FIELDS = frozenset({
    "model", "prompt", "negative_prompt", "width", "height",
    "duration_seconds", "fps", "seed", "audio",
})
_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float, Decimal),
    "boolean": bool,
}


def _validate_field(name: str, value: Any, rule: dict[str, Any]) -> None:
    value_type = rule.get("type")
    expected = _TYPE_MAP.get(value_type)
    if expected is None:
        raise ValidationError(f"async_media request_schema.{name}.type 不受支持")
    if value_type in {"integer", "number"} and isinstance(value, bool):
        raise ValidationError(f"async_media smoke_inputs.{name} 类型无效")
    if not isinstance(value, expected):
        raise ValidationError(f"async_media smoke_inputs.{name} 类型无效")
    if isinstance(value, str):
        minimum = int(rule.get("minLength", 0))
        maximum = int(rule.get("maxLength", 60000))
        if not minimum <= len(value) <= maximum:
            raise ValidationError(f"async_media smoke_inputs.{name} 长度越界")
    if value_type in {"integer", "number"}:
        if "minimum" in rule and value < rule["minimum"]:
            raise ValidationError(f"async_media smoke_inputs.{name} 小于 minimum")
        if "maximum" in rule and value > rule["maximum"]:
            raise ValidationError(f"async_media smoke_inputs.{name} 大于 maximum")
    if "enum" in rule and value not in rule["enum"]:
        raise ValidationError(f"async_media smoke_inputs.{name} 不在 enum 中")


def async_operation_contract(
    profile: ModelProfileVersion,
    operation: str,
) -> dict[str, Any]:
    verify_json_contract(
        profile.specification,
        schema_version=profile.specification_schema_version,
        size_bytes=profile.specification_size_bytes,
        sha256=profile.specification_sha256,
    )
    if operation not in ASYNC_MEDIA_OPERATIONS:
        raise ValidationError("async_rest 首版只支持文生图和文生视频")
    specification = profile.specification
    if not isinstance(specification, dict) or specification.get("schema_version") != 1:
        raise ValidationError("async_rest model specification.schema_version 必须为 1")
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list) or operation not in capabilities:
        raise ValidationError(f"model specification 未声明 {operation}")
    async_media = specification.get("async_media")
    if not isinstance(async_media, dict) or set(async_media) != {"schema_version", "operations"}:
        raise ValidationError("model specification.async_media 结构无效")
    if async_media.get("schema_version") != 1 or not isinstance(async_media.get("operations"), dict):
        raise ValidationError("model specification.async_media schema 无效")
    contract = async_media["operations"].get(operation)
    if not isinstance(contract, dict) or set(contract) != {
        "output_kind", "request_schema", "smoke_inputs",
    }:
        raise ValidationError(f"async_media.operations.{operation} 结构无效")
    expected_kind = "image" if operation == "image.text_to_image" else "video"
    if contract.get("output_kind") != expected_kind:
        raise ValidationError(f"{operation} output_kind 必须为 {expected_kind}")
    schema = contract.get("request_schema")
    if (
        not isinstance(schema, dict)
        or set(schema) != {"type", "additionalProperties", "required", "properties"}
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(schema.get("required"), list)
        or not isinstance(schema.get("properties"), dict)
    ):
        raise ValidationError(f"{operation} request_schema 必须是封闭对象 Schema")
    properties = schema["properties"]
    if not properties or not set(properties) <= STANDARD_MEDIA_FIELDS:
        raise ValidationError(f"{operation} request_schema 含未知标准语义")
    required = schema["required"]
    if len(required) != len(set(required)) or not set(required) <= set(properties):
        raise ValidationError(f"{operation} request_schema.required 无效")
    if "prompt" not in required:
        raise ValidationError(f"{operation} 必须要求 prompt")
    for name, rule in properties.items():
        if not isinstance(rule, dict):
            raise ValidationError(f"{operation}.{name} Schema 必须是对象")
        allowed = {"type", "minimum", "maximum", "minLength", "maxLength", "enum"}
        if set(rule) - allowed:
            raise ValidationError(f"{operation}.{name} Schema 含不支持关键字")
        if rule.get("type") not in _TYPE_MAP:
            raise ValidationError(f"{operation}.{name}.type 不受支持")
    smoke_inputs = contract.get("smoke_inputs")
    if not isinstance(smoke_inputs, dict) or set(smoke_inputs) - set(properties):
        raise ValidationError(f"{operation} smoke_inputs 含未知字段")
    if any(name not in smoke_inputs for name in required):
        raise ValidationError(f"{operation} smoke_inputs 缺少必填字段")
    for name, value in smoke_inputs.items():
        _validate_field(name, value, properties[name])
    return contract


def validate_async_model_profile(profile: ModelProfileVersion) -> list[str]:
    if profile.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        return []
    capabilities = profile.specification.get("capabilities") if isinstance(profile.specification, dict) else None
    declared = [item for item in (capabilities or []) if item in ASYNC_MEDIA_OPERATIONS]
    if not declared:
        raise ValidationError("async_rest model 必须声明至少一个首版媒体能力")
    for operation in declared:
        async_operation_contract(profile, operation)
    driver = driver_for(profile.provider)
    protocol = set((driver.capabilities() or {}).get("operations") or [])
    if not {"health", "submit", "poll", "cancel", "collect"} <= protocol:
        raise ValidationError("async_rest driver 缺少完整异步执行协议")
    return sorted(declared)


def validate_standard_payload(
    contract: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    schema = contract["request_schema"]
    properties = schema["properties"]
    unknown = sorted(set(payload) - (set(properties) | {"model", "idempotency_key", "schema_version"}))
    if unknown:
        raise ValidationError(f"媒体请求含未声明字段: {', '.join(unknown)}")
    for name in schema["required"]:
        if name not in payload:
            raise ValidationError(f"媒体请求缺少必填字段: {name}")
    for name, value in payload.items():
        if name in properties:
            _validate_field(name, value, properties[name])
    return payload


def deployment_target_payload(deployment: ModelDeployment) -> dict[str, Any]:
    provider = deployment.profile_version.provider
    return {
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "deployment_status": deployment.status,
        "provider_id": str(provider.id),
        "provider_driver_type": provider.driver_type,
        "provider_driver_contract_version": 1,
        "provider_active": provider.is_active,
        "provider_config_sha256": provider.driver_config_sha256,
        "deployment_config_sha256": deployment.runtime_config_sha256,
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "credential_material_sha256": credential_material_sha256(deployment),
        "profile_version_id": str(deployment.profile_version_id),
        "profile_status": deployment.profile_version.status,
        "specification_sha256": deployment.profile_version.specification_sha256,
        "model_identifier": deployment.profile_version.model_identifier,
    }


def deployment_target_sha256(deployment: ModelDeployment) -> str:
    return json_contract(deployment_target_payload(deployment)).sha256


def fresh_smoke_snapshot(
    deployment: ModelDeployment,
    operation: str,
) -> ModelDeploymentCapabilitySnapshot | None:
    target = deployment_target_sha256(deployment)
    candidates = deployment.capability_snapshots.filter(
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        target_sha256=target,
        expires_at__gt=timezone.now(),
    ).order_by("-revision")
    for snapshot in candidates:
        verify_json_contract(
            snapshot.operations,
            schema_version=snapshot.operations_schema_version,
            size_bytes=snapshot.operations_size_bytes,
            sha256=snapshot.operations_sha256,
        )
        verify_json_contract(
            snapshot.evidence,
            schema_version=snapshot.evidence_schema_version,
            size_bytes=snapshot.evidence_size_bytes,
            sha256=snapshot.evidence_sha256,
        )
        if operation in snapshot.operations and snapshot.evidence.get("execution_submitted") is True:
            return snapshot
    return None


def frozen_async_deployment(route: dict[str, Any]) -> ModelDeployment:
    deployment = ModelDeployment.objects.select_related("profile_version__provider").filter(
        pk=route.get("model_deployment_id"),
        status=ModelDeployment.Status.ACTIVE,
    ).first()
    if deployment is None:
        raise ValidationError("冻结 async deployment 不存在或未启用")
    provider = deployment.profile_version.provider
    live_checks = {
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "model_profile_version_id": str(deployment.profile_version_id),
        "provider_id": str(provider.id),
        "driver_type": provider.driver_type,
    }
    for key, current in live_checks.items():
        if str(route.get(key) or "") != str(current or ""):
            raise ValidationError(f"冻结 async route 的 {key} 已漂移")
    if not provider.is_active or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED:
        raise ValidationError("冻结 async route 的 provider/model 已停用")
    if provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("冻结 route 不是 async_rest")
    provider_config = route.get("provider_config")
    deployment_config = route.get("deployment_config")
    specification = route.get("model_specification")
    for value, digest, label in (
        (provider_config, route.get("provider_config_sha256"), "provider_config"),
        (deployment_config, route.get("deployment_config_sha256"), "deployment_config"),
        (specification, route.get("frozen_model_specification_sha256"), "model specification"),
    ):
        if not isinstance(value, dict) or json_contract(value).sha256 != digest:
            raise ValidationError(f"冻结 async route 的 {label} hash 无效")
    frozen_provider = copy(provider)
    provider_contract = json_contract(provider_config)
    frozen_provider.driver_config = provider_config
    frozen_provider.driver_config_schema_version = provider_contract.schema_version
    frozen_provider.driver_config_size_bytes = provider_contract.size_bytes
    frozen_provider.driver_config_sha256 = provider_contract.sha256
    frozen_profile = copy(deployment.profile_version)
    frozen_profile.provider = frozen_provider
    frozen_profile.model_identifier = str(route.get("model_identifier") or "")
    spec_contract = json_contract(specification)
    frozen_profile.specification = specification
    frozen_profile.specification_schema_version = spec_contract.schema_version
    frozen_profile.specification_size_bytes = spec_contract.size_bytes
    frozen_profile.specification_sha256 = spec_contract.sha256
    frozen = copy(deployment)
    frozen.profile_version = frozen_profile
    runtime_contract = json_contract(deployment_config)
    frozen.runtime_config = deployment_config
    frozen.runtime_config_schema_version = runtime_contract.schema_version
    frozen.runtime_config_size_bytes = runtime_contract.size_bytes
    frozen.runtime_config_sha256 = runtime_contract.sha256
    driver_for(frozen_provider).validate_config({**provider_config, **deployment_config})
    return frozen
