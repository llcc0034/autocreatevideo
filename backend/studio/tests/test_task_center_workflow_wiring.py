from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.task_center_workflow_wiring import scalar_port_rows, graph_node_rows
from studio.task_center_media_routes import media_port_routes


class ScalarPortRowsTests(SimpleTestCase):
    """Pure isolated projection fixtures, not real execution evidence."""
    def fixture(self, kind="string", value="original prompt"):
        snapshot = {"graph": {"n": {"class_type": "Encode", "inputs": {"text": "template"}}},
            "semantic_bindings": [{"semantic_key": "prompt.positive", "node_id": "n", "input_name": "text",
                "value_type": kind, "is_required": True}]}
        compiled = {"graph": {"n": {"class_type": "Encode", "inputs": {"text": value}}},
            "resolved_semantics": {"prompt.positive": value}}
        return snapshot, compiled

    def test_explicit_one_to_many_bindings_and_no_value_duplication(self):
        snapshot, compiled = self.fixture()
        snapshot["graph"]["second"] = deepcopy(snapshot["graph"]["n"])
        compiled["graph"]["second"] = deepcopy(compiled["graph"]["n"])
        snapshot["semantic_bindings"].append({**snapshot["semantic_bindings"][0], "node_id": "second"})
        before = deepcopy((snapshot, compiled))
        rows = scalar_port_rows(snapshot, compiled)
        self.assertEqual([r["state"] for r in rows], ["matched", "matched"])
        self.assertNotIn("original prompt", str(rows))
        self.assertEqual((snapshot, compiled), before)

    def test_scalar_types_false_zero_empty_and_mismatches(self):
        for kind, value in (("string", ""), ("enum", "euler"), ("integer", 0), ("number", 1.5), ("number", 10**400), ("boolean", False)):
            with self.subTest(kind=kind):
                snapshot, compiled = self.fixture(kind, value)
                self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "matched")
                for invalid in (None, {}, ["n", 0]):
                    compiled["graph"]["n"]["inputs"]["text"] = invalid
                    self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "mismatch")
        for kind, actual, expected in (("integer", True, 1), ("boolean", 0, False), ("number", True, 1), ("number", float("inf"), float("inf"))):
            snapshot, compiled = self.fixture(kind, expected)
            compiled["graph"]["n"]["inputs"]["text"] = actual
            self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "mismatch")

    def test_optional_omission_is_not_default_or_required_success(self):
        snapshot, compiled = self.fixture()
        compiled["resolved_semantics"] = {}
        compiled["graph"]["n"]["inputs"] = {}
        self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "mismatch")
        snapshot["semantic_bindings"][0]["is_required"] = False
        self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "omitted")
        compiled["graph"]["n"]["inputs"]["text"] = "template"
        self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "mismatch")

    def test_missing_or_changed_node_never_searches_same_text_elsewhere(self):
        snapshot, compiled = self.fixture()
        compiled["graph"]["clone"] = compiled["graph"].pop("n")
        row = scalar_port_rows(snapshot, compiled)[0]
        self.assertEqual(row["state"], "unavailable")
        self.assertEqual(row["node_id"], "n")
        compiled["graph"]["n"] = {"class_type": "Other", "inputs": {"text": "original prompt"}}
        self.assertEqual(scalar_port_rows(snapshot, compiled)[0]["state"], "unavailable")

    def test_file_bindings_and_internal_output_prefix_are_excluded(self):
        snapshot, compiled = self.fixture()
        snapshot["semantic_bindings"][0]["value_type"] = "asset"
        self.assertEqual(scalar_port_rows(snapshot, compiled), [])
        snapshot["semantic_bindings"][0].update(value_type="string", semantic_key="output.prefix")
        self.assertEqual(scalar_port_rows(snapshot, compiled), [])


