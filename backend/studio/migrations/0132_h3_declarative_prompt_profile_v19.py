from copy import deepcopy
import hashlib
import json

from django.db import migrations
from django.utils import timezone


PROFILE_KEY = "minimax_h3.video.ref2va"
SOURCE_VERSION = 18
TARGET_VERSION = 19
SOURCE_CONTRACT_SHA256 = (
    "018811b84783af16ca67233ed1a8fa0f0ef191c2111e28964e292d0720363de1"
)
SOURCE_BINDING_KEY = "minimax-h3-ref2va-official-v8"
TARGET_BINDING_KEY = "minimax-h3-ref2va-declarative-v19"
TARGET_PROFILE_NAME = (
    "MiniMax H3 Ref2VA 页面可配置原语组合 v19 / Declarative Primitive Composition"
)
TARGET_BINDING_NAME = "MiniMax H3 Ref2VA 声明式原语组合 v19"
EXPECTED_FIELDS = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]
PRIMITIVES = {
    "subject_definitions": "reference_context.subject_definitions",
    "summary": "reference_context.summary",
    "retention_analysis": "reference_context.retention_analysis",
    "detailed_description": "reference_context.detailed_description",
    "overall_soundscape": "reference_context.overall_soundscape",
    "non_diegetic_music": "reference_context.non_diegetic_music",
}


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


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
        "projection_primitive": rule.projection_primitive,
    }


