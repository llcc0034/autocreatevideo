"""Generic final results: independent of operation, renderer destination and key.

These are isolated author/prepare/adopt/compile tests, not real model Smoke.
"""
import json
from unittest.mock import patch

from studio.services.execution_field_contracts import expand_field_request

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile, clone_prompt_profile_successor
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests import test_execution_reference_preparation as reference
from studio.tests import test_profile_structured_generation as direct
from studio.tests import test_execution_native_text as native
from studio.tests import test_media_appearance_profile_images as image_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class GenericReferenceResultTests(TestCase):
    input = staticmethod(reference.ExecutionReferencePreparationTests.input)
    write = reference.ExecutionReferencePreparationTests.write
    resolve = reference.ExecutionReferencePreparationTests.resolve
    prepare = reference.ExecutionReferencePreparationTests.prepare
    preview = reference.ExecutionReferencePreparationTests.preview
    select = reference.ExecutionReferencePreparationTests.select

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential",
                       "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No network in generic field tests")))
        reference.ExecutionReferencePreparationTests.setUp(self, configure_profile=self.configure_profile)

    def configure_profile(self, profile):
        from studio.tests.reference_rendering_fixtures import generation_structure_fixture
        generation_structure_fixture(profile)
        # Existing per-unit direction stays deterministic, while arbitrary
        # complete fields can be generated once for this request.
        for position, primitive in enumerate(("subject_definitions", "summary", "retention_analysis"), start=2):
            field = create_field_definition(key=f"custom_{primitive}", label=primitive, value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="projection.output",
                projection_primitive=f"reference_context.{primitive}", generation_mode="model_generated",
                generation_output_semantics="final", generation_source_selector="projection.output",
                generation_instruction="Use the exact supplied facts. Preserve supplied reference tokens.",
                generation_required=True, language_policy="en", channel="positive_prompt", position=position,
                required=True, max_chars=7000, generation_validator_keys=["protected_tokens_at_least_once"])

    def adopt(self, key, text, unit="composition"):
        self.prep.refresh_from_db()
        row = create_manual_execution_field(preparation_id=self.prep.pk, unit_key=unit, field_key=key, text=text,
            revision=1, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=row.pk, expected_text_sha256=row.text_sha256,
            expected_source_sha256=row.source_sha256, expected_preparation_revision=self.prep.revision,
            select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.user)

    def test_final_fields_are_once_per_request_and_not_per_unit(self):
        self.prepare()
        requests = self.prep.contract.resolved_inputs["field_requests"]
        self.assertEqual(len(requests), 1)
        items = expand_field_request(requests[0]["value"])["items"]
        self.assertEqual(len(items), 5)
        final = [row for row in items if row["field_contract"]["generation_output_semantics"] == "final"]
        self.assertEqual({row["unit_key"] for row in final}, {"composition"})
        self.assertEqual({row["field_contract"]["field_key"] for row in final},
            {"custom_subject_definitions", "custom_summary", "custom_retention_analysis"})
        self.assertTrue(all(isinstance(row["source_text"], str) for row in final))
        self.assertNotIn("projection.output", self.definition.value["interface"]["profile_inputs"])
        self.assertFalse(self.prep.field_values.exists())

    def test_final_and_contribution_results_share_selection_compiler_and_history(self):
        self.prepare()
        self.select("first", "A courier waits beside a doorway.")
        self.select("second", "A parcel opens on the table.")
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        for key, text in (("subject_definitions", "The supplied courier and doorway references."),
                          ("summary", "A courier arrives and the parcel opens."),
                          ("retention_analysis", "Preserve the supplied references and declared positions.")):
            self.adopt(f"custom_{key}", text)
        result = self.preview()
        body = result.resolved_channels["values"]["positive_prompt"]
        self.assertEqual(body.count("A courier arrives and the parcel opens."), 1)
        self.assertIn("A courier waits beside a doorway.", body)
        self.assertIn("A parcel opens on the table.", body)
        self.assertEqual(len(result.compilation.reviewed_field_evidence["field_selections"]), 5)
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"]["direction"] = "Changed later"
        self.assertEqual(self.preview().resolved_channels, result.resolved_channels)
        self.assertIsNone(self.prep.request_frozen_at)

    def test_final_result_cannot_be_selected_into_a_different_unit(self):
        self.prepare()
        with self.assertRaises(ValidationError):
            self.adopt("custom_summary", "No hidden merge of unrelated unit text.", unit="first")
        self.assertFalse(self.prep.field_values.exists())

    def test_projected_source_cannot_be_injected_as_a_request_value(self):
        self.sources["projection.output"] = {"kind": "literal", "value": "Untrusted fabricated bindings"}
        with self.assertRaises(ValidationError):
            self.prepare()
        self.assertFalse(self.job.execution_preparations.exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class GenericDirectResultTests(direct.StructuredGenerationTests):
    # Inherited tests verify the same existing root, selected paths and adopted
    # value behavior. Additional author cases are intentionally not H3-specific.
    def test_text_operations_accept_the_same_generated_business_field(self):
        self.configure()
        for operation in ("script.generate", "script.structure", "visual_rules.generate"):
            with self.subTest(operation=operation):
                draft = clone_prompt_profile_successor(self.profile,
                    expected_source_contract_sha256=self.profile.contract_sha256, name=operation)
                draft.operation, draft.mode, draft.review_state = operation, "", "ready"
                draft.save(update_fields=("operation", "mode", "review_state"))
                refresh_prompt_profile_contract(draft)
                published = publish_prompt_profile(draft, actor=self.actor)
                self.assertEqual(published.status, "published")
                self.profile = published

    def test_auxiliary_leaf_cannot_generate_recursively(self):
        self.configure()
        draft = clone_prompt_profile_successor(self.profile,
            expected_source_contract_sha256=self.profile.contract_sha256, name="Invalid leaf")
        draft.operation, draft.review_state = "prompt.fields.generate", "ready"
        draft.save(update_fields=("operation", "review_state"))
        refresh_prompt_profile_contract(draft)
        with self.assertRaisesMessage(ValidationError, "叶子"):
            publish_prompt_profile(draft, actor=self.actor)

    def test_system_instruction_remains_authored_not_generated(self):
        self.configure()
        draft = clone_prompt_profile_successor(self.profile,
            expected_source_contract_sha256=self.profile.contract_sha256, name="Invalid system generation")
        rule = draft.field_rules.get(field_definition=self.profile_field)
        rule.channel = "system_instruction"
        rule.save()
        draft.review_state = "ready"
        draft.save(update_fields=("review_state",))
        with self.assertRaises(ValidationError):
            refresh_prompt_profile_contract(draft)
            publish_prompt_profile(draft, actor=self.actor)

    def test_main_text_and_structured_extra_context_are_independent(self):
        self.configure()
        draft = clone_prompt_profile_successor(self.profile,
            expected_source_contract_sha256=self.profile.contract_sha256, name="Independent sources")
        rule = draft.field_rules.get(field_definition=self.profile_field)
        rule.source_selector = rule.generation_source_selector = "request.action"
        rule.source_path = None
        rule.generation_context_selectors = ['request.structured_input:["custom.part",0,"count"]']
        rule.save()
        draft.review_state = "ready"
        draft.save(update_fields=("review_state",))
        refresh_prompt_profile_contract(draft)
        published = publish_prompt_profile(draft, actor=self.actor)
        from studio.services.execution_profile_inputs import profile_input_targets
        targets = profile_input_targets(published)
        self.assertIn("request.action", targets)
        root = targets["request.structured_input"]
        self.assertIn("value_schema", root)
        self.assertEqual(root["fields"][0]["usage"], "generation_context")

    def test_options_expose_operation_independent_generation_and_projected_source(self):
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data["generation_leaf_operations"]), {"prompt.fields.generate", "prompt.fields.translate"})
        self.assertIn("projection.output", {row["value"] for row in response.data["generation_source_selectors"]})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class GenericNativeResultTests(TestCase):
    setUpTestData = classmethod(native.ExecutionNativeTextTests.setUpTestData.__func__)
    make_output = native.ExecutionNativeTextTests.make_output
    setUp = native.ExecutionNativeTextTests.setUp
    smoke = native.ExecutionNativeTextTests.smoke

    def test_script_smoke_uses_the_shared_field_request_selection_and_message_compiler(self):
        from copy import deepcopy
        from studio.models import ModelProfileVersion, ModelDeployment, PromptProfileVersion
        from studio.services.json_contracts import assign_json_contract
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft, publish_execution_version
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.tests.test_execution_text_contracts import text_specification
        from studio.tests.test_execution_auxiliary_definitions import ExecutionAuxiliaryDefinitionTests

        model = ModelProfileVersion(profile_key="generic-leaf", version=1, provider=self.deployment.provider,
            model_identifier="declared-text-model", modality="text", status="published")
        assign_json_contract(model, "specification", text_specification()); model.save()
        deployment = ModelDeployment(key="generic-leaf", name="Field", profile_version=model)
        assign_json_contract(deployment, "runtime_config", {}); deployment.save()
        instance = create_execution_instance(key="generic-leaf", name="Field", description="", actor=self.actor)
        leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None,
                "prompt_profile_version": None, "prompt_mode": "none"})
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor,
            changes={"inputs": [ExecutionAuxiliaryDefinitionTests.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
        leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        profile = PromptProfileVersion.objects.create(profile_key="generic-script", version=1, name="Generic script",
            model_family="text-protocol", operation="script.generate", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="custom_business_direction", label="Business direction", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="request.text",
            generation_source_selector="request.text", generation_mode="model_generated", generation_output_semantics="final",
            generation_instruction="Clarify the supplied creative facts without adding a plot.", generation_required=True,
            language_policy="en", channel="positive_prompt", required=True, position=1, max_chars=4000)
        refresh_prompt_profile_contract(profile); profile = publish_prompt_profile(profile, actor=self.actor)
        instance = create_execution_instance(key="generic-native", name="Script with shared inputs", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "script.generate", "mode": "", "prompt_mode": "profile",
                "prompt_profile_version": profile, "workflow_version": None, "output_version": self.output_version})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": deepcopy(self.definition.value["configuration"]["inputs"]),
                "auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.fixture.update(manual_channels={}, source_language="English", target_language="English")
        self.fixture["inputs"]["request.text"] = {"kind": "literal", "value": "A traveler sets a box on the table."}
        _, prep, _ = self.smoke()
        request = prep.contract.resolved_inputs["field_requests"][0]["value"]
        self.assertEqual(request["items"][0]["source_text"], "A traveler sets a box on the table.")
        self.assertIsNone(prep.request_frozen_at)
        text = "[Beat 2] English direction.\nDialogue: 请把盒子放下。 Custom <Actor 7> notation."
        value = create_manual_execution_field(preparation_id=prep.pk, unit_key=prep.unit_key, field_key=field.key,
            text=text, revision=1, expected_preparation_revision=prep.revision, actor=self.actor)
        prep.refresh_from_db()
        confirm_execution_field(preparation_id=prep.pk, field_value_id=value.pk, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, expected_preparation_revision=prep.revision,
            select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.actor)
        prep.refresh_from_db()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        sent = json.loads(transport["payload"]["messages"][1]["content"])
        self.assertIn(text, sent["prompt_profile"]["positive_prompt"])
        self.assertNotIn(text, transport["payload"]["messages"][0]["content"])
        self.assertEqual(transport["operation_plan"]["plan"]["inputs"]["episode_count"], 1)
        self.assertIsNone(prep.request_frozen_at)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class GenericDirectMediaResultTests(image_fixture.StructureFixture):
    create_job = image_fixture.ProfileImageAppearanceTests.create_job
    primary_version = image_fixture.ProfileImageAppearanceTests.primary_version
    write = image_fixture.ProfileImageAppearanceTests.write
    input = staticmethod(image_fixture.ProfileImageAppearanceTests.input)
    request = image_fixture.ProfileImageAppearanceTests.request
    job = image_fixture.ProfileImageAppearanceTests.job
    preview = image_fixture.ProfileImageAppearanceTests.preview

    def setUp(self):
        from copy import deepcopy
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
        image_fixture.ProfileImageAppearanceTests.setUp(self)
        profile = clone_prompt_profile_successor(self.prompt_profile,
            expected_source_contract_sha256=self.prompt_profile.contract_sha256, name="Direct image fields")
        reference_final = getattr(self, "reference_final", False)
        if not reference_final:
            profile.field_rules.get(field_definition__key="unit_description").delete()
        rule = profile.field_rules.get(field_definition__key="exact_change")
        rule.generation_mode, rule.generation_output_semantics = "model_generated", "final"
        rule.generation_instruction = "Refine the supplied edit while preserving its exact reference labels."
        rule.generation_validator_keys = ["protected_tokens_at_least_once"]
        rule.generation_required = True
        rule.save()
        if not reference_final:
            profile.source_projection_key, profile.source_projection_config = "direct_v1", {}
        profile.review_state = "ready"
        profile.save()
        refresh_prompt_profile_contract(profile)
        self.prompt_profile = publish_prompt_profile(profile, actor=self.user)
        config = deepcopy(self.definition.value["configuration"])
        if reference_final:
            next(row for row in config["inputs"] if row["key"] == "reference_2")["semantic_contract"]["allowed_subject_kinds"] = ["external"]
        instance = create_execution_instance(key="direct-image-field", name="Direct image fields", description="", actor=self.user)
        self.version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "image.image_to_image", "mode": "",
                "workflow_version": self.workflow, "prompt_profile_version": self.prompt_profile, "prompt_mode": "profile"},
            configuration={"inputs": [row for row in config["inputs"] if reference_final or row["key"] != "units"],
                "auxiliaries": config["auxiliaries"]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        if not reference_final:
            self.inputs.pop("units")
        self.inputs["request.structured_input"]["value"]["changes"][1]["detail.text"] = "Turn only the red collar of <Picture 2> outward."

    def test_real_image_labels_and_original_variant_difference_survive_generated_final_field(self):
        from studio.services.json_contracts import json_contract
        from studio.services.media_generation_targets import read_media_generation_target
        job, created = self.job("structured_field", "direct-image-field")
        self.assertTrue(created)
        items = expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["field_contract"]["generation_output_semantics"], "final")
        self.assertEqual(items[0]["reference_facts"]["protected_tokens"], ["<Picture 2>"])
        self.assertEqual(len(items[0]["reference_facts"]["logical_inputs"]), 3)
        self.assertEqual(read_media_generation_target(self.prep).value["appearance"]["difference"]["value_sha256"],
            json_contract("Turn only the red collar of <Picture 2> outward.").sha256)
        row = create_manual_execution_field(preparation_id=self.prep.pk, unit_key=self.prep.unit_key, field_key="exact_change",
            text="Using <Picture 2>, turn its red collar outward without changing the identity.",
            revision=1, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=row.pk, expected_text_sha256=row.text_sha256,
            expected_source_sha256=row.source_sha256, expected_preparation_revision=self.prep.revision,
            select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.user)
        compiled = self.preview()
        self.assertIn(row.text, compiled.resolved_channels["values"]["positive_prompt"])
        self.assertEqual(compiled.resolved_channels["values"]["positive_prompt"].count(row.text), 1)
        self.assertIsNone(job.output_asset_id)
        self.external.assert_not_called()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class GenericReferenceMediaResultTests(image_fixture.StructureFixture):
    reference_final = True
    setUp = GenericDirectMediaResultTests.setUp
    create_job = GenericDirectMediaResultTests.create_job
    primary_version = GenericDirectMediaResultTests.primary_version
    write = GenericDirectMediaResultTests.write
    input = staticmethod(GenericDirectMediaResultTests.input)
    request = GenericDirectMediaResultTests.request
    job = GenericDirectMediaResultTests.job
    preview = GenericDirectMediaResultTests.preview
    select = reference.ExecutionReferencePreparationTests.select
    adopt = GenericReferenceResultTests.adopt

    def test_reference_final_subject_facts_replay_after_json_database_roundtrip(self):
        from uuid import uuid4
        identity = str(uuid4())
        self.inputs["reference_2"]["items"][0]["subject_ref"] = {"kind": "external", "id": identity}
        for unit in self.inputs["units"]["value"]["ordered_units"]:
            unit["subject_keys"] = [["external", identity]]
        job, created = self.create_job(self.request("structured_field"), key="reference-final-json", manual_channels={},
            source_language="English", target_language="English",
            subject_facts=[{"id": identity, "category": "prop", "name": "Parcel", "description": "A red parcel."}])
        self.prep = job.execution_preparations.get()
        self.assertTrue(created)
        self.prep.refresh_from_db()
        requests = self.prep.contract.resolved_inputs["field_requests"]
        self.assertEqual(requests, json.loads(json.dumps(requests)))
        final = next(row for row in expand_field_request(requests[0]["value"])["items"]
                     if row["field_contract"]["generation_output_semantics"] == "final")
        self.assertTrue(final["reference_facts"]["subject_facts"])
        self.assertTrue(all(isinstance(row["description_parts"], list)
                            for row in final["reference_facts"]["subject_facts"]))
        self.select("first", "Keep the parcel closed.")
        self.select("second", "Open the red parcel.")
        self.adopt("exact_change", "Turn only the red collar of <Picture 2> outward.", unit=self.prep.unit_key)
        compiled = self.preview()
        self.assertIn("red collar of <Picture 2>", compiled.resolved_channels["values"]["positive_prompt"])
        self.assertIsNone(job.output_asset_id)
        self.external.assert_not_called()
