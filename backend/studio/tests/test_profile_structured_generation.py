"""Exact custom input paths feed the original preparation/field-selection chain.

Isolated SQLite only. All network/executor entry points fail if invoked.
"""
from unittest.mock import patch

from studio.services.execution_field_contracts import expand_field_request

from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ModelDeployment, PromptProfileVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_draft, publish_execution_version, update_execution_draft
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.json_contracts import json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract, clone_prompt_profile_successor
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_execution_auxiliary_definitions as composite
from studio.tests import test_execution_profile_preparation as preparation
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class StructuredGenerationTests(StructureFixture):
    write_contract = staticmethod(composite.ExecutionAuxiliaryDefinitionTests.write_contract)
    input = staticmethod(composite.ExecutionAuxiliaryDefinitionTests.input)
    resolve = preparation.ExecutionProfilePreparationTests.resolve
    prepare = preparation.ExecutionProfilePreparationTests.prepare
    preview = preparation.ExecutionProfilePreparationTests.preview
    selected_text = preparation.ExecutionProfilePreparationTests.selected_text

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        composite.ExecutionAuxiliaryDefinitionTests.setUpTestData.__func__(cls)

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential",
                       "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No real model calls in structured path tests")))
        composite.ExecutionAuxiliaryDefinitionTests.setUp(self)

    def configure(self, mode="model_generated", *, generate_caption=False, context_paths=False):
        structure = self.publish(self.create([
            self.node("root", "object"), self.node("group", "list", "root", property_name="custom.part"),
            self.node("item", "object", "group"),
            self.node("motion", "text", "item", property_name="motion", allow_null=True),
            self.node("caption", "text", "item", property_name="caption", position=1),
            self.node("count", "integer", "item", property_name="count", position=2),
        ]))
        self.profile = PromptProfileVersion.objects.create(profile_key="exact-path-generation", version=1, name="Isolated paths",
            model_family="async_cloud", operation="video.generate", mode="T2VA", review_state="ready", input_structure_version=structure)
        for index, (key, path, generated) in enumerate((("composite_direction", "motion", True), ("custom_caption", "caption", generate_caption)), 1):
            # The original field services accept arbitrary exact field identities.
            if key == "composite_direction":
                field = self.profile_field = composite.FieldDefinition.objects.get(key="composite_direction")
            else:
                field = create_field_definition(key=key, label=path, value_type="text")
            create_profile_field_rule(profile_version=self.profile, field_definition=field,
                source_selector=SOURCE, source_path=["custom.part", 0, path], generation_source_selector=SOURCE,
                generation_mode=mode if generated else "passthrough", generation_output_semantics="final",
                generation_context_selectors=(["project.style"] + ([
                    'request.structured_input:["custom.part",0,"caption"]',
                    'request.structured_input:["custom.part",0,"count"]'] if context_paths and path == "motion" else [])) if generated else [],
                generation_instruction="Use only the exact selected source." if generated else "",
                generation_required=generated, language_policy="en", channel="positive_prompt", position=index,
                required=True, max_chars=4000)
        refresh_prompt_profile_contract(self.profile)
        self.profile = publish_prompt_profile(self.profile, actor=self.actor)
        if mode == "translate_only":
            instance = self.leaf.instance
            instance.refresh_from_db()
            leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
                selection={"deployment": ModelDeployment.objects.get(key="field-target"), "operation": "prompt.fields.translate",
                    "mode": "", "workflow_version": None, "prompt_profile_version": None, "prompt_mode": "none"})
            leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor,
                changes={"inputs": [self.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
            self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
                expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"prompt_profile_version": self.profile, "inputs": [self.input("style", "context.project.style", kind="json")], "auxiliaries": [
                {"purpose": "translate_fields" if mode == "translate_only" else "generate_fields", "target_version_id": str(self.leaf.pk)}]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.sources = {SOURCE: {"kind": "literal", "value": {"custom.part": [
            {"motion": "A courier opens a parcel.", "caption": "Soft daylight.", "count": 0}], "private_note": "NOT MODEL CONTEXT"}},
            "style": {"kind": "literal", "value": {"lighting": "soft"}}}
        self.parameters = {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3}
        self.job = AssetGenerationJob.objects.create(owner=self.actor, model_deployment=self.deployment, execution_instance_version=self.version,
            job_type="asset", media_type="video", operation="video.text_to_video", prompt="",
            idempotency_key="structured-generation", request_snapshot_sha256=json_contract({}).sha256)

    def test_selected_path_freezes_only_its_value_and_explicit_context_then_reuses_selection_compiler(self):
        self.configure()
        self.prepare()
        item = expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])["items"][0]
        self.assertEqual(item["source_text"], "A courier opens a parcel.")
        self.assertEqual(item["context_facts"], {"project.style": {"lighting": "soft"}})
        self.assertEqual(item["field_contract"]["source_path"], ["custom.part", 0, "motion"])
        self.assertNotIn("NOT MODEL CONTEXT", str(item))
        for row in item["reference_facts"]["logical_inputs"]:
            self.assertEqual({use["field_key"] for use in row["uses"]}, {"composite_direction"})
        self.assertFalse(self.prep.field_values.exists())
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        self.selected_text("A courier carefully opens the parcel.")
        result = self.preview()
        self.assertIn("carefully opens", result.resolved_channels["values"]["positive_prompt"])
        self.assertIn("Soft daylight.", result.resolved_channels["values"]["positive_prompt"])
        self.sources[SOURCE]["value"]["custom.part"][0]["motion"] = "Changed current value."
        self.assertEqual(result.resolved_channels, self.preview().resolved_channels)
        self.assertIsNone(self.prep.request_frozen_at)

    def test_translate_path_uses_translation_operation_and_same_original_manual_selection(self):
        self.configure("translate_only")
        self.prepare()
        request = expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])
        self.assertEqual(request["operation"], "prompt.fields.translate")
        self.assertEqual(request["items"][0]["source_text"], "A courier opens a parcel.")
        self.selected_text("A courier opens a parcel.")
        self.assertIn("A courier opens a parcel.", self.preview().resolved_channels["values"]["positive_prompt"])

    def test_multiple_context_paths_freeze_typed_values_without_other_field_leakage(self):
        self.configure(generate_caption=True, context_paths=True)
        self.prepare()
        items = expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])["items"]
        self.assertEqual(items[0]["context_facts"], {"project.style": {"lighting": "soft"},
            'request.structured_input:["custom.part",0,"caption"]': "Soft daylight.",
            'request.structured_input:["custom.part",0,"count"]': 0})
        self.assertEqual(items[1]["context_facts"], {"project.style": {"lighting": "soft"}})
        self.assertNotIn("NOT MODEL CONTEXT", str(items))
        self.sources[SOURCE]["value"]["custom.part"][0]["caption"] = "Changed later"
        self.prep.contract.refresh_from_db()
        self.assertEqual(expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])["items"], items)
        self.assertNotIn('context.request.structured_input:["custom.part",0,"caption"]', self.definition.value["interface"]["profile_inputs"])

    def test_missing_selected_context_is_not_silently_omitted(self):
        self.configure(context_paths=True)
        values = self.sources[SOURCE]["value"]
        del values["custom.part"][0]["count"]
        with self.assertRaises(ValidationError):
            project_execution_profile_sources(self.definition, {SOURCE: values, "style": {}})
        self.assertFalse(self.job.execution_preparations.exists())

    def test_context_paths_api_checks_schema_and_preserves_published_source(self):
        self.configure()
        original_sha = self.profile.contract_sha256
        draft = clone_prompt_profile_successor(self.profile, expected_source_contract_sha256=original_sha, name="Context edit")
        rule = draft.field_rules.get(field_definition=self.profile_field)
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/prompt-profile-field-rules/{rule.pk}/"
        selector = 'request.structured_input:["custom.part",0,"count"]'
        response = client.patch(url, {"generation_context_selectors": [selector]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["generation_context_selectors"], [selector])
        draft.refresh_from_db(); good_sha = draft.contract_sha256
        for selectors in ([selector, selector], ['request.structured_input:["missing"]'],
                          ['request.structured_input:["custom.part",-1]'], ['request.structured_input:[true]'],
                          ['request.structured_input:[ "custom.part",0,"count"]'], ['target_shot.everything']):
            with self.subTest(selectors=selectors):
                response = client.patch(url, {"generation_context_selectors": selectors}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                draft.refresh_from_db(); self.assertEqual(draft.contract_sha256, good_sha)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.contract_sha256, original_sha)

    def test_translation_can_use_same_explicit_structured_context(self):
        self.configure("translate_only", context_paths=True)
        self.prepare()
        request = expand_field_request(self.prep.contract.resolved_inputs["field_requests"][0]["value"])
        self.assertEqual(request["operation"], "prompt.fields.translate")
        self.assertEqual(request["items"][0]["context_facts"]['request.structured_input:["custom.part",0,"count"]'], 0)

    def test_missing_null_wrong_type_and_array_offset_never_become_generation_text(self):
        self.configure()
        for value in ({"custom.part": [{}]}, {"custom.part": [{"motion": None}]},
                      {"custom.part": [{"motion": 0}]}, {"custom.part": []}, {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                project_execution_profile_sources(self.definition, {SOURCE: value, "style": {}})
        self.assertFalse(self.job.execution_preparations.exists())

    def test_two_generated_fields_share_root_but_never_each_others_text_or_path(self):
        self.configure(generate_caption=True)
        self.prepare()
        requests = self.prep.contract.resolved_inputs["field_requests"]
        self.assertEqual(len(requests), 1)
        items = expand_field_request(requests[0]["value"])["items"]
        self.assertEqual([row["source_text"] for row in items], ["A courier opens a parcel.", "Soft daylight."])
        for row, key, path in zip(items, ("composite_direction", "custom_caption"), ("motion", "caption")):
            root = next(item for item in row["reference_facts"]["logical_inputs"] if item["target_key"] == SOURCE)
            self.assertEqual(root["uses"], [{"field_key": key, "usage": "generation_source", "selector": SOURCE,
                "source_path": ["custom.part", 0, path]}])
        self.assertEqual(self.prep.contract.resolved_inputs["profile_sources"]["projection"]["assembly_sources"], {})

    def test_rule_api_and_frozen_validation_reject_divergent_or_non_text_paths(self):
        self.configure()
        draft = clone_prompt_profile_successor(self.profile, expected_source_contract_sha256=self.profile.contract_sha256, name="Edit")
        rule = draft.field_rules.get(field_definition=self.profile_field)
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/prompt-profile-field-rules/{rule.pk}/"
        good = client.patch(url, {"generation_mode": "translate_only", "generation_instruction": "Translate this selected text."}, format="json")
        self.assertEqual(good.status_code, 200, good.data)
        self.assertEqual(good.data["source_path"], ["custom.part", 0, "motion"])
        draft.refresh_from_db(); expected = draft.contract_sha256
        for change in ({"generation_source_selector": "shot.action"}, {"source_path": ["custom.part", 0, "count"]},
                       {"generation_output_semantics": "contribution"}, {"source_path": ["unknown"]}):
            with self.subTest(change=change):
                response = client.patch(url, change, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                draft.refresh_from_db(); self.assertEqual(draft.contract_sha256, expected)
        from studio.services.prompt_profiles import prompt_profile_contract_payload, verify_prompt_profile_contract
        contract = prompt_profile_contract_payload(self.profile)
        self.assertEqual(verify_prompt_profile_contract(self.profile), contract)
        for change in ({"generation_source_selector": "shot.action"}, {"source_path": ["custom.part", 0, "count"]},
                       {"generation_output_semantics": "contribution"}):
            rules = list(self.profile.field_rules.order_by("position"))
            for key, value in change.items():
                setattr(rules[0], key, value)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                prompt_profile_contract_payload(self.profile, rules=rules)

    def test_production_compiler_never_uses_raw_path_or_rule_default_as_unselected_generated_value(self):
        from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
        from studio.services.prompt_profiles import PromptProfileError
        self.configure()
        facts = PromptProfileCompilationFacts(sources={SOURCE: self.sources[SOURCE]["value"]}, source_language="en",
            target_language="en", reference_context=None, reviewed_values={}, generation_contributions={})
        with self.assertRaises(PromptProfileError):
            compile_prompt_profile_facts(self.profile, facts=facts)
        draft = clone_prompt_profile_successor(self.profile, expected_source_contract_sha256=self.profile.contract_sha256, name="Optional")
        rule = draft.field_rules.get(field_definition=self.profile_field)
        rule.required = False; rule.generation_required = False; rule.has_default = True
        rule.default_value = "NOT A GENERATED RESULT"; rule.omit_if_empty = True
        rule.save()
        refresh_prompt_profile_contract(draft)
        draft.review_state = "ready"; draft.save(update_fields=("review_state",))
        profile = publish_prompt_profile(draft, actor=self.actor)
        compiled = compile_prompt_profile_facts(profile, facts=facts)
        self.assertNotIn("courier", str(compiled.assembly))
        self.assertNotIn("NOT A GENERATED RESULT", str(compiled.assembly))
