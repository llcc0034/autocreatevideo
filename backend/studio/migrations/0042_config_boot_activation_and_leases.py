import hashlib
import json

from django.db import migrations, models
from django.utils import timezone


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def bootstrap_system_config(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")

    additions = (
        (
            "production.default_resolution_width",
            "默认生产分辨率宽度",
            {"minimum": 64, "maximum": 8192},
            576,
            "pixels",
        ),
        (
            "production.default_resolution_height",
            "默认生产分辨率高度",
            {"minimum": 64, "maximum": 8192},
            1024,
            "pixels",
        ),
        (
            "production.default_concurrency",
            "默认生产并发数",
            {"minimum": 1, "maximum": 32},
            1,
            "workers",
        ),
    )
    for key, description, schema, initial, unit in additions:
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        ConfigDefinition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "production",
                "value_type": "integer",
                "description": description,
                "unit": unit,
                "impact_modules": ["production", "recommendation"],
                "allowed_scopes": "system,project,run",
                "allowed_default_sources": "model,workflow",
                "is_required": False,
                "is_secret": False,
                "is_active": True,
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

    published_exists = ConfigRevision.objects.filter(
        scope_type="system",
        status="published",
    ).exists()
    if published_exists:
        return

    definitions = list(ConfigDefinition.objects.filter(is_active=True).order_by("key"))
    missing_required = []
    values = {}
    for definition in definitions:
        if definition.is_secret or definition.initial_value is None:
            if definition.is_required:
                missing_required.append(definition)
            continue
        values[definition.key] = definition.initial_value
    for definition in missing_required:
        definition.needs_review = True
        definition.review_reason = "required 配置缺少 initial_value，未生成默认值"
        definition.save(update_fields=("needs_review", "review_reason", "updated_at"))

    now = timezone.now()
    next_revision = (
        ConfigRevision.objects.filter(scope_type="system")
        .aggregate(value=models.Max("revision"))["value"]
        or 0
    ) + 1
    revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        activation_state="active",
        activated_at=now,
        published_at=now,
        change_summary="0042 从 ConfigDefinition.initial_value 构造产品默认配置",
    )
    digest_values = {}
    rows = []
    by_key = {item.key: item for item in definitions}
    for key, value in values.items():
        size, digest = _contract(value)
        digest_values[key] = value
        rows.append(ConfigValue(
            revision=revision,
            definition=by_key[key],
            value=value,
            value_schema_version=1,
            value_size_bytes=size,
            value_sha256=digest,
            credential_ref="",
        ))
    ConfigValue.objects.bulk_create(rows)
    revision.values_sha256 = _contract(digest_values)[1]
    revision.save(update_fields=("values_sha256", "updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0041_async_rest_media_runtime"),
    ]

    operations = [
        migrations.AddField(
            model_name="configdefinition",
            name="needs_review",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="configdefinition",
            name="review_reason",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="configrevision",
            name="activation_requested_boot_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="workerlease",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workerlease",
            name="holder_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(bootstrap_system_config, migrations.RunPython.noop),
    ]
