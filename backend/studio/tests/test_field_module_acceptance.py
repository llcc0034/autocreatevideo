"""M1-A01/A02/A03: author through public APIs, then preview the saved contracts.

All definitions and structures belong to Django's isolated test database. No
business task, provider, current configuration, or browser mock is used here.
"""
import json
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.tests.test_structure_mapping_preview import each, field, obj


class FieldModuleAcceptanceTests(TestCase):
    root = "/api/v1/data-structure-versions/"

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(
            username="field-module-acceptance", is_staff=True,
        ))
        self.definitions = {}

    def post(self, url, body, status=200):
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, status, response.data)
        return response.data

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def definition(self, key, kind):
        if key not in self.definitions:
            identity = self.post("/api/v1/field-definitions/", {"key": key}, 201)
            version = self.post("/api/v1/field-definition-versions/", {
                "field_definition": identity["id"], "label": key,
                "description": "隔离验收自定义字段", "category": "acceptance",
                "value_type": kind,
            }, 201)
            self.post(f"/api/v1/field-definition-versions/{version['id']}/publish/", {})
            self.definitions[key] = version
        return self.definitions[key]

    def node(self, key, kind="object", parent=None, *, identity=None, **values):
        definition = self.definition(identity or f"acceptance_{kind}", kind)
        return {"node_key": key, "parent_key": parent,
            "field_definition_version": definition["id"], **values}

    def structure(self, nodes, **values):
        row = self.post(self.root, {"name": "M1隔离验收", "nodes": nodes, **values}, 201)
        self.post(f"{self.root}{row['id']}/publish/", {})
        return row

    def schema(self, row):
        result = self.get(f"{self.root}{row['id']}/schema-preview/")
        self.assertTrue(result["valid"], result)
        return result["json_schema"]

    def preview(self, source, target, raw, node):
        original = deepcopy(raw)
        with CaptureQueriesContext(connection) as queries:
            result = self.post(f"{self.root}{target['id']}/mapping-preview/", {
                "source_structure_version": source["id"],
                "source_text": json.dumps(raw, ensure_ascii=False),
                "mapping": {"schema_version": 1, "node": node},
            })
        self.assert_no_writes(queries)
        self.assertTrue(result["preview_only"])
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["raw_value"], original)
        self.assertEqual(raw, original)
        self.assertEqual(result["source_structure_version_id"], str(source["id"]))
        self.assertEqual(result["target_structure_version_id"], str(target["id"]))
        return result

    def assert_no_writes(self, queries):
        writes = [row["sql"] for row in queries
            if row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])

    def layout(self, episodes, shots, action, mood):
        return self.structure([
            self.node("root"), self.node("episodes", "list", "root", property_name=episodes),
            self.node("episode", parent="episodes"),
            self.node("shots", "list", "episode", property_name=shots),
            self.node("shot", parent="shots"),
            self.node("action", "text", "shot", identity="creative_action", property_name=action, required=True),
            self.node("mood", "text", "shot", identity="creative_mood", property_name=mood, required=True),
            self.node("off_screen", "boolean", "shot", property_name="off_screen", required=True),
        ])

    def test_a01_two_api_authored_layouts_share_identity_and_map_nested_rows(self):
        target = self.layout("episodes", "shots", "action", "mood")
        expected = {"episodes": [{"shots": [
            {"action": "转身", "mood": "紧张", "off_screen": False},
            {"action": "停步", "mood": "平静", "off_screen": True},
        ]}, {"shots": []}]}
        target_schema = self.schema(target)
        for episodes, shots, action, mood in (
            ("episodes", "shots", "action", "mood_note"),
            ("chapters", "frames", "motion", "tone"),
        ):
            with self.subTest(layout=episodes):
                source = self.layout(episodes, shots, action, mood)
                raw = {episodes: [{shots: [{action: row["action"], mood: row["mood"],
                    "off_screen": row["off_screen"]} for row in expected["episodes"][0]["shots"]]}, {shots: []}]}
                source_schema = self.schema(source)
                source_action = source_schema["properties"][episodes]["items"]["properties"][shots]["items"]["properties"][action]
                target_action = target_schema["properties"]["episodes"]["items"]["properties"]["shots"]["items"]["properties"]["action"]
                self.assertEqual(source_action["x-field-definition"], target_action["x-field-definition"])
                result = self.preview(source, target, raw, obj(episodes=each(field(episodes), obj(
                    shots=each(field(shots, scope="item"), obj(action=field(action, scope="item"),
                        mood=field(mood, scope="item"), off_screen=field("off_screen", scope="item")))))))
                self.assertEqual(result["value"], expected)
                origin = next(row for row in result["provenance"]
                    if row["path"] == "value.episodes[0].shots[1].action")
                self.assertEqual(origin["source_path"], f"source.{episodes}[0].{shots}[1].{action}")

    def test_a02_new_non_script_field_reaches_schema_paths_and_downstream_without_code_branch(self):
        source = self.structure([self.node("root"), self.node("reading", "decimal", "root",
            identity="inspection.reading", property_name="reading", required=True)])
        first_schema = self.schema(source)
        accepted = self.node("accepted", "boolean", "root", identity="inspection.accepted",
            property_name="accepted", required=True)
        successor = self.structure([*source["nodes"], accepted], source_version=source["id"])
        self.assertEqual(successor["version"], source["version"] + 1)
        self.assertEqual(self.schema(source), first_schema)
        self.assertNotIn("accepted", first_schema["properties"])
        schema = self.schema(successor)
        self.assertEqual(schema["properties"]["accepted"]["type"], "boolean")
        self.assertEqual(schema["properties"]["reading"]["type"], "number")
        options = self.get(f"{self.root}{successor['id']}/path-options/")
        self.assertTrue(options["valid"], options)
        self.assertIn(["accepted"], [row["path"] for row in options["options"]])
        target = self.structure([self.node("root"), self.node("result", parent="root", property_name="result"),
            self.node("reading", "decimal", "result", identity="inspection.reading", property_name="reading"),
            self.node("accepted", "boolean", "result", identity="inspection.accepted", property_name="accepted")])
        raw = {"reading": 0, "accepted": False, "unregistered_note": "原文保留，不猜测用途"}
        result = self.preview(successor, target, raw, obj(result=obj(reading=field("reading"), accepted=field("accepted"))))
        self.assertEqual(result["value"], {"result": {"reading": 0, "accepted": False}})
        self.assertEqual(result["defaults"], [])
        self.assertTrue(any(row["path"] == "source.unregistered_note" for row in result["warnings"]))

    def test_a03_saved_node_rules_distinguish_missing_null_empty_zero_and_false(self):
        cases = [("text", None, {"allow_null": True}, "configured"),
            ("text", "", {"allow_blank": True}, "configured"),
            ("list", [], {"allow_empty": True}, ["configured"]),
            ("integer", 0, {}, 9), ("boolean", False, {}, True)]
        for kind, supplied, options, default in cases:
            with self.subTest(kind=kind, value=supplied):
                nodes = [self.node("root"), self.node("entry", kind, "root", property_name="entry",
                    required=True, has_default=True, default_value=default, **options)]
                if kind == "list":
                    nodes.append(self.node("item", "text", "entry"))
                version = self.structure(nodes)
                url = f"{self.root}{version['id']}/value-preview/"
                with CaptureQueriesContext(connection) as queries:
                    present = self.post(url, {"value": {"entry": supplied}})
                    missing = self.post(url, {"value": {}})
                self.assert_no_writes(queries)
                self.assertTrue(present["valid"], present)
                self.assertEqual(present["value"], {"entry": supplied})
                self.assertEqual(present["defaults"], [])
                self.assertTrue(missing["valid"], missing)
                self.assertEqual(missing["value"], {"entry": default})
                self.assertEqual(missing["defaults"][0]["source"], "configured_default")
                self.assertEqual(missing["defaults"][0]["structure_version_id"], str(version["id"]))
        for required in (False, True):
            version = self.structure([self.node("root"), self.node("entry", "text", "root",
                property_name="entry", required=required, allow_null=False, allow_blank=False)])
            url = f"{self.root}{version['id']}/value-preview/"
            missing = self.post(url, {"value": {}})
            self.assertEqual(missing["valid"], not required)
            self.assertEqual(missing["value"], {})
            for supplied in (None, ""):
                result = self.post(url, {"value": {"entry": supplied}})
                self.assertFalse(result["valid"])
                self.assertIn("value.entry", result["errors"])
                self.assertEqual(result["value"], {"entry": supplied})
                self.assertEqual(result["defaults"], [])
