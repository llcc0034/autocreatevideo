import hashlib
import json

from django.db import migrations
from django.utils import timezone


CANONICAL_CONTEXT_POLICY = {
    "schema_version": 1,
    "proper_name_glossary_advisory_entries": 40,
    "visual_identity_glossary_advisory_entries": 80,
    "description_advisory_characters": 800,
    "visual_identity_advisory_characters": 800,
    "glossary_term_advisory_characters": 160,
}


STANDARD_CONFIG = {
    "production.canonical_context_policy": (
        "production",
        "object",
        CANONICAL_CONTEXT_POLICY,
        "contract",
        {
            "type": "object",
            "required": sorted(CANONICAL_CONTEXT_POLICY),
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "proper_name_glossary_advisory_entries": {
                    "type": "integer",
                    "minimum": 1,
                },
                "visual_identity_glossary_advisory_entries": {
                    "type": "integer",
                    "minimum": 1,
                },
                "description_advisory_characters": {
                    "type": "integer",
                    "minimum": 1,
                },
                "visual_identity_advisory_characters": {
                    "type": "integer",
                    "minimum": 1,
                },
                "glossary_term_advisory_characters": {
                    "type": "integer",
                    "minimum": 1,
                },
            },
            "closed_contract": True,
        },
        (
            "canonical glossary 模型上下文建议容量；超过建议值只告警并审计，"
            "不会截断剧本/Bible/术语事实，最终请求仍受安全字节边界保护"
            " / Advisory canonical-context capacities without authored-fact truncation"
        ),
        ["production", "translation", "glossary", "prompt"],
        "system,project",
    ),
    "runtime.gpu_probe_timeout_seconds": (
        "runtime",
        "number",
        5.0,
        "seconds",
        {
            "minimum": 0.001,
            "security_ceiling_key": "security.runtime.max_timeout_seconds",
        },
        (
            "本机 GPU 能力探测命令超时 / Local GPU capability probe command timeout"
        ),
        ["runtime", "host_probe", "gpu"],
        "system",
    ),
}


SECURITY_BOUNDARIES = {
    "security.provider.async_rest_terminal_status_max_items": (
        32,
        "items",
        {"minimum": 1},
        (
            "受限异步 REST 每类终态字符串最大项数"
            " / Absolute terminal-status item ceiling per restricted async REST class"
        ),
        ["security", "provider", "async_rest"],
    ),
    "security.provider.async_rest_terminal_status_max_characters": (
        120,
        "characters",
        {"minimum": 1, "maximum": 120},
        (
            "受限异步 REST 单个终态/返回状态最大字符数"
            " / Absolute restricted async REST status-string length ceiling"
        ),
        ["security", "provider", "async_rest"],
    ),
    "security.provider.async_rest_artifact_host_max_items": (
        32,
        "items",
        {"minimum": 1},
        (
            "受限异步 REST 产物主机白名单最大项数"
            " / Absolute restricted async REST artifact-host item ceiling"
        ),
        ["security", "provider", "async_rest", "artifact"],
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


def configure_canonical_context_and_provider_limits(apps, schema_editor):
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
            raise RuntimeError(f"0126 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    for key, (
        domain,
        value_type,
        initial,
        unit,
        schema,
        description,
        modules,
        allowed_scopes,
    ) in STANDARD_CONFIG.items():
        defaults = _definition_defaults(
            domain=domain,
            configuration_class="standard",
            value_type=value_type,
            description=description,
            unit=unit,
            impact_modules=modules,
            allowed_scopes=allowed_scopes,
            value_schema=schema,
            initial_value=initial,
        )
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults=defaults,
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
            or not _definition_contract_matches(definition, defaults)
        ):
            raise RuntimeError(f"0126 标准配置定义 {key} 与既有合同冲突")
        definitions[key] = definition

    # Clarify the pre-existing ordinary batch fields: they are operational
    # advisories.  Only security.canonical_glossary.* blocks a request.
    updated_name_policy = Definition.objects.filter(
        key="production.canonical_name_policy",
    ).update(
        description=(
            "canonical English 名称合同与 glossary 建议批量策略。名称长度/词数必须满足合同；"
            "实体与推理项容量只触发告警和 Provider 分批，不阻断请求；"
            "ProductionRun 会冻结生效配置。"
        )
    )
    if updated_name_policy != 1:
        raise RuntimeError("0126 缺少唯一 production.canonical_name_policy 定义")

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
        raise RuntimeError("0126 缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: value[0] for key, value in SECURITY_BOUNDARIES.items()},
        **{key: value[2] for key, value in STANDARD_CONFIG.items()},
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
            "0126 配置化 canonical 完整上下文、GPU 探测超时与异步 Provider 合同边界"
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
        ("studio", "0125_configurable_operational_list_and_recovery_limits"),
    ]

    operations = [
        migrations.RunPython(
            configure_canonical_context_and_provider_limits,
            migrations.RunPython.noop,
        ),
    ]
