from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
)
from studio.services.json_contracts import json_contract
from studio.services.media_request_limits import media_prompt_absolute_max_chars
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.provider_registry import (
    credential_binding_sha256,
    credential_material_sha256,
    driver_for,
)
from studio.services.security_boundaries import boundary_int
from studio.services.async_media_schema import (
    ASYNC_MEDIA_OUTPUT_KIND, FIELD_TYPES, STANDARD_MEDIA_FIELDS, api_parameter_rule,
)


ASYNC_MEDIA_OPERATIONS = frozenset(ASYNC_MEDIA_OUTPUT_KIND)


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _validate_field(name: str, value: Any, rule: dict[str, Any], *, source: str) -> None:
    from studio.services.execution_parameters import resolve_execution_parameters

    parameter = api_parameter_rule(name, rule, required=True)
    if parameter["type"] == "string":
        parameter["max_length"] = min(parameter.get("max_length", media_prompt_absolute_max_chars()), media_prompt_absolute_max_chars())
    resolve_execution_parameters({name: [{"source": source, "rule": parameter}]}, {name: value})


def async_operation_contract(
    profile: ModelProfileVersion,
    operation: str,
) -> dict[str, Any]:
    specification = verified_model_profile_specification(profile)
    return _operation_contract(specification, operation)


def _operation_contract(specification: dict[str, Any], operation: str) -> dict[str, Any]:
    if operation not in ASYNC_MEDIA_OPERATIONS:
        raise ValidationError("async_rest 只支持已登记的文生图、文生视频和文生音频能力")
    if type(specification.get("schema_version")) is not int or specification["schema_version"] != 1:
        raise ValidationError("async_rest model specification.schema_version 必须为 1")
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list) or operation not in capabilities:
        raise ValidationError(f"model specification 未声明 {operation}")
    async_media = specification.get("async_media")
    if not isinstance(async_media, dict) or set(async_media) != {"schema_version", "operations"}:
        raise ValidationError("model specification.async_media 结构无效")
    if type(async_media.get("schema_version")) is not int or async_media["schema_version"] != 1 or not isinstance(async_media.get("operations"), dict):
        raise ValidationError("model specification.async_media schema 无效")
    contract = async_media["operations"].get(operation)
    if not isinstance(contract, dict) or set(contract) != {
        "output_kind", "request_schema", "smoke_inputs",
    }:
        raise ValidationError(f"async_media.operations.{operation} 结构无效")
    expected_kind = ASYNC_MEDIA_OUTPUT_KIND[operation]
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
    if any(not isinstance(item, str) for item in required) or len(required) != len(set(required)) or not set(required) <= set(properties):
        raise ValidationError(f"{operation} request_schema.required 无效")
    if "prompt" not in required:
        raise ValidationError(f"{operation} 必须要求 prompt")
    for name, rule in properties.items():
        if not isinstance(rule, dict):
            raise ValidationError(f"{operation}.{name} Schema 必须是对象")
        if rule.get("type") not in FIELD_TYPES[name]:
            raise ValidationError(f"{operation}.{name}.type 不受支持")
        api_parameter_rule(name, rule, required=name in required)
        if rule.get("type") == "string":
            absolute_maximum = media_prompt_absolute_max_chars()
            maximum = rule.get("maxLength")
            if name == "prompt" and maximum is None:
                raise ValidationError(
                    f"{operation}.prompt 必须由冻结请求 Schema 明确声明 maxLength",
                )
            if maximum is not None and (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1 <= maximum <= absolute_maximum
            ):
                raise ValidationError({
                    f"{operation}.{name}.maxLength": (
                        f"必须是 1 到当前安全边界 {absolute_maximum} 的整数"
                    ),
                })
            minimum = rule.get("minLength")
            if minimum is not None and (
                isinstance(minimum, bool)
                or not isinstance(minimum, int)
                or minimum < 0
                or minimum > absolute_maximum
                or (maximum is not None and minimum > maximum)
            ):
                raise ValidationError({
                    f"{operation}.{name}.minLength": "必须是不大于 maxLength 的非负整数",
                })
    smoke_inputs = contract.get("smoke_inputs")
    if not isinstance(smoke_inputs, dict) or set(smoke_inputs) - set(properties):
        raise ValidationError(f"{operation} smoke_inputs 含未知字段")
    if any(name not in smoke_inputs for name in required):
        raise ValidationError(f"{operation} smoke_inputs 缺少必填字段")
    for name, value in smoke_inputs.items():
        _validate_field(name, value, properties[name], source=f"{operation}.smoke_inputs")
    return contract


def validate_async_media_specification(specification: dict[str, Any], *, required: bool = False) -> list[str]:
    """Validate authoring before persistence, as well as publish and runtime."""
    if not required and "async_media" not in specification:
        return []
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise ValidationError("async_rest capabilities 必须是已登记能力列表")
    declared = sorted(set(capabilities) & ASYNC_MEDIA_OPERATIONS)
    media = specification.get("async_media")
    if not declared or not isinstance(media, dict) or not isinstance(media.get("operations"), dict):
        raise ValidationError("async_rest model 必须声明至少一个受支持的媒体能力及完整 async_media 合同")
    if set(media["operations"]) != set(declared):
        raise ValidationError("async_media.operations 必须与已选择的异步媒体能力完全一致；未选择合同须明确移除")
    for operation in declared:
        _operation_contract(specification, operation)
    return declared


def validate_async_model_profile(profile: ModelProfileVersion) -> list[str]:
    if profile.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        return []
    specification = verified_model_profile_specification(profile)
    declared = validate_async_media_specification(specification, required=True)
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
            _validate_field(name, value, properties[name], source="media_request")
    return payload


def deployment_target_payload(deployment: ModelDeployment) -> dict[str, Any]:
    provider = deployment.profile_version.provider
    verified_model_profile_specification(deployment.profile_version)
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
    return json_contract(
        deployment_target_payload(deployment),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
