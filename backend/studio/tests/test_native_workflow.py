from django.core.management import call_command
from django.test import TestCase
from uuid import uuid4

from studio.models import PromptArtifact, Shot, WorkflowTemplate
from studio.services.h3_prompts import (
    H3PromptOptions,
    SDXL_BASE_MODEL_ID,
    build_consistent_image_prompt,
    build_h3_video_prompt,
    sync_consistency_locks,
)
from studio.services.system_workflow_contracts import (
    MINIMAX_REFERENCE_IMAGE_DEFINITION_SHA256,
    approved_minimax_reference_image_definition,
    canonical_workflow_sha256,
    is_approved_minimax_reference_image_definition,
)
from studio.services.workflow import WorkflowBuildError, build_workflow
from studio.tests.factories import build_story


class NativeH3WorkflowTests(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        self.user, self.project, _, _, self.episode, self.shot1, self.shot2 = build_story()
        sync_consistency_locks(self.project)

    def _prompt(self, mode="T2VA", include_audio=True, shots=None):
        shots = shots or [self.shot1]
        durations = {str(shot.id): 5000 for shot in shots}
        result = build_h3_video_prompt(
            self.project, shots, H3PromptOptions(
                mode=mode, include_audio=include_audio, duration_overrides=durations,
            )
        )
        return PromptArtifact.objects.create(
            project=self.project, episode=self.episode,
            shot=shots[0] if len(shots) == 1 else None,
            created_by=self.user, prompt_type="video", h3_mode=mode,
            include_audio=include_audio, total_duration_ms=result.total_duration_ms,
            prompt_text=result.text, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
        )

    def _image_prompt(self):
        result = build_consistent_image_prompt(self.project, self.shot1)
        return PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=self.shot1,
            created_by=self.user, prompt_type=PromptArtifact.PromptType.IMAGE,
            model=result.payload["model"], h3_mode="T2VA", include_audio=False,
            total_duration_ms=0, prompt_text=result.text,
            negative_prompt="blurry, duplicate person, watermark",
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )

    def _ref_prompt_with_picture(self):
        prompt = self._prompt(mode=Shot.H3Mode.REF2VA)
        asset_id = str(uuid4())
        entity_id = str(uuid4())
        binding = {
            "category": "character",
            "input_slot": 1,
            "input_label": "<Picture 1>",
            "asset_id": asset_id,
            "entity_id": entity_id,
            "entity_name": "Reference subject",
        }
        prompt.prompt_text = f"{prompt.prompt_text}\n<Picture 1>"
        prompt.structured_payload = {
            **prompt.structured_payload,
            "reference_bindings": [binding],
            "reference_assets": [binding],
        }
        return prompt

    def test_base_graph_is_native_and_frame_aligned(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, values, _ = build_workflow(template, prompt, self.project)
        self.assertEqual(workflow["5"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertEqual(workflow["5"]["inputs"]["length"], 124)
        self.assertEqual(values["EFFECTIVE_DURATION_MS"], 5167)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors")
        self.assertEqual(workflow["9"]["inputs"]["steps"], 20)

    def test_official_h3_workflow_keeps_fixed_steps_and_rejects_override(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, values, _ = build_workflow(template, prompt, self.project)
        self.assertEqual(workflow["9"]["inputs"]["steps"], 20)
        self.assertEqual(values["STEPS"], 20)
        with self.assertRaisesRegex(WorkflowBuildError, "STEPS"):
            build_workflow(template, prompt, self.project, {"STEPS": 4})

    def test_audio_disabled_prunes_decode_and_mux_input(self):
        prompt = self._prompt(include_audio=False)
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, _, _ = build_workflow(template, prompt, self.project)
        self.assertNotIn("12", workflow)
        self.assertNotIn("4", workflow)
        self.assertNotIn("audio", workflow["13"]["inputs"])

    def test_ref_graph_zero_reference_adds_no_load_image_nodes(self):
        prompt = self._prompt(mode=Shot.H3Mode.REF2VA, shots=[self.shot1, self.shot2])
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-reference-video")
        workflow, _, _ = build_workflow(template, prompt, self.project)
        load_nodes = [node for node in workflow.values() if node.get("class_type") == "LoadImage"]
        self.assertEqual(load_nodes, [])

    def test_t8_dual_clock_accepts_bounded_parameters_and_adapts_references(self):
        prompt = self._ref_prompt_with_picture()
        template = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-t8-dual-clock-4step",
        )

        workflow, values, _ = build_workflow(template, prompt, self.project, {
            "STEPS": 7,
            "LORA_STRENGTH": 0.75,
        })

        self.assertEqual(workflow["7"]["inputs"]["steps"], 7)
        self.assertEqual(workflow["2"]["inputs"]["strength_model"], 0.75)
        self.assertEqual(values["STEPS"], 7)
        self.assertEqual(values["LORA_STRENGTH"], 0.75)
        load_id = next(
            node_id for node_id, node in workflow.items()
            if node["class_type"] == "LoadImage"
        )
        self.assertEqual(
            workflow["6"]["inputs"]["ref_images.ref_image_0"], [load_id, 0],
        )

    def test_turbo_lora_accepts_bounded_parameters_and_adapts_references(self):
        prompt = self._ref_prompt_with_picture()
        template = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-turbo-lora",
        )

        workflow, values, _ = build_workflow(template, prompt, self.project, {
            "STEPS": 13,
            "LORA_STRENGTH": 1.25,
        })

        self.assertEqual(workflow["9"]["inputs"]["steps"], 13)
        self.assertEqual(workflow["15"]["inputs"]["strength_model"], 1.25)
        self.assertEqual(workflow["7"]["inputs"]["model"], ["15", 0])
        self.assertEqual(workflow["9"]["inputs"]["model"], ["15", 0])
        self.assertEqual(values["STEPS"], 13)
        self.assertEqual(values["LORA_STRENGTH"], 1.25)
        load_id = next(
            node_id for node_id, node in workflow.items()
            if node["class_type"] == "LoadImage"
        )
        self.assertEqual(
            workflow["5"]["inputs"]["ref_images.ref_image_0"], [load_id, 0],
        )

    def test_workflow_user_parameters_reject_type_bounds_step_and_unknown_keys(self):
        prompt = self._prompt(mode=Shot.H3Mode.REF2VA)
        t8 = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-t8-dual-clock-4step",
        )
        turbo = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-turbo-lora",
        )
        cases = [
            (t8, {"STEPS": 21}, "STEPS"),
            (t8, {"STEPS": 4.0}, "STEPS"),
            (turbo, {"LORA_STRENGTH": 2.01}, "LORA_STRENGTH"),
            (turbo, {"LORA_STRENGTH": 0.03}, "LORA_STRENGTH"),
            (turbo, {"UNREVIEWED_PARAMETER": 1}, "UNREVIEWED_PARAMETER"),
        ]
        for template, overrides, expected in cases:
            with self.subTest(template=template.slug, overrides=overrides):
                with self.assertRaisesRegex(WorkflowBuildError, expected):
                    build_workflow(template, prompt, self.project, overrides)

    def test_template_rejects_wrong_h3_mode(self):
        prompt = self._prompt(mode=Shot.H3Mode.REF2VA)
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        with self.assertRaises(WorkflowBuildError):
            build_workflow(template, prompt, self.project)

    def test_h3_workflow_rejects_unconfigured_4k_override(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        with self.assertRaisesRegex(WorkflowBuildError, "4K"):
            build_workflow(template, prompt, self.project, {"WIDTH": 2160, "HEIGHT": 3840})

    def test_h3_workflow_accepts_non_32_official_resolution_multiple(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, values, _ = build_workflow(template, prompt, self.project, {
            "WIDTH": 420,
            "HEIGHT": 740,
            "RESOLUTION_PROFILE": "0.3",
            "RESOLUTION_MULTIPLE": 20,
        })
        self.assertEqual((workflow["5"]["inputs"]["width"], workflow["5"]["inputs"]["height"]), (420, 740))
        self.assertEqual(values["RESOLUTION_PROFILE"], "0.3")
        self.assertEqual(values["RESOLUTION_MULTIPLE"], 20)

    def test_h3_workflow_rejects_invalid_resolution_multiple(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        with self.assertRaisesRegex(WorkflowBuildError, "divisible by 4"):
            build_workflow(template, prompt, self.project, {"RESOLUTION_MULTIPLE": 18})

    def test_sdxl_image_template_is_active_audited_text_to_image_graph(self):
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-reference-image")
        self.assertTrue(template.is_active)
        self.assertTrue(template.is_system)
        self.assertEqual(template.model, "sd_xl_base_1.0.safetensors")
        self.assertEqual(template.input_schema["model_id"], SDXL_BASE_MODEL_ID)
        self.assertEqual(template.input_schema["workflow_mode"], "text_to_image")
        self.assertFalse(template.input_schema["pixel_reference_inputs"])
        self.assertEqual(template.input_schema["required_dimension_multiple"], 8)
        self.assertEqual(template.input_schema["recommended_resolution_profile"], "0.98")
        self.assertEqual(template.input_schema["recommended_megapixels_min"], 0.8)
        self.assertEqual(
            template.input_schema["low_resolution_smoke_profiles"],
            ["0.1", "0.2", "0.3"],
        )
        self.assertEqual(
            template.input_schema["model_file"]["sha256"],
            "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
        )
        self.assertEqual(
            [node["class_type"] for node in template.definition.values()],
            [
                "CheckpointLoaderSimple", "CLIPTextEncode", "CLIPTextEncode",
                "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage",
            ],
        )
        self.assertNotIn("LoadImage", str(template.definition))
        self.assertNotIn("MiniMaxImage", str(template.definition))
        self.assertTrue(is_approved_minimax_reference_image_definition(
            template.definition,
        ))
        self.assertEqual(
            template.definition, approved_minimax_reference_image_definition(),
        )
        self.assertEqual(
            canonical_workflow_sha256(template.definition),
            MINIMAX_REFERENCE_IMAGE_DEFINITION_SHA256,
        )

    def test_sdxl_image_workflow_pins_checkpoint_and_injects_only_text_conditioning(self):
        prompt = self._image_prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-reference-image")

        workflow, values, _ = build_workflow(template, prompt, self.project, {
            "SEED": 1234,
            "WIDTH": 608,
            "HEIGHT": 352,
        })

        self.assertEqual(prompt.model, SDXL_BASE_MODEL_ID)
        self.assertEqual(workflow["1"]["inputs"]["ckpt_name"], template.model)
        self.assertEqual(workflow["2"]["inputs"]["text"], prompt.prompt_text)
        self.assertEqual(workflow["3"]["inputs"]["text"], prompt.negative_prompt)
        self.assertEqual(workflow["4"]["inputs"], {
            "width": 608, "height": 352, "batch_size": 1,
        })
        self.assertEqual(workflow["5"]["inputs"]["seed"], 1234)
        self.assertEqual(workflow["5"]["inputs"]["steps"], 30)
        self.assertEqual(workflow["5"]["inputs"]["cfg"], 6.5)
        self.assertEqual(workflow["5"]["inputs"]["sampler_name"], "dpmpp_2m")
        self.assertEqual(workflow["5"]["inputs"]["scheduler"], "karras")
        self.assertEqual(values["MODEL"], template.model)
        self.assertEqual(values["EFFECTIVE_CONDITIONING_MODE"], "TEXT_ONLY")
        self.assertEqual(values["REFERENCE_BINDINGS"], [])
        self.assertFalse(any(
            node["class_type"] == "LoadImage" for node in workflow.values()
        ))

    def test_sdxl_image_workflow_rejects_model_override_and_non_multiple_of_eight(self):
        prompt = self._image_prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-reference-image")
        with self.assertRaisesRegex(WorkflowBuildError, "禁止覆盖 MODEL"):
            build_workflow(template, prompt, self.project, {"MODEL": "other.safetensors"})
        with self.assertRaisesRegex(WorkflowBuildError, "被 8 整除"):
            build_workflow(template, prompt, self.project, {"WIDTH": 610, "HEIGHT": 352})

    def test_h3_workflow_rejects_duration_below_five_seconds(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        with self.assertRaisesRegex(WorkflowBuildError, "5000–15000ms"):
            build_workflow(template, prompt, self.project, {"DURATION_MS": 4999})

    def test_h3_workflow_rejects_duration_above_fifteen_seconds(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        with self.assertRaisesRegex(WorkflowBuildError, "5000–15000ms"):
            build_workflow(template, prompt, self.project, {"DURATION_MS": 15001})

    def test_h3_maximum_duration_aligns_to_362_frames(self):
        prompt = self._prompt()
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, values, _ = build_workflow(
            template, prompt, self.project, {"DURATION_MS": 15000},
        )
        self.assertEqual(workflow["5"]["inputs"]["length"], 362)
        self.assertEqual(values["EFFECTIVE_DURATION_MS"], 15083)
