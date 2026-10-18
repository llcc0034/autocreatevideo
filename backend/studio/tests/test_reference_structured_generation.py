"""Per-unit custom paths, through the original published preparation and choices."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIClient

from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.prompt_profiles import clone_prompt_profile_successor, prompt_profile_contract_payload
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_execution_reference_preparation as reference
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class ReferenceStructuredGenerationTests(StructureFixture):
    input = staticmethod(reference.ExecutionReferencePreparationTests.input)
    write = reference.ExecutionReferencePreparationTests.write
    resolve = reference.ExecutionReferencePreparationTests.resolve
    prepare = reference.ExecutionReferencePreparationTests.prepare
    preview = reference.ExecutionReferencePreparationTests.preview
    select = reference.ExecutionReferencePreparationTests.select

    def configure_profile(self, profile):
        self.structure = self.publish(self.create([
            self.node("root", "object"), self.node("custom", "list", "root", property_name="custom.part"),
            self.node("item", "object", "custom"),
            self.node("motion", "text", "item", property_name="motion", allow_null=True),
            self.node("number", "integer", "item", property_name="count", position=1),
        ]))
        profile.input_structure_version = self.structure
        profile.save(update_fields=("input_structure_version",))
        rule = profile.field_rules.get()
        rule.generation_source_selector = SOURCE
        rule.source_path = ["custom.part", 0, "motion"]
        rule.save()

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential",
                       "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No real model calls in unit source tests")))
        reference.ExecutionReferencePreparationTests.setUp(self, configure_profile=self.configure_profile)
        for unit, text in zip(self.sources["units"]["value"]["ordered_units"], ("A courier waits.", "A parcel opens.")):
            unit["structured_input"] = {"custom.part": [{"motion": text, "count": 0}], "unselected": "PRIVATE NON-CONTEXT"}

    def test_exact_per_unit_sources_freeze_and_selected_results_reach_original_renderer(self):
        original = deepcopy(self.sources)
        targets = profile_input_targets(self.prompt_profile)
        unit_schema = targets["reference_context.units"]["value_schema"]["properties"]["ordered_units"]["items"]
        self.assertIn("structured_input", unit_schema["required"])
        self.assertEqual(unit_schema["properties"]["structured_input"]["properties"]["custom.part"]["type"], "array")
        self.prepare()
        frozen = self.prep.contract.resolved_inputs
        items = frozen["field_requests"][0]["value"]["items"]
        self.assertEqual([item["source_text"] for item in items], ["A courier waits.", "A parcel opens."])
        self.assertNotIn("PRIVATE NON-CONTEXT", str(items))
        self.assertEqual(self.sources, original)
        self.assertEqual(frozen["profile_sources"]["projection"]["reference"]["unit_input"], original["units"]["value"])
        self.assertNotIn("structured_input", frozen["profile_sources"]["reference_context"]["value"]["context"]["ordered_units"][0])
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        self.select("first", "The courier waits beside a blue doorway.")
        self.select("second", "The parcel opens on the table.")
        result = self.preview()
        prompt = result.resolved_channels["values"]["positive_prompt"]
        self.assertIn("The courier waits beside a blue doorway.", prompt)
        self.assertIn("The parcel opens on the table.", prompt)
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"] = {"changed": True}
        self.assertEqual(self.preview().resolved_channels, result.resolved_channels)

    def test_missing_null_nontext_and_out_of_range_sources_create_no_preparation(self):
        first = self.sources["units"]["value"]["ordered_units"][0]
        for value in (None, {}, {"custom.part": []}, {"custom.part": [{"motion": None}]}, {"custom.part": [{"motion": 7}]}):
            with self.subTest(value=value):
                first["structured_input"] = value
                with self.assertRaises(ValidationError):
                    self.prepare()
                self.assertFalse(self.job.execution_preparations.exists())
        del first["structured_input"]
        with self.assertRaises(ValidationError):
            self.prepare()
        self.assertFalse(self.job.execution_preparations.exists())

    def test_contract_writer_requires_path_and_exact_structure_and_text_type(self):
        for change in ({"source_path": None}, {"source_path": ["custom.part", 0, "count"]}):
            rules = list(self.prompt_profile.field_rules.order_by("position"))
            for key, value in change.items():
                setattr(rules[0], key, value)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                prompt_profile_contract_payload(self.prompt_profile, rules=rules)

    def test_formal_rule_patch_keeps_input_path_distinct_from_render_primitive(self):
        draft = clone_prompt_profile_successor(self.prompt_profile, expected_source_contract_sha256=self.prompt_profile.contract_sha256, name="Unit edit")
        rule = draft.field_rules.get()
        client = APIClient()
        self.user.is_staff = self.user.is_superuser = True
        self.user.save(update_fields=("is_staff", "is_superuser"))
        client.force_authenticate(self.user)
        url = f"/api/v1/prompt-profile-field-rules/{rule.pk}/"
        response = client.patch(url, {"source_path": ["custom.part", 0, "motion"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["generation_source_selector"], SOURCE)
        self.assertEqual(response.data["projection_primitive"], "reference_context.detailed_description")
        response = client.patch(url, {"source_path": ["custom.part", 0, "count"]}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        rule.refresh_from_db()
        self.assertEqual(rule.source_path, ["custom.part", 0, "motion"])

    def test_two_paths_translate_separately_without_changing_their_render_destinations(self):
        from studio.services.execution_reference_fields import prepare_reference_profile_fields
        from studio.services.reference_context_facts import read_reference_context_facts
        from studio.services.json_contracts import JsonContract
        from studio.services.execution_instances import execution_configuration_max_bytes
        self.prepare()
        context = read_reference_context_facts(JsonContract(**self.prep.contract.resolved_inputs["profile_sources"]["reference_context"]),
            max_bytes=execution_configuration_max_bytes())
        profile = clone_prompt_profile_successor(self.prompt_profile, expected_source_contract_sha256=self.prompt_profile.contract_sha256, name="Translation")
        rule = profile.field_rules.get()
        rule.generation_mode = "translate_only"
        rule.save()
        field = create_field_definition(key="custom_sound", label="Custom sound", value_type="text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="projection.output",
            projection_primitive="reference_context.overall_soundscape", generation_mode="translate_only",
            generation_output_semantics="contribution", generation_source_selector=SOURCE, source_path=["custom.part", 1, "motion"],
            generation_instruction="Translate only this text.", generation_context_selectors=["project.style"],
            generation_required=True, language_policy="en", channel="positive_prompt", position=2, max_chars=7000)
        refresh_prompt_profile_contract(profile)
        profile.review_state = "ready"
        profile.save(update_fields=("review_state",))
        profile = publish_prompt_profile(profile, actor=self.user)
        values = {row["unit_key"]: deepcopy(row["structured_input"]) for row in self.sources["units"]["value"]["ordered_units"]}
        for key, value in values.items():
            value["custom.part"].append({"motion": f"Sound for {key}."})
        result = prepare_reference_profile_fields(profile=profile, reference_context=context, structured_inputs=values,
            generation_contexts={key: {"project.style": self.sources["styles"]["value"][key]} for key in values},
            source_language="Chinese", target_language="English").value
        request = result["field_requests"][0]["value"]
        self.assertEqual(request["operation"], "prompt.fields.translate")
        self.assertEqual([(row["unit_key"], row["field_contract"]["field_key"], row["source_text"]) for row in request["items"]], [
            ("first", "unit_description", "A courier waits."), ("first", "custom_sound", "Sound for first."),
            ("second", "unit_description", "A parcel opens."), ("second", "custom_sound", "Sound for second.")])
        with self.assertRaisesMessage(ValidationError, "一一对应"):
            prepare_reference_profile_fields(profile=profile, reference_context=context, structured_inputs={"first": values["first"]},
                generation_contexts={key: {"project.style": self.sources["styles"]["value"][key]} for key in values},
                source_language="Chinese", target_language="English")

    def test_original_appearance_difference_reader_uses_raw_unit_path_not_renderer_text(self):
        from studio.services.media_generation_appearances import media_appearance_difference
        from studio.services.json_contracts import json_contract
        self.prepare()
        source = {"kind": "reference_field", "key": "units", "unit_key": "first", "field_key": "unit_description"}
        resolved = self.prep.contract.resolved_inputs
        result = media_appearance_difference(source, definition=self.definition, resolved_inputs=resolved, request={})
        self.assertEqual(result, {**source, "value_sha256": json_contract("A courier waits.").sha256})
        self.assertNotIn("text", result)
        changed = deepcopy(resolved)
        # Preserve projection/input agreement, but remove the actual leaf.
        for root in (changed["inputs"]["logical_values"]["units"], changed["profile_sources"]["projection"]["reference"]["unit_input"]):
            del root["ordered_units"][0]["structured_input"]["custom.part"][0]["motion"]
        with self.assertRaises(ValidationError):
            media_appearance_difference(source, definition=self.definition, resolved_inputs=changed, request={})

    def test_structured_context_is_read_from_its_own_unit_not_an_external_context_port(self):
        from studio.services.execution_reference_fields import prepare_reference_profile_fields
        from studio.services.reference_context_facts import read_reference_context_facts
        from studio.services.json_contracts import JsonContract
        from studio.services.execution_instances import execution_configuration_max_bytes
        self.prepare()
        context = read_reference_context_facts(JsonContract(**self.prep.contract.resolved_inputs["profile_sources"]["reference_context"]),
            max_bytes=execution_configuration_max_bytes())
        profile = clone_prompt_profile_successor(self.prompt_profile, expected_source_contract_sha256=self.prompt_profile.contract_sha256, name="Context paths")
        rule = profile.field_rules.get()
        selector = 'request.structured_input:["custom.part",0,"count"]'
        rule.generation_context_selectors = [selector]
        rule.save(); refresh_prompt_profile_contract(profile)
        profile.review_state = "ready"; profile.save(update_fields=("review_state",))
        profile = publish_prompt_profile(profile, actor=self.user)
        self.assertNotIn("context." + selector, profile_input_targets(profile))
        values = {row["unit_key"]: deepcopy(row["structured_input"]) for row in self.sources["units"]["value"]["ordered_units"]}
        values["first"]["custom.part"][0]["count"] = 1
        values["second"]["custom.part"][0]["count"] = 2
        result = prepare_reference_profile_fields(profile=profile, reference_context=context, structured_inputs=values,
            generation_contexts={}, source_language="Chinese", target_language="English").value
        items = result["field_requests"][0]["value"]["items"]
        self.assertEqual([row["context_facts"] for row in items], [{selector: 1}, {selector: 2}])
        self.assertNotIn("PRIVATE NON-CONTEXT", str(items))
        with self.assertRaises(ValidationError):
            prepare_reference_profile_fields(profile=profile, reference_context=context, structured_inputs=values,
                generation_contexts={"first": {selector: 999}}, source_language="Chinese", target_language="English")
