import hashlib
import json

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "z-image.bible.reference"
SOURCE_VERSION = 2
TARGET_VERSION = 3
SOURCE_BINDING_KEY = "z-image-turbo-bible-v2"
TARGET_BINDING_KEY = "z-image-turbo-bible-v3"
SOURCE_CONFIG_SHA256 = (
    "5a652310db69eb2c64b59149fa3bcbb4035a5a7623e204d5e93ba3a13c79e13d"
)
EXPECTED_SOURCE_SELECTORS = {
    "request.subject",
    "request.theme",
    "request.composition",
    "request.environment",
    "request.lighting",
    "request.style",
    "request.color_palette",
    "request.negative_constraints",
}

INITIAL_SNAPSHOT_CLAUSE_POLICY = {
    "delimiters": [",", ";", "，", "；"],
    "initial_state_markers": [
        "initially",
        "at first",
        "initial state",
        "starts as",
        "begins as",
        "初始",
        "起初",
        "最初",
        "初态",
        "开始时",
    ],
    "continuity_source_selector": "request.action",
    "trailing_clause_action": "relocate_to_continuity",
}


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _profile_contract(profile, rules):
    return {
        "schema_version": 4,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "source_projection_key": profile.source_projection_key,
        "channel_limits": profile.channel_limits,
        "source_projection_config": profile.source_projection_config,
        "rules": [{
            "field_key": rule.field_definition.key,
            "field_value_type": rule.field_definition.value_type,
            "field_default_value": rule.field_definition.default_value,
            "field_value_options": rule.field_definition.value_options,
            "field_required": rule.field_definition.is_required,
            "source_selector": rule.source_selector,
            "channel": rule.channel,
            "position": rule.position,
            "label_style": rule.label_style,
            "separator": rule.separator,
            "required": rule.required,
            "omit_if_empty": rule.omit_if_empty,
            "max_chars": rule.max_chars,
            "generation_mode": rule.generation_mode,
            "generation_source_selector": rule.generation_source_selector,
            "generation_instruction": rule.generation_instruction,
            "generation_output_semantics": rule.generation_output_semantics,
            "generation_required": rule.generation_required,
            "language_policy": rule.language_policy,
            "length_unit": rule.length_unit,
            "generation_min": rule.generation_min,
            "generation_max": rule.generation_max,
            "generation_validator_keys": rule.generation_validator_keys,
        } for rule in rules],
    }


