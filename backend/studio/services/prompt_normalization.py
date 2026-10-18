"""Validate the explicit native prompt.normalize model protocol."""
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.prompt_normalization_contract import (
    PROMPT_NORMALIZE_OPERATION, PROMPT_NORMALIZATION_POLICY_KEY,
    PromptNormalizationContractError, prompt_normalization_policy_from_specification,
)
from studio.services.provider_registry import driver_for


def validate_prompt_normalization_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, Any] | None:
    """Validate an exact prompt.normalize declaration before publication."""

    specification = verified_model_profile_specification(profile)
    declared = specification.get("capabilities")
    aliases = {
        field
        for field in ("operations", "protocols")
        if PROMPT_NORMALIZE_OPERATION in (specification.get(field) or [])
    }
    if aliases:
        raise ValidationError({
            "specification": (
                "prompt.normalize 只能在 specification.capabilities 中声明；"
                f"禁止兼容字段: {', '.join(sorted(aliases))}"
            ),
        })
    if not isinstance(declared, list) or PROMPT_NORMALIZE_OPERATION not in declared:
        if PROMPT_NORMALIZATION_POLICY_KEY in specification:
            raise ValidationError({
                "specification": (
                    "只有精确声明 prompt.normalize 能力时才能配置 "
                    "specification.prompt_normalization"
                ),
            })
        return None
    if any(not isinstance(item, str) or not item.strip() for item in declared):
        raise ValidationError({"specification": "capabilities 必须是非空字符串数组"})
    if len(declared) != len(set(declared)):
        raise ValidationError({"specification": "capabilities 不能重复"})
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        raise ValidationError({"modality": "prompt.normalize 只支持 text/multimodal 模型"})
    driver = driver_for(profile.provider)
    operations = set((driver.capabilities() or {}).get("operations") or [])
    if PROMPT_NORMALIZE_OPERATION not in operations:
        raise ValidationError({
            "specification": "provider driver 未实现 prompt.normalize",
        })
    try:
        policy = prompt_normalization_policy_from_specification(
            specification,
            required=True,
        )
    except PromptNormalizationContractError as exc:
        raise ValidationError({"specification": str(exc)}) from exc
    assert policy is not None
    return policy
