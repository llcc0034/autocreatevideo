"""Difference identity reuses the published structured Profile path and resolver."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import update_execution_draft
from studio.services.execution_profile_preparation import prepare_execution_profile
from studio.services.json_contracts import json_contract
from studio.services.media_generation_appearances import media_appearance_difference, normalize_media_appearance_request
from studio.services.prompt_profiles import assemble_prompt_profile
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests import test_profile_structured_inputs as fixtures
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class StructuredDifferenceTests(StructureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        fixtures.execution_fixtures.ExecutionDefinitionTests.setUpTestData.__func__(cls)
    write_contract = staticmethod(fixtures.ProfileStructuredInputTests.write_contract)
    profile = fixtures.ProfileStructuredInputTests.profile

    def setUp(self):
        fixtures.ProfileStructuredInputTests.setUp(self)
        self.prompt_profile = self.profile()
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            changes={"prompt_mode": "profile", "prompt_profile_version": self.prompt_profile, "inputs": []}, actor=self.actor)
        self.definition = build_execution_definition(self.version.pk)
        self.source = {"kind": "structured_field", "key": SOURCE, "field_key": "custom_motion"}
        self.raw = {"自定义.输入": {"motion": "Turn the red collar outward.", "flag": False, "count": 0}}

    def prepare(self, raw):
        inputs = resolve_execution_input_sources(self.definition, {SOURCE: {"kind": "literal", "value": raw}},
            owner=self.actor, subject_facts=[], max_media_bytes=65536)
        prepared = prepare_execution_profile(self.definition, inputs, profile=self.prompt_profile, unit_key="fixture",
            source_language="zh", target_language="en")
        return {**prepared.value, "inputs": inputs.value}

    def difference(self, facts=None, source=None):
        return media_appearance_difference(source or self.source, definition=self.definition,
            resolved_inputs=facts if facts is not None else self.prepare(self.raw), request={})

    def test_published_path_and_original_assembly_consume_the_same_nonempty_text(self):
        facts = self.prepare(self.raw)
        original = deepcopy(facts)
        result = self.difference(facts)
        compiled = assemble_prompt_profile(self.prompt_profile, sources=facts["profile_sources"]["projection"]["assembly_sources"])
        text = compiled["structured_fields"]["custom_motion"]
        self.assertEqual(text, self.raw["自定义.输入"]["motion"])
        self.assertEqual(result, {**self.source, "value_sha256": json_contract(text).sha256})
        self.assertNotIn(text, str(result))
        self.assertEqual(facts, original)

    def test_original_defaults_and_missing_evidence_cannot_supply_difference(self):
        raw = deepcopy(self.raw)
        del raw["自定义.输入"]["motion"]
        facts = self.prepare(raw)
        self.assertEqual(facts["inputs"]["logical_values"][SOURCE]["自定义.输入"]["motion"], "wave")
        with self.assertRaisesMessage(ValidationError, "默认值"):
            self.difference(facts)
        facts = self.prepare(self.raw)
        for origins in ([], facts["inputs"]["input_provenance"] * 2):
            with self.subTest(origins=origins), self.assertRaises(ValidationError):
                self.difference({**facts, "inputs": {**facts["inputs"], "input_provenance": origins}})

    def test_unmapped_nontext_empty_and_cross_source_are_rejected(self):
        for changes in ({"field_key": "unknown"}, {"field_key": "custom_flag"}, {"field_key": "custom_count"}, {"key": "other"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.difference(source={**self.source, **changes})
        raw = deepcopy(self.raw)
        raw["自定义.输入"]["motion"] = " "
        with self.assertRaisesMessage(ValidationError, "非空文本"):
            self.difference(self.prepare(raw))

    def test_caller_cannot_add_path_unit_or_text_to_published_field_reference(self):
        declaration = {"role": "variant", "setting_revision_id": None,
            "primary_version_id": "11111111-1111-4111-8111-111111111111",
            "primary_input": {"input_key": "image", "item_key": "main"}, "difference_input": self.source}
        self.assertEqual(normalize_media_appearance_request(declaration), declaration)
        for changes in ({"path": ["flag"]}, {"unit_key": "other"}, {"text": "copy"}, {"field_key": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                normalize_media_appearance_request({**declaration, "difference_input": {**self.source, **changes}})

    def test_changed_projection_or_profile_identity_is_not_silently_reassembled(self):
        for change in ("projection", "identity"):
            facts = self.prepare(self.raw)
            if change == "projection":
                facts["profile_sources"]["projection"]["assembly_sources"][SOURCE]["自定义.输入"]["motion"] = "Changed"
            else:
                facts["profile_sources"]["profile"]["contract_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.difference(facts)
