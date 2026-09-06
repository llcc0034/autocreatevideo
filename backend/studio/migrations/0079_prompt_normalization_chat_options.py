import hashlib
import json
import re

from django.db import migrations
from django.utils import timezone


SOURCE_PROMPT_NORMALIZATION_POLICY = {
    "max_input_bytes": 29_000,
    "max_output_tokens": 8_192,
    "max_items_per_batch": 4,
    "timeout_seconds": 180.0,
}
TARGET_PROMPT_NORMALIZATION_POLICY = {
    **SOURCE_PROMPT_NORMALIZATION_POLICY,
    "chat_completion_options": {
        "thinking": {"type": "disabled"},
    },
}
STABLE_ROUTES = (
    "deepseek-text-v5-prod",
    "deepseek-visual-rules-v7-prod",
)
SOURCE_VERSIONS = (8, 9)
TARGET_VERSION = 10
TEXT_CAPABILITIES = (
    "script.generate",
    "prompt.normalize",
    "script.structure",
)
FULL_CAPABILITIES = (*TEXT_CAPABILITIES, "visual_rules.generate")


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
            f"Cannot publish ModelProfile v{TARGET_VERSION}: source checksum drifted",
        )


def _binding_key(profile_key, operation, mode):
    stem = re.sub(r"[^a-z0-9.-]+", "-", str(profile_key).casefold()).strip("-.")
    operation_stem = re.sub(
        r"[^a-z0-9.-]+",
        "-",
        str(operation).casefold(),
    ).strip("-.")
    mode_stem = re.sub(r"[^a-z0-9.-]+", "-", str(mode).casefold()).strip("-.")
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


def _validate_runtime_host_merge(text_deployment, full_deployment):
    runtime_host_id = text_deployment.runtime_host_id
    if (
        runtime_host_id is not None
        and runtime_host_id == full_deployment.runtime_host_id
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v10: active Deployment runtime_host conflict",
        )


