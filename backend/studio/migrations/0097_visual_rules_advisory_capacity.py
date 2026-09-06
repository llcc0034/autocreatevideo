import hashlib
import json
import re

from django.db import migrations
from django.utils import timezone


STABLE_ROUTES = (
    "deepseek-text-v5-prod",
    "deepseek-visual-rules-v7-prod",
)
SOURCE_VERSION = 15
TARGET_VERSION = 16
FULL_CAPABILITIES = (
    "script.generate",
    "prompt.normalize",
    "script.structure",
    "visual_rules.generate",
)
SOURCE_POLICY = {
    "max_input_bytes": 64_000,
    "max_output_tokens": 8_192,
    "timeout_seconds": 180,
}
TARGET_POLICY = {
    "max_input_bytes": 65_536,
    "input_byte_warning_threshold": 16_000,
    "max_output_tokens": 131_072,
    "output_token_warning_threshold": 32_768,
    "timeout_seconds": 600.0,
    "chat_completion_options": {
        "thinking": {"type": "disabled"},
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


def publish_visual_rules_capacity_successor(apps, schema_editor):
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
            "Cannot publish ModelProfile v16: stable Deployment closure drifted",
        )
    source = deployments[0].profile_version
    source_size, source_sha = _canonical_contract(source.specification)
    if (
        source.profile_key != "deepseek-text"
        or source.version != SOURCE_VERSION
        or source.status != "published"
        or source.specification_size_bytes != source_size
        or source.specification_sha256 != source_sha
        or source.specification.get("visual_rules") != SOURCE_POLICY
        or tuple(source.specification.get("capabilities") or ())
        != FULL_CAPABILITIES
    ):
        raise RuntimeError("Cannot publish ModelProfile v16: source contract drifted")
    deployment_ids = [row.pk for row in deployments]
    if Deployment.objects.filter(
        profile_version_id=source.id,
        status="active",
    ).exclude(pk__in=deployment_ids).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v15: another active Deployment references it",
        )
    if Workflow.objects.filter(
        model_profile_version_id=source.id,
        status="published",
    ).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v15: a published Workflow references it",
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
            "Cannot publish ModelProfile v16: source binding closure drifted",
        )

    specification = dict(source.specification)
    specification["visual_rules"] = TARGET_POLICY
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
        raise RuntimeError("Cannot publish ModelProfile v16: successor drifted")

    target_binding_keys = set()
    source_contracts = set()
    for source_binding in source_bindings:
        key = _binding_key(
            source.profile_key,
            source_binding.operation,
            source_binding.mode,
        )
        target_binding_keys.add(key)
        expected = {
            "name": (
                f"{model_family} {source_binding.operation} / ModelProfile v16"
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
            raise RuntimeError(f"Cannot publish ModelProfile v16: binding {key} drifted")
        source_contracts.add((
            source_binding.operation,
            source_binding.mode,
            source_binding.model_family,
            source_binding.prompt_profile_version_id,
            source_binding.priority,
        ))

    target_bindings = list(Binding.objects.filter(model_profile_version_id=target.id))
    if (
        {row.key for row in target_bindings} != target_binding_keys
        or any(not row.is_active for row in target_bindings)
        or {
            (
                row.operation, row.mode, row.model_family,
                row.prompt_profile_version_id, row.priority,
            )
            for row in target_bindings
        } != source_contracts
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v16: target binding closure drifted",
        )

    Deployment.objects.filter(pk__in=deployment_ids).update(
        profile_version_id=target.id,
    )
    Binding.objects.filter(
        model_profile_version_id=source.id,
        is_active=True,
    ).update(is_active=False)
    Profile.objects.filter(pk=source.id, status="published").update(status="retired")


class Migration(migrations.Migration):
    dependencies = [("studio", "0096_text_contract_retry_policy")]

    operations = [
        migrations.RunPython(
            publish_visual_rules_capacity_successor,
            migrations.RunPython.noop,
        ),
    ]
