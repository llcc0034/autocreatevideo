import hashlib
import json

from django.db import migrations, models
from django.utils import timezone


PROFILE_KEY = "z-image.bible.reference"
TARGET_VERSION = 2
TARGET_BINDING_KEY = "z-image-turbo-bible-v2"
H3_PROFILE_KEY = "minimax_h3.video.ref2va"
H3_TARGET_VERSION = 8
H3_TARGET_BINDING_KEY = "minimax-h3-ref2va-official-v8"
SOURCE_PROJECTION_CONFIG = {
    "schema_version": 1,
    "static_reference_policy": {
        "snapshot_selector": "initial",
        "default_target_kind": "character",
        "allow_context": {
            "default": False,
            "target_overrides": {"scene": True},
        },
        "context_source_selectors": [
            "request.environment",
            "request.lighting",
            "request.color_palette",
            "request.style",
        ],
        "transition_markers": [
            " and changes to ",
            " and changes once to ",
            " then changes to ",
            " changes once to ",
            " changes to ",
            " becomes ",
            " transforms into ",
            " switches to ",
            " turns into ",
            "→",
            "->",
            "随后变为",
            "然后变为",
            "变为",
            "变成",
            "转为",
            "切换为",
            "最终变为",
        ],
        "transition_patterns": [
            {
                "kind": "from_to",
                "from_marker": " changes from ",
                "to_marker": " to ",
            },
        ],
        "context_clause_policy": {
            "delimiters": [",", ";", "，", "；"],
            "discard_markers": [
                " holding ",
                " holds ",
                " held in ",
                " carrying ",
                " carries ",
                " interacting with ",
                " standing in ",
                " inside ",
                " beside ",
                " next to ",
                "手持",
                "拿着",
                "握着",
                "携带",
                "位于",
                "站在",
                "旁边",
                "内部",
            ],
        },
        "negative_token_exclusions": {
            "request.negative_constraints": ["readable text", "text"],
        },
        "forbidden_markers": {
            "always": [
                " simultaneously ",
                " both states ",
                " at the same time ",
                "同时显示",
                "两个状态",
                "两种状态",
            ],
            "when_context_disallowed": [
                " holding ",
                " holds ",
                " held in ",
                " carrying ",
                " carries ",
                " interacting with ",
                " standing in ",
                " inside ",
                " beside ",
                " next to ",
                "手持",
                "拿着",
                "握着",
                "携带",
                "位于",
                "站在",
                "旁边",
                "内部",
            ],
        },
        "violation_action": "block",
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


def create_static_reference_profile(apps, schema_editor):
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
        # A clean server still needs its image model/workflow/profile configured
        # through the control plane.  Do not invent a model-specific route when
        # no reviewed source contract exists.
        return
    if source.operation != "image.generate" or source.source_projection_key != "direct_v1":
        raise RuntimeError(
            "Cannot create static Bible PromptProfile v2: source contract drifted",
        )
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    required_context_selectors = set(
        SOURCE_PROJECTION_CONFIG["static_reference_policy"][
            "context_source_selectors"
        ],
    )
    if not source_rules or not required_context_selectors.issubset({
        rule.source_selector for rule in source_rules
    }):
        raise RuntimeError(
            "Cannot create static Bible PromptProfile v2: source rules are incomplete",
        )

    target = Profile.objects.filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=PROFILE_KEY,
            version=TARGET_VERSION,
            name="Bible 单一静态初态参考 / Single Static Initial Reference",
            description=(
                "由 PromptProfile schema v4 冻结静态参考策略：按配置选择初态、"
                "按目标类型控制上下文，并在图片模型调用前阻断残留的多状态或跨实体关系。"
            ),
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            source_projection_config=SOURCE_PROJECTION_CONFIG,
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
                generation_instruction=source_rule.generation_instruction,
                generation_output_semantics=source_rule.generation_output_semantics,
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
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = digest
        target.save(update_fields=(
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))
    elif (
        target.contract_schema_version != 4
        or target.status not in ("published", "retired")
        or target.source_projection_config != SOURCE_PROJECTION_CONFIG
        or target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != digest
    ):
        raise RuntimeError(
            "Cannot activate static Bible PromptProfile v2: existing contract drifted",
        )

    source_binding = (
        Binding.objects.filter(
            prompt_profile_version_id=source.id,
            is_active=True,
        )
        .order_by("-priority", "key")
        .first()
    ) or (
        Binding.objects.filter(prompt_profile_version_id=source.id)
        .order_by("-priority", "key")
        .first()
    )
    if source_binding is None:
        return
    target_binding, _created = Binding.objects.get_or_create(
        key=TARGET_BINDING_KEY,
        defaults={
            "name": "Bible 单一静态初态参考 v2",
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
            "Cannot activate static Bible PromptProfile v2: binding key drifted",
        )
    Binding.objects.filter(
        operation=source_binding.operation,
        model_family=source_binding.model_family,
        mode=source_binding.mode,
        model_profile_version_id=source_binding.model_profile_version_id,
        is_active=True,
    ).exclude(pk=target_binding.pk).update(is_active=False)
    Binding.objects.filter(pk=target_binding.pk).update(is_active=True)


def upgrade_h3_profile_contract_v4(apps, schema_editor):
    """Carry the active configured H3 contract into schema v4 unchanged.

    This is control-plane seed data, not a runtime model branch.  The generic
    runtime requires every newly selected PromptProfile to use the current
    contract schema, so the active v3 profile needs an immutable v4 successor
    even though it does not opt into a source projection configuration.
    """

    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    source = (
        Profile.objects.filter(
            profile_key=H3_PROFILE_KEY,
            status__in=("published", "retired"),
            version__lt=H3_TARGET_VERSION,
        )
        .order_by("-version")
        .first()
    )
    if source is None:
        return
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if not source_rules:
        raise RuntimeError("Cannot create H3 PromptProfile v8: source rules are empty")
    target = Profile.objects.filter(
        profile_key=H3_PROFILE_KEY,
        version=H3_TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=H3_PROFILE_KEY,
            version=H3_TARGET_VERSION,
            name=source.name,
            description=source.description,
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            source_projection_config={},
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
                generation_instruction=source_rule.generation_instruction,
                generation_output_semantics=source_rule.generation_output_semantics,
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
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = digest
        target.save(update_fields=(
            "contract_size_bytes", "contract_sha256", "updated_at",
        ))
    elif (
        target.contract_schema_version != 4
        or target.status not in ("published", "retired")
        or target.source_projection_config != {}
        or target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != digest
    ):
        raise RuntimeError("Cannot activate H3 PromptProfile v8: contract drifted")
    source_binding = (
        Binding.objects.filter(
            prompt_profile_version_id=source.id,
            is_active=True,
        )
        .order_by("-priority", "key")
        .first()
    ) or (
        Binding.objects.filter(prompt_profile_version_id=source.id)
        .order_by("-priority", "key")
        .first()
    )
    if source_binding is None:
        return
    target_binding, _created = Binding.objects.get_or_create(
        key=H3_TARGET_BINDING_KEY,
        defaults={
            "name": "MiniMax H3 Ref2VA 来源投影配置合同 v8",
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
        raise RuntimeError("Cannot activate H3 PromptProfile v8: binding drifted")
    Binding.objects.filter(
        operation=source_binding.operation,
        model_family=source_binding.model_family,
        mode=source_binding.mode,
        model_profile_version_id=source_binding.model_profile_version_id,
        is_active=True,
    ).exclude(pk=target_binding.pk).update(is_active=False)
    Binding.objects.filter(pk=target_binding.pk).update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [("studio", "0076_prompt_profile_channel_limits_v3")]

    operations = [
        migrations.AddField(
            model_name="promptprofileversion",
            name="source_projection_config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "来源投影器的版本化数据合同；只接受服务端已登记的声明式配置，"
                    "不接受模板、函数或自定义正则。"
                ),
            ),
        ),
        migrations.RunPython(
            upgrade_h3_profile_contract_v4,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            create_static_reference_profile,
            migrations.RunPython.noop,
        ),
    ]
