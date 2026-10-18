"""Retire the replaced fixed-script estimator; never rewrite stored requests."""
import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


KEY = "production.series_concept_resource_advisory"


def forward(apps, schema_editor):
    canonical = importlib.import_module("studio.migrations.0177_system_resource_review_policy").canonical
    alias = schema_editor.connection.alias
    Definition, Revision, Value = (apps.get_model("studio", name)
        for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    Definition.objects.using(alias).filter(key=KEY).update(is_active=False, is_required=False,
        description="固定格式剧本估算器已移除，不再读取此配置。新请求使用确切输出合同和系统输出额度。")
    affected = Value.objects.using(alias).filter(definition__key=KEY).values_list("revision_id", flat=True)
    active = list(Revision.objects.using(alias).filter(pk__in=affected,
        scope_type__in=("system", "project"), status="published", activation_state="active"))
    for previous in active:
        scope = {"scope_type": previous.scope_type, "project_id": previous.project_id, "run_id": previous.run_id}
        number = Revision.objects.using(alias).filter(**scope).order_by("-revision").values_list("revision", flat=True).first()
        now = timezone.now()
        successor = Revision.objects.using(alias).create(**scope, revision=number + 1,
            status="published", activation_state="inactive", based_on_revision=previous, published_at=now,
            change_summary="移除已被可配置剧本合同替代的固定格式资源估算设置")
        digest, values = {}, []
        for row in Value.objects.using(alias).filter(revision=previous).exclude(
                definition__key=KEY).select_related("definition", "credential_version"):
            values.append(Value(revision=successor, definition_id=row.definition_id, value=row.value,
                value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
                value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
            digest[row.definition.key] = row.value if not row.credential_version_id else {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    f"version:{row.credential_version_id}:{row.credential_version.version}".encode()).hexdigest()}
        Value.objects.using(alias).bulk_create(values)
        Revision.objects.using(alias).filter(pk=successor.pk).update(
            values_sha256=hashlib.sha256(canonical(digest)).hexdigest())
        Revision.objects.using(alias).filter(pk=previous.pk).update(
            status="superseded", activation_state="inactive", updated_at=now)
        Revision.objects.using(alias).filter(pk=successor.pk).update(
            activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0213_script_revision_structure")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