class GraphNodeRowsTests(SimpleTestCase):
    def setUp(self):
        # Isolate the configured usage-count boundary, not the real redaction.
        boundary = patch("studio.services.security_boundaries.boundary_resolution",
            return_value=SimpleNamespace(values={"security.prompt.max_usage_count": 1000000}))
        self.boundary = boundary.start()
        self.addCleanup(boundary.stop)

    def test_exact_baseline_added_removed_and_typed_values_without_mutation(self):
        snapshot = {"graph": {"resize": {"class_type": "Resize", "inputs": {
            "width": 640, "height": 0, "enabled": 0, "options": {"crop": False}, "removed": "private previous value"}}}}
        graph = {"loader": {"class_type": "LoadImage", "inputs": {"mode": "center"}},
            "resize": {"class_type": "Resize", "inputs": {"width": 352, "height": 0, "enabled": False,
                "options": {"crop": False}, "image": ["loader", 0], "empty": "", "null": None}},
            "unreachable": {"class_type": "Other", "inputs": {"secret": "must not appear"}}}
        before = deepcopy((graph, snapshot))
        rows = graph_node_rows(graph, snapshot, {"resize", "loader"})
        self.boundary.assert_called_once()
        self.assertEqual([row["origin"] for row in rows], ["added", "original"])
        inputs = {row["input_name"]: row for row in rows[1]["inputs"]}
        self.assertEqual(inputs["width"]["change"], "changed")
        self.assertEqual(inputs["height"]["change"], "unchanged")
        self.assertEqual(inputs["enabled"]["change"], "changed")
        self.assertIs(inputs["enabled"]["value"], False)
        self.assertEqual(inputs["options"]["change"], "unchanged")
        self.assertEqual(inputs["image"]["kind"], "connection")
        self.assertEqual(inputs["empty"]["value"], "")
        self.assertIsNone(inputs["null"]["value"])
        self.assertEqual(rows[1]["removed_inputs"], ["removed"])
        self.assertNotIn("private previous value", str(rows))
        self.assertNotIn("must not appear", str(rows))
        self.assertEqual((graph, snapshot), before)

    def test_missing_or_replaced_baseline_is_not_unchanged_or_name_matched(self):
        graph = {"n": {"class_type": "New", "inputs": {"width": 10}}}
        for snapshot, expected in ((None, "unavailable"),
            ({"graph": {"n": {"class_type": "Old", "inputs": {"width": 10}}}}, "replaced")):
            row = graph_node_rows(graph, snapshot, {"n"})[0]
            self.assertEqual(row["origin"], expected)
            self.assertEqual(row["inputs"][0]["change"], "unavailable")
            self.assertEqual(row["removed_inputs"], [])
        row = graph_node_rows(graph, {"graph": {"other": graph["n"]}}, {"n"})[0]
        self.assertEqual(row["origin"], "added")

    def test_secrets_paths_and_media_tokens_hidden_in_raw_response(self):
        from studio.services.workflow_media_tokens import MEDIA_USE_TOKEN_PREFIX
        token = MEDIA_USE_TOKEN_PREFIX + "a" * 64
        values = {"api_key": "secret-value", "filename": "private.png", "filename_prefix": "autoprompt/private",
            "arbitrary": "https://internal.test/path",
            "nested": {"file": "private.dat"}, "items": ["safe", {"uri": "private://data"}],
            "path_in_text": "C:/private/file.png", "bearer": "Bearer sensitive-value", "image": token,
            "nested_media": {"items": [token]}, "width": 512}
        row = graph_node_rows({"n": {"class_type": "Test", "inputs": values}}, {"graph": {}}, {"n"})[0]
        inputs = {item["input_name"]: item for item in row["inputs"]}
        for name in values.keys() - {"width"}:
            self.assertIsNone(inputs[name]["value"], name)
            self.assertIn(inputs[name]["kind"], {"redacted", "media"})
        self.assertEqual(inputs["width"]["value"], 512)


