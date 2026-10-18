"""Model transport validation and explicit planning inputs.

New model requests and responses use script_semantic_requests, not a fixed
script response format. Storyboards consume the original saved M2 structure.
"""
from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.provider_registry import driver_for
from studio.services.script_model_policies import validate_script_model_policy


SCRIPT_STRUCTURE_OPERATION = "script.structure"


def validate_script_structure_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, Any] | None:
    specification = verified_model_profile_specification(profile)
    aliases = sorted(
        field
        for field in ("operations", "protocols")
        if SCRIPT_STRUCTURE_OPERATION in (specification.get(field) or [])
    )
    if aliases:
        raise ValidationError({
            "specification": (
                "script.structure 只能在 specification.capabilities 中声明；"
                f"禁止兼容字段: {', '.join(aliases)}"
            ),
        })
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    if (
        any(not isinstance(item, str) or not item.strip() for item in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise ValidationError({"specification": "capabilities 必须是无重复的非空字符串数组"})
    if SCRIPT_STRUCTURE_OPERATION not in capabilities:
        return None
    if specification.get("schema_version") != 1:
        raise ValidationError({"specification": "script.structure specification.schema_version 必须为 1"})
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        raise ValidationError({"modality": "script.structure 只支持 text/multimodal 模型"})
    operations = set((driver_for(profile.provider).capabilities() or {}).get("operations") or [])
    if "chat_completion" not in operations:
        raise ValidationError({"specification": "provider driver 未实现受控 chat_completion transport"})
    return validate_script_model_policy(specification.get("script_structure"), key="script_structure")


def script_structure_planning_schema():
    fields = ("episode_count", "shots_per_episode", "episode_duration_seconds")
    return {"type": "object", "properties": {key: {"type": "integer", "minimum": 1} for key in fields},
        "required": list(fields), "additionalProperties": False}
