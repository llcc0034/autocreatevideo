import hashlib
import json

from django.db import migrations
from django.utils import timezone


RENAMES = {
    "security.prompt_normalization.output_max_tokens": (
        "security.prompt_normalization.output_max_units",
        "规范化输出 token 绝对上限 / Absolute normalization output-token limit",
        "tokens",
    ),
    "security.secret.max_bytes": (
        "security.managed_value.max_plaintext_bytes",
        "受管秘密正文最大字节数 / Maximum managed-secret bytes",
        "bytes",
    ),
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def repair_sensitive_keys(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    old_definitions = {
        key: Definition.objects.filter(key=key, is_active=True).first()
        for key in RENAMES
    }
    old_definitions = {
        key: row for key, row in old_definitions.items() if row is not None
    }
    if not old_definitions:
        return
    if Definition.objects.filter(
        key__in=[RENAMES[key][0] for key in old_definitions],
    ).exists():
        raise RuntimeError("0107 安全键修复发现新旧定义并存")
    replacements = {}
    for old_key, old in old_definitions.items():
        new_key, description, unit = RENAMES[old_key]
        schema_bytes = _canonical(old.value_schema or {})
        initial_bytes = _canonical(old.initial_value)
        replacements[old.id] = Definition.objects.create(
            key=new_key,
            domain=old.domain,
            configuration_class=old.configuration_class,
            value_type=old.value_type,
            description=(
                description
                + "。命中后 fail closed；提高该值会增加资源占用或攻击面。"
            ),
            unit=unit,
            impact_modules=old.impact_modules,
            restart_required=old.restart_required,
            allowed_scopes=old.allowed_scopes,
            allowed_default_sources=old.allowed_default_sources,
            is_required=True,
            is_secret=False,
            is_active=True,
            needs_review=False,
            review_reason="",
            value_schema=old.value_schema,
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
        raise RuntimeError("0107 安全键修复缺少 active system revision")
    existing = list(Value.objects.filter(revision=active).select_related(
        "definition", "credential_version",
    ))
    missing_values = set(replacements) - {row.definition_id for row in existing}
    if missing_values:
        raise RuntimeError("0107 安全键修复缺少旧活动值")
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
        change_summary="0107 修复会被秘密字段检测误判的安全配置键",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for row in existing:
        replacement = replacements.get(row.definition_id)
        definition_id = replacement.id if replacement else row.definition_id
        key = replacement.key if replacement else row.definition.key
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
    Definition.objects.filter(pk__in=replacements).update(
        is_active=False,
        is_required=False,
        needs_review=False,
        review_reason="0107 已由不含敏感字段名的安全配置键取代",
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
    dependencies = [("studio", "0106_repair_security_usage_key")]

    operations = [
        migrations.RunPython(repair_sensitive_keys, migrations.RunPython.noop),
    ]
