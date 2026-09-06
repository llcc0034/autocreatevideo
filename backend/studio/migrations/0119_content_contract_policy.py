import hashlib
import json

from django.db import migrations
from django.utils import timezone


SECURITY_BOUNDARIES = {
    "security.translation.max_entries_per_request": (
        100, "items", "单次翻译请求最大字段数 / Maximum translation entries per request",
        ["security", "translation", "prompt"],
    ),
    "security.translation.max_promotions_per_request": (
        20, "items", "单次人工确认同步最大字段数 / Maximum confirmed translations promoted per request",
        ["security", "translation", "prompt"],
    ),
    "security.canonical_glossary.max_entities_per_request": (
        100, "entities", "单次 canonical glossary 最大实体数 / Maximum canonical-glossary entities per request",
        ["security", "translation", "glossary"],
    ),
    "security.canonical_glossary.max_runtime_items_per_request": (
        100, "items", "单次 canonical glossary 最大推理项数 / Maximum canonical-glossary inference items per request",
        ["security", "translation", "glossary"],
    ),
    "security.audit_note.minimum_characters": (
        8, "characters", "人工审计说明最小字符数 / Minimum operator audit-note characters",
        ["security", "audit", "smoke"],
    ),
    "security.audit_note.maximum_characters": (
        4_000, "characters", "人工审计说明最大字符数 / Maximum operator audit-note characters",
        ["security", "audit", "smoke"],
    ),
    "security.contract_complexity.canonical_name_max_characters": (
        80, "characters", "canonical English 名称最大字符数 / Maximum canonical-English name characters",
        ["security", "translation", "glossary"],
    ),
    "security.contract_complexity.canonical_name_max_words": (
        8, "words", "canonical English 名称最大词数 / Maximum canonical-English name words",
        ["security", "translation", "glossary"],
    ),
    "security.contract_complexity.prompt_profile_operation_max_characters": (
        100, "characters", "PromptProfile operation key 最大字符数 / Maximum PromptProfile operation-key characters",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.max_validators": (
        8, "items", "PromptProfile generation validator 最大项数 / Maximum PromptProfile generation validators",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.max_context_selectors": (
        16, "items", "PromptProfile generation context selector 最大项数 / Maximum PromptProfile context selectors",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.field_list_max_items": (
        100, "items", "PromptProfile 文本列表最大项数 / Maximum PromptProfile text-list items",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.assignment_separator_max_characters": (
        32, "characters", "PromptProfile assignment separator 最大字符数 / Maximum assignment-separator characters",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.transition_patterns_max_items": (
        16, "items", "PromptProfile transition pattern 最大项数 / Maximum transition-pattern items",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.value_options_max_items": (
        100, "items", "PromptProfile value option 最大项数 / Maximum PromptProfile value options",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.prompt_profile.value_option_max_characters": (
        500, "characters", "PromptProfile value option 最大字符数 / Maximum PromptProfile value-option characters",
        ["security", "prompt_profile"],
    ),
    "security.contract_complexity.media_contract_max_rules": (
        16, "items", "媒体能力合同单类规则最大项数 / Maximum media-capability rules per class",
        ["security", "media", "contract"],
    ),
    "security.contract_complexity.media_contract_max_modes": (
        32, "items", "媒体能力合同最大模式数 / Maximum media-capability modes",
        ["security", "media", "contract"],
    ),
    "security.contract_complexity.media_contract_text_max_characters": (
        500, "characters", "媒体能力合同说明文本最大字符数 / Maximum media-capability contract text characters",
        ["security", "media", "contract"],
    ),
}


CANONICAL_NAME_POLICY = {
    "schema_version": 1,
    "max_characters": 80,
    "max_words": 8,
    "max_entities_per_request": 100,
    "max_entities_with_visual_identity_translation": 50,
    "max_runtime_items_per_request": 100,
}


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def _definition_defaults(*, domain, configuration_class, value_type, description, unit,
                         impact_modules, allowed_scopes, value_schema, initial_value):
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


