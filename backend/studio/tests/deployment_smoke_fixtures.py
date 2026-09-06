"""Drive the public durable Smoke lifecycle in tests (provider calls stay mocked)."""
from studio.services.deployment_smoke_queue import (
    enqueue_deployment_smoke, claim_next_deployment_smoke, process_deployment_smoke,
)


def create_test_capacity_profile(actor):
    """Explicit minimal media-field contract for routing tests, not production defaults.

    Full multi-field and near-capacity coverage lives in test_deployment_text_smoke_queue.
    """
    from studio.models import PromptFieldDefinition, PromptProfileVersion, PromptProfileFieldRule, PromptProfileBinding
    from studio.services.prompt_profiles import publish_prompt_profile
    if PromptProfileBinding.objects.filter(key="test-capacity-binding").exists():
        return
    profile = PromptProfileVersion.objects.create(profile_key="test.capacity", version=1,
        name="Test translated media field", model_family="test_capacity", operation="image.generate",
        source_projection_key="direct_v1", contract_schema_version=6, review_state="ready")
    field = PromptFieldDefinition.objects.create(key="test_capacity_detail", label="Detail", category="test", value_type="long_text")
    PromptProfileFieldRule.objects.create(profile_version=profile, field_definition=field,
        source_selector="shot.detailed_description", channel="positive_prompt", position=1,
        generation_mode="translate_only", generation_source_selector="shot.detailed_description",
        generation_instruction="Translate the supplied facts to English without changing protected labels.",
        language_policy="en", length_unit="none", generation_min=None, generation_max=None)
    profile = publish_prompt_profile(profile, actor=actor)
    PromptProfileBinding.objects.create(key="test-capacity-binding", name="Test capacity binding",
        operation=profile.operation, model_family=profile.model_family, prompt_profile_version=profile, is_active=True)


def test_normalization_response(payload, text="A restrained slow camera push-in."):
    from studio.services.prompt_normalization_contract import _protected_token_sentinel
    return {"schema_version": 1, "items": [
        {"request_id": item["request_id"], "translation": text + " " + " ".join(
            _protected_token_sentinel(token)
            for token in item.get("protected_tokens", [])
            for _ in range(item["text"].count(token))
        )} for item in payload["items"]], "metadata": {}}


def run_test_deployment_smoke(deployment, *, operation):
    from studio.models import PromptProfileBinding
    # Test setup creates exactly one explicit reviewed binding per model/operation.
    binding = PromptProfileBinding.objects.get(model_profile_version=deployment.profile_version,
        operation=operation, is_active=True)
    queued, _ = enqueue_deployment_smoke(deployment, operation=operation, prompt_profile_binding_id=binding.id)
    claimed = claim_next_deployment_smoke("fixture-smoke-worker")
    if claimed is None or claimed.pk != queued.pk:
        raise AssertionError("The fixture did not claim its own queued Smoke attempt")
    finished = process_deployment_smoke(claimed)
    if finished.final_snapshot_id is None:
        raise AssertionError(f"Smoke has no terminal evidence: {finished.status}")
    return finished.final_snapshot
