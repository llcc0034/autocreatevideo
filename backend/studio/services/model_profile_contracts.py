from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelProfileVersion
from studio.services.json_contracts import (
    find_forbidden_secret_fields,
    json_contract,
    verify_json_contract,
)
from studio.services.security_boundaries import boundary_int, boundary_values


CHAT_REQUEST_OPTIONS_KEY = "chat_request_options"
RETIRED_CHAT_COMPLETION_OPTIONS_KEY = "chat_completion_options"
_CHAT_REQUEST_OPTION_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")
_CHAT_REQUEST_RESERVED_FIELDS = frozenset({
    "input",
    "model",
    "messages",
    "prompt",
    "max_completion_tokens",
    "max_new_tokens",
    "max_output_tokens",
    "max_tokens",
    "stream",
    "seed",
    "timeout",
    "timeout_seconds",
    CHAT_REQUEST_OPTIONS_KEY,
    RETIRED_CHAT_COMPLETION_OPTIONS_KEY,
})
_CHAT_REQUEST_TRANSPORT_FIELDS = frozenset({
    "api_base",
    "base_url",
    "chat_path",
    "endpoint",
    "endpoint_url",
    "headers",
    "host",
    "url",
})
_CHAT_REQUEST_RESERVED_FIELDS_COLLAPSED = frozenset(
    field.replace("_", "") for field in _CHAT_REQUEST_RESERVED_FIELDS
)
_CHAT_REQUEST_TRANSPORT_FIELDS_COLLAPSED = frozenset(
    field.replace("_", "") for field in _CHAT_REQUEST_TRANSPORT_FIELDS
)


def _forbidden_chat_request_field_paths(
    value: Any,
    *,
    prefix: str = "",
) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            normalized = key.casefold().replace("-", "_")
            collapsed = normalized.replace("_", "")
            if (
                normalized in _CHAT_REQUEST_RESERVED_FIELDS
                or normalized in _CHAT_REQUEST_TRANSPORT_FIELDS
                or collapsed in _CHAT_REQUEST_RESERVED_FIELDS_COLLAPSED
                or collapsed in _CHAT_REQUEST_TRANSPORT_FIELDS_COLLAPSED
                or "timeout" in normalized
                or "endpoint" in normalized
            ):
                found.append(path)
            found.extend(_forbidden_chat_request_field_paths(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                _forbidden_chat_request_field_paths(
                    child,
                    prefix=f"{prefix}[{index}]",
                ),
            )
    return found


def _chat_request_option_limits() -> dict[str, int]:
    from studio.services.configuration import ConfigurationResolutionError

    keys = {
        "max_bytes": "security.prompt_normalization.chat_options_max_bytes",
        "max_depth": "security.prompt_normalization.chat_options_max_depth",
        "max_fields": "security.prompt_normalization.chat_options_max_fields",
        "max_array_items": (
            "security.prompt_normalization.chat_options_max_array_items"
        ),
        "max_string_characters": (
            "security.prompt_normalization.chat_option_string_max_characters"
        ),
    }
    values = boundary_values(*keys.values())
    limits: dict[str, int] = {}
    for label, key in keys.items():
        value = values[key]
        if type(value) is not int or value < 1:
            raise ConfigurationResolutionError(
                f"安全边界 {key} 必须是正整数",
                code="security_boundary_value_invalid",
                missing_keys=(key,),
            )
        limits[label] = value
    return limits


def _validated_chat_request_option_value(
    value: Any,
    *,
    depth: int,
    limits: dict[str, int],
) -> Any:
    if depth > limits["max_depth"]:
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 嵌套过深",
        })
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError({
                CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 包含非有限数值",
            })
        return value
    if isinstance(value, str):
        if len(value) > limits["max_string_characters"]:
            raise ValidationError({
                CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 字符串过长",
            })
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValidationError({
                CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 字符串包含控制字符",
            })
        return value
    if isinstance(value, list):
        if len(value) > limits["max_array_items"]:
            raise ValidationError({
                CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 数组过长",
            })
        return [
            _validated_chat_request_option_value(
                item,
                depth=depth + 1,
                limits=limits,
            )
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > limits["max_fields"]:
            raise ValidationError({
                CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 字段过多",
            })
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or _CHAT_REQUEST_OPTION_KEY_RE.fullmatch(key) is None
            ):
                raise ValidationError({
                    CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 字段名无效",
                })
            normalized[key] = _validated_chat_request_option_value(
                item,
                depth=depth + 1,
                limits=limits,
            )
        return normalized
    raise ValidationError({
        CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 包含不支持的数据类型",
    })


