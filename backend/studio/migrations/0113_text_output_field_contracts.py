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
SOURCE_VERSION = 18
TARGET_VERSION = 19
FULL_CAPABILITIES = (
    "script.generate",
    "prompt.normalize",
    "script.structure",
    "visual_rules.generate",
)

SERIES_CONCEPT_FIELD_CONTRACT = {
    "schema_version": 1,
    "idea": {"max_chars": 6_000, "required": True, "single_line": False},
    "style_hint": {"max_chars": 2_000, "required": False, "single_line": False},
    "title": {"max_chars": 60, "required": True, "single_line": True},
    "logline": {"max_chars": 500, "required": True, "single_line": True},
    "bible.entity.name": {"max_chars": 160, "required": True, "single_line": True},
    "bible.entity.description": {"max_chars": 2_000, "required": False, "single_line": True},
    "bible.entity.visual_identity_en": {"max_chars": 3_000, "required": False, "single_line": True},
    "episode.title": {"max_chars": 80, "required": True, "single_line": True},
    "shot.setting": {"max_chars": 200, "required": True, "single_line": True},
    "shot.action": {"max_chars": 500, "required": True, "single_line": True},
    "shot.dialogue.speaker": {"max_chars": 80, "required": False, "single_line": True},
    "shot.dialogue.delivery": {"max_chars": 120, "required": False, "single_line": True},
    "shot.dialogue.text": {"max_chars": 240, "required": False, "single_line": True},
}

