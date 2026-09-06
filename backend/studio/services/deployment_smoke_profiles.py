"""One explicit, frozen PromptProfile selection for each text Smoke."""
from types import SimpleNamespace

from django.core.exceptions import ValidationError

from studio.services.prompt_profiles import (
    assemble_prompt_profile, compatible_prompt_profile_bindings,
    frozen_profile_reference, load_frozen_prompt_profile_selection,
    model_family_for_profile, require_explicit_prompt_profile_binding,
    text_profile_sources,
)


def smoke_profile_choices(deployment, operation):
    return compatible_prompt_profile_bindings(
        operation=operation,
        model_family=model_family_for_profile(deployment.profile_version),
        model_profile_version=deployment.profile_version,
    )


def select_smoke_profile(deployment, operation, binding_id, *, lock=False):
    return require_explicit_prompt_profile_binding(
        binding_id, operation=operation,
        model_family=model_family_for_profile(deployment.profile_version),
        model_profile_version=deployment.profile_version, lock=lock,
    )


def smoke_profile_reference(deployment, operation, resolved):
    return frozen_profile_reference(
        resolved, model_profile_version=deployment.profile_version,
        operation=operation,
    )


def assemble_text_smoke_profile(resolved, operation, inputs):
    # Synthetic facts make a repeatable fixture, not a second business strategy.
    project = SimpleNamespace(
        title="灯塔来信", logline="旅人在风雨中的灯塔发现一封旧信。",
        master_script="旅人走进灯塔，发现一封旧信。", language="zh-CN",
        aspect_ratio="9:16",
    )
    return assemble_prompt_profile(
        resolved.profile_version,
        sources=text_profile_sources(project=project, inputs=inputs, operation=operation),
    )


def verify_smoke_profile(deployment, operation, reference):
    return load_frozen_prompt_profile_selection(
        reference, model_profile_version=deployment.profile_version, operation=operation,
    )


def profile_columns(reference):
    if not reference:
        return {}
    return {
        "prompt_profile_binding_id": reference["prompt_profile_binding_id"],
        "prompt_profile_version_id": reference["prompt_profile_version_id"],
        "prompt_profile_contract_sha256": reference["prompt_profile_contract_sha256"],
    }


def snapshot_profile_matches(snapshot, binding_id=None):
    """Old provider-only probes never certify a configured prompt strategy."""
    reference = (snapshot.evidence or {}).get("prompt_profile")
    if not isinstance(reference, dict) or not snapshot.prompt_profile_binding_id:
        return False
    if binding_id and str(snapshot.prompt_profile_binding_id) != str(binding_id):
        return False
    try:
        if any(str(getattr(snapshot, key)) != str(value) for key, value in profile_columns(reference).items()):
            return False
        verify_smoke_profile(snapshot.deployment, reference.get("operation"), reference)
        current = select_smoke_profile(
            snapshot.deployment, reference.get("operation"), snapshot.prompt_profile_binding_id,
        )
        if (
            current.profile_version.id != snapshot.prompt_profile_version_id
            or current.profile_version.contract_sha256 != snapshot.prompt_profile_contract_sha256
        ):
            return False
    except (ValidationError, KeyError, TypeError, ValueError):
        return False
    return True
