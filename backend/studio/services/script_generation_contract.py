from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.json_contracts import verify_json_contract
from studio.services.provider_registry import driver_for


SCRIPT_GENERATE_OPERATION = "script.generate"
SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION = 1


def validate_script_generation_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, int | float] | None:
    """Validate the one supported script.generate model-profile contract.

    Profiles that do not declare ``script.generate`` are outside this contract
    and return ``None``.  A declaration through a retired alias is always an
    error so publication cannot manufacture an execution path that the worker
    parses differently later.
    """

    specification = verify_json_contract(
        profile.specification,
        schema_version=profile.specification_schema_version,
        size_bytes=profile.specification_size_bytes,
        sha256=profile.specification_sha256,
    ).value
    if not isinstance(specification, dict):
        raise ValidationError({"specification": "model profile specification 必须是对象"})
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
    if not isinstance(series, dict):
        raise ValidationError({
            "specification": "script.generate model profile 缺少 series_concept policy",
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
    if not 0.5 <= timeout <= 3600:
        raise ValidationError({
            "specification": "series_concept.timeout_seconds 必须在 0.5 到 3600 秒之间",
        })
    return {
        "minimum_shot_duration_ms": minimum,
        "maximum_shot_duration_ms": maximum,
        "max_output_tokens": int(series["max_output_tokens"]),
        "timeout_seconds": timeout,
    }
