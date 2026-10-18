from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_structure_mapping import bind_structure, evaluate_structure, validate_structure, validate_template_schema, freeze_structure_source_schemas
from studio.services.structure_paths import path_schema, read_structure_path


class DeepStructurePathTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.execution_structure_mapping.execution_configuration_max_bytes", return_value=65536))
        self.schema = {"type": "object", "properties": {"a.b": {"type": "array", "items": {
            "type": "object", "properties": {"嵌套": {"type": "array", "items": {
                "type": "object", "properties": {"情绪": {"type": "string"}, "flag": {"type": "boolean"}}}}}}}}}
        self.value = {"a.b": [{"嵌套": [{"情绪": "紧张", "flag": False}, {"情绪": "释然"}]}]}
        option = {"selector_key": "fixture", "kind": "json", "value_schema": self.schema}
        self.enterContext(patch("studio.services.execution_structure_mapping.execution_context_field_options", return_value=[option]))
        self.enterContext(patch("studio.services.execution_structure_mapping.execution_text_output_options", return_value=[{**option, "output_key": "fixture"}]))

    def test_nested_each_uses_nearest_item_and_keeps_actual_indices(self):
        for kind, key in (("context_field", "selector_key"), ("selected_output", "output_key")):
            source = {"kind": kind, key: "fixture", "path": ["a.b"]}
            template = {"kind": "each", "source": source, "item": {"kind": "each",
                "source": {"kind": "item_field", "path": ["嵌套"]}, "item": {"kind": "object", "fields": {
                    "mood": {"kind": "item_field", "path": ["情绪"]},
                    "flag": {"kind": "item_field", "path": ["flag"], "if_missing": False}}}}}
            before = deepcopy(template)
            validate_structure(template, leaf_validator=lambda *args: None, path="mapping")
            validate_template_schema(template, {"type": "array", "items": {"type": "array", "items": {
                "type": "object", "properties": {"mood": {"type": "string"}, "flag": {"type": "boolean"}}}}}, "mapping")
            calls = []
            bound = bind_structure(template, lambda node: calls.append(node) or {**node, "revision": "exact"}, source_schemas=freeze_structure_source_schemas())
            self.assertEqual(len(calls), 1)
            self.assertNotIn("path", calls[0])
            def read(node):
                self.assertNotIn("path", node)
                self.assertNotIn("source_schema_sha256", node)
                return self.value, {"revision": node["revision"]}
            value, evidence = evaluate_structure(bound, read_leaf=read, unit_key="unit", reference_subjects=[], path="target")
            self.assertEqual(value, [[{"mood": "紧张", "flag": False}, {"mood": "释然", "flag": False}]])
            self.assertEqual(evidence[-1]["source_path"], 'source["a.b"][0]["嵌套"][1].flag')
            self.assertEqual(evidence[-1]["resolution"], "explicit_if_missing")
            self.assertEqual(template, before)

    def test_tokens_are_literal_and_nullable_container_does_not_invent_values(self):
        nullable = {"anyOf": [{"type": "null"}, self.schema]}
        self.assertEqual(path_schema(nullable, ["a.b", 0, "嵌套", 0, "情绪"], "path")["type"], "string")
        for tokens in (None, [True], [-1], [0.1], ["a", "b"], ["a.b", "*"], ["a.b", 0, "missing"]):
            with self.subTest(tokens=tokens), self.assertRaises(ValidationError): path_schema(nullable, tokens, "path")
        for value, tokens in ((None, ["a.b"]), ({"a.b": None}, ["a.b", 0]), (self.value, ["a.b", 9])):
            with self.subTest(value=value), self.assertRaises(ValidationError): read_structure_path(value, tokens, "source", "target", if_missing=False)
        self.assertEqual(read_structure_path({"a.b": None}, ["a.b"], "source", "target", if_missing=False), (None, 'source["a.b"]', True))

    def test_old_field_unknown_schema_and_unscoped_item_are_rejected(self):
        for node in ({"kind": "item_field", "field": "情绪"}, {"kind": "item_field", "path": []},
                {"kind": "each", "source": {"kind": "item_field", "path": ["嵌套"]}, "item": {"kind": "literal", "value": 1}}):
            with self.subTest(node=node), self.assertRaises(ValidationError): validate_structure(node, leaf_validator=lambda *args: None, path="test")
        with self.assertRaises(ValidationError): path_schema({}, ["anything"], "path")
        self.assertEqual(path_schema({}, [], "path"), {})

    def test_only_absent_final_property_uses_fallback(self):
        self.assertEqual(read_structure_path({"parent": {}}, ["parent", "flag"], "source", "target", if_missing=False)[::2], (False, False))
        for value in ({}, {"parent": None}, {"parent": []}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                read_structure_path(value, ["parent", "flag"], "source", "target", if_missing=False)

    def test_first_binding_rejects_catalog_drift_since_plan_freeze(self):
        frozen = freeze_structure_source_schemas()
        before = deepcopy(frozen)
        node = {"kind": "context_field", "selector_key": "fixture", "path": ["a.b", 0, "嵌套", 0, "情绪"]}
        self.schema["description"] = "changed reader contract with same path and type"
        calls = []
        with self.assertRaisesMessage(ValidationError, "冻结运行计划不一致"):
            bind_structure(node, lambda row: calls.append(row), source_schemas=frozen)
        self.assertEqual(calls, [])
        self.assertEqual(frozen, before)
