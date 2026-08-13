from django.test import SimpleTestCase

from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
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


class ComfyUICanvasConversionTests(SimpleTestCase):
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
