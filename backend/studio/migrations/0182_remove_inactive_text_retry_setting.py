"""Remove the unused automatic text retry setting from the current path."""
import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


def forward(apps, schema_editor):
    helper = importlib.import_module("studio.migrations.0177_system_resource_review_policy")
    alias = schema_editor.connection.alias
    Definition, Revision, Value = (apps.get_model("studio", name) for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    retired = "production.text_contract_retry_limit"
    Definition.objects.using(alias).filter(key=retired).update(is_active=False, is_required=False,
        description="已移除的自动文本重试设置；当前文本响应需人工处理，不读取此键。")
    Definition.objects.using(alias).filter(key="execution.resources.review_policy").update(
        description="系统资源提醒：budget_by_currency 按币种设置每轮累计费用提醒；未配置币种需人工确认。call_count 为每轮主/辅助/重试调用次数提醒；output_length 为新建文本请求（含字段辅助）的输出额度及已有请求的额度提醒，单位token（不是模型物理上限）。超额保留任务，人工确认后另行继续。动态生效，不改写已保存请求。")
    active = Revision.objects.using(alias).get(scope_type="system", status="published", activation_state="active")
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(scope_type="system", revision=number + 1, status="published",
        activation_state="inactive", based_on_revision=active, published_at=now,
        change_summary="移除失效的自动文本重试设置，明确所有文本请求统一使用系统输出额度")
    digest, values = {}, []
    for row in Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"):
        if row.definition.key == retired:
            continue
        values.append(Value(revision=revision, definition_id=row.definition_id, value=row.value,
            value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest[row.definition.key] = row.value if not row.credential_version_id else {"credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(f"version:{row.credential_version_id}:{row.credential_version.version}".encode()).hexdigest()}
    Value.objects.using(alias).bulk_create(values)
    Revision.objects.using(alias).filter(pk=revision.pk).update(values_sha256=hashlib.sha256(helper.canonical(digest)).hexdigest())
    Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0181_project_creation_without_call_allowance")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
