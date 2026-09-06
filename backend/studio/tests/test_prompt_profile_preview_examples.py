from django.test import SimpleTestCase

from studio.models import PromptFieldDefinition
from studio.services.prompt_profile_preview_examples import (
    PROMPT_PROFILE_PREVIEW_EXAMPLES,
    prompt_profile_preview_example,
)


class PromptProfilePreviewExampleTests(SimpleTestCase):
    def definition(self, *, key: str, value_type: str, **extra):
        return PromptFieldDefinition(
            key=key,
            label=key,
            category="preview",
            description="Preview example test field",
            impact="Preview only",
            value_type=value_type,
            **extra,
        )

    def test_h3_examples_are_neutral_non_empty_and_preview_only(self):
        for key in (
            "subject_definitions",
            "summary",
            "retention_analysis",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        ):
            with self.subTest(key=key):
                definition = self.definition(
                    key=key,
                    value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
                    default_value="PRODUCTION DEFAULT MUST NOT BE USED",
                )
                example = prompt_profile_preview_example(definition)
                self.assertIsInstance(example, str)
                self.assertTrue(example.strip())
                self.assertNotEqual(example, definition.default_value)
                self.assertEqual(example, PROMPT_PROFILE_PREVIEW_EXAMPLES[key])

    def test_unknown_fields_receive_type_compatible_generic_examples(self):
        cases = (
            (PromptFieldDefinition.ValueType.TEXT, {}, "Generic preview value"),
            (
                PromptFieldDefinition.ValueType.LONG_TEXT,
                {},
                "A neutral example used only to preview the configured prompt structure.",
            ),
            (PromptFieldDefinition.ValueType.STRING_LIST, {}, ["generic_reference"]),
            (PromptFieldDefinition.ValueType.BOOLEAN, {}, True),
            (PromptFieldDefinition.ValueType.NUMBER, {}, 1),
            (
                PromptFieldDefinition.ValueType.ENUM,
                {"value_options": ["cinematic", "natural"]},
                "cinematic",
            ),
        )
        for index, (value_type, extra, expected) in enumerate(cases):
            with self.subTest(value_type=value_type):
                definition = self.definition(
                    key=f"custom_preview_{index}",
                    value_type=value_type,
                    **extra,
                )
                self.assertEqual(prompt_profile_preview_example(definition), expected)

    def test_mutable_examples_are_copied_per_response(self):
        definition = self.definition(
            key="reference_assets",
            value_type=PromptFieldDefinition.ValueType.STRING_LIST,
        )
        first = prompt_profile_preview_example(definition)
        first.append("mutated")
        self.assertEqual(
            prompt_profile_preview_example(definition),
            ["reviewed_reference_asset"],
        )
