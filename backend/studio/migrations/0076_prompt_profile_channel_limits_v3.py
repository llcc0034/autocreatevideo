import hashlib
import json

from django.db import migrations, models
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
TARGET_VERSION = 7
TARGET_BINDING_KEY = "minimax-h3-ref2va-official-v7"
CHANNEL_LIMITS = {
    "positive_prompt": {
        "max_chars": 7000,
        "warn_at_chars": 6200,
    },
}


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _profile_contract(profile, rules):
    return {
        "schema_version": 3,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "source_projection_key": profile.source_projection_key,
        "channel_limits": CHANNEL_LIMITS,
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


def create_v3_h3_profile(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")

    source = (
        Profile.objects.filter(
            profile_key=PROFILE_KEY,
            status__in=("published", "retired"),
            version__lt=TARGET_VERSION,
        )
        .order_by("-version")
        .first()
    )
    if source is None:
        return
    if source.source_projection_key != "classified_reference_context_ir_v1":
        raise RuntimeError(
            "Cannot create H3 Ref2VA PromptProfile v7: source projection drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if not source_rules:
        raise RuntimeError(
            "Cannot create H3 Ref2VA PromptProfile v7: source rules are empty",
        )

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=PROFILE_KEY,
            version=TARGET_VERSION,
            name="MiniMax H3 Ref2VA 配置化通道合同 / Configured Channel Contract",
            description=(
                "六段字段生成与装配继续由字段规则驱动；最终正向通道的上限与预警阈值"
                "由 PromptProfile schema v3 channel_limits 冻结，不再由 H3 代码常量决定。"
            ),
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            official_reference=source.official_reference,
            channel_limits=CHANNEL_LIMITS,
            review_state="ready",
            status="published",
            contract_schema_version=3,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
        )
        for source_rule in source_rules:
            values = {
                "profile_version_id": target.id,
                "field_definition_id": source_rule.field_definition_id,
                "source_selector": source_rule.source_selector,
                "channel": source_rule.channel,
                "position": source_rule.position,
                "label_style": source_rule.label_style,
                "separator": source_rule.separator,
                "required": source_rule.required,
                "omit_if_empty": source_rule.omit_if_empty,
                "max_chars": source_rule.max_chars,
                "generation_mode": source_rule.generation_mode,
                "generation_source_selector": source_rule.generation_source_selector,
                "generation_instruction": source_rule.generation_instruction,
                "generation_output_semantics": source_rule.generation_output_semantics,
                "generation_required": source_rule.generation_required,
                "language_policy": source_rule.language_policy,
                "length_unit": source_rule.length_unit,
                "generation_min": source_rule.generation_min,
                "generation_max": source_rule.generation_max,
                "generation_validator_keys": source_rule.generation_validator_keys,
            }
            if source_rule.field_definition.key == "detailed_description":
                values.update({
                    "length_unit": "english_words",
                    "generation_min": 350,
                    "generation_max": 500,
                })
            Rule.objects.create(**values)

        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        encoded = _canonical_bytes(_profile_contract(target, target_rules))
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = hashlib.sha256(encoded).hexdigest()
        target.save(update_fields=(
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))
    else:
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        encoded = _canonical_bytes(_profile_contract(target, target_rules))
        detail_rule = next((
            rule for rule in target_rules
            if rule.field_definition.key == "detailed_description"
        ), None)
        if (
            target.contract_schema_version != 3
            or target.status not in ("published", "retired")
            or target.channel_limits != CHANNEL_LIMITS
            or detail_rule is None
            or detail_rule.length_unit != "english_words"
            or detail_rule.generation_min != 350
            or detail_rule.generation_max != 500
            or target.contract_size_bytes != len(encoded)
            or target.contract_sha256 != hashlib.sha256(encoded).hexdigest()
        ):
            raise RuntimeError(
                "Cannot activate H3 Ref2VA PromptProfile v7: existing contract drifted",
            )

    source_binding = (
        Binding.objects.filter(
            prompt_profile_version_id=source.id,
            is_active=True,
        )
        .order_by("-priority", "key")
        .first()
    )
    if source_binding is None:
        source_binding = (
            Binding.objects.filter(prompt_profile_version_id=source.id)
            .order_by("-priority", "key")
            .first()
        )
    if source_binding is None:
        return
    target_binding, _created = Binding.objects.get_or_create(
        key=TARGET_BINDING_KEY,
        defaults={
            "name": "MiniMax H3 Ref2VA 配置化通道合同 v7",
            "operation": source_binding.operation,
            "model_family": source_binding.model_family,
            "mode": source_binding.mode,
            "model_profile_version_id": source_binding.model_profile_version_id,
            "prompt_profile_version_id": target.id,
            "priority": source_binding.priority,
            "is_active": True,
        },
    )
    if (
        target_binding.prompt_profile_version_id != target.id
        or target_binding.operation != source_binding.operation
        or target_binding.model_family != source_binding.model_family
        or target_binding.mode != source_binding.mode
        or target_binding.model_profile_version_id
        != source_binding.model_profile_version_id
    ):
        raise RuntimeError(
            "Cannot activate H3 Ref2VA PromptProfile v7: binding key drifted",
        )
    Binding.objects.filter(pk=target_binding.pk).update(is_active=True)
    Binding.objects.filter(pk=source_binding.pk).exclude(
        pk=target_binding.pk,
    ).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("studio", "0075_generic_prompt_language_gate_config")]

    operations = [
        migrations.AddField(
            model_name="promptprofileversion",
            name="channel_limits",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "按输出通道配置最终提示词长度合同；键为受控 channel，值包含 "
                    "max_chars 与可选 warn_at_chars。"
                ),
            ),
        ),
        migrations.RunPython(create_v3_h3_profile, migrations.RunPython.noop),
    ]
