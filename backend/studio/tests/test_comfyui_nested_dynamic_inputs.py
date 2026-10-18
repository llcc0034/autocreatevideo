from copy import deepcopy
import os
from unittest.mock import patch

from django.test import TestCase

from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    ComfyUICanvasImportError,
    api_graph_to_ui_workflow,
    ui_workflow_to_api_graph,
)
from studio.services.comfyui_contract import ComfyUIContractError, validate_comfyui_api_contract
from studio.services.comfyui_schema import dynamic_widget_definitions
from studio.services.comfyui_workflow_imports import _node_input_definition, _definition_options


def dynamic(options):
    return ["COMFY_DYNAMICCOMBO_V3", {"options": options}]


class NestedDynamicInputsTests(TestCase):
    def setUp(self):
        allowed = patch.dict(os.environ, {"COMFYUI_EXTRA_ALLOWED_NODE_TYPES": "NestedWriter"})
        allowed.start()
        self.addCleanup(allowed.stop)
        self.info = {"NestedWriter": {
            "input": {"required": {
                "format": dynamic([
                    {"key": "mp4", "inputs": {"required": {
                        "codec": dynamic([
                            {"key": "h264", "inputs": {"optional": {
                                "encoding": dynamic([
                                    {"key": "auto", "inputs": {}},
                                    {"key": "re-encode", "inputs": {"required": {
                                        "crf": ["INT", {"min": 0, "max": 51}],
                                    }}},
                                ]),
                            }}},
                            {"key": "auto", "inputs": {}},
                        ]),
                    }}},
                    {"key": "raw", "inputs": {}},
                ]),
                "suffix": ["STRING", {}],
            }},
            "output": [], "output_node": True,
        }}
        self.inputs = {
            "format": "mp4", "format.codec": "h264",
            "format.codec.encoding": "re-encode",
            "format.codec.encoding.crf": 20, "suffix": "done",
        }

    def graph(self, values=None):
        return {"1": {"class_type": "NestedWriter", "inputs": deepcopy(self.inputs if values is None else values)}}

    def test_contract_accepts_three_nested_levels(self):
        validate_comfyui_api_contract(self.graph(), self.info)

    def test_required_descendant_missing_is_not_filled_from_defaults(self):
        values = dict(self.inputs)
        del values["format.codec.encoding.crf"]
        with self.assertRaisesRegex(ComfyUIContractError, "format.codec.encoding.crf"):
            validate_comfyui_api_contract(self.graph(values), self.info)
        with self.assertRaisesRegex(ComfyUICanvasExportError, "required dynamic"):
            api_graph_to_ui_workflow(self.graph(values), self.info)

    def test_nested_enum_and_numeric_constraints_remain_enforced(self):
        for key, value in (("format.codec", "unknown"), ("format.codec.encoding", "other"),
                           ("format.codec.encoding.crf", 52), ("format.codec.encoding.crf", "20")):
            with self.subTest(key=key, value=value), self.assertRaises(ComfyUIContractError):
                validate_comfyui_api_contract(self.graph({**self.inputs, key: value}), self.info)

    def test_inactive_descendants_are_rejected(self):
        with self.assertRaises(ComfyUIContractError):
            validate_comfyui_api_contract(self.graph({**self.inputs, "format.codec": "auto"}), self.info)

    def test_absent_optional_branch_is_not_required(self):
        values = {"format": "mp4", "format.codec": "h264", "suffix": "done"}
        validate_comfyui_api_contract(self.graph(values), self.info)
        # Positional canvases cannot encode a hole before the next widget.
        # Reject export rather than manufacture a value or shift the suffix.
        with self.assertRaisesRegex(ComfyUICanvasExportError, "explicit widget values"):
            api_graph_to_ui_workflow(self.graph(values), self.info)

    def test_hidden_legacy_optional_widget_is_not_invented_by_export(self):
        self.info["NestedWriter"]["input"]["optional"] = {
            "legacy_codec": ["COMFY_DYNAMICCOMBO_V3", {"hidden": True, "options": [{"key": "auto", "inputs": {}}]}],
        }
        canvas = api_graph_to_ui_workflow(self.graph(), self.info)
        converted = ui_workflow_to_api_graph(canvas, self.info, primary_output_node_ids=[1])
        self.assertEqual(converted.api_graph, self.graph())

    def test_keyed_canvas_rejects_inactive_branch_and_bounded_overflow(self):
        canvas = api_graph_to_ui_workflow(self.graph(), self.info)
        canvas["nodes"][0]["inputs"] = []
        canvas["nodes"][0]["widgets_values"] = {**self.inputs, "format": "raw"}
        with self.assertRaisesRegex(ComfyUICanvasImportError, "unknown keyed"):
            ui_workflow_to_api_graph(canvas, self.info, primary_output_node_ids=[1])
        canvas["nodes"][0]["widgets_values"] = dict(self.inputs)
        with patch("studio.services.comfyui_schema._dynamic_widgets_max_items", return_value=2):
            with self.assertRaisesRegex(ComfyUICanvasImportError, "configured limit"):
                ui_workflow_to_api_graph(canvas, self.info, primary_output_node_ids=[1])

    def test_keyed_and_positional_canvas_round_trip(self):
        for keyed in (False, True):
            with self.subTest(keyed=keyed):
                canvas = api_graph_to_ui_workflow(self.graph(), self.info)
                if keyed:
                    canvas["nodes"][0]["widgets_values"] = dict(self.inputs)
                else:
                    # Upstream canvases may omit all widget-backed ports.
                    canvas["nodes"][0]["inputs"] = []
                    self.assertEqual(canvas["nodes"][0]["widgets_values"],
                                     ["mp4", "h264", "re-encode", 20, "done"])
                converted = ui_workflow_to_api_graph(canvas, self.info, primary_output_node_ids=[1])
                self.assertEqual(converted.api_graph, self.graph())
                validate_comfyui_api_contract(converted.api_graph, self.info)

    def test_import_classification_resolves_nested_enum_and_number(self):
        node = self.graph()["1"]
        self.assertEqual(_definition_options(_node_input_definition(self.info, node, "format.codec")),
                         ("h264", "auto"))
        self.assertEqual(_node_input_definition(self.info, node, "format.codec.encoding.crf"),
                         ["INT", {"min": 0, "max": 51}])
        node["inputs"]["format"] = "raw"
        self.assertIsNone(_node_input_definition(self.info, node, "format.codec"))

    def test_one_configured_bound_covers_all_nested_levels(self):
        with patch("studio.services.comfyui_schema._dynamic_widgets_max_items", return_value=2):
            with self.assertRaisesRegex(ComfyUIContractError, "configured limit"):
                validate_comfyui_api_contract(self.graph(), self.info)
            with self.assertRaisesRegex(ComfyUICanvasExportError, "configured limit"):
                api_graph_to_ui_workflow(self.graph(), self.info)

    def test_nested_name_collision_is_rejected(self):
        self.info["NestedWriter"]["input"]["required"]["format.codec"] = ["STRING", {}]
        with self.assertRaises(ComfyUIContractError):
            validate_comfyui_api_contract(self.graph(), self.info)
        with self.assertRaisesRegex(ComfyUICanvasExportError, "colliding"):
            api_graph_to_ui_workflow(self.graph(), self.info)

    def test_legacy_flat_dynamic_names_and_unknown_selection(self):
        config = {"formats": {"mp4": [["crf", "INT", {"min": 0, "max": 51}]]}}
        self.assertEqual(dynamic_widget_definitions("format", "COMBO", config, selected_value="mp4"),
                         (("crf", ["INT", {"min": 0, "max": 51}], True),))
        self.assertEqual(dynamic_widget_definitions("format", "COMBO", config, selected_value=["1", 0]), ())
