from django.test import TestCase

from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    ComfyUICanvasImportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
    ui_workflow_to_api_graph,
)
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    validate_comfyui_api_contract,
)


def _autogrow(child_name: str, child_type: str, prefix: str, maximum: int):
    return [
        "COMFY_AUTOGROW_V3",
        {
            "template": {
                "input": {"required": {child_name: [child_type, {}]}},
                "prefix": prefix,
                "min": 0,
                "max": maximum,
            },
        },
    ]


class ComfyUICanvasConversionTests(TestCase):
    def object_info(self):
        return {
            "LoadImage": {
                "input": {"required": {"image": [["server-private.png"], {"image_upload": True}]}},
                "input_order": {"required": ["image"]},
                "output": ["IMAGE", "MASK"],
                "output_name": ["IMAGE", "MASK"],
            },
            "LoadVideo": {
                "input": {"required": {"file": ["COMBO", {"video_upload": True, "options": []}]}},
                "input_order": {"required": ["file"]},
                "output": ["VIDEO"],
                "output_name": ["VIDEO"],
            },
            "GetVideoComponents": {
                "input": {"required": {"video": ["VIDEO", {}]}},
                "input_order": {"required": ["video"]},
                "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
                "output_name": ["images", "audio", "fps", "bit_depth"],
            },
            "LoadAudio": {
                "input": {"required": {"audio": ["COMBO", {"audio_upload": True, "options": []}]}},
                "input_order": {"required": ["audio"]},
                "output": ["AUDIO"],
                "output_name": ["AUDIO"],
            },
            "MiniMaxH3ReferenceToVideo": {
                "input": {
                    "required": {
                        "prompt": ["STRING", {"default": "", "multiline": True}],
                        "width": ["INT", {"default": 1344}],
                        "height": ["INT", {"default": 768}],
                    },
                    "optional": {
                        "ref_images": _autogrow("ref_image", "IMAGE", "ref_image_", 9),
                        "ref_videos": _autogrow("ref_video", "IMAGE", "ref_video_", 3),
                        "ref_video_audios": _autogrow(
                            "ref_video_audio", "AUDIO", "ref_video_audio_", 3
                        ),
                        "ref_audios": _autogrow("ref_audio", "AUDIO", "ref_audio_", 3),
                    },
                },
                "input_order": {
                    "required": ["prompt", "width", "height"],
                    "optional": [
                        "ref_images",
                        "ref_videos",
                        "ref_video_audios",
                        "ref_audios",
                    ],
                },
                "output": ["CONDITIONING", "LATENT"],
                "output_name": ["positive", "LATENT"],
            },
        }

    def graph(self):
        return {
            "1": {"class_type": "LoadImage", "inputs": {"image": "character.png"}},
            "2": {"class_type": "LoadVideo", "inputs": {"file": "motion.mp4"}},
            "3": {"class_type": "GetVideoComponents", "inputs": {"video": ["2", 0]}},
            "4": {"class_type": "LoadAudio", "inputs": {"audio": "voice.wav"}},
            "5": {
                "class_type": "MiniMaxH3ReferenceToVideo",
                "inputs": {
                    "prompt": (
                        "<Picture 1> defines identity; <Video 1> defines motion; "
                        "<Audio 1> defines the synchronized voice."
                    ),
                    "width": 736,
                    "height": 416,
                    "ref_images.ref_image_0": ["1", 0],
                    "ref_videos.ref_video_0": ["3", 0],
                    "ref_video_audios.ref_video_audio_0": ["3", 1],
                    "ref_audios.ref_audio_0": ["4", 0],
                },
            },
        }

    def test_ref2va_dynamic_image_video_and_audio_links_are_canvas_ports(self):
        canvas = api_graph_to_ui_workflow(self.graph(), self.object_info())

        self.assertTrue(is_comfyui_ui_workflow(canvas))
        target = next(
            node for node in canvas["nodes"]
            if node["type"] == "MiniMaxH3ReferenceToVideo"
        )
        target_inputs = {item["name"]: item for item in target["inputs"]}
        expected = {
            "ref_images.ref_image_0": "IMAGE",
            "ref_videos.ref_video_0": "IMAGE",
            "ref_video_audios.ref_video_audio_0": "AUDIO",
            "ref_audios.ref_audio_0": "AUDIO",
        }
        for name, input_type in expected.items():
            self.assertEqual(target_inputs[name]["type"], input_type)
            self.assertIsInstance(target_inputs[name]["link"], int)
            self.assertEqual(target_inputs[name]["shape"], 7)
        # One unconnected expandable socket per group keeps the imported H3
        # node editable in the same way as the official workflow.
        self.assertIn("ref_images.ref_image_1", target_inputs)
        self.assertIsNone(target_inputs["ref_images.ref_image_1"]["link"])
        self.assertIn("ref_videos.ref_video_1", target_inputs)
        self.assertIn("ref_video_audios.ref_video_audio_1", target_inputs)
        self.assertIn("ref_audios.ref_audio_1", target_inputs)
        self.assertEqual(len(canvas["links"]), 5)

        load_video = next(node for node in canvas["nodes"] if node["type"] == "LoadVideo")
        load_audio = next(node for node in canvas["nodes"] if node["type"] == "LoadAudio")
        self.assertEqual([item["name"] for item in load_video["inputs"]], ["file", "upload"])
        self.assertEqual(load_video["widgets_values"], ["motion.mp4", "video"])
        self.assertEqual([item["name"] for item in load_audio["inputs"]], ["audio", "upload"])
        self.assertEqual(load_audio["widgets_values"], ["voice.wav", "audio"])
        self.assertNotIn("server-private.png", str(canvas))

    def test_converter_rejects_unknown_api_input_even_if_object_info_is_remote(self):
        graph = self.graph()
        graph["5"]["inputs"]["unreviewed_remote_socket"] = "value"

        with self.assertRaisesRegex(ComfyUICanvasExportError, "unknown inputs"):
            api_graph_to_ui_workflow(graph, self.object_info())

    def test_converter_rejects_invalid_source_output_slot(self):
        graph = self.graph()
        graph["5"]["inputs"]["ref_images.ref_image_0"] = ["1", 999]

        with self.assertRaisesRegex(ComfyUICanvasExportError, "invalid output slot"):
            api_graph_to_ui_workflow(graph, self.object_info())

    def test_converter_rejects_dynamic_slot_beyond_object_info_limit(self):
        graph = self.graph()
        graph["5"]["inputs"]["ref_videos.ref_video_3"] = ["3", 0]

        with self.assertRaisesRegex(ComfyUICanvasExportError, "exceeds its declared maximum"):
            api_graph_to_ui_workflow(graph, self.object_info())

    def test_fixed_h3_loader_models_are_preserved_as_widget_values(self):
        object_info = {
            "UNETLoader": {
                "input": {
                    "required": {
                        "unet_name": [["server-default.safetensors"], {}],
                        "weight_dtype": [["default", "fp8_e4m3fn"], {}],
                    },
                },
                "input_order": {"required": ["unet_name", "weight_dtype"]},
                "output": ["MODEL"],
                "output_name": ["MODEL"],
            },
            "CLIPLoader": {
                "input": {
                    "required": {
                        "clip_name": [["server-default-clip.safetensors"], {}],
                        "type": [["minimax"], {}],
                        "device": [["default", "cpu"], {}],
                    },
                },
                "input_order": {"required": ["clip_name", "type", "device"]},
                "output": ["CLIP"],
                "output_name": ["CLIP"],
            },
            "VAELoader": {
                "input": {
                    "required": {"vae_name": [["server-default-vae.safetensors"], {}]},
                },
                "input_order": {"required": ["vae_name"]},
                "output": ["VAE"],
                "output_name": ["VAE"],
            },
        }
        graph = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
                    "type": "minimax",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
            },
        }

        canvas = api_graph_to_ui_workflow(graph, object_info)

        values = {node["type"]: node["widgets_values"] for node in canvas["nodes"]}
        self.assertEqual(
            values["UNETLoader"],
            ["minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"],
        )
        self.assertEqual(
            values["CLIPLoader"],
            ["qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "minimax", "default"],
        )
        self.assertEqual(values["VAELoader"], ["minimax_h3_video_vae_fp16.safetensors"])
        self.assertNotIn("server-default", str(canvas))


class ComfyUICanvasImportTests(TestCase):
    def object_info(self):
        return {
            "LoadImage": {
                "input": {
                    "required": {
                        "image": [["server-private.png"], {"image_upload": True}],
                    },
                },
                "input_order": {"required": ["image"]},
                "output": ["IMAGE", "MASK"],
                "output_name": ["IMAGE", "MASK"],
            },
            "SaveImage": {
                "input": {
                    "required": {
                        "images": ["IMAGE", {}],
                        "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    },
                },
                "input_order": {"required": ["images", "filename_prefix"]},
                "output": [],
                "output_name": [],
                "output_node": True,
            },
            "IdentityImage": {
                "input": {"required": {"image": ["IMAGE", {}]}},
                "input_order": {"required": ["image"]},
                "output": ["IMAGE"],
                "output_name": ["IMAGE"],
            },
            "AmbiguousImage": {
                "input": {
                    "required": {
                        "left": ["IMAGE", {}],
                        "right": ["IMAGE", {}],
                    },
                },
                "input_order": {"required": ["left", "right"]},
                "output": ["IMAGE"],
                "output_name": ["IMAGE"],
            },
        }

    @staticmethod
    def node(node_id, node_type, *, inputs=None, outputs=None, widgets=None, mode=0):
        return {
            "id": node_id,
            "type": node_type,
            "pos": [0, 0],
            "size": [240, 120],
            "flags": {},
            "order": node_id if isinstance(node_id, int) else 0,
            "mode": mode,
            "inputs": inputs or [],
            "outputs": outputs or [],
            "properties": {"Node name for S&R": node_type},
            "widgets_values": widgets or [],
        }

    @staticmethod
    def canvas(nodes, links, *, definitions=None):
        value = {
            "id": "5f2a5148-90ff-424a-bf49-66666b85139d",
            "revision": 0,
            "last_node_id": 100,
            "last_link_id": 100,
            "nodes": nodes,
            "links": links,
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }
        if definitions is not None:
            value["definitions"] = {"subgraphs": definitions}
        return value

    def basic_canvas(self):
        load = self.node(
            1,
            "LoadImage",
            inputs=[
                {"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None},
                {"name": "upload", "type": "IMAGEUPLOAD", "widget": {"name": "upload"}, "link": None},
            ],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
            widgets=["chosen.png", "image"],
        )
        save = self.node(
            2,
            "SaveImage",
            inputs=[
                {"name": "images", "type": "IMAGE", "link": 1},
                {
                    "name": "filename_prefix",
                    "type": "STRING",
                    "widget": {"name": "filename_prefix"},
                    "link": None,
                },
            ],
            widgets=["approved/output"],
        )
        return self.canvas([load, save], [[1, 1, 0, 2, 0, "IMAGE"]])

    def test_top_level_widgets_links_and_mapping_are_deterministic(self):
        canvas = self.basic_canvas()

        first = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )
        second = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.api_graph,
            {
                "1": {"class_type": "LoadImage", "inputs": {"image": "chosen.png"}},
                "2": {
                    "class_type": "SaveImage",
                    "inputs": {
                        "images": ["1", 0],
                        "filename_prefix": "approved/output",
                    },
                },
            },
        )
        self.assertEqual(first.node_id_map, {"top/1": "1", "top/2": "2"})
        self.assertEqual(first.primary_output_node_ids, ("2",))

    def test_explicit_primary_prunes_unreachable_nodes_and_ui_notes(self):
        canvas = self.basic_canvas()
        canvas["nodes"].extend([
            self.node(8, "Note", widgets=["operator note"]),
            self.node(
                9,
                "LoadImage",
                inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}}],
                outputs=[{"name": "IMAGE", "type": "IMAGE", "links": None}],
                widgets=["unused.png", "image"],
            ),
        ])

        result = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )

        self.assertEqual(set(result.node_id_map), {"top/1", "top/2"})
        dropped = {item["scoped_id"]: item["reason"] for item in result.conversion_report["dropped_nodes"]}
        self.assertEqual(dropped["top/8"], "ui_only")
        self.assertEqual(dropped["top/9"], "unreachable")

    def test_subgraph_literal_proxy_overrides_stale_internal_widget_and_output_proxy(self):
        subgraph_id = "b18f50d1-645b-48aa-982b-c9998d01b836"
        inner = self.node(
            1,
            "LoadImage",
            inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": 11}],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [12]}],
            widgets=["stale-inner.png", "image"],
        )
        definition = {
            "id": subgraph_id,
            "name": "Load selected image",
            "version": 1,
            "inputs": [{"name": "image", "type": "COMBO"}],
            "outputs": [{"name": "image", "type": "IMAGE"}],
            "nodes": [inner],
            "links": [
                {"id": 11, "origin_id": -10, "origin_slot": 0, "target_id": 1, "target_slot": 0, "type": "COMBO"},
                {"id": 12, "origin_id": 1, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "IMAGE"},
            ],
        }
        instance = self.node(
            10,
            subgraph_id,
            inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None}],
            outputs=[{"name": "image", "type": "IMAGE", "links": [20]}],
            widgets=["outer-authoritative.png"],
        )
        save = self.node(
            20,
            "SaveImage",
            inputs=[
                {"name": "images", "type": "IMAGE", "link": 20},
                {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}},
            ],
            widgets=["subgraph/output"],
        )
        canvas = self.canvas(
            [instance, save],
            [[20, 10, 0, 20, 0, "IMAGE"]],
            definitions=[definition],
        )

        result = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[20],
        )

        load_id = result.node_id_map[f"top/10/{subgraph_id}/1"]
        save_id = result.node_id_map["top/20"]
        self.assertEqual(result.api_graph[load_id]["inputs"]["image"], "outer-authoritative.png")
        self.assertEqual(result.api_graph[save_id]["inputs"]["images"], [load_id, 0])
        self.assertNotIn("stale-inner.png", str(result.api_graph))
        self.assertEqual(result.conversion_report["expanded_subgraph_count"], 1)

    def test_subgraph_link_proxy_and_direct_output_passthrough(self):
        subgraph_id = "e1768bbb-2e47-43e0-a8ad-fe8c2fb58934"
        definition = {
            "id": subgraph_id,
            "name": "Pass through",
            "version": 1,
            "inputs": [{"name": "image", "type": "IMAGE"}],
            "outputs": [{"name": "image", "type": "IMAGE"}],
            "nodes": [],
            "links": [
                {"id": 30, "origin_id": -10, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "IMAGE"},
            ],
        }
        canvas = self.basic_canvas()
        instance = self.node(
            3,
            subgraph_id,
            inputs=[{"name": "image", "type": "IMAGE", "link": 1}],
            outputs=[{"name": "image", "type": "IMAGE", "links": [2]}],
        )
        canvas["nodes"] = [canvas["nodes"][0], instance, canvas["nodes"][1]]
        canvas["nodes"][2]["inputs"][0]["link"] = 2
        canvas["links"] = [
            [1, 1, 0, 3, 0, "IMAGE"],
            [2, 3, 0, 2, 0, "IMAGE"],
        ]
        canvas["definitions"] = {"subgraphs": [definition]}

        result = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )

        self.assertEqual(result.api_graph["2"]["inputs"]["images"], ["1", 0])
        self.assertNotIn("top/3", result.node_id_map)

    def test_nested_subgraph_proxies_expand_recursively(self):
        inner_id = "56abac90-5009-46ac-8390-779effb5a2f7"
        outer_id = "83bd79c7-4702-41a8-b4e3-f0e4a36fac92"
        inner = {
            "id": inner_id,
            "name": "inner pass through",
            "version": 1,
            "inputs": [{"name": "image", "type": "IMAGE"}],
            "outputs": [{"name": "image", "type": "IMAGE"}],
            "nodes": [],
            "links": [
                {"id": 1, "origin_id": -10, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "IMAGE"},
            ],
        }
        inner_instance = self.node(
            1,
            inner_id,
            inputs=[{"name": "image", "type": "IMAGE", "link": 2}],
            outputs=[{"name": "image", "type": "IMAGE", "links": [3]}],
        )
        outer = {
            "id": outer_id,
            "name": "outer pass through",
            "version": 1,
            "inputs": [{"name": "image", "type": "IMAGE"}],
            "outputs": [{"name": "image", "type": "IMAGE"}],
            "nodes": [inner_instance],
            "links": [
                {"id": 2, "origin_id": -10, "origin_slot": 0, "target_id": 1, "target_slot": 0, "type": "IMAGE"},
                {"id": 3, "origin_id": 1, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "IMAGE"},
            ],
        }
        canvas = self.basic_canvas()
        outer_instance = self.node(
            3,
            outer_id,
            inputs=[{"name": "image", "type": "IMAGE", "link": 1}],
            outputs=[{"name": "image", "type": "IMAGE", "links": [2]}],
        )
        canvas["nodes"] = [canvas["nodes"][0], outer_instance, canvas["nodes"][1]]
        canvas["nodes"][2]["inputs"][0]["link"] = 2
        canvas["links"] = [[1, 1, 0, 3, 0, "IMAGE"], [2, 3, 0, 2, 0, "IMAGE"]]
        canvas["definitions"] = {"subgraphs": [outer, inner]}

        result = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )

        self.assertEqual(result.api_graph["2"]["inputs"]["images"], ["1", 0])
        self.assertEqual(result.conversion_report["expanded_subgraph_count"], 2)

    def test_reachable_mode_four_is_bypassed_only_when_unambiguous_and_type_safe(self):
        canvas = self.basic_canvas()
        bypass = self.node(
            3,
            "IdentityImage",
            inputs=[{"name": "image", "type": "IMAGE", "link": 1}],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [2]}],
            mode=4,
        )
        canvas["nodes"] = [canvas["nodes"][0], bypass, canvas["nodes"][1]]
        canvas["nodes"][2]["inputs"][0]["link"] = 2
        canvas["links"] = [[1, 1, 0, 3, 0, "IMAGE"], [2, 3, 0, 2, 0, "IMAGE"]]

        result = ui_workflow_to_api_graph(
            canvas,
            self.object_info(),
            primary_output_node_ids=[2],
        )

        self.assertEqual(result.api_graph["2"]["inputs"]["images"], ["1", 0])
        self.assertEqual(result.conversion_report["bypassed_nodes"], ["top/3"])

        ambiguous = self.node(
            4,
            "AmbiguousImage",
            inputs=[
                {"name": "left", "type": "IMAGE", "link": 1},
                {"name": "right", "type": "IMAGE", "link": 1},
            ],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [2]}],
            mode=4,
        )
        canvas["nodes"][1] = ambiguous
        canvas["links"] = [[1, 1, 0, 4, 0, "IMAGE"], [2, 4, 0, 2, 0, "IMAGE"]]

        with self.assertRaisesRegex(ComfyUICanvasImportError, "cannot be safely bypassed"):
            ui_workflow_to_api_graph(
                canvas,
                self.object_info(),
                primary_output_node_ids=[2],
            )

    def test_recursive_subgraph_cycle_and_duplicate_target_are_rejected(self):
        cyclic_id = "c85001a1-6cd7-432a-8187-7813916e7881"
        cyclic = {
            "id": cyclic_id,
            "name": "recursive",
            "version": 1,
            "inputs": [],
            "outputs": [],
            "nodes": [self.node(1, cyclic_id)],
            "links": [],
        }
        canvas = self.canvas([self.node(1, cyclic_id)], [], definitions=[cyclic])
        with self.assertRaisesRegex(ComfyUICanvasImportError, "recursive subgraph cycle"):
            ui_workflow_to_api_graph(
                canvas,
                self.object_info(),
                primary_output_node_ids=[1],
            )

        canvas = self.basic_canvas()
        canvas["links"].append([2, 1, 0, 2, 0, "IMAGE"])
        with self.assertRaisesRegex(ComfyUICanvasImportError, "multiple links target"):
            ui_workflow_to_api_graph(
                canvas,
                self.object_info(),
                primary_output_node_ids=[2],
            )

    def test_subgraph_expansion_depth_is_bounded(self):
        definitions = []
        definition_ids = [f"definition-{index}" for index in range(10)]
        for index, definition_id in enumerate(definition_ids):
            child_nodes = (
                [self.node(1, definition_ids[index + 1])]
                if index + 1 < len(definition_ids) else []
            )
            definitions.append({
                "id": definition_id,
                "name": definition_id,
                "version": 1,
                "inputs": [],
                "outputs": [],
                "nodes": child_nodes,
                "links": [],
            })
        canvas = self.canvas(
            [self.node(1, definition_ids[0])],
            [],
            definitions=definitions,
        )

        with self.assertRaisesRegex(ComfyUICanvasImportError, "maximum depth"):
            ui_workflow_to_api_graph(
                canvas,
                self.object_info(),
                primary_output_node_ids=[1],
            )

    def test_primary_output_is_required_and_must_be_unambiguous(self):
        with self.assertRaisesRegex(ComfyUICanvasImportError, "primary_output_node_ids"):
            ui_workflow_to_api_graph(
                self.basic_canvas(),
                self.object_info(),
                primary_output_node_ids=[],
            )

        canvas = self.basic_canvas()
        with self.assertRaisesRegex(ComfyUICanvasImportError, "does not identify"):
            ui_workflow_to_api_graph(
                canvas,
                self.object_info(),
                primary_output_node_ids=[999],
            )

    def test_dynamic_combo_consumes_only_selected_nested_widgets(self):
        load = self.basic_canvas()["nodes"][0]
        save = self.node(
            195,
            "SaveImageAdvanced",
            inputs=[
                {"name": "images", "type": "IMAGE", "link": 1},
                {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}},
                {"name": "format", "type": "COMFY_DYNAMICCOMBO_V3", "widget": {"name": "format"}},
                {"name": "format.bit_depth", "type": "COMBO", "widget": {"name": "format.bit_depth"}},
                {"name": "format.input_color_space", "type": "COMBO", "widget": {"name": "format.input_color_space"}},
            ],
            outputs=[{"name": "images", "type": "IMAGE", "links": None}],
            widgets=["managed/output", "png", "8-bit", "sRGB"],
        )
        object_info = {
            **self.object_info(),
            "SaveImageAdvanced": {
                "input": {"required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                        {"key": "png", "inputs": {"required": {
                            "bit_depth": ["COMBO", {"default": "8-bit", "options": ["8-bit", "16-bit"]}],
                            "input_color_space": ["COMBO", {"default": "sRGB", "options": ["sRGB"]}],
                        }}},
                        {"key": "exr", "inputs": {"required": {
                            "bit_depth": ["COMBO", {"default": "32-bit float", "options": ["32-bit float"]}],
                            "input_color_space": ["COMBO", {"default": "sRGB", "options": ["sRGB", "HDR", "linear"]}],
                        }}},
                    ]}],
                }},
                "input_order": {"required": ["images", "filename_prefix", "format"]},
                "output": ["IMAGE"],
                "output_name": ["images"],
                "output_node": True,
            },
        }
        canvas = self.canvas([load, save], [[1, 1, 0, 195, 0, "IMAGE"]])

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[195])
        validate_comfyui_api_contract(
            result.api_graph,
            object_info,
            uploaded_paths={"chosen.png"},
        )

        self.assertEqual(result.api_graph["2"]["inputs"], {
            "images": ["1", 0],
            "filename_prefix": "managed/output",
            "format": "png",
            "format.bit_depth": "8-bit",
            "format.input_color_space": "sRGB",
        })

    def test_save_audio_advanced_infers_omitted_positional_widgets(self):
        source = self.node(
            1,
            "LoadAudio",
            inputs=[
                {"name": "audio", "type": "COMBO", "widget": {"name": "audio"}},
                {"name": "upload", "type": "AUDIOUPLOAD", "widget": {"name": "upload"}},
            ],
            outputs=[{"name": "AUDIO", "type": "AUDIO", "links": [1]}],
            widgets=["fixture.wav", "audio"],
        )
        save = self.node(
            2,
            "SaveAudioAdvanced",
            # Current Comfy canvases can omit every widget-backed input here;
            # widgets_values still follows live object_info positional order.
            inputs=[{"name": "audio", "type": "AUDIO", "link": 1}],
            widgets=["autoprompt/audio", "mp3", "V0"],
        )
        object_info = {
            "LoadAudio": {
                "input": {"required": {
                    "audio": ["COMBO", {"audio_upload": True, "options": []}],
                }},
                "input_order": {"required": ["audio"]},
                "output": ["AUDIO"],
                "output_name": ["AUDIO"],
            },
            "SaveAudioAdvanced": {
                "input": {"required": {
                    "audio": ["AUDIO", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                        {"key": "wav", "inputs": {"required": {}}},
                        {"key": "mp3", "inputs": {"required": {
                            "quality": ["COMBO", {
                                "default": "V0",
                                "options": ["V0", "V2", "128k"],
                            }],
                        }}},
                    ]}],
                }},
                "input_order": {
                    "required": ["audio", "filename_prefix", "format"],
                },
                "output": [],
                "output_name": [],
                "output_node": True,
            },
        }
        canvas = self.canvas(
            [source, save],
            [[1, 1, 0, 2, 0, "AUDIO"]],
        )

        result = ui_workflow_to_api_graph(
            canvas,
            object_info,
            primary_output_node_ids=[2],
        )
        validate_comfyui_api_contract(
            result.api_graph,
            object_info,
            uploaded_paths={"fixture.wav"},
        )

        self.assertEqual(result.api_graph["2"]["inputs"], {
            "audio": ["1", 0],
            "filename_prefix": "autoprompt/audio",
            "format": "mp3",
            "format.quality": "V0",
        })

    def test_text_dynamic_combo_is_deterministic(self):
        source = self.node(
            1,
            "CLIPLoader",
            inputs=[{"name": "clip_name", "type": "COMBO", "widget": {"name": "clip_name"}}],
            outputs=[{"name": "CLIP", "type": "CLIP", "links": [1]}],
            widgets=["qwen.safetensors"],
        )
        text = self.node(
            3,
            "TextGenerate",
            inputs=[
                {"name": "clip", "type": "CLIP", "link": 1},
                {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}},
                {"name": "max_length", "type": "INT", "widget": {"name": "max_length"}},
                {"name": "sampling_mode", "type": "COMFY_DYNAMICCOMBO_V3", "widget": {"name": "sampling_mode"}},
                {"name": "sampling_mode.temperature", "type": "FLOAT", "widget": {"name": "sampling_mode.temperature"}},
                {"name": "sampling_mode.seed", "type": "INT", "widget": {"name": "sampling_mode.seed"}},
            ],
            outputs=[{"name": "text", "type": "STRING", "links": [2]}],
            widgets=["managed prompt", 256, "on", 0.7, 42],
        )
        preview = self.node(
            4,
            "PreviewAny",
            inputs=[{"name": "source", "type": "STRING", "link": 2}],
        )
        object_info = {
            "CLIPLoader": {
                "input": {"required": {"clip_name": [["qwen.safetensors"], {}]}},
                "input_order": {"required": ["clip_name"]},
                "output": ["CLIP"], "output_name": ["CLIP"],
            },
            "TextGenerate": {
                "input": {"required": {
                    "clip": ["CLIP", {}],
                    "prompt": ["STRING", {"default": ""}],
                    "max_length": ["INT", {"default": 512, "min": 1}],
                    "sampling_mode": ["COMFY_DYNAMICCOMBO_V3", {"options": [{
                        "key": "on", "inputs": {"required": {
                            "temperature": ["FLOAT", {"default": 0.7, "min": 0.01, "max": 2}],
                            "seed": ["INT", {"default": 0, "min": 0}],
                        }},
                    }, {"key": "off", "inputs": {"required": {}}}]}],
                }},
                "input_order": {"required": ["clip", "prompt", "max_length", "sampling_mode"]},
                "output": ["STRING"], "output_name": ["generated_text"],
            },
            "PreviewAny": {
                "input": {"required": {"source": ["STRING", {}]}},
                "input_order": {"required": ["source"]},
                "output": [], "output_name": [], "output_node": True,
            },
        }
        canvas = self.canvas(
            [source, text, preview],
            [[1, 1, 0, 3, 0, "CLIP"], [2, 3, 0, 4, 0, "STRING"]],
        )

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[4])
        validate_comfyui_api_contract(result.api_graph, object_info)

        text_inputs = result.api_graph["2"]["inputs"]
        self.assertEqual(text_inputs["sampling_mode"], "on")
        self.assertEqual(text_inputs["sampling_mode.temperature"], 0.7)
        self.assertEqual(text_inputs["sampling_mode.seed"], 42)

    def test_seed_control_marker_is_skipped_only_after_seed_widget(self):
        image = self.basic_canvas()["nodes"][0]
        upscaler = self.node(
            10,
            "SeedVR2VideoUpscaler",
            inputs=[
                {"name": "image", "type": "IMAGE", "link": 1},
                {"name": "seed", "type": "INT", "widget": {"name": "seed"}},
                {"name": "resolution", "type": "INT", "widget": {"name": "resolution"}},
            ],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [2]}],
            widgets=[42, "randomize", 2048],
        )
        save = self.node(
            28,
            "SaveImage",
            inputs=[
                {"name": "images", "type": "IMAGE", "link": 2},
                {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}},
            ],
            widgets=["managed/seed"],
        )
        object_info = {
            **self.object_info(),
            "SeedVR2VideoUpscaler": {
                "input": {"required": {
                    "image": ["IMAGE", {}],
                    "seed": ["INT", {"default": 42, "min": 0}],
                    "resolution": ["INT", {"default": 1080, "min": 16}],
                }},
                "input_order": {"required": ["image", "seed", "resolution"]},
                "output": ["IMAGE"], "output_name": ["IMAGE"],
            },
        }
        canvas = self.canvas(
            [image, upscaler, save],
            [[1, 1, 0, 10, 0, "IMAGE"], [2, 10, 0, 28, 0, "IMAGE"]],
        )

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[28])
        validate_comfyui_api_contract(
            result.api_graph,
            object_info,
            uploaded_paths={"chosen.png"},
        )

        self.assertEqual(result.api_graph["2"]["inputs"]["seed"], 42)
        self.assertEqual(result.api_graph["2"]["inputs"]["resolution"], 2048)
        self.assertNotIn("randomize", str(result.api_graph))

    def test_vhs_keyed_widgets_are_whitelisted_with_selected_format_children(self):
        image = self.basic_canvas()["nodes"][0]
        video = self.node(
            12,
            "VHS_VideoCombine",
            inputs=[{"name": "images", "type": "IMAGE", "link": 1}],
            outputs=[{"name": "Filenames", "type": "VHS_FILENAMES", "links": None}],
            widgets={
                "frame_rate": 24,
                "loop_count": 0,
                "filename_prefix": "managed/video",
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "trim_to_audio": False,
                "pingpong": False,
                "save_output": True,
            },
        )
        object_info = {
            **self.object_info(),
            "VHS_VideoCombine": {
                "input": {"required": {
                    "images": ["IMAGE", {}],
                    "frame_rate": ["FLOAT", {"default": 8, "min": 1}],
                    "loop_count": ["INT", {"default": 0, "min": 0}],
                    "filename_prefix": ["STRING", {"default": "AnimateDiff"}],
                    "format": [["video/h264-mp4"], {"formats": {
                        "video/h264-mp4": [
                            ["pix_fmt", ["yuv420p", "yuv444p"], {"default": "yuv420p"}],
                            ["crf", "INT", {"default": 19, "min": 0, "max": 51}],
                            ["save_metadata", "BOOLEAN", {"default": True}],
                            ["trim_to_audio", "BOOLEAN", {"default": False}],
                        ],
                    }}],
                    "pingpong": ["BOOLEAN", {"default": False}],
                    "save_output": ["BOOLEAN", {"default": True}],
                }},
                "input_order": {"required": [
                    "images", "frame_rate", "loop_count", "filename_prefix",
                    "format", "pingpong", "save_output",
                ]},
                "output": ["VHS_FILENAMES"], "output_name": ["Filenames"],
                "output_node": True,
            },
        }
        canvas = self.canvas([image, video], [[1, 1, 0, 12, 0, "IMAGE"]])

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[12])
        validate_comfyui_api_contract(
            result.api_graph,
            object_info,
            uploaded_paths={"chosen.png"},
        )

        self.assertEqual(result.api_graph["2"]["inputs"]["pix_fmt"], "yuv420p")
        self.assertEqual(result.api_graph["2"]["inputs"]["crf"], 19)
        bad = self.canvas([image, {**video, "widgets_values": {
            **video["widgets_values"], "unreviewed": "value",
        }}], [[1, 1, 0, 12, 0, "IMAGE"]])
        with self.assertRaisesRegex(ComfyUICanvasImportError, "unknown keyed widget"):
            ui_workflow_to_api_graph(bad, object_info, primary_output_node_ids=[12])

    def test_matchtype_output_is_derived_from_consistent_concrete_branches(self):
        false_source = self.node(
            1,
            "UNETLoader",
            inputs=[
                {"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}},
                {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}},
            ],
            outputs=[{"name": "MODEL", "type": "MODEL", "links": [2]}],
            widgets=["model.safetensors", "default"],
        )
        true_source = self.node(
            2,
            "UNETLoader",
            inputs=[
                {"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}},
                {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}},
            ],
            outputs=[{"name": "MODEL", "type": "MODEL", "links": [3]}],
            widgets=["model.safetensors", "default"],
        )
        switch = self.node(
            3,
            "ComfySwitchNode",
            inputs=[
                {"name": "switch", "type": "BOOLEAN", "widget": {"name": "switch"}},
                {"name": "on_false", "type": "MODEL", "link": 2},
                {"name": "on_true", "type": "MODEL", "link": 3},
            ],
            outputs=[{"name": "output", "type": "MODEL", "links": [4]}],
            widgets=[False],
        )
        sink = self.node(
            4,
            "LoraLoaderModelOnly",
            inputs=[
                {"name": "model", "type": "MODEL", "link": 4},
                {"name": "lora_name", "type": "COMBO", "widget": {"name": "lora_name"}},
                {"name": "strength_model", "type": "FLOAT", "widget": {"name": "strength_model"}},
            ],
            outputs=[{"name": "MODEL", "type": "MODEL", "links": None}],
            widgets=["style.safetensors", 1.0],
        )
        object_info = {
            "UNETLoader": {
                "input": {"required": {
                    "unet_name": [["model.safetensors"], {}],
                    "weight_dtype": [["default"], {}],
                }},
                "input_order": {"required": ["unet_name", "weight_dtype"]},
                "output": ["MODEL"],
                "output_name": ["MODEL"],
            },
            "ComfySwitchNode": {
                "input": {"required": {
                    "switch": ["BOOLEAN", {}],
                    "on_false": ["COMFY_MATCHTYPE_V3", {
                        "template": {"template_id": "switch", "allowed_types": "*"},
                    }],
                    "on_true": ["COMFY_MATCHTYPE_V3", {
                        "template": {"template_id": "switch", "allowed_types": "*"},
                    }],
                }},
                "input_order": {"required": ["switch", "on_false", "on_true"]},
                "output": ["COMFY_MATCHTYPE_V3"],
                "output_name": ["output"],
                "output_matchtypes": ["switch"],
            },
            "LoraLoaderModelOnly": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "lora_name": [["style.safetensors"], {}],
                    "strength_model": ["FLOAT", {"default": 1.0}],
                }},
                "input_order": {"required": ["model", "lora_name", "strength_model"]},
                "output": ["MODEL"],
                "output_name": ["MODEL"],
            },
        }
        canvas = self.canvas(
            [false_source, true_source, switch, sink],
            [
                [2, 1, 0, 3, 1, "MODEL"],
                [3, 2, 0, 3, 2, "MODEL"],
                [4, 3, 0, 4, 0, "MODEL"],
            ],
        )

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[4])
        validate_comfyui_api_contract(result.api_graph, object_info)
        self.assertEqual(result.api_graph["3"]["inputs"]["on_false"], ["1", 0])

        bad_object_info = {**object_info, "PrimitiveFloat": {
            "input": {"required": {"value": ["FLOAT", {"default": 0.0}]}},
            "input_order": {"required": ["value"]},
            "output": ["FLOAT"],
            "output_name": ["value"],
        }}
        bad_true = self.node(
            2,
            "PrimitiveFloat",
            inputs=[{"name": "value", "type": "FLOAT", "widget": {"name": "value"}}],
            outputs=[{"name": "value", "type": "FLOAT", "links": [3]}],
            widgets=[1.0],
        )
        bad = self.canvas(
            [false_source, bad_true, switch, sink],
            [
                [2, 1, 0, 3, 1, "MODEL"],
                [3, 2, 0, 3, 2, "FLOAT"],
                [4, 3, 0, 4, 0, "MODEL"],
            ],
        )
        with self.assertRaisesRegex(ComfyUICanvasImportError, "matchtype .* disagree"):
            ui_workflow_to_api_graph(bad, bad_object_info, primary_output_node_ids=[4])
        bad_api_graph = {
            node_id: {"class_type": node["class_type"], "inputs": dict(node["inputs"])}
            for node_id, node in result.api_graph.items()
        }
        bad_api_graph["2"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 1.0}}
        with self.assertRaisesRegex(ComfyUIContractError, "matchtype"):
            validate_comfyui_api_contract(bad_api_graph, bad_object_info)

    def test_muted_source_without_passthrough_drops_only_optional_target(self):
        muted = self.node(
            11,
            "LoadImage",
            inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}}],
            outputs=[{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
            widgets=["old.png", "image"],
            mode=4,
        )
        optional = self.node(
            12,
            "TextGenerate",
            inputs=[
                {"name": "clip", "type": "CLIP", "link": None},
                {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}},
                {"name": "image", "type": "IMAGE", "link": 1},
            ],
            outputs=[{"name": "text", "type": "STRING", "links": [2]}],
            widgets=["managed"],
        )
        preview = self.node(13, "PreviewAny", inputs=[{"name": "source", "type": "STRING", "link": 2}])
        object_info = {
            **self.object_info(),
            "TextGenerate": {
                "input": {
                    "required": {"clip": ["CLIP", {}], "prompt": ["STRING", {}]},
                    "optional": {"image": ["IMAGE", {}]},
                },
                "input_order": {"required": ["clip", "prompt"], "optional": ["image"]},
                "output": ["STRING"], "output_name": ["text"],
            },
            "PreviewAny": {
                "input": {"required": {"source": ["STRING", {}]}},
                "input_order": {"required": ["source"]},
                "output": [], "output_name": [], "output_node": True,
            },
        }
        # A required unrelated CLIP is absent, so use a literal connection type
        # only to exercise the muted optional image path without persisting.
        object_info["TextGenerate"]["input"]["required"].pop("clip")
        optional["inputs"] = [item for item in optional["inputs"] if item["name"] != "clip"]
        canvas = self.canvas(
            [muted, optional, preview],
            [[1, 11, 0, 12, 1, "IMAGE"], [2, 12, 0, 13, 0, "STRING"]],
        )

        result = ui_workflow_to_api_graph(canvas, object_info, primary_output_node_ids=[13])

        self.assertNotIn("image", result.api_graph["1"]["inputs"])
        self.assertIn("top/11", result.conversion_report["bypassed_nodes"])
