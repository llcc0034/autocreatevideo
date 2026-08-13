from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import (
    AssemblyJob,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
)
from studio.media_signing import signed_media_token
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.assembly import (
    Clip,
    TaskSelection,
    _ffmpeg_command,
    _probe_media_streams,
    _resolve_ai_label_fontfile,
    _resolve_output,
)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.provenance import assembly_manifest_path
from studio.tests.factories import build_story


class AssemblyTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.comfy_output = root / "comfy-output"
        self.media_root = root / "media"
        self.label_font = root / "label-font.ttf"
        self.comfy_output.mkdir()
        self.label_font.write_bytes(b"test-font")
        self.override = override_settings(
            COMFYUI_OUTPUT_DIR=str(self.comfy_output),
            MEDIA_ROOT=self.media_root,
            FFMPEG_BINARY="ffmpeg-test",
            ASSEMBLY_AI_LABEL_ENABLED=True,
            ASSEMBLY_AI_LABEL_TEXT="AI-generated · MiniMax H3",
            ASSEMBLY_AI_LABEL_FONTFILE=str(self.label_font),
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.probe_patcher = patch(
            "studio.services.assembly._probe_media_streams",
            return_value=frozenset({"video", "audio"}),
        )
        self.probe_mock = self.probe_patcher.start()
        self.addCleanup(self.probe_patcher.stop)
        self.user, self.project, _, _, self.episode, self.shot1, self.shot2 = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="assembly", slug="assembly", output_kind="video",
            definition={"1": {"class_type": "Mock", "inputs": {}}},
        )

    def _successful_task(self, shot, filename):
        result = build_h3_video_prompt(self.project, [shot], H3PromptOptions())
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=shot, created_by=self.user,
            prompt_type="video", h3_mode="T2VA", prompt_text=result.text,
            total_duration_ms=result.total_duration_ms, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
            request_options={"shot_ids": [str(shot.id)]},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=self.template, created_by=self.user,
            workflow_json=self.template.definition, content_hash=(str(shot.id).replace("-", "") * 2)[:64],
        )
        task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=workflow, kind="video", status=GenerationTask.Status.SUCCEEDED,
            progress=100, effective_duration_ms=result.total_duration_ms,
            outputs=[{"kind": "videos", "filename": filename, "subfolder": "episode-1", "type": "output"}],
        )
        path = self.comfy_output / "episode-1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-video")
        return task

    def _successful_multishot_task(self, shots, filename):
        result = build_h3_video_prompt(self.project, shots, H3PromptOptions())
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=None, created_by=self.user,
            prompt_type="video", h3_mode="T2VA", prompt_text=result.text,
            is_multi_shot=True, include_audio=True,
            total_duration_ms=result.total_duration_ms, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
            request_options={"shot_ids": [str(shot.id) for shot in shots]},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=self.template, created_by=self.user,
            workflow_json=self.template.definition, content_hash="b" * 64,
        )
        task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=workflow, kind="video", status=GenerationTask.Status.SUCCEEDED,
            progress=100, effective_duration_ms=result.total_duration_ms,
            outputs=[{"kind": "videos", "filename": filename, "subfolder": "episode-1", "type": "output"}],
        )
        path = self.comfy_output / "episode-1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-multishot-video")
        return task

    @staticmethod
    def _fake_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"assembled-video")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _process_next(self):
        job = claim_next_assembly(worker_id="test-assembly-worker")
        self.assertIsNotNone(job)
        return process_assembly_job(job)

    def test_ffprobe_streams_are_parsed_from_the_actual_media_file(self):
        result = subprocess.CompletedProcess(
            ["ffprobe"], 0,
            stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            stderr="",
        )
        with patch("studio.services.assembly.subprocess.run", return_value=result) as run:
            streams = _probe_media_streams(Path("actual-output.mp4"))

        self.assertEqual(streams, frozenset({"video", "audio"}))
        self.assertEqual(run.call_args.args[0][0], "ffprobe")
        self.assertIn("stream=codec_type", run.call_args.args[0])

    def test_clip_audio_flag_uses_ffprobe_not_prompt_intent(self):
        task = self._successful_task(self.shot1, "silent-shot.mp4")
        prompt = task.generated_workflow.prompt_artifact
        prompt.include_audio = True
        prompt.save(update_fields=("include_audio", "updated_at"))
        self.probe_mock.return_value = frozenset({"video"})

        clip = _resolve_output(TaskSelection(task=task, shot=self.shot1))

        self.assertFalse(clip.has_audio)

    def test_mixed_audio_inputs_fill_missing_stream_with_silence(self):
        task1 = self._successful_task(self.shot1, "audio-shot.mp4")
        task2 = self._successful_task(self.shot2, "silent-shot.mp4")
        clips = [
            Clip(task1, self.comfy_output / "episode-1" / "audio-shot.mp4", True, 5),
            Clip(task2, self.comfy_output / "episode-1" / "silent-shot.mp4", False, 5),
        ]

        command = _ffmpeg_command(clips, self.media_root / "mixed.mp4", 768, 1344)
        filters = command[command.index("-filter_complex") + 1]

        self.assertIn("[0:a:0]", filters)
        self.assertNotIn("[1:a:0]", filters)
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=5.000", filters)
        self.assertIn("concat=n=2:v=1:a=1", filters)
        self.assertIn("[aout]", command)

    def test_all_silent_inputs_are_filled_with_silent_audio(self):
        task1 = self._successful_task(self.shot1, "silent-01.mp4")
        task2 = self._successful_task(self.shot2, "silent-02.mp4")
        clips = [
            Clip(task1, self.comfy_output / "episode-1" / "silent-01.mp4", False, 5),
            Clip(task2, self.comfy_output / "episode-1" / "silent-02.mp4", False, 5),
        ]

        command = _ffmpeg_command(clips, self.media_root / "silent.mp4", 768, 1344)
        filters = command[command.index("-filter_complex") + 1]

        self.assertNotIn(":a:0]", filters)
        self.assertEqual(filters.count("anullsrc=r=48000:cl=stereo"), 2)
        self.assertIn("concat=n=2:v=1:a=1", filters)
        self.assertIn("[aout]", command)
        self.assertIn("-c:a", command)

    def test_silent_sources_report_audio_for_the_assembled_output(self):
        self._successful_task(self.shot1, "silent-01.mp4")
        self._successful_task(self.shot2, "silent-02.mp4")
        self.probe_mock.return_value = frozenset({"video"})

        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["has_audio"])
        filters = response.data["ffmpeg_command"][
            response.data["ffmpeg_command"].index("-filter_complex") + 1
        ]
        self.assertEqual(filters.count("anullsrc=r=48000:cl=stereo"), 2)

    def test_media_probe_is_cached_for_repeated_multishot_source(self):
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        self._successful_multishot_task([self.shot1, self.shot2], "whole-source.mp4")
        self.probe_mock.reset_mock()

        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.probe_mock.assert_called_once()

    def test_episode_dry_run_writes_ordered_manifest_without_completing(self):
        task1 = self._successful_task(self.shot1, "shot-01.mp4")
        task2 = self._successful_task(self.shot2, "shot-02.mp4")
        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], AssemblyJob.Status.PLANNED)
        manifest = Path(response.data["concat_manifest_path"])
        self.assertTrue(manifest.is_file())
        text = manifest.read_text(encoding="utf-8")
        self.assertLess(text.index("shot-01.mp4"), text.index("shot-02.mp4"))
        self.assertLess(text.index(str(self.shot1.id)), text.index(str(self.shot2.id)))
        self.assertEqual({str(value) for value in response.data["source_tasks"]}, {str(task1.id), str(task2.id)})
        command = response.data["ffmpeg_command"]
        joined = " ".join(command)
        self.assertIn("drawtext=fontfile=", joined)
        self.assertIn("text='AI-generated · MiniMax H3'", joined)
        self.assertIn("comment=AI-generated · MiniMax H3", command)
        self.assertIn("description=AI-generated content produced with MiniMax H3", command)
        self.episode.refresh_from_db()
        self.assertNotEqual(self.episode.status, "completed")

    def test_disabled_visible_label_keeps_mp4_metadata_without_drawtext(self):
        task = self._successful_task(self.shot1, "clean-shot.mp4")
        clips = [
            Clip(task, self.comfy_output / "episode-1" / "clean-shot.mp4", False, 5),
        ]

        with override_settings(ASSEMBLY_AI_LABEL_ENABLED=False):
            command = _ffmpeg_command(clips, self.media_root / "clean.mp4", 768, 1344)

        joined = " ".join(command)
        self.assertNotIn("drawtext=", joined)
        self.assertIn("comment=AI-generated · MiniMax H3", command)
        self.assertIn("description=AI-generated content produced with MiniMax H3", command)
        self.assertIn("[vout]", command[command.index("-filter_complex") + 1])

    def test_execute_marks_complete_only_after_nonempty_output(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        self._successful_task(self.shot2, "shot-02.mp4")

        with patch("studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg) as run:
            response = self.client.post("/api/v1/assemblies/episode/", {
                "episode_id": str(self.episode.id), "dry_run": False,
                "output_name": "episode-final.mp4",
            }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], AssemblyJob.Status.QUEUED)
        run.assert_not_called()

        with patch("studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg) as run:
            completed = self._process_next()
        self.assertEqual(completed.status, AssemblyJob.Status.SUCCEEDED)
        run.assert_called_once()
        completed_response = self.client.get(f"/api/v1/assemblies/{completed.id}/")
        self.assertEqual(completed_response.status_code, 200, completed_response.data)
        self.assertTrue(Path(completed_response.data["output_path"]).is_file())
        self.assertTrue(completed_response.data["output_url"].startswith("/api/v1/media/"))
        anonymous = APIClient()
        preview = anonymous.get(completed_response.data["output_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview["Content-Disposition"].startswith("inline;"))
        preview.close()
        download = anonymous.get(completed_response.data["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))
        download.close()
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.status, "completed")

    def test_assembly_provenance_is_hash_only_tenant_bound_and_detects_tampering(self):
        task1 = self._successful_task(self.shot1, "source-01.mp4")
        task2 = self._successful_task(self.shot2, "source-02.mp4")
        with patch(
            "studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg,
        ):
            queued = self.client.post("/api/v1/assemblies/episode/", {
                "episode_id": str(self.episode.id), "dry_run": False,
                "output_name": "sealed-episode.mp4",
            }, format="json")
            completed = self._process_next()
        self.assertEqual(queued.status_code, 201, queued.data)
        self.assertEqual(completed.status, AssemblyJob.Status.SUCCEEDED)

        detail = self.client.get(f"/api/v1/assemblies/{completed.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertTrue(detail.data["ai_generated"])
        self.assertTrue(detail.data["provenance_url"].startswith("/api/v1/media/"))
        anonymous = APIClient()
        manifest_response = anonymous.get(detail.data["provenance_url"])
        self.assertEqual(manifest_response.status_code, 200)
        document = manifest_response.json()
        self.assertTrue(document["disclosure"]["ai_generated"])
        self.assertEqual(document["verification"]["status"], "verified")
        self.assertEqual(document["assembly"]["job_id"], str(completed.id))
        self.assertEqual(
            {row["task_id"] for row in document["source_outputs"]},
            {str(task1.id), str(task2.id)},
        )
        self.assertEqual(
            [row["shot_id"] for row in document["source_outputs"]],
            [str(self.shot1.id), str(self.shot2.id)],
        )
        self.assertEqual(
            [row["clip_index"] for row in document["source_outputs"]], [0, 1],
        )
        for row in document["source_outputs"]:
            self.assertRegex(row["artifact_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["prompt_sha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertNotIn(task1.generated_workflow.prompt_artifact.prompt_text, serialized)
        self.assertNotIn(str(Path(self.temp.name)), serialized)

        download = anonymous.get(detail.data["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["X-AI-Generated"], "true")
        self.assertEqual(
            download["X-Artifact-SHA256"], document["artifact"]["sha256"],
        )
        self.assertEqual(
            download["X-Provenance-Manifest"], detail.data["provenance_url"],
        )
        download.close()

        other = get_user_model().objects.create_user(
            username="assembly-provenance-bob", password="StrongPass123!",
        )
        wrong_owner_token = signed_media_token(
            purpose="assembly_provenance", object_id=completed.id, owner_id=other.id,
        )
        self.assertEqual(
            anonymous.get(reverse("signed-media", kwargs={"token": wrong_owner_token})).status_code,
            404,
        )

        output_path = Path(completed.output_path)
        output_path.write_bytes(b"tampered-assembly")
        self.assertEqual(anonymous.get(detail.data["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(detail.data["output_url"]).status_code, 404)
        self.assertEqual(anonymous.get(detail.data["download_url"]).status_code, 404)

        output_path.write_bytes(b"assembled-video")
        sidecar = assembly_manifest_path(completed)
        tampered = json.loads(sidecar.read_text(encoding="utf-8"))
        tampered["assembly"]["models"] = ["tampered-model"]
        sidecar.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertEqual(anonymous.get(detail.data["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(detail.data["output_url"]).status_code, 404)
        self.assertEqual(anonymous.get(detail.data["download_url"]).status_code, 404)

    def test_same_sources_and_trim_windows_reuse_succeeded_assembly(self):
        task1 = self._successful_task(self.shot1, "shot-01.mp4")
        task2 = self._successful_task(self.shot2, "shot-02.mp4")
        payload = {
            "episode_id": str(self.episode.id),
            "dry_run": False,
            "output_name": "stable-episode.mp4",
        }

        with patch(
            "studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg,
        ) as ffmpeg_run:
            first = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            second = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            self.assertEqual(second.data["id"], first.data["id"])
            self.assertEqual(second.data["status"], AssemblyJob.Status.QUEUED)
            self._process_next()
            third = self.client.post("/api/v1/assemblies/episode/", payload, format="json")

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(third.data["id"], first.data["id"])
        self.assertEqual(third.data["status"], AssemblyJob.Status.SUCCEEDED)
        self.assertEqual(ffmpeg_run.call_count, 1)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        job = AssemblyJob.objects.get()
        self.assertIn(".source-", job.concat_manifest_path)
        self.assertEqual(
            {str(value) for value in job.source_tasks.values_list("id", flat=True)},
            {str(task1.id), str(task2.id)},
        )

    def test_changed_multishot_trim_windows_do_not_reuse_assembly(self):
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        task = self._successful_multishot_task(
            [self.shot1, self.shot2], "whole-episode.mp4",
        )
        payload = {"episode_id": str(self.episode.id), "dry_run": False}

        with patch(
            "studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg,
        ) as ffmpeg_run:
            first = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            self._process_next()
            prompt = task.generated_workflow.prompt_artifact
            structured_payload = dict(prompt.structured_payload)
            structured_payload["shot_durations_ms"] = [4000, 6000]
            prompt.structured_payload = structured_payload
            prompt.save(update_fields=("structured_payload", "updated_at"))
            second = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            self._process_next()

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(ffmpeg_run.call_count, 2)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        signatures = {
            Path(value).name.split(".source-", 1)[1]
            for value in AssemblyJob.objects.values_list("concat_manifest_path", flat=True)
        }
        self.assertEqual(len(signatures), 2)

    def test_missing_assembled_output_is_not_reused(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        self._successful_task(self.shot2, "shot-02.mp4")
        payload = {"episode_id": str(self.episode.id), "dry_run": False}

        with patch(
            "studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg,
        ) as ffmpeg_run:
            first = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            self._process_next()
            Path(first.data["output_path"]).unlink()
            second = self.client.post("/api/v1/assemblies/episode/", payload, format="json")
            self._process_next()

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(ffmpeg_run.call_count, 2)
        self.assertEqual(AssemblyJob.objects.count(), 2)

    def test_missing_successful_shot_output_is_rejected(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 0)

    def test_episode_assembly_ignores_image_reference_shots(self):
        task = self._successful_task(self.shot1, "shot-01.mp4")
        self.shot2.media_type = "image"
        self.shot2.save(update_fields=("media_type", "updated_at"))

        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([str(value) for value in response.data["source_tasks"]], [str(task.id)])

    def test_episode_without_video_shots_has_clear_error(self):
        self.shot1.media_type = "image"
        self.shot2.media_type = "image"
        self.shot1.save(update_fields=("media_type", "updated_at"))
        self.shot2.save(update_fields=("media_type", "updated_at"))

        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("没有可组装的视频分镜", str(response.data))

    def test_new_single_shot_replaces_only_its_segment_from_older_multishot_video(self):
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        old_multishot = self._successful_multishot_task(
            [self.shot1, self.shot2], "old-multishot.mp4",
        )
        new_single = self._successful_task(self.shot1, "new-shot-01.mp4")

        response = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            {str(value) for value in response.data["source_tasks"]},
            {str(old_multishot.id), str(new_single.id)},
        )
        command = response.data["ffmpeg_command"]
        self.assertLess(command.index(str(self.comfy_output / "episode-1" / "new-shot-01.mp4")),
                        command.index(str(self.comfy_output / "episode-1" / "old-multishot.mp4")))
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[1:v:0]trim=start=5.000:duration=5.000", filters)
        self.assertIn("[1:a:0]atrim=start=5.000:duration=5.000", filters)

    def test_newer_active_shot_task_blocks_assembly_until_it_finishes(self):
        old_task = self._successful_task(self.shot1, "old-shot-01.mp4")
        self._successful_task(self.shot2, "old-shot-02.mp4")
        pending = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=old_task.generated_workflow, kind="video",
            status=GenerationTask.Status.QUEUED,
        )

        blocked = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("最新视频仍生成中", str(blocked.data))
        self.assertEqual(AssemblyJob.objects.count(), 0)

        pending.status = GenerationTask.Status.FAILED
        pending.save(update_fields=("status", "updated_at"))
        fallback = self.client.post("/api/v1/assemblies/episode/", {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")
        self.assertEqual(fallback.status_code, 201, fallback.data)

    def test_missing_configured_label_font_returns_a_clear_failure(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        self._successful_task(self.shot2, "shot-02.mp4")
        missing = self.media_root / "missing-font.ttf"
        with override_settings(ASSEMBLY_AI_LABEL_FONTFILE=str(missing)):
            response = self.client.post("/api/v1/assemblies/episode/", {
                "episode_id": str(self.episode.id), "dry_run": True,
            }, format="json")
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("AI 标识字体文件不存在", response.data["error"])
        self.assertEqual(response.data["assembly"]["status"], AssemblyJob.Status.FAILED)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        persisted = AssemblyJob.objects.get(pk=response.data["assembly"]["id"])
        self.assertEqual(persisted.status, AssemblyJob.Status.FAILED)
        self.assertIn("AI 标识字体文件不存在", persisted.error_message)
        self.assertIsNotNone(persisted.finished_at)
        self.assertFalse(list(self.media_root.rglob("*.ffconcat")))

    def test_windows_uses_an_installed_font_when_no_fontfile_is_configured(self):
        windows_root = Path(self.temp.name) / "Windows"
        fonts_dir = windows_root / "Fonts"
        fonts_dir.mkdir(parents=True)
        expected = fonts_dir / "msyh.ttc"
        expected.write_bytes(b"test-font")
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"WINDIR": str(windows_root)}),
            override_settings(ASSEMBLY_AI_LABEL_FONTFILE=""),
        ):
            resolved = _resolve_ai_label_fontfile()
        self.assertEqual(resolved, expected.resolve().as_posix())
