from pathlib import Path
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Asset, Shot
from studio.services.reference_bindings import resolve_group_reference_bindings
from studio.tests.factories import build_story


class ReferenceBindingPurposeContractTests(TestCase):
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
        (
            self.user, self.project, _character, _scene,
            _episode, self.shot, _shot2,
        ) = build_story("reference-purpose")
        self.shot.duration_ms = 5000
        self.shot.save(update_fields=("duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def asset(self, kind, filename, mime_type, *, metadata):
        return Asset.objects.create(
            owner=self.user,
            style=self.project.style,
            kind=kind,
            name=filename,
            file=SimpleUploadedFile(filename, b"verified-reference-purpose-fixture"),
            mime_type=mime_type,
            checksum=f"checksum-{filename}",
            metadata=metadata,
        )

    def patch_bindings(self, bindings):
        return self.client.patch(
            f"/api/v1/shots/{self.shot.id}/",
            {"h3_mode": Shot.H3Mode.REF2VA, "reference_bindings": bindings},
            format="json",
        )

    def test_legacy_media_bindings_receive_canonical_default_purposes(self):
        embedded_video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "embedded.mp4",
            "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": True},
        )
        standalone_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO,
            "standalone.wav",
            "audio/wav",
            metadata={"duration_ms": 5000},
        )

        response = self.patch_bindings({
            "videos": [{
                "asset_id": str(embedded_video.id),
                "use_embedded_audio": True,
            }],
            "audios": [{"asset_id": str(standalone_audio.id)}],
        })

        self.assertEqual(response.status_code, 200, response.data)
        expected = {
            "videos": [{
                "asset_id": str(embedded_video.id),
                "purpose": "motion_camera_reference",
                "use_embedded_audio": True,
                "audio_purpose": "audio_reference",
            }],
            "audios": [{
                "asset_id": str(standalone_audio.id),
                "purpose": "audio_reference",
            }],
        }
        self.assertEqual(response.data["reference_bindings"], expected)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.reference_bindings, expected)

    def test_explicit_purposes_round_trip_and_reach_resolved_input_records(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "edit-source.mp4",
            "video/mp4",
            metadata={"duration_ms": 6000, "has_audio": False},
        )
        paired_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO,
            "paired.wav",
            "audio/wav",
            metadata={"duration_ms": 6000},
        )
        standalone_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO,
            "music.wav",
            "audio/wav",
            metadata={"duration_ms": 5000},
        )
        bindings = {
            "videos": [{
                "asset_id": str(video.id),
                "purpose": "video_editing",
                "use_embedded_audio": False,
                "audio_asset_id": str(paired_audio.id),
                "audio_purpose": "audio_reuse",
            }],
            "audios": [{
                "asset_id": str(standalone_audio.id),
                "purpose": "audio_reuse",
            }],
        }

        response = self.patch_bindings(bindings)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["reference_bindings"], bindings)
        self.shot.refresh_from_db()
        records = resolve_group_reference_bindings(
            project=self.project,
            shots=[self.shot],
            mode=Shot.H3Mode.REF2VA,
            snapshot={},
        )
        video_record = next(item for item in records if item["category"] == "video")
        audio_record = next(item for item in records if item["category"] == "audio")
        self.assertEqual(video_record["purpose"], "video_editing")
        self.assertEqual(video_record["audio_purpose"], "audio_reuse")
        self.assertEqual(audio_record["purpose"], "audio_reuse")

    def test_video_continuation_is_a_distinct_supported_role(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "continuation.mp4",
            "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": False},
        )
        response = self.patch_bindings({
            "videos": [{
                "asset_id": str(video.id),
                "purpose": "video_continuation",
                "use_embedded_audio": False,
            }],
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["reference_bindings"]["videos"][0]["purpose"],
            "video_continuation",
        )

    def test_invalid_or_inapplicable_purposes_are_rejected(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "invalid.mp4",
            "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": False},
        )
        audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO,
            "invalid.wav",
            "audio/wav",
            metadata={"duration_ms": 5000},
        )
        cases = [
            {"videos": [{
                "asset_id": str(video.id),
                "purpose": "copy_everything",
                "use_embedded_audio": False,
            }]},
            {"videos": [{
                "asset_id": str(video.id),
                "use_embedded_audio": False,
                "audio_purpose": "audio_reference",
            }]},
            {"audios": [{
                "asset_id": str(audio.id),
                "purpose": "lip_sync_only",
            }]},
        ]
        for bindings in cases:
            with self.subTest(bindings=bindings):
                response = self.patch_bindings(bindings)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("reference_bindings", response.data["error"]["details"])
