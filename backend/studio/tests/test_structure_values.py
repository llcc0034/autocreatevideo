from copy import deepcopy
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.data_structures import (
    compile_structure_nodes, data_structure_snapshot, resolve_structure_value,
    transition_data_structure, validate_structure_value, write_data_structure,
)
from studio.tests.test_data_structures import StructureFixture


class StructureMissingValueTests(StructureFixture):
    def contract(self, kind="text", **options):
        return data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("entry", kind, "root", property_name="entry", **options),
        ])))

    def test_missing_required_optional_and_explicit_default_are_distinct(self):
        for required in (False, True):
            for has_default in (False, True):
                with self.subTest(required=required, has_default=has_default):
                    frozen = self.contract(required=required, has_default=has_default,
                        default_value="configured" if has_default else None)
                    with self.assertNumQueries(0):
                        result = resolve_structure_value(frozen, {})
                    self.assertEqual(result["valid"], not required or has_default)
                    self.assertEqual(result["value"], {"entry": "configured"} if has_default else {})
                    self.assertEqual(bool(result["defaults"]), has_default)
                    self.assertEqual(result["errors"], {"value.entry": ["缺少必填字段"]} if required and not has_default else {})

    def test_six_value_groups_keep_presence_separate_from_truthiness(self):
        cases = [("text", None, True), ("text", "", False), ("integer", 0, False),
            ("boolean", False, False), ("object", {}, False)]
        for kind, value, nullable in cases:
            with self.subTest(kind=kind, value=value):
                default = {"text": "fallback", "integer": 7, "boolean": True, "object": {"custom": 1}}[kind]
                frozen = self.contract(kind, required=True, allow_null=nullable, has_default=True, default_value=default)
                supplied = {"entry": value, "unknown": {"raw": False}}
                original = deepcopy(supplied)
                result = resolve_structure_value(frozen, supplied)
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["value"], original)
                self.assertEqual(result["defaults"], [])
                self.assertEqual(supplied, original)
                result["value"]["unknown"]["raw"] = True
                self.assertEqual(supplied, original)
        frozen = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("entry", "list", "root", property_name="entry", required=True,
                has_default=True, default_value=["fallback"]), self.node("item", "text", "entry"),
        ])))
        self.assertEqual(resolve_structure_value(frozen, {"entry": []})["value"], {"entry": []})
        self.assertEqual(resolve_structure_value(frozen, {})["value"], {"entry": ["fallback"]})

    def test_null_blank_and_empty_constraints_do_not_trigger_default_replacement(self):
        for kind, value, options, default in [
            ("text", None, {"allow_null": False}, "fallback"),
            ("text", "", {"allow_blank": False}, "fallback"),
            ("object", {}, {"allow_empty": False}, {"custom": 1}),
        ]:
            with self.subTest(kind=kind, value=value):
                frozen = self.contract(kind, has_default=True, default_value=default, **options)
                result = resolve_structure_value(frozen, {"entry": value})
                self.assertFalse(result["valid"])
                self.assertIn("value.entry", result["errors"])
                self.assertEqual(result["value"], {"entry": value})
                self.assertEqual(result["defaults"], [])
        frozen = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("entry", "list", "root", property_name="entry", allow_empty=False,
                has_default=True, default_value=["fallback"]), self.node("item", "text", "entry"),
        ])))
        self.assertFalse(resolve_structure_value(frozen, {"entry": []})["valid"])
        self.assertEqual(resolve_structure_value(frozen, {"entry": []})["value"], {"entry": []})

    def test_defaults_can_explicitly_be_null_blank_zero_false_or_empty(self):
        for kind, default in [("text", None), ("text", ""), ("integer", 0), ("boolean", False), ("object", {})]:
            with self.subTest(kind=kind, default=default):
                frozen = self.contract(kind, required=True, allow_null=True, has_default=True, default_value=default)
                result = resolve_structure_value(frozen, {})
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["value"], {"entry": default})
                self.assertEqual(result["defaults"], [{"source": "configured_default", "path": "value.entry",
                    "node_key": "entry", "value": default, "structure_version_id": frozen["id"],
                    "field_definition_version_id": str(self.definitions[kind].pk)}])

    def test_nested_objects_list_elements_and_unmapped_values_remain_explicit(self):
        structure = self.publish(self.create([
            self.node("root", "object"),
            self.node("omitted", "object", "root", property_name="optional"),
            self.node("in_omitted", "text", "omitted", property_name="text", has_default=True, default_value="unused"),
            self.node("group", "object", "root", property_name="group", has_default=True, default_value={}),
            self.node("flag", "boolean", "group", property_name="flag", required=True, has_default=True, default_value=False),
            self.node("rows", "list", "root", property_name="rows", required=True),
            self.node("row", "object", "rows", allow_null=True),
            self.node("amount", "integer", "row", property_name="amount", has_default=True, default_value=0),
        ]))
        frozen = data_structure_snapshot(structure)
        raw = {"rows": [{}, {"amount": 5}, None], "extra": ["keep"]}
        original = deepcopy(raw)
        result = resolve_structure_value(frozen, raw)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"], {"group": {"flag": False}, "rows": [{"amount": 0}, {"amount": 5}, None], "extra": ["keep"]})
        self.assertEqual([source["path"] for source in result["defaults"]], ["value.group", "value.group.flag", "value.rows[0].amount"])
        self.assertEqual(raw, original)
        self.assertNotIn("optional", result["value"])

    def test_invalid_defaults_wrong_switches_and_impossible_locations_are_rejected(self):
        for kind, value, options in [("integer", False, {}), ("boolean", "false", {}),
                ("text", None, {}), ("text", "", {"allow_blank": False}), ("enum", "unknown", {})]:
            with self.subTest(kind=kind, value=value):
                with self.assertRaises(ValidationError) as caught:
                    self.contract(kind, has_default=True, default_value=value, **options)
                self.assertIn("nodes.entry.default_value", caught.exception.message_dict)
        for options in ({"has_default": True}, {"has_default": "true", "default_value": "x"}, {"default_value": "hidden"}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.create([self.node("root", "object"), self.node("entry", "text", "root", property_name="entry", **options)])
        for nodes in ([self.node("root", "text", has_default=True, default_value="x")],
                [self.node("root", "list"), self.node("item", "text", "root", has_default=True, default_value="x")]):
            with self.assertRaises(ValidationError):
                self.publish(self.create(nodes))

    def test_snapshot_is_complete_and_changes_never_reinterpret_existing_defaults(self):
        frozen = self.contract("integer", required=True, has_default=True, default_value=0)
        before = deepcopy(frozen)
        clone = write_data_structure(source_version_id=frozen["id"], content={})
        child = clone.nodes.get(node_key="entry")
        child.default_value = 9
        child.save()
        clone = self.publish(clone)
        transition_data_structure(version_id=frozen["id"], status="retired")
        with self.assertNumQueries(0):
            self.assertEqual(resolve_structure_value(frozen, {})["value"], {"entry": 0})
        self.assertEqual(frozen, before)
        with self.assertRaises(ValidationError):
            resolve_structure_value({**frozen, "schema_version": 1}, {})
        nodes = deepcopy(frozen["nodes"])
        nodes[1].pop("has_default")
        with self.assertRaises(ValidationError):
            compile_structure_nodes(nodes)
        with self.assertRaises(ValidationError):
            validate_structure_value(frozen, {})
        self.assertEqual(resolve_structure_value(data_structure_snapshot(clone), {})["value"], {"entry": 9})

    def test_paths_disambiguate_arbitrary_json_property_names(self):
        frozen = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("name", "text", "root", property_name="a.b[]名", required=True),
        ])))
        result = resolve_structure_value(frozen, {})
        self.assertIn('value["a.b[]名"]', result["errors"])

    def test_nullable_parent_still_reports_missing_child_and_does_not_coerce_values(self):
        frozen = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object", allow_null=True),
            self.node("entry", "integer", "root", property_name="entry", required=True),
        ])))
        self.assertIn("value.entry", resolve_structure_value(frozen, {})["errors"])
        for value in (False, "0", None):
            result = resolve_structure_value(frozen, {"entry": value})
            self.assertIn("value.entry", result["errors"])
            self.assertEqual(result["value"], {"entry": value})
        self.assertTrue(resolve_structure_value(frozen, None)["valid"])

    def test_empty_list_default_and_non_finite_input(self):
        frozen = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("entry", "list", "root", property_name="entry",
                has_default=True, default_value=[]), self.node("item", "decimal", "entry"),
        ])))
        self.assertEqual(resolve_structure_value(frozen, {})["defaults"][0]["value"], [])
        with self.assertRaises(ValidationError):
            resolve_structure_value(frozen, {"entry": [float("nan")]})


