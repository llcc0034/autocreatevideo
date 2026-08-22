from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import UUID

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    Episode,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    Shot,
    WorkflowTemplate,
)
from studio.tests.factories import build_story
from studio.services.shot_versions import legacy_shot_version_key
from studio.views import _episode_batch_fingerprint


class WholeEpisodeGenerationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = override_settings(
            MEDIA_ROOT=root / "media",
            ASSET_LIBRARY_ROOT=root / "library",
            H3_PROMPT_LANGUAGE_GATE="warn",
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        (root / "media").mkdir()
        (root / "library").mkdir()
        call_command("seed_data", verbosity=0)
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot1,
            self.shot2,
        ) = build_story("episode-batch")
        self.shot1.duration_ms = 5000
        self.shot1.h3_mode = Shot.H3Mode.T2VA
        self.shot1.save(update_fields=("duration_ms", "h3_mode", "updated_at"))
        self.shot2.duration_ms = 5000
        self.shot2.h3_mode = Shot.H3Mode.FL2VA
        self.shot2.save(update_fields=("duration_ms", "h3_mode", "updated_at"))
        self.first_frame = Asset.objects.create(
            owner=self.user,
            style=self.project.style,
            kind=Asset.Kind.REFERENCE,
            name="第二镜首帧",
            file=SimpleUploadedFile("shot-two-first.png", b"local-image-fixture"),
            mime_type="image/png",
            checksum="shot-two-first",
        )
        self.shot2.reference_assets.add(self.first_frame)
        self.shot2.reference_bindings = {
            "first_frame_asset_id": str(self.first_frame.id),
        }
        self.shot2.save(update_fields=("reference_bindings", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def payload(self, key="episode-batch-key", **overrides):
        return {
            "episode_ids": [str(self.episode.id)],
            "whole_episode": True,
            "shot_version_key": legacy_shot_version_key(self.episode.id),
            # The server must still split per shot when an older caller sends
            # the historical default value.
            "multi_shot": True,
            "enqueue": True,
            "idempotency_key": key,
            **overrides,
        }

    def create_storyboard_version(self, key: str, label: str, count: int = 2):
        start_number = Shot.objects.filter(episode=self.episode).count() + 1
        shots = []
        for ordinal in range(1, count + 1):
            shot = Shot.objects.create(
                episode=self.episode,
                number=start_number + ordinal - 1,
                title=f"{label} 镜头 {ordinal}",
                narrative=f"{label} 的第 {ordinal} 个连续动作。",
                camera="中景跟拍",
                scene=self.scene,
                duration_ms=5000,
                h3_mode=Shot.H3Mode.T2VA,
                metadata={
                    "creative_version_key": key,
                    "creative_version_label": label,
                    "creative_version_shot_number": ordinal,
                    "creative_version_active": True,
                },
            )
            shot.characters.add(self.character)
            shots.append(shot)
        return shots

    def test_whole_episode_creates_one_traceable_task_per_ordered_shot(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {
            "prompts": 2,
            "workflows": 2,
            "tasks": 2,
        })
        self.assertFalse(response.data["idempotent_replay"])
        self.assertEqual(response.data["batch_kind"], "episode")
        self.assertEqual(response.data["batch_size"], 2)
        self.assertEqual(response.data["episode_number"], self.episode.number)
        self.assertEqual(response.data["episode_title"], self.episode.title)

        units = response.data["production_units"]
        self.assertEqual([item["batch_index"] for item in units], [1, 2])
        self.assertEqual([item["batch_size"] for item in units], [2, 2])
        self.assertEqual([item["shot_id"] for item in units], [str(self.shot1.id), str(self.shot2.id)])
        self.assertEqual([item["shot_number"] for item in units], [1, 2])
        self.assertEqual([item["shot_order"] for item in units], [1, 2])
        self.assertEqual([item["shot_title"] for item in units], [self.shot1.title, self.shot2.title])
        self.assertEqual([item["h3_mode"] for item in units], ["T2VA", "FL2VA"])
        self.assertEqual(
            [item["template_slug"] for item in units],
            ["minimax-h3-base-video", "minimax-h3-first-last-video"],
        )

        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            [task.generated_workflow.prompt_artifact.shot_id for task in tasks],
            [self.shot1.id, self.shot2.id],
        )
        self.assertEqual(
            tasks[1].capability_snapshot["reference_assets"][0]["id"],
            str(self.first_frame.id),
        )
        self.assertEqual({task.capability_snapshot["batch_id"] for task in tasks}, {response.data["batch_id"]})

        serialized = response.data["tasks"]
        self.assertEqual([item["episode_number"] for item in serialized], [1, 1])
        self.assertEqual([item["episode_title"] for item in serialized], [self.episode.title] * 2)
        self.assertEqual([item["shot_order"] for item in serialized], [1, 2])
        self.assertEqual([item["shot_ids"] for item in serialized], [
            [str(self.shot1.id)], [str(self.shot2.id)],
        ])

        by_batch = self.client.get("/api/v1/tasks/", {"batch_id": response.data["batch_id"]})
        self.assertEqual(by_batch.status_code, 200, by_batch.data)
        batch_results = by_batch.data.get("results", by_batch.data)
        self.assertEqual(len(batch_results), 2)
        self.assertEqual([item["batch_index"] for item in batch_results], [1, 2])
        by_shot = self.client.get("/api/v1/tasks/", {"shot_id": str(self.shot2.id)})
        self.assertEqual(by_shot.status_code, 200, by_shot.data)
        shot_results = by_shot.data.get("results", by_shot.data)
        self.assertEqual([item["shot_id"] for item in shot_results], [str(self.shot2.id)])

    def test_whole_episode_freezes_only_the_selected_storyboard_version(self):
        version_key = "creative-draft:11111111-1111-4111-8111-111111111111"
        version_shots = self.create_storyboard_version(version_key, "V2", count=3)

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("selected-storyboard-version", shot_version_key=version_key),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        expected_ids = [str(shot.id) for shot in version_shots]
        self.assertEqual(response.data["shot_version_key"], version_key)
        self.assertEqual(response.data["shot_version_label"], "V2")
        self.assertEqual(response.data["batch_size"], 3)
        self.assertEqual(response.data["batch_shot_ids"], expected_ids)
        self.assertEqual(
            [item["shot_id"] for item in response.data["production_units"]],
            expected_ids,
        )
        self.assertEqual(
            [item["shot_number"] for item in response.data["production_units"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [item["shot_id"] for item in response.data["batch_slots"]],
            expected_ids,
        )
        self.assertEqual(
            {item["shot_version_key"] for item in response.data["batch_slots"]},
            {version_key},
        )
        self.assertEqual(
            {item["shot_version_label"] for item in response.data["batch_slots"]},
            {"V2"},
        )
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        self.assertEqual(len(tasks), 3)
        for index, task in enumerate(tasks, start=1):
            snapshot = task.capability_snapshot
            self.assertEqual(snapshot["shot_version_key"], version_key)
            self.assertEqual(snapshot["shot_version_label"], "V2")
            self.assertEqual(snapshot["batch_shot_ids"], expected_ids)
            self.assertEqual(snapshot["shot_number"], index)

    def test_whole_episode_rejects_unknown_version_without_partial_writes(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload(
                "unknown-storyboard-version",
                shot_version_key="creative-draft:22222222-2222-4222-8222-222222222222",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("shot_version_key", str(response.data))
        self.assertEqual(PromptArtifact.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    def test_whole_episode_idempotency_key_cannot_switch_storyboard_version(self):
        version_key = "creative-draft:33333333-3333-4333-8333-333333333333"
        self.create_storyboard_version(version_key, "V3")
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        first = self.client.post(
            url,
            self.payload("version-bound-idempotency", shot_version_key=version_key),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)

        changed = self.client.post(
            url,
            self.payload(
                "version-bound-idempotency",
                shot_version_key=legacy_shot_version_key(self.episode.id),
            ),
            format="json",
        )

        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(
            changed.data["error"]["code"],
            "production_idempotency_conflict",
        )
        self.assertEqual(GenerationTask.objects.count(), 2)

    def test_task_list_exposes_not_created_slots_without_active_deferred(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("batch-slots-not-created"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        shot3 = Shot.objects.create(
            episode=self.episode,
            number=3,
            title="第三镜",
            narrative="第三镜动作",
            camera="稳定推进",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.FL2VA,
            scene=self.scene,
            requires_first_frame_continuity=True,
        )
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        frozen_ids = [str(self.shot1.id), str(self.shot2.id), str(shot3.id)]
        for task in tasks:
            snapshot = dict(task.capability_snapshot)
            snapshot["batch_size"] = 3
            snapshot["batch_shot_ids"] = frozen_ids
            task.capability_snapshot = snapshot
            task.save(update_fields=("capability_snapshot", "updated_at"))

        by_batch = self.client.get("/api/v1/tasks/", {"batch_id": response.data["batch_id"]})

        self.assertEqual(by_batch.status_code, 200, by_batch.data)
        batch_results = by_batch.data.get("results", by_batch.data)
        self.assertEqual(len(batch_results), 2)
        slots = batch_results[0]["batch_slots"]
        self.assertEqual([slot["batch_index"] for slot in slots], [1, 2, 3])
        self.assertEqual(
            [slot["slot_status"] for slot in slots],
            ["materialized", "materialized", "not_created"],
        )
        self.assertEqual(slots[2]["shot_id"], str(shot3.id))
        self.assertIn("没有 active deferred", slots[2]["message"])

    def test_legacy_batch_without_manifest_does_not_guess_missing_slot_from_episode(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("legacy-batch-no-manifest"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        anchor = tasks[0]
        tasks[1].delete()
        snapshot = dict(anchor.capability_snapshot)
        snapshot.pop("batch_shot_ids", None)
        snapshot.pop("shot_version_key", None)
        snapshot.pop("shot_version_label", None)
        anchor.capability_snapshot = snapshot
        anchor.save(update_fields=("capability_snapshot", "updated_at"))

        Shot.objects.create(
            episode=self.episode,
            number=3,
            title="后来新增的镜头",
            narrative="不得被旧批次读侧吸收。",
            camera="固定镜头",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.T2VA,
            scene=self.scene,
        )

        by_batch = self.client.get(
            "/api/v1/tasks/", {"batch_id": response.data["batch_id"]},
        )

        self.assertEqual(by_batch.status_code, 200, by_batch.data)
        batch_results = by_batch.data.get("results", by_batch.data)
        slots = batch_results[0]["batch_slots"]
        self.assertEqual([slot["slot_status"] for slot in slots], ["materialized", "not_created"])
        self.assertIsNone(slots[1]["shot_id"])
        self.assertEqual(slots[1]["shot_title"], "")

    def test_resume_batch_retries_only_latest_failed_child(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("resume-failed-child"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        tasks[1].status = GenerationTask.Status.FAILED
        tasks[1].error_message = "fixture failure"
        tasks[1].finished_at = timezone.now()
        tasks[1].save(update_fields=("status", "error_message", "finished_at", "updated_at"))

        resumed = self.client.post(
            f"/api/v1/tasks/{tasks[0].id}/resume-batch/",
            {"extended_generation_confirmed": False},
            format="json",
        )

        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertEqual(resumed.data["batch_id"], response.data["batch_id"])
        self.assertEqual(len(resumed.data["retried_task_ids"]), 1)
        retry = GenerationTask.objects.get(pk=resumed.data["retried_task_ids"][0])
        self.assertEqual(retry.capability_snapshot["retry_of"], str(tasks[1].id))
        self.assertEqual(retry.capability_snapshot["batch_id"], response.data["batch_id"])
        self.assertEqual(retry.capability_snapshot["batch_index"], 2)
        self.assertEqual(retry.capability_snapshot["batch_role"], "retry")

    def test_resume_batch_does_not_duplicate_existing_retry(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("resume-existing-retry"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        tasks[1].status = GenerationTask.Status.FAILED
        tasks[1].error_message = "fixture failure"
        tasks[1].finished_at = timezone.now()
        tasks[1].save(update_fields=("status", "error_message", "finished_at", "updated_at"))
        first_resume = self.client.post(
            f"/api/v1/tasks/{tasks[1].id}/resume-batch/",
            {"extended_generation_confirmed": False},
            format="json",
        )
        self.assertEqual(first_resume.status_code, 201, first_resume.data)

        second_resume = self.client.post(
            f"/api/v1/tasks/{tasks[1].id}/resume-batch/",
            {"extended_generation_confirmed": False},
            format="json",
        )

        self.assertEqual(second_resume.status_code, 200, second_resume.data)
        self.assertEqual(second_resume.data["retried_task_ids"], [])
        self.assertEqual(
            GenerationTask.objects.filter(
                capability_snapshot__batch_id=response.data["batch_id"],
            ).count(),
            3,
        )
        retry_id = first_resume.data["retried_task_ids"][0]
        self.assertTrue(any(
            item["task_id"] == retry_id and item["reason"] == "仍在排队或运行中，跳过。"
            for item in second_resume.data["skipped"]
        ))

    def test_resume_batch_is_scoped_to_exact_batch_id(self):
        first = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("resume-exact-batch-a", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("resume-exact-batch-b", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(second.status_code, 201, second.data)
        first_tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=first.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))
        second_tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=second.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))
        for task in (first_tasks[1], second_tasks[1]):
            task.status = GenerationTask.Status.FAILED
            task.error_message = "fixture failure"
            task.finished_at = timezone.now()
            task.save(update_fields=("status", "error_message", "finished_at", "updated_at"))

        resumed = self.client.post(
            f"/api/v1/tasks/{second_tasks[0].id}/resume-batch/",
            {"extended_generation_confirmed": True},
            format="json",
        )

        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertEqual(resumed.data["batch_id"], second.data["batch_id"])
        self.assertEqual(len(resumed.data["retried_task_ids"]), 1)
        retry = GenerationTask.objects.get(pk=resumed.data["retried_task_ids"][0])
        self.assertEqual(retry.capability_snapshot["batch_id"], second.data["batch_id"])
        self.assertEqual(
            GenerationTask.objects.filter(
                capability_snapshot__batch_id=first.data["batch_id"],
                capability_snapshot__retry_of=str(first_tasks[1].id),
            ).count(),
            0,
        )

    def test_cancel_batch_is_scoped_to_exact_batch_id(self):
        first = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("cancel-exact-batch-a", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("cancel-exact-batch-b", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(second.status_code, 201, second.data)
        first_tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=first.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))
        second_tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=second.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))

        cancelled = self.client.post(
            f"/api/v1/tasks/{second_tasks[0].id}/cancel-batch/",
            {},
            format="json",
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["batch_id"], second.data["batch_id"])
        self.assertEqual(set(cancelled.data["cancelled_task_ids"]), {
            str(task.id) for task in second_tasks
        })
        self.assertEqual(
            set(GenerationTask.objects.filter(
                capability_snapshot__batch_id=first.data["batch_id"],
            ).values_list("status", flat=True)),
            {GenerationTask.Status.QUEUED},
        )
        self.assertEqual(
            set(GenerationTask.objects.filter(
                capability_snapshot__batch_id=second.data["batch_id"],
            ).values_list("status", flat=True)),
            {GenerationTask.Status.CANCELLED},
        )

    def test_delete_batch_is_scoped_to_exact_batch_id(self):
        first = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("delete-exact-batch-a", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("delete-exact-batch-b", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(second.status_code, 201, second.data)
        second_tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=second.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))
        for task in second_tasks:
            task.status = GenerationTask.Status.FAILED
            task.error_message = "fixture terminal failure"
            task.finished_at = timezone.now()
            task.save(update_fields=("status", "error_message", "finished_at", "updated_at"))

        deleted = self.client.delete(
            f"/api/v1/tasks/{second_tasks[0].id}/delete-batch/",
            format="json",
        )

        self.assertEqual(deleted.status_code, 200, deleted.data)
        self.assertEqual(deleted.data["batch_id"], second.data["batch_id"])
        self.assertEqual(set(deleted.data["deleted_task_ids"]), {
            str(task.id) for task in second_tasks
        })
        self.assertFalse(GenerationTask.objects.filter(
            capability_snapshot__batch_id=second.data["batch_id"],
        ).exists())
        self.assertEqual(
            GenerationTask.objects.filter(
                capability_snapshot__batch_id=first.data["batch_id"],
            ).count(),
            2,
        )

    def test_delete_batch_rejects_non_terminal_tasks(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("delete-active-batch", extended_generation_confirmed=True),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        tasks = list(GenerationTask.objects.filter(
            capability_snapshot__batch_id=response.data["batch_id"],
        ).order_by("capability_snapshot__batch_index"))
        tasks[0].status = GenerationTask.Status.FAILED
        tasks[0].error_message = "fixture terminal failure"
        tasks[0].finished_at = timezone.now()
        tasks[0].save(update_fields=("status", "error_message", "finished_at", "updated_at"))

        deleted = self.client.delete(
            f"/api/v1/tasks/{tasks[0].id}/delete-batch/",
            format="json",
        )

        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertIn("active_tasks", deleted.data["error"]["details"])
        self.assertEqual(
            GenerationTask.objects.filter(
                capability_snapshot__batch_id=response.data["batch_id"],
            ).count(),
            2,
        )

    def test_whole_episode_reuses_each_latest_unchanged_shot_preview(self):
        preview_ids = []
        for shot in (self.shot1, self.shot2):
            preview = self.client.post("/api/v1/prompts/generate/", {
                "project_id": str(self.project.id),
                "episode_id": str(self.episode.id),
                "shot_ids": [str(shot.id)],
                "prompt_type": "video",
                "h3_mode": shot.h3_mode,
                "multi_shot": False,
                "include_audio": shot.include_audio,
                "shot_durations": [{
                    "shot_id": str(shot.id),
                    "duration_ms": shot.duration_ms,
                }],
                "overall_soundscape": shot.sound_effects,
                "non_diegetic_music": shot.music,
            }, format="json")
            self.assertEqual(preview.status_code, 201, preview.data)
            preview_ids.append(preview.data[0]["id"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("reuse-shot-previews"),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["prompt_artifact_ids"], preview_ids)
        self.assertTrue(all(
            unit["prompt_artifact_reused"]
            for unit in response.data["production_units"]
        ))
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)

    def test_whole_episode_ignores_script_breakdown_multi_shot_preference(self):
        self.episode.prompt_settings = {
            "multi_shot": True,
            "include_audio": True,
            "default_h3_mode": Shot.H3Mode.T2VA,
            "breakdown_method": "deterministic-script-breakdown-v1",
        }
        self.episode.save(update_fields=("prompt_settings", "updated_at"))
        self.shot2.h3_mode = Shot.H3Mode.T2VA
        self.shot2.reference_bindings = {}
        self.shot2.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        self.shot2.reference_assets.clear()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("script-breakdown-whole-episode"),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 2, "workflows": 2, "tasks": 2})
        prompts = list(PromptArtifact.objects.order_by("created_at"))
        self.assertEqual([prompt.shot_id for prompt in prompts], [self.shot1.id, self.shot2.id])
        self.assertTrue(all(not prompt.is_multi_shot for prompt in prompts))
        self.assertEqual(
            [task.capability_snapshot["shot_ids"] for task in GenerationTask.objects.order_by(
                "capability_snapshot__batch_index"
            )],
            [[str(self.shot1.id)], [str(self.shot2.id)]],
        )

    def test_normal_build_still_respects_episode_multi_shot_preference(self):
        self.episode.prompt_settings = {
            "multi_shot": True,
            "include_audio": True,
            "default_h3_mode": Shot.H3Mode.T2VA,
            "breakdown_method": "deterministic-script-breakdown-v1",
        }
        self.episode.save(update_fields=("prompt_settings", "updated_at"))
        self.shot2.h3_mode = Shot.H3Mode.T2VA
        self.shot2.reference_bindings = {}
        self.shot2.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        self.shot2.reference_assets.clear()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            {
                "episode_ids": [str(self.episode.id)],
                "whole_episode": False,
                "multi_shot": False,
                "enqueue": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"prompts": 1, "workflows": 1, "tasks": 0})
        prompt = PromptArtifact.objects.get()
        self.assertTrue(prompt.is_multi_shot)
        self.assertIsNone(prompt.shot_id)
        self.assertEqual(
            prompt.request_options["shot_ids"],
            [str(self.shot1.id), str(self.shot2.id)],
        )

    def test_whole_episode_ref2va_children_inject_only_their_own_shot_prompt(self):
        self.episode.synopsis = (
            "第一镜（0:00-0:05）为“发现线索”，"
            "第二镜（0:05-0:10）为“追击脱离”。"
        )
        self.episode.save(update_fields=("synopsis", "updated_at"))
        for shot in (self.shot1, self.shot2):
            shot.h3_mode = Shot.H3Mode.REF2VA
            shot.reference_bindings = {}
            shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
            shot.reference_assets.clear()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("scoped-ref2va-prompts"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        tasks = list(
            GenerationTask.objects.select_related(
                "generated_workflow__prompt_artifact__shot",
            ).order_by("capability_snapshot__batch_index")
        )
        self.assertEqual(len(tasks), 2)
        for index, (task, own_shot, other_shot) in enumerate((
            (tasks[0], self.shot1, self.shot2),
            (tasks[1], self.shot2, self.shot1),
        ), start=1):
            workflow = task.generated_workflow
            prompt = workflow.prompt_artifact
            self.assertEqual(prompt.shot_id, own_shot.id)
            self.assertFalse(prompt.is_multi_shot)
            self.assertEqual(prompt.total_duration_ms, 5000)
            self.assertEqual(prompt.structured_payload["shot_durations_ms"], [5000])
            summary = prompt.structured_payload["fields"]["summary"]
            self.assertTrue(summary.startswith("[reference generation]"), summary)
            self.assertNotIn(own_shot.title, summary)
            self.assertNotIn(other_shot.title, summary)
            self.assertNotIn(self.episode.synopsis, prompt.prompt_text)
            # A short title can legitimately occur in a continuity Bible
            # field (for example a shot named "闪电" and scene lighting
            # containing "窗外闪电").  The other shot's narrative is the
            # unambiguous cross-shot leakage marker.
            self.assertNotIn(other_shot.narrative, prompt.prompt_text)
            detailed_lines = prompt.structured_payload["fields"]["detailed_description"]
            # Ref2VA keeps one or two style/color sentences before [Shot 1]
            # as required by the official prompt contract.  Scope isolation is
            # therefore measured by shot entries, not by every prose line in
            # the detailed_description section.
            self.assertEqual(
                len([line for line in detailed_lines if line.startswith("[Shot ")]),
                1,
            )
            self.assertEqual(workflow.injected_values["PROMPT"], prompt.prompt_text)
            self.assertEqual(workflow.injected_values["REQUESTED_DURATION_MS"], 5000)
            self.assertEqual(task.capability_snapshot["shot_id"], str(own_shot.id))
            self.assertEqual(task.capability_snapshot["batch_index"], index)

    def test_same_idempotency_request_reuses_batch_and_changed_request_conflicts(self):
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        first = self.client.post(url, self.payload(), format="json")
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.client.post(url, self.payload(), format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(replay.data["batch_id"], first.data["batch_id"])
        self.assertEqual(replay.data["task_ids"], first.data["task_ids"])
        self.assertEqual(GenerationTask.objects.count(), 2)
        self.assertEqual(PromptArtifact.objects.count(), 2)
        self.assertEqual(GeneratedWorkflow.objects.count(), 2)

        conflict = self.client.post(url, self.payload(priority=7), format="json")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(
            conflict.data["error"]["code"], "production_idempotency_conflict",
        )
        self.assertIn("idempotency_key", str(conflict.data))
        self.assertEqual(GenerationTask.objects.count(), 2)

        self.shot1.continuity_notes = "新的连续性约束会改变提示词"
        self.shot1.save(update_fields=("continuity_notes", "updated_at"))
        stale_source = self.client.post(url, self.payload(), format="json")
        self.assertEqual(stale_source.status_code, 409, stale_source.data)
        self.assertEqual(
            stale_source.data["error"]["code"], "production_idempotency_conflict",
        )
        self.assertIn("idempotency_key", str(stale_source.data))
        self.assertEqual(GenerationTask.objects.count(), 2)

    def test_incomplete_whole_episode_batch_is_idempotency_conflict(self):
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        payload = self.payload("incomplete-whole-episode")
        created = self.client.post(url, payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        GenerationTask.objects.filter(pk=created.data["task_ids"][-1]).delete()

        replay = self.client.post(url, payload, format="json")

        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(
            replay.data["error"]["code"], "production_idempotency_conflict",
        )
        self.assertEqual(
            replay.data["error"]["details"]["idempotency_key"],
            payload["idempotency_key"],
        )
        self.assertEqual(GenerationTask.objects.count(), 1)

    def test_idempotency_fingerprint_includes_workflow_selection_and_parameters(self):
        for shot in (self.shot1, self.shot2):
            shot.h3_mode = Shot.H3Mode.REF2VA
            shot.reference_bindings = {}
            shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
            shot.reference_assets.clear()
        variant = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-r2v-t8-dual-clock-4step",
        )
        selection = {
            "output_kind": Shot.MediaType.VIDEO,
            "h3_mode": Shot.H3Mode.REF2VA,
            "template_id": str(variant.id),
            "parameters": {"STEPS": 4, "LORA_STRENGTH": 1.0},
        }
        url = f"/api/v1/projects/{self.project.id}/build-production/"

        first = self.client.post(
            url,
            self.payload("workflow-selection-fingerprint", workflow_selections=[selection]),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.client.post(
            url,
            self.payload("workflow-selection-fingerprint", workflow_selections=[selection]),
            format="json",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["idempotent_replay"])

        changed = self.client.post(
            url,
            self.payload(
                "workflow-selection-fingerprint",
                workflow_selections=[{
                    **selection,
                    "parameters": {"STEPS": 5, "LORA_STRENGTH": 1.0},
                }],
            ),
            format="json",
        )
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(
            changed.data["error"]["code"], "production_idempotency_conflict",
        )
        self.assertIn("idempotency_key", str(changed.data))
        self.assertEqual(GenerationTask.objects.count(), 2)
        self.assertEqual(PromptArtifact.objects.count(), 2)
        self.assertEqual(GeneratedWorkflow.objects.count(), 2)

    def test_whole_episode_fingerprint_canonicalizes_workflow_selection_order(self):
        selections = [
            {
                "output_kind": Shot.MediaType.VIDEO,
                "h3_mode": mode,
                "template_id": str(WorkflowTemplate.objects.get(
                    owner=None, slug=slug,
                ).id),
                "parameters": {},
            }
            for mode, slug in (
                (Shot.H3Mode.T2VA, "minimax-h3-base-video"),
                (Shot.H3Mode.FL2VA, "minimax-h3-first-last-video"),
            )
        ]
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        first = self.client.post(
            url,
            self.payload("canonical-workflow-order", workflow_selections=selections),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.client.post(
            url,
            self.payload(
                "canonical-workflow-order",
                workflow_selections=list(reversed(selections)),
            ),
            format="json",
        )

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(replay.data["task_ids"], first.data["task_ids"])
        self.assertEqual(GenerationTask.objects.count(), 2)

    def test_whole_episode_fingerprint_canonicalizes_related_ids_and_consistency(self):
        class RelatedValues:
            def __init__(self, values):
                self.values = values

            def all(self):
                return list(self.values)

        first_id = UUID(int=1)
        second_id = UUID(int=2)
        related = [SimpleNamespace(id=second_id), SimpleNamespace(id=first_id)]
        characters = RelatedValues(list(related))
        props = RelatedValues(list(related))
        references = RelatedValues(list(related))
        shot = SimpleNamespace(
            id=UUID(int=3),
            number=1,
            title="稳定指纹",
            narrative="稳定指纹",
            detailed_description="",
            camera="固定镜头",
            action="",
            dialogue=[],
            sound_effects="",
            music="",
            duration_ms=5000,
            media_type=Shot.MediaType.VIDEO,
            h3_mode=Shot.H3Mode.T2VA,
            include_audio=True,
            scene_id=None,
            characters=characters,
            props=props,
            reference_assets=references,
            reference_bindings={},
            requires_first_frame_continuity=False,
            requires_last_frame_continuity=False,
            continuity_notes="",
            metadata={},
        )
        data = {
            "shot_version_key": legacy_shot_version_key(self.episode.id),
            "template_id": None,
            "priority": 0,
            "max_attempts": 3,
            "resolution_profile": "project",
            "resolution_multiple": 32,
            "image_resolution_profile": "0.98",
            "image_resolution_multiple": 32,
            "workflow_overrides": {},
            "workflow_selections": [],
        }
        first_consistency = {
            "characters": [
                {"id": str(second_id), "name": "同名角色"},
                {"id": str(first_id), "name": "同名角色"},
            ],
            "locks": [
                {"scope_id": str(second_id), "scope_type": "character"},
                {"scope_id": str(first_id), "scope_type": "character"},
            ],
        }
        first = _episode_batch_fingerprint(
            project=self.project,
            episode=self.episode,
            shots=[shot],
            data=data,
            consistency_snapshot=first_consistency,
        )
        characters.values.reverse()
        props.values.reverse()
        references.values.reverse()
        second = _episode_batch_fingerprint(
            project=self.project,
            episode=self.episode,
            shots=[shot],
            data=data,
            consistency_snapshot={
                "locks": list(reversed(first_consistency["locks"])),
                "characters": list(reversed(first_consistency["characters"])),
            },
        )

        self.assertEqual(first, second)

    def test_retry_preserves_episode_shot_batch_identity_without_breaking_replay(self):
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        created = self.client.post(url, self.payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        source = GenerationTask.objects.get(pk=created.data["task_ids"][0])
        source.status = GenerationTask.Status.FAILED
        source.submission_state = GenerationTask.SubmissionState.TERMINAL_FAILED
        source.error_message = "verified remote failure"
        source.save(update_fields=("status", "submission_state", "error_message", "updated_at"))

        retry = self.client.post(f"/api/v1/tasks/{source.id}/retry/", {}, format="json")
        self.assertEqual(retry.status_code, 201, retry.data)
        self.assertEqual(retry.data["retry_of"], str(source.id))
        self.assertEqual(retry.data["batch_id"], created.data["batch_id"])
        self.assertEqual(retry.data["batch_role"], "retry")
        self.assertEqual(retry.data["shot_id"], str(self.shot1.id))
        self.assertEqual(retry.data["shot_order"], 1)
        self.assertEqual(retry.data["episode_number"], 1)

        replay = self.client.post(url, self.payload(), format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["task_ids"], created.data["task_ids"])
        self.assertNotIn(str(retry.data["id"]), replay.data["task_ids"])

    def test_legacy_task_without_batch_snapshot_still_exposes_episode_and_shot(self):
        created = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("legacy-display-key"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        legacy = GenerationTask.objects.get(pk=created.data["task_ids"][1])
        legacy.capability_snapshot = {}
        legacy.status = GenerationTask.Status.RUNNING
        legacy.save(update_fields=("capability_snapshot", "status", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{legacy.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["batch_id"])
        self.assertEqual(response.data["episode_id"], str(self.episode.id))
        self.assertEqual(response.data["episode_number"], self.episode.number)
        self.assertEqual(response.data["episode_title"], self.episode.title)
        self.assertEqual(response.data["shot_ids"], [str(self.shot2.id)])
        self.assertEqual(response.data["shot_id"], str(self.shot2.id))
        self.assertEqual(response.data["shot_number"], self.shot2.number)
        self.assertEqual(response.data["shot_order"], self.shot2.number)
        self.assertEqual(response.data["shot_title"], self.shot2.title)
        filtered = self.client.get("/api/v1/tasks/", {"shot_id": str(self.shot2.id)})
        self.assertEqual(filtered.status_code, 200, filtered.data)
        filtered_results = filtered.data.get("results", filtered.data)
        self.assertIn(str(legacy.id), [item["id"] for item in filtered_results])

    @override_settings(
        TASK_RECOMMENDED_MAX_PENDING=37,
        TASK_RECOMMENDED_MAX_ACTIVE_PER_USER=19,
        TASK_MAX_CONCURRENT=3,
        TASK_GENERATION_TIMEOUT_SECONDS=4321,
        VIDEO_SHOT_MIN_DURATION_MS=6000,
        VIDEO_SHOT_MAX_DURATION_MS=12000,
        SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS=7000,
        SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY=True,
        SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY=False,
        SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS=500,
        SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS=240,
    )
    def test_production_capability_exposes_configured_guidance_not_business_caps(self):
        response = self.client.get("/api/v1/capabilities/production/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["recommended_max_pending"], 37)
        self.assertEqual(response.data["recommended_max_active_per_user"], 19)
        self.assertEqual(response.data["max_concurrent"], 3)
        self.assertEqual(response.data["task_timeout_seconds"], 4321)
        self.assertEqual(response.data["video_shot_min_duration_ms"], 6000)
        self.assertEqual(response.data["video_shot_max_duration_ms"], 12000)
        self.assertEqual(response.data["script_breakdown_default_shot_duration_ms"], 7000)
        self.assertTrue(response.data["script_breakdown_default_first_frame_continuity"])
        self.assertFalse(response.data["script_breakdown_default_last_frame_continuity"])
        self.assertEqual(response.data["script_breakdown_auto_split_trigger_chars"], 500)
        self.assertEqual(response.data["script_breakdown_auto_split_target_chars"], 240)
        self.assertTrue(response.data["confirmation_required"])
        self.assertFalse(response.data["business_volume_limited"])

    @override_settings(
        TASK_RECOMMENDED_MAX_PENDING=1,
        TASK_RECOMMENDED_MAX_ACTIVE_PER_USER=1,
    )
    def test_confirmed_full_series_build_creates_one_traceable_task_per_shot(self):
        second_episode = Episode.objects.create(
            project=self.project,
            number=2,
            title="第二集",
            synopsis="第二集测试",
            script="第二集测试镜头",
        )
        second_shot = Shot.objects.create(
            episode=second_episode,
            number=1,
            title="第二集镜头",
            narrative="第二集动作",
            camera="稳定推进",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.T2VA,
            scene=self.scene,
        )

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            {
                "multi_shot": False,
                "enqueue": True,
                "extended_generation_confirmed": True,
                "idempotency_key": "confirmed-full-series",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"]["tasks"], 3)
        self.assertEqual(response.data["batch_kind"], "series")
        self.assertEqual(response.data["batch_size"], 3)
        batch_id = response.data["batch_id"]
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        self.assertEqual(len(tasks), 3)
        self.assertEqual(
            [task.capability_snapshot["batch_index"] for task in tasks],
            [1, 2, 3],
        )
        self.assertEqual(
            {task.capability_snapshot["batch_id"] for task in tasks},
            {batch_id},
        )
        self.assertEqual(
            tasks[-1].capability_snapshot["shot_id"],
            str(second_shot.id),
        )

    def test_keyed_series_selection_replays_without_writes_and_conflicts_on_changes(self):
        second_episode = Episode.objects.create(
            project=self.project,
            number=2,
            title="第二集",
            synopsis="第二集测试",
            script="第二集测试镜头",
        )
        second_shot = Shot.objects.create(
            episode=second_episode,
            number=1,
            title="第二集镜头",
            narrative="第二集动作",
            camera="稳定推进",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.T2VA,
            scene=self.scene,
        )
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        payload = {
            "episode_ids": [str(self.episode.id), str(second_episode.id)],
            "whole_episode": False,
            "multi_shot": False,
            "enqueue": True,
            "idempotency_key": "keyed-series-selection",
        }

        first = self.client.post(url, payload, format="json")

        self.assertEqual(first.status_code, 201, first.data)
        self.assertFalse(first.data["whole_episode"])
        self.assertFalse(first.data["idempotent_replay"])
        self.assertEqual(first.data["batch_kind"], "series")
        self.assertEqual(first.data["batch_size"], 3)
        self.assertEqual(len(first.data["tasks"]), 3)
        first_ids = first.data["task_ids"]
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        self.assertEqual(len(tasks), 3)
        self.assertEqual(
            {task.capability_snapshot["idempotency_key"] for task in tasks},
            {payload["idempotency_key"]},
        )
        fingerprints = {
            task.capability_snapshot["request_fingerprint"] for task in tasks
        }
        self.assertEqual(len(fingerprints), 1)
        self.assertTrue(next(iter(fingerprints)))
        counts = (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
        )

        replay = self.client.post(url, payload, format="json")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(replay.data["task_ids"], first_ids)
        self.assertEqual(
            (
                PromptArtifact.objects.count(),
                GeneratedWorkflow.objects.count(),
                GenerationTask.objects.count(),
            ),
            counts,
        )

        for changed in (
            {
                **payload,
                "shot_ids": [str(self.shot1.id), str(second_shot.id)],
            },
            {
                **payload,
                "workflow_overrides": {"SEED": 314159},
            },
        ):
            with self.subTest(changed=changed):
                conflict = self.client.post(url, changed, format="json")
                self.assertEqual(conflict.status_code, 409, conflict.data)
                self.assertEqual(
                    conflict.data["error"]["code"],
                    "production_idempotency_conflict",
                )
                self.assertEqual(
                    conflict.data["error"]["details"]["idempotency_key"],
                    payload["idempotency_key"],
                )
                self.assertEqual(
                    (
                        PromptArtifact.objects.count(),
                        GeneratedWorkflow.objects.count(),
                        GenerationTask.objects.count(),
                    ),
                    counts,
                )

    @override_settings(
        TASK_RECOMMENDED_MAX_PENDING=1,
        TASK_RECOMMENDED_MAX_ACTIVE_PER_USER=1,
    )
    def test_confirmed_one_hundred_episode_series_keeps_every_shot_task(self):
        episodes = [
            Episode(
                project=self.project,
                number=number,
                title=f"第 {number} 集",
                synopsis=f"第 {number} 集概要",
                script=f"第 {number} 集单镜剧情。",
            )
            for number in range(2, 101)
        ]
        Episode.objects.bulk_create(episodes)
        Shot.objects.bulk_create(
            [
                Shot(
                    episode=self.episode,
                    number=shot_number,
                    title=f"第 1 集镜头 {shot_number}",
                    narrative=f"第 1 集第 {shot_number} 镜动作。",
                    camera="稳定推进",
                    duration_ms=5000,
                    h3_mode=Shot.H3Mode.T2VA,
                    scene=self.scene,
                )
                for shot_number in range(3, 12)
            ]
            + [
                Shot(
                    episode=episode,
                    number=shot_number,
                    title=f"第 {episode.number} 集镜头 {shot_number}",
                    narrative=f"第 {episode.number} 集第 {shot_number} 镜动作。",
                    camera="稳定推进",
                    duration_ms=5000,
                    h3_mode=Shot.H3Mode.T2VA,
                    scene=self.scene,
                )
                for episode in episodes
                for shot_number in range(1, 12)
            ]
        )

        payload = {
            "multi_shot": False,
            "enqueue": True,
            "idempotency_key": "one-hundred-episode-series",
        }
        unconfirmed = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            payload,
            format="json",
        )
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.data)
        self.assertEqual(
            unconfirmed.data["error"]["code"],
            "production_queue_confirmation_required",
        )
        self.assertEqual(
            unconfirmed.data["error"]["details"]["requested_task_count"],
            1100,
        )
        self.assertEqual(GenerationTask.objects.count(), 0)

        confirmed = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            {**payload, "extended_generation_confirmed": True},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.data)
        self.assertEqual(confirmed.data["counts"]["tasks"], 1100)
        self.assertEqual(confirmed.data["batch_kind"], "series")
        self.assertEqual(confirmed.data["batch_size"], 1100)
        self.assertEqual(len(confirmed.data["task_ids"]), 1100)
        self.assertEqual(len(confirmed.data["production_units"]), 1100)
        self.assertEqual(GenerationTask.objects.count(), 1100)
        tasks = list(GenerationTask.objects.order_by("capability_snapshot__batch_index"))
        self.assertEqual(
            [task.capability_snapshot["batch_index"] for task in tasks],
            list(range(1, 1101)),
        )
        self.assertEqual(
            {task.capability_snapshot["batch_size"] for task in tasks},
            {1100},
        )

    @override_settings(
        TASK_RECOMMENDED_MAX_PENDING=1,
        TASK_RECOMMENDED_MAX_ACTIVE_PER_USER=1,
    )
    def test_queue_guidance_requires_confirmation_then_keeps_complete_episode_batch(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("capacity-key"),
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "production_queue_confirmation_required",
        )
        self.assertEqual(GenerationTask.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(PromptArtifact.objects.count(), 0)

        confirmed = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload(
                "capacity-key",
                extended_generation_confirmed=True,
            ),
            format="json",
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.data)
        self.assertEqual(GenerationTask.objects.count(), 2)
        self.assertEqual(confirmed.data["counts"]["tasks"], 2)
        self.assertEqual(confirmed.data["batch_kind"], "episode")
        self.assertEqual(confirmed.data["batch_size"], 2)
        self.assertEqual(confirmed.data["extended_generation_confirmed"], True)

    def test_creation_error_rolls_back_all_preceding_children(self):
        # Bypass API validation to simulate an invalid legacy row appearing
        # after the first valid child in episode order.
        Shot.objects.filter(pk=self.shot2.pk).update(duration_ms=4000)
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("atomic-error-key"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(GenerationTask.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(PromptArtifact.objects.count(), 0)

    def test_whole_episode_contract_requires_one_episode_and_idempotency_key(self):
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        missing_key = self.client.post(url, {
            "episode_ids": [str(self.episode.id)],
            "whole_episode": True,
            "shot_version_key": legacy_shot_version_key(self.episode.id),
            "enqueue": True,
        }, format="json")
        self.assertEqual(missing_key.status_code, 400, missing_key.data)
        self.assertIn("idempotency_key", str(missing_key.data))

        missing_version = self.client.post(url, {
            "episode_ids": [str(self.episode.id)],
            "whole_episode": True,
            "idempotency_key": "missing-version-key",
            "enqueue": True,
        }, format="json")
        self.assertEqual(missing_version.status_code, 400, missing_version.data)
        self.assertIn("shot_version_key", str(missing_version.data))

        selected_shot = self.client.post(url, self.payload(
            "selected-shot-key", shot_ids=[str(self.shot1.id)],
        ), format="json")
        self.assertEqual(selected_shot.status_code, 400, selected_shot.data)
        self.assertIn("shot_ids", str(selected_shot.data))

    def test_nonwhole_multi_task_enqueue_requires_key_but_single_task_remains_compatible(self):
        url = f"/api/v1/projects/{self.project.id}/build-production/"
        multi = self.client.post(url, {
            "episode_ids": [str(self.episode.id)],
            "whole_episode": False,
            "multi_shot": False,
            "enqueue": True,
        }, format="json")
        self.assertEqual(multi.status_code, 400, multi.data)
        self.assertIn("idempotency_key", str(multi.data))
        self.assertEqual(GenerationTask.objects.count(), 0)

        single = self.client.post(url, {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot1.id)],
            "whole_episode": False,
            "multi_shot": False,
            "enqueue": True,
        }, format="json")
        self.assertEqual(single.status_code, 201, single.data)
        self.assertEqual(single.data["batch_size"], 1)
        self.assertEqual(single.data["counts"]["tasks"], 1)

    def test_workflow_generate_rejects_non_object_overrides_without_server_error(self):
        created = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self.payload("workflow-overrides-key"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        unit = created.data["production_units"][0]
        response = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": unit["prompt_artifact_id"],
            "template_id": unit["template_id"],
            "overrides": ["not", "an", "object"],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("overrides", str(response.data))
