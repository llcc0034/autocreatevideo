from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.provider_registry import driver_for
from studio.services.script_model_policies import validate_script_model_policy


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
    return validate_script_model_policy(specification.get("series_concept"), key="series_concept")
