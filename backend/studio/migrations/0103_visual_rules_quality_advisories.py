from copy import deepcopy
import hashlib
import json
import re

from django.db import migrations
from django.utils import timezone


STABLE_ROUTES = (
    "deepseek-text-v5-prod",
    "deepseek-visual-rules-v7-prod",
)
SOURCE_VERSION = 16
TARGET_VERSION = 17
FULL_CAPABILITIES = (
    "script.generate",
    "prompt.normalize",
    "script.structure",
    "visual_rules.generate",
)
SOURCE_POLICY = {
    "max_input_bytes": 65_536,
    "input_byte_warning_threshold": 16_000,
    "max_output_tokens": 131_072,
    "output_token_warning_threshold": 32_768,
    "timeout_seconds": 600.0,
    "chat_completion_options": {
        "thinking": {"type": "disabled"},
    },
}
QUALITY_ADVISORIES = {
    "schema_version": 1,
    "base_colors_min_items": 1,
    "base_colors_max_items": 3,
    "negative_constraints_min_items": 0,
    "negative_constraints_max_items": 32,
    "negative_constraint_max_chars": 120,
    "recommended_negative_clauses": [
        "identity drift",
        "face drift",
        "age drift",
        "hairstyle change",
        "wardrobe change",
        "prop mutation",
        "inconsistent architecture",
        "duplicate person",
        "extra limbs",
        "extra fingers",
        "deformed hands",
        "readable text",
        "subtitles",
        "watermark",
        "logo",
        "flicker",
        "jitter",
        "camera teleport",
        "low detail",
    ],
    "continuity_summary_max_chars": 3_000,
    "negative_prompt_max_chars": 8_000,
    "color_script_max_chars": 2_000,
}
TARGET_POLICY = {
    **SOURCE_POLICY,
    "quality_advisories": QUALITY_ADVISORIES,
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


def _binding_key(profile_key, operation, mode):
    stem = re.sub(r"[^a-z0-9.-]+", "-", str(profile_key).casefold()).strip("-.")
    operation_stem = re.sub(
        r"[^a-z0-9.-]+", "-", str(operation).casefold(),
    ).strip("-.")
    mode_stem = re.sub(
        r"[^a-z0-9.-]+", "-", str(mode).casefold(),
    ).strip("-.")
    suffix = f"-{mode_stem}" if mode_stem else ""
    key = f"{stem}-model-v{TARGET_VERSION}-{operation_stem}{suffix}"
    if len(key) <= 120:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{key[:107]}-{digest}"


def _binding_contract(row):
    return (
        row.operation,
        row.mode,
        row.model_family,
        row.prompt_profile_version_id,
        row.priority,
    )


def _verify_profile_contract(profile, *, version, policy, status):
    size, sha256 = _canonical_contract(profile.specification)
    if (
        profile.version != version
        or profile.status != status
        or profile.specification_size_bytes != size
        or profile.specification_sha256 != sha256
        or profile.specification.get("visual_rules") != policy
        or tuple(profile.specification.get("capabilities") or ()) != FULL_CAPABILITIES
    ):
        raise RuntimeError(
            f"Cannot use ModelProfile v{version}: contract drifted",
        )


def publish_visual_rules_quality_advisory_successor(apps, schema_editor):
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
    if (
        len(deployments) != len(STABLE_ROUTES)
        or any(row.status != "active" for row in deployments)
        or len({row.profile_version_id for row in deployments}) != 1
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v17: stable Deployment closure drifted",
        )
    source = deployments[0].profile_version
    if source.version == TARGET_VERSION:
        _verify_profile_contract(
            source,
            version=TARGET_VERSION,
            policy=TARGET_POLICY,
            status="published",
        )
        expected_keys = {
            _binding_key(source.profile_key, operation, "")
            for operation in FULL_CAPABILITIES
        }
        bindings = list(Binding.objects.filter(model_profile_version_id=source.id))
        if (
            {row.key for row in bindings} != expected_keys
            or any(not row.is_active for row in bindings)
        ):
            raise RuntimeError(
                "Cannot reuse ModelProfile v17: binding closure drifted",
            )
        return
    _verify_profile_contract(
        source,
        version=SOURCE_VERSION,
        policy=SOURCE_POLICY,
        status="published",
    )
    deployment_ids = [row.pk for row in deployments]
    if Deployment.objects.filter(
        profile_version_id=source.id,
        status="active",
    ).exclude(pk__in=deployment_ids).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v16: another active Deployment references it",
        )
    if Workflow.objects.filter(
        model_profile_version_id=source.id,
        status="published",
    ).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v16: a published Workflow references it",
        )

    model_family = str(source.specification.get("model_family") or "").strip()
    source_bindings = list(Binding.objects.filter(
        model_profile_version_id=source.id,
        is_active=True,
    ).order_by("operation", "mode", "key"))
    identities = {(row.operation, row.mode) for row in source_bindings}
    if (
        not model_family
        or len(source_bindings) != len(FULL_CAPABILITIES)
        or identities != {(operation, "") for operation in FULL_CAPABILITIES}
        or any(row.model_family != model_family for row in source_bindings)
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v17: source binding closure drifted",
        )

    specification = deepcopy(source.specification)
    specification["visual_rules"] = deepcopy(TARGET_POLICY)
    target_size, target_sha = _canonical_contract(specification)
    target = Profile.objects.filter(
        profile_key=source.profile_key,
        version=TARGET_VERSION,
    ).first()
    target_values = {
        "provider_id": source.provider_id,
        "model_identifier": source.model_identifier,
        "modality": source.modality,
        "specification": specification,
        "specification_schema_version": source.specification_schema_version,
        "specification_size_bytes": target_size,
        "specification_sha256": target_sha,
        "status": "published",
    }
    if target is None:
        target = Profile.objects.create(
            profile_key=source.profile_key,
            version=TARGET_VERSION,
            published_at=timezone.now(),
            published_by_id=source.published_by_id,
            **target_values,
        )
    elif any(getattr(target, field) != value for field, value in target_values.items()):
        raise RuntimeError("Cannot publish ModelProfile v17: successor drifted")
    if Deployment.objects.filter(profile_version_id=target.id).exists():
        raise RuntimeError(
            "Cannot publish ModelProfile v17: successor Deployment closure drifted",
        )

    target_binding_keys = set()
    source_contracts = {_binding_contract(row) for row in source_bindings}
    for source_binding in source_bindings:
        key = _binding_key(
            source.profile_key,
            source_binding.operation,
            source_binding.mode,
        )
        target_binding_keys.add(key)
        expected = {
            "name": (
                f"{model_family} {source_binding.operation} / ModelProfile v17"
            )[:160],
            "operation": source_binding.operation,
            "model_family": source_binding.model_family,
            "mode": source_binding.mode,
            "model_profile_version_id": target.id,
            "prompt_profile_version_id": source_binding.prompt_profile_version_id,
            "priority": source_binding.priority,
            "is_active": True,
        }
        row = Binding.objects.filter(key=key).first()
        if row is None:
            row = Binding.objects.create(key=key, **expected)
        elif any(getattr(row, field) != value for field, value in expected.items()):
            raise RuntimeError(f"Cannot publish ModelProfile v17: binding {key} drifted")

    target_bindings = list(Binding.objects.filter(model_profile_version_id=target.id))
    if (
        {row.key for row in target_bindings} != target_binding_keys
        or any(not row.is_active for row in target_bindings)
        or {_binding_contract(row) for row in target_bindings} != source_contracts
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v17: target binding closure drifted",
        )

    Deployment.objects.filter(pk__in=deployment_ids).update(
        profile_version_id=target.id,
    )
    Binding.objects.filter(
        model_profile_version_id=source.id,
        is_active=True,
    ).update(is_active=False)
    Profile.objects.filter(pk=source.id, status="published").update(status="retired")


