from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import SeriesProject, Shot
from studio.services.execution_field_contracts import verify_execution_field_request
from studio.services.execution_reference_fields import prepare_reference_profile_fields
from studio.services.json_contracts import JsonContract
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_profiles import refresh_prompt_profile_contract
from studio.services.reference_context_facts import read_reference_context_facts
from studio.tests import test_execution_profile_compilation as profile_fixtures
from studio.tests.reference_rendering_fixtures import rendering_config, generation_structure_fixture


class ExecutionReferenceFieldTests(TestCase):
    _field = profile_fixtures.ExecutionProfileCompilationTests._field
    _profile = profile_fixtures.ExecutionProfileCompilationTests._profile
    _rule = profile_fixtures.ExecutionProfileCompilationTests._rule
    _publish = profile_fixtures.ExecutionProfileCompilationTests._publish

    def setUp(self):
        profile_fixtures.ExecutionProfileCompilationTests.setUp(self)
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential"):
            self.enterContext(patch(target, side_effect=AssertionError("Preparing fields cannot call a model")))
        self.profile = self._profile(source_projection_key="declarative_v1")
        self.profile.contract_schema_version = 8
        self.profile.source_projection_config = rendering_config()
        self.profile.save()
        generation_structure_fixture(self.profile)
        self.rule = self._rule(self.profile, self._field("reference_description"), selector="projection.output")
        self.rule.projection_primitive = "reference_context.detailed_description"
        self.rule.generation_mode = "model_generated"
        self.rule.generation_output_semantics = "contribution"
        self.rule.generation_source_selector = "request.structured_input"
        self.rule.source_path = ["direction"]
        self.rule.generation_context_selectors = ["project.style", "target_shot.core"]
        self.rule.generation_instruction = "Describe only this unit using its declared source and context."
        self.rule.language_policy = "en"
        self.rule.generation_required = True
        self.rule.save()
        refresh_prompt_profile_contract(self.profile)
        self.profile = self._publish(self.profile)
        first = replace(self.unit, unit_key="first", render_values={**self.unit.render_values, "description": "A courier arrives."})
        second = replace(self.unit, unit_key="second", render_values={**self.unit.render_values, "description": "The parcel opens."})
        self.reference = replace(self.reference, ordered_units=(first, second))
        self.structured_inputs = {"first": {"direction": "A courier arrives."}, "second": {"direction": "The parcel opens."}}
        self.contexts = {"first": {"project.style": {"palette": "blue"}, "target_shot.core": {"action": "arrival"}},
            "second": {"project.style": {"palette": "blue"}, "target_shot.core": {"action": "open parcel"}}}

    def prepare(self, **changes):
        return prepare_reference_profile_fields(**{"profile": self.profile, "reference_context": self.reference,
            "generation_contexts": self.contexts, "structured_inputs": self.structured_inputs,
            "source_language": "English", "target_language": "English", **changes})

    def test_each_actual_unit_gets_its_own_source_and_declared_context(self):
        result = self.prepare()
        self.assertEqual(len(result.value["field_requests"]), 1)
        request = verify_execution_field_request(JsonContract(**result.value["field_requests"][0]))
        self.assertEqual(request["operation"], "prompt.fields.generate")
        self.assertEqual([item["unit_key"] for item in request["items"]], ["first", "second"])
        self.assertEqual([item["source_text"] for item in request["items"]], ["A courier arrives.", "The parcel opens."])
        for item in request["items"]:
            self.assertEqual(item["context_facts"], self.contexts[item["unit_key"]])
            self.assertEqual(item["field_contract"]["generation_output_semantics"], "contribution")
        self.assertEqual(len({item["request_id"] for item in request["items"]}), 2)
        self.assertEqual(result, self.prepare())
        self.assertEqual(SeriesProject.objects.count(), 0)
        self.assertEqual(Shot.objects.count(), 0)

    def test_frozen_reference_round_trip_does_not_acquire_approval_or_current_content(self):
        frozen = self.prepare().value
        actual = read_reference_context_facts(JsonContract(**frozen["reference_context"]), max_bytes=65536)
        self.assertEqual(actual, self.reference)
        self.assertEqual(actual.generation_contributions, {})
        self.contexts["first"]["target_shot.core"]["action"] = "later change"
        self.assertEqual(frozen["generation_contexts"]["first"]["target_shot.core"]["action"], "arrival")
        self.assertNotIn("approved", frozen)
        with self.assertRaises(PromptBuildError):
            self.prepare(reference_context=replace(self.reference,
                generation_contributions={("first", "shot.detailed_description"): "not approved by a field record"}))

    def test_aggregate_reference_preparation_uses_resolved_cap_not_leaf_cap(self):
        # Each source and auxiliary batch is still <64KiB, but their combined
        # preparation envelope legitimately exceeds it; no model is invoked.
        from studio.services.json_contracts import canonical_json_bytes
        self.contexts["first"]["project.style"]["palette"] = "x" * 20000
        self.contexts["second"]["project.style"]["palette"] = "y" * 20000
        result = self.prepare()
        self.assertGreater(result.size_bytes, 65536)
        self.assertLess(result.size_bytes, 262144)
        self.assertLess(len(canonical_json_bytes(result.value["generation_contexts"])), 65536)
        for row in result.value["field_requests"]:
            self.assertLess(row["size_bytes"], 65536)
            verify_execution_field_request(JsonContract(**row))
        with patch("studio.services.execution_reference_fields.execution_resolved_inputs_max_bytes", return_value=65536):
            with self.assertRaisesMessage(ValidationError, "65536"):
                self.prepare()

    def test_missing_unit_unknown_unit_and_extra_context_are_not_silently_accepted(self):
        cases = [dict(list(self.contexts.items())[:1]), {**self.contexts, "unknown": {}},
            {**self.contexts, "first": {"project.style": {}}},
            {**self.contexts, "first": {**self.contexts["first"], "hidden": "extra context"}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.prepare(generation_contexts=value)

    def test_reference_tokens_from_another_unit_are_not_allowed(self):
        reference = {"category": "prop", "entity_id": None, "input_label": "<Picture 1>", "unit_keys": ["second"]}
        self.structured_inputs["first"]["direction"] = "Use <Picture 1>."
        with self.assertRaisesMessage(ValidationError, "未声明引用"):
            self.prepare(reference_context=replace(self.reference, reference_bindings=(reference,)))

    def test_unit_reference_facts_do_not_leak_to_another_units_task(self):
        reference = {"category": "prop", "entity_id": None, "input_label": "<Picture 1>", "unit_keys": ["second"],
            "source_selection": {"source": "asset_upload", "overridden": True}}
        context = replace(self.reference, reference_bindings=(reference,))
        items = verify_execution_field_request(JsonContract(**self.prepare(reference_context=context).value["field_requests"][0]))["items"]
        self.assertEqual(items[0]["reference_facts"]["logical_inputs"], [])
        self.assertEqual(items[1]["reference_facts"]["logical_inputs"], [reference])

    def test_generated_subject_labels_share_renderer_identity_and_keep_unit_sources_scoped(self):
        from studio.services.reference_context_facts import ReferenceSubjectFacts
        from studio.services.prompt_profiles import verify_prompt_profile_contract
        contract = deepcopy(verify_prompt_profile_contract(self.profile))
        policy = contract["source_projection_config"]["primitive_options"]["reference_context_ir_v1"]["rendering"]
        policy["reference_layout"] = {"subject_label_template": "<Subject {index}>", "source_separator": ", ",
            "summary_reference_template": "{label}: {usage}", "shot_reference_template": ""}
        policy["templates"]["entity_reference"] = "{label} is {subject} from {source_labels}."
        context = replace(self.reference, subject_facts=(ReferenceSubjectFacts("prop", "one-id", ("same prop",)),),
            ordered_units=tuple(replace(unit, subject_keys=(("prop", "one-id"),)) for unit in self.reference.ordered_units),
            reference_bindings=tuple({"category": "prop", "entity_id": "one-id", "entity_label": "same prop",
                "input_label": f"<Picture {index}>", "unit_keys": [unit]} for index, unit in enumerate(("first", "second"), 1)))
        with patch("studio.services.execution_reference_fields.verify_prompt_profile_contract", return_value=contract):
            items = verify_execution_field_request(JsonContract(**self.prepare(reference_context=context).value["field_requests"][0]))["items"]
        for index, item in enumerate(items, 1):
            subject = item["reference_facts"]["subject_facts"][0]
            self.assertEqual(subject["reference_label"], "<Subject 1>")
            self.assertEqual(subject["source_labels"], [f"<Picture {index}>"])

    def test_language_or_mode_conflict_does_not_use_defaults(self):
        with self.assertRaisesMessage(ValidationError, "目标语言"):
            self.prepare(target_language="Chinese")
        with self.assertRaises(ValidationError):
            self.prepare(source_language=None)
        if self.profile.mode:
            with self.assertRaisesMessage(ValidationError, "参考模式"):
                self.prepare(reference_context=replace(self.reference, mode="unconfigured-mode"))

    def test_unconfigured_reference_category_or_purpose_fails_before_field_requests(self):
        for category, purpose in (("external", ""), ("video", ""), ("video", "unknown")):
            with self.subTest(category=category, purpose=purpose):
                reference = {"category": category, "purpose": purpose, "entity_id": None,
                    "input_label": "<Picture 1>", "unit_keys": ["second"]}
                with self.assertRaisesMessage(ValidationError, "参考用途未配置"):
                    self.prepare(reference_context=replace(self.reference, reference_bindings=(reference,)))

    def test_unconfigured_paired_audio_purpose_fails_before_field_requests(self):
        reference = {"category": "video", "purpose": "motion_camera_reference", "entity_id": None,
            "input_label": "<Video 1>", "audio_input_label": "<Audio 1>", "audio_purpose": "",
            "unit_keys": ["second"]}
        with self.assertRaisesMessage(ValidationError, "参考用途未配置"):
            self.prepare(reference_context=replace(self.reference, reference_bindings=(reference,)))
