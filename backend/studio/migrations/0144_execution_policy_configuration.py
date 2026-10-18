"""Register execution policies in the existing versioned configuration center."""
import hashlib
import json

from django.db import migrations
from django.utils import timezone


# Initial installation data, never a runtime fallback. Operators edit these
# through ConfigRevision, not by editing Python or an instance definition.
POLICIES = {
    "execution.admission.smoke_policy": ("string", "require_passed", {"enum": ["require_passed", "require_fresh"]}, "",
        "组合烟测策略：require_passed 要求精确组合真实通过，过期仅提示；require_fresh 还要求证据未过期。最近同身份失败始终阻断；不替代权限、预算与协议检查。"),
    "execution.admission.capacity_policy": ("string", "warn", {"enum": ["warn", "require_evidence"]}, "",
        "本次规格容量策略：warn 在缺少相同规格观测时提示；require_evidence 阻断没有真实覆盖证据的生产和新辅助调用。只有正式主烟测可保留告警取样；不预先认定容量通过，不放宽输入合同、权限或预算。"),
    "execution.preparation.recheck_interval_seconds": ("integer", 10, {"minimum": 1, "maximum": 300}, "seconds",
        "原平台 worker 检查待推进执行准备的间隔。只推进已授权工作，不自动批准字段或重试失败的付费调用；不是烟测有效期。"),
    "execution.preparation.claim_timeout_seconds": ("integer", 60, {"minimum": 10, "maximum": 600}, "seconds",
        "执行准备的数据库工作租约时长；过期后可由原 worker 接续。不是远端请求超时，不自动重发远端状态不确定的任务。"),
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _metadata(name, value):
    raw = _canonical(value)
    return {name: value, f"{name}_schema_version": 1, f"{name}_size_bytes": len(raw),
        f"{name}_sha256": hashlib.sha256(raw).hexdigest()}


def register_execution_policies(apps, schema_editor):
    alias = schema_editor.connection.alias
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    definitions = {}
    for key, (kind, initial, schema, unit, description) in POLICIES.items():
        defaults = {"domain": "execution", "configuration_class": "standard", "value_type": kind,
            "description": description, "unit": unit, "impact_modules": ["execution", "assets", "production", "smoke"],
            "restart_required": False, "allowed_scopes": "system", "allowed_default_sources": "",
            "is_required": True, "is_secret": False, "is_active": True, "needs_review": False, "review_reason": "",
            **_metadata("value_schema", schema), **_metadata("initial_value", initial)}
        # value_schema uses value_schema_version, not value_schema_schema_version.
        defaults["value_schema_version"] = defaults.pop("value_schema_schema_version")
        definition, created = Definition.objects.using(alias).get_or_create(key=key, defaults=defaults)
        if not created and any(getattr(definition, field) != value for field, value in defaults.items()):
            raise RuntimeError(f"0144 配置定义冲突：{key}")
        definitions[key] = definition
    active_rows = list(Revision.objects.using(alias).filter(scope_type="system", status="published", activation_state="active"))
    if len(active_rows) != 1:
        raise RuntimeError("0144 需要唯一已启用的系统配置修订")
    active = active_rows[0]
    existing = list(Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"))
    missing = sorted(set(POLICIES) - {row.definition.key for row in existing})
    if not missing:
        return
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(scope_type="system", revision=number + 1,
        status="published", activation_state="inactive", based_on_revision=active, published_at=now,
        change_summary="0144 登记统一执行准入策略与准备恢复间隔；不改变现有执行证据或任务")
    values, digest = [], {}
    for row in existing:
        values.append(Value(revision=revision, definition_id=row.definition_id, value=row.value,
            value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest[row.definition.key] = row.value if not row.credential_version_id else {
            "credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(
                f"version:{row.credential_version_id}:{row.credential_version.version}".encode("utf-8")).hexdigest(),
        }
    for key in missing:
        value = POLICIES[key][1]
        values.append(Value(revision=revision, definition=definitions[key], **_metadata("value", value)))
        digest[key] = value
    Value.objects.using(alias).bulk_create(values)
    revision.values_sha256 = hashlib.sha256(_canonical(digest)).hexdigest()
    revision.save(using=alias, update_fields=("values_sha256", "updated_at"))
    Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0143_execution_preparation_evaluation")]
    operations = [migrations.RunPython(register_execution_policies, migrations.RunPython.noop)]
