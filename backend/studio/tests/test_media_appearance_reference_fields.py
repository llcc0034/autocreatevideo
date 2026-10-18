"""Original structured reference fields as compact appearance difference sources.

Published isolated Profile/Workflow fixtures, no network, live data or invented
field adoption. Source tests use the original multi-unit reference preparation;
they do not claim full image variant lifecycle acceptance.
"""
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.services.json_contracts import json_contract
from studio.services.media_generation_appearances import media_appearance_difference, normalize_media_appearance_request
from studio.tests import test_execution_reference_preparation as reference_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class ReferenceFieldDifferenceTests(TestCase):
    input = staticmethod(reference_fixtures.ExecutionReferencePreparationTests.input)
    write = reference_fixtures.ExecutionReferencePreparationTests.write
    resolve = reference_fixtures.ExecutionReferencePreparationTests.resolve
    prepare = reference_fixtures.ExecutionReferencePreparationTests.prepare
    preview = reference_fixtures.ExecutionReferencePreparationTests.preview
    select = reference_fixtures.ExecutionReferencePreparationTests.select

    def setUp(self):
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No real model")))
        reference_fixtures.ExecutionReferencePreparationTests.setUp(self)
        self.facts = self.resolve(self.definition, self.job).resolved_inputs.value
        self.source = {"kind": "reference_field", "key": "units", "unit_key": "second", "field_key": "unit_description"}

    def difference(self, source=None, facts=None):
        return media_appearance_difference(source or self.source, definition=self.definition,
            resolved_inputs=self.facts if facts is None else facts, request={})

    def test_exact_unit_field_reuses_original_text_and_does_not_copy_body(self):
        before = deepcopy(self.facts)
        with CaptureQueriesContext(connection) as queries:
            result = self.difference()
        # Security limits still resolve through the ordinary config registry;
        # no current Profile, business unit or field candidate may be reread.
        for query in queries:
            self.assertNotRegex(query["sql"], r'(?i)\b(?:FROM|JOIN) "studio_(?:promptprofile|execution|asset|shot|seriesproject|bible)')
        self.assertEqual(result, {**self.source, "value_sha256": json_contract("A parcel opens.").sha256})
        self.assertNotIn("A parcel opens.", str(result))
        self.assertEqual(self.facts, before)
        self.assertNotEqual(result["value_sha256"], self.difference({**self.source, "unit_key": "first"})["value_sha256"])

    def test_declared_shape_is_exact_and_cannot_smuggle_text_or_selector(self):
        base = {"role": "variant", "setting_revision_id": None, "primary_version_id": "11111111-1111-4111-8111-111111111111",
            "primary_input": {"input_key": "image", "item_key": "main"}, "difference_input": self.source}
        self.assertEqual(normalize_media_appearance_request(base), base)
        for source in ({**self.source, "text": "copy"}, {**self.source, "selector": "shot.action"},
                {k: v for k, v in self.source.items() if k != "unit_key"}, {**self.source, "field_key": ""},
                {**self.source, "unit_key": "x" * 192}, {**self.source, "kind": "logical_input"}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                normalize_media_appearance_request({**base, "difference_input": source})

    def test_context_unknown_unit_and_unprepared_field_are_not_difference_sources(self):
        for changes in ({"key": "styles"}, {"unit_key": "missing"}, {"field_key": "unmapped"},
                {"kind": "logical_input", "key": "units", "unit_key": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.difference({**self.source, **changes})

    def test_missing_duplicate_or_corrupt_original_field_requests_are_rejected(self):
        for change in ("missing", "duplicate", "digest", "source", "profile"):
            facts = deepcopy(self.facts)
            task = facts["field_requests"][0]
            if change == "missing":
                facts["field_requests"] = []
            elif change == "duplicate":
                facts["field_requests"].append(deepcopy(task))
            elif change == "digest":
                task["sha256"] = "0" * 64
            else:
                if change == "source":
                    task["value"]["items"][1]["source_text"] = "Another unit's difference."
                else:
                    task["value"]["profile"]["contract_sha256"] = "0" * 64
                facts["field_requests"][0] = asdict(json_contract(task["value"]))
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.difference(facts=facts)

    def test_reference_context_must_match_original_unit_even_with_valid_envelope_digest(self):
        for change in ("missing", "corrupt", "source"):
            facts = deepcopy(self.facts)
            context = facts["profile_sources"]["reference_context"]
            if change == "missing":
                del facts["profile_sources"]["reference_context"]
            elif change == "corrupt":
                context["sha256"] = "0" * 64
            else:
                context["value"]["context"]["ordered_units"][1]["duration_ms"] = 999
                facts["profile_sources"]["reference_context"] = asdict(json_contract(context["value"]))
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.difference(facts=facts)

    def test_empty_source_is_not_repaired_from_context_or_another_unit(self):
        self.sources["units"]["value"]["ordered_units"][1]["structured_input"]["direction"] = "   "
        self.facts = self.resolve(self.definition, self.job).resolved_inputs.value
        with self.assertRaisesMessage(ValidationError, "非空文本"):
            self.difference()

    def test_original_field_choices_compile_without_changing_the_frozen_difference_basis(self):
        original = self.difference()
        self.prepare()
        self.select("first", "A courier arrives beside a blue doorway.")
        self.select("second", "A red parcel opens on the table.")
        compiled = self.preview()
        self.assertIn("A red parcel opens on the table.", compiled.resolved_channels["values"]["positive_prompt"])
        self.prep.refresh_from_db()
        self.assertEqual(self.difference(facts=self.prep.contract.resolved_inputs), original)
        self.assertNotEqual(original["value_sha256"], json_contract("A red parcel opens on the table.").sha256)
