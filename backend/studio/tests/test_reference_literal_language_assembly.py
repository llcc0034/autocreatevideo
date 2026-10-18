"""Exact English-policy name bindings survive the formal preview/freeze chain."""
from django.test import TestCase, override_settings

from studio.tests import test_prompt_final_assembly_contract as assembly_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class EnglishReferenceAssemblyTests(assembly_fixture.AssemblyAssertions, TestCase):
    _create_version = assembly_fixture.ReferenceFinalAssemblyTests._create_version
    selection = assembly_fixture.ReferenceFinalAssemblyTests.selection
    resolve = assembly_fixture.ReferenceFinalAssemblyTests.resolve
    prepare = assembly_fixture.ReferenceFinalAssemblyTests.prepare
    candidate = assembly_fixture.ReferenceFinalAssemblyTests.candidate
    references = assembly_fixture.ReferenceFinalAssemblyTests.references

    def configure(self, profile):
        auxiliaries = assembly_fixture.ReferenceFinalAssemblyTests.configure(self, profile)
        rule = profile.field_rules.get(field_definition__key="custom_name_mapping")
        rule.language_policy = "en"
        rule.save(update_fields=("language_policy",))
        return auxiliaries

    def setUp(self):
        assembly_fixture.ReferenceFinalAssemblyTests.setUp(self)

    def test_original_names_slots_and_bytes_match_preview_and_frozen_request_under_en(self):
        assembly_fixture.ReferenceFinalAssemblyTests.test_names_dynamic_body_original_dialogue_and_sound_match_preview_and_frozen_request(self)
