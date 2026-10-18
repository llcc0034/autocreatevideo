"""Scalar projection of the existing model-wide media constraints.

Adapters provide reviewed semantic roles; names never select a model-specific
formula. Coupled constraints (short/long side, frames versus duration) remain in
the full model contract and must also be checked on the resolved request/output.
"""
from django.core.exceptions import ValidationError
from copy import deepcopy
from decimal import Decimal
import math

from studio.services.media_capabilities import normalize_model_media_constraints, validate_media_resolution, validate_media_timing
from studio.services.model_profile_contracts import verified_model_profile_specification


def model_media_parameter_layers(profile, semantic_keys):
    """Return restrictions only, with no invented required flags or defaults."""
    roles = {"width", "height", "fps", "duration_seconds", "duration_ms"}
    if not isinstance(semantic_keys, dict) or set(semantic_keys) - roles:
        raise ValidationError({"parameters": "媒体适配器包含未注册的标量语义"})
    if any(not isinstance(key, str) or not key for key in semantic_keys.values()) or len(set(semantic_keys.values())) != len(semantic_keys):
        raise ValidationError({"parameters": "媒体标量语义必须接到不同的明确参数"})
    constraints = normalize_model_media_constraints(verified_model_profile_specification(profile))
    source = f"model.{profile.pk}.media_constraints"
    layers = {}
    for role, key in semantic_keys.items():
        rule = {"type": "number" if role == "duration_seconds" else "integer", "required": False}
        if role in {"width", "height"}:
            resolution = constraints["resolution"]
            for bound in ("minimum", "maximum"):
                if f"{bound}_{role}" in resolution:
                    rule[bound] = resolution[f"{bound}_{role}"]
            if f"{role}_multiple" in resolution:
                rule["alignments"] = [{"modulus": resolution[f"{role}_multiple"], "remainder": 0}]
        elif role == "fps" and constraints["fps"]["allowed"]:
            rule["enum"] = constraints["fps"]["allowed"]
        elif role in {"duration_seconds", "duration_ms"}:
            for bound, value in constraints["duration_ms"].items():
                # Unit conversion of an explicitly declared millisecond bound,
                # not a duration default or a rounding-to-valid-frame policy.
                rule[bound] = value / 1000 if role == "duration_seconds" else value
        if set(rule) != {"type", "required"}:
            layers[key] = {"source": source, "rule": rule}
    return {"layers": layers, "media_constraints": constraints, "semantic_keys": deepcopy(semantic_keys)}


def validate_resolved_execution_media(*, constraints, semantic_keys, values, output_kind):
    """Validate coupled operands after the common scalar resolver.

    The adapter's frozen semantic mapping is authoritative. This function does
    not invent dimensions, convert a filename into a model choice, rescale an
    image or derive duration from frames with an undeclared model formula.
    """
    roles = {"width", "height", "fps", "duration_ms", "duration_seconds"}
    if not isinstance(semantic_keys, dict) or set(semantic_keys) - roles or any(not isinstance(key, str) or not key for key in semantic_keys.values()) or len(set(semantic_keys.values())) != len(semantic_keys):
        raise ValidationError({"media_parameters": "必须提供受审适配器的唯一媒体语义接线"})
    if not isinstance(values, dict) or output_kind not in ("image", "video", "audio"):
        raise ValidationError({"media_parameters": "必须明确媒体输出类别和已解析参数"})
    if not isinstance(constraints, dict) or type(constraints.get("schema_version")) is not int or constraints["schema_version"] != 1 or set(constraints) != {"schema_version", "resolution", "fps", "duration_ms"} or any(not isinstance(constraints[key], dict) for key in ("resolution", "fps", "duration_ms")):
        raise ValidationError({"media_constraints": "需要已核对的完整冻结媒体约束"})
    resolved = {role: values[key] for role, key in semantic_keys.items() if key in values}
    for role, value in resolved.items():
        integer = role in {"width", "height", "duration_ms"}
        if type(value) not in ((int,) if integer else (int, float)) or (type(value) is float and not math.isfinite(value)) or value <= 0:
            raise ValidationError({f"media_parameters.{role}": "必须为明确的正整数或合同允许的有限正数"})
    seconds = resolved.get("duration_seconds")
    duration_ms = resolved.get("duration_ms")
    if seconds is not None:
        converted = Decimal(str(seconds)) * 1000
        if duration_ms is not None and converted != duration_ms:
            raise ValidationError({"media_parameters.duration_ms": "秒与毫秒时长声明冲突，不能选择其中一个覆盖"})
        duration_ms = converted
    if output_kind in {"image", "video"}:
        validate_media_resolution(constraints=constraints["resolution"], width=resolved.get("width"), height=resolved.get("height"))
    if output_kind in {"video", "audio"}:
        validate_media_timing(
            fps_constraints=constraints["fps"] if output_kind == "video" else {},
            duration_constraints=constraints["duration_ms"], fps=resolved.get("fps"), duration_ms=duration_ms,
        )
