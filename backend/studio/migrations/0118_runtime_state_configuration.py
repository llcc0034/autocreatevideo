import hashlib
import json

from django.db import migrations
from django.utils import timezone


SECURITY_CONFIG = {
    "security.delivery.public_distribution_enabled": (
        "boolean",
        True,
        {},
        "是否允许生成和下载带可见披露的公开分发副本 / Allow visibly disclosed public distribution copies",
        "gate",
    ),
}


STANDARD_CONFIG = {
    "runtime.readiness_generation_worker_required": (
        "boolean", True, {}, "就绪诊断是否要求生成 Worker / Require generation worker for readiness diagnostics", "flag",
    ),
    "runtime.readiness_text_generation_worker_required": (
        "boolean", True, {}, "就绪诊断是否要求文本生成 Worker / Require text-generation worker for readiness diagnostics", "flag",
    ),
    "runtime.readiness_assembly_worker_required": (
        "boolean", True, {}, "就绪诊断是否要求组装 Worker / Require assembly worker for readiness diagnostics", "flag",
    ),
    "runtime.distribution_lock_stale_seconds": (
        "integer",
        180,
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "公开分发互斥锁的陈旧回收时间 / Public-distribution stale-lock recovery interval",
        "seconds",
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


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def configure_runtime_state(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions = {}
    for key, (value_type, initial, schema, description, unit) in SECURITY_CONFIG.items():
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": value_type,
                "description": description + "。命中后 fail closed；只有超级管理员可修改。",
                "unit": unit,
                "impact_modules": ["security", "delivery", "distribution"],
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
            definition.configuration_class != "security_boundary"
            or definition.domain != "security"
            or definition.value_type != value_type
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0118 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    for key, (value_type, initial, schema, description, unit) in STANDARD_CONFIG.items():
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "runtime",
                "configuration_class": "standard",
                "value_type": value_type,
                "description": description,
                "unit": unit,
                "impact_modules": ["runtime", "readiness", "distribution"],
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
            or definition.domain != "runtime"
            or definition.value_type != value_type
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0118 标准配置定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0118 运行状态配置缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: spec[1] for key, spec in SECURITY_CONFIG.items()},
        **{key: spec[1] for key, spec in STANDARD_CONFIG.items()},
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
        change_summary="0118 收敛就绪与公开分发运行状态配置",
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
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest(),
            }
        else:
            digest_values[row.definition.key] = row.value
    for key in missing:
        value = initial_values[key]
        value_size, value_sha = _contract(value)
        rows.append(Value(
            revision=revision,
            definition=definitions[key],
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
    dependencies = [("studio", "0117_explicit_provider_execution_contracts")]

    operations = [
        migrations.RunPython(configure_runtime_state, migrations.RunPython.noop),
    ]
