import hashlib
import json

from django.db import migrations
from django.utils import timezone


SPECS = (
    (
        "access.throttle_anon_rate", "access", "string", "60/minute",
        {"pattern": r"^[1-9][0-9]*/(second|minute|hour|day)$", "maxLength": 40},
        "匿名请求限流速率",
    ),
    (
        "access.throttle_user_rate", "access", "string", "600/minute",
        {"pattern": r"^[1-9][0-9]*/(second|minute|hour|day)$", "maxLength": 40},
        "登录用户请求限流速率",
    ),
    (
        "access.throttle_registration_rate", "access", "string", "5/hour",
        {"pattern": r"^[1-9][0-9]*/(second|minute|hour|day)$", "maxLength": 40},
        "公开注册请求限流速率",
    ),
    (
        "access.throttle_asset_upload_rate", "access", "string", "30/hour",
        {"pattern": r"^[1-9][0-9]*/(second|minute|hour|day)$", "maxLength": 40},
        "素材上传请求限流速率",
    ),
    (
        "production.h3_prompt_language_gate", "production", "string", "block",
        {"enum": ["block", "warn"]},
        "H3 导演提示词仍含待改写内容时的生产门禁策略",
    ),
)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def forwards(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")
    now = timezone.now()
    definitions = {}
    for key, domain, value_type, initial, schema, description in SPECS:
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        defaults = {
            "domain": domain,
            "value_type": value_type,
            "description": description,
            "unit": "",
            "impact_modules": ["api", "production"],
            "restart_required": False,
            "allowed_scopes": "system,project" if domain == "production" else "system",
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
        }
        definition, created = ConfigDefinition.objects.get_or_create(
            key=key, defaults=defaults,
        )
        if not created:
            immutable = (
                "value_type", "restart_required", "allowed_scopes",
                "allowed_default_sources", "is_required", "is_secret",
                "value_schema", "value_schema_version", "value_schema_size_bytes",
                "value_schema_sha256", "initial_value", "initial_value_schema_version",
                "initial_value_size_bytes", "initial_value_sha256",
            )
            conflicts = [name for name in immutable if getattr(definition, name) != defaults[name]]
            if conflicts or not definition.is_active:
                raise RuntimeError(
                    f"0050 配置定义 {key} 与既有审计合同冲突: "
                    f"{', '.join(conflicts or ['is_active'])}"
                )
        definitions[key] = (definition, initial, initial_size, initial_sha)

    active = ConfigRevision.objects.filter(
        scope_type="system", status="published", activation_state="active",
    ).order_by("-revision").first()
    existing = {}
    if active:
        existing = {
            row.definition.key: row
            for row in ConfigValue.objects.filter(revision=active).select_related(
                "definition", "credential_version"
            )
        }
    if all(key in existing for key in definitions):
        return

    next_revision = (
        ConfigRevision.objects.filter(scope_type="system").order_by("-revision")
        .values_list("revision", flat=True).first() or 0
    ) + 1
    new_revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0050 将访问限流与提示词语言策略迁入数据库",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for key, row in existing.items():
        rows.append(ConfigValue(
            revision=new_revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        if row.credential_version_id:
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    for key, (definition, initial, size, sha) in definitions.items():
        if key in existing:
            continue
        rows.append(ConfigValue(
            revision=new_revision,
            definition=definition,
            value=initial,
            value_schema_version=1,
            value_size_bytes=size,
            value_sha256=sha,
        ))
        digest_values[key] = initial
    ConfigValue.objects.bulk_create(rows)
    new_revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    new_revision.save(update_fields=("values_sha256", "updated_at"))
    if active:
        ConfigRevision.objects.filter(pk=active.pk).update(
            status="superseded", activation_state="inactive", updated_at=now,
        )
    ConfigRevision.objects.filter(pk=new_revision.pk).update(
        activation_state="active", activated_at=now, updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0049_database_runtime_configuration")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
