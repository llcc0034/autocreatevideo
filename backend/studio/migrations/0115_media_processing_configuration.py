import hashlib
import json

from django.db import migrations
from django.utils import timezone


BOUNDARIES = {
    "security.delivery.disclosure_text_max_characters": (
        400,
        "characters",
        "交付 AI 披露标签与描述最大字符数 / Maximum delivery AI-disclosure label and description characters",
    ),
    "security.media.duration_verification_tolerance_max_ms": (
        1_000,
        "milliseconds",
        "媒体规范化时长验证容差绝对上限 / Maximum media-normalization duration verification tolerance",
    ),
    "security.media.audio_bitrate_max_kbps": (
        2_048,
        "kbps",
        "受管媒体音频码率绝对上限 / Maximum managed-media audio bitrate",
    ),
    "security.media.audio_sample_rate_max_hz": (
        384_000,
        "Hz",
        "受管媒体音频采样率绝对上限 / Maximum managed-media audio sample rate",
    ),
    "security.media.processing_argument_max_characters": (
        80,
        "characters",
        "媒体处理 codec/preset/pixel-format 参数最大字符数 / Maximum media-processing argument characters",
    ),
}


ASSEMBLY_PROFILE = {
    "schema_version": 1,
    "probe_timeout_seconds": 30,
    "video_codec": "libx264",
    "video_preset": "medium",
    "video_crf": 18,
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate_kbps": 192,
    "audio_sample_rate_hz": 48_000,
    "overlay": {
        "font_color": "white",
        "font_size_divisor": 40,
        "box_color": "black",
        "box_opacity": 0.62,
        "box_border_width_px": 10,
        "margin_x_px": 24,
        "margin_y_px": 24,
    },
}


DISTRIBUTION_PROFILE = {
    "schema_version": 1,
    "probe_timeout_seconds": 30,
    "render_timeout_seconds": 100,
    "video_codec": "libx264",
    "video_preset": "medium",
    "video_crf": 18,
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate_kbps": 192,
    "audio_sample_rate_hz": 48_000,
    "video_overlay": {
        "font_color": "white",
        "font_size_divisor": 40,
        "box_color": "black",
        "box_opacity": 0.70,
        "box_border_width_px": 10,
        "margin_x_px": 24,
        "margin_y_px": 24,
    },
    "image_overlay": {
        "font_size_divisor": 28,
        "minimum_font_size_px": 8,
        "width_inset_px": 16,
        "padding_divisor": 80,
        "minimum_padding_px": 4,
        "margin_divisor": 50,
        "minimum_margin_px": 6,
        "minimum_corner_radius_px": 2,
        "minimum_source_dimension_px": 32,
        "box_rgba": [0, 0, 0, 190],
        "text_rgba": [255, 255, 255, 255],
    },
}


REFERENCE_PROFILE = {
    "schema_version": 1,
    "probe_timeout_seconds": 45,
    "video_normalize_timeout_seconds": 180,
    "audio_normalize_timeout_seconds": 120,
    "video_codec": "libx264",
    "video_preset": "medium",
    "video_crf": 12,
    "pixel_format": "yuv420p",
    "embedded_audio_codec": "aac",
    "embedded_audio_bitrate_kbps": 192,
    "paired_audio_codec": "pcm_s16le",
    "audio_sample_rate_hz": 48_000,
    "video_duration_tolerance_ms": 60,
    "embedded_audio_duration_tolerance_ms": 80,
    "paired_audio_duration_tolerance_ms": 50,
}


def _object_schema(value, *, security_paths):
    def property_schema(item):
        if isinstance(item, bool):
            return {"type": "boolean"}
        if isinstance(item, int):
            return {"type": "integer", "minimum": 0}
        if isinstance(item, float):
            return {"type": "number", "minimum": 0, "maximum": 1}
        if isinstance(item, str):
            return {"type": "string", "minLength": 1}
        if isinstance(item, list):
            if len(item) == 4 and all(isinstance(child, int) for child in item):
                return {
                    "type": "array",
                    "minItems": 4,
                    "maxItems": 4,
                    "items": {"type": "integer", "minimum": 0, "maximum": 255},
                }
            return {"type": "array"}
        if isinstance(item, dict):
            return {
                "type": "object",
                "required": sorted(item),
                "additionalProperties": False,
                "properties": {
                    key: property_schema(child) for key, child in item.items()
                },
            }
        raise TypeError(type(item))

    schema = property_schema(value)
    schema["properties"]["schema_version"]["enum"] = [1]
    schema["closed_contract"] = True
    schema["security_ceiling_paths"] = dict(security_paths)
    return schema


