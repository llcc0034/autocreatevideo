"""Logical values -> exact Profile facts -> original compiler; no source lookup."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
from studio.tests import test_execution_profile_inputs as profile_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileSourceProjectionTests(TestCase):
    write_contract = staticmethod(profile_fixtures.ExecutionProfileInputTests.write_contract)
    setUpTestData = classmethod(profile_fixtures.ExecutionProfileInputTests.setUpTestData.__func__)
    setUp = profile_fixtures.ExecutionProfileInputTests.setUp
    profile = profile_fixtures.ExecutionProfileInputTests.profile
    logical_input = profile_fixtures.ExecutionProfileInputTests.logical_input
    configure = profile_fixtures.ExecutionProfileInputTests.configure

    def test_actual_declared_input_compiles_through_existing_profile_without_fake_project(self):
        profile = self.profile()
        self.configure(profile, [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        source = {"request.theme": "A paper boat crosses a still pond."}
        projection = project_execution_profile_sources(definition, source)
        self.assertEqual(projection["assembly_sources"], {"request.theme": source["request.theme"]})
        self.assertEqual(projection["generation_sources"], {})
        self.assertEqual(projection["omitted_targets"], [])
        self.assertEqual(projection["provenance"][0]["input_key"], "request.theme")
        compiled = compile_prompt_profile_facts(profile=profile, facts=PromptProfileCompilationFacts(
            sources=projection["assembly_sources"], source_language="English", target_language="English",
            reference_context=None, reviewed_values={}, generation_contributions={},
        ))
        self.assertIn(source["request.theme"], compiled.assembly["channels"]["positive_prompt"])
        self.assertNotIn("approved", projection)

    def test_actual_definition_resolves_literal_before_the_shared_compiler(self):
        profile = self.profile()
        self.configure(profile, [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        resolved = resolve_execution_input_sources(definition, {"request.theme": {"kind": "literal", "value": "A blue parcel."}},
            owner=self.actor, subject_facts=[], max_media_bytes=65536)
        projection = project_execution_profile_sources(definition, resolved.value["logical_values"])
        compiled = compile_prompt_profile_facts(profile=profile, facts=PromptProfileCompilationFacts(
            sources=projection["assembly_sources"], source_language="English", target_language="English",
            reference_context=None, reviewed_values={}, generation_contributions={},
        ))
        self.assertIn("A blue parcel.", compiled.assembly["channels"]["positive_prompt"])

    def test_missing_and_unknown_sources_do_not_use_graph_examples_or_silent_defaults(self):
        self.configure(self.profile(), [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        for value in ({}, {"request.theme": 7}, {"request.theme": None}, {"request.theme": "actual", "undeclared": "ignored?"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                project_execution_profile_sources(definition, value)

    def test_declared_default_is_left_to_the_original_field_assembler(self):
        self.configure(self.profile(default="Declared field default"), [self.logical_input(required=False)])
        projection = project_execution_profile_sources(build_execution_definition(self.version.pk), {})
        self.assertEqual(projection["assembly_sources"], {})
        self.assertEqual(projection["omitted_targets"], ["request.theme"])
        self.assertNotIn("Declared field default", str(projection))

    def test_boolean_facts_are_not_converted_to_strings_or_treated_as_missing(self):
        self.configure(self.profile(value_type="boolean"), [self.logical_input(kind="json")])
        definition = build_execution_definition(self.version.pk)
        projection = project_execution_profile_sources(definition, {"request.theme": False})
        self.assertIs(projection["assembly_sources"]["request.theme"], False)
        for value in (0, "false", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                project_execution_profile_sources(definition, {"request.theme": value})

    def test_generation_uses_its_own_source_not_final_output(self):
        # Alter only a local immutable test value; generated-source integration
        # and publication are independently covered by auxiliary definitions.
        self.configure(self.profile(), [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        value = deepcopy(definition.value)
        target = value["interface"]["profile_inputs"]["request.theme"]
        target["fields"][0].update(usage="generation_source", selector="shot.detailed_description")
        frozen = json_contract(value, max_bytes=65536)
        projection = project_execution_profile_sources(frozen, {"request.theme": "Actual source, not generated text."})
        self.assertEqual(projection["assembly_sources"], {})
        self.assertEqual(projection["generation_sources"], {"source_theme": "Actual source, not generated text."})
        self.assertEqual(projection["generation_contexts"], {})
        target["fields"][0]["selector"] = "projection.output"
        with self.assertRaises(ValidationError):
            project_execution_profile_sources(json_contract(value, max_bytes=65536), {"request.theme": "forged projection"})

    def test_string_list_preserves_item_order_and_rejects_scalar_coercion(self):
        self.configure(self.profile(value_type="string_list"), [self.logical_input(kind="json")])
        definition = build_execution_definition(self.version.pk)
        projection = project_execution_profile_sources(definition, {"request.theme": ["second", "first"]})
        self.assertEqual(projection["assembly_sources"]["request.theme"], ["second", "first"])
        for value in ("second, first", [1], ["first", False]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                project_execution_profile_sources(definition, {"request.theme": value})

    def test_checksum_mismatch_is_not_read_as_a_current_contract(self):
        self.configure(self.profile(), [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        definition.value["configuration"]["operation"] = "changed"
        with self.assertRaises(ValidationError):
            project_execution_profile_sources(definition, {"request.theme": "value"})
