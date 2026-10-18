from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_structure_mapping import (evaluate_structure, validate_structure,
    validate_structure_value, validate_template_schema)


class ExplicitMissingMappingTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.execution_structure_mapping.execution_configuration_max_bytes", return_value=65536))
        self.node = {"kind": "item_field", "path": ["voiceover"], "if_missing": False}
        self.schema = {"type": "object", "properties": {"voiceover": {"type": "boolean"}}}

    def evaluate(self, item, node=None):
        return evaluate_structure(node or self.node, item=item, read_leaf=lambda _: None,
            unit_key="shot:one", reference_subjects=[], path="dialogue[0].voiceover")

    def test_authored_false_only_for_absence_with_auditable_evidence(self):
        for source in ({}, {"voiceover": True}, {"voiceover": False}):
            before = deepcopy(source)
            value, evidence = self.evaluate(source)
            self.assertIs(value, source.get("voiceover", False))
            self.assertEqual(source, before)
            self.assertEqual(evidence[0].get("resolution") == "explicit_if_missing", "voiceover" not in source)

    def test_present_invalid_values_are_not_replaced_and_missing_still_fails_without_rule(self):
        for value in (None, "false", 0, [], {}):
            resolved, _ = self.evaluate({"voiceover": value})
            self.assertEqual(resolved, value)
            with self.assertRaises(ValidationError): validate_structure_value(resolved, {"type": "boolean"}, "voiceover")
        with self.assertRaisesMessage(ValidationError, "缺少所选字段"):
            self.evaluate({}, {"kind": "item_field", "path": ["voiceover"]})
        with self.assertRaises(ValidationError): self.evaluate(None)

    def test_rule_must_match_source_and_target_and_closed_node_shape(self):
        validate_structure(self.node, leaf_validator=lambda *a: None, path="test", item_schema=self.schema)
        validate_template_schema(self.node, {"type": "boolean"}, "test", item_schema=self.schema)
        for change in ({"if_missing": None}, {"if_missing": 0}, {"field": "unknown"}, {"silent": True}):
            with self.assertRaises(ValidationError):
                validate_structure({**self.node, **change}, leaf_validator=lambda *a: None, path="test", item_schema=self.schema)
        with self.assertRaises(ValidationError): validate_template_schema(self.node, {"type": "string"}, "test", item_schema=self.schema)
