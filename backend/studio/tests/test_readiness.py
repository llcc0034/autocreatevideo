from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import WorkerLease
from studio.services.comfyui import ComfyUIError
from studio.services.readiness import (
    REQUIRED_COMFYUI_MODELS,
    REQUIRED_COMFYUI_NODES,
    REVIEWED_COMFYUI_CONTRACT_PROBES,
    collect_readiness,
)
from studio.services.worker_heartbeat import touch_worker_heartbeat


class ReadinessTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        WorkerLease.objects.create(key="generation-worker-heartbeat")
        WorkerLease.objects.create(key="assembly-worker-heartbeat")
        self.runtime_settings = override_settings(
            COMFYUI_OUTPUT_DIR=self.temp_dir.name,
            FFMPEG_BINARY="ffmpeg-test",
            FFPROBE_BINARY="ffprobe-test",
        )
        self.runtime_settings.enable()
        self.addCleanup(self.runtime_settings.disable)
        self.binary_patch = patch(
            "studio.services.readiness._binary_available", return_value=True,
        )
        self.binary_patch.start()
        self.addCleanup(self.binary_patch.stop)

    @staticmethod
    def _healthy_comfyui_payload():
        return {
            "system": {"comfyui_version": "0.3.76"},
            "devices": [{"name": "NVIDIA GeForce RTX 4090", "type": "cuda"}],
        }

    @staticmethod
    def _healthy_object_info():
        # Reuse the contract-test fixtures for the complex Ref2VA autogrow and
        # SDXL shapes, then add the remaining reviewed H3 sampler/video nodes.
        from studio.tests.test_comfyui_contract import ComfyUIContractTests

        fixture = ComfyUIContractTests(methodName="runTest")
        info = fixture.object_info()
        info.update(fixture.sdxl_object_info())
        info["UNETLoader"]["input"]["required"]["unet_name"][0] = list(
            REQUIRED_COMFYUI_MODELS["diffusion_models"]
        )
        info["CLIPLoader"]["input"]["required"]["clip_name"][0] = list(
            REQUIRED_COMFYUI_MODELS["text_encoders"]
        )
        info["VAELoader"]["input"]["required"]["vae_name"][0] = list(
            REQUIRED_COMFYUI_MODELS["vae"]
        )
        info["MiniMaxH3ImageToVideo"] = {
            "input": {
                "required": {
                    "clip": ["CLIP", {}],
                    "vae": ["VAE", {}],
                    "prompt": ["STRING", {"multiline": True}],
                    "width": ["INT", {"min": 32, "max": 16384}],
                    "height": ["INT", {"min": 32, "max": 16384}],
                    "length": ["INT", {"min": 5, "max": 3600}],
                },
                "optional": {
                    "first_frame": ["IMAGE", {}],
                    "last_frame": ["IMAGE", {}],
                },
            },
            "output": ["CONDITIONING", "LATENT"],
        }
        info.update({
            "LoraLoaderModelOnly": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "lora_name": [list(REQUIRED_COMFYUI_MODELS["loras"])],
                    "strength_model": ["FLOAT", {
                        "default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01,
                    }],
                }},
                "output": ["MODEL"],
            },
            "LoraLoaderBypassModelOnly": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "lora_name": [list(REQUIRED_COMFYUI_MODELS["loras"])],
                    "strength_model": ["FLOAT", {
                        "default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01,
                    }],
                }},
                "output": ["MODEL"],
            },
            "MiniMaxH3AudioConditioningT8": {
                "input": {
                    "required": {
                        "clip": ["CLIP", {}],
                        "video_vae": ["VAE", {}],
                        "audio_vae": ["VAE", {}],
                        "prompt": ["STRING", {"multiline": True}],
                        "width": ["INT", {"min": 32, "max": 16384, "step": 32}],
                        "height": ["INT", {"min": 32, "max": 16384, "step": 32}],
                        "length": ["INT", {"min": 5, "max": 3600, "step": 17}],
                        "task_type": [[
                            "auto", "T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA", "Hybrid",
                        ]],
                        "audio_mode": [[
                            "lock_source", "remix_source", "reference_only", "native",
                        ]],
                        "audio_denoise_strength": ["FLOAT", {
                            "min": 0.0, "max": 1.0, "step": 0.01,
                        }],
                        "add_source_as_reference": ["BOOLEAN", {}],
                        "prompt_primary_audio_ordinal": ["INT", {"min": 0, "max": 9}],
                        "strict_prompt_tags": ["BOOLEAN", {}],
                        "ref_image_size": [["match", "max"]],
                        "reference_video_policy": [["official_2_to_15s", "model_minimum"]],
                    },
                    "optional": {
                        "drive_audio": ["AUDIO", {}],
                        "final_audio": ["AUDIO", {}],
                        "first_frame": ["IMAGE", {}],
                        "last_frame": ["IMAGE", {}],
                        "ref_images": fixture.autogrow("ref_image_", "ref_image", "IMAGE", 9),
                        "ref_videos": fixture.autogrow("ref_video_", "ref_video", "IMAGE", 3),
                        "ref_video_audios": fixture.autogrow(
                            "ref_video_audio_", "ref_video_audio", "AUDIO", 3,
                        ),
                        "ref_audios": fixture.autogrow("ref_audio_", "ref_audio", "AUDIO", 3),
                    },
                },
                "output": ["CONDITIONING", "LATENT", "AUDIO", "STRING", "STRING", "STRING"],
            },
            "MiniMaxH3DualClockSamplerT8": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "av_latent": ["LATENT", {}],
                    "steps": ["INT", {"min": 1, "max": 1000}],
                    "shift_video": ["FLOAT", {"min": 0.01, "max": 100.0, "step": 0.01}],
                    "shift_audio": ["FLOAT", {"min": 0.01, "max": 100.0, "step": 0.01}],
                }},
                "output": ["MODEL", "SAMPLER", "SIGMAS"],
            },
            "MiniMaxH3AVDecodeT8": {
                "input": {"required": {
                    "av_latent": ["LATENT", {}],
                    "video_vae": ["VAE", {}],
                    "audio_vae": ["VAE", {}],
                }},
                "output": ["IMAGE", "AUDIO", "LATENT", "LATENT"],
            },
            "RandomNoise": {
                "input": {"required": {
                    "noise_seed": ["INT", {"min": 0, "max": 18446744073709551615}],
                }},
                "output": ["NOISE"],
            },
            "BasicGuider": {
                "input": {"required": {
                    "model": ["MODEL", {}], "conditioning": ["CONDITIONING", {}],
                }},
                "output": ["GUIDER"],
            },
            "KSamplerSelect": {
                "input": {"required": {"sampler_name": [["res_multistep"]]}},
                "output": ["SAMPLER"],
            },
            "BasicScheduler": {
                "input": {"required": {
                    "model": ["MODEL", {}],
                    "scheduler": [["simple", "karras"]],
                    "steps": ["INT", {"min": 1, "max": 10000}],
                    "denoise": ["FLOAT", {"min": 0.0, "max": 1.0}],
                }},
                "output": ["SIGMAS"],
            },
            "SamplerCustomAdvanced": {
                "input": {"required": {
                    "noise": ["NOISE", {}],
                    "guider": ["GUIDER", {}],
                    "sampler": ["SAMPLER", {}],
                    "sigmas": ["SIGMAS", {}],
                    "latent_image": ["LATENT", {}],
                }},
                "output": ["LATENT", "LATENT"],
            },
            "VAEDecodeAudio": {
                "input": {"required": {
                    "samples": ["LATENT", {}], "vae": ["VAE", {}],
                }},
                "output": ["AUDIO"],
            },
            "CreateVideo": {
                "input": {
                    "required": {
                        "images": ["IMAGE", {}],
                        "fps": ["FLOAT", {"min": 1.0, "max": 120.0}],
                    },
                    "optional": {
                        "audio": ["AUDIO", {}],
                        "bit_depth": ["INT", {"min": 8, "max": 10}],
                    },
                },
                "output": ["VIDEO"],
            },
            "SaveVideo": {
                "input": {"required": {
                    "video": ["VIDEO", {}],
                    "filename_prefix": ["STRING", {}],
                    "format": [["auto", "mp4"]],
                    "codec": ["COMFY_DYNAMICCOMBO_V3", {
                        "options": [{"key": "auto"}, {"key": "h264"}],
                    }],
                }},
                "output": ["VIDEO"],
            },
        })
        return info

    @classmethod
    def _configure_healthy_comfyui_client(cls, client_factory):
        client = client_factory.return_value
        client.health.return_value = cls._healthy_comfyui_payload()
        client.object_info.return_value = cls._healthy_object_info()
        client.models.side_effect = lambda folder: list(REQUIRED_COMFYUI_MODELS[folder])
        return client

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_WORKER_MAX_AGE_SECONDS=30,
        READINESS_GENERATION_WORKER_REQUIRED=True,
        READINESS_ASSEMBLY_WORKER_REQUIRED=True,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_all_required_dependencies_report_ready(self, client_factory):
        self._configure_healthy_comfyui_client(client_factory)
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "ready")
        self.assertTrue(all(item["ok"] for item in payload["checks"].values()))
        client_factory.assert_called_once_with(timeout=2.0)

    def test_required_nodes_are_derived_from_all_six_reviewed_capability_probes(self):
        self.assertEqual(set(REVIEWED_COMFYUI_CONTRACT_PROBES), {
            "h3_text_to_video",
            "h3_first_last_to_video",
            "h3_reference_to_video",
            "h3_reference_to_video_t8_dual_clock_4step",
            "h3_reference_to_video_turbo_lora",
            "sdxl_text_to_image",
        })
        self.assertTrue({
            "BasicGuider", "BasicScheduler", "KSamplerSelect", "RandomNoise",
            "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio",
            "LoadImage", "LoadVideo", "GetVideoComponents", "LoadAudio",
            "LoraLoaderModelOnly", "LoraLoaderBypassModelOnly",
            "MiniMaxH3AudioConditioningT8", "MiniMaxH3DualClockSamplerT8",
            "MiniMaxH3AVDecodeT8",
        }.issubset(REQUIRED_COMFYUI_NODES))
        self.assertEqual(len(REQUIRED_COMFYUI_MODELS["loras"]), 1)
        first_last = REVIEWED_COMFYUI_CONTRACT_PROBES["h3_first_last_to_video"]
        reference = REVIEWED_COMFYUI_CONTRACT_PROBES["h3_reference_to_video"]
        self.assertEqual(
            sum(node["class_type"] == "LoadImage" for node in first_last.values()), 2,
        )
        self.assertEqual(
            sum(node["class_type"] == "LoadImage" for node in reference.values()), 9,
        )
        self.assertEqual(
            sum(node["class_type"] == "LoadVideo" for node in reference.values()), 3,
        )
        self.assertEqual(
            sum(node["class_type"] == "LoadAudio" for node in reference.values()), 3,
        )

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_cpu_only_is_not_ready_but_mixed_devices_with_cuda_are_ready(self, client_factory):
        client = self._configure_healthy_comfyui_client(client_factory)
        client.health.return_value = {
            "system": {"comfyui_version": "0.3.76"},
            "devices": [{"name": "AMD64 CPU", "type": "cpu"}],
        }
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            cpu_only = collect_readiness()
        self.assertEqual(cpu_only["status"], "not_ready")
        self.assertEqual(cpu_only["checks"]["comfyui"]["cuda_device_count"], 0)

        self._configure_healthy_comfyui_client(client_factory)
        client.health.return_value = {
            "system": {"comfyui_version": "0.3.76"},
            "devices": [
                {"name": "AMD64 CPU", "type": "cpu"},
                {"name": "NVIDIA GeForce RTX 4090", "type": "cuda"},
            ],
        }
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            mixed = collect_readiness()
        self.assertEqual(mixed["status"], "ready")
        self.assertEqual(mixed["checks"]["comfyui"]["device_count"], 2)
        self.assertEqual(mixed["checks"]["comfyui"]["cuda_device_count"], 1)

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_live_node_port_or_output_drift_is_not_ready(self, client_factory):
        client = self._configure_healthy_comfyui_client(client_factory)
        del client.object_info.return_value["BasicGuider"]["input"]["required"]["conditioning"]
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            missing_port = collect_readiness()
        comfy = missing_port["checks"]["comfyui"]
        self.assertEqual(missing_port["status"], "not_ready")
        self.assertFalse(comfy["missing_nodes"])
        self.assertIn("h3_text_to_video", comfy["invalid_contracts"])

        client = self._configure_healthy_comfyui_client(client_factory)
        client.object_info.return_value["GetVideoComponents"]["output"] = ["IMAGE"]
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            wrong_output = collect_readiness()
        self.assertEqual(wrong_output["status"], "not_ready")
        self.assertIn(
            "h3_reference_to_video",
            wrong_output["checks"]["comfyui"]["invalid_contracts"],
        )

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_fixed_model_enum_and_dynamic_reference_capacity_are_contract_requirements(
        self, client_factory,
    ):
        client = self._configure_healthy_comfyui_client(client_factory)
        enum = client.object_info.return_value["UNETLoader"]["input"]["required"]["unet_name"][0]
        enum.remove("MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors")
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            missing_enum = collect_readiness()
        comfy = missing_enum["checks"]["comfyui"]
        self.assertEqual(missing_enum["status"], "not_ready")
        self.assertFalse(comfy["missing_models"])
        self.assertIn("h3_text_to_video", comfy["invalid_contracts"])

        client = self._configure_healthy_comfyui_client(client_factory)
        ref_schema = client.object_info.return_value["MiniMaxH3ReferenceToVideo"]
        ref_schema["input"]["optional"]["ref_images"][1]["template"]["max"] = 8
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            narrowed = collect_readiness()
        self.assertEqual(narrowed["status"], "not_ready")
        self.assertIn(
            "h3_reference_to_video",
            narrowed["checks"]["comfyui"]["invalid_contracts"],
        )

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_optional_lora_inventory_failure_does_not_block_core_readiness(
        self, client_factory,
    ):
        client = self._configure_healthy_comfyui_client(client_factory)
        healthy_models = client.models.side_effect

        def models(folder):
            if folder == "loras":
                raise ComfyUIError("optional lora inventory unavailable")
            return healthy_models(folder)

        client.models.side_effect = models
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            result = collect_readiness()

        comfy = result["checks"]["comfyui"]
        self.assertEqual(result["status"], "ready")
        self.assertTrue(comfy["ok"])
        self.assertFalse(comfy["optional_workflows_ready"])
        self.assertEqual(comfy["optional_model_errors"], {"loras": "unavailable"})

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=100,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=False,
    )
    def test_comfyui_output_must_meet_the_same_minimum_free_space(self):
        root = Path(self.temp_dir.name)
        media = root / "media"
        output = root / "comfy-output"
        media.mkdir()
        output.mkdir()

        def disk_usage(path):
            return SimpleNamespace(free=1_000 if Path(path) == media else 99)

        with (
            self.settings(MEDIA_ROOT=media, COMFYUI_OUTPUT_DIR=output),
            patch("studio.services.readiness.shutil.disk_usage", side_effect=disk_usage),
        ):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertTrue(payload["checks"]["storage"]["ok"])
        self.assertFalse(payload["checks"]["comfyui_output"]["ok"])
        self.assertEqual(payload["checks"]["comfyui_output"]["free_bytes"], 99)
        self.assertEqual(payload["checks"]["comfyui_output"]["minimum_free_bytes"], 100)

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_missing_required_comfyui_node_or_model_is_not_ready(self, client_factory):
        client = self._configure_healthy_comfyui_client(client_factory)
        from studio.services.readiness import CORE_COMFYUI_NODES

        missing_node = next(iter(CORE_COMFYUI_NODES))
        client.object_info.return_value.pop(missing_node)
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            node_result = collect_readiness()
        self.assertEqual(node_result["status"], "not_ready")
        self.assertIn(missing_node, node_result["checks"]["comfyui"]["missing_nodes"])

        self._configure_healthy_comfyui_client(client_factory)
        missing_folder = "checkpoints"
        client.models.side_effect = lambda folder: (
            [] if folder == missing_folder else list(REQUIRED_COMFYUI_MODELS[folder])
        )
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            model_result = collect_readiness()
        self.assertEqual(model_result["status"], "not_ready")
        self.assertIn(missing_folder, model_result["checks"]["comfyui"]["missing_models"])

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_WORKER_MAX_AGE_SECONDS=10,
        READINESS_COMFYUI_REQUIRED=False,
    )
    def test_stale_worker_heartbeat_makes_service_not_ready(self):
        stale = timezone.now() - timedelta(minutes=5)
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(updated_at=stale)
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["generation_worker"]["ok"])
        self.assertIn("没有新鲜心跳", payload["checks"]["generation_worker"]["message"])

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_comfyui_error_is_sanitized(self, client_factory):
        client_factory.return_value.health.side_effect = ComfyUIError("secret backend detail")
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["checks"]["comfyui"]["message"], "ComfyUI 不可用")
        self.assertNotIn("secret", str(payload))

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_empty_comfyui_object_is_not_ready(self, client_factory):
        client_factory.return_value.health.return_value = {}
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["comfyui"]["ok"])
        self.assertEqual(payload["checks"]["comfyui"]["message"], "ComfyUI 响应无效")

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_comfyui_requires_nonempty_devices_with_name_and_type(self, client_factory):
        invalid_payloads = (
            {"system": {"comfyui_version": "0.3.76"}, "devices": []},
            {"system": {"comfyui_version": "0.3.76"}, "devices": [{}]},
            {"system": {"comfyui_version": "0.3.76"}, "devices": [{"name": "GPU"}]},
            {"system": {"comfyui_version": "0.3.76"}, "devices": [{"type": "cuda"}]},
            {"system": {"comfyui_version": ""}, "devices": [{"name": "GPU", "type": "cuda"}]},
        )
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid):
                client_factory.return_value.health.return_value = invalid
                with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
                    payload = collect_readiness()
                self.assertEqual(payload["status"], "not_ready")
                self.assertFalse(payload["checks"]["comfyui"]["ok"])
                self.assertEqual(payload["checks"]["comfyui"]["message"], "ComfyUI 响应无效")

    def test_worker_heartbeat_touch_creates_and_refreshes_lease(self):
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").delete()
        touch_worker_heartbeat("generation-worker-heartbeat")
        first = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(
            updated_at=first - timedelta(minutes=1),
        )

        touch_worker_heartbeat("generation-worker-heartbeat")

        refreshed = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        self.assertGreaterEqual(refreshed, first)

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=False,
        READINESS_ASSEMBLY_WORKER_REQUIRED=False,
        READINESS_COMFYUI_REQUIRED=False,
    )
    def test_missing_toolchain_or_comfyui_output_directory_is_not_ready(self):
        with (
            patch("studio.services.readiness._binary_available", return_value=False),
            self.settings(MEDIA_ROOT=Path(self.temp_dir.name)),
        ):
            toolchain = collect_readiness()
        self.assertFalse(toolchain["checks"]["toolchain"]["ok"])

        missing_output = Path(self.temp_dir.name) / "missing-comfy-output"
        with self.settings(
            MEDIA_ROOT=Path(self.temp_dir.name),
            COMFYUI_OUTPUT_DIR=missing_output,
        ):
            output = collect_readiness()
        self.assertFalse(output["checks"]["comfyui_output"]["ok"])

    @override_settings(
        READINESS_MIN_DISK_FREE_BYTES=1,
        READINESS_GENERATION_WORKER_REQUIRED=True,
        READINESS_ASSEMBLY_WORKER_REQUIRED=True,
        READINESS_COMFYUI_REQUIRED=True,
    )
    @patch("studio.services.readiness.ComfyUIClient")
    def test_readiness_endpoint_returns_200_only_when_dependencies_are_ready(self, client_factory):
        self._configure_healthy_comfyui_client(client_factory)
        with self.settings(MEDIA_ROOT=Path(self.temp_dir.name)):
            ready = self.client.get("/api/v1/health/ready/")
            WorkerLease.objects.filter(pk="assembly-worker-heartbeat").delete()
            unavailable = self.client.get("/api/v1/health/ready/")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.data["status"], "ready")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.data["status"], "not_ready")
        self.assertFalse(unavailable.data["checks"]["assembly_worker"]["ok"])

    def test_liveness_endpoint_does_not_require_database_workers_or_comfyui(self):
        WorkerLease.objects.all().delete()

        response = self.client.get("/api/v1/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")
