import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 12
TARGET_VERSION = 13
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 连续散文覆盖预算 v12"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 十二长句播放序 v13"
DETAIL_FIELD_KEY = "detailed_description"
DETAIL_GENERATION_INSTRUCTION = (
    "This is generation, not translation or summarization; the Chinese source is a "
    "frozen fact ledger. Write only the detailed_description as one continuous "
    "playback-order English director-prose block. Produce exactly twelve complete "
    "sentences, each targeting 30-36 English words, for a target total of 360-432 "
    "English words; the returned contribution itself, excluding server-appended "
    "authored-literal blocks, must remain inside the official inclusive 350-500 "
    "English word contract. A single shot does not reduce this budget. Do not output "
    "sentence numbers, headings, section labels, bullets, an outline, a checklist, or "
    "word-count commentary. Keep the twelve sentences as natural playback-order prose "
    "in the order of the supplied shot and timeline facts, and distribute their focus "
    "without labeling them: the "
    "first establishes supported composition and screen position; the second records "
    "referenced subject appearance, materials, and reference relationships; the third "
    "establishes supported subject-object and environment depth relationships; the "
    "fourth develops supported lighting and surface response; the fifth follows the "
    "earliest supplied action phase; the sixth follows mechanically necessary "
    "intermediate motion inside that supplied interval; the seventh follows the next "
    "supplied action phase and its supported physical consequence; the eighth follows "
    "camera framing, movement, focus, and camera-subject geometry; the ninth follows "
    "synchronized physical sound, supported speaker delivery or lip motion, and their "
    "supplied timing; the tenth follows supported reference-effect timing; the "
    "eleventh carries the last supplied action phase into its continuity state; and "
    "the twelfth resolves only the supported ending composition and continuity facts. "
    "For multiple shots or timeline phases, continue this allocation across them in "
    "their supplied playback order instead of grouping details by category. Preserve "
    "only [Shot N] and time markers already present in the frozen facts; never create "
    "a new numbered marker or timestamp. Preserve every server-provided "
    "protected_tokens placeholder exactly as presented, at least once and in its "
    "supplied order; the server restores official reference labels after validation. "
    "When the frozen ledger is compact, expand only observable spatial relationships, "
    "supported material or surface and light response, mechanically necessary causal "
    "motion between supplied states, camera-subject geometry, depth, framing, and "
    "timing inside supplied intervals. These are authorized elaborations, not new "
    "story facts. Do not add an unsupported identity, age, color, material, "
    "expression, object, sound, location, weather, event, action, dialogue, or "
    "outcome. Do not emit, translate, paraphrase, quote, wrap, or reconstruct authored "
    "dialogue, lyrics, or visible text; the deterministic server projection appends "
    "those frozen original-language blocks after review. Count and revise the twelve "
    "sentences before returning so their contribution satisfies both the sentence "
    "targets and the 350-500 English word validator without violating frozen facts."
)


_previous = importlib.import_module(
    "studio.migrations.0086_prompt_profile_coverage_pass_budget",
)
_canonical_bytes = _previous._canonical_bytes
_profile_contract = _previous._profile_contract
_rule_signature = _previous._rule_signature


def _target_instruction(rule):
    if rule.field_definition.key == DETAIL_FIELD_KEY:
        return DETAIL_GENERATION_INSTRUCTION
    return rule.generation_instruction


def publish_twelve_sentence_scaffold_successor(apps, schema_editor):
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
            "Cannot create H3 PromptProfile v13: v12 identity or projection drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if (
        len(source_rules) != 6
        or sum(
            rule.field_definition.key == DETAIL_FIELD_KEY
            for rule in source_rules
        )
        != 1
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v13: v12 six-section fields drifted",
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
            "Cannot create H3 PromptProfile v13: official detail budget drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v13: v12 contract checksum drifted",
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
                "MiniMax H3 Ref2VA 十二长句播放序 / "
                "Twelve-Sentence Playback Scaffold"
            ),
            description=(
                "以十二个按播放顺序排列的完整长句替代私有覆盖轮次，保持官方六段、"
                "350-500 英文词校验、受保护占位符和服务端原文块。"
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
                generation_source_selector=(
                    source_rule.generation_source_selector
                ),
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
    expected_signatures = [
        _rule_signature(
            rule,
            generation_instruction=_target_instruction(rule),
        )
        for rule in source_rules
    ]
    configured_signatures = [_rule_signature(rule) for rule in target_rules]
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
        or configured_signatures != expected_signatures
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v13: contract drifted")
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = digest
        target.save(
            update_fields=(
                "contract_size_bytes",
                "contract_sha256",
                "updated_at",
            )
        )
    elif (
        target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != digest
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v13: contract drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v13: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v13: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


def restore_coverage_pass_budget_binding(apps, schema_editor):
    """Restore the v12 binding without deleting published v13 audit facts."""

    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        return
    source = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
        status__in=("published", "retired"),
    ).first()
    if source is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v13: v12 source is missing",
        )

    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    target_rules = list(
        Rule.objects.filter(profile_version_id=target.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    detail_rule = next(
        (
            rule
            for rule in source_rules
            if rule.field_definition.key == DETAIL_FIELD_KEY
        ),
        None,
    )
    if (
        source.contract_schema_version != 4
        or source.operation != "video.generate"
        or source.source_projection_key != "classified_reference_context_ir_v1"
        or len(source_rules) != 6
        or len(target_rules) != 6
        or detail_rule is None
        or detail_rule.length_unit != "english_words"
        or detail_rule.generation_min != 350
        or detail_rule.generation_max != 500
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v13: managed contract drifted",
        )

    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    target_encoded = _canonical_bytes(_profile_contract(target, target_rules))
    expected_target_signatures = [
        _rule_signature(
            rule,
            generation_instruction=_target_instruction(rule),
        )
        for rule in source_rules
    ]
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
        or target.contract_schema_version != 4
        or target.operation != source.operation
        or target.model_family != source.model_family
        or target.mode != source.mode
        or target.source_projection_key != source.source_projection_key
        or target.source_projection_config != source.source_projection_config
        or target.channel_limits != source.channel_limits
        or target.status != "published"
        or target.review_state != "ready"
        or [_rule_signature(rule) for rule in target_rules]
        != expected_target_signatures
        or target.contract_size_bytes != len(target_encoded)
        or target.contract_sha256
        != hashlib.sha256(target_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v13: contract checksum drifted",
        )

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v13: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v13: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0086_prompt_profile_coverage_pass_budget"),
    ]

    operations = [
        migrations.RunPython(
            publish_twelve_sentence_scaffold_successor,
            restore_coverage_pass_budget_binding,
        ),
    ]
