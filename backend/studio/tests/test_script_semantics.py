import json
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.data_structures import validate_data_structure_snapshot
from studio.services.json_contracts import json_contract
from studio.services.script_semantics import (
    SEMANTICS_KEY, freeze_script_semantics, preview_script_semantics, script_semantic_options,
    script_semantic_schema, verify_script_semantics,
)
from studio.services.structure_mapping import preview_structure_mapping
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.script_semantics_fixtures import each, literal, obj, path, script_example


LIMIT = 2 * 1024 * 1024


class ScriptSemanticsTests(SimpleTestCase):
    def test_creative_use_description_is_optional_and_never_defaulted(self):
        for value in (None, "", "仅用于说明，保留原文"):
            raw, _, mapping = script_example()
            for episode in raw["episodes"]:
                for shot in episode["shots"]:
                    use = shot["references"][0]
                    if value is None:
                        del use["purpose"]
                    else:
                        use["purpose"] = value
            source = frozen_response_structure(raw)
            result = self.preview(raw, source, mapping)
            self.assertTrue(result["valid"], result)
            use = result["value"]["episodes"][0]["shots"][0]["references"][0]
            self.assertEqual(use.get("purpose"), value)
            self.assertEqual("purpose" in use, value is not None)

    # SimpleTestCase rejects any accidental database access, including current
    # registry lookup or task writes. All input identities here are test data.
    def freeze(self, source, mapping):
        return freeze_script_semantics(source_contract=source, mapping=mapping, max_bytes=LIMIT)

    def preview(self, value, source, mapping):
        return preview_script_semantics(configuration=self.freeze(source, mapping),
            response_text=json.dumps(value, ensure_ascii=False), max_bytes=LIMIT)

    def test_two_layouts_two_episodes_two_variants_preserve_custom_fields(self):
        results = []
        for layout in ("a", "b"):
            raw, source, mapping = script_example(layout)
            before = deepcopy((raw, source, mapping))
            result = self.preview(raw, source, mapping)
            self.assertTrue(result["valid"], result["errors"])
            self.assertTrue(result["preview_only"])
            self.assertEqual(result["validation_scope"], "structure_and_mapping")
            self.assertEqual((raw, source, mapping), before)
            self.assertEqual(result["raw_value"], raw)
            self.assertEqual(result["source_value"], raw)
            self.assertEqual(result["warnings"], [])
            episodes_key, shots_key, custom_key = ("episodes", "shots", "mood_note") if layout == "a" else ("chapters", "frames", "tone")
            self.assertEqual(result["source_value"][episodes_key][0][shots_key][0][custom_key], "紧张")
            self.assertTrue(any(row["path"] and row["path"][-1] == custom_key for row in result["source_fields"]))
            self.assertEqual(len(result["value"]["entities"][0]["variants"]), 2)
            for episode in result["value"]["episodes"]:
                self.assertEqual([s["references"][0]["variant_key"] for s in episode["shots"]], ["uniform", "coat"])
            self.assertIs(result["value"]["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"], False)
            results.append(result["value"])
        self.assertEqual(results[0], results[1])

    def test_provenance_is_exact_and_custom_creative_field_can_feed_other_mapping(self):
        raw, source, mapping = script_example("b")
        result = self.preview(raw, source, mapping)
        origin = next(row for row in result["provenance"] if row["path"] == "value.episodes[1].shots[1].action")
        self.assertEqual(origin["source_path"], "source.chapters[1].frames[1].motion")
        self.assertEqual(origin["source_structure_version_id"], source["id"])
        self.assertEqual(origin["semantic_contract"], SEMANTICS_KEY)
        self.assertEqual(origin["configuration_sha256"], result["configuration_sha256"])
        downstream = preview_structure_mapping(source_contract=source,
            target_contract=frozen_response_structure({"prompt_note": ""}),
            source_text=json.dumps(result["source_value"]), mapping={"schema_version": 1,
                "node": obj(prompt_note=path("chapters", 1, "frames", 0, "tone", scope="root"))}, max_bytes=LIMIT)
        self.assertTrue(downstream["valid"], downstream)
        self.assertEqual(downstream["value"], {"prompt_note": "紧张"})

    def test_unknown_input_remains_raw_but_is_not_implicitly_a_business_field(self):
        raw, source, mapping = script_example()
        raw["unregistered"] = {"new_note": "原文不丢"}
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["raw_value"]["unregistered"], raw["unregistered"])
        self.assertEqual(result["source_value"]["unregistered"], raw["unregistered"])
        self.assertEqual(result["warnings"][0]["path"], "source.unregistered")
        self.assertNotIn("unregistered", result["value"])
        mapping["node"]["fields"]["hidden_action"] = literal("do not execute")
        with self.assertRaises(ValidationError):
            self.freeze(source, mapping)

    def test_frozen_configuration_detects_source_and_mapping_drift_and_is_a_copy(self):
        _raw, source, mapping = script_example()
        frozen = self.freeze(source, mapping)
        source["name"] = "later name"
        mapping["node"]["fields"]["entities"] = literal([])
        self.assertEqual(verify_script_semantics(frozen, max_bytes=LIMIT), frozen)
        for key, value in (("mapping", mapping), ("source_structure", source), ("key", "script_semantics.v999")):
            changed = {**frozen, key: value}
            with self.subTest(key=key), self.assertRaises(ValidationError):
                verify_script_semantics(changed, max_bytes=LIMIT)
        changed = deepcopy(frozen)
        changed["mapping"]["node"] = {"kind": "eval", "code": "unsafe()"}
        changed["sha256"] = json_contract({key: value for key, value in changed.items() if key != "sha256"}, max_bytes=LIMIT).sha256
        with self.assertRaises(ValidationError):
            verify_script_semantics(changed, max_bytes=LIMIT)

    def test_declared_default_is_used_only_for_missing_false_and_zero_are_retained(self):
        raw, source, mapping = script_example()
        flag = next(row for row in source["nodes"] if row["property_name"] == "is_voiceover")
        flag.update(has_default=True, default_value=False)
        del raw["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"]
        raw["episodes"][0]["shots"][1]["dialogue"]["is_voiceover"] = True
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(len(result["defaults"]), 1)
        self.assertEqual(result["defaults"][0]["path"], "source.episodes[0].shots[0].dialogue.is_voiceover")
        self.assertNotIn("is_voiceover", result["raw_value"]["episodes"][0]["shots"][0]["dialogue"])
        self.assertIs(result["value"]["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"], False)
        self.assertIs(result["value"]["episodes"][0]["shots"][1]["dialogue"]["is_voiceover"], True)
        self.assertEqual(result["value"]["episodes"][0]["shots"][0]["dialogue"]["start_ms"], 0)

    def test_no_implicit_voiceover_or_optional_creative_content(self):
        raw = {"cast": [{"id": "p", "name": "名", "kind": "character"}],
            "chapters": [{"number": 1, "frames": [{"number": 1}]}]}
        mapping = {"schema_version": 1, "node": obj(entities=each(path("cast", scope="root"),
            obj(key=path("id"), kind=path("kind"), name=path("name"))), episodes=each(path("chapters", scope="root"),
                obj(episode=path("number"), shots=each(path("frames"), obj(shot=path("number"))))))}
        result = self.preview(raw, frozen_response_structure(raw), mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"]["episodes"][0]["shots"], [{"shot": 1}])
        self.assertNotIn("variants", result["value"]["entities"][0])
        self.assertEqual(result["defaults"], [])

    def test_shape_failure_keeps_actual_invalid_semantics_not_fake_success(self):
        raw, source, mapping = script_example()
        for value in (0, -1, False, "1"):
            modified = deepcopy(mapping)
            modified["node"]["fields"]["episodes"]["item"]["fields"]["episode"] = literal(value)
            result = self.preview(raw, source, modified)
            self.assertFalse(result["valid"], result)
            self.assertIn("value.episodes[0].episode", result["errors"])
            self.assertEqual(result["raw_value"], raw)
            self.assertEqual(result["value"]["episodes"][0]["episode"], value)

    def test_missing_field_and_invalid_source_type_do_not_fall_back(self):
        raw, source, mapping = script_example()
        for mutate, expected_path in ((lambda row: row.pop("action"), "source.episodes[0].shots[0].action"),
                (lambda row: row.update(duration_ms="5000"), "source.episodes[0].shots[0].duration_ms")):
            changed = deepcopy(raw)
            mutate(changed["episodes"][0]["shots"][0])
            result = self.preview(changed, source, mapping)
            self.assertFalse(result["valid"])
            self.assertIn(expected_path, result["errors"])
            self.assertIsNone(result["value"])
            self.assertEqual(result["raw_value"], changed)

    def test_null_and_empty_variant_list_are_not_defaulted_or_replaced(self):
        raw, source, mapping = script_example()
        node = next(row for row in source["nodes"] if row["property_name"] == "variant_key")
        node["allow_null"] = True
        raw["entities"][0]["variants"] = []
        for episode in raw["episodes"]:
            for shot in episode["shots"]:
                shot["references"][0]["variant_key"] = None
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"]["entities"][0]["variants"], [])
        self.assertIsNone(result["value"]["episodes"][0]["shots"][0]["references"][0]["variant_key"])
        self.assertEqual(result["defaults"], [])

    def test_invalid_json_is_not_ai_repaired_or_old_marker_parsed(self):
        _raw, source, mapping = script_example()
        configuration = self.freeze(source, mapping)
        for text in ('{\n"entities": }', '{"entities": [], "entities": []}', '```json\n{}\n```', '【SCRIPT_BIBLE_V2】\n{}'):
            result = preview_script_semantics(configuration=configuration, response_text=text, max_bytes=LIMIT)
            self.assertFalse(result["parsed"])
            self.assertFalse(result["valid"])
            self.assertIn("response_text", result["errors"])
            self.assertIsNone(result["value"])

    def test_catalog_has_no_people_or_runtime_defaults_and_returns_new_objects(self):
        options = script_semantic_options()
        self.assertEqual(options["key"], SEMANTICS_KEY)
        self.assertEqual(options["json_schema"], script_semantic_schema())
        encoded = json.dumps(options, ensure_ascii=False)
        for text in ("测试人物", "校服", '"default"', '"maxLength"'):
            self.assertNotIn(text, encoded)
        options["json_schema"]["properties"].clear()
        self.assertEqual(set(script_semantic_schema()["properties"]), {"entities", "episodes"})

    def test_configuration_budget_is_explicit_and_no_default_inflation(self):
        _raw, source, mapping = script_example()
        with self.assertRaises(ValidationError):
            freeze_script_semantics(source_contract=source, mapping=mapping, max_bytes=100)
        self.assertEqual(validate_data_structure_snapshot(source)["type"], "object")

    def test_typed_mapping_rejects_wrong_paths_scopes_and_implicit_conversions(self):
        _raw, source, mapping = script_example()
        for value in (path("unknown", scope="root"), path("entities", scope="wrong"),
                path("episodes", scope="item"), path("entities", 0, "name", scope="root")):
            changed = deepcopy(mapping)
            changed["node"]["fields"]["entities"] = value
            with self.subTest(node=value), self.assertRaises(ValidationError):
                self.freeze(source, changed)

    def test_empty_collections_and_empty_text_are_not_manufactured_into_content(self):
        raw, source, mapping = script_example()
        raw["entities"] = []
        raw["episodes"] = []
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"], {"entities": [], "episodes": []})
        raw, source, mapping = script_example()
        raw["episodes"][0]["shots"][0]["dialogue"]["text"] = ""
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["value"]["episodes"][0]["shots"][0]["dialogue"]["text"], "")
        self.assertEqual(result["defaults"], [])

    def test_domain_shape_does_not_pretend_cross_record_links_were_accepted(self):
        raw, source, mapping = script_example()
        raw["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "undeclared-person"
        result = self.preview(raw, source, mapping)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["validation_scope"], "structure_and_mapping")
        self.assertEqual(result["value"]["episodes"][0]["shots"][0]["references"][0]["entity_key"], "undeclared-person")
        self.assertNotIn("approved", result)
        self.assertNotIn("materialized", result)

    def test_minimal_semantic_shape_cannot_be_replaced_with_unknown_top_level_data(self):
        raw, source, mapping = script_example()
        mapping["node"] = literal({"entities": [], "unknown": "not an episode"})
        result = self.preview(raw, source, mapping)
        self.assertFalse(result["valid"], result)
        self.assertIn("value.episodes", result["errors"])
        self.assertIn("value", result["errors"])
        self.assertEqual(result["value"], {"entities": [], "unknown": "not an episode"})
