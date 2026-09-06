"""Version operational image-scaling limits; preserve existing revision values."""
import hashlib
import json
from django.db import migrations
from django.utils import timezone

SECURITY_BOUNDARIES = {
    "security.workflow.image_resolution_min_dimension": (64, "pixels", {"minimum": 1, "security_ceiling_key": "security.workflow.image_resolution_max_dimension"}, "图片缩放最小边长，影响配置校验及执行准入，不修改已冻结尺寸", ["security", "workflow"]),
    "security.workflow.image_resolution_max_dimension": (8192, "pixels", {"minimum": 1}, "图片缩放最大边长，影响配置校验及执行准入，不修改已冻结尺寸", ["security", "workflow"]),
    "security.workflow.image_resolution_max_pixels": (16777216, "pixels", {"minimum": 1}, "图片缩放单图最大总像素，影响配置校验及执行准入，不修改已冻结尺寸", ["security", "workflow"]),
    "security.workflow.image_resolution_max_candidates": (32, "items", {"minimum": 1}, "图片缩放目标尺寸最多候选数，影响配置校验及执行准入，不修改已冻结尺寸", ["security", "workflow"]),
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


def _definition_defaults(
    *, domain, configuration_class, value_type, description, unit,
    impact_modules, allowed_scopes, value_schema, initial_value,
):
    schema_size, schema_sha = _contract(value_schema)
    initial_size, initial_sha = _contract(initial_value)
    return {
        "domain": domain,
        "configuration_class": configuration_class,
        "value_type": value_type,
        "description": description,
        "unit": unit,
        "impact_modules": impact_modules,
        "restart_required": False,
        "allowed_scopes": allowed_scopes,
        "allowed_default_sources": "",
        "is_required": True,
        "is_secret": False,
        "is_active": True,
        "needs_review": False,
        "review_reason": "",
        "value_schema": value_schema,
        "value_schema_version": 1,
        "value_schema_size_bytes": schema_size,
        "value_schema_sha256": schema_sha,
        "initial_value": initial_value,
        "initial_value_schema_version": 1,
        "initial_value_size_bytes": initial_size,
        "initial_value_sha256": initial_sha,
    }


def _definition_contract_matches(definition, defaults):
    return all(
        getattr(definition, field) == defaults[field]
        for field in (
            "value_schema",
            "value_schema_version",
            "value_schema_size_bytes",
            "value_schema_sha256",
            "initial_value",
            "initial_value_schema_version",
            "initial_value_size_bytes",
            "initial_value_sha256",
        )
    )


def configure_image_resolution_limits(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions = {}
    for key, (initial, unit, schema, description, modules) in SECURITY_BOUNDARIES.items():
        defaults = _definition_defaults(
            domain="security",
            configuration_class="security_boundary",
            value_type="integer",
            description=(
                description + "。命中后 fail closed；只有超级管理员可以修改。"
            ),
            unit=unit,
            impact_modules=modules,
            allowed_scopes="system",
            value_schema=schema,
            initial_value=initial,
        )
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults=defaults,
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
            or not _definition_contract_matches(definition, defaults)
        ):
            raise RuntimeError(f"0139 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

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
        raise RuntimeError("0139 缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: value[0] for key, value in SECURITY_BOUNDARIES.items()},
    }
    missing = sorted(
        set(initial_values) - {row.definition.key for row in existing}
    )
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
        change_summary=(
            "0139 将工作流图片缩放安全边界纳入配置中心"
        ),
        published_at=now,
    )
    rows = []
    digest_values = {}
    for row in existing:
        rows.append(
            Value(
                revision=revision,
                definition_id=row.definition_id,
                value=row.value,
                value_schema_version=row.value_schema_version,
                value_size_bytes=row.value_size_bytes,
                value_sha256=row.value_sha256,
                credential_version_id=row.credential_version_id,
            )
        )
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
    for key in missing:
        value = initial_values[key]
        value_size, value_sha = _contract(value)
        rows.append(
            Value(
                revision=revision,
                definition=definitions[key],
                value=value,
                value_schema_version=1,
                value_size_bytes=value_size,
                value_sha256=value_sha,
            )
        )
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
    dependencies = [
        ("studio", "0138_deployment_smoke_prompt_profile"),
    ]

    operations = [
        migrations.RunPython(
            configure_image_resolution_limits,
            migrations.RunPython.noop,
        ),
    ]
