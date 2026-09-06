import hashlib
import json

from django.db import migrations
from django.utils import timezone


CONFIG_KEY = "workflow.comfyui_saved_workflow_directory"
INITIAL_VALUE = "workflows"
VALUE_SCHEMA = {
    "minLength": 9,
    "maxLength": 500,
    "pattern": r"workflows(?:/(?!\.{1,2}(?:/|$))[^/\\:%\x00-\x1f\x7f]+)*",
    "format": "comfyui-saved-workflow-directory",
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


def configure_saved_workflow_directory(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    schema_size, schema_sha = _contract(VALUE_SCHEMA)
    value_size, value_sha = _contract(INITIAL_VALUE)
    definition, created = Definition.objects.get_or_create(
        key=CONFIG_KEY,
        defaults={
            "domain": "workflow",
            "configuration_class": "standard",
            "value_type": "string",
            "description": (
                "ComfyUI userdata 中允许递归扫描已保存工作流的目录；"
                "只能配置 workflows 或其子目录，不是本机绝对路径"
                " / ComfyUI userdata-relative saved-workflow directory"
            ),
            "unit": "userdata relative path",
            "impact_modules": ["workflow", "comfyui", "import"],
            "restart_required": False,
            "allowed_scopes": "system",
            "allowed_default_sources": "",
            "is_required": True,
            "is_secret": False,
            "is_active": True,
            "needs_review": False,
            "review_reason": "",
            "value_schema": VALUE_SCHEMA,
            "value_schema_version": 1,
            "value_schema_size_bytes": schema_size,
            "value_schema_sha256": schema_sha,
            "initial_value": INITIAL_VALUE,
            "initial_value_schema_version": 1,
            "initial_value_size_bytes": value_size,
            "initial_value_sha256": value_sha,
        },
    )
    if not created and (
        definition.configuration_class != "standard"
        or definition.domain != "workflow"
        or definition.value_type != "string"
        or definition.allowed_scopes != "system"
        or definition.allowed_default_sources
        or not definition.is_required
        or definition.is_secret
        or not definition.is_active
    ):
        raise RuntimeError(f"0134 配置定义 {CONFIG_KEY} 与既有合同冲突")

    active = (
        Revision.objects.filter(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        .order_by("-revision")
        .first()
    )
    if active is None:
        raise RuntimeError("0134 缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    if any(row.definition.key == CONFIG_KEY for row in existing):
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
        change_summary="0134 配置化 ComfyUI 已保存工作流扫描目录",
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
        digest_values[row.definition.key] = (
            row.value
            if not row.credential_version_id
            else {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    (
                        f"version:{row.credential_version_id}:"
                        f"{row.credential_version.version}"
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    rows.append(Value(
        revision=revision,
        definition=definition,
        value=INITIAL_VALUE,
        value_schema_version=1,
        value_size_bytes=value_size,
        value_sha256=value_sha,
    ))
    digest_values[CONFIG_KEY] = INITIAL_VALUE
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
    dependencies = [("studio", "0133_comfy_workflow_source_scope")]

    operations = [
        migrations.RunPython(
            configure_saved_workflow_directory,
            migrations.RunPython.noop,
        ),
    ]
