import hashlib
import json

from django.db import migrations
from django.utils import timezone


BOUNDARIES = {
    "security.prompt_normalization.advisory_max_items": (200, "items", "单次提示词规范化建议记录绝对上限 / Maximum normalization advisory records"),
    "security.prompt_normalization.smoke_snapshot_max_bytes": (1_048_576, "bytes", "提示词规范化 Smoke 快照最大字节数 / Maximum normalization smoke snapshot bytes"),
    "security.prompt_profile.max_validators": (16, "items", "单字段校验器绝对上限 / Maximum validators per prompt field"),
    "security.prompt_profile.max_context_selectors": (8, "items", "单字段上下文选择器绝对上限 / Maximum context selectors per prompt field"),
    "security.prompt_profile.max_static_reference_markers": (64, "items", "静态参考标记绝对上限 / Maximum static-reference markers"),
    "security.prompt_profile.static_reference_marker_max_characters": (100, "characters", "静态参考标记最大字符数 / Maximum static-reference marker characters"),
    "security.prompt_profile.field_text_max_characters": (30_000, "characters", "PromptProfile 单字段文本绝对上限 / Maximum prompt-profile field text characters"),
    "security.prompt_profile.field_list_max_items": (100, "items", "PromptProfile 单字段数组绝对上限 / Maximum prompt-profile list items"),
    "security.prompt_profile.field_list_item_max_characters": (1_000, "characters", "PromptProfile 数组单项字符绝对上限 / Maximum prompt-profile list-item characters"),
    "security.prompt_profile.numeric_absolute_max": (1_000_000_000, "units", "PromptProfile 数值字段绝对值上限 / Maximum absolute prompt-profile numeric value"),
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def add_prompt_boundaries(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    schema = {"minimum": 1}
    schema_size, schema_sha = _contract(schema)
    definitions = {}
    for key, (value, unit, label) in BOUNDARIES.items():
        value_size, value_sha = _contract(value)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": label + "。命中后 fail closed；提高该值会增加资源占用或攻击面。",
                "unit": unit,
                "impact_modules": ["security", "prompt"],
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
            raise RuntimeError(f"0109 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(scope_type="system", status="published", activation_state="active").order_by("-revision").first()
    if active is None:
        raise RuntimeError("0109 扩展提示词安全边界缺少 active system revision")
    existing = list(Value.objects.filter(revision=active).select_related("definition", "credential_version"))
    existing_keys = {row.definition.key for row in existing}
    missing = sorted(set(BOUNDARIES) - existing_keys)
    if not missing:
        return
    now = timezone.now()
    next_revision = (Revision.objects.filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
    revision = Revision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.id,
        activation_state="inactive",
        change_summary="0109 接入提示词与 PromptProfile 安全边界",
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
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            digest_values[row.definition.key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
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
    Revision.objects.filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0108_expand_security_boundaries")]

    operations = [migrations.RunPython(add_prompt_boundaries, migrations.RunPython.noop)]
