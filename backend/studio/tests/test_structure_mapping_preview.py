import json
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.data_structures import data_structure_snapshot, describe_data_structure
from studio.services.json_contracts import parse_json_object_text, parse_json_value_text
from studio.services.structure_mapping import preview_structure_mapping, structure_path_options
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.test_data_structures import StructureFixture


def field(*tokens, scope="root"):
    return {"kind": "path", "scope": scope, "path": list(tokens)}


def obj(**fields):
    return {"kind": "object", "fields": fields}


def each(source, item):
    return {"kind": "each", "source": source, "item": item}


def fixed(value):
    return {"kind": "literal", "value": value}


def preview(value, node, *, source=None, target=None, text=None):
    return preview_structure_mapping(source_contract=source or frozen_response_structure(value),
        target_contract=target or frozen_response_structure({"result": ""}),
        source_text=json.dumps(value, ensure_ascii=False) if text is None else text,
        mapping={"schema_version": 1, "node": node}, max_bytes=1024 * 1024)


class JsonStructureMappingTests(SimpleTestCase):
    def test_two_layouts_nested_lists_and_custom_field_only_change_configuration(self):
        expected = {"episodes": [{"shots": [{"action": "转身", "mood": "紧张", "off_screen": False}]}]}
        target = frozen_response_structure(expected)
        for episodes, shots, motion, mood in [("episodes", "shots", "action", "mood_note"), ("chapters", "frames", "motion", "tone")]:
            with self.subTest(layout=episodes):
                raw = {episodes: [{shots: [{motion: "转身", mood: "紧张", "off_screen": False}]}]}
                node = obj(episodes=each(field(episodes), obj(shots=each(field(shots, scope="item"), obj(
                    action=field(motion, scope="item"), mood=field(mood, scope="item"), off_screen=field("off_screen", scope="item"))))))
                before = deepcopy(raw)
                result = preview(raw, node, target=target)
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["value"], expected)
                self.assertEqual(raw, before)
                leaf = next(row for row in result["provenance"] if row["path"] == "value.episodes[0].shots[0].action")
                self.assertEqual(leaf["source_path"], f"source.{episodes}[0].{shots}[0].{motion}")
                self.assertEqual(result["raw_value"], before)

    def lookup(self, collection="entities", key=None, value_path=None):
        return {"kind": "lookup", "collection": field(collection), "key_path": ["key"],
            "key": key or fixed("actor-1"), "value_path": ["name"] if value_path is None else value_path}

    def test_lookup_uses_declared_key_not_translated_or_display_name(self):
        raw = {"entities": [{"key": "actor-2", "name": "同名"}, {"key": "actor-1", "name": "同名"}],
            "shots": [{"actor": "actor-1"}]}
        node = {"kind": "array", "items": [self.lookup(), fixed("constant")]}
        result = preview(raw, node, target=frozen_response_structure([""]))
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"], ["同名", "constant"])
        self.assertEqual(result["provenance"][-2]["source_path"], "source.entities[1].name")
        rows = each(field("shots"), self.lookup(key=field("actor", scope="item")))
        result = preview(raw, rows, target=frozen_response_structure([""]))
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"], ["同名"])

    def test_duplicate_missing_or_empty_keys_never_select_first_or_fuzzy_match(self):
        for entities in ([{"key": "actor-1", "name": "a"}, {"key": "actor-1", "name": "b"}],
                [{"key": "actor-2", "name": "actor-1"}], [{"key": "", "name": "a"}]):
            raw = {"entities": entities}
            result = preview(raw, obj(result=self.lookup()))
            self.assertFalse(result["valid"], result)
            self.assertIsNone(result["value"])
            self.assertEqual(result["raw_value"], raw)
        source = frozen_response_structure({"entities": [{"key": "x", "name": "a"}]})
        result = preview({"entities": [{"name": "a"}]}, obj(result=self.lookup()), source=source)
        self.assertIn("source.entities[0].key", result["errors"])

    def test_lookup_can_supply_nested_each_without_losing_source_coordinates(self):
        raw = {"entities": [{"key": "actor-1", "variants": [{"name": "校服"}, {"name": "外套"}]}]}
        result = preview(raw, each(self.lookup(value_path=["variants"]), field("name", scope="item")),
            target=frozen_response_structure([""]))
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"], ["校服", "外套"])
        self.assertEqual(result["provenance"][-1]["source_path"], "source.entities[0].variants[1].name")

    def test_unknown_fields_remain_raw_and_cannot_be_selected_until_registered(self):
        source = frozen_response_structure({"known": "a", "rows": [{"flag": False}]})
        raw = {"known": "a", "extra": {"keep": 0}, "rows": [{"flag": False, "new": "raw"}]}
        result = preview(raw, obj(result=field("known")), source=source)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["raw_value"], raw)
        self.assertEqual({row["path"] for row in result["warnings"]}, {"source.extra", "source.rows[0].new"})
        result = preview(raw, obj(result=field("extra")), source=source)
        self.assertFalse(result["valid"])
        self.assertIn("mapping.node.fields.result.path[0]", result["errors"])

    def test_bad_json_preserves_error_position_not_repaired_or_wrapped(self):
        source = frozen_response_structure({"a": "text"})
        target = frozen_response_structure({"result": ""})
        for text in ('{\n"a": }', '```json\n{}\n```', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e400}', ''):
            result = preview({}, obj(result=field("a")), source=source, target=target, text=text)
            self.assertFalse(result["parsed"], result)
            self.assertFalse(result["valid"])
            self.assertIn("source_text", result["errors"])
            self.assertIsNone(result["value"])
        result = preview({}, obj(result=field("a")), source=source, target=target, text='{\n"a": }')
        self.assertIn("第2行第6列", result["errors"]["source_text"][0])

    def test_missing_runtime_path_and_out_of_range_index_are_not_defaulted(self):
        source = frozen_response_structure({"optional": "", "rows": [""]})
        for node in source["nodes"]:
            if node["property_name"] == "optional":
                node["required"] = False
        for path in (field("optional"), field("rows", 3)):
            result = preview({"rows": ["a"]}, obj(result=path), source=source)
            self.assertFalse(result["valid"], result)
            self.assertTrue(any(key in result["errors"] for key in ("source.optional", "source.rows[3]")))
            self.assertIsNone(result["value"])

    def test_mapping_contract_errors_are_located_without_executing_anything(self):
        raw = {"a": "x", "rows": [{"name": "a"}]}
        for node in ({"kind": "eval", "code": "1+1"}, field("a", scope="item"), field("rows", True),
                field("a", "__class__"), {"kind": "object", "fields": []}, obj(unregistered=fixed("x")),
                each(field("a"), fixed("x")), {"kind": []}, {"kind": "literal"},
                obj(result=field("rows")), {"kind": "lookup", "collection": field("rows")}):
            with self.subTest(node=node):
                result = preview(raw, node)
                self.assertFalse(result["valid"], result)
                self.assertTrue(any(path.startswith("mapping") for path in result["errors"]), result)

    def test_default_provenance_is_visible_on_source_and_target_and_does_not_overwrite(self):
        source = frozen_response_structure({"flag": False, "count": 0})
        target = frozen_response_structure({"flag": False, "count": 0, "note": ""})
        for contract, name, value in [(source, "flag", False), (target, "note", "configured")]:
            next(row for row in contract["nodes"] if row["property_name"] == name).update(has_default=True, default_value=value)
        result = preview({"count": 0}, obj(flag=field("flag"), count=field("count")), source=source, target=target)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["raw_value"], {"count": 0})
        self.assertEqual(result["value"], {"flag": False, "count": 0, "note": "configured"})
        self.assertEqual([row["path"] for row in result["defaults"]], ["source.flag", "value.note"])

    def test_catalog_has_exact_field_versions_and_list_item_markers(self):
        snapshot = frozen_response_structure({"a.b": [{"mood": "紧张"}]})
        options = structure_path_options(snapshot)
        self.assertEqual(options[-1]["path"], ["a.b", {"list_item": True}, "mood"])
        self.assertEqual(options[-1]["field_definition"], snapshot["nodes"][-1]["field_definition"])
        result = preview({"a.b": [{"mood": "紧张"}]}, obj(result=field("a.b", 0, "mood")), source=snapshot)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["provenance"][0]["source_path"], 'source["a.b"][0].mood')

    def test_fixed_object_uses_same_target_defaults_and_validation_as_constructed_object(self):
        target = frozen_response_structure({"result": ""})
        target["nodes"][1].update(has_default=True, default_value="visible default")
        for node in (obj(), fixed({})):
            result = preview(False, node, target=target)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["value"], {"result": "visible default"})
            self.assertEqual(result["defaults"][0]["path"], "value.result")
        result = preview(False, fixed({"result": False}), target=target)
        self.assertFalse(result["valid"])
        self.assertEqual(result["value"], {"result": False})
        self.assertIn("value.result", result["errors"])

    def test_invalid_frozen_contracts_are_reported_without_registry_fallback(self):
        target = frozen_response_structure(False)
        for source in (None, {}, {**target, "schema_version": 1}):
            report = preview_structure_mapping(source_contract=source, target_contract=target,
                source_text="false", mapping={"schema_version": 1, "node": field()}, max_bytes=100)
            self.assertFalse(report["valid"])
            self.assertIn("data_structure_version", report["errors"])

    def test_repeated_root_each_is_bounded_before_cartesian_expansion(self):
        from studio.services.json_contracts import JsonExpansionBudget
        raw = {"rows": list(range(100))}
        node, target = fixed(0), 0
        for _ in range(4):
            node, target = each(field("rows"), node), [target]
        consumed = []
        original = JsonExpansionBudget.consume
        def count(budget, value):
            consumed.append(1)
            return original(budget, value)
        with patch.object(JsonExpansionBudget, "consume", count):
            result = preview_structure_mapping(source_contract=frozen_response_structure(raw),
                target_contract=frozen_response_structure(target), source_text=json.dumps(raw),
                mapping={"schema_version": 1, "node": node}, max_bytes=16384)
        self.assertFalse(result["valid"])
        self.assertIn("mapping.preview_budget", result["errors"])
        self.assertLess(len(consumed), 1000)
        self.assertEqual(result["raw_value"], raw)
        self.assertIsNone(result["value"])

    def test_root_null_false_zero_and_list_are_supported_by_same_decoder(self):
        for value in (None, False, 0, [], ""):
            self.assertEqual(parse_json_value_text(json.dumps(value), max_bytes=100), value)
        for text in ("[]", "null", '{"a":1,"a":2}', " " * 101 + "{}"):
            with self.assertRaises(ValidationError):
                parse_json_object_text(text, max_bytes=100)
        source = frozen_response_structure(False)
        for snapshot in (source, {**source, "nodes": [{**source["nodes"][0], "allow_null": True}]}):
            for text in ("false", "null"):
                result = preview(False, field(), source=snapshot, target=snapshot, text=text)
                self.assertEqual(result["valid"], text == "false" or snapshot["nodes"][0]["allow_null"])

    def test_missing_defaults_cannot_expand_source_without_consuming_preview_budget(self):
        source = frozen_response_structure({"rows": [{"text": ""}]})
        source["nodes"][-1].update(has_default=True, default_value="x" * 1000)
        raw = {"rows": [{} for _ in range(100)]}
        result = preview_structure_mapping(source_contract=source, target_contract=frozen_response_structure(False),
            source_text=json.dumps(raw), mapping={"schema_version": 1, "node": fixed(False)}, max_bytes=4096)
        self.assertFalse(result["valid"])
        self.assertIn("mapping.preview_budget", result["errors"])
        self.assertEqual(result["raw_value"], raw)

    def test_compound_default_expansion_respects_existing_configuration_boundary(self):
        source = frozen_response_structure({"rows": [{"text": ""}]})
        source["nodes"][1].update(has_default=True, default_value=[{} for _ in range(100)])
        source["nodes"][-1].update(has_default=True, default_value="x" * 1000)
        result = preview({}, fixed(False), source=source, target=frozen_response_structure(False))
        self.assertFalse(result["valid"])
        self.assertIn("nodes.node_1.default_value", result["errors"])

    def nested(self, value):
        for index in range(20):
            value = {f"level_{index}_" + "x" * 140: value}
        return value

    def test_long_list_paths_are_charged_before_bulk_allocation(self):
        from studio.services.data_structures import _value_path
        source = frozen_response_structure(self.nested([0]))
        raw = self.nested([0] * 100000)
        with patch("studio.services.data_structures._value_path", wraps=_value_path) as paths:
            result = preview_structure_mapping(source_contract=source, target_contract=frozen_response_structure(False),
                source_text=json.dumps(raw), mapping={"schema_version": 1, "node": fixed(False)}, max_bytes=512 * 1024)
        self.assertFalse(result["valid"])
        self.assertIn("mapping.preview_budget", result["errors"])
        self.assertLess(paths.call_count, 5000)

    def test_unknown_warning_paths_use_same_budget_before_append(self):
        from studio.services.data_structures import _value_path
        source = frozen_response_structure(self.nested({}))
        raw = self.nested({f"extra_{index}": 0 for index in range(2000)})
        with patch("studio.services.structure_mapping._value_path", wraps=_value_path) as paths:
            result = preview_structure_mapping(source_contract=source, target_contract=frozen_response_structure(False),
                source_text=json.dumps(raw), mapping={"schema_version": 1, "node": fixed(False)}, max_bytes=128 * 1024)
        self.assertFalse(result["valid"])
        self.assertIn("mapping.preview_budget", result["errors"])
        self.assertLess(paths.call_count, 500)


