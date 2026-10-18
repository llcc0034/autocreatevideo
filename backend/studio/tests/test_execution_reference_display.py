"""Frozen correspondence through the original preparation, no model dispatch."""
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.services.execution_reference_display import frozen_reference_display
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.tests import test_execution_shot_reference_facts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class FrozenReferenceDisplayTests(TestCase):
    _create_version = fixtures.ShotReferencePreparationPersistenceTests._create_version
    selection = fixtures.ShotReferencePreparationPersistenceTests.selection
    prepare = fixtures.ShotReferencePreparationPersistenceTests.prepare
    configure = fixtures.ShotReferencePreparationPersistenceTests.configure
    resolve = fixtures.ShotReferencePreparationPersistenceTests.resolve
    setUp = fixtures.ShotReferencePreparationPersistenceTests.setUp

    def prepared(self):
        prep = self.prepare()
        facts = deepcopy(prep.contract.resolved_inputs)
        uses = read_execution_workflow_uses(self.definition, json_contract(facts["inputs"]))
        return facts, uses

    def test_saved_name_version_and_slot_are_read_without_current_choice_or_writes(self):
        facts, uses = self.prepared()
        key, = uses.items
        original = deepcopy(facts)
        # Warm scoped configuration once; the projection itself needs no ORM.
        with security_boundary_resolution_cache():
            expected = frozen_reference_display(facts, uses)
            with self.assertNumQueries(0):
                actual = frozen_reference_display(facts, uses)
        self.assertEqual(actual, expected)
        self.assertEqual(actual[key]["shot_reference"], self.expected)
        self.assertEqual(actual[key]["input_slot"], 1)
        self.assertEqual(actual[key]["position"], 0)
        self.assertEqual(actual[key]["reference_name"], self.asset.name)
        self.assertFalse(actual[key]["name_disambiguated"])
        self.use.reference_version = None
        self.use.asset = self.asset
        self.use.save(update_fields=("reference_version", "asset"))
        self.asset.name = "Current name is not a frozen label"
        self.asset.save(update_fields=("name",))
        with patch("studio.services.shot_reference_sources.resolve_shot_reference_sources", side_effect=AssertionError("No current selection")):
            self.assertEqual(frozen_reference_display(facts, uses), expected)
        actual[key]["shot_reference"]["purpose"] = "changed outside"
        self.assertEqual(facts, original)

    def test_absent_profile_mapping_is_explicit_not_invented(self):
        facts, uses = self.prepared()
        facts.pop("profile_sources")
        row, = frozen_reference_display(facts, uses).values()
        self.assertIsNone(row["reference_name"])
        self.assertIsNone(row["name_disambiguated"])
        self.assertIsNone(row["input_slot"])
        self.assertEqual(row["shot_reference"], self.expected)
        self.assertEqual(row["original_name"], self.asset.name)

    def test_rehashed_conflicting_name_version_or_slot_is_not_silently_repaired(self):
        facts, uses = self.prepared()
        for field, value in (("source_sha256", "0"*64), ("original_name", "wrong"), ("input_slot", 99),
                ("input_label", "<Picture 99>"), ("shot_reference", None), ("name_disambiguated", "false")):
            with self.subTest(field=field):
                damaged = deepcopy(facts)
                context = damaged["profile_sources"]["reference_context"]["value"]
                context["context"]["reference_bindings"][0][field] = value
                damaged["profile_sources"]["reference_context"] = asdict(json_contract(context))
                with self.assertRaises(ValidationError):
                    frozen_reference_display(damaged, uses)
