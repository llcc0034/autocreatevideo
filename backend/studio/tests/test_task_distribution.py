from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from studio.models import (
    ConfigRevision,
    GenerationTask,
)
from studio.services.distribution import (
    DistributionError,
    distribution_artifact_path,
    distribution_configuration,
    distribution_manifest_path,
    prepare_task_distribution,
    verified_distribution_manifest,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.provenance import sha256_file
from studio.tests.prepared_production_media_fixtures import PreparedProductionMediaFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManagedTaskDistributionTests(PreparedProductionMediaFixture, TestCase):
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
            TASK_DISTRIBUTION_FONTFILE=str(self.font),
            FFMPEG_BINARY=shutil.which("ffmpeg") or "ffmpeg",
            FFPROBE_BINARY=shutil.which("ffprobe") or "ffprobe",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_user(username="distribution-author", is_staff=True)
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "delivery.ai_label_enabled": True,
                "delivery.ai_label_text": "AI-generated",
            },
            created_by=self.user,
            change_summary="generic distribution disclosure test",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _set_distribution_lock_stale_seconds(self, value: int) -> None:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"runtime.distribution_lock_stale_seconds": value},
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

    def test_public_distribution_gate_follows_superadmin_security_boundary(self):
        admin = get_user_model().objects.create_superuser(
            username="distribution-security-admin",
            email="distribution@example.test",
            password="StrongPass123!",
        )
        disabled = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"security.delivery.public_distribution_enabled": False},
            created_by=admin,
        )
        publish_config_revision(disabled, published_by=admin)
        with self.assertRaisesRegex(DistributionError, "disabled"):
            distribution_configuration()

    def _image_task(self) -> tuple[GenerationTask, Path]:
        task = self.archive_isolated_media("image")
        self.media_root = Path(settings.MEDIA_ROOT)
        self.client.force_authenticate(self.user)
        return task, self.output

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
        self.assertEqual(
            first["disclosure"]["label"],
            f"AI-generated · {self.profile.model_identifier}",
        )
        self.assertEqual(first["release"]["model_key"], str(self.profile.pk))
        self.assertEqual(first["disclosure"]["version"], "shot-public-v1")
        self.assertTrue(first["release"]["render"]["label_bbox"])
        with Image.open(public) as opened:
            self.assertEqual(
                opened.info["Comment"],
                f"AI-generated · {self.profile.model_identifier}",
            )
            self.assertEqual(
                opened.info["Description"],
                f"AI-generated; media=image; model={self.profile.model_identifier}",
            )
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
        self.assertTrue(public.parent.resolve().is_relative_to(self.media_root.resolve() / "distributions"))
        shutil.rmtree(public.parent)
        outputs = list(task.outputs)
        outputs[0] = dict(outputs[0])
        outputs[0].pop("distribution", None)
        task.outputs = outputs
        task.save(update_fields=("outputs", "updated_at"))
        release_id = record["release_id"]
        return public.parent.parent / f".{release_id}.lock", release_id

    def test_fresh_distribution_lock_is_never_reclaimed(self):
        self._set_distribution_lock_stale_seconds(60)
        task, _ = self._image_task()
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

    def test_orphaned_stale_distribution_lock_is_recovered(self):
        self._set_distribution_lock_stale_seconds(60)
        task, master = self._image_task()
        master_hash = sha256_file(master)
        prepare_task_distribution(task, 0)
        lock, release_id = self._clear_distribution_for_lock_test(task)
        old = time.time() - 120
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
        task, master = self._image_task()
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
        task, _ = self._image_task()
        prepare_task_distribution(task, 0)
        detail = self.client.get(f"/api/v1/tasks/{task.id}/").data
        media = detail["output_media"][0]
        anonymous = APIClient()
        task.refresh_from_db()

        public = distribution_artifact_path(task, 0)
        original_public = public.read_bytes()
        public.write_bytes(original_public + b"tamper")
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["public_provenance_url"]).status_code, 404)
        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        preview.close()
        with self.assertRaisesRegex(DistributionError, "failed verification"):
            prepare_task_distribution(task, 0)

        # Restore the artifact and then prove signed-sidecar mutation is also
        # independently rejected.
        public.write_bytes(original_public)
        prepare_task_distribution(task, 0)
        detail = self.client.get(f"/api/v1/tasks/{task.id}/").data
        task.refresh_from_db()
        sidecar = distribution_manifest_path(task, 0)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        document["disclosure"]["label"] = "not the approved label"
        sidecar.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(anonymous.get(detail["output_media"][0]["download_url"]).status_code, 404)

    def test_missing_font_and_invalid_label_fail_explicitly_and_never_offer_download(self):
        task, master = self._image_task()
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

        invalid = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"delivery.ai_label_text": "model-only label"},
            created_by=self.user,
            change_summary="invalid disclosure test",
            clone_published=True,
        )
        publish_config_revision(invalid, published_by=self.user)
        with self.assertRaisesRegex(DistributionError, "AI 生成内容披露"):
            prepare_task_distribution(task, 0)

    def test_video_distribution_has_visible_filter_and_verified_container_metadata(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg/ffprobe are not installed")
        task = self.archive_isolated_media("video")
        master = self.output
        master_hash = sha256_file(master)

        document = prepare_task_distribution(task, 0)
        task.refresh_from_db()
        public = distribution_artifact_path(task, 0)

        self.assertEqual(sha256_file(master), master_hash)
        self.assertNotEqual(sha256_file(public), master_hash)
        self.assertTrue(document["release"]["render"]["visible_overlay"])
        self.assertEqual(document["release"]["model_key"], str(self.profile.pk))
        self.assertEqual(
            document["release"]["render"]["overlay_filter"],
            "configured bottom-right boxed drawtext",
        )
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "format_tags=comment,description",
            "-of", "json", public.as_posix(),
        ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        tags = json.loads(probe.stdout)["format"]["tags"]
        self.assertEqual(tags["comment"], f"AI-generated · {self.profile.model_identifier}")
        self.assertEqual(
            tags["description"],
            f"AI-generated; media=video; model={self.profile.model_identifier}",
        )
        self.assertEqual(verified_distribution_manifest(task, 0)["verification"]["status"], "verified")
