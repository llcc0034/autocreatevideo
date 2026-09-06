import hashlib
import json

from django.db import migrations
from django.utils import timezone


OLD_KEY = "production.h3_prompt_language_gate"
NEW_KEY = "production.prompt_language_gate"
OLD_DESCRIPTION = "H3 导演提示词仍含待改写内容时的生产门禁策略"
NEW_DESCRIPTION = "生产提示词仍含待改写内容时的通用语言门禁策略"
VALUE_SCHEMA = {"enum": ["block", "warn"]}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _credential_digest(version):
    if version is None:
        raise RuntimeError("0075 配置 revision 的密钥引用缺失")
    identity = f"version:{version.id}:{version.version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _revision_digest(ConfigValue, revision_id):
    values = {}
    rows = ConfigValue.objects.filter(revision_id=revision_id).select_related(
        "definition", "credential_version",
    )
    for row in rows:
        definition = row.definition
        key = definition.key
        if key in values:
            raise RuntimeError(f"0075 配置 revision 出现重复 key: {key}")
        if definition.is_secret:
            if row.value is not None:
                raise RuntimeError(f"0075 密钥配置 {key} 意外保存了明文值")
            values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": _credential_digest(
                    row.credential_version,
                ),
            }
            continue
        encoded = _canonical(row.value)
        if (
            row.value_size_bytes != len(encoded)
            or row.value_sha256 != hashlib.sha256(encoded).hexdigest()
        ):
            raise RuntimeError(f"0075 配置值 {key} 的不可变合同校验失败")
        values[key] = row.value
    return hashlib.sha256(_canonical(values)).hexdigest()


def _validate_definition(definition, source_key):
    expected = {
        "domain": "production",
        "value_type": "string",
        "allowed_scopes": "system,project",
        "is_required": True,
        "is_secret": False,
        "is_active": True,
        "value_schema": VALUE_SCHEMA,
        "initial_value": "block",
    }
    conflicts = [
        name for name, value in expected.items()
        if getattr(definition, name) != value
    ]
    if conflicts:
        raise RuntimeError(
            f"0075 配置定义 {source_key} 与发布合同冲突: "
            f"{', '.join(conflicts)}"
        )


def _rename_definition(apps, *, source_key, target_key, description):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")

    source = ConfigDefinition.objects.filter(key=source_key).first()
    target = ConfigDefinition.objects.filter(key=target_key).first()
    if source is not None and target is not None:
        raise RuntimeError(
            f"0075 同时存在 {source_key} 与 {target_key}，"
            "无法判断权威配置值；请先人工消歧。"
        )
    if source is None:
        raise RuntimeError(f"0075 缺少待重命名配置定义 {source_key}")
    _validate_definition(source, source_key)

    active_system = ConfigRevision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).first()
    if active_system is None:
        raise RuntimeError("0075 缺少 active published system 配置 revision")
    active_value = ConfigValue.objects.filter(
        revision_id=active_system.pk,
        definition_id=source.pk,
    ).first()
    if active_value is None or active_value.value not in {"block", "warn"}:
        raise RuntimeError(
            f"0075 当前已发布配置缺少有效 {source_key} 值"
        )

    affected_revision_ids = list(
        ConfigValue.objects.filter(definition_id=source.pk)
        .values_list("revision_id", flat=True)
        .distinct()
    )
    before_values = {
        row.id: (
            row.value,
            row.value_schema_version,
            row.value_size_bytes,
            row.value_sha256,
            row.credential_version_id,
        )
        for row in ConfigValue.objects.filter(definition_id=source.pk)
    }
    for revision in ConfigRevision.objects.filter(id__in=affected_revision_ids):
        digest = _revision_digest(ConfigValue, revision.id)
        if digest != revision.values_sha256:
            raise RuntimeError(
                f"0075 配置 revision {revision.id} 在重命名前校验和不一致"
            )

    now = timezone.now()
    ConfigDefinition.objects.filter(pk=source.pk).update(
        key=target_key,
        description=description,
        updated_at=now,
    )
    for revision in ConfigRevision.objects.filter(id__in=affected_revision_ids):
        ConfigRevision.objects.filter(pk=revision.pk).update(
            values_sha256=_revision_digest(ConfigValue, revision.id),
            updated_at=now,
        )

    after_values = {
        row.id: (
            row.value,
            row.value_schema_version,
            row.value_size_bytes,
            row.value_sha256,
            row.credential_version_id,
        )
        for row in ConfigValue.objects.filter(definition_id=source.pk)
    }
    if before_values != after_values:
        raise RuntimeError(
            f"0075 重命名 {source_key} 时配置值发生了非预期变化"
        )


def forwards(apps, schema_editor):
    del schema_editor
    _rename_definition(
        apps,
        source_key=OLD_KEY,
        target_key=NEW_KEY,
        description=NEW_DESCRIPTION,
    )


def backwards(apps, schema_editor):
    del schema_editor
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    if not ConfigDefinition.objects.filter(
        key__in=(OLD_KEY, NEW_KEY),
    ).exists():
        # TransactionTestCase flushes can legitimately leave a migration state
        # without seeded rows. There is then no persisted configuration to
        # restore during an unapply operation.
        return
    _rename_definition(
        apps,
        source_key=NEW_KEY,
        target_key=OLD_KEY,
        description=OLD_DESCRIPTION,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0074_generic_runtime_contracts")]

    operations = [migrations.RunPython(forwards, backwards)]
