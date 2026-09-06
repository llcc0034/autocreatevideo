import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 11
TARGET_VERSION = 12
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 官方细节预算 v11"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 连续散文覆盖预算 v12"
DETAIL_FIELD_KEY = "detailed_description"
DETAIL_GENERATION_INSTRUCTION = (
    "This is generation, not translation or summarization; the Chinese source is a "
    "frozen fact ledger. Write only the detailed_description as continuous English "
    "director prose. The returned contribution itself, excluding server-appended "
    "authored-literal blocks, must be 360-450 English words and therefore remains "
    "inside the official inclusive 350-500 English word contract. A single shot does "
    "not reduce this budget. Privately draft all seven coverage passes before merging "
    "them: reserve about 45-55 words "
    "for composition and screen position; 55-65 for referenced subject appearance, "
    "materials, and supported spatial relationships; 45-55 for environment depth, "
    "lighting, and supported surface response; 80-100 for chronological action "
    "phases, mechanically necessary intermediate motion, and timing; 45-55 for "
    "camera framing, movement, focus, and camera-subject relationships; 50-60 for "
    "synchronized physical sound, supported delivery or lip motion, and reference-"
    "effect timing; and 40-60 for the final continuity state and ending composition. "
    "These are private drafting budgets, not output sections: merge every supported "
    "detail into natural playback-order prose, emit no coverage-pass names, headings, "
    "outline, checklist, bullets, or word-count commentary, and do not omit supported "
    "coverage merely because the shot is short. Preserve only [Shot N] and time "
    "markers already present in the frozen facts; do not create a new numbered marker "
    "or timestamp. Preserve every server-provided protected_tokens placeholder exactly "
    "as presented, at least once; the server restores official reference labels after "
    "validation. If one pass has too few facts, "
    "move its unused words to supported chronology, framing, depth, light response, "
    "or continuity instead of inventing content. Expanding observable spatial "
    "relationships, supported material or surface and light response, mechanically "
    "necessary causal motion between supplied states, camera-subject geometry, and "
    "timing inside supplied intervals is authorized elaboration of the frozen ledger, "
    "not creation of a new fact. Do not add an unsupported identity, age, color, "
    "material, expression, object, sound, location, weather, event, action, dialogue, "
    "or outcome. Do not emit, translate, "
    "paraphrase, quote, wrap, or reconstruct authored dialogue, lyrics, or visible "
    "text; the deterministic server projection appends those frozen original-language "
    "blocks after review. Count only the returned contribution itself, without those "
    "future server-appended blocks, and revise before returning until it is 360-450 "
    "English words without violating the frozen facts."
)


_previous = importlib.import_module(
    "studio.migrations.0085_prompt_profile_official_detail_budget",
)
_canonical_bytes = _previous._canonical_bytes
_profile_contract = _previous._profile_contract


def _target_instruction(rule):
    if rule.field_definition.key == DETAIL_FIELD_KEY:
        return DETAIL_GENERATION_INSTRUCTION
    return rule.generation_instruction


def _rule_signature(rule, *, generation_instruction=None):
    return (
        rule.field_definition_id,
        rule.source_selector,
        rule.channel,
        rule.position,
        rule.label_style,
        rule.separator,
        rule.required,
        rule.omit_if_empty,
        rule.max_chars,
        rule.generation_mode,
        rule.generation_source_selector,
        (
            rule.generation_instruction
            if generation_instruction is None
            else generation_instruction
        ),
        rule.generation_output_semantics,
        rule.generation_required,
        rule.language_policy,
        rule.length_unit,
        rule.generation_min,
        rule.generation_max,
        list(rule.generation_validator_keys or []),
    )


def publish_coverage_pass_budget_successor(apps, schema_editor):
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
            "Cannot create H3 PromptProfile v12: v11 identity or projection drifted",
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
        ) != 1
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v12: v11 six-section fields drifted",
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
            "Cannot create H3 PromptProfile v12: official detail budget drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v12: v11 contract checksum drifted",
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
                "MiniMax H3 Ref2VA 连续散文覆盖预算 / "
                "Continuous Prose Coverage Budget"
            ),
            description=(
                "在字段生成合同中把细节覆盖拆成七个私有词数预算，再合并为"
                "连续时序散文；保留官方六段顺序、350-500 英文词校验和服务端原文块。"
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
        raise RuntimeError("Cannot activate H3 PromptProfile v12: contract drifted")
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
        raise RuntimeError("Cannot activate H3 PromptProfile v12: contract drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v12: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v12: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


def restore_official_detail_budget_binding(apps, schema_editor):
    """Restore the v11 binding without deleting published v12 audit facts."""

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
            "Cannot reverse H3 PromptProfile v12: v11 source is missing",
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
            "Cannot reverse H3 PromptProfile v12: managed contract drifted",
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
            "Cannot reverse H3 PromptProfile v12: contract checksum drifted",
        )

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v12: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v12: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0085_prompt_profile_official_detail_budget"),
    ]

    operations = [
        migrations.RunPython(
            publish_coverage_pass_budget_successor,
            restore_official_detail_budget_binding,
        ),
    ]
