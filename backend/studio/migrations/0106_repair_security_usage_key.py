import hashlib
import json

from django.db import migrations
from django.utils import timezone


OLD_KEY = "security.prompt.max_usage_tokens"
NEW_KEY = "security.prompt.max_usage_count"


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def repair_usage_key(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    old = Definition.objects.filter(key=OLD_KEY).first()
    if old is None:
        return
    if Definition.objects.filter(key=NEW_KEY).exists():
        raise RuntimeError("0106 安全用量键修复发现新旧定义并存")
    schema = dict(old.value_schema or {})
    schema_bytes = _canonical(schema)
    initial_bytes = _canonical(old.initial_value)
    new = Definition.objects.create(
        key=NEW_KEY,
        domain=old.domain,
        configuration_class=old.configuration_class,
        value_type=old.value_type,
        description=(
            "Provider 用量计数可接受绝对上限 / Maximum accepted provider usage "
            "count。命中后 fail closed；提高该值会增加资源占用或攻击面。"
        ),
        unit="units",
        impact_modules=old.impact_modules,
        restart_required=old.restart_required,
        allowed_scopes=old.allowed_scopes,
        allowed_default_sources=old.allowed_default_sources,
        is_required=True,
        is_secret=False,
        is_active=True,
        needs_review=False,
        review_reason="",
        value_schema=schema,
        value_schema_version=old.value_schema_version,
        value_schema_size_bytes=len(schema_bytes),
        value_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        initial_value=old.initial_value,
        initial_value_schema_version=old.initial_value_schema_version,
        initial_value_size_bytes=len(initial_bytes),
        initial_value_sha256=hashlib.sha256(initial_bytes).hexdigest(),
    )
    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0106 安全用量键修复缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    old_value = next((row for row in existing if row.definition_id == old.id), None)
    if old_value is None:
        raise RuntimeError("0106 安全用量键修复缺少旧活动值")
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
        change_summary="0106 修复会被秘密字段检测误判的安全用量配置键",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for row in existing:
        if row.definition_id == old.id:
            definition_id = new.id
            key = NEW_KEY
        else:
            definition_id = row.definition_id
            key = row.definition.key
        rows.append(Value(
            revision=revision,
            definition_id=definition_id,
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
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8"),
                ).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Definition.objects.filter(pk=old.pk).update(
        is_active=False,
        is_required=False,
        needs_review=False,
        review_reason="0106 已由 security.prompt.max_usage_count 取代",
        updated_at=now,
    )
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
    dependencies = [("studio", "0105_security_boundary_configuration")]

    operations = [
        migrations.RunPython(repair_usage_key, migrations.RunPython.noop),
    ]