def restore_v16_and_remove_v17(apps, schema_editor):
    Profile = apps.get_model("studio", "ModelProfileVersion")
    Deployment = apps.get_model("studio", "ModelDeployment")
    Binding = apps.get_model("studio", "PromptProfileBinding")
    Artifact = apps.get_model("studio", "ModelArtifact")
    Workflow = apps.get_model("studio", "WorkflowTemplateVersion")

    deployments = list(
        Deployment.objects.select_related("profile_version")
        .filter(key__in=STABLE_ROUTES)
        .order_by("key")
    )
    if not deployments:
        return
    if (
        len(deployments) != len(STABLE_ROUTES)
        or any(row.status != "active" for row in deployments)
        or len({row.profile_version_id for row in deployments}) != 1
    ):
        raise RuntimeError(
            "Cannot reverse ModelProfile v17: stable Deployment closure drifted",
        )
    target = deployments[0].profile_version
    if target.version == SOURCE_VERSION:
        _verify_profile_contract(
            target,
            version=SOURCE_VERSION,
            policy=SOURCE_POLICY,
            status="published",
        )
        if Profile.objects.filter(
            profile_key=target.profile_key,
            version=TARGET_VERSION,
        ).exists():
            raise RuntimeError(
                "Cannot reverse ModelProfile v17: orphan successor still exists",
            )
        return
    _verify_profile_contract(
        target,
        version=TARGET_VERSION,
        policy=TARGET_POLICY,
        status="published",
    )
    source = Profile.objects.filter(
        profile_key=target.profile_key,
        version=SOURCE_VERSION,
    ).first()
    if source is None:
        raise RuntimeError("Cannot reverse ModelProfile v17: v16 source is missing")
    _verify_profile_contract(
        source,
        version=SOURCE_VERSION,
        policy=SOURCE_POLICY,
        status="retired",
    )
    deployment_ids = [row.pk for row in deployments]
    if Deployment.objects.filter(profile_version_id=target.id).exclude(
        pk__in=deployment_ids,
    ).exists():
        raise RuntimeError(
            "Cannot reverse ModelProfile v17: another Deployment references it",
        )
    if (
        Artifact.objects.filter(profile_version_id=target.id).exists()
        or Workflow.objects.filter(model_profile_version_id=target.id).exists()
    ):
        raise RuntimeError(
            "Cannot reverse ModelProfile v17: successor acquired artifact/workflow references",
        )

    source_bindings = list(Binding.objects.filter(
        model_profile_version_id=source.id,
    ).order_by("operation", "mode", "key"))
    target_bindings = list(Binding.objects.filter(
        model_profile_version_id=target.id,
    ).order_by("operation", "mode", "key"))
    expected_target_keys = {
        _binding_key(target.profile_key, operation, "")
        for operation in FULL_CAPABILITIES
    }
    if (
        len(source_bindings) != len(FULL_CAPABILITIES)
        or any(row.is_active for row in source_bindings)
        or {row.operation for row in source_bindings} != set(FULL_CAPABILITIES)
        or {row.key for row in target_bindings} != expected_target_keys
        or any(not row.is_active for row in target_bindings)
        or {_binding_contract(row) for row in source_bindings}
        != {_binding_contract(row) for row in target_bindings}
    ):
        raise RuntimeError(
            "Cannot reverse ModelProfile v17: binding closure drifted",
        )

    Deployment.objects.filter(pk__in=deployment_ids).update(
        profile_version_id=source.id,
    )
    Binding.objects.filter(pk__in=[row.pk for row in source_bindings]).update(
        is_active=True,
    )
    Binding.objects.filter(pk__in=[row.pk for row in target_bindings]).delete()
    Profile.objects.filter(pk=source.id, status="retired").update(status="published")
    target.delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0102_h3_quality_advisories")]

    operations = [
        migrations.RunPython(
            publish_visual_rules_quality_advisory_successor,
            restore_v16_and_remove_v17,
        ),
    ]
