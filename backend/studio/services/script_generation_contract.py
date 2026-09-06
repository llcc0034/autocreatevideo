from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.model_profile_contracts import (
    CHAT_REQUEST_OPTIONS_KEY,
    chat_request_options_from_policy,
    verified_model_profile_specification,
)
from studio.services.provider_registry import driver_for
from studio.services.security_boundaries import boundary_number
from studio.services.text_field_contracts import (
    SERIES_CONCEPT_FIELD_KEYS,
    validate_text_field_contract,
)


SCRIPT_GENERATE_OPERATION = "script.generate"
SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION = 1


def validate_script_generation_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, Any] | None:
    """Validate the one supported script.generate model-profile contract.

    Profiles that do not declare ``script.generate`` are outside this contract
    and return ``None``.  A declaration through a retired alias is always an
    error so publication cannot manufacture an execution path that the worker
    parses differently later.
    """

    specification = verified_model_profile_specification(profile)
    aliases = sorted(
        field
        for field in ("operations", "protocols")
        if SCRIPT_GENERATE_OPERATION in (specification.get(field) or [])
    )
    if aliases:
        raise ValidationError({
            "specification": (
                "script.generate 只能在 specification.capabilities 中声明；"
                f"禁止兼容字段: {', '.join(aliases)}"
            ),
        })
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list) or SCRIPT_GENERATE_OPERATION not in capabilities:
        return None
    if specification.get("schema_version") != SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION:
        raise ValidationError({
            "specification": (
                "script.generate model profile specification.schema_version "
                f"必须为 {SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION}"
            ),
        })
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in capabilities
    ):
        raise ValidationError({"specification": "capabilities 必须是精确的非空字符串数组"})
    if len(capabilities) != len(set(capabilities)):
        raise ValidationError({"specification": "capabilities 不能重复"})
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        raise ValidationError({"modality": "script.generate 只支持 text/multimodal 模型"})
    operations = set((driver_for(profile.provider).capabilities() or {}).get("operations") or [])
    if "chat_completion" not in operations:
        raise ValidationError({
            "specification": "provider driver 未实现受控 chat_completion transport",
        })
    series = specification.get("series_concept")
    required_series_fields = {
        "minimum_shot_duration_ms",
        "maximum_shot_duration_ms",
        "max_output_tokens",
        "timeout_seconds",
        "field_contract",
        CHAT_REQUEST_OPTIONS_KEY,
    }
    if not isinstance(series, dict) or set(series) != required_series_fields:
        raise ValidationError({
            "specification": (
                "script.generate model profile 缺少精确 series_concept policy，"
                "或包含未注册字段"
            ),
        })
    integer_fields = (
        "minimum_shot_duration_ms",
        "maximum_shot_duration_ms",
        "max_output_tokens",
    )
    invalid = [
        field
        for field in integer_fields
        if type(series.get(field)) is not int or series[field] < 1
    ]
    if invalid:
        raise ValidationError({
            "specification": f"series_concept policy 无效: {', '.join(invalid)}",
        })
    minimum = int(series["minimum_shot_duration_ms"])
    maximum = int(series["maximum_shot_duration_ms"])
    if minimum > maximum:
        raise ValidationError({
            "specification": "series_concept minimum_shot_duration_ms 不能大于 maximum",
        })
    timeout_value: Any = series.get("timeout_seconds")
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ValidationError({
            "specification": "series_concept.timeout_seconds 必须显式配置为数值",
        })
    timeout = float(timeout_value)
    timeout_max = boundary_number(
        "security.provider.async_rest_timeout_max_seconds",
        minimum=0.001,
    )
    if not 0 < timeout <= timeout_max:
        raise ValidationError({
            "specification": (
                "series_concept.timeout_seconds 必须大于 0 且不超过"
                f"当前安全边界 {timeout_max:g} 秒"
            ),
        })
    field_contract = validate_text_field_contract(
        series["field_contract"],
        fields=SERIES_CONCEPT_FIELD_KEYS,
        label="series_concept",
    )
    return {
        "minimum_shot_duration_ms": minimum,
        "maximum_shot_duration_ms": maximum,
        "max_output_tokens": int(series["max_output_tokens"]),
        "timeout_seconds": timeout,
        "field_contract": field_contract,
        CHAT_REQUEST_OPTIONS_KEY: chat_request_options_from_policy(
            series,
            label="series_concept",
        ),
    }