def publish_initial_clause_profile(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")

    source = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
        status__in=("published", "retired"),
    ).first()
    if source is None:
        return
    source_config = source.source_projection_config
    source_policy = (
        source_config.get("static_reference_policy")
        if isinstance(source_config, dict)
        else None
    )
    if (
        source.contract_schema_version != 4
        or source.operation != "image.generate"
        or source.source_projection_key != "direct_v1"
        or not isinstance(source_policy, dict)
        or source_config.get("schema_version") != 1
        or source_policy.get("snapshot_selector") != "initial"
        or "initial_snapshot_clause_policy" in source_policy
    ):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: source contract drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    source_config_sha256 = hashlib.sha256(
        _canonical_bytes(source_config),
    ).hexdigest()
    source_contract = _canonical_bytes(_profile_contract(source, source_rules))
    selected_sources = {row.source_selector for row in source_rules}
    if (
        not source_rules
        or source_config_sha256 != SOURCE_CONFIG_SHA256
        or source.contract_size_bytes != len(source_contract)
        or source.contract_sha256
        != hashlib.sha256(source_contract).hexdigest()
        or selected_sources != EXPECTED_SOURCE_SELECTORS
    ):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: source rules cannot isolate continuity facts",
        )

    target_config = json.loads(json.dumps(source_config, ensure_ascii=False))
    target_config["schema_version"] = 2
    target_config["static_reference_policy"][
        "initial_snapshot_clause_policy"
    ] = INITIAL_SNAPSHOT_CLAUSE_POLICY
    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    expected = {
        "name": "Bible 单一静态初态分句参考 / Static Initial Clause Reference",
        "description": (
            "通过 PromptProfile 数据合同保留显式初态分句，将其后分句"
            "保留为 continuity/action 事实，不让动态变化同时出现在单张参考图。"
        ),
        "model_family": source.model_family,
        "operation": source.operation,
        "mode": source.mode,
        "source_projection_key": source.source_projection_key,
        "source_projection_config": target_config,
        "official_reference": source.official_reference,
        "channel_limits": source.channel_limits,
        "review_state": "ready",
        "status": "published",
        "contract_schema_version": 4,
    }
    if target is None:
        target = Profile.objects.create(
            profile_key=PROFILE_KEY,
            version=TARGET_VERSION,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
            **expected,
        )
        for source_rule in source_rules:
            Rule.objects.create(
                profile_version_id=target.id,
                field_definition_id=source_rule.field_definition_id,
                source_selector=source_rule.source_selector,
                channel=source_rule.channel,
                position=source_rule.position,
                label_style=source_rule.label_style,
                separator=source_rule.separator,
                required=source_rule.required,
                omit_if_empty=source_rule.omit_if_empty,
                max_chars=source_rule.max_chars,
                generation_mode=source_rule.generation_mode,
                generation_source_selector=source_rule.generation_source_selector,
                generation_instruction=source_rule.generation_instruction,
                generation_output_semantics=source_rule.generation_output_semantics,
                generation_required=source_rule.generation_required,
                language_policy=source_rule.language_policy,
                length_unit=source_rule.length_unit,
                generation_min=source_rule.generation_min,
                generation_max=source_rule.generation_max,
                generation_validator_keys=source_rule.generation_validator_keys,
            )
    elif any(getattr(target, field) != value for field, value in expected.items()):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: existing successor drifted",
        )

    target_rules = list(
        Rule.objects.filter(profile_version_id=target.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    source_rule_contracts = [
        (
            row.field_definition_id,
            row.source_selector,
            row.channel,
            row.position,
            row.label_style,
            row.separator,
            row.required,
            row.omit_if_empty,
            row.max_chars,
            row.generation_mode,
            row.generation_source_selector,
            row.generation_instruction,
            row.generation_output_semantics,
            row.generation_required,
            row.language_policy,
            row.length_unit,
            row.generation_min,
            row.generation_max,
            row.generation_validator_keys,
        )
        for row in source_rules
    ]
    target_rule_contracts = [
        (
            row.field_definition_id,
            row.source_selector,
            row.channel,
            row.position,
            row.label_style,
            row.separator,
            row.required,
            row.omit_if_empty,
            row.max_chars,
            row.generation_mode,
            row.generation_source_selector,
            row.generation_instruction,
            row.generation_output_semantics,
            row.generation_required,
            row.language_policy,
            row.length_unit,
            row.generation_min,
            row.generation_max,
            row.generation_validator_keys,
        )
        for row in target_rules
    ]
    if target_rule_contracts != source_rule_contracts:
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: copied rules drifted",
        )
    encoded = _canonical_bytes(_profile_contract(target, target_rules))
    digest = hashlib.sha256(encoded).hexdigest()
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = digest
        target.save(update_fields=(
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))
    elif (
        target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != digest
    ):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: contract checksum drifted",
        )

    source_binding = Binding.objects.filter(key=SOURCE_BINDING_KEY).first()
    if source_binding is None:
        return
    if (
        source_binding.prompt_profile_version_id != source.id
        or source_binding.operation != source.operation
        or source_binding.model_family != source.model_family
        or source_binding.mode != source.mode
    ):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: source binding drifted",
        )
    binding_expected = {
        "name": "Bible 静态初态分句参考 v3",
        "operation": source_binding.operation,
        "model_family": source_binding.model_family,
        "mode": source_binding.mode,
        "model_profile_version_id": source_binding.model_profile_version_id,
        "prompt_profile_version_id": target.id,
        "priority": source_binding.priority,
        "is_active": True,
    }
    target_binding = Binding.objects.filter(key=TARGET_BINDING_KEY).first()
    if target_binding is None:
        target_binding = Binding.objects.create(
            key=TARGET_BINDING_KEY,
            **binding_expected,
        )
    elif any(
        getattr(target_binding, field) != value
        for field, value in binding_expected.items()
    ):
        raise RuntimeError(
            "Cannot publish Bible PromptProfile v3: target binding drifted",
        )
    Binding.objects.filter(
        operation=source_binding.operation,
        model_family=source_binding.model_family,
        mode=source_binding.mode,
        model_profile_version_id=source_binding.model_profile_version_id,
        is_active=True,
    ).exclude(pk=target_binding.pk).update(is_active=False)
    Binding.objects.filter(pk=target_binding.pk).update(is_active=True)
    Profile.objects.filter(pk=source.pk, status="published").update(status="retired")


class Migration(migrations.Migration):
    dependencies = [("studio", "0097_visual_rules_advisory_capacity")]

    operations = [
        migrations.RunPython(
            publish_initial_clause_profile,
            migrations.RunPython.noop,
        ),
    ]