def validate_chat_request_options(value: Any) -> dict[str, Any]:
    """Validate one model-owned, provider-neutral Chat request contract.

    The operation policy must explicitly freeze this options object; an empty
    object is valid when the selected model needs no optional Chat parameters.
    Transport location, credentials, timeout, prompt messages, output budget
    and runtime seed remain owned by their dedicated contracts.  Additional
    provider options are allowed only when they are safe JSON and therefore
    can be frozen with the immutable ModelProfile version.
    """

    if not isinstance(value, dict):
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 必须是 JSON 对象",
        })
    collisions = sorted(_forbidden_chat_request_field_paths(value))
    if collisions:
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: (
                "chat_request_options 不得覆盖服务端运行或传输字段: "
                + ", ".join(collisions)
            ),
        })
    if find_forbidden_secret_fields(value):
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: "chat_request_options 不得包含凭据或秘密字段",
        })
    temperature = value.get("temperature")
    if temperature is not None and (
        type(temperature) not in {int, float}
        or not math.isfinite(float(temperature))
        or float(temperature) < 0
    ):
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: "temperature 必须是非负有限数值",
        })
    response_format = value.get("response_format")
    if response_format is not None and (
        not isinstance(response_format, dict)
        or not isinstance(response_format.get("type"), str)
        or not response_format["type"].strip()
    ):
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: (
                "response_format 必须是包含非空 type 的 JSON 对象"
            ),
        })
    limits = _chat_request_option_limits()
    normalized = _validated_chat_request_option_value(
        value,
        depth=0,
        limits=limits,
    )
    assert isinstance(normalized, dict)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > limits["max_bytes"]:
        raise ValidationError({
            CHAT_REQUEST_OPTIONS_KEY: (
                "chat_request_options 超过当前安全边界 "
                f"{limits['max_bytes']} 字节"
            ),
        })
    return deepcopy(normalized)


def chat_request_options_from_policy(
    policy: Any,
    *,
    label: str,
) -> dict[str, Any]:
    """Require the canonical request field and reject its retired alias."""

    if not isinstance(policy, dict):
        raise ValidationError({"specification": f"{label} policy 必须是对象"})
    if RETIRED_CHAT_COMPLETION_OPTIONS_KEY in policy:
        raise ValidationError({
            "specification": (
                f"{label}.{RETIRED_CHAT_COMPLETION_OPTIONS_KEY} 已退役；"
                f"请使用 {CHAT_REQUEST_OPTIONS_KEY}"
            ),
        })
    if CHAT_REQUEST_OPTIONS_KEY not in policy:
        raise ValidationError({
            "specification": f"{label} 缺少显式 {CHAT_REQUEST_OPTIONS_KEY}",
        })
    try:
        return validate_chat_request_options(policy[CHAT_REQUEST_OPTIONS_KEY])
    except ValidationError as exc:
        raise ValidationError({
            "specification": f"{label}.{CHAT_REQUEST_OPTIONS_KEY} 无效: {exc}",
        }) from exc


def verified_model_profile_specification(
    profile: ModelProfileVersion,
) -> dict[str, Any]:
    """Return a ModelProfile specification only after integrity verification.

    ModelProfileVersion stores the canonical JSON byte length and SHA-256 next
    to the JSON body.  Runtime callers must use this accessor instead of
    reading ``profile.specification`` directly so an out-of-band body change
    cannot keep using capability, routing, or smoke evidence from the former
    contract.
    """

    contract = verify_json_contract(
        profile.specification,
        schema_version=profile.specification_schema_version,
        size_bytes=profile.specification_size_bytes,
        sha256=profile.specification_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    if not isinstance(contract.value, dict):
        raise ValidationError({
            "specification": "ModelProfile specification 必须是 JSON 对象",
        })
    return contract.value


def verified_frozen_model_profile_specification(
    profile: ModelProfileVersion,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Verify that a frozen route came from the current signed profile body."""

    live_specification = verified_model_profile_specification(profile)
    frozen_specification = route.get("model_specification")
    if not isinstance(frozen_specification, dict):
        raise ValidationError("冻结 route 缺少 model specification")
    live_sha256 = json_contract(
        live_specification,
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    frozen_sha256 = json_contract(
        frozen_specification,
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    expected_sha256 = profile.specification_sha256
    if any(
        digest != expected_sha256
        for digest in (
            live_sha256,
            str(route.get("model_specification_sha256") or ""),
            str(route.get("frozen_model_specification_sha256") or ""),
            frozen_sha256,
        )
    ):
        raise ValidationError("冻结 route 的 model specification 来源证明无效")
    return frozen_specification
