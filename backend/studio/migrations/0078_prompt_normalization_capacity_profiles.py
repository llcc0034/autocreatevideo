import hashlib
import json
import re

from django.db import migrations
from django.utils import timezone


PROMPT_NORMALIZATION_POLICY = {
    "max_input_bytes": 29_000,
    "max_output_tokens": 8_192,
    "max_items_per_batch": 4,
    "timeout_seconds": 180.0,
}

# These deployment keys are the stable recipe routes in the reviewed assisted
# v4 and unattended v6 catalogs.  Keeping the deployment IDs/keys stable lets
# published recipes resolve the successor profile without rewriting history.
SUCCESSORS = (
    (
        "deepseek-text-v5-prod",
        5,
        8,
        "DeepSeek 剧本、结构化与提示词（受管）",
    ),
    (
        "deepseek-visual-rules-v7-prod",
        7,
        9,
        "DeepSeek 全剧影视规则（受管）",
    ),
)


def _canonical_contract(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _binding_key(profile_key, version, operation, mode):
    stem = re.sub(r"[^a-z0-9.-]+", "-", str(profile_key).casefold()).strip("-.")
    operation_stem = re.sub(
        r"[^a-z0-9.-]+",
        "-",
        str(operation).casefold(),
    ).strip("-.")
    mode_stem = re.sub(r"[^a-z0-9.-]+", "-", str(mode).casefold()).strip("-.")
    suffix = f"-{mode_stem}" if mode_stem else ""
    key = f"{stem}-model-v{version}-{operation_stem}{suffix}"
    if len(key) <= 120:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{key[:107]}-{digest}"


def _capability_operations(specification):
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list):
        raise RuntimeError("Cannot publish prompt-normalization successor: capabilities drifted")
    supported = {
        "prompt.normalize",
        "script.generate",
        "script.structure",
        "visual_rules.generate",
    }
    operations = []
    for value in capabilities:
        if value in supported and value not in operations:
            operations.append(value)
    if "prompt.normalize" not in operations:
        raise RuntimeError(
            "Cannot publish prompt-normalization successor: source capability is missing",
        )
    return operations


def _source_bindings(Binding, source, *, operation, model_family):
    exact = list(
        Binding.objects.filter(
            is_active=True,
            model_profile_version_id=source.id,
            operation=operation,
            model_family=model_family,
        ).order_by("-priority", "key")
    )
    if exact:
        return exact
    fallback = (
        Binding.objects.filter(
            is_active=True,
            operation=operation,
            model_family=model_family,
            model_profile_version__profile_key=source.profile_key,
            model_profile_version__provider_id=source.provider_id,
            model_profile_version__model_identifier=source.model_identifier,
        )
        .order_by("-priority", "-model_profile_version__version", "key")
        .first()
    )
    return [fallback] if fallback is not None else []


def publish_capacity_successors(apps, schema_editor):
    Profile = apps.get_model("studio", "ModelProfileVersion")
    Deployment = apps.get_model("studio", "ModelDeployment")
    Binding = apps.get_model("studio", "PromptProfileBinding")

    resolved = []
    for deployment_key, source_version, target_version, deployment_name in SUCCESSORS:
        deployment = (
            Deployment.objects.select_related("profile_version")
            .filter(key=deployment_key, status="active")
            .first()
        )
        if deployment is None:
            continue
        source = deployment.profile_version
        if source.version != source_version or source.status not in ("published", "retired"):
            raise RuntimeError(
                f"Cannot repoint {deployment_key}: source profile version drifted",
            )
        specification = dict(source.specification)
        if specification.get("prompt_normalization") not in (
            None,
            PROMPT_NORMALIZATION_POLICY,
        ):
            raise RuntimeError(
                f"Cannot repoint {deployment_key}: prompt normalization policy drifted",
            )
        specification["prompt_normalization"] = PROMPT_NORMALIZATION_POLICY
        size_bytes, digest = _canonical_contract(specification)
        target = Profile.objects.filter(
            profile_key=source.profile_key,
            version=target_version,
        ).first()
        if target is None:
            target = Profile.objects.create(
                profile_key=source.profile_key,
                version=target_version,
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
            raise RuntimeError(
                f"Cannot repoint {deployment_key}: successor profile v{target_version} drifted",
            )

        model_family = str(specification.get("model_family") or "").strip()
        if not model_family:
            raise RuntimeError(
                f"Cannot repoint {deployment_key}: model_family is missing",
            )
        operations = _capability_operations(specification)
        created_binding_ids = []
        for operation in operations:
            sources = _source_bindings(
                Binding,
                source,
                operation=operation,
                model_family=model_family,
            )
            if not sources:
                raise RuntimeError(
                    f"Cannot repoint {deployment_key}: {operation} binding is missing",
                )
            for source_binding in sources:
                key = _binding_key(
                    source.profile_key,
                    target_version,
                    operation,
                    source_binding.mode,
                )
                target_binding = Binding.objects.filter(key=key).first()
                expected = {
                    "name": (
                        f"{source_binding.name} / ModelProfile v{target_version}"
                    )[:160],
                    "operation": source_binding.operation,
                    "model_family": source_binding.model_family,
                    "mode": source_binding.mode,
                    "model_profile_version_id": target.id,
                    "prompt_profile_version_id": source_binding.prompt_profile_version_id,
                    "priority": source_binding.priority,
                    "is_active": True,
                }
                if target_binding is None:
                    target_binding = Binding.objects.create(key=key, **expected)
                elif any(
                    getattr(target_binding, field) != value
                    for field, value in expected.items()
                ):
                    raise RuntimeError(
                        f"Cannot repoint {deployment_key}: successor binding {key} drifted",
                    )
                created_binding_ids.append(target_binding.id)
        Deployment.objects.filter(pk=deployment.id).update(
            profile_version_id=target.id,
            name=deployment_name,
        )
        resolved.append((source.id, target.id, tuple(created_binding_ids)))

    for source_id, _target_id, _binding_ids in resolved:
        Binding.objects.filter(
            model_profile_version_id=source_id,
            is_active=True,
        ).update(is_active=False)
        if not Deployment.objects.filter(
            profile_version_id=source_id,
            status="active",
        ).exists():
            Profile.objects.filter(pk=source_id, status="published").update(status="retired")


class Migration(migrations.Migration):
    dependencies = [("studio", "0077_prompt_profile_static_reference_policy_v4")]

    operations = [
        migrations.RunPython(
            publish_capacity_successors,
            migrations.RunPython.noop,
        ),
    ]