def publish_chat_options_successor(apps, schema_editor):
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
            "Cannot publish ModelProfile v10: stable Deployment set is incomplete",
        )
    deployments_by_key = {deployment.key: deployment for deployment in deployments}
    text_deployment = deployments_by_key["deepseek-text-v5-prod"]
    full_deployment = deployments_by_key["deepseek-visual-rules-v7-prod"]
    if any(deployment.status != "active" for deployment in deployments):
        raise RuntimeError(
            "Cannot publish ModelProfile v10: stable Deployment is inactive",
        )
    _validate_runtime_host_merge(text_deployment, full_deployment)
    source_v8 = text_deployment.profile_version
    source_v9 = full_deployment.profile_version
    if (
        source_v8.version != SOURCE_VERSIONS[0]
        or source_v9.version != SOURCE_VERSIONS[1]
        or source_v8.status != "published"
        or source_v9.status != "published"
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v10: source versions drifted",
        )
    for source in (source_v8, source_v9):
        _verify_contract(source)
        if source.specification.get("prompt_normalization") != (
            SOURCE_PROMPT_NORMALIZATION_POLICY
        ):
            raise RuntimeError(
                "Cannot publish ModelProfile v10: source policy drifted",
            )
    if any(
        getattr(source_v8, field) != getattr(source_v9, field)
        for field in (
            "profile_key",
            "provider_id",
            "model_identifier",
            "modality",
            "specification_schema_version",
        )
    ):
        raise RuntimeError(
            "Cannot publish ModelProfile v10: source model identity drifted",
        )
    if tuple(source_v8.specification.get("capabilities") or ()) != TEXT_CAPABILITIES:
        raise RuntimeError(
            "Cannot publish ModelProfile v10: v8 capability closure drifted",
        )
    if tuple(source_v9.specification.get("capabilities") or ()) != FULL_CAPABILITIES:
        raise RuntimeError(
            "Cannot publish ModelProfile v10: v9 capability closure drifted",
        )
    shared_v8 = dict(source_v8.specification)
    shared_v9 = dict(source_v9.specification)
    shared_v8.pop("capabilities", None)
    shared_v9.pop("capabilities", None)
    visual_rules = shared_v9.pop("visual_rules", None)
    if shared_v8 != shared_v9 or not isinstance(visual_rules, dict):
        raise RuntimeError(
            "Cannot publish ModelProfile v10: shared source contracts drifted",
        )

    source_ids = (source_v8.id, source_v9.id)
    if Deployment.objects.filter(
        profile_version_id__in=source_ids,
        status="active",
    ).exclude(pk__in=[deployment.pk for deployment in deployments]).exists():
        raise RuntimeError(
            "Cannot retire source profiles: another active Deployment still references them",
        )
    if Workflow.objects.filter(
        model_profile_version_id__in=source_ids,
        status="published",
    ).exists():
        raise RuntimeError(
            "Cannot retire source profiles: a published Workflow still references them",
        )

    model_family = str(source_v9.specification.get("model_family") or "").strip()
    if not model_family:
        raise RuntimeError("Cannot publish ModelProfile v10: model_family is missing")
    bindings_by_version = {}
    for source, expected_operations in (
        (source_v8, TEXT_CAPABILITIES),
        (source_v9, FULL_CAPABILITIES),
    ):
        rows = list(Binding.objects.filter(
            model_profile_version_id=source.id,
            is_active=True,
            model_family=model_family,
        ).order_by("operation", "mode", "key"))
        keys = [(row.operation, row.mode) for row in rows]
        if (
            len(rows) != len(expected_operations)
            or len(keys) != len(set(keys))
            or {row.operation for row in rows} != set(expected_operations)
        ):
            raise RuntimeError(
                f"Cannot publish ModelProfile v10: v{source.version} exact bindings drifted",
            )
        bindings_by_version[source.version] = {
            (row.operation, row.mode): row for row in rows
        }
    for identity, v8_binding in bindings_by_version[8].items():
        v9_binding = bindings_by_version[9].get(identity)
        if v9_binding is None or _binding_contract(v8_binding) != _binding_contract(
            v9_binding,
        ):
            raise RuntimeError(
                "Cannot publish ModelProfile v10: overlapping bindings disagree",
            )

    specification = dict(source_v9.specification)
    specification["prompt_normalization"] = TARGET_PROMPT_NORMALIZATION_POLICY
    size_bytes, digest = _canonical_contract(specification)
    target = Profile.objects.filter(
        profile_key=source_v9.profile_key,
        version=TARGET_VERSION,
    ).first()
    if target is None:
        target = Profile.objects.create(
            profile_key=source_v9.profile_key,
            version=TARGET_VERSION,
            provider_id=source_v9.provider_id,
            model_identifier=source_v9.model_identifier,
            modality=source_v9.modality,
            specification=specification,
            specification_schema_version=source_v9.specification_schema_version,
            specification_size_bytes=size_bytes,
            specification_sha256=digest,
            status="published",
            published_at=timezone.now(),
            published_by_id=source_v9.published_by_id,
        )
    elif (
        target.provider_id != source_v9.provider_id
        or target.model_identifier != source_v9.model_identifier
        or target.modality != source_v9.modality
        or target.specification != specification
        or target.specification_schema_version != source_v9.specification_schema_version
        or target.specification_size_bytes != size_bytes
        or target.specification_sha256 != digest
        or target.status != "published"
    ):
        raise RuntimeError("Cannot publish ModelProfile v10: successor drifted")

    target_binding_ids = []
    for source_binding in bindings_by_version[9].values():
        key = _binding_key(
            source_v9.profile_key,
            source_binding.operation,
            source_binding.mode,
        )
        expected = {
            "name": (
                f"{model_family} {source_binding.operation} / ModelProfile v10"
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
            target_binding = Binding.objects.create(key=key, **expected)
        elif any(
            getattr(target_binding, field) != value
            for field, value in expected.items()
        ):
            raise RuntimeError(f"Cannot publish ModelProfile v10: binding {key} drifted")
        target_binding_ids.append(target_binding.id)
    if len(target_binding_ids) != len(FULL_CAPABILITIES):
        raise RuntimeError("Cannot publish ModelProfile v10: binding closure is incomplete")

    Deployment.objects.filter(pk__in=[row.pk for row in deployments]).update(
        profile_version_id=target.id,
    )
    Binding.objects.filter(
        model_profile_version_id__in=source_ids,
        is_active=True,
    ).update(is_active=False)
    Profile.objects.filter(
        pk__in=source_ids,
        status="published",
    ).update(status="retired")


class Migration(migrations.Migration):
    dependencies = [("studio", "0078_prompt_normalization_capacity_profiles")]

    operations = [
        migrations.RunPython(
            publish_chat_options_successor,
            migrations.RunPython.noop,
        ),
    ]