class StructureMappingApiTests(StructureFixture):
    def test_mapping_options_use_validator_catalog_without_writes(self):
        from studio.services.structure_mapping import MAPPING_NODE_LABELS, MAPPING_SCOPE_LABELS, validate_mapping
        url = "/api/v1/data-structure-versions/mapping-options/"
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["schema_version"], 1)
        self.assertEqual([row["value"] for row in response.data["nodes"]], list(MAPPING_NODE_LABELS))
        self.assertEqual([row["value"] for row in response.data["scopes"]], list(MAPPING_SCOPE_LABELS))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        with self.assertRaises(ValidationError):
            validate_mapping({"schema_version": 1, "node": {"kind": "context_field"}}, {"type": "object"}, {"type": "object"})
        for invalid_scope in ([], {}, None, 1):
            with self.assertRaises(ValidationError):
                validate_mapping({"schema_version": 1, "node": {"kind": "path", "scope": invalid_scope, "path": []}}, {"type": "object"}, {"type": "object"})
        self.client.force_authenticate(get_user_model().objects.create_user(username="options-reader"))
        self.assertEqual(self.client.get(url).status_code, 403)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="mapping-preview", is_staff=True))
        self.source = self.create([self.node("root", "object"),
            self.node("note", "text", "root", property_name="custom_note", required=True)])
        self.target = self.create([self.node("root", "object"),
            self.node("note", "text", "root", property_name="text", required=True)])

    def url(self, version, action):
        return f"/api/v1/data-structure-versions/{version.pk}/{action}/"

    def request(self):
        return {"source_structure_version": str(self.source.pk), "source_text": '{"custom_note":"sample"}',
            "mapping": {"schema_version": 1, "node": obj(text=field("custom_note"))}}

    def test_draft_preview_and_path_directory_have_no_task_or_configuration_writes(self):
        before = (self.source.updated_at, self.target.updated_at)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.url(self.target, "mapping-preview"), self.request(), format="json")
            paths = self.client.get(self.url(self.source, "path-options"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["preview_only"])
        self.assertTrue(response.data["valid"], response.data)
        self.assertEqual(response.data["value"], {"text": "sample"})
        self.assertTrue(paths.data["valid"], paths.data)
        self.assertEqual(paths.data["options"][-1]["path"], ["custom_note"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.source.refresh_from_db(); self.target.refresh_from_db()
        self.assertEqual((self.source.updated_at, self.target.updated_at), before)

    def test_permissions_errors_and_no_latest_version_substitution(self):
        for values in ({"source_text": 123}, {"create_task": True}, {"source_structure_version": "bad-id"}):
            response = self.client.post(self.url(self.target, "mapping-preview"), {**self.request(), **values}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        invalid = self.create([self.node("root", "list")])
        self.assertFalse(self.client.get(self.url(invalid, "path-options")).data["valid"])
        published = self.publish(self.source)
        frozen = data_structure_snapshot(published)
        self.assertEqual(describe_data_structure(published), frozen)
        self.client.force_authenticate(get_user_model().objects.create_user(username="mapping-reader"))
        self.assertEqual(self.client.post(self.url(self.target, "mapping-preview"), self.request(), format="json").status_code, 403)