class MediaPortRoutesTests(SimpleTestCase):
    def edge(self, source, destination, name="image", index=0):
        return {"from_node": source, "output_index": index, "to_node": destination, "input_name": name}

    def route(self, edges, *, port=None, snapshot=None, locations=None):
        return media_port_routes([{"target_key": "references", "port": port or {
            "kind": "node_input", "node_id": "target", "input_name": "image"}}],
            locations or [{"node_id": "loader", "input_path": ["file"]}], edges, snapshot)

    def test_shortest_witness_keeps_output_and_input_ports_without_mutation(self):
        edges = [self.edge("loader", "split", "video"), self.edge("split", "target", index=1),
                 self.edge("split", "unused", index=0), self.edge("unused", "target", "other")]
        before = deepcopy(edges)
        row = self.route(edges)[0]
        self.assertEqual(row["state"], "connected")
        self.assertEqual(row["edge_indexes"], [0, 1])
        self.assertEqual(row["entry_node_id"], "loader")
        self.assertIn("不是全部路径", row["message"])
        self.assertEqual(edges, before)

    def test_wrong_port_disconnected_and_cycles_are_bounded(self):
        edges = [self.edge("loader", "other"), self.edge("other", "loader"),
                 self.edge("other", "target", "wrong"), self.edge("target", "loop"), self.edge("loop", "target")]
        row = self.route(edges)[0]
        self.assertEqual(row["state"], "disconnected")
        self.assertIsNone(row["entry_node_id"])
        self.assertEqual(row["edge_indexes"], [])

    def test_multiple_paths_do_not_enumerate_combinatorial_routes(self):
        edges, previous = [], ["loader"]
        for index in range(30):
            current = [f"a{index}", f"b{index}"]
            edges.extend(self.edge(source, target) for source in previous for target in current)
            previous = current
        edges.extend(self.edge(source, "target") for source in previous)
        row = self.route(edges)[0]
        self.assertEqual(row["state"], "connected")
        self.assertEqual(len(row["edge_indexes"]), 31)

    def test_frozen_group_ordered_and_reduce_ports_not_clone_name_guesses(self):
        group = {"semantic_keys": ["asset.one", "asset.two"], "target_nodes": ["target", "second"],
                 "input_names": ["image", "image2"], "wiring": "ordered_ports"}
        snapshot = {"input_contract": {"asset_groups": [group]}, "semantic_bindings": []}
        port = {"kind": "semantic", "semantic_key": "asset.two"}
        edges = [self.edge("loader", "target", "image2"), self.edge("loader", "second", "image2")]
        rows = self.route(edges, port=port, snapshot=snapshot)
        self.assertEqual([r["state"] for r in rows], ["connected", "connected"])
        self.assertEqual([r["destination"]["input_name"] for r in rows], ["image2", "image2"])
        group.update(wiring="reduce", input_names=["image"])
        edges = [self.edge("loader", "arbitrary-merge-id", "b"), self.edge("arbitrary-merge-id", "target")]
        rows = self.route(edges, port=port, snapshot=snapshot)
        self.assertEqual([r["state"] for r in rows], ["connected", "disconnected"])
        self.assertEqual(rows[0]["edge_indexes"], [0, 1])

    def test_direct_semantic_requires_exact_widget_and_missing_contract_unavailable(self):
        snapshot = {"input_contract": {}, "semantic_bindings": [{"semantic_key": "asset.one", "value_type": "asset_image",
            "node_id": "loader", "input_name": "file"}]}
        port = {"kind": "semantic", "semantic_key": "asset.one"}
        row = self.route([], port=port, snapshot=snapshot)[0]
        self.assertEqual(row["state"], "connected")
        self.assertEqual(row["edge_indexes"], [])
        snapshot["semantic_bindings"][0]["input_name"] = "other"
        self.assertEqual(self.route([], port=port, snapshot=snapshot)[0]["state"], "disconnected")
        self.assertEqual(self.route([], port=port)[0]["state"], "unavailable")
        self.assertEqual(media_port_routes([], [], [], None), [])
