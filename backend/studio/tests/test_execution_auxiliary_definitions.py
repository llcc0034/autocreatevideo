from studio.tests.field_definition_fixtures import create_field_definition
"""Complete composite publication uses verified leaves, not published labels."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import ExecutionAuxiliaryBinding, ExecutionVersionContract, ModelDeployment, ModelProfileVersion, ProviderProfile, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.execution_definitions import build_execution_definition, read_execution_input_contract, verify_current_execution_definition, execution_authoring_options
from studio.services.execution_instances import create_execution_draft, create_execution_instance, publish_execution_version, update_execution_draft
from studio.services.execution_profile_inputs import profile_input_targets, project_execution_profile_sources
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests import test_execution_definitions as definition_fixtures
from studio.tests.test_execution_text_contracts import text_specification


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAuxiliaryDefinitionTests(TestCase):
    write_contract = staticmethod(definition_fixtures.ExecutionDefinitionTests.write_contract)
    setUpTestData = classmethod(definition_fixtures.ExecutionDefinitionTests.setUpTestData.__func__)

    def setUp(self):
        definition_fixtures.ExecutionDefinitionTests.setUp(self)
        self.leaf_provider = ProviderProfile(key="field-provider", name="Field", driver_type="openai_compatible", modalities="text")
        self.write_contract(self.leaf_provider, "driver_config", {"base_url": "https://api.example.com", "chat_path": "/v1/chat/completions", "timeout_seconds": 40, "capability_ttl_seconds": 3600})
        model = ModelProfileVersion(profile_key="field-model", version=1, provider=self.leaf_provider, model_identifier="declared-model", modality="text", status="published")
        self.write_contract(model, "specification", text_specification())
        deployment = ModelDeployment(key="field-target", name="Field", profile_version=model)
        self.write_contract(deployment, "runtime_config", {})
        instance = create_execution_instance(key="field-instance", name="Field", description="", actor=self.actor)
        leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None, "prompt_profile_version": None, "prompt_mode": "none"})
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor, changes={"inputs": [
            self.input("field_request", "field_request", kind="json", target_kind="operation_input"),
        ]})
        self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision, expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        field = create_field_definition(key="composite_direction", label="Direction", value_type="long_text")
        profile = PromptProfileVersion.objects.create(profile_key="composite-profile", name="Composite", version=1, model_family="async_cloud", operation="video.generate", mode="T2VA", review_state="ready", contract_schema_version=8)
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="request.text", generation_source_selector="request.action", generation_context_selectors=["project.style"],
            generation_mode="model_generated", generation_output_semantics="final", generation_instruction="Use the declared source and style only.", generation_required=True, language_policy="en", channel="positive_prompt", position=1, required=True, max_chars=1000)
        profile.channel_limits = getattr(self, "channel_limits", {})
        profile.save(update_fields=("channel_limits",))
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        self.inputs = [self.input("request.action", "request.action"), self.input("style", "context.project.style", kind="json", position=1)]
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"prompt_mode": "profile", "prompt_profile_version": self.profile, "inputs": self.inputs})

    @staticmethod
    def input(key, target, *, kind="text", position=0, target_kind="profile_input"):
        return {"key": key, "position": position, "kind": kind, "required": True, "min_items": 1, "max_items": 1,
            "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
            "targets": [{"target_kind": target_kind, "target_key": target, "is_primary": target_kind != "profile_input"}]}

    def bind(self):
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(self.leaf.pk)}]})

    def test_generated_sources_are_distinct_from_final_destinations_and_contexts_are_explicit(self):
        targets = profile_input_targets(self.profile)
        self.assertEqual(set(targets), {"request.action", "context.project.style"})
        self.assertEqual(targets["request.action"]["fields"][0]["usage"], "generation_source")
        self.assertEqual(targets["context.project.style"]["fields"][0]["selector"], "project.style")
        self.assertEqual(targets["context.project.style"]["kind"], "json")
        self.assertNotIn("request.text", targets)

    def test_missing_leaf_is_not_a_publishable_combination(self):
        options = execution_authoring_options(self.version.pk)
        self.assertEqual(options["auxiliary_purposes"], [{"purpose": "generate_fields", "operation": "prompt.fields.generate"}])
        self.assertIn("context.project.style", options["profile_inputs"])
        with self.assertRaisesMessage(ValidationError, "尚未配置策略所需"):
            build_execution_definition(self.version.pk)
        self.assertFalse(ExecutionVersionContract.objects.filter(version=self.version).exists())

    def test_real_published_leaf_identity_is_frozen_with_parent_without_smoke(self):
        self.bind()
        definition = build_execution_definition(self.version.pk)
        self.assertEqual(definition.value["interface"]["auxiliaries"]["generate_fields"], {
            "version_id": str(self.leaf.pk), "definition_sha256": self.leaf.contract.sha256,
            "target_fingerprint": self.leaf.contract.target_fingerprint, "operation": "prompt.fields.generate",
        })
        published = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        self.assertEqual(verify_current_execution_definition(published.contract).sha256, definition.sha256)
        self.assertNotEqual(definition.value["configuration"]["operation"], self.profile.operation)

    def test_published_flag_without_leaf_contract_is_rejected(self):
        self.bind()
        self.leaf.contract.delete()
        with self.assertRaisesMessage(ValidationError, "缺少冻结合同"):
            build_execution_definition(self.version.pk)

    def test_resolved_sources_and_context_use_the_complete_auxiliary_definition(self):
        self.bind()
        definition = build_execution_definition(self.version.pk)
        projection = project_execution_profile_sources(definition, {
            "request.action": "A courier opens a blue parcel.", "style": {"lighting": "soft daylight"},
        })
        self.assertEqual(projection["assembly_sources"], {})
        self.assertEqual(projection["generation_sources"], {"composite_direction": "A courier opens a blue parcel."})
        self.assertEqual(projection["generation_contexts"], {"composite_direction": {"project.style": {"lighting": "soft daylight"}}})
        self.assertEqual([row["input_key"] for row in projection["provenance"]], ["style", "request.action"])
        self.assertNotIn("approved", projection)
        with self.assertRaises(ValidationError):
            project_execution_profile_sources(definition, {"request.action": "Explicit source without required style"})

    def test_leaf_execution_drift_blocks_current_parent_but_does_not_rewrite_frozen_detail(self):
        self.bind()
        definition = build_execution_definition(self.version.pk)
        published = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        config = deepcopy(self.leaf_provider.driver_config)
        config["timeout_seconds"] += 1
        self.write_contract(self.leaf_provider, "driver_config", config)
        self.assertEqual(read_execution_input_contract(published.pk)["sha256"], definition.sha256)
        with self.assertRaisesMessage(ValidationError, "目标已变化"):
            verify_current_execution_definition(published.contract)

    def test_recursive_leaf_shape_is_rejected_before_recursing_into_contracts(self):
        self.bind()
        ExecutionAuxiliaryBinding.objects.create(version=self.leaf, purpose="generate_fields", target_version=self.version)
        with self.assertRaisesMessage(ValidationError, "叶子"):
            build_execution_definition(self.version.pk)

    def test_missing_context_or_wrong_type_cannot_publish(self):
        self.bind()
        for inputs in ([self.inputs[0]], [self.inputs[0], {**self.inputs[1], "kind": "text"}]):
            self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor, changes={"inputs": inputs})
            with self.assertRaises(ValidationError):
                build_execution_definition(self.version.pk)
