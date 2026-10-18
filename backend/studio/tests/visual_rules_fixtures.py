from studio.tests.field_definition_fixtures import create_field_definition
"""Explicit configurable input fixture; no provider or production data writes."""
from uuid import uuid4

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import ModelDeployment, ModelProfileVersion, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile


def configure_visual_instance(case, *, project_sources=False, reviewed_sources=False, reviewed_fields=None, quality=None, allowed_values=None, field_sources=None, response_keys=None, max_input_bytes=16000, field_max_chars=2000):
    key = "visual-input-" + str(uuid4())
    model = ModelProfileVersion(profile_key=key, version=1, model_identifier="visual-protocol",
        provider=case.deployment.provider, modality="text", status="published")
    assign_json_contract(model, "specification", {"schema_version": 1, "capabilities": ["visual_rules.generate"],
        "model_family": "visual-fixture", "visual_rules": {"max_input_bytes": max_input_bytes, "timeout_seconds": 30, "chat_request_options": {}}})
    model.save()
    deployment = ModelDeployment(key=key, name="Configured visual", profile_version=model, credential=case.deployment.credential)
    assign_json_contract(deployment, "runtime_config", {})
    deployment.save()
    profile = PromptProfileVersion.objects.create(profile_key=key, version=1, name="Style and logline",
        model_family="visual-fixture", operation="visual_rules.generate", mode="", source_projection_key="direct_v1",
        review_state="ready", contract_schema_version=8)
    fields = (("theme", "request.theme", "一句话梗概"), ("style", "request.style", "视觉风格"))
    if reviewed_sources:
        from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS
        from studio.services.visual_rules_output_contract import VISUAL_RULE_REVIEW_FIELDS
        fields = tuple((field, key, label) for key, (field, label) in VISUAL_RULE_SOURCE_FIELDS.items()
            if field in (reviewed_fields if reviewed_fields is not None else VISUAL_RULE_REVIEW_FIELDS))
    for position, (name, selector, label) in enumerate(fields):
        if project_sources:
            selector = {"theme": "project.logline", "style": "style.visual_prompt"}[name]
        field = create_field_definition(key=key + "-" + name, label=label, value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=selector,
            generation_source_selector=selector, generation_mode="passthrough", channel="positive_prompt",
            position=position, required=True, language_policy="any", max_chars=field_max_chars)
    refresh_prompt_profile_contract(profile)
    profile = publish_prompt_profile(profile, actor=case.actor)
    instance = create_execution_instance(key=key, name="Visual", description="", actor=case.actor)
    from studio.services.business_outputs import write_output
    from studio.tests.output_structure_fixtures import response_structure
    output = write_output(actor=case.actor, data={"name": "Visual test output", "parser_key": "visual_rules.v2", "quality_advisories": quality or {},
        "data_structure_version": response_structure(field_sources=field_sources, response_keys=response_keys).pk,
        **({"allowed_values": allowed_values} if allowed_values is not None else {}),
        **({"response_keys": response_keys} if response_keys is not None else {}),
        **({"field_sources": field_sources} if field_sources is not None else {})})
    output = write_output(actor=case.actor, output_id=output.pk, expected_revision=output.revision, action="publish")
    version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=case.actor,
        selection={"deployment": deployment, "operation": "visual_rules.generate", "mode": "", "prompt_mode": "profile",
            "prompt_profile_version": profile, "workflow_version": None, "output_version": output})
    case.definition = build_execution_definition(version.pk)
    case.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
        expected_definition_sha256=case.definition.sha256, actor=case.actor)
    case.fixture["inputs"] = {"request.theme": {"kind": "literal", "value": "A traveler follows a mysterious letter."},
        "request.style": {"kind": "literal", "value": "Restrained cool realism."}}
    case.fixture["manual_channels"] = {}
