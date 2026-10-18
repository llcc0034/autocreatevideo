"""Static M1 mapping compatibility for assembly, without database or media."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.assembly_contracts import assembly_output_schema, assembly_timing_schema
from studio.services.structure_mapping import evaluate_mapping, validate_mapping
from studio.tests.test_structure_mapping_preview import field, fixed, obj


class AssemblyMappingValidationTests(SimpleTestCase):
    def validate(self, node, source, target, *, complete=True):
        validate_mapping({"schema_version": 1, "node": node}, source, target, require_complete=complete)

    def test_integer_and_nullable_paths_match_nullable_integer_target(self):
        for source in ({"type": "integer"}, {"type": ["integer", "null"]},
                       {"anyOf": [{"type": "integer"}, {"type": "null"}]}):
            with self.subTest(source=source):
                self.validate(field(), source, {"type": ["integer", "null"], "minimum": 0})

    def test_incompatible_scalar_types_remain_rejected_without_coercion(self):
        for kind in ("string", "boolean", "number", "object", "array"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                self.validate(field(), {"type": kind}, {"type": ["integer", "null"]})

    def test_whole_object_path_checks_declared_properties_recursively(self):
        with patch("studio.services.assembly_contracts.boundary_int", return_value=8192):
            target = assembly_output_schema()
        for change in ("wrong_type", "missing", "extra"):
            source = deepcopy(target)
            if change == "wrong_type":
                source["properties"]["width"] = {"type": "string"}
            elif change == "missing":
                source["properties"].pop("width")
            else:
                source["properties"]["unexpected"] = {"type": "string"}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.validate(field(), source, target)
        self.validate(field(), deepcopy(target), target)

    def test_nested_array_path_checks_item_property_types(self):
        target = assembly_timing_schema()
        source = deepcopy(target)
        source["properties"]["episodes"]["items"]["properties"]["shots"]["items"]["properties"]["duration_ms"] = {"type": "string"}
        with self.assertRaises(ValidationError):
            self.validate(field(), source, target)
        self.validate(field(), deepcopy(target), target)

    def test_explicit_array_cardinality_is_checked_before_values_exist(self):
        for node in (obj(episodes={"kind": "array", "items": []}),
                     obj(episodes={"kind": "array", "items": [obj(number=fixed(1), shots={"kind": "array", "items": []})]})):
            with self.subTest(node=node), self.assertRaises(ValidationError):
                self.validate(node, {"type": "object"}, assembly_timing_schema())
        with self.assertRaises(ValidationError):
            self.validate({"kind": "array", "items": [fixed(1), fixed(2)]}, {},
                          {"type": "array", "maxItems": 1, "items": {"type": "integer"}})

    def test_nullable_values_are_not_coerced_or_defaulted_during_evaluation(self):
        schema = {"type": "object", "properties": {
            "start": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}
        target = {"type": "object", "properties": {"start": {"type": ["integer", "null"]}}, "required": ["start"]}
        node = obj(start=field("start"))
        self.validate(node, schema, target)
        for value in (0, None, 1250):
            result, origins = evaluate_mapping({"schema_version": 1, "node": node}, {"start": value},
                source_schema=schema, target_schema=target, consume=lambda _value: None)
            self.assertEqual(result, {"start": value})
            self.assertEqual(origins[0]["source_path"], "source.start")

    def test_general_partial_mapping_still_defers_configured_defaults(self):
        target = {"type": "object", "properties": {"x": {"type": "integer", "default": 2}}, "required": ["x"]}
        self.validate(obj(), {"type": "object"}, target, complete=False)
        self.validate(field(), {"type": "object", "properties": {}}, target, complete=False)
