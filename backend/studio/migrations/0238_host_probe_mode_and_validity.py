"""Versioned host-probe scheduling; existing snapshots and provider Smoke stay unchanged."""
import hashlib
import json

from django.db import migrations
from django.utils import timezone


SETTINGS = {
    "runtime.host_probe_mode": (
        "string", "automatic", "mode",
        {"enum": ["automatic", "manual"], "enum_labels": {"automatic": "自动探测", "manual": "手动探测"}},
        "主机探测模式：自动按快照到期时间提前刷新；手动只通过探测按钮启动。发布后影响新调度，不中断已排队或运行的探测；两种模式均保留正常过期、失败和文件匹配检查。",
    ),
    "runtime.capability_ttl_seconds": (
        "integer", 2_592_000, "seconds", {"minimum": 30, "maximum": 31_536_000},
        "自动主机探测有效期，默认2592000秒（30天）。系统值用于新主机快照 expires_at；改值不改写已有快照，不影响模型部署及组合烟测有效期。",
    ),
    "runtime.host_probe_manual_validity_days": (
        "integer", 36_500, "days", {"minimum": 1, "maximum": 36_500},
        "手动主机探测的长期有效期，默认36500天（约100年），不是跳过过期检查。切换手动后点击探测生成长期快照；改值或切回自动不改写旧结果，需再次探测应用新期限。",
    ),
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def contract_fields(name, value):
    payload = canonical(value)
    return {name: value, f"{name}_schema_version": 1, f"{name}_size_bytes": len(payload),
            f"{name}_sha256": hashlib.sha256(payload).hexdigest()}


def forward(apps, schema_editor):
    alias = schema_editor.connection.alias
    Definition, Revision, Value = (apps.get_model("studio", name) for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    definitions = {}
    for key, (value_type, initial, unit, schema, description) in SETTINGS.items():
        fields = dict(domain="runtime", configuration_class="standard", value_type=value_type, unit=unit,
            description=description, impact_modules=["runtime", "host_probe"], restart_required=False,
            allowed_default_sources="", is_active=True, is_required=True, is_secret=False,
            needs_review=False, review_reason="", **contract_fields("initial_value", initial),
            value_schema=schema, value_schema_version=1, value_schema_size_bytes=len(canonical(schema)),
            value_schema_sha256=hashlib.sha256(canonical(schema)).hexdigest())
        definition, created = Definition.objects.using(alias).get_or_create(key=key,
            defaults={**fields, "allowed_scopes": "system"})
        if not created:
            Definition.objects.using(alias).filter(pk=definition.pk).update(**fields)
        definitions[key] = definition
    active = Revision.objects.using(alias).get(scope_type="system", status="published", activation_state="active")
    existing = list(Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"))
    # Once mode is installed, a repeated migration must preserve subsequent user choices.
    if any(row.definition.key == "runtime.host_probe_mode" for row in existing):
        return
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(scope_type="system", revision=number + 1, status="published",
        activation_state="inactive", based_on_revision=active, published_at=now,
        change_summary="主机探测新增自动/手动模式；自动默认30天、手动默认36500天；不改变历史快照或烟测")
    values, digest = [], {}
    for row in existing:
        if row.definition.key in SETTINGS:
            continue
        values.append(Value(revision=revision, definition_id=row.definition_id, value=row.value,
            value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest[row.definition.key] = row.value if not row.credential_version_id else {"credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(f"version:{row.credential_version_id}:{row.credential_version.version}".encode()).hexdigest()}
    for key, (_, initial, _, _, _) in SETTINGS.items():
        values.append(Value(revision=revision, definition_id=definitions[key].pk, **contract_fields("value", initial)))
        digest[key] = initial
    Value.objects.using(alias).bulk_create(values)
    Revision.objects.using(alias).filter(pk=revision.pk).update(values_sha256=hashlib.sha256(canonical(digest)).hexdigest())
    Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0237_unify_production_retry_policy")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
