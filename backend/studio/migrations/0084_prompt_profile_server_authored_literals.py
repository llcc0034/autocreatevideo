import hashlib
import json

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 9
TARGET_VERSION = 10
CURRENT_BINDING_KEY = "minimax-h3-ref2va-official-v8"
DETAIL_FIELD_KEY = "detailed_description"
DETAIL_GENERATION_INSTRUCTION = (
    "Expand the frozen shot facts into 350–500 English words of full-reference "
    "director prose. Use the exact <Subject N>, <Picture N>, <Video N>, and "
    "<Audio N> labels; write [Shot 1] and later [Shot N] At MM:SS.mmm; describe "
    "composition, referenced appearance and positions, environment and lighting, "
    "visible actions and state changes, camera movement, synchronized sound, and "
    "the exact points where referenced content takes effect. Do not emit, translate, "
    "paraphrase, quote, wrap, or reconstruct authored dialogue, lyrics, or visible "
    "text; the deterministic server projection appends those frozen original-language "
    "blocks after review. You may describe only supported speaker delivery, lip motion, "
    "timing, and interaction with visible action in English. Preserve exact duration, "
    "continuity, and reference identities; do not invent unsupported events."
)


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def publish_server_authored_literal_successor(apps, schema_editor):
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
            "Cannot create H3 PromptProfile v10: v9 identity or projection drifted",
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
            "Cannot create H3 PromptProfile v10: v9 six-section fields drifted",
        )
    source_encoded = _canonical_bytes(_profile_contract(source, source_rules))
    if (
        source.contract_size_bytes != len(source_encoded)
        or source.contract_sha256
        != hashlib.sha256(source_encoded).hexdigest()
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v10: v9 contract checksum drifted",
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
                "MiniMax H3 Ref2VA 服务端原文块 / Server-authored Literals"
            ),
            description=(
                "模型只生成配置声明的英文贡献字段；对白、歌词和真实屏显文字由"
                "确定性 source projection 从冻结事实中追加，Provider 不得自行构造原文标签。"
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
        raise RuntimeError("Cannot activate H3 PromptProfile v10: contract drifted")
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
        raise RuntimeError("Cannot activate H3 PromptProfile v10: contract drifted")

    binding = Binding.objects.filter(key=CURRENT_BINDING_KEY).first()
    if binding is None:
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v10: current binding is missing",
        )
    if (
        binding.operation != source.operation
        or binding.model_family != source.model_family
        or binding.mode != source.mode
        or binding.prompt_profile_version_id not in {source.id, target.id}
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v10: current binding drifted",
        )
    Binding.objects.filter(pk=binding.pk).update(
        name="MiniMax H3 Ref2VA 服务端原文块 v10",
        prompt_profile_version_id=target.id,
        is_active=True,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0083_prompt_profile_protected_token_policy")]

    operations = [
        migrations.RunPython(
            publish_server_authored_literal_successor,
            migrations.RunPython.noop,
        ),
    ]
