import hashlib
import json
import re

from django.db import migrations
from django.utils import timezone


STABLE_ROUTES = (
    "deepseek-text-v5-prod",
    "deepseek-visual-rules-v7-prod",
)
SOURCE_VERSION = 12
TARGET_VERSION = 13
FULL_CAPABILITIES = (
    "script.generate",
    "prompt.normalize",
    "script.structure",
    "visual_rules.generate",
)
SOURCE_PROMPT_NORMALIZATION_POLICY = {
    "max_input_bytes": 16_000,
    "max_output_tokens": 8_192,
    "max_items_per_batch": 1,
    "timeout_seconds": 180.0,
    "chat_completion_options": {
        "thinking": {"type": "disabled"},
    },
}
TARGET_PROMPT_NORMALIZATION_POLICY = {
    **SOURCE_PROMPT_NORMALIZATION_POLICY,
    "chat_completion_options": {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    },
}


def _canonical_contract(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _verify_contract(profile):
    size_bytes, digest = _canonical_contract(profile.specification)
    if (
        profile.specification_size_bytes != size_bytes
        or profile.specification_sha256 != digest
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: source checksum drifted",
        )


def _binding_key(profile_key, operation, mode):
    stem = re.sub(r"[^a-z0-9.-]+", "-", str(profile_key).casefold()).strip("-.")
    operation_stem = re.sub(
        r"[^a-z0-9.-]+",
        "-",
        str(operation).casefold(),
    ).strip("-.")
    mode_stem = re.sub(
        r"[^a-z0-9.-]+",
        "-",
        str(mode).casefold(),
    ).strip("-.")
    suffix = f"-{mode_stem}" if mode_stem else ""
    key = f"{stem}-model-v{TARGET_VERSION}-{operation_stem}{suffix}"
    if len(key) <= 120:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{key[:107]}-{digest}"


def _binding_contract(binding):
    return (
        binding.operation,
        binding.mode,
        binding.model_family,
        binding.prompt_profile_version_id,
        binding.priority,
    )


def publish_thinking_mode_successor(apps, schema_editor):
    Profile = apps.get_model("studio", "ModelProfileVersion")
    Deployment = apps.get_model("studio", "ModelDeployment")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    Workflow = apps.get_model("studio", "WorkflowTemplateVersion")

    deployments = list(
        Deployment.objects.select_related("profile_version")
        .filter(key__in=STABLE_ROUTES)
        .order_by("key")
    )
    if not deployments:
        return
    if len(deployments) != len(STABLE_ROUTES):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: stable Deployment set is incomplete",
        )
    if any(deployment.status != "active" for deployment in deployments):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: stable Deployment is inactive",
        )
    source_ids = {deployment.profile_version_id for deployment in deployments}
    if len(source_ids) != 1:
        raise RuntimeError(
            "Cannot publish ModelProfile v13: stable Deployments do not share v12",
        )
    source = deployments[0].profile_version
    if (
        source.profile_key != "deepseek-text"
        or source.version != SOURCE_VERSION
        or source.status != "published"
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: source version drifted",
        )
    _verify_contract(source)
    if source.specification.get("prompt_normalization") != (
        SOURCE_PROMPT_NORMALIZATION_POLICY
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: source policy drifted",
        )
    if tuple(source.specification.get("capabilities") or ()) != FULL_CAPABILITIES:
        raise RuntimeError(
            "Cannot publish ModelProfile v13: source capability closure drifted",
        )
    if Deployment.objects.filter(
        profile_version_id=source.id,
        status="active",
    ).exclude(pk__in=[deployment.pk for deployment in deployments]).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v12: another active Deployment references it",
        )
    if Workflow.objects.filter(
        model_profile_version_id=source.id,
        status="published",
    ).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v12: a published Workflow references it",
        )

    model_family = str(source.specification.get("model_family") or "").strip()
    if not model_family:
        raise RuntimeError("Cannot publish ModelProfile v13: model_family is missing")
    source_bindings = list(
        Binding.objects.filter(
            model_profile_version_id=source.id,
            is_active=True,
        ).order_by("operation", "mode", "key")
    )
    identities = [(row.operation, row.mode) for row in source_bindings]
    if (
        len(source_bindings) != len(FULL_CAPABILITIES)
        or len(identities) != len(set(identities))
        or set(identities) != {(operation, "") for operation in FULL_CAPABILITIES}
        or any(row.model_family != model_family for row in source_bindings)
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: source exact bindings drifted",
        )

    specification = dict(source.specification)
    specification["prompt_normalization"] = TARGET_PROMPT_NORMALIZATION_POLICY
    size_bytes, digest = _canonical_contract(specification)
    target = Profile.objects.filter(
        profile_key=source.profile_key,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=source.profile_key,
            version=TARGET_VERSION,
            provider_id=source.provider_id,
            model_identifier=source.model_identifier,
            modality=source.modality,
            specification=specification,
            specification_schema_version=source.specification_schema_version,
            specification_size_bytes=size_bytes,
            specification_sha256=digest,
            status="published",
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
        )
    elif (
        target.provider_id != source.provider_id
        or target.model_identifier != source.model_identifier
        or target.modality != source.modality
        or target.specification != specification
        or target.specification_schema_version != source.specification_schema_version
        or target.specification_size_bytes != size_bytes
        or target.specification_sha256 != digest
        or target.status != "published"
    ):
        raise RuntimeError("Cannot publish ModelProfile v13: successor drifted")
    if Deployment.objects.filter(
        profile_version_id=target.id,
        status="active",
    ).exclude(pk__in=[deployment.pk for deployment in deployments]).exists():
        raise RuntimeError(
            "Cannot publish ModelProfile v13: successor already has another active Deployment",
        )

    expected_target_keys = set()
    for source_binding in source_bindings:
        key = _binding_key(
            source.profile_key,
            source_binding.operation,
            source_binding.mode,
        )
        expected_target_keys.add(key)
        expected = {
            "name": (
                f"{model_family} {source_binding.operation} / ModelProfile v13"
            )[:160],
            "operation": source_binding.operation,
            "model_family": source_binding.model_family,
            "mode": source_binding.mode,
            "model_profile_version_id": target.id,
            "prompt_profile_version_id": source_binding.prompt_profile_version_id,
            "priority": source_binding.priority,
            "is_active": True,
        }
        target_binding = Binding.objects.filter(key=key).first()
        if target_binding is None:
            Binding.objects.create(key=key, **expected)
        elif any(
            getattr(target_binding, field) != value
            for field, value in expected.items()
        ):
            raise RuntimeError(
                f"Cannot publish ModelProfile v13: binding {key} drifted",
            )
    all_target_bindings = list(
        Binding.objects.filter(model_profile_version_id=target.id).order_by("key")
    )
    active_target_bindings = [row for row in all_target_bindings if row.is_active]
    if (
        {row.key for row in all_target_bindings} != expected_target_keys
        or {row.key for row in active_target_bindings} != expected_target_keys
        or {_binding_contract(row) for row in active_target_bindings}
        != {_binding_contract(row) for row in source_bindings}
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v13: target exact bindings drifted",
        )

    Deployment.objects.filter(pk__in=[row.pk for row in deployments]).update(
        profile_version_id=target.id,
    )
    Binding.objects.filter(
        model_profile_version_id=source.id,
        is_active=True,
    ).update(is_active=False)
    Profile.objects.filter(pk=source.id, status="published").update(status="retired")


class Migration(migrations.Migration):
    dependencies = [("studio", "0087_prompt_profile_twelve_sentence_scaffold")]

    operations = [
        migrations.RunPython(
            publish_thinking_mode_successor,
            migrations.RunPython.noop,
        ),
    ]
