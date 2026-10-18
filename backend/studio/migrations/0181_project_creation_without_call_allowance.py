"""Remove the redundant project creation allowance from the active contract.

Old revision bytes remain audit facts, not accepted current input formats.
"""
from copy import deepcopy
import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


def forward(apps, schema_editor):
    helper = importlib.import_module("studio.migrations.0177_system_resource_review_policy")
    alias = schema_editor.connection.alias
    Definition, Revision, Value = (apps.get_model("studio", name) for name in ("ConfigDefinition", "ConfigRevision", "ConfigValue"))
    key = "production.project_creation_policy"
    definition = Definition.objects.using(alias).get(key=key)
    schema, initial = deepcopy(definition.value_schema), deepcopy(definition.initial_value)
    for value in (initial,):
        value.pop("default_max_generation_tasks")
        value["schema_version"] = 3
    schema["properties"].pop("default_max_generation_tasks")
    schema["required"].remove("default_max_generation_tasks")
    schema["security_ceiling_paths"].pop("default_max_generation_tasks")
    schema["properties"]["schema_version"]["enum"] = [3]
    fields = {**helper.metadata("value_schema", schema), **helper.metadata("initial_value", initial)}
    fields.pop("value_schema_schema_version")
    fields["value_schema_version"] = 3
    fields["initial_value_schema_version"] = 3
    Definition.objects.using(alias).filter(pk=definition.pk).update(**fields)
    active = Revision.objects.using(alias).get(scope_type="system", status="published", activation_state="active")
    number = Revision.objects.using(alias).filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first()
    now = timezone.now()
    revision = Revision.objects.using(alias).create(scope_type="system", revision=number + 1, status="published",
        activation_state="inactive", based_on_revision=active, published_at=now,
        change_summary="移除项目创建调用次数设置，资源额度仅由统一系统策略提供")
    digest, values = {}, []
    for row in Value.objects.using(alias).filter(revision=active).select_related("definition", "credential_version"):
        data = row.value
        if row.definition.key == key:
            data = deepcopy(data)
            data.pop("default_max_generation_tasks")
            data["schema_version"] = 3
            metadata = helper.metadata("value", data)
            metadata["value_schema_version"] = 3
            values.append(Value(revision=revision, definition_id=row.definition_id, **metadata))
        else:
            values.append(Value(revision=revision, definition_id=row.definition_id, value=data,
                value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
                value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest[row.definition.key] = data if not row.credential_version_id else {"credential_configured": True,
            "credential_binding_sha256": hashlib.sha256(f"version:{row.credential_version_id}:{row.credential_version.version}".encode()).hexdigest()}
    Value.objects.using(alias).bulk_create(values)
    Revision.objects.using(alias).filter(pk=revision.pk).update(values_sha256=hashlib.sha256(helper.canonical(digest)).hexdigest())
    Revision.objects.using(alias).filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.using(alias).filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0180_remove_retired_resource_allowances")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