PROFILES = {
    "delivery.assembly_media_profile": (
        "delivery",
        ASSEMBLY_PROFILE,
        "逐集/全剧装配编码、音频与 AI 披露样式合同 / Assembly encoding, audio and disclosure-style contract",
        ["assembly", "delivery", "ffmpeg"],
    ),
    "delivery.distribution_media_profile": (
        "delivery",
        DISTRIBUTION_PROFILE,
        "公开分发副本编码、探测与 AI 披露样式合同 / Distribution rendering, probing and disclosure-style contract",
        ["distribution", "delivery", "ffmpeg"],
    ),
    "reference.media_preprocessing_profile": (
        "reference",
        REFERENCE_PROFILE,
        "动态参考媒体探测、CFR/音频规范化与验证容差合同 / Reference-media probing, normalization and verification contract",
        ["reference", "workflow", "ffmpeg"],
    ),
}


STANDARD_VALUES = {
    "artifact.media_probe_timeout_seconds": (
        "artifact",
        30,
        "number",
        "seconds",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "受管视频/音频产物 FFprobe 超时 / Managed video and audio artifact probe timeout",
        ["artifact", "media", "ffprobe"],
    ),
}


COMMON_AV_SECURITY_PATHS = {
    "audio_bitrate_kbps": "security.media.audio_bitrate_max_kbps",
    "audio_sample_rate_hz": "security.media.audio_sample_rate_max_hz",
}

