import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 14
TARGET_VERSION = 15
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 建议长度与人工复核 v14"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 官方引用布局 v15"
DETAIL_FIELD_KEY = "detailed_description"
TARGET_SOURCE_PROJECTION_CONFIG = {
    "schema_version": 1,
    "classified_reference_context_policy": {
        "retention_analysis": {
            "include_subject_shot_usage": True,
            "assignment_separator": " - ",
        },
        "detailed_description": {
            "style_opening_placement": "before_first_shot",
            "style_opening_max_sentences": 2,
            "subject_reference_placement": "first_clear_appearance",
        },
    },
}


_previous = importlib.import_module(
    "studio.migrations.0090_prompt_profile_advisory_lengths",
)
_canonical_bytes = _previous._canonical_bytes
_profile_contract = _previous._profile_contract
_rule_signature = _previous._rule_signature
DETAIL_GENERATION_INSTRUCTION = (
    _previous.DETAIL_GENERATION_INSTRUCTION
    + " The final deterministic renderer places one or two locked visual-style "
    "sentences before [Shot 1]; do not duplicate that opening or invent a new "
    "style. Mention each supplied protected Subject token at its first clear "
    "visual appearance in playback order. If the frozen input does not expose "
    "a Subject token to this contribution, do not invent or renumber one; the "
    "configured server projection inserts the locked reference label at that "
    "subject's first shot appearance."
)


def _target_instruction(rule):
    if rule.field_definition.key == DETAIL_FIELD_KEY:
        return DETAIL_GENERATION_INSTRUCTION
    return rule.generation_instruction


def _target_signature(rule):
    return _rule_signature(
        rule,
        generation_instruction=_target_instruction(rule),
    )


def publish_official_reference_layout_successor(apps, schema_editor):
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
        or source.mode != "Ref2VA"
        or source.source_projection_key != "classified_reference_context_ir_v1"
        or source.source_projection_config != {}
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v15: v14 identity or projection drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    expected_fields = [
        "subject_definitions",
        "summary",
        "retention_analysis",
        "detailed_description",
        "overall_soundscape",
        "non_diegetic_music",
    ]
    if [rule.field_definition.key for rule in source_rules] != expected_fields:
        raise RuntimeError(
            "Cannot create H3 PromptProfile v15: v14 six-section order drifted",
        )
    detail_rule = source_rules[3]
    if (
        detail_rule.generation_mode != "model_generated"
        or detail_rule.generation_output_semantics != "contribution"
        or detail_rule.length_unit != "english_words"
        or detail_rule.generation_min != 350
        or detail_rule.generation_max != 500
        or "length_advisory" not in (detail_rule.generation_validator_keys or [])
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v15: v14 advisory detail contract drifted",
        )
    if any(
        rule.generation_mode != "server_locked"
        or rule.generation_output_semantics != "final"
        for rule in source_rules[:3]
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v15: v14 locked sections drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v15: v14 contract checksum drifted",
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
                "MiniMax H3 Ref2VA 官方引用布局 / "
                "Official Reference Layout"
            ),
            description=(
                "保持官方六段、对白锁定与建议长度；由版本化投影配置声明 retention "
                "镜头出现标记、ASCII 分隔符、首镜头前风格开场和 Subject 首次引用位置。"
            ),
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            source_projection_config=TARGET_SOURCE_PROJECTION_CONFIG,
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
                generation_output_semantics=(
                    source_rule.generation_output_semantics
                ),
                generation_required=source_rule.generation_required,
                language_policy=source_rule.language_policy,
                length_unit=source_rule.length_unit,
                generation_min=source_rule.generation_min,
                generation_max=source_rule.generation_max,
                generation_validator_keys=source_rule.generation_validator_keys,
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
        or target.source_projection_config != TARGET_SOURCE_PROJECTION_CONFIG
        or target.channel_limits != source.channel_limits
        or target.contract_schema_version != 4
        or target.status != "published"
        or target.review_state != "ready"
        or [_rule_signature(rule) for rule in target_rules]
        != [_target_signature(rule) for rule in source_rules]
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v15: contract drifted")
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
        raise RuntimeError("Cannot activate H3 PromptProfile v15: checksum drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v15: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v15: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


def restore_v14_binding(apps, schema_editor):
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
        raise RuntimeError("Cannot reverse H3 PromptProfile v15: v14 source missing")
    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None or binding.prompt_profile_version_id not in {
        source.id,
        target.id,
    }:
        raise RuntimeError("Cannot reverse H3 PromptProfile v15: binding drifted")
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0092_signed_media_throttle_scope"),
    ]

    operations = [
        migrations.RunPython(
            publish_official_reference_layout_successor,
            restore_v14_binding,
        ),
    ]
