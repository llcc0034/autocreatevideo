"""Authoring choices must be backed by the same executable reader directory."""
from types import SimpleNamespace
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.content_sources import source_reader_options
from studio.services.execution_source_readers import validate_source_reader
from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.prompt_generation_sources import prompt_generation_source_options, prompt_generation_source_values
from studio.services.prompt_profiles import PromptProfileError, validate_current_generation_source


class PromptGenerationSourceTests(SimpleTestCase):
    def projected_inputs(self, source):
        contract = {"schema_version": 8, "source_projection_key": "direct_v1", "source_projection_config": {},
            "rules": [{"field_key": "text", "required": True, "source_selector": "request.text",
                "generation_mode": "model_generated", "generation_output_semantics": "final",
                "generation_source_selector": source, "generation_context_selectors": []}]}
        with patch("studio.services.execution_profile_inputs.verify_prompt_profile_contract", return_value=contract), \
                patch("studio.services.execution_profile_inputs.execution_configuration_max_bytes", return_value=1000000):
            return profile_input_targets(SimpleNamespace(pk="profile"))

    def test_new_inputs_reject_retired_or_non_reader_frozen_generation_sources(self):
        for source in ("shot.narrative", "character.face_identity", "fixed.value", "projection.output"):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.projected_inputs(source)

    def test_current_reader_inputs_are_projected_without_database_lookup(self):
        for source in prompt_generation_source_values() - {"projection.output"}:
            with self.subTest(source=source):
                targets = self.projected_inputs(source)
                self.assertEqual(list(targets), [source])
                self.assertEqual(targets[source]["fields"][0]["usage"], "generation_source")
                self.assertTrue(targets[source]["required"])

    def test_choices_are_derived_from_implemented_readers_without_database_queries(self):
        options = prompt_generation_source_options()
        readers = source_reader_options()
        self.assertEqual([item["value"] for item in options], [item["value"] for item in readers] + ["projection.output"])
        self.assertEqual(len(options), len(prompt_generation_source_values()))
        for item, source in zip(options, readers):
            validate_source_reader(source["reader"])
            self.assertEqual(item["scope_type"], source["reader"]["scope_kind"])
            self.assertEqual(item["scope"], source["execution_usage"])
            self.assertTrue(all(item[key] for key in ("label", "group", "group_label", "description", "availability")))

    def test_retired_column_names_are_not_accepted_even_if_the_name_looks_valid(self):
        for source in ("character.face_identity", "scene.lighting", "prop.continuity_state",
                       "shot.narrative", "shot.detailed_description", "project.continuity_summary", "style.negative_prompt"):
            with self.subTest(source=source):
                self.assertNotIn(source, prompt_generation_source_values())
                rule = SimpleNamespace(generation_mode="model_generated", generation_source_selector=source,
                    source_selector="request.text", channel="positive_prompt", profile_version=SimpleNamespace(operation="video.generate"))
                with self.assertRaises(PromptProfileError) as error:
                    validate_current_generation_source(rule)
                self.assertEqual(error.exception.code, "prompt_profile_generation_source_unknown")

    def test_every_current_text_reader_is_valid_for_both_generation_modes(self):
        for source in prompt_generation_source_values() - {"projection.output"}:
            for mode in ("model_generated", "translate_only"):
                with self.subTest(source=source, mode=mode):
                    validate_current_generation_source(SimpleNamespace(generation_mode=mode,
                        generation_source_selector=source, source_selector="request.text", channel="positive_prompt",
                        profile_version=SimpleNamespace(operation="image.generate")))
