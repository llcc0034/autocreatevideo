from django.test import SimpleTestCase
from studio.models import FieldDefinition, FieldDefinitionVersion
from studio.services.prompt_profile_preview_examples import prompt_profile_preview_example


class PromptProfilePreviewExampleTests(SimpleTestCase):
    def definition(self, *, key="arbitrary_field", value_type="long_text", **extra):
        return FieldDefinitionVersion(field_definition=FieldDefinition(key=key), version=1,
            label=key, category="preview", value_type=value_type, **extra)

    def test_examples_depend_on_type_not_business_key(self):
        sample = prompt_profile_preview_example(self.definition())
        for key in ("subject_definitions", "summary", "seed", "custom_non_script"):
            self.assertEqual(prompt_profile_preview_example(self.definition(key=key)), sample)

    def test_examples_are_type_compatible_and_structured_examples_are_not_guessed(self):
        for kind, options, value in (
            ("text", [], "Generic preview value"), ("integer", [], 1), ("decimal", [], 1),
            ("boolean", [], True), ("enum", ["甲", "乙"], "甲"), ("list", [], ["generic_reference"]),
            ("object", [], None), ("media", [], None), ("", [], None),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(prompt_profile_preview_example(self.definition(value_type=kind, value_options=options)), value)

    def test_examples_are_not_shared_mutable_defaults(self):
        definition = self.definition(value_type="list")
        prompt_profile_preview_example(definition).append("changed")
        self.assertEqual(prompt_profile_preview_example(definition), ["generic_reference"])
        self.assertFalse(hasattr(definition, "default_value"))
