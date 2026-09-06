from copy import deepcopy

from django.test import TestCase

from studio.services.comfyui import ComfyUIError
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    ComfyUIContractUnavailable,
    preflight_comfyui_workflow,
    validate_comfyui_api_contract,
)


class _OfflineClient:
    posted = False

    def object_info(self):
        raise ComfyUIError("offline")

    def queue_prompt(self, *args, **kwargs):
        self.posted = True
        raise AssertionError("preflight must never submit")


class ComfyUIContractTests(TestCase):
    CONFIRMED_UPLOADS = {
        "autoprompt/task/person.png",
        "autoprompt/task/motion.mp4",
        "autoprompt/task/voice.wav",
    }

    def object_info(self):
        return {
            "UNETLoader": {
                "input": {"required": {
                    "unet_name": [["h3-model.safetensors"]],
                    "weight_dtype": [["default", "fp8"]],
                }},
                "output": ["MODEL"],
            },
            "CLIPLoader": {
                "input": {
                    "required": {
                        "clip_name": [["h3-clip.safetensors"]],
                        "type": [["minimax"]],
                    },
                    "optional": {"device": [["default", "cpu"]]},
                },
                "output": ["CLIP"],
            },
            "VAELoader": {
                "input": {"required": {
                    "vae_name": [["video-vae.safetensors", "audio-vae.safetensors"]],
                }},
                "output": ["VAE"],
            },
            "LoadImage": {
                "input": {"required": {
                    # Real ComfyUI often lists root files only even though the
                    # upload API has just returned a valid subfolder path.
                    "image": [["root-only.png"], {"image_upload": True}],
                }},
                "output": ["IMAGE", "MASK"],
            },
            "LoadVideo": {
                "input": {"required": {
                    "file": ["COMBO", {"options": [], "video_upload": True}],
                }},
                "output": ["VIDEO"],
            },
            "GetVideoComponents": {
                "input": {"required": {"video": ["VIDEO", {}]}},
                "output": ["IMAGE", "AUDIO", "FLOAT", "INT"],
            },
            "LoadAudio": {
                "input": {"required": {
                    "audio": ["COMBO", {"options": [], "audio_upload": True}],
                }},
                "output": ["AUDIO"],
            },
            "MiniMaxH3ReferenceToVideo": {
                "input": {
                    "required": {
                        "clip": ["CLIP", {}],
                        "vae": ["VAE", {}],
                        "audio_vae": ["VAE", {}],
                        "prompt": ["STRING", {"multiline": True}],
                        "width": ["INT", {"min": 32, "max": 2048, "step": 32}],
                        "height": ["INT", {"min": 32, "max": 2048, "step": 32}],
                        "length": ["INT", {"min": 5, "max": 362, "step": 17}],
                        "ref_image_size": ["COMBO", {"options": ["match", "max"]}],
                    },
                    "optional": {
                        "ref_images": self.autogrow("ref_image_", "ref_image", "IMAGE", 9),
                        "ref_videos": self.autogrow("ref_video_", "ref_video", "IMAGE", 3),
                        "ref_video_audios": self.autogrow(
                            "ref_video_audio_", "ref_video_audio", "AUDIO", 3,
                        ),
                        "ref_audios": self.autogrow("ref_audio_", "ref_audio", "AUDIO", 3),
                    },
                },
                "output": ["CONDITIONING", "LATENT"],
            },
        }

    @staticmethod
    def autogrow(prefix, child_name, child_type, maximum):
        return ["COMFY_AUTOGROW_V3", {"template": {
            "input": {"required": {child_name: [child_type, {}]}},
            "prefix": prefix,
            "min": 0,
            "max": maximum,
        }}]

    def graph(self):
        return {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": "h3-model.safetensors", "weight_dtype": "default",
            }},
            "2": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": "h3-clip.safetensors", "type": "minimax", "device": "default",
            }},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "video-vae.safetensors"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": "audio-vae.safetensors"}},
            "5": {"class_type": "LoadImage", "inputs": {"image": "autoprompt/task/person.png"}},
            "6": {"class_type": "LoadVideo", "inputs": {"file": "autoprompt/task/motion.mp4"}},
            "7": {"class_type": "GetVideoComponents", "inputs": {"video": ["6", 0]}},
            "8": {"class_type": "LoadAudio", "inputs": {"audio": "autoprompt/task/voice.wav"}},
            "9": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "audio_vae": ["4", 0],
                "prompt": "A restrained tracking shot.",
                "width": 608,
                "height": 352,
                "length": 124,
                "ref_image_size": "match",
                "ref_images.ref_image_0": ["5", 0],
                "ref_videos.ref_video_0": ["7", 0],
                "ref_video_audios.ref_video_audio_0": ["7", 1],
                "ref_audios.ref_audio_0": ["8", 0],
            }},
        }

    @staticmethod
    def sdxl_object_info():
        return {
            "CheckpointLoaderSimple": {
                "input": {"required": {
                    "ckpt_name": [["sd_xl_base_1.0.safetensors"]],
                }},
                "output": ["MODEL", "CLIP", "VAE"],
            },
            "CLIPTextEncode": {
                "input": {"required": {
                    "text": ["STRING", {"multiline": True}],
                    "clip": ["CLIP", {}],
                }},
                "output": ["CONDITIONING"],
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {"min": 16, "max": 16384, "step": 8}],
                    "height": ["INT", {"min": 16, "max": 16384, "step": 8}],
                    "batch_size": ["INT", {"min": 1, "max": 4096}],
                }},
                "output": ["LATENT"],
            },
            "KSampler": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "seed": ["INT", {"min": 0, "max": 18446744073709551615}],
                    "steps": ["INT", {"min": 1, "max": 10000}],
                    "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}],
                    "sampler_name": [["euler", "dpmpp_2m"]],
                    "scheduler": [["simple", "karras"]],
                    "positive": ["CONDITIONING", {}],
                    "negative": ["CONDITIONING", {}],
                    "latent_image": ["LATENT", {}],
                    "denoise": ["FLOAT", {"min": 0.0, "max": 1.0}],
                }},
                "output": ["LATENT"],
            },
            "VAEDecode": {
                "input": {"required": {
                    "samples": ["LATENT", {}], "vae": ["VAE", {}],
                }},
                "output": ["IMAGE"],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["IMAGE", {}], "filename_prefix": ["STRING", {}],
                }},
                "output": ["IMAGE"],
            },
        }

    @staticmethod
    def sdxl_graph():
        return {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
                "ckpt_name": "sd_xl_base_1.0.safetensors",
            }},
            "2": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "cinematic production still", "clip": ["1", 1],
            }},
            "3": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "blurry, watermark", "clip": ["1", 1],
            }},
            "4": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 608, "height": 352, "batch_size": 1,
            }},
            "5": {"class_type": "KSampler", "inputs": {
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0], "seed": 42, "steps": 30, "cfg": 6.5,
                "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
            }},
            "6": {"class_type": "VAEDecode", "inputs": {
                "samples": ["5", 0], "vae": ["1", 2],
            }},
            "7": {"class_type": "SaveImage", "inputs": {
                "images": ["6", 0], "filename_prefix": "autoprompt/test/image",
            }},
        }

    def validate(self, graph, object_info=None):
        return validate_comfyui_api_contract(
            graph,
            object_info or self.object_info(),
            uploaded_paths=self.CONFIRMED_UPLOADS,
        )

    def test_validates_dynamic_media_loaders_autogrow_links_widgets_and_models(self):
        report = self.validate(self.graph())

        self.assertEqual(report.node_count, 9)
        self.assertEqual(report.link_count, 8)
        self.assertIn("LoadVideo", report.class_types)
        self.assertIn("GetVideoComponents", report.class_types)
        self.assertEqual(len(report.schema_sha256), 64)

    def test_rejects_missing_fixed_model_and_unknown_widget_enum(self):
        graph = self.graph()
        graph["1"]["inputs"]["unet_name"] = "not-installed.safetensors"
        graph["9"]["inputs"]["ref_image_size"] = "largest"

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        detail = str(caught.exception)
        self.assertNotIn("not-installed.safetensors", detail)
        self.assertNotIn("largest", detail)
        self.assertIn("实时 /object_info 受控枚举", detail)

    def test_rejects_invalid_source_slot_and_link_type(self):
        graph = self.graph()
        graph["9"]["inputs"]["ref_images.ref_image_0"] = ["7", 1]
        graph["9"]["inputs"]["ref_audios.ref_audio_0"] = ["7", 99]

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        detail = str(caught.exception)
        self.assertIn("需要 IMAGE", detail)
        self.assertIn("输出 AUDIO", detail)
        self.assertIn("仅有 4 个输出", detail)

    def test_rejects_dependency_cycle_before_submission(self):
        graph = self.graph()
        graph["7"]["inputs"]["video"] = ["7", 0]

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        self.assertIn("依赖环", str(caught.exception))

    def test_accepts_sparse_optional_autogrow_slot_and_rejects_out_of_range(self):
        graph = self.graph()
        del graph["9"]["inputs"]["ref_images.ref_image_0"]
        graph["9"]["inputs"]["ref_images.ref_image_2"] = ["5", 0]

        report = self.validate(graph)
        self.assertEqual(report.node_count, 9)

        graph["9"]["inputs"]["ref_images.ref_image_9"] = ["5", 0]

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        detail = str(caught.exception)
        self.assertIn("超过 autogrow 最大槽位 8", detail)

    def test_autogrow_minimum_requires_the_first_declared_slots(self):
        object_info = self.object_info()
        group = object_info["MiniMaxH3ReferenceToVideo"]["input"]["optional"]["ref_images"]
        group[1]["template"]["min"] = 1
        graph = self.graph()
        del graph["9"]["inputs"]["ref_images.ref_image_0"]
        graph["9"]["inputs"]["ref_images.ref_image_2"] = ["5", 0]

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph, object_info)

        self.assertIn("ref_images.ref_image_0", str(caught.exception))

    def test_rejects_required_input_numeric_range_and_unstaged_placeholder(self):
        graph = self.graph()
        del graph["9"]["inputs"]["clip"]
        graph["9"]["inputs"]["width"] = 16
        graph["6"]["inputs"]["file"] = "__AUTOPROMPT_ASSET__:pending"

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        detail = str(caught.exception)
        self.assertIn("clip 缺少必填输入", detail)
        self.assertIn("不能小于 32", detail)
        self.assertIn("未上传的素材占位符", detail)

    def test_numeric_step_is_ui_hint_not_api_rejection(self):
        graph = self.graph()
        graph["9"]["inputs"]["width"] = 620

        report = self.validate(graph)

        self.assertEqual(report.node_count, 9)

    def test_upload_loader_requires_live_choice_or_confirmed_task_upload(self):
        graph = self.graph()
        graph["6"]["inputs"]["file"] = "autoprompt/another-task/unknown.mp4"

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(graph)

        self.assertIn("本任务已确认的上传结果", str(caught.exception))

    def test_source_canvas_structural_mode_accepts_only_safe_unverified_upload_names(self):
        graph = self.graph()
        graph["6"]["inputs"]["file"] = "legacy/source-video.mp4"

        report = validate_comfyui_api_contract(
            graph,
            self.object_info(),
            uploaded_paths=self.CONFIRMED_UPLOADS,
            allow_unverified_upload_names=True,
        )

        self.assertEqual(report.node_count, 9)
        graph["6"]["inputs"]["file"] = "../outside.mp4"
        with self.assertRaises(ComfyUIContractError) as caught:
            validate_comfyui_api_contract(
                graph,
                self.object_info(),
                uploaded_paths=self.CONFIRMED_UPLOADS,
                allow_unverified_upload_names=True,
            )
        self.assertIn("安全相对路径", str(caught.exception))

    def test_rejects_dynamic_node_missing_from_live_server(self):
        object_info = self.object_info()
        del object_info["LoadAudio"]

        with self.assertRaises(ComfyUIContractError) as caught:
            self.validate(self.graph(), object_info)

        self.assertIn("LoadAudio", str(caught.exception))
        self.assertIn("未安装", str(caught.exception))

    def test_live_schema_unavailable_never_posts_or_creates_false_contract(self):
        client = _OfflineClient()

        with self.assertRaises(ComfyUIContractUnavailable) as caught:
            preflight_comfyui_workflow(client, self.graph())

        self.assertFalse(client.posted)
        self.assertIn("/object_info", str(caught.exception))
        self.assertIn("GPU 未提交", str(caught.exception))

    def test_validation_is_read_only(self):
        graph = self.graph()
        before = deepcopy(graph)

        self.validate(graph)

        self.assertEqual(graph, before)

    def test_sdxl_text_to_image_graph_passes_live_shape_contract(self):
        report = validate_comfyui_api_contract(
            self.sdxl_graph(), self.sdxl_object_info(), uploaded_paths=set(),
        )

        self.assertEqual(report.node_count, 7)
        self.assertEqual(report.link_count, 9)
        self.assertEqual(report.class_types, (
            "CLIPTextEncode", "CheckpointLoaderSimple", "EmptyLatentImage",
            "KSampler", "SaveImage", "VAEDecode",
        ))

    def test_sdxl_contract_rejects_checkpoint_not_in_live_enum(self):
        graph = self.sdxl_graph()
        graph["1"]["inputs"]["ckpt_name"] = "missing.safetensors"

        with self.assertRaises(ComfyUIContractError) as caught:
            validate_comfyui_api_contract(
                graph, self.sdxl_object_info(), uploaded_paths=set(),
            )

        self.assertNotIn("missing.safetensors", str(caught.exception))
        self.assertIn("实时 /object_info 受控枚举", str(caught.exception))
