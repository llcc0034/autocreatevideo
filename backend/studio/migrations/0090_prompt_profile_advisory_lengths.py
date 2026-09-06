import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 13
TARGET_VERSION = 14
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 十二长句播放序 v13"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 建议长度与人工复核 v14"
ADVISORY_VALIDATOR = "length_advisory"
DETAIL_FIELD_KEY = "detailed_description"


_previous = importlib.import_module(
    "studio.migrations.0087_prompt_profile_twelve_sentence_scaffold",
)
_canonical_bytes = _previous._canonical_bytes
_profile_contract = _previous._profile_contract
_rule_signature = _previous._rule_signature
DETAIL_GENERATION_INSTRUCTION = (
    _previous.DETAIL_GENERATION_INSTRUCTION
    .replace(
        "Produce exactly twelve complete sentences",
        "Target twelve complete sentences",
    )
    .replace(
        "must remain inside the official inclusive 350-500 English word contract.",
        "should target the official recommended 350-500 English word range; "
        "falling outside that range produces an advisory review warning rather "
        "than blocking a complete truthful result.",
    )
    .replace(
        "Count and revise the twelve sentences before returning so their contribution "
        "satisfies both the sentence targets and the 350-500 English word validator "
        "without violating frozen facts.",
        "Count and revise the target sentences before returning so their contribution "
        "aims for both the sentence targets and the recommended 350-500 English word "
        "range without violating frozen facts; when those goals conflict, preserve "
        "complete frozen facts and allow the server to record an advisory warning.",
    )
)
ADVISORY_INSTRUCTION_SUFFIX = (
    " Treat the configured length minimum and maximum as a recommended target; "
    "never omit a complete truthful result solely to satisfy that target."
)


def _advisory_rule(rule):
    return (
        rule.generation_mode in {"model_generated", "translate_only"}
        and rule.length_unit != "none"
        and (
            rule.generation_min is not None
            or rule.generation_max is not None
        )
    )


def _target_instruction(rule):
    if rule.field_definition.key == DETAIL_FIELD_KEY:
        return DETAIL_GENERATION_INSTRUCTION
    if _advisory_rule(rule):
        return str(rule.generation_instruction or "") + ADVISORY_INSTRUCTION_SUFFIX
    return rule.generation_instruction


def _target_validators(rule):
    values = list(rule.generation_validator_keys or [])
    if _advisory_rule(rule) and ADVISORY_VALIDATOR not in values:
        values.append(ADVISORY_VALIDATOR)
    return values


def _target_signature(rule):
    source = _rule_signature(
        rule,
        generation_instruction=_target_instruction(rule),
    )
    return (*source[:-1], _target_validators(rule))


def publish_advisory_length_successor(apps, schema_editor):
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
        source.contract_schema_version != 4
        or source.operation != "video.generate"
        or source.source_projection_key != "classified_reference_context_ir_v1"
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v14: v13 identity or projection drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    generated_rules = [rule for rule in source_rules if _advisory_rule(rule)]
    if (
        len(source_rules) != 6
        or {rule.field_definition.key for rule in generated_rules}
        != {
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        }
        or any(
            ADVISORY_VALIDATOR in (rule.generation_validator_keys or [])
            for rule in source_rules
        )
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v14: v13 six-section length contract drifted",
        )
    detail_rule = next(
        rule
        for rule in source_rules
        if rule.field_definition.key == DETAIL_FIELD_KEY
    )
    if (
        detail_rule.length_unit != "english_words"
        or detail_rule.generation_min != 350
        or detail_rule.generation_max != 500
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v14: official detail target drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v14: v13 contract checksum drifted",
        )

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=source.profile_key,
            version=TARGET_VERSION,
            name=(
                "MiniMax H3 Ref2VA 建议长度与人工复核 / "
                "Advisory Lengths and Human Review"
            ),
            description=(
                "保持官方六段与十二句生成目标；三段篇幅范围作为可审计建议值，"
                "越界不阻断，完整性、安全性与受保护内容仍严格校验。"
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
            contract_schema_version=4,
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
                generation_instruction=_target_instruction(source_rule),
                generation_output_semantics=source_rule.generation_output_semantics,
                generation_required=source_rule.generation_required,
                language_policy=source_rule.language_policy,
                length_unit=source_rule.length_unit,
                generation_min=source_rule.generation_min,
                generation_max=source_rule.generation_max,
                generation_validator_keys=_target_validators(source_rule),
            )

    target_rules = list(
        Rule.objects.filter(profile_version_id=target.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    encoded = _canonical_bytes(_profile_contract(target, target_rules))
    digest = hashlib.sha256(encoded).hexdigest()
    if (
        target.profile_key != source.profile_key
        or target.version != TARGET_VERSION
        or target.model_family != source.model_family
        or target.operation != source.operation
        or target.mode != source.mode
        or target.source_projection_key != source.source_projection_key
        or target.source_projection_config != source.source_projection_config
        or target.channel_limits != source.channel_limits
        or target.contract_schema_version != 4
        or target.status != "published"
        or target.review_state != "ready"
        or [_rule_signature(rule) for rule in target_rules]
        != [_target_signature(rule) for rule in source_rules]
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v14: contract drifted")
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = digest
        target.save(
            update_fields=(
                "contract_size_bytes",
                "contract_sha256",
                "updated_at",
            ),
        )
    elif (
        target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != digest
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v14: checksum drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v14: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v14: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


def restore_v13_binding(apps, schema_editor):
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
        raise RuntimeError("Cannot reverse H3 PromptProfile v14: v13 source missing")
    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None or binding.prompt_profile_version_id not in {
        source.id,
        target.id,
    }:
        raise RuntimeError("Cannot reverse H3 PromptProfile v14: binding drifted")
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0089_prompt_normalization_reasoning_budget"),
    ]

    operations = [
        migrations.RunPython(
            publish_advisory_length_successor,
            restore_v13_binding,
        ),
    ]
