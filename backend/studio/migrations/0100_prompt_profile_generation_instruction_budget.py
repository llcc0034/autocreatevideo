import hashlib
import json

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 16
TARGET_VERSION = 17
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 镜头级上下文隔离 v16"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 镜头级上下文隔离 v17"
TARGET_PROFILE_NAME = (
    "MiniMax H3 Ref2VA 镜头级上下文隔离 v17 / Shot-scoped Context Isolation"
)
MODEL_GENERATED_FIELDS = {
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
}
TARGET_CONTEXT_SELECTORS = [
    "project.style",
    "target_shot.core",
    "target_shot.dialogue_metadata",
    "target_shot.linked_bible",
]
SOURCE_CONTEXT_INSTRUCTION = (
    " The current target shot is the only authoritative shot scope. Its dialogue "
    "metadata is authoritative only for speaker identity, delivery, timing, and "
    "voiceover status. Do not quote, paraphrase, translate, or infer authored "
    "dialogue text, and never use another shot's dialogue. Authored dialogue is "
    "locked and assembled separately by the deterministic server renderer."
)
TARGET_CONTEXT_INSTRUCTION = (
    " Only target-shot facts are authoritative. Dialogue metadata covers speaker "
    "identity, delivery, timing and voiceover, not text. Never infer, quote, "
    "translate, or use other-shot dialogue; server locks and separately assembles "
    "authored dialogue."
)
MAX_GENERATION_INSTRUCTION_CHARS = 4000
EXPECTED_FIELDS = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _rule_payload(rule):
    return {
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
        "generation_context_selectors": rule.generation_context_selectors,
    }


def _profile_contract(profile, rules):
    return {
        "schema_version": 5,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "source_projection_key": profile.source_projection_key,
        "channel_limits": profile.channel_limits,
        "source_projection_config": profile.source_projection_config,
        "rules": [_rule_payload(rule) for rule in rules],
    }


def _condensed_instruction(source_rule):
    instruction = source_rule.generation_instruction
    if source_rule.field_definition.key not in MODEL_GENERATED_FIELDS:
        return instruction
    if not instruction.endswith(SOURCE_CONTEXT_INSTRUCTION):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v17: v16 context instruction drifted",
        )
    condensed = (
        instruction[: -len(SOURCE_CONTEXT_INSTRUCTION)].rstrip()
        + TARGET_CONTEXT_INSTRUCTION
    )
    if len(condensed) > MAX_GENERATION_INSTRUCTION_CHARS:
        raise RuntimeError(
            "Cannot create H3 PromptProfile v17: condensed instruction exceeds 4000 chars",
        )
    return condensed


def publish_instruction_budget_successor(apps, schema_editor):
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
    if (
        source.contract_schema_version != 5
        or source.operation != "video.generate"
        or source.mode != "Ref2VA"
        or source.source_projection_key != "classified_reference_context_ir_v1"
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v17: v16 identity drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if [rule.field_definition.key for rule in source_rules] != EXPECTED_FIELDS:
        raise RuntimeError(
            "Cannot create H3 PromptProfile v17: v16 six-section order drifted",
        )
    for rule in source_rules:
        expected_context = (
            TARGET_CONTEXT_SELECTORS
            if rule.field_definition.key in MODEL_GENERATED_FIELDS else []
        )
        if rule.generation_context_selectors != expected_context:
            raise RuntimeError(
                "Cannot create H3 PromptProfile v17: v16 context scope drifted",
            )
        _condensed_instruction(rule)
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v17: v16 checksum drifted",
        )

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=source.profile_key,
            version=TARGET_VERSION,
            name=TARGET_PROFILE_NAME,
            description=(
                "保持 v16 的字段级镜头上下文授权，仅精简生成说明，"
                "使其满足通用 4000 字符安全上限。"
            ),
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            source_projection_config=source.source_projection_config,
            official_reference=source.official_reference,
            channel_limits=source.channel_limits,
            review_state="ready",
            status="published",
            contract_schema_version=5,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
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
                generation_instruction=_condensed_instruction(source_rule),
                generation_output_semantics=(
                    source_rule.generation_output_semantics
                ),
                generation_required=source_rule.generation_required,
                language_policy=source_rule.language_policy,
                length_unit=source_rule.length_unit,
                generation_min=source_rule.generation_min,
                generation_max=source_rule.generation_max,
                generation_validator_keys=source_rule.generation_validator_keys,
                generation_context_selectors=(
                    source_rule.generation_context_selectors
                ),
            )

    target_rules = list(
        Rule.objects.filter(profile_version_id=target.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if (
        target.name != TARGET_PROFILE_NAME
        or target.model_family != source.model_family
        or target.operation != source.operation
        or target.mode != source.mode
        or target.source_projection_key != source.source_projection_key
        or target.source_projection_config != source.source_projection_config
        or target.channel_limits != source.channel_limits
        or target.contract_schema_version != 5
        or target.status != "published"
        or target.review_state != "ready"
        or [rule.field_definition.key for rule in target_rules] != EXPECTED_FIELDS
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v17: contract identity drifted",
        )
    for source_rule, target_rule in zip(source_rules, target_rules, strict=True):
        expected_payload = _rule_payload(source_rule)
        expected_payload["generation_instruction"] = _condensed_instruction(
            source_rule,
        )
        if _rule_payload(target_rule) != expected_payload:
            raise RuntimeError(
                "Cannot activate H3 PromptProfile v17: copied rule drifted",
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
            "Cannot activate H3 PromptProfile v17: checksum drifted",
        )
    if target.contract_sha256 == source.contract_sha256:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v17: v16 smoke hash remained valid",
        )

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v17: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v17: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )
    Profile.objects.filter(pk=source.pk, status="published").update(
        status="retired",
        updated_at=timezone.now(),
    )


def restore_v16_binding_and_remove_v17(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    source = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
        status__in=("published", "retired"),
    ).first()
    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        return
    if source is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v17: v16 source missing",
        )
    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None or binding.prompt_profile_version_id not in {
        source.id,
        target.id,
    }:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v17: binding drifted",
        )
    Profile.objects.filter(pk=source.pk).update(
        status="published",
        updated_at=timezone.now(),
    )
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )
    target.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0099_prompt_profile_generation_context_scope"),
    ]

    operations = [
        migrations.RunPython(
            publish_instruction_budget_successor,
            restore_v16_binding_and_remove_v17,
        ),
    ]
