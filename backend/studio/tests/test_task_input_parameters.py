import json
from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.serializers import GenerationTaskSerializer
from studio.tests.factories import build_story


class GenerationTaskInputParametersTests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot1,
            self.shot2,
        ) = build_story("task-input-parameters")
        self.video_asset_id = uuid4()
        self.paired_audio_asset_id = uuid4()
        self.audio_asset_id = uuid4()
        self.prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=None,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model="minimax-h3-prompt-fallback",
            h3_mode="ref2va",
            is_multi_shot=True,
            include_audio=True,
            total_duration_ms=5600,
            prompt_text="stable prompt",
            structured_payload={
                "prompt_template_key": "ref2va:classified:v1",
                "reference_bindings": [
                    {
                        "asset_id": str(self.character.anchor_asset_id),
                        "category": "character",
                        "input_label": "<Picture 1>",
                        "local_path": "C:/private/character.png",
                    },
                ],
            },
            request_options={
                "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            },
            content_hash="i" * 64,
        )
        self.template = WorkflowTemplate.objects.create(
            name="task-input-template",
            slug="task-input-template",
            output_kind=PromptArtifact.PromptType.VIDEO,
            model="minimax-h3-template-fallback",
            is_system=True,
            definition={"1": {"class_type": "SaveVideo", "inputs": {}}},
            input_schema={
                "workflow_mode": "reference_to_video",
                "source_workflow": "official/video_minimax_h3_r2v.json",
                "lora_model": (
                    "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\"
                    "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
                ),
                "user_parameters": {
                    "STEPS": {"type": "integer", "default": 10},
                    "LORA_STRENGTH": {"type": "number", "default": 1.0},
                },
            },
        )
        self.workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=self.prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            injected_values={
                "MODEL": "injected-model",
                "REQUESTED_H3_MODE": "ref2va",
                "WORKFLOW_MODE": "reference_to_video",
                "WORKFLOW_TEMPLATE_SLUG": self.template.slug,
                "SOURCE_WORKFLOW": "official/video_minimax_h3_r2v.json",
                "PROMPT_TEMPLATE_KEY": "ref2va:classified:v1",
                "RESOLUTION_PROFILE": "0.3",
                "WIDTH": 416,
                "HEIGHT": 736,
                "RESOLUTION_MULTIPLE": 32,
                "FPS": 24,
                "REQUESTED_DURATION_MS": 5600,
                "EFFECTIVE_DURATION_MS": 5667,
                "FRAMES": 136,
                "SEED": 42,
                "AUDIO_ENABLED": True,
                "STEPS": 12,
                "LORA_STRENGTH": 0.8,
            },
            content_hash="w" * 64,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_task(self, *, capability_snapshot: dict) -> GenerationTask:
        return GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=self.workflow,
            kind=GenerationTask.Kind.VIDEO,
            backend="comfy_local_h3",
            model_profile="task-model-profile",
            workflow_version=self.template.version,
            seed=9001,
            effective_frames=222,
            effective_duration_ms=9250,
            capability_snapshot=capability_snapshot,
        )

    def test_serializer_prefers_capability_snapshot_and_returns_path_free_reference_summary(self):
        capability = {
            "model": "snapshot-model",
            "model_profile": "snapshot-profile",
            "h3_mode": "fl2va",
            "effective_conditioning_mode": "i2va",
            "workflow_mode": "snapshot-reference-to-video",
            "template_slug": "snapshot-template",
            "source_workflow": "video_minimax_h3_r2v.json",
            "prompt_template_key": "snapshot-template-key",
            "resolution_profile": "0.8",
            "width": 672,
            "height": 1216,
            "resolution_multiple": 16,
            "fps": 30,
            "requested_duration_ms": 7000,
            "effective_duration_ms": 7033,
            "effective_frames": 211,
            "seed": 123456,
            "include_audio": False,
            "workflow_parameters": {
                "STEPS": 7,
                "LORA_STRENGTH": 1.25,
                "UNDECLARED": "C:/private/must-not-leak.safetensors",
            },
            "shot_count": 2,
            "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            "batch_id": str(uuid4()),
            "batch_kind": "episode",
            "batch_role": "child",
            "batch_index": 1,
            "batch_size": 2,
            "idempotency_key": "episode-1-v1",
            "reference_assets": [
                {
                    "asset_id": str(self.character.anchor_asset_id),
                    "name": "林默标准像",
                    "category": "character",
                    "input_slot": 1,
                    "input_label": "<Picture 1>",
                    "entity_id": str(self.character.id),
                    "entity_name": self.character.name,
                    "checksum": "character-checksum",
                    "local_path": "C:/private/character.png",
                },
                {
                    "asset_id": str(self.video_asset_id),
                    "name": "动作参考",
                    "category": "video",
                    "input_slot": 1,
                    "input_label": "<Video 1>",
                    "audio_asset_id": str(self.paired_audio_asset_id),
                    "audio_input_label": "<Audio 1>",
                    "audio_name": "参考音轨",
                    "audio_local_path": "E:/secret/reference.wav",
                    "local_path": "E:/secret/reference.mp4",
                    "media_metadata": {
                        "source_fps": 29.97003,
                        "ref2va_effective_fps": 24,
                    },
                },
                {
                    "asset_id": str(self.audio_asset_id),
                    "name": "环境声",
                    "category": "audio",
                    "input_slot": 1,
                    "input_label": "<Audio 2>",
                    "local_path": "/srv/private/ambience.wav",
                },
            ],
        }
        task = self.create_task(capability_snapshot=capability)
        task.staged_inputs = [
            {
                "asset_id": str(self.video_asset_id),
                "token": f"__AUTOPROMPT_ASSET__:{self.video_asset_id}",
                "comfy_path": "autoprompt/task/reference.mp4",
                "media_role": "reference_video",
                "source_fps": 29.97003,
                "effective_fps": 24,
                "effective_duration_ms": 5042,
                "cfr_guaranteed": True,
            },
            {
                "asset_id": str(self.paired_audio_asset_id),
                "token": f"__AUTOPROMPT_ASSET__:{self.paired_audio_asset_id}",
                "comfy_path": "autoprompt/task/reference.wav",
                "media_role": "paired_reference_audio",
                "paired_video_asset_id": str(self.video_asset_id),
                "duration_aligned": True,
            },
        ]
        task.save(update_fields=("staged_inputs", "updated_at"))

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["model"], "snapshot-model")
        self.assertEqual(parameters["model_profile"], "snapshot-profile")
        self.assertEqual(parameters["h3_mode"], "fl2va")
        self.assertEqual(parameters["effective_conditioning_mode"], "i2va")
        self.assertEqual(parameters["workflow_mode"], "snapshot-reference-to-video")
        self.assertEqual(parameters["resolution_profile"], "0.8")
        self.assertEqual(parameters["width"], 672)
        self.assertEqual(parameters["height"], 1216)
        self.assertEqual(parameters["resolution_multiple"], 16)
        self.assertEqual(parameters["fps"], 30)
        self.assertEqual(parameters["requested_duration_ms"], 7000)
        self.assertEqual(parameters["effective_duration_ms"], 7033)
        self.assertEqual(parameters["effective_frames"], 211)
        self.assertEqual(parameters["seed"], 123456)
        self.assertFalse(parameters["include_audio"])
        self.assertEqual(parameters["workflow_parameters"], {
            "STEPS": 7,
            "LORA_STRENGTH": 1.25,
            "LORA_MODEL": (
                "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\"
                "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
            ),
        })
        self.assertEqual(parameters["reference_asset_count"], 3)
        self.assertEqual(parameters["reference_input_count"], 4)
        self.assertEqual(parameters["reference_image_count"], 1)
        self.assertEqual(parameters["reference_video_count"], 1)
        self.assertEqual(parameters["reference_audio_count"], 2)
        video_summary = next(
            item for item in parameters["reference_assets"] if item["category"] == "video"
        )
        self.assertAlmostEqual(video_summary["source_fps"], 29.97003, places=5)
        self.assertEqual(video_summary["effective_fps"], 24)
        self.assertEqual(video_summary["effective_duration_ms"], 5042)
        self.assertTrue(video_summary["cfr_guaranteed"])
        self.assertTrue(video_summary["paired_audio_duration_aligned"])
        self.assertEqual(parameters["continuity_assets"], [])
        self.assertEqual(parameters["continuity_asset_count"], 0)
        self.assertEqual(parameters["shot_count"], 2)
        self.assertTrue(parameters["whole_episode"])
        self.assertEqual(parameters["batch_index"], 1)
        self.assertEqual(parameters["batch_size"], 2)
        encoded = json.dumps(parameters, ensure_ascii=False)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/secret", encoded)
        self.assertNotIn("/srv/private", encoded)

    def test_detail_api_backfills_historical_task_without_capability_fields(self):
        self.workflow.injected_values = {
            **self.workflow.injected_values,
            "MODEL": "C:/models/minimax_h3.safetensors",
            "SOURCE_WORKFLOW": "E:/ComfyUI/workflows/video_minimax_h3_r2v.json",
            "AUDIO_ENABLED": False,
            "REFERENCE_BINDINGS": [
                {
                    "asset_id": str(self.character.anchor_asset_id),
                    "name": "林默标准像",
                    "category": "character",
                    "input_slot": 1,
                    "input_label": "<Picture 1>",
                    "local_path": "C:/private/history/character.png",
                },
            ],
        }
        self.workflow.save(update_fields=("injected_values", "updated_at"))
        task = self.create_task(capability_snapshot={
            "resolution_profile": "0.2",
            # Early tasks persisted an empty placeholder before the worker
            # captured REFERENCE_BINDINGS in injected_values.
            "reference_assets": [],
        })

        response = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        parameters = response.data["input_parameters"]
        self.assertEqual(parameters["model"], "minimax_h3.safetensors")
        self.assertEqual(parameters["model_profile"], "task-model-profile")
        self.assertEqual(parameters["h3_mode"], "ref2va")
        self.assertEqual(parameters["effective_conditioning_mode"], "ref2va")
        self.assertEqual(parameters["workflow_mode"], "reference_to_video")
        self.assertEqual(parameters["template_slug"], self.template.slug)
        self.assertEqual(parameters["source_workflow"], "video_minimax_h3_r2v.json")
        self.assertEqual(parameters["prompt_template_key"], "ref2va:classified:v1")
        self.assertEqual(parameters["resolution_profile"], "0.2")
        self.assertEqual(parameters["width"], 416)
        self.assertEqual(parameters["height"], 736)
        self.assertEqual(parameters["resolution_multiple"], 32)
        self.assertEqual(parameters["fps"], 24)
        self.assertEqual(parameters["requested_duration_ms"], 5600)
        self.assertEqual(parameters["effective_duration_ms"], 5667)
        self.assertEqual(parameters["effective_frames"], 136)
        self.assertEqual(parameters["seed"], 42)
        self.assertFalse(parameters["include_audio"])
        self.assertEqual(parameters["reference_asset_count"], 1)
        self.assertEqual(parameters["reference_input_count"], 1)
        self.assertEqual(parameters["reference_image_count"], 1)
        self.assertEqual(parameters["reference_video_count"], 0)
        self.assertEqual(parameters["reference_audio_count"], 0)
        self.assertEqual(parameters["continuity_assets"], [])
        self.assertEqual(parameters["continuity_asset_count"], 0)
        self.assertEqual(parameters["shot_count"], 2)
        self.assertEqual(parameters["shot_ids"], [str(self.shot1.id), str(self.shot2.id)])
        self.assertIsNone(parameters["batch_id"])
        self.assertIsNone(parameters["batch_index"])
        self.assertIsNone(parameters["batch_size"])
        self.assertFalse(parameters["whole_episode"])
        encoded = json.dumps(parameters, ensure_ascii=False)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("C:/models", encoded)
        self.assertNotIn("E:/ComfyUI", encoded)
        self.assertNotIn("C:/private", encoded)

    def test_historical_t2va_dirty_snapshot_reports_continuity_assets_not_inputs(self):
        self.prompt.h3_mode = "T2VA"
        self.prompt.structured_payload = {"prompt_template_key": "h3_t2v_text_v1"}
        self.prompt.save(update_fields=("h3_mode", "structured_payload", "updated_at"))
        self.template.input_schema = {
            **self.template.input_schema,
            "workflow_mode": "text_to_video",
        }
        self.template.save(update_fields=("input_schema", "updated_at"))
        # Reproduce early generated workflows which did not persist the newer
        # conditioning-mode fields, but did carry Bible material as legacy
        # metadata even though the graph had no reference input nodes.
        self.workflow.injected_values = {
            key: value
            for key, value in self.workflow.injected_values.items()
            if key not in {
                "REQUESTED_H3_MODE",
                "H3_MODE",
                "EFFECTIVE_CONDITIONING_MODE",
                "WORKFLOW_MODE",
            }
        }
        self.workflow.save(update_fields=("injected_values", "updated_at"))
        dirty_assets = [
            {
                "asset_id": str(self.character.anchor_asset_id),
                "name": "Character anchor",
                "category": "character",
                "input_label": "<Picture 1>",
                "local_path": "C:/private/character.png",
            },
            {
                "asset_id": str(uuid4()),
                "name": "Inn scene",
                "category": "scene",
                "input_label": "<Picture 2>",
                "local_path": "E:/private/inn.png",
            },
            {
                "asset_id": str(uuid4()),
                "name": "Sword prop",
                "category": "prop",
                "input_label": "<Picture 3>",
                "local_path": "/srv/private/sword.png",
            },
        ]
        task = self.create_task(capability_snapshot={
            "h3_mode": "T2VA",
            "reference_assets": dirty_assets,
        })

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["h3_mode"], "T2VA")
        self.assertEqual(parameters["effective_conditioning_mode"], "T2VA")
        self.assertEqual(parameters["workflow_mode"], "text_to_video")
        self.assertEqual(parameters["reference_assets"], [])
        self.assertEqual(parameters["reference_asset_count"], 0)
        self.assertEqual(parameters["reference_input_count"], 0)
        self.assertEqual(parameters["reference_image_count"], 0)
        self.assertEqual(parameters["reference_video_count"], 0)
        self.assertEqual(parameters["reference_audio_count"], 0)
        self.assertEqual(parameters["continuity_asset_count"], 3)
        self.assertEqual(
            [item["display_name"] for item in parameters["continuity_assets"]],
            ["Character anchor", "Inn scene", "Sword prop"],
        )
        encoded = json.dumps(parameters, ensure_ascii=False)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)
        self.assertNotIn("/srv/private", encoded)

    def test_effective_t2va_alone_reclassifies_legacy_references_as_continuity(self):
        task = self.create_task(capability_snapshot={
            "h3_mode": "FL2VA",
            "effective_conditioning_mode": "t2va",
            "workflow_mode": "first_last_to_video",
            "reference_assets": [{
                "asset_id": str(self.character.anchor_asset_id),
                "name": "Legacy Bible anchor",
                "category": "character",
                "input_label": "<Picture 1>",
            }],
        })

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["reference_assets"], [])
        self.assertEqual(parameters["reference_asset_count"], 0)
        self.assertEqual(parameters["reference_input_count"], 0)
        self.assertEqual(parameters["continuity_asset_count"], 1)

    def test_historical_image_task_hides_video_only_parameters(self):
        self.prompt.prompt_type = PromptArtifact.PromptType.IMAGE
        self.prompt.h3_mode = ""
        self.prompt.total_duration_ms = 0
        self.prompt.include_audio = False
        self.prompt.structured_payload = {
            "effective_conditioning_mode": "TEXT_ONLY",
            "prompt_template_key": "sdxl_txt2img_bible_v1",
        }
        self.prompt.save(update_fields=(
            "prompt_type", "h3_mode", "total_duration_ms", "include_audio",
            "structured_payload", "updated_at",
        ))
        self.template.output_kind = PromptArtifact.PromptType.IMAGE
        self.template.input_schema = {
            **self.template.input_schema,
            "workflow_mode": "text_to_image",
        }
        self.template.save(update_fields=("output_kind", "input_schema", "updated_at"))
        task = self.create_task(capability_snapshot={
            "effective_conditioning_mode": "TEXT_ONLY",
            "prompt_template_key": "sdxl_txt2img_bible_v1",
            "workflow_mode": "text_to_image",
            # Historical image tasks could persist these technical graph
            # defaults even though they were not image-generation controls.
            "fps": 24,
            "requested_duration_ms": 5000,
            "include_audio": False,
        })
        task.kind = GenerationTask.Kind.IMAGE
        task.save(update_fields=("kind", "updated_at"))

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["effective_conditioning_mode"], "TEXT_ONLY")
        self.assertEqual(parameters["prompt_template_key"], "sdxl_txt2img_bible_v1")
        for video_only_key in (
            "fps", "requested_duration_ms", "effective_duration_ms", "effective_frames",
            "include_audio",
        ):
            self.assertNotIn(video_only_key, parameters)

    def test_conditioned_modes_keep_their_actual_reference_inputs(self):
        cases = (
            ("I2VA", "I2VA", "first_last_to_video"),
            ("L2VA", "L2VA", "first_last_to_video"),
            ("FL2VA", "FL2VA", "first_last_to_video"),
            ("Ref2VA", "Ref2VA", "reference_to_video"),
        )
        for h3_mode, effective_mode, workflow_mode in cases:
            with self.subTest(effective_mode=effective_mode):
                task = self.create_task(capability_snapshot={
                    "h3_mode": h3_mode,
                    "effective_conditioning_mode": effective_mode,
                    "workflow_mode": workflow_mode,
                    "reference_assets": [{
                        "asset_id": str(self.character.anchor_asset_id),
                        "name": f"{effective_mode} input",
                        "category": "character",
                        "input_label": "<Picture 1>",
                    }],
                })

                parameters = GenerationTaskSerializer(task).data["input_parameters"]

                self.assertEqual(parameters["reference_asset_count"], 1)
                self.assertEqual(parameters["reference_input_count"], 1)
                self.assertEqual(parameters["reference_image_count"], 1)
                self.assertEqual(parameters["continuity_assets"], [])
                self.assertEqual(parameters["continuity_asset_count"], 0)
