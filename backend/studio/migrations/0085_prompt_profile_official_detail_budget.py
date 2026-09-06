import hashlib
import importlib

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 10
TARGET_VERSION = 11
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_BINDING_NAME = "MiniMax H3 Ref2VA 服务端原文块 v10"
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 官方细节预算 v11"
DETAIL_FIELD_KEY = "detailed_description"
DETAIL_GENERATION_INSTRUCTION = (
    "Write a complete 350-500 English word full-reference director description "
    "from only the frozen shot facts. A single shot does not justify a shorter "
    "description. Before returning, count the English words and revise until the "
    "result is inside the inclusive range. In natural playback-order prose, cover "
    "every supported composition and screen position, referenced appearance and "
    "material detail, environment depth and lighting response, chronological action "
    "phase and intermediate physical motion, camera framing and movement, synchronized "
    "physical sound timing, reference-effect timing, and final continuity state. Use "
    "the exact <Subject N>, <Picture N>, <Video N>, and <Audio N> labels; write "
    "[Shot 1] and later [Shot N] At MM:SS.mmm. When the frozen facts are compact, "
    "add only observable spatial relationships, surface and light response, causal "
    "intermediate motion, framing, depth, and timing that remain consistent with those "
    "facts; never add a new event, identity, object, location, or story outcome. Do not "
    "emit, translate, paraphrase, quote, wrap, or reconstruct authored dialogue, lyrics, "
    "or visible text; the deterministic server projection appends those frozen "
    "original-language blocks after review. You may describe only supported speaker "
    "delivery, lip motion, timing, and interaction with visible action in English."
)


_previous = importlib.import_module(
    "studio.migrations.0084_prompt_profile_server_authored_literals",
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


def publish_official_detail_budget_successor(apps, schema_editor):
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
            "Cannot create H3 PromptProfile v11: v10 identity or projection drifted",
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
            "Cannot create H3 PromptProfile v11: v10 six-section fields drifted",
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
            "Cannot create H3 PromptProfile v11: official detail budget drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v11: v10 contract checksum drifted",
        )

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=source.profile_key,
            version=TARGET_VERSION,
            name="MiniMax H3 Ref2VA 官方细节预算 / Official Detail Budget",
            description=(
                "保持官方 350-500 英文词范围，并把单镜头不缩短、逐类覆盖与"
                "有限事实扩写权限写入版本化字段生成合同。"
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
        raise RuntimeError("Cannot activate H3 PromptProfile v11: contract drifted")
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
        raise RuntimeError("Cannot activate H3 PromptProfile v11: contract drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v11: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v11: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=TARGET_BINDING_NAME,
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


def restore_server_authored_literal_binding(apps, schema_editor):
    """Restore the v10 runtime binding without deleting published v11 audit facts."""

    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        # The forward migration deliberately skips hosts that do not have the
        # managed v10 contract, so there is nothing to reverse on those hosts.
        return
    source = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
        status__in=("published", "retired"),
    ).first()
    if source is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v11: v10 source is missing",
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
    if (
        source.contract_schema_version != 4
        or source.operation != "video.generate"
        or source.source_projection_key != "classified_reference_context_ir_v1"
        or len(source_rules) != 6
        or len(target_rules) != 6
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v11: managed contract drifted",
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
        detail_rule is None
        or detail_rule.length_unit != "english_words"
        or detail_rule.generation_min != 350
        or detail_rule.generation_max != 500
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v11: official detail budget drifted",
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
            "Cannot reverse H3 PromptProfile v11: contract checksum drifted",
        )

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v11: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot reverse H3 PromptProfile v11: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name=SOURCE_BINDING_NAME,
        prompt_profile_version_id=source.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0084_prompt_profile_server_authored_literals"),
    ]

    operations = [
        migrations.RunPython(
            publish_official_detail_budget_successor,
            restore_server_authored_literal_binding,
        ),
    ]
