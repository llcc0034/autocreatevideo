import hashlib
import json

from django.db import migrations
from django.utils import timezone


BOUNDARIES = {
    "security.provider.smoke_image_max_bytes": (
        67_108_864,
        "bytes",
        "Provider 图片 Smoke 单产物最大字节数 / Maximum provider image-smoke artifact bytes",
    ),
    "security.provider.smoke_video_max_bytes": (
        536_870_912,
        "bytes",
        "Provider 视频 Smoke 单产物最大字节数 / Maximum provider video-smoke artifact bytes",
    ),
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def add_provider_smoke_boundaries(apps, schema_editor):
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
                "impact_modules": ["security", "provider", "smoke"],
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
            raise RuntimeError(f"0110 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(scope_type="system", status="published", activation_state="active").order_by("-revision").first()
    if active is None:
        raise RuntimeError("0110 扩展 Provider Smoke 安全边界缺少 active system revision")
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
        change_summary="0110 接入 Provider Smoke 产物安全边界",
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
    dependencies = [("studio", "0109_prompt_security_boundaries")]

    operations = [migrations.RunPython(add_provider_smoke_boundaries, migrations.RunPython.noop)]
