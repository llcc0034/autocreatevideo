"""Replace obsolete seed configuration, never convert authored AI routes.

Authored incompatible revisions and their references must be removed by the
reviewed cutover before migrating. Only untouched installation seed revisions
may be discarded here. No historical checksum or running task is rewritten.
"""
import hashlib
import json

from django.db import migrations
from django.utils import timezone


OBSOLETE_KEYS = (
    "production.prompt_normalization_deployment_id",
    "production.prompt_language_gate",
    "runtime.deferred_generation_retry_backoff_seconds",
)
RECOMMENDATION_KEY = "production.prompt_normalization_execution_instance_version_id"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _metadata(field, value):
    raw = _canonical(value)
    version_field = "value_schema_version" if field == "value_schema" else f"{field}_schema_version"
    return {field: value, version_field: 1, f"{field}_size_bytes": len(raw),
        f"{field}_sha256": hashlib.sha256(raw).hexdigest()}


def replace_obsolete_configuration(apps, schema_editor):
    alias = schema_editor.connection.alias
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    affected = set(Value.objects.using(alias).filter(
        definition__key__in=OBSOLETE_KEYS).values_list("revision_id", flat=True))
    while True:
        derived = set(Revision.objects.using(alias).filter(
            based_on_revision_id__in=affected).values_list("pk", flat=True))
        if derived <= affected:
            break
        affected.update(derived)
    revisions = Revision.objects.using(alias).filter(pk__in=affected)
    if revisions.exclude(scope_type="system", created_by_id=None).exists():
        raise RuntimeError("0166 请先按精确清单清理含旧 AI 配置的人工修订及其关联；迁移不转换历史路由")
    for relation in Revision._meta.related_objects:
        model = relation.related_model
        if model in (Revision, Value):
            continue
        if model.objects.using(alias).filter(**{f"{relation.field.name}_id__in": affected}).exists():
            raise RuntimeError("0166 旧配置修订仍有业务或审计引用，请先执行核定清理")
    active = list(Revision.objects.using(alias).filter(
        scope_type="system", status="published", activation_state="active"))
    if len(active) != 1:
        raise RuntimeError("0166 需要唯一有效的系统配置基线")
    active = active[0]
    existing = list(Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"))
    old_digest, retained_digest, retained = {}, {}, []
    for row in existing:
        if row.definition.is_secret:
            # The formal configuration writer stores only the immutable
            # credential reference, not a JSON null contract or plaintext.
            valid = (row.credential_version_id is not None and row.value is None
                and row.value_schema_version >= 1 and row.value_size_bytes == 0 and row.value_sha256 == "")
        else:
            raw = _canonical(row.value)
            valid = (row.credential_version_id is None and row.value_schema_version >= 1
                and row.value_size_bytes == len(raw) and row.value_sha256 == hashlib.sha256(raw).hexdigest())
        if not valid:
            raise RuntimeError(f"0166 原配置值摘要不一致：{row.definition.key}；不能作为新配置来源")
        digest_value = row.value if not row.credential_version_id else {
            "credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(
                f"version:{row.credential_version_id}:{row.credential_version.version}".encode("utf-8")).hexdigest(),
        }
        old_digest[row.definition.key] = digest_value
        if row.definition.key not in OBSOLETE_KEYS:
            retained.append(row)
            retained_digest[row.definition.key] = digest_value
    if active.values_sha256 != hashlib.sha256(_canonical(old_digest)).hexdigest():
        raise RuntimeError("0166 原配置整版摘要不一致，不能静默重算")
    schema = {"maxLength": 36, "resource_ref": {
        "resource": "execution_instance_version", "capability": "prompt.normalize", "allow_empty": True,
    }}
    definition, created = Definition.objects.using(alias).get_or_create(key=RECOMMENDATION_KEY, defaults={
        "domain": "production", "configuration_class": "standard", "value_type": "string",
        "description": "提示词辅助的默认推荐版本；空值表示未推荐。只供明确选择，不等于烟测通过，不在 worker 中自动替换目标。实际调用以已确认的精确执行版本及 Preparation 为准。",
        "impact_modules": ["execution", "production"], "restart_required": False,
        "allowed_scopes": "system,project,run", "allowed_default_sources": "",
        "is_required": False, "is_secret": False, "is_active": True,
        "needs_review": False, "review_reason": "",
        **_metadata("value_schema", schema), **_metadata("initial_value", ""),
    })
    if not created:
        raise RuntimeError("0166 新配置键已经存在，请核查迁移状态，不覆盖现有定义")
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(
        scope_type="system", revision=number + 1, status="published", activation_state="inactive",
        based_on_revision_id=None if active.pk in affected else active.pk,
        published_at=now,
        change_summary="0166 登记精确执行实例推荐；移除旧安装配置，不转换旧 Deployment、不产生烟测证据",
    )
    values = [Value(revision=revision, definition_id=row.definition_id, value=row.value,
        value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
        value_sha256=row.value_sha256, credential_version_id=row.credential_version_id) for row in retained]
    values.append(Value(revision=revision, definition=definition, **_metadata("value", "")))
    retained_digest[RECOMMENDATION_KEY] = ""
    Value.objects.using(alias).bulk_create(values)
    revision.values_sha256 = hashlib.sha256(_canonical(retained_digest)).hexdigest()
    revision.save(using=alias, update_fields=("values_sha256", "updated_at"))
    # Only the verified, unreferenced seed closure is removed, not arbitrary
    # project settings, tasks, audit rows, or valid shared resources.
    revisions.update(based_on_revision=None)
    revisions.delete()
    Definition.objects.using(alias).filter(key__in=OBSOLETE_KEYS).delete()
    if active.pk not in affected:
        Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0165_remove_legacy_workflow_source_target")]
    operations = [migrations.RunPython(replace_obsolete_configuration, migrations.RunPython.noop)]
