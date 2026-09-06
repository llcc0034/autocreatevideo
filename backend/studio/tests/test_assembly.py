from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import (
    AssemblyJob,
    ConfigRevision,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
)
from studio.media_signing import signed_media_token
from studio.tests.media_prompt_fixtures import MediaPromptOptions, build_test_video_prompt
from studio.services.assembly import (
    AssemblyError,
    Clip,
    TaskSelection,
    _ffmpeg_command,
    _find_reusable_assembly,
    _probe_media_streams,
    _resolve_ai_label_fontfile,
    _resolve_output,
    create_assembly,
)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.configuration import create_config_revision, publish_config_revision
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
        self.media_facts_patcher = patch(
            "studio.services.assembly.verified_local_media_facts",
            side_effect=lambda path, expected_kind, **_kwargs: {
                "mime_type": "image/png" if expected_kind == "image" else "video/mp4",
                "size_bytes": Path(path).stat().st_size,
                "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            },
        )
        self.media_facts_patcher.start()
        self.addCleanup(self.media_facts_patcher.stop)
        self.user, self.project, _, _, self.episode, self.shot1, self.shot2 = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="assembly", slug="assembly", output_kind="video",
            definition={"1": {"class_type": "Mock", "inputs": {}}},
        )

    def _successful_task(self, shot, filename):
        result = build_test_video_prompt(
            self.project, [shot], MediaPromptOptions(
                model_identifier="minimax-h3-test", language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=True,
            project=self.project, episode=self.episode, shot=shot, created_by=self.user,
            prompt_type="video", conditioning_mode="T2VA", prompt_text=result.text,
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
            max_attempts=3,
            outputs=[{"kind": "videos", "filename": filename, "subfolder": "episode-1", "type": "output"}],
        )
        path = self.comfy_output / "episode-1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-video")
        return task

    def _successful_multishot_task(self, shots, filename):
        result = build_test_video_prompt(
            self.project, shots, MediaPromptOptions(
                model_identifier="minimax-h3-test", language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=None, created_by=self.user,
            prompt_type="video", conditioning_mode="T2VA", prompt_text=result.text,
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
            max_attempts=3,
            outputs=[{"kind": "videos", "filename": filename, "subfolder": "episode-1", "type": "output"}],
        )
        path = self.comfy_output / "episode-1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-multishot-video")
        return task

    def _successful_image_task(self, shot, filename):
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=False,
            project=self.project,
            episode=self.episode,
            shot=shot,
            created_by=self.user,
            prompt_type="image",
            prompt_text="A production still.",
            total_duration_ms=shot.duration_ms,
            structured_payload={"duration_ms": shot.duration_ms},
            consistency_snapshot={},
            content_hash=(str(shot.id).replace("-", "") * 2)[:64],
            request_options={"shot_ids": [str(shot.id)]},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            content_hash=("f" + str(shot.id).replace("-", "") * 2)[:64],
        )
        task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.IMAGE,
            status=GenerationTask.Status.SUCCEEDED,
            progress=100,
            max_attempts=3,
            outputs=[{"kind": "images", "filename": filename, "subfolder": "episode-1", "type": "output"}],
        )
        path = self.comfy_output / "episode-1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-image")
        return task

    @staticmethod
    def _fake_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"assembled-video")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _process_next(self):
        job = claim_next_assembly(worker_id="test-assembly-worker")
        self.assertIsNotNone(job)
        return process_assembly_job(job)

    def _create_episode(self, payload, **_kwargs):
        """Exercise the ProductionRun assembly service without restoring its retired public API."""
        try:
            job = create_assembly(
                owner=self.user,
                project=self.project,
                episode=self.episode,
                dry_run=bool(payload.get("dry_run", True)),
                output_name=str(payload.get("output_name") or ""),
            )
        except AssemblyError as exc:
            if exc.job is not None:
                return SimpleNamespace(
                    status_code=422,
                    data={"error": str(exc), "assembly": {
                        "id": str(exc.job.id),
                        "status": exc.job.status,
                    }},
                )
            return SimpleNamespace(status_code=400, data={"error": str(exc)})
        return SimpleNamespace(
            status_code=201,
            data={
                "id": str(job.id),
                "status": job.status,
                "has_audio": job.has_audio,
                "ffmpeg_command": job.ffmpeg_command,
                "concat_manifest_path": job.concat_manifest_path,
                "source_tasks": list(job.source_tasks.values_list("id", flat=True)),
                "output_path": job.output_path,
            },
        )

    def test_ffprobe_streams_are_parsed_from_the_actual_media_file(self):
        result = subprocess.CompletedProcess(
            ["ffprobe"], 0,
            stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            stderr="",
        )
        with patch("studio.services.assembly.subprocess.run", return_value=result) as run:
            streams = _probe_media_streams(Path("actual-output.mp4"))

        self.assertEqual(streams, frozenset({"video", "audio"}))
        self.assertEqual(Path(run.call_args.args[0][0]).stem.lower(), "ffprobe")
        self.assertIn("stream=codec_type", run.call_args.args[0])

    def test_clip_audio_flag_uses_ffprobe_not_prompt_intent(self):
        task = self._successful_task(self.shot1, "silent-shot.mp4")
        prompt = task.generated_workflow.prompt_artifact
        prompt.include_audio = True
        prompt.save(update_fields=("include_audio", "updated_at"))
        self.probe_mock.return_value = frozenset({"video"})

        clip = _resolve_output(TaskSelection(task=task, shot=self.shot1))

        self.assertFalse(clip.has_audio)

    def test_resolve_output_preserves_the_approved_subsecond_shot_duration(self):
        task = self._successful_task(self.shot1, "subsecond-shot.mp4")
        self.shot1.duration_ms = 50
        self.shot1.save(update_fields=("duration_ms", "updated_at"))

        clip = _resolve_output(TaskSelection(task=task, shot=self.shot1))

        self.assertEqual(clip.duration_seconds, 0.05)

    def test_mixed_audio_inputs_fill_missing_stream_with_silence(self):
        task1 = self._successful_task(self.shot1, "audio-shot.mp4")
        task2 = self._successful_task(self.shot2, "silent-shot.mp4")
        clips = [
            Clip(task1, self.comfy_output / "episode-1" / "audio-shot.mp4", True, 5),
            Clip(task2, self.comfy_output / "episode-1" / "silent-shot.mp4", False, 5),
        ]

        command = _ffmpeg_command(clips, self.media_root / "mixed.mp4", 768, 1344, 24)
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

        command = _ffmpeg_command(clips, self.media_root / "silent.mp4", 768, 1344, 24)
        filters = command[command.index("-filter_complex") + 1]

        self.assertNotIn(":a:0]", filters)
        self.assertEqual(filters.count("anullsrc=r=48000:cl=stereo"), 2)
        self.assertIn("concat=n=2:v=1:a=1", filters)
        self.assertIn("[aout]", command)
        self.assertIn("-c:a", command)

    def test_still_image_input_is_looped_and_trimmed_to_approved_duration(self):
        self.shot1.media_type = "image"
        self.shot1.duration_ms = 5000
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        task = self._successful_image_task(self.shot1, "still.png")
        clip = Clip(
            task,
            self.comfy_output / "episode-1" / "still.png",
            False,
            5,
            media_kind="image",
        )

        command = _ffmpeg_command([clip], self.media_root / "still.mp4", 768, 1344, 24)
        filters = command[command.index("-filter_complex") + 1]
        image_path_index = command.index(str(clip.path))

        self.assertEqual(command[image_path_index - 5:image_path_index], [
            "-loop", "1", "-framerate", "24", "-i",
        ])
        self.assertIn("trim=duration=5.000", filters)
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=5.000", filters)

    def test_silent_sources_report_audio_for_the_assembled_output(self):
        self._successful_task(self.shot1, "silent-01.mp4")
        self._successful_task(self.shot2, "silent-02.mp4")
        self.probe_mock.return_value = frozenset({"video"})

        response = self._create_episode( {
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

        response = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.probe_mock.assert_called_once()

    def test_multishot_video_uses_approved_shot_duration_not_prompt_window(self):
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        task = self._successful_multishot_task(
            [self.shot1, self.shot2], "approved-duration-source.mp4",
        )
        self.shot2.duration_ms = 3000
        self.shot2.save(update_fields=("duration_ms", "updated_at"))

        clip = _resolve_output(TaskSelection(task=task, shot=self.shot2))

        self.assertEqual(clip.start_seconds, 5)
        self.assertEqual(clip.duration_seconds, 3)

    def test_episode_dry_run_writes_ordered_manifest_without_completing(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "delivery.ai_label_enabled": True,
                "delivery.ai_label_text": "AI-generated DB label",
            },
            created_by=self.user,
            change_summary="assembly disclosure label test",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.user)
        task1 = self._successful_task(self.shot1, "shot-01.mp4")
        task2 = self._successful_task(self.shot2, "shot-02.mp4")
        response = self._create_episode( {
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
        self.assertIn("text='AI-generated DB label'", joined)
        self.assertIn("comment=AI-generated DB label", command)
        self.assertIn("description=AI-generated DB label", command)
        self.episode.refresh_from_db()
        self.assertNotEqual(self.episode.status, "completed")

    def test_disabled_visible_label_keeps_mp4_metadata_without_drawtext(self):
        task = self._successful_task(self.shot1, "clean-shot.mp4")
        clips = [
            Clip(task, self.comfy_output / "episode-1" / "clean-shot.mp4", False, 5),
        ]

        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "delivery.ai_label_enabled": False,
                "delivery.ai_label_text": "AI-generated studio label",
            },
            created_by=self.user,
            change_summary="assembly disclosure test",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.user)
        command = _ffmpeg_command(clips, self.media_root / "clean.mp4", 768, 1344, 24)

        joined = " ".join(command)
        self.assertNotIn("drawtext=", joined)
        self.assertIn("comment=AI-generated studio label", command)
        self.assertIn("description=AI-generated studio label", command)
        self.assertIn("[vout]", command[command.index("-filter_complex") + 1])

    def test_project_delivery_fps_drives_still_inputs_filters_and_signature(self):
        self.project.fps = 30
        self.project.save(update_fields=("fps", "updated_at"))
        self.shot1.media_type = "image"
        self.shot2.media_type = "image"
        self.shot1.save(update_fields=("media_type", "updated_at"))
        self.shot2.save(update_fields=("media_type", "updated_at"))
        self._successful_image_task(self.shot1, "still-30fps.png")
        self._successful_image_task(self.shot2, "still-30fps-2.png")

        response = self._create_episode({
            "episode_id": str(self.episode.id),
            "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        command = response.data["ffmpeg_command"]
        self.assertIn("30", command[command.index("-framerate") + 1])
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("fps=30", filters)
        self.assertNotIn("MiniMax", " ".join(command))

    def test_execute_marks_complete_only_after_nonempty_output(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        self._successful_task(self.shot2, "shot-02.mp4")

        with patch("studio.services.assembly_queue._run_ffmpeg", side_effect=self._fake_ffmpeg) as run:
            response = self._create_episode( {
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
            queued = self._create_episode( {
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
        self.addCleanup(manifest_response.close)
        self.assertEqual(manifest_response.status_code, 200)
        document = manifest_response.json()
        manifest_response.close()
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
        self.addCleanup(download.close)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["X-AI-Generated"], "true")
        self.assertEqual(
            download["X-Artifact-SHA256"], document["artifact"]["sha256"],
        )
        provenance_header = download["X-Provenance-Manifest"]
        self.assertTrue(provenance_header.startswith("/api/v1/media/"))
        header_manifest = anonymous.get(provenance_header)
        self.addCleanup(header_manifest.close)
        self.assertEqual(header_manifest.status_code, 200)
        header_document = header_manifest.json()
        self.assertEqual(header_document["artifact"]["sha256"], document["artifact"]["sha256"])
        self.assertEqual(header_document["assembly"]["job_id"], document["assembly"]["job_id"])
        self.assertEqual(header_document["source_outputs"], document["source_outputs"])
        self.assertEqual(header_document["verification"]["status"], "verified")
        header_manifest.close()
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
            first = self._create_episode( payload, format="json")
            second = self._create_episode( payload, format="json")
            self.assertEqual(second.data["id"], first.data["id"])
            self.assertEqual(second.data["status"], AssemblyJob.Status.QUEUED)
            self._process_next()
            third = self._create_episode( payload, format="json")

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

    def test_legacy_mixed_media_contract_is_never_reused(self):
        signature = "a" * 64
        AssemblyJob.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            scope=AssemblyJob.Scope.EPISODE,
            status=AssemblyJob.Status.SUCCEEDED,
            dry_run=False,
            max_attempts=3,
            source_signature=signature,
            source_contract_version=2,
            output_path=str(self.media_root / "legacy-mixed.mp4"),
        )

        with patch(
            "studio.services.assembly._reusable_output_exists",
            return_value=True,
        ) as reusable:
            result = _find_reusable_assembly(
                owner=self.user,
                project=self.project,
                episode=self.episode,
                source_signature=signature,
            )

        self.assertIsNone(result)
        reusable.assert_not_called()
        self.assertEqual(AssemblyJob.objects.count(), 1)

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
            first = self._create_episode( payload, format="json")
            self._process_next()
            prompt = task.generated_workflow.prompt_artifact
            structured_payload = dict(prompt.structured_payload)
            structured_payload["shot_durations_ms"] = [4000, 6000]
            prompt.structured_payload = structured_payload
            prompt.save(update_fields=("structured_payload", "updated_at"))
            second = self._create_episode( payload, format="json")
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
            first = self._create_episode( payload, format="json")
            self._process_next()
            Path(first.data["output_path"]).unlink()
            second = self._create_episode( payload, format="json")
            self._process_next()

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(ffmpeg_run.call_count, 2)
        self.assertEqual(AssemblyJob.objects.count(), 2)

    def test_missing_successful_shot_output_is_rejected(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        response = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 0)

    def test_episode_assembly_includes_image_and_video_in_storyboard_order(self):
        self.shot1.media_type = "image"
        self.shot1.duration_ms = 5000
        self.shot2.duration_ms = 5000
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        self.shot2.save(update_fields=("duration_ms", "updated_at"))
        image_task = self._successful_image_task(self.shot1, "shot-01.png")
        video_task = self._successful_task(self.shot2, "shot-02.mp4")

        response = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            {str(value) for value in response.data["source_tasks"]},
            {str(image_task.id), str(video_task.id)},
        )
        job = AssemblyJob.objects.get(pk=response.data["id"])
        sources = list(job.source_items.order_by("ordinal"))
        self.assertEqual([source.shot_id for source in sources], [self.shot1.id, self.shot2.id])
        self.assertEqual([source.media_kind for source in sources], ["image", "video"])
        filters = response.data["ffmpeg_command"][
            response.data["ffmpeg_command"].index("-filter_complex") + 1
        ]
        self.assertEqual(filters.count("tpad=stop_mode=clone:stop_duration=5.000"), 2)
        self.assertIn("concat=n=2:v=1:a=1", filters)

    def test_episode_with_only_images_creates_a_video_timeline(self):
        self.shot1.media_type = "image"
        self.shot2.media_type = "image"
        self.shot1.save(update_fields=("media_type", "updated_at"))
        self.shot2.save(update_fields=("media_type", "updated_at"))
        self._successful_image_task(self.shot1, "shot-01.png")
        self._successful_image_task(self.shot2, "shot-02.png")

        response = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        job = AssemblyJob.objects.get(pk=response.data["id"])
        self.assertEqual(list(job.source_items.values_list("media_kind", flat=True)), ["image", "image"])

    def test_missing_selected_image_source_fails_closed(self):
        self.shot1.media_type = "image"
        self.shot1.save(update_fields=("media_type", "updated_at"))
        self._successful_task(self.shot2, "shot-02.mp4")

        response = self._create_episode({
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("分镜 1", str(response.data))
        self.assertIn("图片", str(response.data))

    def test_new_single_shot_replaces_only_its_segment_from_older_multishot_video(self):
        for shot in (self.shot1, self.shot2):
            shot.duration_ms = 5000
            shot.save(update_fields=("duration_ms", "updated_at"))
        old_multishot = self._successful_multishot_task(
            [self.shot1, self.shot2], "old-multishot.mp4",
        )
        new_single = self._successful_task(self.shot1, "new-shot-01.mp4")

        response = self._create_episode( {
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
            max_attempts=3,
        )

        blocked = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")

        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("最新视频仍生成中", str(blocked.data))
        self.assertEqual(AssemblyJob.objects.count(), 0)

        pending.status = GenerationTask.Status.FAILED
        pending.save(update_fields=("status", "updated_at"))
        fallback = self._create_episode( {
            "episode_id": str(self.episode.id), "dry_run": True,
        }, format="json")
        self.assertEqual(fallback.status_code, 201, fallback.data)

    def test_missing_configured_label_font_returns_a_clear_failure(self):
        self._successful_task(self.shot1, "shot-01.mp4")
        self._successful_task(self.shot2, "shot-02.mp4")
        missing = self.media_root / "missing-font.ttf"
        with override_settings(ASSEMBLY_AI_LABEL_FONTFILE=str(missing)):
            response = self._create_episode( {
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
