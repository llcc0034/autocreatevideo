import hashlib
import json

from django.db import migrations
from django.utils import timezone


SECURITY_BOUNDARIES = {
    "security.dialogue.max_entries": (
        64,
        "items",
        "单镜头对白条目绝对上限 / Maximum dialogue entries per shot",
    ),
    "security.dialogue.speaker_id_max_characters": (
        16,
        "characters",
        "对白稳定说话人 ID 最大字符数 / Maximum stable dialogue speaker-id characters",
    ),
    "security.dialogue.text_max_characters": (
        2_000,
        "characters",
        "单条对白正文最大字符数 / Maximum dialogue text characters per entry",
    ),
    "security.dialogue.delivery_max_characters": (
        500,
        "characters",
        "单条对白表演说明最大字符数 / Maximum dialogue delivery-direction characters",
    ),
}


SERIES_CONCEPT_RESOURCE_ADVISORY = {
    "schema_version": 1,
    "input_protocol_overhead_chars": 2_048,
    "output_base_units": 512,
    "output_units_per_shot": 200,
    "minimum_output_units": 1_024,
    "output_hint_warning_percent": 80,
    "script_breakdown_reference_chars": 1_000_000,
}


SCRIPT_PROP_ALIAS_POLICY = {
    "schema_version": 1,
    "minimum_core_characters": 2,
    "maximum_prefix_trim_characters": 2,
    "generic_aliases": ["东西", "物件", "物品", "设备", "工具", "道具"],
}


STANDARD_CONFIG = {
    "runtime.host_probe_heartbeat_seconds": (
        "runtime",
        "number",
        2.0,
        "seconds",
        {
            "minimum": 0.1,
            "security_ceiling_key": "security.runtime.max_interval_seconds",
        },
        "运行主机探测持久心跳间隔 / Runtime-host probe durable heartbeat interval",
        ["runtime", "worker", "host_probe"],
        "system",
    ),
    "runtime.host_probe_lease_seconds": (
        "runtime",
        "integer",
        300,
        "seconds",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "运行主机探测独占租约时长 / Runtime-host probe exclusive lease duration",
        ["runtime", "worker", "host_probe"],
        "system",
    ),
    "runtime.host_probe_stale_seconds": (
        "runtime",
        "integer",
        300,
        "seconds",
        {
            "minimum": 60,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "运行主机探测失联回收阈值 / Runtime-host probe stale-claim recovery threshold",
        ["runtime", "worker", "host_probe"],
        "system",
    ),
    "runtime.host_probe_refresh_buffer_seconds": (
        "runtime",
        "integer",
        120,
        "seconds",
        {
            "minimum": 0,
            "security_ceiling_key": "security.runtime.max_interval_seconds",
        },
        "运行主机能力快照刷新调度余量 / Runtime-host capability refresh scheduling buffer",
        ["runtime", "worker", "host_probe"],
        "system",
    ),
    "runtime.platform_maintenance_poll_seconds": (
        "runtime",
        "number",
        60.0,
        "seconds",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_interval_seconds",
        },
        "平台维护 Worker 扫描间隔 / Platform-maintenance worker sweep interval",
        ["runtime", "worker", "maintenance"],
        "system",
    ),
    "runtime.platform_maintenance_lease_seconds": (
        "runtime",
        "integer",
        180,
        "seconds",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "平台维护单实例租约时长 / Platform-maintenance single-flight lease duration",
        ["runtime", "worker", "maintenance"],
        "system",
    ),
    "runtime.readiness_worker_max_age_seconds": (
        "runtime",
        "integer",
        30,
        "seconds",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        "就绪检查可接受的 Worker 心跳最大年龄 / Maximum worker-heartbeat age accepted by readiness",
        ["runtime", "readiness", "worker"],
        "system",
    ),
    "runtime.readiness_min_disk_free_bytes": (
        "runtime",
        "integer",
        536_870_912,
        "bytes",
        {"minimum": 0},
        "就绪检查要求的媒体卷最小剩余空间 / Minimum free media-volume bytes required by readiness",
        ["runtime", "readiness", "storage"],
        "system",
    ),
    "production.series_concept_resource_advisory": (
        "production",
        "object",
        SERIES_CONCEPT_RESOURCE_ADVISORY,
        "advisory_contract",
        {
            "type": "object",
            "required": sorted(SERIES_CONCEPT_RESOURCE_ADVISORY),
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "input_protocol_overhead_chars": {"type": "integer", "minimum": 0},
                "output_base_units": {"type": "integer", "minimum": 0},
                "output_units_per_shot": {"type": "integer", "minimum": 0},
                "minimum_output_units": {"type": "integer", "minimum": 1},
                "output_hint_warning_percent": {"type": "integer", "minimum": 1},
                "script_breakdown_reference_chars": {"type": "integer", "minimum": 1},
            },
            "closed_contract": True,
        },
        "剧本候选资源估算与拆稿预算建议；仅提示，不阻断 / Series-concept resource and breakdown advisories; warning only",
        ["production", "script", "quality_advisory"],
        "system,project",
    ),
    "production.script_prop_alias_policy": (
        "production",
        "object",
        SCRIPT_PROP_ALIAS_POLICY,
        "contract",
        {
            "type": "object",
            "required": sorted(SCRIPT_PROP_ALIAS_POLICY),
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "minimum_core_characters": {"type": "integer", "minimum": 1},
                "maximum_prefix_trim_characters": {"type": "integer", "minimum": 0},
                "generic_aliases": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "closed_contract": True,
        },
        "确定性拆稿的道具简称推导策略 / Deterministic script-breakdown prop-alias policy",
        ["production", "storyboard", "bible"],
        "system,project",
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


def configure_runtime_policies(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions = {}
    boundary_schema = {"minimum": 1}
    boundary_schema_size, boundary_schema_sha = _contract(boundary_schema)
    for key, (initial, unit, description) in SECURITY_BOUNDARIES.items():
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": description + "。命中后 fail closed；只有超级管理员可修改。",
                "unit": unit,
                "impact_modules": ["security", "dialogue", "production"],
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": boundary_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": boundary_schema_size,
                "value_schema_sha256": boundary_schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
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
            raise RuntimeError(f"0116 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    for key, (
        domain,
        value_type,
        initial,
        unit,
        schema,
        description,
        impact_modules,
        allowed_scopes,
    ) in STANDARD_CONFIG.items():
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": domain,
                "configuration_class": "standard",
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
                "value_schema": schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )
        if not created and (
            definition.configuration_class != "standard"
            or definition.domain != domain
            or definition.value_type != value_type
            or definition.allowed_scopes != allowed_scopes
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0116 标准配置定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0116 运行期策略配置缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: spec[0] for key, spec in SECURITY_BOUNDARIES.items()},
        **{key: spec[2] for key, spec in STANDARD_CONFIG.items()},
    }
    missing = sorted(set(initial_values) - {row.definition.key for row in existing})
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
        change_summary="0116 收敛运行期、对白与剧本建议策略配置",
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
        value = initial_values[key]
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
    dependencies = [("studio", "0115_media_processing_configuration")]

    operations = [
        migrations.RunPython(
            configure_runtime_policies,
            migrations.RunPython.noop,
        ),
    ]
