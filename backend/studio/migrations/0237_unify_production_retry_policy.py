"""One configured total-attempt budget for production primary and field calls.

Only active configuration receives successors. Frozen Runs and historical
revisions retain their original values and hashes.
"""
import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


def forward(apps, schema_editor):
    alias = schema_editor.connection.alias
    canonical = importlib.import_module("studio.migrations.0177_system_resource_review_policy").canonical
    Definition, Revision, Value = (apps.get_model("studio", name) for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    retired = "production.prompt_field_retry_limit"
    Definition.objects.using(alias).filter(key=retired).update(is_active=False, is_required=False,
        description="旧字段重试设置已移除；配方主调用与辅助字段统一使用 production.task_max_attempts。历史快照不改写。")
    Definition.objects.using(alias).filter(key="production.task_max_attempts").update(
        description="配方每个主请求及辅助字段批次的最大总尝试次数（含首次，1表示不自动重试）。随Run配置冻结；明确失败按配置重试，耗尽转人工。结果未知、已有可用响应或产物不自动重发。")
    Definition.objects.using(alias).filter(key="execution.preparation.recheck_interval_seconds").update(
        description="原worker检查待推进执行准备的间隔；配方也将其冻结为失败后自动重试的最小等待秒数，不保证精确启动时间。不自动批准字段，不替代烟测有效期。")
    active_ids = Value.objects.using(alias).filter(definition__key=retired,
        revision__status="published", revision__activation_state="active").values_list("revision_id", flat=True)
    for active in list(Revision.objects.using(alias).filter(pk__in=active_ids)):
        scope = dict(scope_type=active.scope_type, project_id=active.project_id, run_id=active.run_id)
        number = Revision.objects.using(alias).filter(**scope).order_by("-revision").values_list("revision", flat=True).first()
        now = timezone.now()
        revision = Revision.objects.using(alias).create(**scope, revision=number + 1, status="published",
            activation_state="inactive", based_on_revision=active, published_at=now,
            change_summary="主调用与字段辅助统一使用总尝试次数；移除不再读取的独立字段重试键")
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
        Revision.objects.using(alias).filter(pk=revision.pk).update(values_sha256=hashlib.sha256(canonical(digest)).hexdigest())
        Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
        Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0236_comfy_workflow_preview_receipt")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