class StructureValuePreviewTests(StructureFixture):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="value-preview", is_staff=True))

    def url(self, structure):
        return f"/api/v1/data-structure-versions/{structure.pk}/value-preview/"

    def test_preview_is_read_only_with_values_defaults_and_partial_errors(self):
        draft = self.create([self.node("root", "object"),
            self.node("amount", "integer", "root", property_name="amount", required=True),
            self.node("name", "text", "root", property_name="name", has_default=True, default_value="Configured"),
        ])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.url(draft), {"value": {"amount": None, "extra": 0}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["preview_only"])
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["value"], {"amount": None, "name": "Configured", "extra": 0})
        self.assertIn("value.amount", response.data["errors"])
        self.assertEqual(response.data["defaults"][0]["structure_version_id"], str(draft.pk))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")

    def test_request_requires_presence_not_truthiness_and_rejects_hidden_writes(self):
        draft = self.create([self.node("root", "boolean", allow_null=True)])
        for value in (False, None):
            response = self.client.post(self.url(draft), {"value": value}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["valid"])
            self.assertEqual(response.data["value"], value)
        for payload in ({}, {"value": False, "nodes": []}):
            self.assertEqual(self.client.post(self.url(draft), payload, format="json").status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="ordinary"))
        self.assertEqual(self.client.post(self.url(draft), {"value": False}, format="json").status_code, 403)

    def test_invalid_contract_preserves_value_and_reports_config_path(self):
        draft = self.create([self.node("root", "list")])
        response = self.client.post(self.url(draft), {"value": ["unchanged"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["value"], ["unchanged"])
        self.assertEqual(response.data["defaults"], [])
        self.assertIn("nodes.root", response.data["errors"])
