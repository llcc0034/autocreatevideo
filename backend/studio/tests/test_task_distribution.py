from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from studio.models import GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.services.distribution import (
    DISTRIBUTION_DESCRIPTION,
    SDXL_DISTRIBUTION_DESCRIPTION,
    DistributionError,
    distribution_artifact_path,
    distribution_manifest_path,
    prepare_task_distribution,
    verified_distribution_manifest,
)
from studio.services.h3_prompts import H3PromptOptions, build_consistent_image_prompt, build_h3_video_prompt
from studio.services.provenance import persist_task_provenance, sha256_file
from studio.tests.factories import build_story


class ManagedTaskDistributionTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output_root = root / "comfy-output"
        self.output_root.mkdir()
        self.media_root = root / "media"
        windows_fonts = Path(r"C:\Windows\Fonts")
        font_candidates = [
            windows_fonts / "msyh.ttc",
            windows_fonts / "arial.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        self.font = next((path for path in font_candidates if path.is_file()), None)
        if self.font is None:
            self.skipTest("No test font is installed")
        self.override = override_settings(
            DEPLOYMENT_PROFILE="production",
            COMFYUI_OUTPUT_DIR=str(self.output_root),
            MEDIA_ROOT=self.media_root,
            TASK_DISTRIBUTION_ENABLED=True,
            TASK_DISTRIBUTION_LABEL_TEXT="AI-generated · MiniMax H3",
            TASK_IMAGE_DISTRIBUTION_LABEL_TEXT="AI-generated · SDXL 1.0",
            TASK_DISTRIBUTION_FONTFILE=str(self.font),
            TASK_DISTRIBUTION_VERSION="shot-public-v1",
            TASK_DISTRIBUTION_TIMEOUT_SECONDS=30,
            FFMPEG_BINARY=shutil.which("ffmpeg") or "ffmpeg",
            FFPROBE_BINARY=shutil.which("ffprobe") or "ffprobe",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user, self.project, _, _, self.episode, self.shot, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _task(self, path: Path, *, kind: str) -> GenerationTask:
        result = (
            build_consistent_image_prompt(self.project, self.shot)
            if kind == "image"
            else build_h3_video_prompt(
                self.project, [self.shot], H3PromptOptions(language_gate_mode="warn"),
            )
        )
        prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type=kind,
            h3_mode="T2VA",
            prompt_text=result.text,
            total_duration_ms=result.total_duration_ms,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
            request_options={"shot_ids": [str(self.shot.id)]},
            revision=PromptArtifact.objects.filter(
                project=self.project, episode=self.episode, shot=self.shot, prompt_type=kind,
            ).count() + 1,
        )
        template = WorkflowTemplate.objects.create(
            name=f"distribution-{kind}-{path.stem}",
            slug=f"distribution-{kind}-{path.stem}",
            output_kind=kind,
            model="SDXL 1.0" if kind == "image" else "MiniMax H3",
            input_schema={},
            definition={"1": {"class_type": "Mock", "inputs": {}}},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=prompt,
            template=template,
            created_by=self.user,
            workflow_json=template.definition,
            content_hash="a" * 64,
        )
        task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=workflow,
            kind=kind,
            status=GenerationTask.Status.SUCCEEDED,
            progress=100,
            finished_at=timezone.now(),
            outputs=[{
                "kind": "images" if kind == "image" else "videos",
                "filename": path.name,
                "subfolder": path.parent.relative_to(self.output_root).as_posix()
                if path.parent != self.output_root else "",
                "type": "output",
            }],
        )
        persist_task_provenance(task, list(task.outputs), generated_at=task.finished_at)
        task.refresh_from_db()
        return task

    def _image_task(self, name: str = "master.png") -> tuple[GenerationTask, Path]:
        path = self.output_root / "episode-1" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (36, 58, 81)).save(path, "PNG")
        return self._task(path, kind="image"), path

    def test_image_distribution_is_atomic_visible_metadata_bound_and_idempotent(self):
        task, master = self._image_task()
        master_hash = sha256_file(master)

        first = prepare_task_distribution(task, 0)
        task.refresh_from_db()
        public = distribution_artifact_path(task, 0)
        sidecar = distribution_manifest_path(task, 0)

        self.assertEqual(sha256_file(master), master_hash)
        self.assertNotEqual(public.read_bytes(), master.read_bytes())
        self.assertTrue(public.is_relative_to(self.media_root / "distributions"))
        self.assertTrue(sidecar.is_file())
        self.assertEqual(first["verification"]["status"], "verified")
        self.assertEqual(first["source_artifact"]["sha256"], master_hash)
        self.assertEqual(first["artifact"]["sha256"], sha256_file(public))
        self.assertTrue(first["disclosure"]["visible_overlay"])
        self.assertTrue(first["disclosure"]["embedded_metadata"])
        self.assertEqual(first["disclosure"]["label"], "AI-generated · SDXL 1.0")
        self.assertEqual(first["release"]["model_key"], "sdxl-1.0")
        self.assertEqual(first["disclosure"]["version"], "shot-public-v1")
        self.assertTrue(first["release"]["render"]["label_bbox"])
        with Image.open(public) as opened:
            self.assertEqual(opened.info["Comment"], "AI-generated · SDXL 1.0")
            self.assertEqual(opened.info["Description"], SDXL_DISTRIBUTION_DESCRIPTION)
            self.assertEqual(opened.info["SourceArtifactSHA256"], master_hash)
            self.assertEqual(opened.info["DistributionVersion"], "shot-public-v1")

        release_id = task.outputs[0]["distribution"]["release_id"]
        with patch("studio.services.distribution._render_image") as render:
            second = prepare_task_distribution(task, 0)
        render.assert_not_called()
        self.assertEqual(second["release"]["release_id"], release_id)
        self.assertEqual(sha256_file(master), master_hash)
        self.assertFalse(any(path.name.endswith(".tmp") for path in public.parent.parent.iterdir()))

        # Recover the narrow crash window after the atomic directory rename
        # but before the task JSON pointer commit, without another render.
        outputs = list(task.outputs)
        outputs[0] = dict(outputs[0])
        outputs[0].pop("distribution")
        task.outputs = outputs
        task.save(update_fields=("outputs", "updated_at"))
        with patch("studio.services.distribution._render_image") as render:
            recovered = prepare_task_distribution(task, 0)
        render.assert_not_called()
        self.assertEqual(recovered["release"]["release_id"], release_id)

    def _clear_distribution_for_lock_test(self, task: GenerationTask) -> tuple[Path, str]:
        task.refresh_from_db()
        record = dict(task.outputs[0]["distribution"])
        public = distribution_artifact_path(task, 0)
        shutil.rmtree(public.parent)
        outputs = list(task.outputs)
        outputs[0] = dict(outputs[0])
        outputs[0].pop("distribution", None)
        task.outputs = outputs
        task.save(update_fields=("outputs", "updated_at"))
        release_id = record["release_id"]
        return public.parent.parent / f".{release_id}.lock", release_id

    @override_settings(TASK_DISTRIBUTION_LOCK_STALE_SECONDS=60)
    def test_fresh_distribution_lock_is_never_reclaimed(self):
        task, _ = self._image_task("fresh-lock.png")
        prepare_task_distribution(task, 0)
        lock, _ = self._clear_distribution_for_lock_test(task)
        lock.write_text(json.dumps({
            "owner_token": "active-publisher",
            "pid": 1234,
            "started_at_epoch": time.time(),
        }), encoding="utf-8")

        with self.assertRaisesRegex(DistributionError, "already being prepared"):
            prepare_task_distribution(task, 0)

        self.assertTrue(lock.is_file())
        self.assertEqual(
            json.loads(lock.read_text(encoding="utf-8"))["owner_token"],
            "active-publisher",
        )

    @override_settings(TASK_DISTRIBUTION_LOCK_STALE_SECONDS=0.05)
    def test_orphaned_stale_distribution_lock_is_recovered(self):
        task, master = self._image_task("stale-lock.png")
        master_hash = sha256_file(master)
        prepare_task_distribution(task, 0)
        lock, release_id = self._clear_distribution_for_lock_test(task)
        old = time.time() - 10
        lock.write_text(json.dumps({
            "owner_token": "crashed-publisher",
            "pid": 999999,
            "started_at_epoch": old,
        }), encoding="utf-8")
        os.utime(lock, (old, old))

        recovered = prepare_task_distribution(task, 0)
        task.refresh_from_db()

        self.assertEqual(recovered["release"]["release_id"], release_id)
        self.assertEqual(sha256_file(master), master_hash)
        self.assertFalse(lock.exists())
        self.assertEqual(task.outputs[0]["distribution"]["status"], "ready")

    def test_public_download_is_managed_copy_while_master_is_preview_only(self):
        task, master = self._image_task("download-master.png")
        prepare_task_distribution(task, 0)
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        media = detail.data["output_media"][0]
        self.assertTrue(media["preview_url"])
        self.assertTrue(media["download_url"])
        self.assertTrue(media["public_provenance_url"])
        anonymous = APIClient()

        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(b"".join(preview.streaming_content), master.read_bytes())
        preview.close()
        self.assertEqual(anonymous.get(f'{media["preview_url"]}?download=1').status_code, 404)

        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(
            b"".join(download.streaming_content), distribution_artifact_path(task, 0).read_bytes(),
        )
        self.assertEqual(download["X-Managed-Distribution"], "true")
        self.assertEqual(download["X-Distribution-Version"], "shot-public-v1")
        self.assertEqual(download["X-Source-Artifact-SHA256"], sha256_file(master))
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))
        download.close()

    def test_distribution_and_sidecar_tampering_fail_closed_without_breaking_master_preview(self):
        task, _ = self._image_task("tamper-master.png")
        prepare_task_distribution(task, 0)
        detail = self.client.get(f"/api/v1/tasks/{task.id}/").data
        media = detail["output_media"][0]
        anonymous = APIClient()
        task.refresh_from_db()

        public = distribution_artifact_path(task, 0)
        public.write_bytes(public.read_bytes() + b"tamper")
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["public_provenance_url"]).status_code, 404)
        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        preview.close()
        with self.assertRaisesRegex(DistributionError, "failed verification"):
            prepare_task_distribution(task, 0)

        # Restore the artifact and then prove signed-sidecar mutation is also
        # independently rejected.
        task, _ = self._image_task("sidecar-master.png")
        prepare_task_distribution(task, 0)
        detail = self.client.get(f"/api/v1/tasks/{task.id}/").data
        task.refresh_from_db()
        sidecar = distribution_manifest_path(task, 0)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        document["disclosure"]["label"] = "not the approved label"
        sidecar.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(anonymous.get(detail["output_media"][0]["download_url"]).status_code, 404)

    def test_missing_font_and_invalid_label_fail_explicitly_and_never_offer_download(self):
        task, master = self._image_task("font-master.png")
        master_hash = sha256_file(master)
        with override_settings(TASK_DISTRIBUTION_FONTFILE=str(Path(self.temp.name) / "missing.ttf")):
            with self.assertRaisesRegex(DistributionError, "font is unavailable"):
                prepare_task_distribution(task, 0)
        task.refresh_from_db()
        self.assertEqual(task.outputs[0]["distribution"]["status"], "failed")
        self.assertEqual(sha256_file(master), master_hash)
        media = self.client.get(f"/api/v1/tasks/{task.id}/").data["output_media"][0]
        self.assertFalse(media["download_url"])
        self.assertIn("font", media["distribution_error"])

        with override_settings(TASK_IMAGE_DISTRIBUTION_LABEL_TEXT="SDXL 1.0"):
            with self.assertRaisesRegex(DistributionError, "explicitly identify"):
                prepare_task_distribution(task, 0)

    def test_video_distribution_has_visible_filter_and_verified_container_metadata(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg/ffprobe are not installed")
        master = self.output_root / "video-master.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=#26415d:s=640x360:d=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", master.as_posix(),
        ], check=True, timeout=30)
        task = self._task(master, kind="video")
        master_hash = sha256_file(master)

        document = prepare_task_distribution(task, 0)
        task.refresh_from_db()
        public = distribution_artifact_path(task, 0)

        self.assertEqual(sha256_file(master), master_hash)
        self.assertNotEqual(sha256_file(public), master_hash)
        self.assertTrue(document["release"]["render"]["visible_overlay"])
        self.assertEqual(document["release"]["model_key"], "minimax-h3")
        self.assertEqual(
            document["release"]["render"]["overlay_filter"],
            "bottom-right boxed drawtext",
        )
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "format_tags=comment,description",
            "-of", "json", public.as_posix(),
        ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        tags = json.loads(probe.stdout)["format"]["tags"]
        self.assertEqual(tags["comment"], "AI-generated · MiniMax H3")
        self.assertEqual(tags["description"], DISTRIBUTION_DESCRIPTION)
        self.assertEqual(verified_distribution_manifest(task, 0)["verification"]["status"], "verified")
