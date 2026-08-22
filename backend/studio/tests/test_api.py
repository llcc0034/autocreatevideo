import importlib
import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.apps import apps as django_apps
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    CharacterBible,
    DeferredGenerationRequest,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PropBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplate,
)
from studio.tests.factories import build_story


@override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
class APIFlowTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene,
            self.episode, self.shot1, self.shot2,
        ) = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_ownership_isolation(self):
        other = get_user_model().objects.create_user(username="bob", password="StrongPass123!")
        self.client.force_authenticate(other)
        response = self.client.get(f"/api/v1/projects/{self.project.id}/")
        self.assertEqual(response.status_code, 404)

    def test_production_options_exposes_only_safe_executable_video_metadata(self):
        call_command("seed_data", verbosity=0)
        WorkflowTemplate.objects.create(
            owner=None,
            slug="inactive-system-video",
            version=1,
            name="inactive system video",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={"1": {"class_type": "SaveVideo", "inputs": {}}},
            input_schema={"h3_modes": [Shot.H3Mode.REF2VA]},
            is_system=True,
            is_active=False,
        )
        WorkflowTemplate.objects.create(
            owner=None,
            slug="active-non-comfy-video",
            version=1,
            name="active non-comfy video",
            provider="remote-api",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={"endpoint": "E:/private/provider-endpoint"},
            input_schema={"h3_modes": [Shot.H3Mode.REF2VA]},
            is_system=True,
            is_active=True,
        )
        WorkflowTemplate.objects.create(
            owner=self.user,
            slug="active-user-video",
            version=1,
            name="active user video",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={
                "1": {"class_type": "SaveVideo", "inputs": {
                    "filename_prefix": "C:/private/user-output",
                }},
            },
            input_schema={
                "h3_modes": [Shot.H3Mode.REF2VA],
                "source_workflow": "C:\\private\\user-workflow.json",
                "local_path": "C:/private/user-workflow.json",
            },
            is_system=False,
            is_active=True,
        )
        variant = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-turbo-lora",
        )
        variant.input_schema = {
            **variant.input_schema,
            "source_workflow": "C:\\private\\video_minimax_h3_r2v_turbo_lora.json",
        }
        variant.save(update_fields=("input_schema", "updated_at"))

        response = self.client.get("/api/v1/workflow-templates/production-options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 5)
        by_slug = {item["slug"]: item for item in response.data}
        self.assertEqual(set(by_slug), {
            "minimax-h3-base-video",
            "minimax-h3-first-last-video",
            "minimax-h3-reference-video",
            "minimax-h3-r2v-t8-dual-clock-4step",
            "minimax-h3-r2v-turbo-lora",
        })
        for item in response.data:
            self.assertEqual(set(item), {
                "id", "name", "slug", "version", "output_kind", "description",
                "input_schema", "is_system", "is_active",
            })
            self.assertEqual(item["output_kind"], WorkflowTemplate.OutputKind.VIDEO)
            self.assertTrue(item["is_system"])
            self.assertTrue(item["is_active"])
            self.assertIn("h3_modes", item["input_schema"])
            self.assertIn("is_default", item["input_schema"])
            self.assertNotIn("definition", item)
            self.assertNotIn("required_models", item["input_schema"])
            self.assertNotIn("lora_model", item["input_schema"])
        expected_steps = {
            "minimax-h3-r2v-t8-dual-clock-4step": {"default": 4, "max": 20},
            "minimax-h3-r2v-turbo-lora": {"default": 10, "max": 20},
        }
        for slug, step_contract in expected_steps.items():
            schema = by_slug[slug]["input_schema"]
            self.assertEqual(schema["h3_modes"], [Shot.H3Mode.REF2VA])
            self.assertFalse(schema["is_default"])
            self.assertEqual(schema["maturity"], "experimental")
            self.assertIn("user_parameters", schema)
            self.assertEqual(set(schema["user_parameters"]), {"STEPS", "LORA_STRENGTH"})
            self.assertEqual(schema["user_parameters"]["STEPS"]["type"], "integer")
            self.assertEqual(schema["user_parameters"]["STEPS"]["min"], 1)
            self.assertEqual(schema["user_parameters"]["STEPS"]["step"], 1)
            self.assertEqual(
                schema["user_parameters"]["STEPS"]["default"], step_contract["default"],
            )
            self.assertEqual(
                schema["user_parameters"]["STEPS"]["max"], step_contract["max"],
            )
            self.assertEqual(
                {
                    key: schema["user_parameters"]["LORA_STRENGTH"][key]
                    for key in ("type", "default", "min", "max", "step")
                },
                {
                    "type": "number", "default": 1.0, "min": 0.0,
                    "max": 2.2, "step": 0.05,
                },
            )
        encoded = json.dumps(response.data, ensure_ascii=False)
        self.assertNotIn("minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("C:\\private", encoded)
        self.assertNotIn("E:/private", encoded)
        self.assertEqual(
            by_slug["minimax-h3-r2v-turbo-lora"]["input_schema"]["source_workflow"],
            "video_minimax_h3_r2v_turbo_lora.json",
        )

    def test_h3_project_default_shot_duration_is_guarded(self):
        self.assertEqual(self.project.default_shot_duration_ms, 5000)
        response = self.client.patch(f"/api/v1/projects/{self.project.id}/", {
            "default_shot_duration_ms": 4999,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("5000–15000ms", str(response.data))

    def test_video_shot_create_and_update_enforce_safe_duration(self):
        payload = {
            "episode": str(self.episode.id),
            "number": 3,
            "narrative": "持续推进的视频镜头",
            "media_type": Shot.MediaType.VIDEO,
        }
        for unsafe_duration in (4999, 15001):
            with self.subTest(duration_ms=unsafe_duration):
                response = self.client.post(
                    "/api/v1/shots/",
                    {**payload, "duration_ms": unsafe_duration},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("duration_ms", str(response.data))

        created = self.client.post("/api/v1/shots/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["duration_ms"], 5000)

        rejected_update = self.client.patch(
            f"/api/v1/shots/{created.data['id']}/",
            {"duration_ms": 15001},
            format="json",
        )
        self.assertEqual(rejected_update.status_code, 400, rejected_update.data)

        image = self.client.post("/api/v1/shots/", {
            "episode": str(self.episode.id), "number": 4,
            "narrative": "允许短时长的静态定帧", "media_type": Shot.MediaType.IMAGE,
            "duration_ms": 100,
        }, format="json")
        self.assertEqual(image.status_code, 201, image.data)
        self.assertEqual(image.data["duration_ms"], 100)

    def test_sync_shots_enforces_video_duration_but_not_image_duration(self):
        for unsafe_duration in (4999, 15001):
            with self.subTest(duration_ms=unsafe_duration):
                response = self.client.post(f"/api/v1/episodes/{self.episode.id}/sync-shots/", {
                    "shot_version_key": f"legacy:{self.episode.id}:initial",
                    "shots": [{
                        "id": str(self.shot1.id), "number": 1,
                        "duration_ms": unsafe_duration, "media_type": Shot.MediaType.VIDEO,
                    }],
                }, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("duration_ms", str(response.data))

        self.shot1.media_type = Shot.MediaType.IMAGE
        self.shot1.duration_ms = 100
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        image_response = self.client.post(f"/api/v1/episodes/{self.episode.id}/sync-shots/", {
            "shot_version_key": f"legacy:{self.episode.id}:initial",
            "shots": [{"id": str(self.shot1.id), "number": 1}],
        }, format="json")
        self.assertEqual(image_response.status_code, 200, image_response.data)
        self.assertEqual(image_response.data["shots"][0]["duration_ms"], 100)

    def test_duration_data_migration_clamps_projects_and_only_video_shots(self):
        self.project.default_shot_duration_ms = 100
        self.project.save(update_fields=("default_shot_duration_ms", "updated_at"))
        high_project = SeriesProject.objects.create(
            owner=self.user, title="超长默认值", default_shot_duration_ms=20000,
        )
        self.shot1.media_type = Shot.MediaType.VIDEO
        self.shot1.duration_ms = 100
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.duration_ms = 100
        self.shot2.save(update_fields=("media_type", "duration_ms", "updated_at"))
        long_video = Shot.objects.create(
            episode=self.episode, number=3, narrative="超长视频", duration_ms=20000,
            media_type=Shot.MediaType.VIDEO,
        )

        migration = importlib.import_module("studio.migrations.0007_clamp_video_shot_durations")
        migration.clamp_video_shot_durations(django_apps, None)

        self.project.refresh_from_db()
        high_project.refresh_from_db()
        self.shot1.refresh_from_db()
        self.shot2.refresh_from_db()
        long_video.refresh_from_db()
        self.assertEqual(self.project.default_shot_duration_ms, 5000)
        self.assertEqual(high_project.default_shot_duration_ms, 15000)
        self.assertEqual(self.shot1.duration_ms, 5000)
        self.assertEqual(long_video.duration_ms, 15000)
        self.assertEqual(self.shot2.duration_ms, 100)

    def test_seeded_demo_video_uses_five_second_duration(self):
        call_command("seed_data", "--with-demo-project", stdout=StringIO(), verbosity=0)
        demo = get_user_model().objects.get(username="demo")
        project = demo.series_projects.get(title="无名剑影")
        self.assertEqual(project.default_shot_duration_ms, 5000)
        self.assertEqual(project.episodes.get(number=1).shots.get(number=1).duration_ms, 5000)

    def test_generate_multishot_h3_prompt(self):
        response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            "prompt_type": "video", "h3_mode": "T2VA", "multi_shot": True,
            "include_audio": True,
            "shot_durations": [{"shot_id": str(self.shot1.id), "duration_ms": 4000}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["is_multi_shot"])
        self.assertIn("[Shot 2] At 00:04.000,", response.data[0]["prompt_text"])
        self.assertEqual(PromptArtifact.objects.count(), 1)

    def test_total_duration_is_distributed_across_shots(self):
        response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            "prompt_type": "video", "h3_mode": "T2VA", "multi_shot": True,
            "total_duration_ms": 7000,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data[0]["total_duration_ms"], 7000)
        self.assertEqual(sum(response.data[0]["structured_payload"]["shot_durations_ms"]), 7000)

    def test_complete_workflow_and_queue_flow(self):
        call_command("seed_data", verbosity=0)
        prompt_response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id), "shot_ids": [str(self.shot1.id)],
            "prompt_type": "video", "h3_mode": "T2VA", "multi_shot": False,
            "shot_durations": [{"shot_id": str(self.shot1.id), "duration_ms": 5000}],
        }, format="json")
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow_response = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": prompt_response.data[0]["id"], "template_id": str(template.id),
        }, format="json")
        self.assertEqual(workflow_response.status_code, 201, workflow_response.data)
        task_response = self.client.post("/api/v1/tasks/", {
            "generated_workflow_id": workflow_response.data["id"], "priority": 5,
        }, format="json")
        self.assertEqual(task_response.status_code, 201, task_response.data)
        self.assertEqual(task_response.data["status"], "queued")

    def test_workflow_endpoint_rejects_unsafe_h3_duration(self):
        call_command("seed_data", verbosity=0)
        prompt_response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id), "shot_ids": [str(self.shot1.id)],
            "prompt_type": "video", "h3_mode": "T2VA", "multi_shot": False,
        }, format="json")
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        below = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": prompt_response.data[0]["id"],
            "template_id": str(template.id),
        }, format="json")
        self.assertEqual(below.status_code, 400, below.data)
        self.assertIn("5000–15000ms", str(below.data))
        above = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": prompt_response.data[0]["id"],
            "template_id": str(template.id), "overrides": {"DURATION_MS": 15001},
        }, format="json")
        self.assertEqual(above.status_code, 400, above.data)
        self.assertIn("5000–15000ms", str(above.data))

    def test_build_entire_project_production_plan(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 6000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))
        self.shot2.duration_ms = 6000
        self.shot2.save(update_fields=("duration_ms", "updated_at"))
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "template_id": str(template.id), "multi_shot": True, "include_audio": True,
            "h3_mode": "Ref2VA", "enqueue": True,
            "episode_ids": [str(self.episode.id)],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 1, "workflows": 1, "tasks": 1})
        self.assertEqual(response.data["episode_ids"], [str(self.episode.id)])

    def test_build_production_can_enqueue_only_one_selected_shot(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot2.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))
        self.shot2.save(update_fields=("duration_ms", "updated_at"))
        self.episode.status = "completed"
        self.episode.save(update_fields=("status", "updated_at"))
        self.project.status = "completed"
        self.project.completed_at = timezone.now()
        self.project.save(update_fields=("status", "completed_at", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot2.id)],
            "multi_shot": False,
            "enqueue": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 1, "workflows": 1, "tasks": 1})
        self.assertEqual(response.data["episode_ids"], [str(self.episode.id)])
        self.assertEqual(response.data["production_units"][0]["shot_ids"], [str(self.shot2.id)])
        prompt = PromptArtifact.objects.get(pk=response.data["prompt_artifact_ids"][0])
        self.assertEqual(prompt.shot_id, self.shot2.id)
        self.assertEqual(prompt.request_options["shot_ids"], [str(self.shot2.id)])
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.status, "generating")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "active")
        self.assertIsNone(self.project.completed_at)

    def test_build_production_defers_ref2va_first_frame_after_previous_tail_task(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.h3_mode = Shot.H3Mode.T2VA
        self.shot1.requires_last_frame_continuity = True
        self.shot1.save(update_fields=(
            "duration_ms", "h3_mode", "requires_last_frame_continuity", "updated_at",
        ))
        self.shot2.duration_ms = 5000
        self.shot2.h3_mode = Shot.H3Mode.REF2VA
        self.shot2.requires_first_frame_continuity = True
        self.shot2.reference_bindings = {}
        self.shot2.save(update_fields=(
            "duration_ms", "h3_mode", "requires_first_frame_continuity",
            "reference_bindings", "updated_at",
        ))
        variant = WorkflowTemplate.objects.get(
            owner=None,
            slug="minimax-h3-r2v-turbo-lora",
        )

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            "multi_shot": True,
            "enqueue": True,
            "idempotency_key": "chain-first-frame-ref2va",
            "resolution_profile": "0.2",
            "workflow_selections": [{
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": Shot.H3Mode.REF2VA,
                "template_id": str(variant.id),
                "parameters": {"STEPS": 8, "LORA_STRENGTH": 0.65},
            }],
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["counts"],
            {"prompts": 1, "workflows": 1, "tasks": 1, "deferred_generation_requests": 1},
        )
        self.assertEqual(len(response.data["production_units"]), 2)
        immediate, deferred = response.data["production_units"]
        self.assertEqual(immediate["shot_id"], str(self.shot1.id))
        self.assertIsNotNone(immediate["task_id"])
        self.assertEqual(deferred["shot_id"], str(self.shot2.id))
        self.assertIsNone(deferred["task_id"])
        self.assertEqual(deferred["workflow_mode"], "deferred_first_frame_continuity")
        self.assertEqual(deferred["deferred_frame_slot"], "first_frame")
        request_id = response.data["deferred_generation_request_ids"][0]
        self.assertEqual(deferred["deferred_generation_request_id"], request_id)
        deferred_request = DeferredGenerationRequest.objects.get(pk=request_id)
        self.assertEqual(deferred_request.source_task_id, GenerationTask.objects.get(pk=immediate["task_id"]).id)
        self.assertEqual(deferred_request.target_shot_id, self.shot2.id)
        self.assertEqual(deferred_request.frame_slot, DeferredGenerationRequest.FrameSlot.FIRST_FRAME)
        self.assertEqual(deferred_request.target_template_id, variant.id)
        self.assertEqual(
            deferred_request.target_workflow_overrides,
            {
                "RESOLUTION_PROFILE": "0.2",
                "RESOLUTION_MULTIPLE": 32,
                "WIDTH": 352,
                "HEIGHT": 608,
                "STEPS": 8,
                "LORA_STRENGTH": 0.65,
            },
        )

    def test_whole_episode_accepts_chained_ref2va_first_frame_waits(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.h3_mode = Shot.H3Mode.T2VA
        self.shot1.requires_last_frame_continuity = True
        self.shot1.save(update_fields=(
            "duration_ms", "h3_mode", "requires_last_frame_continuity", "updated_at",
        ))
        chained_shots = [self.shot2]
        for number in (3, 4):
            shot = Shot.objects.create(
                episode=self.episode,
                number=number,
                title=f"连续镜头 {number}",
                narrative=f"林默继续推进到第 {number} 个连续动作。",
                camera="中景跟拍",
                scene=self.scene,
                duration_ms=5000,
                h3_mode=Shot.H3Mode.REF2VA,
            )
            shot.characters.add(self.character)
            chained_shots.append(shot)
        for shot in chained_shots:
            shot.duration_ms = 5000
            shot.h3_mode = Shot.H3Mode.REF2VA
            shot.requires_first_frame_continuity = True
            shot.requires_last_frame_continuity = True
            shot.reference_bindings = {}
            shot.save(update_fields=(
                "duration_ms", "h3_mode", "requires_first_frame_continuity",
                "requires_last_frame_continuity", "reference_bindings", "updated_at",
            ))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "whole_episode": True,
            "shot_version_key": f"legacy:{self.episode.id}:initial",
            "multi_shot": True,
            "enqueue": True,
            "idempotency_key": "whole-episode-chained-ref2va",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["counts"],
            {"prompts": 1, "workflows": 1, "tasks": 1, "deferred_generation_requests": 1},
        )
        self.assertEqual(len(response.data["production_units"]), 4)
        self.assertEqual(response.data["production_units"][0]["shot_id"], str(self.shot1.id))
        self.assertIsNotNone(response.data["production_units"][0]["task_id"])
        self.assertEqual(
            response.data["production_units"][1]["workflow_mode"],
            "deferred_first_frame_continuity",
        )
        self.assertEqual(
            response.data["production_units"][2]["workflow_mode"],
            "waiting_for_deferred_first_frame_source",
        )
        self.assertEqual(
            response.data["production_units"][3]["workflow_mode"],
            "waiting_for_deferred_first_frame_chain",
        )
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.assertEqual(DeferredGenerationRequest.objects.count(), 1)

    def test_build_production_plan_without_enqueue_marks_selected_episode_ready(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))
        self.episode.status = "completed"
        self.episode.save(update_fields=("status", "updated_at"))
        self.project.status = "completed"
        self.project.completed_at = timezone.now()
        self.project.save(update_fields=("status", "completed_at", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "multi_shot": False,
            "enqueue": False,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.status, "ready")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "active")
        self.assertIsNone(self.project.completed_at)

    def test_build_production_rejects_shot_outside_project_or_selected_episode(self):
        call_command("seed_data", verbosity=0)
        _, _, _, _, _, foreign_shot, _ = build_story("foreign-shot-owner")
        foreign_shot.duration_ms = 5000
        foreign_shot.save(update_fields=("duration_ms", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(foreign_shot.id)],
            "multi_shot": False,
            "enqueue": False,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("shot_ids", str(response.data))
        self.assertEqual(PromptArtifact.objects.filter(project=self.project).count(), 0)

    def test_build_production_uses_controlled_official_resolution_profile(self):
        call_command("seed_data", verbosity=0)
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "multi_shot": False,
            "enqueue": True,
            "idempotency_key": "controlled-official-resolution-profile",
            "resolution_profile": "0.3",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 2, "workflows": 2, "tasks": 2})
        for unit in response.data["production_units"]:
            self.assertEqual(unit["resolution_profile"], "0.3")
            self.assertEqual((unit["width"], unit["height"], unit["fps"]), (416, 736, 24))
        tasks = self.project.generation_tasks.all()
        self.assertTrue(all(task.capability_snapshot["resolution_profile"] == "0.3" for task in tasks))
        self.assertTrue(all(task.capability_snapshot["width"] == 416 for task in tasks))
        self.assertTrue(all(task.capability_snapshot["height"] == 736 for task in tasks))

    def test_build_production_injects_official_0_1_resolution_profile(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "multi_shot": False,
            "enqueue": True,
            "resolution_profile": "0.1",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual(unit["resolution_profile"], "0.1")
        self.assertEqual((unit["width"], unit["height"]), (256, 416))
        workflow = GeneratedWorkflow.objects.get(pk=unit["workflow_id"])
        self.assertEqual(workflow.injected_values["RESOLUTION_PROFILE"], "0.1")
        self.assertEqual(workflow.injected_values["WIDTH"], 256)
        self.assertEqual(workflow.injected_values["HEIGHT"], 416)

    def test_build_production_supports_official_2_0_profile_and_default_multiple(self):
        call_command("seed_data", verbosity=0)
        self.project.aspect_ratio = "16:9"
        self.project.width = 1344
        self.project.height = 768
        self.project.save(update_fields=("aspect_ratio", "width", "height", "updated_at"))
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "multi_shot": False,
            "enqueue": True,
            "resolution_profile": "2.0",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual((unit["width"], unit["height"]), (1920, 1088))
        self.assertEqual(unit["resolution_multiple"], 32)
        workflow = GeneratedWorkflow.objects.get(pk=unit["workflow_id"])
        self.assertEqual(workflow.injected_values["RESOLUTION_PROFILE"], "2.0")
        self.assertEqual(workflow.injected_values["RESOLUTION_MULTIPLE"], 32)
        task = GenerationTask.objects.get(pk=unit["task_id"])
        self.assertEqual(task.capability_snapshot["resolution_multiple"], 32)

    def test_build_production_non_default_multiple_drives_size_and_snapshot(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "multi_shot": False,
            "enqueue": True,
            "resolution_profile": "0.3",
            "resolution_multiple": 20,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual((unit["width"], unit["height"]), (420, 740))
        self.assertEqual(unit["resolution_multiple"], 20)
        workflow = GeneratedWorkflow.objects.get(pk=unit["workflow_id"])
        self.assertEqual(workflow.injected_values["RESOLUTION_MULTIPLE"], 20)
        task = GenerationTask.objects.get(pk=unit["task_id"])
        self.assertEqual(task.capability_snapshot["resolution_multiple"], 20)

    def test_project_resolution_is_rounded_for_non_default_multiple(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "multi_shot": False,
            "enqueue": True,
            "resolution_profile": "project",
            "resolution_multiple": 20,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual((unit["width"], unit["height"]), (760, 1340))
        self.assertEqual(unit["resolution_multiple"], 20)

    def test_resolution_multiple_rejects_out_of_step_value(self):
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "enqueue": False,
            "resolution_profile": "0.3",
            "resolution_multiple": 18,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("resolution_multiple", str(response.data))
        self.assertEqual(PromptArtifact.objects.count(), 0)

    def test_image_resolution_multiple_requires_multiple_of_eight(self):
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "enqueue": False,
            "resolution_profile": "0.3",
            "resolution_multiple": 12,
            "image_resolution_profile": "0.98",
            "image_resolution_multiple": 12,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("image_resolution_multiple", str(response.data))
        self.assertIn("divisible by 8", str(response.data))
        self.assertEqual(PromptArtifact.objects.count(), 0)

    def test_resolution_profile_rejects_conflicting_manual_dimensions(self):
        call_command("seed_data", verbosity=0)
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "enqueue": False,
            "resolution_profile": "0.2",
            "workflow_overrides": {"WIDTH": 416, "HEIGHT": 736},
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("workflow_overrides", str(response.data))
        self.assertEqual(PromptArtifact.objects.count(), 0)

    def test_jwt_token_endpoint(self):
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/v1/auth/token/", {"username": "alice", "password": "StrongPass123!"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_breakdown_master_script_binds_bible_and_creates_shots(self):
        self.episode.delete()
        self.project.master_script = (
            "第1集：烧焦的信\n镜头1【时长=8秒】【首帧连续=否】【尾帧连续=是】：旧档案室里。\n"
            "[0-3秒] 动作：林默展开证物。\n[3-6秒] 林默：这不是昨晚烧的。\n"
            "镜头2：闪电照亮门后人影。\n\n第2集：追踪\n镜头1：林默冲进旧档案室。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        response = self.client.post(f"/api/v1/projects/{self.project.id}/breakdown-script/", {
            "episode_count_hint": 9, "default_shot_duration_ms": 5000,
            "multi_shot": True, "include_audio": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"episodes": 2, "shots": 3})
        first_shot = self.project.episodes.get(number=1).shots.get(number=1)
        self.assertEqual(first_shot.dialogue[0]["speaker_id"], "S1")
        self.assertEqual(first_shot.dialogue[0]["start_ms"], 3000)
        self.assertEqual(first_shot.dialogue[0]["end_ms"], 6000)
        self.assertEqual(first_shot.duration_ms, 8000)
        self.assertFalse(first_shot.requires_first_frame_continuity)
        self.assertTrue(first_shot.requires_last_frame_continuity)
        self.assertIn("[0s–3s] 林默展开证物。", first_shot.narrative)
        self.assertEqual(list(first_shot.characters.values_list("stable_speaker_id", flat=True)), ["S1"])

    def test_page_size_query_returns_full_90_asset_library_slice(self):
        style = Style.objects.create(owner=self.user, slug="pagination-style", name="分页测试")
        Asset.objects.bulk_create([
            Asset(owner=self.user, style=style, kind=Asset.Kind.REFERENCE, name=f"asset-{index:03d}")
            for index in range(90)
        ])
        response = self.client.get("/api/v1/assets/", {"style": str(style.id), "page_size": 200})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 90)
        self.assertEqual(len(response.data["results"]), 90)
        first_page = self.client.get("/api/v1/assets/", {"style": str(style.id), "page_size": 50})
        self.assertEqual(len(first_page.data["results"]), 50)
        self.assertIn("page_size=50", first_page.data["next"])
        second_page = self.client.get(first_page.data["next"])
        self.assertEqual(len(second_page.data["results"]), 40)
        self.assertIsNone(second_page.data["next"])

    def test_sync_shots_atomically_deletes_reorders_and_preserves_structured_fields(self):
        stale = Shot.objects.create(
            episode=self.episode, number=3, narrative="应删除的旧镜头", duration_ms=5000,
        )
        reference = self.character.anchor_asset
        self.shot1.reference_assets.add(reference)
        self.shot1.metadata = {"framing": "特写", "lens": "50mm"}
        self.shot1.save(update_fields=("metadata", "updated_at"))
        response = self.client.post(f"/api/v1/episodes/{self.episode.id}/sync-shots/", {
            "shot_version_key": f"legacy:{self.episode.id}:initial",
            "shots": [
                {
                    "id": str(self.shot2.id), "number": 1, "narrative": self.shot2.narrative,
                    "dialogue": self.shot2.dialogue, "characters": [str(self.character.id)],
                    "duration_ms": 5000, "media_type": "video", "h3_mode": "T2VA",
                    "include_audio": True, "metadata": {"framing": "大全景"},
                },
                {
                    "id": str(self.shot1.id), "number": 2, "narrative": self.shot1.narrative,
                    "dialogue": [{
                        **self.shot1.dialogue[0], "start_ms": 500, "end_ms": 2500,
                    }], "characters": [str(self.character.id)],
                    "reference_assets": [str(reference.id)], "duration_ms": 5000,
                    "media_type": "video", "h3_mode": "T2VA", "include_audio": True,
                    "requires_first_frame_continuity": True,
                    "requires_last_frame_continuity": True,
                    "metadata": {"framing": "特写", "lens": "50mm"},
                },
                {
                    "number": 3, "narrative": "新建定帧", "duration_ms": 5000,
                    "media_type": "image", "include_audio": True,
                    "metadata": {"framing": "中景"},
                },
            ],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["counts"], {"created": 1, "updated": 2, "deleted": 1})
        self.assertFalse(Shot.objects.filter(pk=stale.pk).exists())
        self.shot1.refresh_from_db()
        self.shot2.refresh_from_db()
        self.assertEqual((self.shot2.number, self.shot1.number), (1, 2))
        self.assertEqual(self.shot1.dialogue[0]["speaker_id"], "S1")
        self.assertEqual(self.shot1.dialogue[0]["start_ms"], 500)
        self.assertEqual(self.shot1.dialogue[0]["end_ms"], 2500)
        self.assertTrue(self.shot1.requires_first_frame_continuity)
        self.assertTrue(self.shot1.requires_last_frame_continuity)
        self.assertEqual(self.shot1.metadata["framing"], "特写")
        self.assertEqual(list(self.shot1.reference_assets.values_list("id", flat=True)), [reference.id])
        created = self.episode.shots.get(number=3)
        self.assertEqual(created.metadata["framing"], "中景")
        self.assertFalse(created.include_audio)

    def test_sync_bibles_keeps_anchor_and_reference_assets_independent(self):
        second_reference = Asset.objects.create(
            owner=self.user, style=self.project.style, kind=Asset.Kind.CHARACTER,
            name="第二人物参考", local_path="C:/fixtures/lin-2.png", mime_type="image/png",
        )
        other = CharacterBible.objects.create(
            project=self.project, slug="other", name="周岚", stable_speaker_id="S2",
        )
        prop = PropBible.objects.create(project=self.project, slug="key", name="铜钥匙")
        response = self.client.post(f"/api/v1/projects/{self.project.id}/sync-bibles/", {
            "characters": [
                {
                    "id": str(self.character.id), "slug": "other", "name": self.character.name,
                    "stable_speaker_id": "S2",
                    "reference_assets": [str(second_reference.id), str(self.character.anchor_asset_id)],
                },
                {
                    "id": str(other.id), "slug": "lin", "name": other.name,
                    "stable_speaker_id": "S1", "reference_assets": [],
                },
            ],
            "scenes": [],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["counts"]["characters"], {
            "created": 0, "updated": 2, "deleted": 0,
        })
        self.assertEqual(response.data["counts"]["scenes"]["deleted"], 1)
        self.character.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.character.slug, self.character.stable_speaker_id), ("other", "S2"))
        self.assertEqual((other.slug, other.stable_speaker_id), ("lin", "S1"))
        self.assertNotEqual(self.character.anchor_asset_id, second_reference.id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {second_reference.id, self.character.anchor_asset_id},
        )
        self.assertTrue(PropBible.objects.filter(pk=prop.pk).exists())

    def test_build_production_respects_each_shot_mode_and_auto_selects_templates(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 6000
        self.shot1.h3_mode = Shot.H3Mode.T2VA
        self.shot1.include_audio = True
        self.shot1.save(update_fields=("duration_ms", "h3_mode", "include_audio", "updated_at"))
        self.shot2.duration_ms = 6000
        self.shot2.h3_mode = Shot.H3Mode.REF2VA
        self.shot2.include_audio = False
        self.shot2.save(update_fields=("duration_ms", "h3_mode", "include_audio", "updated_at"))
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "multi_shot": True, "enqueue": False,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 2, "workflows": 2, "tasks": 0})
        units = response.data["production_units"]
        self.assertEqual([item["h3_mode"] for item in units], ["T2VA", "Ref2VA"])
        slugs = list(WorkflowTemplate.objects.filter(
            id__in=[item["template_id"] for item in units],
        ).values_list("slug", flat=True))
        self.assertCountEqual(slugs, ["minimax-h3-base-video", "minimax-h3-reference-video"])

    def test_build_production_selects_ref2va_variant_without_affecting_other_modes(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 6000
        self.shot1.h3_mode = Shot.H3Mode.T2VA
        self.shot1.save(update_fields=("duration_ms", "h3_mode", "updated_at"))
        self.shot2.duration_ms = 6000
        self.shot2.h3_mode = Shot.H3Mode.REF2VA
        self.shot2.save(update_fields=("duration_ms", "h3_mode", "updated_at"))
        variant = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-turbo-lora",
        )

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "multi_shot": True,
            "enqueue": False,
            "workflow_selections": [{
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": Shot.H3Mode.REF2VA,
                "template_id": str(variant.id),
                "parameters": {"STEPS": 12, "LORA_STRENGTH": 0.8},
            }],
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        units = {item["h3_mode"]: item for item in response.data["production_units"]}
        self.assertEqual(units["T2VA"]["template_slug"], "minimax-h3-base-video")
        self.assertEqual(units["T2VA"]["workflow_parameters"], {})
        self.assertEqual(units["Ref2VA"]["template_slug"], variant.slug)
        self.assertEqual(
            units["Ref2VA"]["workflow_parameters"],
            {"STEPS": 12, "LORA_STRENGTH": 0.8},
        )
        ref_workflow = GeneratedWorkflow.objects.get(pk=units["Ref2VA"]["workflow_id"])
        self.assertEqual(ref_workflow.workflow_json["9"]["inputs"]["steps"], 12)
        self.assertEqual(ref_workflow.workflow_json["15"]["inputs"]["strength_model"], 0.8)

    def test_build_production_rejects_incompatible_or_invalid_workflow_selection(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 6000
        self.shot1.h3_mode = Shot.H3Mode.REF2VA
        self.shot1.save(update_fields=("duration_ms", "h3_mode", "updated_at"))
        t2va = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        turbo = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-r2v-turbo-lora")
        base_payload = {
            "shot_ids": [str(self.shot1.id)], "multi_shot": False, "enqueue": False,
        }
        cases = [
            {
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": Shot.H3Mode.REF2VA,
                "template_id": str(t2va.id),
                "parameters": {},
            },
            {
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": Shot.H3Mode.REF2VA,
                "template_id": str(turbo.id),
                "parameters": {"STEPS": "12"},
            },
            {
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": Shot.H3Mode.REF2VA,
                "template_id": str(turbo.id),
                "parameters": {"UNKNOWN": 1},
            },
        ]
        for selection in cases:
            with self.subTest(selection=selection):
                response = self.client.post(
                    f"/api/v1/projects/{self.project.id}/build-production/",
                    {**base_payload, "workflow_selections": [selection]},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)

    def test_build_production_rebalances_video_chunks_into_safe_duration_range(self):
        call_command("seed_data", verbosity=0)
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 4000
            shot.h3_mode = Shot.H3Mode.T2VA
            shot.include_audio = True
            shot.save(update_fields=("duration_ms", "h3_mode", "include_audio", "updated_at"))
        for number in (3, 4):
            shot = Shot.objects.create(
                episode=self.episode, number=number, narrative=f"连续动作 {number}",
                duration_ms=4000, h3_mode=Shot.H3Mode.T2VA, include_audio=True,
                scene=self.scene,
            )
            shot.characters.add(self.character)
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "multi_shot": True, "enqueue": False,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"]["workflows"], 2)
        self.assertEqual(
            [item["requested_duration_ms"] for item in response.data["production_units"]],
            [8000, 8000],
        )
        self.assertTrue(all(
            5000 <= item["requested_duration_ms"] <= 15000
            for item in response.data["production_units"]
        ))

    def test_build_production_rejects_image_without_active_verified_template(self):
        call_command("seed_data", verbosity=0)
        WorkflowTemplate.objects.filter(
            owner=None, slug="minimax-reference-image",
        ).update(is_active=False)
        self.shot1.delete()
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.save(update_fields=("media_type", "updated_at"))
        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "multi_shot": True, "enqueue": False,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("没有 active", str(response.data))
        self.assertEqual(PromptArtifact.objects.count(), 0)

    def test_build_production_creates_active_sdxl_image_workflow_without_pixel_references(self):
        call_command("seed_data", verbosity=0)
        self.shot1.delete()
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.save(update_fields=("media_type", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot2.id)],
            "multi_shot": False,
            "enqueue": False,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 1, "workflows": 1, "tasks": 0})
        unit = response.data["production_units"][0]
        self.assertEqual(unit["media_type"], Shot.MediaType.IMAGE)
        self.assertEqual(unit["resolution_profile"], "0.98")
        self.assertEqual(unit["resolution_multiple"], 32)
        self.assertEqual((unit["width"], unit["height"]), (768, 1344))
        self.assertEqual(unit["workflow_mode"], "text_to_image")
        self.assertEqual(unit["effective_conditioning_mode"], "TEXT_ONLY")
        for video_only_key in (
            "fps", "include_audio", "requested_duration_ms", "effective_duration_ms",
        ):
            self.assertNotIn(video_only_key, unit)
        prompt = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        workflow = GeneratedWorkflow.objects.get(pk=unit["workflow_id"])
        self.assertEqual(prompt.model, "stabilityai/stable-diffusion-xl-base-1.0")
        self.assertEqual(
            prompt.structured_payload["model"],
            "stabilityai/stable-diffusion-xl-base-1.0",
        )
        self.assertEqual(prompt.structured_payload["reference_assets"], [])
        self.assertGreaterEqual(len(prompt.structured_payload["continuity_assets"]), 2)
        self.assertFalse(prompt.structured_payload["pixel_reference_inputs"])
        self.assertEqual(
            workflow.workflow_json["1"]["inputs"]["ckpt_name"],
            "sd_xl_base_1.0.safetensors",
        )
        self.assertFalse(any(
            node["class_type"] == "LoadImage"
            for node in workflow.workflow_json.values()
        ))
        task_response = self.client.post("/api/v1/tasks/", {
            "generated_workflow_id": str(workflow.id),
        }, format="json")
        self.assertEqual(task_response.status_code, 201, task_response.data)
        task = GenerationTask.objects.get(pk=task_response.data["id"])
        self.assertEqual(task.capability_snapshot["model"], "sd_xl_base_1.0.safetensors")
        self.assertEqual(task.effective_frames, 0)
        self.assertEqual(task.effective_duration_ms, 0)
        for video_only_key in ("fps", "include_audio", "requested_duration_ms"):
            self.assertNotIn(video_only_key, task.capability_snapshot)
        parameters = task_response.data["input_parameters"]
        self.assertEqual(parameters["effective_conditioning_mode"], "TEXT_ONLY")
        self.assertEqual(parameters["prompt_template_key"], "sdxl_txt2img_bible_v1")
        for video_only_key in (
            "fps", "include_audio", "requested_duration_ms", "effective_duration_ms",
            "effective_frames",
        ):
            self.assertNotIn(video_only_key, parameters)

    def test_mixed_production_keeps_video_smoke_profile_but_images_default_to_sdxl_quality(self):
        call_command("seed_data", verbosity=0)
        self.shot1.duration_ms = 5000
        self.shot1.media_type = Shot.MediaType.VIDEO
        self.shot1.save(update_fields=("duration_ms", "media_type", "updated_at"))
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.save(update_fields=("media_type", "updated_at"))

        response = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "episode_ids": [str(self.episode.id)],
            "multi_shot": False,
            "enqueue": False,
            "resolution_profile": "0.2",
            "resolution_multiple": 20,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 2, "workflows": 2, "tasks": 0})
        by_kind = {item["media_type"]: item for item in response.data["production_units"]}
        self.assertEqual(by_kind[Shot.MediaType.VIDEO]["resolution_profile"], "0.2")
        self.assertEqual(by_kind[Shot.MediaType.VIDEO]["resolution_multiple"], 20)
        self.assertEqual(by_kind[Shot.MediaType.IMAGE]["resolution_profile"], "0.98")
        self.assertEqual(by_kind[Shot.MediaType.IMAGE]["resolution_multiple"], 32)
        image_workflow = GeneratedWorkflow.objects.get(
            pk=by_kind[Shot.MediaType.IMAGE]["workflow_id"],
        )
        self.assertEqual(
            image_workflow.injected_values["RESOLUTION_PROFILE"], "0.98",
        )
        self.assertEqual(
            image_workflow.injected_values["RESOLUTION_MULTIPLE"], 32,
        )
        self.assertGreaterEqual(
            image_workflow.injected_values["WIDTH"]
            * image_workflow.injected_values["HEIGHT"],
            int(0.9 * 1024 * 1024),
        )

    def test_image_template_validation_checks_live_checkpoint_inventory(self):
        call_command("seed_data", verbosity=0)
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-reference-image")
        node_types = {node["class_type"] for node in template.definition.values()}
        with patch("studio.views.ComfyUIClient") as client_class:
            client = client_class.return_value
            client.object_info.return_value = {name: {} for name in node_types}
            client.models.return_value = ["sd_xl_base_1.0.safetensors"]

            available = self.client.get(
                f"/api/v1/workflow-templates/{template.id}/validate-comfyui/",
            )

            self.assertEqual(available.status_code, 200, available.data)
            self.assertTrue(available.data["valid"])
            self.assertEqual(available.data["missing_models"], {})
            client.models.assert_called_once_with("checkpoints")

            client.models.return_value = []
            missing = self.client.get(
                f"/api/v1/workflow-templates/{template.id}/validate-comfyui/",
            )

            self.assertEqual(missing.status_code, 200, missing.data)
            self.assertFalse(missing.data["valid"])
            self.assertEqual(
                missing.data["missing_models"]["checkpoints"],
                ["sd_xl_base_1.0.safetensors"],
            )

    def test_locked_bible_must_be_explicitly_unlocked_before_identity_change(self):
        character = self.project.characters.get(stable_speaker_id="S1")
        denied = self.client.patch(f"/api/v1/characters/{character.id}/", {
            "wardrobe": "突然换成红色礼服",
        }, format="json")
        self.assertEqual(denied.status_code, 400)
        allowed = self.client.patch(f"/api/v1/characters/{character.id}/", {
            "is_locked": False, "wardrobe": "剧情需要的红色礼服",
        }, format="json")
        self.assertEqual(allowed.status_code, 200, allowed.data)
