"""Versioned, configuration-backed FFmpeg profiles for managed media work.

The profile values are ordinary system/project configuration.  Resource and
abuse ceilings are separate ``security_boundary`` values, so an operator may
tune quality without turning an implementation constant into a hidden gate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any

from studio.services.configuration import resolve_configuration
from studio.services.security_boundaries import boundary_values


ASSEMBLY_PROFILE_KEY = "delivery.assembly_media_profile"
DISTRIBUTION_PROFILE_KEY = "delivery.distribution_media_profile"
REFERENCE_PROFILE_KEY = "reference.media_preprocessing_profile"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_COLOR_RE = re.compile(r"^(?:[A-Za-z]+|#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)$")


class MediaProcessingProfileError(ValueError):
    """A published media-processing profile is structurally unusable."""


@dataclass(frozen=True)
class ResolvedMediaProcessingProfile:
    key: str
    value: dict[str, Any]
    sha256: str
    configuration_sha256: str
    revision_ids: tuple[str, ...]


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def media_profile_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _exact_fields(
    value: Any,
    *,
    key: str,
    fields: set[str],
    schema_versioned: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        missing = sorted(fields - set(value) if isinstance(value, dict) else fields)
        extra = sorted(set(value) - fields if isinstance(value, dict) else set())
        raise MediaProcessingProfileError(
            f"{key} 字段合同不完整 (missing={missing}, extra={extra})"
        )
    if schema_versioned and value.get("schema_version") != 1:
        raise MediaProcessingProfileError(f"{key}.schema_version 必须为 1")
    return deepcopy(value)


def _positive_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MediaProcessingProfileError(f"{field} 必须为正整数")
    if maximum is not None and value > maximum:
        raise MediaProcessingProfileError(f"{field} 超过安全边界 {maximum}")
    return value


def _nonnegative_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MediaProcessingProfileError(f"{field} 必须为非负整数")
    if maximum is not None and value > maximum:
        raise MediaProcessingProfileError(f"{field} 超过安全边界 {maximum}")
    return value


def _token(value: Any, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not _TOKEN_RE.fullmatch(value)
    ):
        raise MediaProcessingProfileError(f"{field} 不是安全的 FFmpeg 参数 token")
    return value


def _color(value: Any, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not _COLOR_RE.fullmatch(value)
    ):
        raise MediaProcessingProfileError(f"{field} 不是安全的颜色值")
    return value


def _opacity(value: Any, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or type(value) not in {int, float}
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise MediaProcessingProfileError(f"{field} 必须是 0–1 的有限数字")
    return float(value)


def _limits() -> dict[str, int]:
    values = boundary_values(
        "security.runtime.max_timeout_seconds",
        "security.media.duration_verification_tolerance_max_ms",
        "security.media.audio_bitrate_max_kbps",
        "security.media.audio_sample_rate_max_hz",
        "security.media.processing_argument_max_characters",
        "security.media.max_dimension",
    )
    return {key: _positive_int(value, field=key) for key, value in values.items()}


def _common_av_profile(profile: dict[str, Any], *, key: str, limits: dict[str, int]) -> None:
    token_max = limits["security.media.processing_argument_max_characters"]
    for field in ("video_codec", "video_preset", "pixel_format", "audio_codec"):
        _token(profile[field], field=f"{key}.{field}", maximum=token_max)
    _nonnegative_int(profile["video_crf"], field=f"{key}.video_crf")
    _positive_int(
        profile["audio_bitrate_kbps"],
        field=f"{key}.audio_bitrate_kbps",
        maximum=limits["security.media.audio_bitrate_max_kbps"],
    )
    _positive_int(
        profile["audio_sample_rate_hz"],
        field=f"{key}.audio_sample_rate_hz",
        maximum=limits["security.media.audio_sample_rate_max_hz"],
    )


def _validate_overlay(value: Any, *, key: str, limits: dict[str, int]) -> dict[str, Any]:
    overlay = _exact_fields(
        value,
        key=key,
        fields={
            "font_color", "font_size_divisor", "box_color", "box_opacity",
            "box_border_width_px", "margin_x_px", "margin_y_px",
        },
    )
    token_max = limits["security.media.processing_argument_max_characters"]
    dimension_max = limits["security.media.max_dimension"]
    _color(overlay["font_color"], field=f"{key}.font_color", maximum=token_max)
    _color(overlay["box_color"], field=f"{key}.box_color", maximum=token_max)
    _positive_int(
        overlay["font_size_divisor"],
        field=f"{key}.font_size_divisor",
        maximum=dimension_max,
    )
    _opacity(overlay["box_opacity"], field=f"{key}.box_opacity")
    for field in ("box_border_width_px", "margin_x_px", "margin_y_px"):
        _nonnegative_int(
            overlay[field], field=f"{key}.{field}", maximum=dimension_max,
        )
    return overlay


def validate_assembly_profile(value: Any) -> dict[str, Any]:
    key = ASSEMBLY_PROFILE_KEY
    profile = _exact_fields(
        value,
        key=key,
        fields={
            "schema_version", "probe_timeout_seconds", "video_codec",
            "video_preset", "video_crf", "pixel_format", "audio_codec",
            "audio_bitrate_kbps", "audio_sample_rate_hz", "overlay",
        },
        schema_versioned=True,
    )
    limits = _limits()
    _positive_int(
        profile["probe_timeout_seconds"],
        field=f"{key}.probe_timeout_seconds",
        maximum=limits["security.runtime.max_timeout_seconds"],
    )
    _common_av_profile(profile, key=key, limits=limits)
    profile["overlay"] = _validate_overlay(
        profile["overlay"], key=f"{key}.overlay", limits=limits,
    )
    return profile


def validate_distribution_profile(value: Any) -> dict[str, Any]:
    key = DISTRIBUTION_PROFILE_KEY
    profile = _exact_fields(
        value,
        key=key,
        fields={
            "schema_version", "probe_timeout_seconds", "render_timeout_seconds",
            "video_codec", "video_preset", "video_crf", "pixel_format",
            "audio_codec", "audio_bitrate_kbps", "audio_sample_rate_hz",
            "video_overlay", "image_overlay",
        },
        schema_versioned=True,
    )
    limits = _limits()
    for field in ("probe_timeout_seconds", "render_timeout_seconds"):
        _positive_int(
            profile[field], field=f"{key}.{field}",
            maximum=limits["security.runtime.max_timeout_seconds"],
        )
    _common_av_profile(profile, key=key, limits=limits)
    profile["video_overlay"] = _validate_overlay(
        profile["video_overlay"], key=f"{key}.video_overlay", limits=limits,
    )
    image = _exact_fields(
        profile["image_overlay"],
        key=f"{key}.image_overlay",
        fields={
            "font_size_divisor", "minimum_font_size_px", "width_inset_px",
            "padding_divisor", "minimum_padding_px", "margin_divisor",
            "minimum_margin_px", "minimum_corner_radius_px",
            "minimum_source_dimension_px", "box_rgba", "text_rgba",
        },
    )
    dimension_max = limits["security.media.max_dimension"]
    for field in (
        "font_size_divisor", "minimum_font_size_px", "width_inset_px",
        "padding_divisor", "minimum_padding_px", "margin_divisor",
        "minimum_margin_px", "minimum_corner_radius_px",
        "minimum_source_dimension_px",
    ):
        _positive_int(
            image[field], field=f"{key}.image_overlay.{field}", maximum=dimension_max,
        )
    for field in ("box_rgba", "text_rgba"):
        rgba = image[field]
        if (
            not isinstance(rgba, list)
            or len(rgba) != 4
            or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255 for item in rgba)
        ):
            raise MediaProcessingProfileError(
                f"{key}.image_overlay.{field} 必须是四个 0–255 整数"
            )
    profile["image_overlay"] = image
    return profile


def validate_reference_profile(value: Any) -> dict[str, Any]:
    key = REFERENCE_PROFILE_KEY
    profile = _exact_fields(
        value,
        key=key,
        fields={
            "schema_version", "probe_timeout_seconds",
            "video_normalize_timeout_seconds", "audio_normalize_timeout_seconds",
            "video_codec", "video_preset", "video_crf", "pixel_format",
            "embedded_audio_codec", "embedded_audio_bitrate_kbps",
            "paired_audio_codec", "audio_sample_rate_hz",
            "video_duration_tolerance_ms", "embedded_audio_duration_tolerance_ms",
            "paired_audio_duration_tolerance_ms",
        },
        schema_versioned=True,
    )
    limits = _limits()
    timeout_max = limits["security.runtime.max_timeout_seconds"]
    for field in (
        "probe_timeout_seconds", "video_normalize_timeout_seconds",
        "audio_normalize_timeout_seconds",
    ):
        _positive_int(profile[field], field=f"{key}.{field}", maximum=timeout_max)
    token_max = limits["security.media.processing_argument_max_characters"]
    for field in (
        "video_codec", "video_preset", "pixel_format",
        "embedded_audio_codec", "paired_audio_codec",
    ):
        _token(profile[field], field=f"{key}.{field}", maximum=token_max)
    _nonnegative_int(profile["video_crf"], field=f"{key}.video_crf")
    _positive_int(
        profile["embedded_audio_bitrate_kbps"],
        field=f"{key}.embedded_audio_bitrate_kbps",
        maximum=limits["security.media.audio_bitrate_max_kbps"],
    )
    _positive_int(
        profile["audio_sample_rate_hz"],
        field=f"{key}.audio_sample_rate_hz",
        maximum=limits["security.media.audio_sample_rate_max_hz"],
    )
    tolerance_max = limits["security.media.duration_verification_tolerance_max_ms"]
    for field in (
        "video_duration_tolerance_ms", "embedded_audio_duration_tolerance_ms",
        "paired_audio_duration_tolerance_ms",
    ):
        _nonnegative_int(profile[field], field=f"{key}.{field}", maximum=tolerance_max)
    return profile


_VALIDATORS = {
    ASSEMBLY_PROFILE_KEY: validate_assembly_profile,
    DISTRIBUTION_PROFILE_KEY: validate_distribution_profile,
    REFERENCE_PROFILE_KEY: validate_reference_profile,
}


def verify_media_processing_profile(
    key: str,
    value: Any,
    *,
    sha256: str = "",
) -> dict[str, Any]:
    try:
        validator = _VALIDATORS[key]
    except KeyError as exc:
        raise MediaProcessingProfileError(f"未知媒体处理 profile: {key}") from exc
    profile = validator(value)
    actual_sha256 = media_profile_sha256(profile)
    if sha256 and sha256 != actual_sha256:
        raise MediaProcessingProfileError(f"{key} 冻结 hash 不一致")
    return profile


def resolve_media_processing_profile(
    key: str,
    *,
    project=None,
) -> ResolvedMediaProcessingProfile:
    resolved = resolve_configuration(project=project, required_keys=(key,))
    profile = verify_media_processing_profile(key, resolved.values[key])
    return ResolvedMediaProcessingProfile(
        key=key,
        value=profile,
        sha256=media_profile_sha256(profile),
        configuration_sha256=resolved.sha256,
        revision_ids=resolved.revision_ids,
    )


def resolve_assembly_media_profile(*, project=None) -> ResolvedMediaProcessingProfile:
    return resolve_media_processing_profile(ASSEMBLY_PROFILE_KEY, project=project)


def resolve_distribution_media_profile(*, project=None) -> ResolvedMediaProcessingProfile:
    return resolve_media_processing_profile(DISTRIBUTION_PROFILE_KEY, project=project)


def resolve_reference_media_profile(*, project=None) -> ResolvedMediaProcessingProfile:
    return resolve_media_processing_profile(REFERENCE_PROFILE_KEY, project=project)
