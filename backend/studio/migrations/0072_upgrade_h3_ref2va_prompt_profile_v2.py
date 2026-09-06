import hashlib
import json

import django.core.validators
from django.db import migrations
from django.db import models
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 4
TARGET_VERSION = 5
SOURCE_BINDING_KEY = "minimax-h3-ref2va-official-v4"
TARGET_BINDING_KEY = "minimax-h3-ref2va-official-v5"

GENERATION_CONTRACTS = {
    "subject_definitions": {
        "generation_mode": "server_locked",
        "generation_source_selector": "request.h3_ref2va.subject_definitions",
        "generation_instruction": "",
        "generation_output_semantics": "final",
        "generation_required": True,
        "language_policy": "any",
        "length_unit": "none",
        "generation_min": None,
        "generation_max": None,
        "generation_validator_keys": [],
    },
    "summary": {
        "generation_mode": "server_locked",
        "generation_source_selector": "request.h3_ref2va.summary",
        "generation_instruction": "",
        "generation_output_semantics": "final",
        "generation_required": True,
        "language_policy": "any",
        "length_unit": "none",
        "generation_min": None,
        "generation_max": None,
        "generation_validator_keys": [],
    },
    "retention_analysis": {
        "generation_mode": "server_locked",
        "generation_source_selector": "request.h3_ref2va.retention_analysis",
        "generation_instruction": "",
        "generation_output_semantics": "final",
        "generation_required": True,
        "language_policy": "any",
        "length_unit": "none",
        "generation_min": None,
        "generation_max": None,
        "generation_validator_keys": [],
    },
    "detailed_description": {
        "generation_mode": "model_generated",
        "generation_source_selector": "shot.detailed_description",
        "generation_instruction": (
            "Expand the frozen shot facts into visible, chronological director prose only. "
            "Preserve exact dialogue, timing, continuity, and reference identities; do not invent events."
        ),
        "generation_output_semantics": "contribution",
        "generation_required": True,
        "language_policy": "en",
        "length_unit": "english_words",
        "generation_min": 140,
        "generation_max": 220,
        "generation_validator_keys": [
            "non_empty",
            "no_control_characters",
            "no_unresolved_placeholders",
        ],
    },
    "overall_soundscape": {
        "generation_mode": "model_generated",
        "generation_source_selector": "shot.sound_effects",
        "generation_instruction": (
            "Describe only diegetic ambience and synchronized sound effects supported by the frozen shot facts."
        ),
        "generation_output_semantics": "contribution",
        "generation_required": True,
        "language_policy": "en",
        "length_unit": "english_words",
        "generation_min": 1,
        "generation_max": 80,
        "generation_validator_keys": [
            "non_empty",
            "no_control_characters",
            "no_unresolved_placeholders",
        ],
    },
    "non_diegetic_music": {
        "generation_mode": "model_generated",
        "generation_source_selector": "shot.music",
        "generation_instruction": (
            "Describe only non-diegetic music style, intensity, transitions, and silence supported by the frozen shot facts."
        ),
        "generation_output_semantics": "contribution",
        "generation_required": True,
        "language_policy": "en",
        "length_unit": "english_words",
        "generation_min": 1,
        "generation_max": 60,
        "generation_validator_keys": [
            "non_empty",
            "no_control_characters",
            "no_unresolved_placeholders",
        ],
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
        "schema_version": 2,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "source_projection_key": profile.source_projection_key,
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


def create_profile_v5(apps, schema_editor):
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
    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=PROFILE_KEY,
            version=TARGET_VERSION,
            name="MiniMax H3 Ref2VA 官方六段合同 / Official Context-IR",
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key="classified_reference_context_ir_v1",
            official_reference=source.official_reference,
            description=(
                "官方六段第三层装配保持不变；第二层生成、语言、长度与"
                "通用校验全部由 schema v2 字段配置驱动。"
            ),
            review_state="ready",
            status="published",
            contract_schema_version=2,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
        )
        source_rules = list(
            Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        if set(
            rule.field_definition.key for rule in source_rules
        ) != set(GENERATION_CONTRACTS):
            raise RuntimeError(
                "Cannot create H3 Ref2VA PromptProfile v5: v4 field set drifted",
            )
        for source_rule in source_rules:
            generation = GENERATION_CONTRACTS[source_rule.field_definition.key]
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
                **generation,
            )
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        encoded = _canonical_bytes(_profile_contract(target, target_rules))
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = hashlib.sha256(encoded).hexdigest()
        target.save(update_fields=("contract_size_bytes", "contract_sha256", "updated_at"))
    else:
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        configured = {
            rule.field_definition.key: {
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
            }
            for rule in target_rules
        }
        encoded = _canonical_bytes(_profile_contract(target, target_rules))
        if (
            target.contract_schema_version != 2
            or target.status not in ("published", "retired")
            or target.source_projection_key != "classified_reference_context_ir_v1"
            or configured != GENERATION_CONTRACTS
            or target.contract_size_bytes != len(encoded)
            or target.contract_sha256 != hashlib.sha256(encoded).hexdigest()
        ):
            raise RuntimeError(
                "Cannot activate H3 Ref2VA PromptProfile v5: existing version is not the reviewed schema v2 contract",
            )

    source_binding = Binding.objects.filter(key=SOURCE_BINDING_KEY).first()
    if source.source_projection_key != "classified_reference_context_ir_v1":
        Profile.objects.filter(pk=source.pk).update(
            source_projection_key="classified_reference_context_ir_v1",
        )
    if source_binding is None:
        return
    target_binding, _created = Binding.objects.get_or_create(
        key=TARGET_BINDING_KEY,
        defaults={
            "name": "MiniMax H3 Ref2VA 官方六段合同 v5 / Official Context-IR v5",
            "operation": source_binding.operation,
            "model_family": source_binding.model_family,
            "mode": source_binding.mode,
            "model_profile_version_id": source_binding.model_profile_version_id,
            "prompt_profile_version_id": target.id,
            "priority": source_binding.priority,
            "is_active": True,
        },
    )
    if target_binding.prompt_profile_version_id != target.id:
        raise RuntimeError(
            "Cannot activate H3 Ref2VA PromptProfile v5: binding key already points elsewhere",
        )
    Binding.objects.filter(pk=target_binding.pk).update(is_active=True)
    Binding.objects.filter(pk=source_binding.pk).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("studio", "0071_prompt_profile_field_generation_contract")]

    operations = [
        migrations.AddField(
            model_name="promptprofileversion",
            name="source_projection_key",
            field=models.CharField(
                default="direct_v1",
                help_text="受控确定性来源投影器；普通策略使用 direct_v1。",
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[a-z][a-z0-9_.-]{1,63}$",
                    ),
                ],
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_instruction",
            field=models.TextField(
                blank=True,
                default="",
                help_text="版本化、不可执行的字段生成说明；禁止模板、脚本和动态表达式。",
                validators=[django.core.validators.MaxLengthValidator(4000)],
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_output_semantics",
            field=models.CharField(
                choices=[
                    ("final", "最终字段值 / Final field value"),
                    ("contribution", "确定性投影贡献 / Projection contribution"),
                ],
                default="final",
                max_length=16,
            ),
        ),
        migrations.RunPython(create_profile_v5, migrations.RunPython.noop),
    ]
