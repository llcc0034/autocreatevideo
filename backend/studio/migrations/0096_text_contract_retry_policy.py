import hashlib
import json

from django.db import migrations
from django.utils import timezone


KEY = "production.text_contract_retry_limit"
INITIAL_VALUE = 2
VALUE_SCHEMA = {"minimum": 0, "maximum": 5}


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


def publish_text_contract_retry_policy(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")
    schema_size, schema_sha256 = _contract(VALUE_SCHEMA)
    value_size, value_sha256 = _contract(INITIAL_VALUE)
    defaults = {
        "domain": "production",
        "value_type": "integer",
        "description": (
            "script.generate、script.structure 与 visual_rules.generate 已收到明确响应，"
            "但安全审计判定输出被截断、缺少最终文本或不符合受控 JSON 外壳时，首次"
            "调用之外允许的自动重试次数。0 表示首次此类失败后立即等待人工确认；"
            "内容过滤、不确定提交、执行前门禁、Smoke/配置漂移与审计缺失不自动重放。"
        ),
        "unit": "retries",
        "impact_modules": ["production", "text", "review"],
        "restart_required": False,
        "allowed_scopes": "system,project,run",
        "allowed_default_sources": "",
        "is_required": True,
        "is_secret": False,
        "is_active": True,
        "needs_review": False,
        "review_reason": "",
        "value_schema": VALUE_SCHEMA,
        "value_schema_version": 1,
        "value_schema_size_bytes": schema_size,
        "value_schema_sha256": schema_sha256,
        "initial_value": INITIAL_VALUE,
        "initial_value_schema_version": 1,
        "initial_value_size_bytes": value_size,
        "initial_value_sha256": value_sha256,
    }
    definition, created = ConfigDefinition.objects.get_or_create(
        key=KEY,
        defaults=defaults,
    )
    if not created:
        immutable = (
            "value_type", "restart_required", "allowed_scopes",
            "allowed_default_sources", "is_required", "is_secret",
            "value_schema", "value_schema_version", "value_schema_size_bytes",
            "value_schema_sha256", "initial_value", "initial_value_schema_version",
            "initial_value_size_bytes", "initial_value_sha256",
        )
        conflicts = [
            name for name in immutable
            if getattr(definition, name) != defaults[name]
        ]
        if conflicts or not definition.is_active:
            raise RuntimeError(
                f"0096 配置定义 {KEY} 与既有审计合同冲突: "
                f"{', '.join(conflicts or ['is_active'])}",
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
                "definition", "credential_version",
            )
        }
    if KEY in existing:
        return

    now = timezone.now()
    next_revision = (
        ConfigRevision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0096 增加通用文本响应硬合同有限重试配置",
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
        value=INITIAL_VALUE,
        value_schema_version=1,
        value_size_bytes=value_size,
        value_sha256=value_sha256,
    ))
    digest_values[KEY] = INITIAL_VALUE
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
    dependencies = [("studio", "0095_retire_workflows_without_explicit_mode")]

    operations = [
        migrations.RunPython(
            publish_text_contract_retry_policy,
            migrations.RunPython.noop,
        ),
    ]