PROFILE_SECURITY_PATHS = {
    "delivery.assembly_media_profile": {
        **COMMON_AV_SECURITY_PATHS,
        "probe_timeout_seconds": "security.runtime.max_timeout_seconds",
        "overlay.font_size_divisor": "security.media.max_dimension",
        "overlay.box_border_width_px": "security.media.max_dimension",
        "overlay.margin_x_px": "security.media.max_dimension",
        "overlay.margin_y_px": "security.media.max_dimension",
    },
    "delivery.distribution_media_profile": {
        **COMMON_AV_SECURITY_PATHS,
        "probe_timeout_seconds": "security.runtime.max_timeout_seconds",
        "render_timeout_seconds": "security.runtime.max_timeout_seconds",
        "video_overlay.font_size_divisor": "security.media.max_dimension",
        "video_overlay.box_border_width_px": "security.media.max_dimension",
        "video_overlay.margin_x_px": "security.media.max_dimension",
        "video_overlay.margin_y_px": "security.media.max_dimension",
        "image_overlay.font_size_divisor": "security.media.max_dimension",
        "image_overlay.minimum_font_size_px": "security.media.max_dimension",
        "image_overlay.width_inset_px": "security.media.max_dimension",
        "image_overlay.padding_divisor": "security.media.max_dimension",
        "image_overlay.minimum_padding_px": "security.media.max_dimension",
        "image_overlay.margin_divisor": "security.media.max_dimension",
        "image_overlay.minimum_margin_px": "security.media.max_dimension",
        "image_overlay.minimum_corner_radius_px": "security.media.max_dimension",
        "image_overlay.minimum_source_dimension_px": "security.media.max_dimension",
    },
    "reference.media_preprocessing_profile": {
        "probe_timeout_seconds": "security.runtime.max_timeout_seconds",
        "video_normalize_timeout_seconds": "security.runtime.max_timeout_seconds",
        "audio_normalize_timeout_seconds": "security.runtime.max_timeout_seconds",
        "embedded_audio_bitrate_kbps": "security.media.audio_bitrate_max_kbps",
        "audio_sample_rate_hz": "security.media.audio_sample_rate_max_hz",
        "video_duration_tolerance_ms": "security.media.duration_verification_tolerance_max_ms",
        "embedded_audio_duration_tolerance_ms": "security.media.duration_verification_tolerance_max_ms",
        "paired_audio_duration_tolerance_ms": "security.media.duration_verification_tolerance_max_ms",
    },
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def configure_media_processing(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions = {}
    boundary_schema = {"minimum": 1}
    boundary_schema_size, boundary_schema_sha = _contract(boundary_schema)
    for key, (initial, unit, description) in BOUNDARIES.items():
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": description + "。命中后 fail closed；只有超级管理员可修改。",
                "unit": unit,
                "impact_modules": ["security", "media", "ffmpeg"],
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": boundary_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": boundary_schema_size,
                "value_schema_sha256": boundary_schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )
        if not created and (
            definition.configuration_class != "security_boundary"
            or definition.domain != "security"
            or definition.value_type != "integer"
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0115 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    for key, (domain, initial, description, impact_modules) in PROFILES.items():
        schema = _object_schema(
            initial,
            security_paths=PROFILE_SECURITY_PATHS[key],
        )
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": domain,
                "configuration_class": "standard",
                "value_type": "object",
                "description": (
                    description
                    + "。普通字段可在 system/project 修订中调整；资源值不得超过 security_ceiling_paths 声明的安全边界。"
                ),
                "unit": "contract",
                "impact_modules": impact_modules,
                "restart_required": False,
                "allowed_scopes": "system,project",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )
        if not created and (
            definition.configuration_class != "standard"
            or definition.value_type != "object"
            or definition.allowed_scopes != "system,project"
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0115 媒体处理配置定义 {key} 与既有合同冲突")
        if not created:
            Definition.objects.filter(pk=definition.pk).update(
                description=(
                    description
                    + "。普通字段可在 system/project 修订中调整；资源值不得超过 security_ceiling_paths 声明的安全边界。"
                ),
                value_schema=schema,
                value_schema_version=1,
                value_schema_size_bytes=schema_size,
                value_schema_sha256=schema_sha,
            )
        definitions[key] = definition

    for key, (
        domain, initial, value_type, unit, schema, description, impact_modules,
    ) in STANDARD_VALUES.items():
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": domain,
                "configuration_class": "standard",
                "value_type": value_type,
                "description": description,
                "unit": unit,
                "impact_modules": impact_modules,
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )
        if not created and (
            definition.configuration_class != "standard"
            or definition.value_type != value_type
            or definition.allowed_scopes != "system"
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0115 媒体探测配置定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0115 媒体处理配置缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: spec[0] for key, spec in BOUNDARIES.items()},
        **{key: spec[1] for key, spec in PROFILES.items()},
        **{key: spec[1] for key, spec in STANDARD_VALUES.items()},
    }
    missing = sorted(set(initial_values) - {row.definition.key for row in existing})
    if not missing:
        return

    now = timezone.now()
    next_revision = (
        Revision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    revision = Revision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.id,
        activation_state="inactive",
        change_summary="0115 媒体装配、分发与参考预处理参数配置化",
        published_at=now,
    )
    rows = []
    digest_values = {}
    for row in existing:
        rows.append(Value(
            revision=revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        if row.credential_version_id:
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            digest_values[row.definition.key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            }
        else:
            digest_values[row.definition.key] = row.value
    for key in missing:
        initial = initial_values[key]
        initial_size, initial_sha = _contract(initial)
        rows.append(Value(
            revision=revision,
            definition=definitions[key],
            value=initial,
            value_schema_version=1,
            value_size_bytes=initial_size,
            value_sha256=initial_sha,
        ))
        digest_values[key] = initial
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Revision.objects.filter(pk=active.pk).update(
        status="superseded",
        activation_state="inactive",
        updated_at=now,
    )
    Revision.objects.filter(pk=revision.pk).update(
        activation_state="active",
        activated_at=now,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0114_api_effective_limit_contracts")]

    operations = [
        migrations.RunPython(
            configure_media_processing,
            migrations.RunPython.noop,
        ),
    ]
