import hashlib
import json

from django.db import migrations
from django.utils import timezone


BOUNDARIES = {
    "security.workflow_smoke.fixture_values_max_bytes": (
        65_536,
        "bytes",
        "工作流 Smoke 规范化 Fixture 最大字节数 / Maximum normalized workflow-smoke fixture bytes",
        ["security", "workflow", "smoke"],
    ),
    "security.workflow_smoke.fixture_max_values": (
        128,
        "items",
        "工作流 Smoke Fixture 参数项绝对上限 / Maximum workflow-smoke fixture values",
        ["security", "workflow", "smoke"],
    ),
    "security.workflow_smoke.fixture_max_assets": (
        18,
        "assets",
        "工作流 Smoke Fixture 物理素材绝对上限 / Maximum workflow-smoke physical assets",
        ["security", "workflow", "smoke", "reference"],
    ),
    "security.deferred.frame_extract_timeout_max_seconds": (
        120,
        "seconds",
        "延迟生成抽帧超时绝对上限 / Maximum deferred frame-extraction timeout",
        ["security", "deferred", "media"],
    ),
    "security.workflow.output_prefix_max_characters": (
        300,
        "characters",
        "工作流输出前缀最大字符数 / Maximum workflow output-prefix characters",
        ["security", "workflow", "comfy"],
    ),
    "security.prompt_normalization.generic_max_depth": (
        8,
        "levels",
        "提示词规范化通用事实投影最大嵌套深度 / Maximum generic normalization projection depth",
        ["security", "prompt"],
    ),
    "security.prompt_normalization.fact_tree_max_depth": (
        5,
        "levels",
        "提示词规范化事实树最大嵌套深度 / Maximum normalization fact-tree depth",
        ["security", "prompt"],
    ),
    "security.prompt_normalization.fact_tree_max_fields": (
        40,
        "fields",
        "提示词规范化事实树单对象最大字段数 / Maximum fields per normalization fact-tree object",
        ["security", "prompt"],
    ),
    "security.prompt_normalization.fact_tree_key_max_characters": (
        80,
        "characters",
        "提示词规范化事实树字段名最大字符数 / Maximum normalization fact-tree key characters",
        ["security", "prompt"],
    ),
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


def add_runtime_contract_boundaries(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    schema = {"minimum": 1}
    schema_size, schema_sha = _contract(schema)
    definitions = {}
    for key, (value, unit, label, impact_modules) in BOUNDARIES.items():
        value_size, value_sha = _contract(value)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": (
                    label
                    + "。命中后 fail closed；提高该值会增加资源占用或攻击面。"
                ),
                "unit": unit,
                "impact_modules": impact_modules,
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": value,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": value_size,
                "initial_value_sha256": value_sha,
            },
        )
        if not created and (
            definition.configuration_class != "security_boundary"
            or definition.domain != "security"
            or definition.value_type != "integer"
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0112 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0112 扩展运行合同安全边界缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition",
            "credential_version",
        )
    )
    existing_keys = {row.definition.key for row in existing}
    missing = sorted(set(BOUNDARIES) - existing_keys)
    if not missing:
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
        change_summary="0112 接入 Smoke、工作流、抽帧与提示词结构安全边界",
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
        if row.credential_version_id:
            identity = (
                f"version:{row.credential_version_id}:"
                f"{row.credential_version.version}"
            )
            digest_values[row.definition.key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest(),
            }
        else:
            digest_values[row.definition.key] = row.value
    for key in missing:
        value = BOUNDARIES[key][0]
        value_size, value_sha = _contract(value)
        rows.append(Value(
            revision=revision,
            definition=definitions[key],
            value=value,
            value_schema_version=1,
            value_size_bytes=value_size,
            value_sha256=value_sha,
        ))
        digest_values[key] = value
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
    dependencies = [("studio", "0111_text_chat_request_options")]

    operations = [
        migrations.RunPython(
            add_runtime_contract_boundaries,
            migrations.RunPython.noop,
        ),
    ]
