import hashlib
import json

from django.db import migrations
from django.utils import timezone


CAPABILITY_BOUND_PROMPT_OPERATIONS = (
    "prompt.normalize",
    "script.generate",
    "script.structure",
)


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _summary(binding, *, is_active):
    return {
        "key": binding.key,
        "operation": binding.operation,
        "model_family": binding.model_family,
        "mode": binding.mode,
        "model_profile_version_id": str(binding.model_profile_version_id),
        "prompt_profile_version_id": str(binding.prompt_profile_version_id),
        "priority": binding.priority,
        "is_active": is_active,
    }


def _declares_exact_capability(model_profile, operation):
    specification = model_profile.specification
    if not isinstance(specification, dict):
        return False
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list):
        return False
    if (
        any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            for item in capabilities
        )
        or len(capabilities) != len(set(capabilities))
    ):
        return False
    return operation in capabilities


def deactivate_invalid_exact_bindings(apps, schema_editor):
    Binding = apps.get_model("studio", "PromptProfileBinding")
    AuditEvent = apps.get_model("studio", "PlatformLifecycleAuditEvent")
    rows = Binding.objects.select_related("model_profile_version").filter(
        is_active=True,
        model_profile_version__isnull=False,
        operation__in=CAPABILITY_BOUND_PROMPT_OPERATIONS,
    )
    for binding in rows.iterator():
        if _declares_exact_capability(
            binding.model_profile_version,
            binding.operation,
        ):
            continue
        before = _summary(binding, is_active=True)
        after = _summary(binding, is_active=False)
        binding.is_active = False
        binding.updated_at = timezone.now()
        binding.save(update_fields=("is_active", "updated_at"))
        AuditEvent.objects.create(
            actor_id=None,
            action="deactivate",
            object_type="prompt_profile_binding",
            object_id=binding.id,
            reason_code="exact_model_capability_missing",
            before_sha256=hashlib.sha256(_canonical_bytes(before)).hexdigest(),
            after_sha256=hashlib.sha256(_canonical_bytes(after)).hexdigest(),
            before_summary=before,
            after_summary=after,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0060_prompt_normalization_resource_reference"),
    ]

    operations = [
        migrations.RunPython(
            deactivate_invalid_exact_bindings,
            migrations.RunPython.noop,
        ),
    ]