SCRIPT_STRUCTURE_FIELD_CONTRACT = {
    "schema_version": 1,
    "title": {"max_chars": 200, "required": True, "single_line": True},
    "logline": {"max_chars": 2_000, "required": True, "single_line": True},
    "bible.entity.name": {"max_chars": 160, "required": True, "single_line": True},
    "bible.entity.description": {"max_chars": 2_000, "required": False, "single_line": True},
    "bible.entity.visual_identity_en": {"max_chars": 3_000, "required": False, "single_line": True},
    "episode.title": {"max_chars": 200, "required": True, "single_line": True},
    "shot.setting": {"max_chars": 500, "required": True, "single_line": True},
    "shot.action": {"max_chars": 4_000, "required": True, "single_line": True},
    "shot.dialogue.speaker": {"max_chars": 160, "required": False, "single_line": True},
    "shot.dialogue.delivery": {"max_chars": 240, "required": False, "single_line": True},
    "shot.dialogue.text": {"max_chars": 2_000, "required": False, "single_line": True},
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


def _verify_integrity(profile, *, version, status):
    size, sha256 = _canonical_contract(profile.specification)
    if (
        profile.version != version
        or profile.status != status
        or profile.specification_size_bytes != size
        or profile.specification_sha256 != sha256
        or tuple(profile.specification.get("capabilities") or ()) != FULL_CAPABILITIES
    ):
        raise RuntimeError(f"Cannot use ModelProfile v{version}: contract drifted")


def _target_specification(source_specification):
    specification = deepcopy(source_specification)
    policies = {
        "series_concept": SERIES_CONCEPT_FIELD_CONTRACT,
        "script_structure": SCRIPT_STRUCTURE_FIELD_CONTRACT,
    }
    for policy_key, field_contract in policies.items():
        policy = specification.get(policy_key)
        if not isinstance(policy, dict) or "field_contract" in policy:
            raise RuntimeError(
                f"Cannot publish ModelProfile v19: {policy_key} source policy drifted",
            )
        policy["field_contract"] = deepcopy(field_contract)
    return specification


def publish_text_output_field_contract_successor(apps, schema_editor):
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
            "Cannot publish ModelProfile v19: stable Deployment closure drifted",
        )

    current = deployments[0].profile_version
    if current.version == TARGET_VERSION:
        _verify_integrity(current, version=TARGET_VERSION, status="published")
        source = Profile.objects.filter(
            profile_key=current.profile_key,
            version=SOURCE_VERSION,
        ).first()
        if source is None:
            raise RuntimeError("Cannot reuse ModelProfile v19: v18 source is missing")
        _verify_integrity(source, version=SOURCE_VERSION, status="retired")
        if current.specification != _target_specification(source.specification):
            raise RuntimeError("Cannot reuse ModelProfile v19: successor drifted")
        expected_keys = {
            _binding_key(current.profile_key, operation, "")
            for operation in FULL_CAPABILITIES
        }
        bindings = list(Binding.objects.filter(model_profile_version_id=current.id))
        if {row.key for row in bindings} != expected_keys or any(not row.is_active for row in bindings):
            raise RuntimeError("Cannot reuse ModelProfile v19: binding closure drifted")
        return

    source = current
    _verify_integrity(source, version=SOURCE_VERSION, status="published")
    target_specification = _target_specification(source.specification)
    deployment_ids = [row.pk for row in deployments]
    if Deployment.objects.filter(
        profile_version_id=source.id,
        status="active",
    ).exclude(pk__in=deployment_ids).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v18: another active Deployment references it",
        )
    if Workflow.objects.filter(
        model_profile_version_id=source.id,
        status="published",
    ).exists():
        raise RuntimeError(
            "Cannot retire ModelProfile v18: a published Workflow references it",
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
            "Cannot publish ModelProfile v19: source binding closure drifted",
        )

    target_size, target_sha256 = _canonical_contract(target_specification)
    target = Profile.objects.filter(
        profile_key=source.profile_key,
        version=TARGET_VERSION,
    ).first()
    target_values = {
        "provider_id": source.provider_id,
        "model_identifier": source.model_identifier,
        "modality": source.modality,
        "specification": target_specification,
        "specification_schema_version": source.specification_schema_version,
        "specification_size_bytes": target_size,
        "specification_sha256": target_sha256,
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
        raise RuntimeError("Cannot publish ModelProfile v19: successor drifted")
    if Deployment.objects.filter(profile_version_id=target.id).exists():
        raise RuntimeError(
            "Cannot publish ModelProfile v19: successor Deployment closure drifted",
        )

    target_binding_keys = set()
    source_contracts = {_binding_contract(row) for row in source_bindings}
    for source_binding in source_bindings:
        key = _binding_key(source.profile_key, source_binding.operation, source_binding.mode)
        target_binding_keys.add(key)
        expected = {
            "name": (
                f"{model_family} {source_binding.operation} / ModelProfile v19"
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
            Binding.objects.create(key=key, **expected)
        elif any(getattr(row, field) != value for field, value in expected.items()):
            raise RuntimeError(f"Cannot publish ModelProfile v19: binding {key} drifted")

    target_bindings = list(Binding.objects.filter(model_profile_version_id=target.id))
    if (
        {row.key for row in target_bindings} != target_binding_keys
        or any(not row.is_active for row in target_bindings)
        or {_binding_contract(row) for row in target_bindings} != source_contracts
    ):
        raise RuntimeError("Cannot publish ModelProfile v19: target binding closure drifted")

    Deployment.objects.filter(pk__in=deployment_ids).update(profile_version_id=target.id)
    Binding.objects.filter(model_profile_version_id=source.id, is_active=True).update(is_active=False)
    Profile.objects.filter(pk=source.id, status="published").update(status="retired")


def restore_v18_and_remove_v19(apps, schema_editor):
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
        raise RuntimeError("Cannot reverse ModelProfile v19: stable Deployment closure drifted")
    target = deployments[0].profile_version
    if target.version == SOURCE_VERSION:
        _verify_integrity(target, version=SOURCE_VERSION, status="published")
        if Profile.objects.filter(profile_key=target.profile_key, version=TARGET_VERSION).exists():
            raise RuntimeError("Cannot reverse ModelProfile v19: orphan successor still exists")
        return

    _verify_integrity(target, version=TARGET_VERSION, status="published")
    source = Profile.objects.filter(
        profile_key=target.profile_key,
        version=SOURCE_VERSION,
    ).first()
    if source is None:
        raise RuntimeError("Cannot reverse ModelProfile v19: v18 source is missing")
    _verify_integrity(source, version=SOURCE_VERSION, status="retired")
    if target.specification != _target_specification(source.specification):
        raise RuntimeError("Cannot reverse ModelProfile v19: successor drifted")
    deployment_ids = [row.pk for row in deployments]
    if Deployment.objects.filter(profile_version_id=target.id).exclude(pk__in=deployment_ids).exists():
        raise RuntimeError("Cannot reverse ModelProfile v19: another Deployment references it")
    if (
        Artifact.objects.filter(profile_version_id=target.id).exists()
        or Workflow.objects.filter(model_profile_version_id=target.id).exists()
    ):
        raise RuntimeError("Cannot reverse ModelProfile v19: successor acquired artifact/workflow references")

    source_bindings = list(Binding.objects.filter(model_profile_version_id=source.id))
    target_bindings = list(Binding.objects.filter(model_profile_version_id=target.id))
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
        raise RuntimeError("Cannot reverse ModelProfile v19: binding closure drifted")

    Deployment.objects.filter(pk__in=deployment_ids).update(profile_version_id=source.id)
    Binding.objects.filter(pk__in=[row.pk for row in source_bindings]).update(is_active=True)
    Binding.objects.filter(pk__in=[row.pk for row in target_bindings]).delete()
    Profile.objects.filter(pk=source.id, status="retired").update(status="published")
    target.delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0112_runtime_contract_security_boundaries")]

    operations = [
        migrations.RunPython(
            publish_text_output_field_contract_successor,
            restore_v18_and_remove_v19,
        ),
    ]
