import hashlib
import json

from django.core.validators import MinValueValidator
from django.db import migrations, models
from django.utils import timezone


BOUNDARIES = {
    "security.production.max_bible_reference_assets_per_entity": (
        32,
        "assets",
        "单个 Bible 实体一次审核可绑定的参考图绝对上限 / Maximum Bible reference assets selected per entity",
        ["security", "production", "bible", "reference"],
    ),
    "security.production.max_bible_regeneration_entities_per_request": (
        32,
        "entities",
        "单次 Bible 参考图重新生成实体绝对上限 / Maximum Bible regeneration entities per request",
        ["security", "production", "bible"],
    ),
}


SCHEMA_LIMITS_TO_REMOVE = {
    "production.recommended_max_pending": ("maximum",),
    "production.recommended_max_active_per_user": ("maximum",),
    "production.script_auto_split_trigger_chars": ("maximum",),
    "production.script_auto_split_target_chars": ("maximum",),
    "runtime.task_health_failure_limit": ("maximum",),
    "delivery.ai_label_text": ("maxLength",),
    "recommendation.vram_tiers": ("maxItems",),
}

SCHEMA_MINIMUM_TO_ONE = {
    "production.default_shot_duration_ms",
    "production.video_shot_min_duration_ms",
    "production.video_shot_max_duration_ms",
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


def configure_api_effective_limits(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions_by_key = {
        row.key: row
        for row in Definition.objects.filter(
            key__in=(set(SCHEMA_LIMITS_TO_REMOVE) | SCHEMA_MINIMUM_TO_ONE),
        )
    }
    missing_definitions = sorted(
        (set(SCHEMA_LIMITS_TO_REMOVE) | SCHEMA_MINIMUM_TO_ONE)
        - set(definitions_by_key)
    )
    if missing_definitions:
        raise RuntimeError(
            "0114 API 有效上限合同缺少配置定义: "
            + ", ".join(missing_definitions)
        )
    for key, fields in SCHEMA_LIMITS_TO_REMOVE.items():
        definition = definitions_by_key[key]
        schema = dict(definition.value_schema or {})
        for field in fields:
            schema.pop(field, None)
        schema_size, schema_sha = _contract(schema)
        Definition.objects.filter(pk=definition.pk).update(
            value_schema=schema,
            value_schema_size_bytes=schema_size,
            value_schema_sha256=schema_sha,
        )
    for key in sorted(SCHEMA_MINIMUM_TO_ONE):
        definition = definitions_by_key[key]
        schema = dict(definition.value_schema or {})
        schema["minimum"] = 1
        schema_size, schema_sha = _contract(schema)
        Definition.objects.filter(pk=definition.pk).update(
            value_schema=schema,
            value_schema_size_bytes=schema_size,
            value_schema_sha256=schema_sha,
        )

    boundary_schema = {"minimum": 1}
    schema_size, schema_sha = _contract(boundary_schema)
    boundary_definitions = {}
    for key, (value, unit, label, impact_modules) in BOUNDARIES.items():
        value_size, value_sha = _contract(value)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": (
                    label
                    + "。命中后 fail closed；提高该值会增加资源占用或攻击面。"
                ),
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
                "value_schema": boundary_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": value,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": value_size,
                "initial_value_sha256": value_sha,
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
            raise RuntimeError(f"0114 安全边界定义 {key} 与既有合同冲突")
        boundary_definitions[key] = definition

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0114 API 有效上限合同缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition",
            "credential_version",
        )
    )
    existing_keys = {row.definition.key for row in existing}
    missing_boundaries = sorted(set(BOUNDARIES) - existing_keys)
    if not missing_boundaries:
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
        change_summary="0114 收敛 API 有效上限并补充 Bible 批量安全边界",
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
            identity = (
                f"version:{row.credential_version_id}:"
                f"{row.credential_version.version}"
            )
            digest_values[row.definition.key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest(),
            }
        else:
            digest_values[row.definition.key] = row.value
    for key in missing_boundaries:
        value = BOUNDARIES[key][0]
        value_size, value_sha = _contract(value)
        rows.append(Value(
            revision=revision,
            definition=boundary_definitions[key],
            value=value,
            value_schema_version=1,
            value_size_bytes=value_size,
            value_sha256=value_sha,
        ))
        digest_values[key] = value
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
    dependencies = [("studio", "0113_text_output_field_contracts")]

    operations = [
        migrations.RunPython(
            configure_api_effective_limits,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="seriesproject",
            name="default_shot_duration_ms",
            field=models.PositiveIntegerField(
                default=5000,
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="shot",
            name="duration_ms",
            field=models.PositiveIntegerField(
                default=5000,
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="promptprofilefieldrule",
            name="max_chars",
            field=models.PositiveIntegerField(
                default=2000,
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="promptprofilefieldrule",
            name="generation_min",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="promptprofilefieldrule",
            name="generation_max",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[MinValueValidator(1)],
            ),
        ),
    ]
