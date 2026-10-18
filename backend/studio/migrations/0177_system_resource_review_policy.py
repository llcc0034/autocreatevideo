"""Install a versioned system policy without changing historical run facts."""
import hashlib
import json

from django.db import migrations
from django.utils import timezone


KEY = "execution.resources.review_policy"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def metadata(name, value):
    raw = canonical(value)
    return {name: value, f"{name}_schema_version": 1, f"{name}_size_bytes": len(raw), f"{name}_sha256": hashlib.sha256(raw).hexdigest()}


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    Definition, Revision, Value = (apps.get_model("studio", name) for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    active = list(Revision.objects.using(alias).filter(scope_type="system", status="published", activation_state="active"))
    if len(active) != 1:
        raise RuntimeError("资源策略迁移需要唯一启用的系统配置")
    active = active[0]
    rows = list(Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"))
    prior = {row.definition.key: row.value for row in rows}
    calls = prior.get("production.max_generation_tasks")
    if type(calls) is not int or calls < 1:
        raise RuntimeError("现有系统调用次数配置无效，不能猜测迁移值")
    # No invented monetary consent: unconfigured currencies require explicit
    # acknowledgement of the concrete estimate. Initial token warning is an
    # installation value, not a provider capacity declaration/runtime fallback.
    initial = {"budget_by_currency": {}, "call_count": calls, "output_length": 4096}
    schema = {"type": "object", "additionalProperties": False,
        "required": ["budget_by_currency", "call_count", "output_length"], "properties": {
            "budget_by_currency": {"type": "object", "propertyNames": {"pattern": "^[A-Z][A-Z0-9]{2,11}$"},
                "additionalProperties": {"type": "string", "pattern": r"^(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,4})?$"}},
            "call_count": {"type": "integer", "minimum": 1}, "output_length": {"type": "integer", "minimum": 1}}}
    fields = {"domain": "execution", "configuration_class": "standard", "value_type": "object",
        "description": "系统资源提醒：budget_by_currency 按币种设置每轮累计费用提醒；未配置币种需人工确认。call_count 为每轮主/辅助/重试调用次数提醒；output_length 为新建原生文本请求的输出额度及已有请求的额度提醒，单位token（不是模型物理上限）。超额保留任务，人工确认后另行继续。动态生效，不改写已保存请求。",
        "unit": "", "impact_modules": ["execution", "production", "assets", "smoke"], "restart_required": False,
        "allowed_scopes": "system", "allowed_default_sources": "", "is_required": True, "is_secret": False,
        "is_active": True, "needs_review": False, "review_reason": "", **metadata("value_schema", schema), **metadata("initial_value", initial)}
    fields["value_schema_version"] = fields.pop("value_schema_schema_version")
    definition = Definition.objects.using(alias).create(key=KEY, **fields)
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(scope_type="system", revision=number + 1, status="published",
        activation_state="inactive", based_on_revision=active, published_at=now,
        change_summary="统一系统资源提醒；旧任务参数及账目不变，超额需人工确认")
    values, digest = [], {}
    for row in rows:
        if row.definition.key == "production.max_generation_tasks":
            continue
        values.append(Value(revision=revision, definition_id=row.definition_id, value=row.value,
            value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest[row.definition.key] = row.value if not row.credential_version_id else {"credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(f"version:{row.credential_version_id}:{row.credential_version.version}".encode()).hexdigest()}
    values.append(Value(revision=revision, definition=definition, **metadata("value", initial)))
    digest[KEY] = initial
    Value.objects.using(alias).bulk_create(values)
    Definition.objects.using(alias).filter(key="production.max_generation_tasks").update(
        is_active=False, is_required=False, description="历史调用次数配置；现由 execution.resources.review_policy 统一管理")
    revision.values_sha256 = hashlib.sha256(canonical(digest)).hexdigest()
    revision.save(using=alias, update_fields=("values_sha256", "updated_at"))
    Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0176_execution_resource_confirmation")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
