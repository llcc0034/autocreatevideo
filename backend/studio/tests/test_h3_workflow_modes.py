import json
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Asset, GenerationTask
from studio.services.comfyui import ComfyUIError
from studio.services.task_queue import prepare_workflow
from studio.tests.factories import build_story


class H3WorkflowModeContractTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = override_settings(
            MEDIA_ROOT=root / "media",
            ASSET_LIBRARY_ROOT=root / "library",
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        (root / "media").mkdir()
        (root / "library").mkdir()
        call_command("seed_data", verbosity=0)
        (
            self.user, self.project, self.character, self.scene,
            self.episode, self.shot, self.shot2,
        ) = build_story("h3-modes")
        self.shot.duration_ms = 5000
        self.shot.save(update_fields=("duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def asset(self, kind, filename, mime_type, *, metadata=None):
        # Keep every fixture's source identity distinct so paired-video/audio
        # checksum handling cannot accidentally pass by comparing equal bytes.
        content = f"verified-local-fixture:{filename}".encode("utf-8")
        return Asset.objects.create(
            owner=self.user,
            style=self.project.style,
            kind=kind,
            name=filename,
            file=SimpleUploadedFile(filename, content),
            mime_type=mime_type,
            checksum=hashlib.sha256(content).hexdigest(),
            metadata=metadata or {},
        )

    def test_shot_api_defers_mode_rules_but_rejects_duplicate_first_last(self):
        first = self.asset(Asset.Kind.REFERENCE, "first.png", "image/png")
        denied = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "conditioning_mode": "T2VA",
            "reference_assets": [str(first.id)],
            "reference_bindings": {"first_frame_asset_id": str(first.id)},
        }, format="json")
        self.assertEqual(denied.status_code, 200, denied.data)
        duplicate = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "conditioning_mode": "FL2VA",
            "reference_assets": [str(first.id)],
            "reference_bindings": {
                "first_frame_asset_id": str(first.id),
                "last_frame_asset_id": str(first.id),
            },
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)

    def test_shot_api_rejects_workflow_smoke_fixture_as_production_reference(self):
        fixture = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE_VIDEO,
            name="Workflow smoke only",
            mime_type="video/mp4",
            checksum="a" * 64,
            metadata={"fixture_scope": "workflow_smoke"},
        )

        response = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "conditioning_mode": "Ref2VA",
            "reference_assets": [str(fixture.id)],
            "reference_bindings": {
                "videos": [{
                    "asset_id": str(fixture.id),
                    "purpose": "motion_camera_reference",
                    "use_embedded_audio": False,
                }],
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Smoke 专用素材不能用于生产分镜", str(response.data))


    def test_reference_media_upload_is_ffprobe_verified(self):
        probe = Mock(
            returncode=0,
            stdout=json.dumps({
                "streams": [
                    {
                        "codec_type": "video", "duration": "5.0",
                        "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001",
                    },
                    {"codec_type": "audio", "duration": "5.0"},
                ],
                "format": {"duration": "5.0"},
            }),
        )
        with patch("studio.serializers.subprocess.run", return_value=probe):
            response = self.client.post("/api/v1/assets/", {
                "style": str(self.project.style_id),
                "kind": Asset.Kind.REFERENCE_VIDEO,
                "name": "upload-video",
                "file": SimpleUploadedFile("clip.mp4", b"fake-container", content_type="video/mp4"),
                    }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["mime_type"], "video/mp4")
        self.assertEqual(response.data["metadata"]["duration_ms"], 5000)
        self.assertTrue(response.data["metadata"]["has_audio"])
        self.assertAlmostEqual(response.data["metadata"]["source_fps"], 29.97003, places=5)
        self.assertEqual(response.data["metadata"]["source_fps_fraction"], "30000/1001")
        self.assertNotIn("ref2va_effective_fps", response.data["metadata"])
        self.assertNotIn("requires_cfr24_staging", response.data["metadata"])

    def test_reference_video_upload_preserves_unknown_fps_for_route_validation(self):
        probe = Mock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "codec_type": "video", "duration": "5.0",
                    "avg_frame_rate": "0/0", "r_frame_rate": "0/0",
                }],
                "format": {"duration": "5.0"},
            }),
        )
        with patch("studio.serializers.subprocess.run", return_value=probe):
            response = self.client.post("/api/v1/assets/", {
                "style": str(self.project.style_id),
                "kind": Asset.Kind.REFERENCE_VIDEO,
                "name": "unknown-fps-video",
                "file": SimpleUploadedFile(
                    "clip.mp4", b"fake-container", content_type="video/mp4",
                ),
                    }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("source_fps", response.data["metadata"])
        self.assertEqual(response.data["metadata"]["source_avg_frame_rate"], "0/0")
        self.assertEqual(response.data["metadata"]["source_r_frame_rate"], "0/0")

    def test_task_without_primary_preparation_cannot_stage_any_file(self):
        task = GenerationTask.objects.create(owner=self.user, project=self.project, episode=self.episode, kind="video")
        client = Mock()
        with self.assertRaisesRegex(ComfyUIError, "唯一的实际主调用与执行准备"):
            prepare_workflow(task, client)
        client.upload_input_file.assert_not_called()
        client.invoke.assert_not_called()