def configure_content_contract_policy(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    boundary_schema = {"minimum": 1}
    definitions = {}
    for key, (initial, unit, description, impact_modules) in SECURITY_BOUNDARIES.items():
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults=_definition_defaults(
                domain="security",
                configuration_class="security_boundary",
                value_type="integer",
                description=description + "。命中后 fail closed；只有超级管理员可修改。",
                unit=unit,
                impact_modules=impact_modules,
                allowed_scopes="system",
                value_schema=boundary_schema,
                initial_value=initial,
            ),
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
            raise RuntimeError(f"0119 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    policy_schema = {
        "type": "object",
        "required": sorted(CANONICAL_NAME_POLICY),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "max_characters": {"type": "integer", "minimum": 1},
            "max_words": {"type": "integer", "minimum": 1},
            "max_entities_per_request": {"type": "integer", "minimum": 1},
            "max_entities_with_visual_identity_translation": {"type": "integer", "minimum": 1},
            "max_runtime_items_per_request": {"type": "integer", "minimum": 1},
        },
        "closed_contract": True,
        "security_ceiling_paths": {
            "max_characters": "security.contract_complexity.canonical_name_max_characters",
            "max_words": "security.contract_complexity.canonical_name_max_words",
            "max_entities_per_request": "security.canonical_glossary.max_entities_per_request",
            "max_entities_with_visual_identity_translation": "security.canonical_glossary.max_entities_per_request",
            "max_runtime_items_per_request": "security.canonical_glossary.max_runtime_items_per_request",
        },
    }
    policy, created = Definition.objects.get_or_create(
        key="production.canonical_name_policy",
        defaults=_definition_defaults(
            domain="production",
            configuration_class="standard",
            value_type="object",
            description=(
                "canonical English 名称与 glossary 批量策略。可在 system/project 修订中调整；"
                "ProductionRun 会冻结其生效配置，且资源值不得超过声明的安全边界。"
            ),
            unit="contract",
            impact_modules=["production", "translation", "glossary", "prompt"],
            allowed_scopes="system,project",
            value_schema=policy_schema,
            initial_value=CANONICAL_NAME_POLICY,
        ),
    )
    if not created and (
        policy.configuration_class != "standard"
        or policy.domain != "production"
        or policy.value_type != "object"
        or policy.allowed_scopes != "system,project"
        or policy.allowed_default_sources
        or not policy.is_required
        or policy.is_secret
        or not policy.is_active
    ):
        raise RuntimeError("0119 canonical name 策略定义与既有合同冲突")
    if not created:
        defaults = _definition_defaults(
            domain="production", configuration_class="standard", value_type="object",
            description=(
                "canonical English 名称与 glossary 批量策略。可在 system/project 修订中调整；"
                "ProductionRun 会冻结其生效配置，且资源值不得超过声明的安全边界。"
            ),
            unit="contract", impact_modules=["production", "translation", "glossary", "prompt"],
            allowed_scopes="system,project", value_schema=policy_schema,
            initial_value=CANONICAL_NAME_POLICY,
        )
        Definition.objects.filter(pk=policy.pk).update(**{
            key: defaults[key]
            for key in (
                "description", "value_schema", "value_schema_version",
                "value_schema_size_bytes", "value_schema_sha256",
            )
        })
    definitions[policy.key] = policy

    active = Revision.objects.filter(
        scope_type="system", status="published", activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0119 内容合同配置缺少 active system revision")
    existing = list(Value.objects.filter(revision=active).select_related(
        "definition", "credential_version",
    ))
    initial_values = {
        **{key: spec[0] for key, spec in SECURITY_BOUNDARIES.items()},
        "production.canonical_name_policy": CANONICAL_NAME_POLICY,
    }
    missing = sorted(set(initial_values) - {row.definition.key for row in existing})
    if not missing:
        return

    now = timezone.now()
    next_revision = (Revision.objects.filter(scope_type="system").order_by("-revision")
                     .values_list("revision", flat=True).first() or 0) + 1
    revision = Revision.objects.create(
        scope_type="system", revision=next_revision, status="published",
        based_on_revision_id=active.id, activation_state="inactive",
        change_summary="0119 收敛内容合同策略、安全边界与 canonical name 配置",
        published_at=now,
    )
    rows = []
    digest_values = {}
    for row in existing:
        rows.append(Value(
            revision=revision, definition_id=row.definition_id, value=row.value,
            value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256, credential_version_id=row.credential_version_id,
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
        value = initial_values[key]
        value_size, value_sha = _contract(value)
        rows.append(Value(
            revision=revision, definition=definitions[key], value=value,
            value_schema_version=1, value_size_bytes=value_size, value_sha256=value_sha,
        ))
        digest_values[key] = value
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Revision.objects.filter(pk=active.pk).update(
        status="superseded", activation_state="inactive", updated_at=now,
    )
    Revision.objects.filter(pk=revision.pk).update(
        activation_state="active", activated_at=now, updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0118_runtime_state_configuration")]

    operations = [
        migrations.RunPython(configure_content_contract_policy, migrations.RunPython.noop),
    ]
