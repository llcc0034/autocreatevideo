import hashlib
import json

from django.db import migrations
from django.utils import timezone


KEY = "access.throttle_signed_media_rate"
INITIAL = "600/minute"
VALUE_SCHEMA = {
    "pattern": r"^[1-9][0-9]*/(second|minute|hour|day)$",
    "maxLength": 40,
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def add_signed_media_throttle(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")

    schema_size, schema_sha = _contract(VALUE_SCHEMA)
    initial_size, initial_sha = _contract(INITIAL)
    defaults = {
        "domain": "access",
        "value_type": "string",
        "description": "签名媒体预览与下载的独立请求限流速率",
        "unit": "",
        "impact_modules": ["api", "assets", "production"],
        "restart_required": False,
        "allowed_scopes": "system",
        "allowed_default_sources": "",
        "is_required": True,
        "is_secret": False,
        "is_active": True,
        "needs_review": False,
        "review_reason": "",
        "value_schema": VALUE_SCHEMA,
        "value_schema_version": 1,
        "value_schema_size_bytes": schema_size,
        "value_schema_sha256": schema_sha,
        "initial_value": INITIAL,
        "initial_value_schema_version": 1,
        "initial_value_size_bytes": initial_size,
        "initial_value_sha256": initial_sha,
    }
    definition, created = ConfigDefinition.objects.get_or_create(
        key=KEY,
        defaults=defaults,
    )
    if not created:
        immutable = (
            "value_type",
            "restart_required",
            "allowed_scopes",
            "allowed_default_sources",
            "is_required",
            "is_secret",
            "value_schema",
            "value_schema_version",
            "value_schema_size_bytes",
            "value_schema_sha256",
            "initial_value",
            "initial_value_schema_version",
            "initial_value_size_bytes",
            "initial_value_sha256",
        )
        conflicts = [
            name for name in immutable
            if getattr(definition, name) != defaults[name]
        ]
        if conflicts or not definition.is_active:
            raise RuntimeError(
                "0092 signed media throttle definition drifted: "
                + ", ".join(conflicts or ["is_active"]),
            )

    active = ConfigRevision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    existing = {}
    if active:
        existing = {
            row.definition.key: row
            for row in ConfigValue.objects.filter(revision=active).select_related(
                "definition",
                "credential_version",
            )
        }
    if KEY in existing:
        return

    next_revision = (
        ConfigRevision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    now = timezone.now()
    revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0092 为签名媒体增加独立可配置限流，避免素材预览耗尽匿名 API 配额",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for key, row in existing.items():
        rows.append(ConfigValue(
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
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8"),
                ).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    rows.append(ConfigValue(
        revision=revision,
        definition=definition,
        value=INITIAL,
        value_schema_version=1,
        value_size_bytes=initial_size,
        value_sha256=initial_sha,
    ))
    digest_values[KEY] = INITIAL
    ConfigValue.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    if active:
        ConfigRevision.objects.filter(pk=active.pk).update(
            status="superseded",
            activation_state="inactive",
            updated_at=now,
        )
    ConfigRevision.objects.filter(pk=revision.pk).update(
        activation_state="active",
        activated_at=now,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0091_prompt_normalization_input_advisory")]

    operations = [
        migrations.RunPython(
            add_signed_media_throttle,
            migrations.RunPython.noop,
        ),
    ]