def _profile_contract(profile, rules):
    return {
        "schema_version": 6,
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


def _profile_summary(profile):
    return {
        "profile_key": profile.profile_key,
        "version": profile.version,
        "status": profile.status,
        "review_state": profile.review_state,
        "contract_sha256": profile.contract_sha256,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
    }


def _binding_summary(binding):
    return {
        "key": binding.key,
        "is_active": binding.is_active,
        "operation": binding.operation,
        "model_family": binding.model_family,
        "mode": binding.mode,
        "model_profile_version_id": str(binding.model_profile_version_id or ""),
        "prompt_profile_version_id": str(binding.prompt_profile_version_id),
        "priority": binding.priority,
    }


def _audit(AuditEvent, *, action, object_type, object_id, before, after):
    AuditEvent.objects.create(
        actor_id=None,
        action=action,
        object_type=object_type,
        object_id=object_id,
        reason_code="h3_declarative_projection_v19",
        before_sha256=_sha(before) if before else "",
        after_sha256=_sha(after) if after else "",
        before_summary=before,
        after_summary=after,
    )


def _target_projection_config(source):
    config = deepcopy(source.source_projection_config)
    if (
        not isinstance(config, dict)
        or config.get("schema_version") != 1
        or set(config) - {
            "schema_version",
            "classified_reference_context_policy",
            "quality_advisories",
        }
        or "classified_reference_context_policy" not in config
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v19: v18 projection config drifted",
        )
    target = {
        "schema_version": 3,
        "primitive_options": {
            "reference_context_ir_v1": {
                "reference_context_policy": deepcopy(
                    config["classified_reference_context_policy"],
                ),
            },
        },
    }
    if "quality_advisories" in config:
        target["quality_advisories"] = deepcopy(config["quality_advisories"])
    return target


def publish_declarative_successor(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    AuditEvent = apps.get_model("studio", "PlatformLifecycleAuditEvent")

    source = Profile.objects.select_for_update().filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
    ).first()
    if source is None:
        return
    source_rules = list(
        Rule.objects.filter(profile_version_id=source.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    if (
        source.contract_schema_version != 5
        or source.contract_sha256 != SOURCE_CONTRACT_SHA256
        or source.operation != "video.generate"
        or source.mode != "Ref2VA"
        or source.source_projection_key != "classified_reference_context_ir_v1"
        or [rule.field_definition.key for rule in source_rules] != EXPECTED_FIELDS
    ):
        raise RuntimeError(
            "Cannot create H3 PromptProfile v19: v18 identity or contract drifted",
        )
    target_config = _target_projection_config(source)
    source_binding = Binding.objects.select_for_update().filter(
        key=SOURCE_BINDING_KEY,
        prompt_profile_version_id=source.id,
    ).first()
    if source_binding is None:
        raise RuntimeError(
            "Cannot create H3 PromptProfile v19: v18 binding is missing or repointed",
        )

    target = Profile.objects.select_for_update().filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        if source.status != "published" or not source_binding.is_active:
            raise RuntimeError(
                "Cannot create H3 PromptProfile v19: v18 route is not active",
            )
        target = Profile.objects.create(
            profile_key=PROFILE_KEY,
            version=TARGET_VERSION,
            name=TARGET_PROFILE_NAME,
            description=(
                "H3 六段格式由冻结字段规则选择 reference_context 原语；字段、顺序、"
                "生成贡献、通道和格式均可在页面创建后继草稿并版本化修改。"
            ),
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key="declarative_v1",
            source_projection_config=target_config,
            official_reference=source.official_reference,
            channel_limits=deepcopy(source.channel_limits),
            review_state="ready",
            status="published",
            contract_schema_version=6,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
            published_by_id=None,
        )
        for source_rule in source_rules:
            field_key = source_rule.field_definition.key
            Rule.objects.create(
                profile_version_id=target.id,
                field_definition_id=source_rule.field_definition_id,
                source_selector="projection.output",
                projection_primitive=PRIMITIVES[field_key],
                channel=source_rule.channel,
                position=source_rule.position,
                label_style=source_rule.label_style,
                separator=source_rule.separator,
                required=source_rule.required,
                omit_if_empty=source_rule.omit_if_empty,
                max_chars=source_rule.max_chars,
                generation_mode=source_rule.generation_mode,
                generation_source_selector=(
                    "projection.output"
                    if source_rule.generation_mode == "server_locked"
                    else source_rule.generation_source_selector
                ),
                generation_instruction=source_rule.generation_instruction,
                generation_output_semantics=(
                    source_rule.generation_output_semantics
                ),
                generation_required=source_rule.generation_required,
                language_policy=source_rule.language_policy,
                length_unit=source_rule.length_unit,
                generation_min=source_rule.generation_min,
                generation_max=source_rule.generation_max,
                generation_validator_keys=deepcopy(
                    source_rule.generation_validator_keys,
                ),
                generation_context_selectors=deepcopy(
                    source_rule.generation_context_selectors,
                ),
            )

    target_rules = list(
        Rule.objects.filter(profile_version_id=target.id)
        .select_related("field_definition")
        .order_by("position", "id")
    )
    encoded = _canonical_bytes(_profile_contract(target, target_rules))
    target_digest = hashlib.sha256(encoded).hexdigest()
    if not target.contract_sha256:
        target.contract_size_bytes = len(encoded)
        target.contract_sha256 = target_digest
        target.save(update_fields=(
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))
    if (
        target.name != TARGET_PROFILE_NAME
        or target.source_projection_key != "declarative_v1"
        or target.source_projection_config != target_config
        or target.contract_schema_version != 6
        or target.contract_size_bytes != len(encoded)
        or target.contract_sha256 != target_digest
        or [rule.field_definition.key for rule in target_rules] != EXPECTED_FIELDS
        or [rule.projection_primitive for rule in target_rules]
        != [PRIMITIVES[key] for key in EXPECTED_FIELDS]
        or any(rule.source_selector != "projection.output" for rule in target_rules)
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v19: target contract drifted",
        )

    target_binding = Binding.objects.select_for_update().filter(
        key=TARGET_BINDING_KEY,
    ).first()
    if target_binding is None:
        target_binding = Binding.objects.create(
            key=TARGET_BINDING_KEY,
            name=TARGET_BINDING_NAME,
            operation=source_binding.operation,
            model_family=source_binding.model_family,
            mode=source_binding.mode,
            model_profile_version_id=source_binding.model_profile_version_id,
            prompt_profile_version_id=target.id,
            priority=source_binding.priority,
            is_active=False,
        )
    if (
        target_binding.prompt_profile_version_id != target.id
        or target_binding.operation != source_binding.operation
        or target_binding.model_family != source_binding.model_family
        or target_binding.mode != source_binding.mode
        or target_binding.model_profile_version_id
        != source_binding.model_profile_version_id
        or target_binding.priority != source_binding.priority
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v19: target binding drifted",
        )
    competitor = Binding.objects.filter(
        operation=source_binding.operation,
        model_family=source_binding.model_family,
        mode=source_binding.mode,
        model_profile_version_id=source_binding.model_profile_version_id,
        priority=source_binding.priority,
        is_active=True,
    ).exclude(pk__in=(source_binding.pk, target_binding.pk))
    if competitor.exists():
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v19: exact route has another active binding",
        )

    if (
        source.status == "retired"
        and not source_binding.is_active
        and target.status == "published"
        and target_binding.is_active
    ):
        return
    if (
        source.status != "published"
        or not source_binding.is_active
        or target_binding.is_active
    ):
        raise RuntimeError(
            "Cannot activate H3 PromptProfile v19: route is partially migrated",
        )

    source_before = _profile_summary(source)
    source.status = "retired"
    source.save(update_fields=("status", "updated_at"))
    _audit(
        AuditEvent,
        action="retire",
        object_type="prompt_profile_version",
        object_id=source.id,
        before=source_before,
        after=_profile_summary(source),
    )
    _audit(
        AuditEvent,
        action="publish",
        object_type="prompt_profile_version",
        object_id=target.id,
        before={},
        after=_profile_summary(target),
    )
    source_binding_before = _binding_summary(source_binding)
    source_binding.is_active = False
    source_binding.save(update_fields=("is_active", "updated_at"))
    _audit(
        AuditEvent,
        action="deactivate",
        object_type="prompt_profile_binding",
        object_id=source_binding.id,
        before=source_binding_before,
        after=_binding_summary(source_binding),
    )
    target_binding_before = _binding_summary(target_binding)
    target_binding.is_active = True
    target_binding.save(update_fields=("is_active", "updated_at"))
    _audit(
        AuditEvent,
        action="activate",
        object_type="prompt_profile_binding",
        object_id=target_binding.id,
        before=target_binding_before,
        after=_binding_summary(target_binding),
    )


def restore_v18_route(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    AuditEvent = apps.get_model("studio", "PlatformLifecycleAuditEvent")
    source = Profile.objects.select_for_update().filter(
        profile_key=PROFILE_KEY,
        version=SOURCE_VERSION,
    ).first()
    target = Profile.objects.select_for_update().filter(
        profile_key=PROFILE_KEY,
        version=TARGET_VERSION,
    ).first()
    source_binding = Binding.objects.select_for_update().filter(
        key=SOURCE_BINDING_KEY,
    ).first()
    target_binding = Binding.objects.select_for_update().filter(
        key=TARGET_BINDING_KEY,
    ).first()
    if not any((source, target, source_binding, target_binding)):
        # Forward migration does nothing on installations without this route.
        # Its inverse must likewise leave an empty installation unchanged.
        return
    if not all((source, target, source_binding, target_binding)):
        raise RuntimeError("Cannot restore H3 v18 route: migrated facts are missing")
    if (
        source_binding.prompt_profile_version_id != source.id
        or target_binding.prompt_profile_version_id != target.id
    ):
        raise RuntimeError("Cannot restore H3 v18 route: binding target drifted")
    if (
        source.status == "published"
        and source_binding.is_active
        and target.status == "retired"
        and not target_binding.is_active
    ):
        return
    if (
        source.status != "retired"
        or source_binding.is_active
        or target.status != "published"
        or not target_binding.is_active
    ):
        raise RuntimeError("Cannot restore H3 v18 route: route is partially changed")

    target_before = _profile_summary(target)
    target.status = "retired"
    target.save(update_fields=("status", "updated_at"))
    _audit(
        AuditEvent,
        action="retire",
        object_type="prompt_profile_version",
        object_id=target.id,
        before=target_before,
        after=_profile_summary(target),
    )
    source_before = _profile_summary(source)
    source.status = "published"
    source.save(update_fields=("status", "updated_at"))
    _audit(
        AuditEvent,
        action="publish",
        object_type="prompt_profile_version",
        object_id=source.id,
        before=source_before,
        after=_profile_summary(source),
    )
    target_binding_before = _binding_summary(target_binding)
    target_binding.is_active = False
    target_binding.save(update_fields=("is_active", "updated_at"))
    _audit(
        AuditEvent,
        action="deactivate",
        object_type="prompt_profile_binding",
        object_id=target_binding.id,
        before=target_binding_before,
        after=_binding_summary(target_binding),
    )
    source_binding_before = _binding_summary(source_binding)
    source_binding.is_active = True
    source_binding.save(update_fields=("is_active", "updated_at"))
    _audit(
        AuditEvent,
        action="activate",
        object_type="prompt_profile_binding",
        object_id=source_binding.id,
        before=source_binding_before,
        after=_binding_summary(source_binding),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0131_promptprofilefieldrule_projection_primitive_and_more"),
    ]

    operations = [
        migrations.RunPython(
            publish_declarative_successor,
            restore_v18_route,
        ),
    ]
