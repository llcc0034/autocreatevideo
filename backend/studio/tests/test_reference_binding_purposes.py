from pathlib import Path
import hashlib
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Asset, Shot
from studio.services.media_capabilities import normalize_conditioning_contract
from studio.tests.media_prompt_fixtures import fixture_mode_contract
from studio.services.reference_bindings import (
    ReferenceBindingError,
    resolve_group_reference_bindings,
    validate_reference_bindings_against_mode_contract,
)
from studio.tests.factories import build_story, conditioning_contract


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
            checksum=hashlib.sha256(b"verified-reference-purpose-fixture").hexdigest(),
            metadata=metadata,
        )

    def patch_bindings(self, bindings, *, mode="Ref2VA"):
        return self.client.patch(
            f"/api/v1/shots/{self.shot.id}/",
            {"conditioning_mode": mode, "reference_bindings": bindings},
            format="json",
        )

    def test_missing_or_unsafe_purposes_are_rejected_without_defaults(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "no-default.mp4",
            "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": True},
        )
        audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO,
            "no-default.wav",
            "audio/wav",
            metadata={"duration_ms": 5000},
        )
        for bindings in (
            {"videos": [{
                "asset_id": str(video.id),
                "use_embedded_audio": False,
            }]},
            {"videos": [{
                "asset_id": str(video.id),
                "purpose": "../unsafe",
                "use_embedded_audio": False,
            }]},
            {"audios": [{"asset_id": str(audio.id)}]},
            {"audios": [{
                "asset_id": str(audio.id),
                "purpose": "bad purpose",
            }]},
        ):
            with self.subTest(bindings=bindings):
                response = self.patch_bindings(bindings)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("reference_bindings", response.data["error"]["details"])

    def test_shot_accepts_fictional_safe_purpose_without_guessing_workflow(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "fictional.mp4",
            "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": False},
        )
        bindings = {"videos": [{
            "asset_id": str(video.id),
            "purpose": "orbit_rhythm_v7",
            "use_embedded_audio": False,
        }]}

        response = self.patch_bindings(bindings)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["reference_bindings"], bindings)

    def test_fictional_mode_contract_drives_resolver_without_h3_names(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO,
            "nebula-orbit.mp4",
            "video/mp4",
            metadata={"duration_ms": 900, "has_audio": False},
        )
        bindings = {"videos": [{
            "asset_id": str(video.id),
            "purpose": "orbit_rhythm_v7",
            "use_embedded_audio": False,
        }]}
        response = self.patch_bindings(bindings, mode="NebulaTransfer")
        self.assertEqual(response.status_code, 200, response.data)
        self.shot.refresh_from_db()
        mode_contract = normalize_conditioning_contract(conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 2),
            video_purposes=("orbit_rhythm_v7", "scene_retarget_x"),
            video_preprocessing_adapter="fictional_video_adapter_v9",
            video_target_fps=17,
            video_requires_source_fps=False,
        ))["modes"][0]

        records = resolve_group_reference_bindings(
            project=self.project,
            shots=[self.shot],
            mode_contract=mode_contract,
            snapshot={},
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["category"], "video")
        self.assertEqual(records[0]["purpose"], "orbit_rhythm_v7")

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
            mode_contract=fixture_mode_contract("Ref2VA"),
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


class GenericReferenceTopologyContractTests(TestCase):
    def mode_contract(self):
        contract = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 2),
            paired_video_audios=(0, 2),
            reference_audios=(0, 1),
            video_purposes=("orbit_rhythm_v7", "scene_retarget_x"),
            audio_purposes=("pulse_track_q",),
            video_preprocessing_adapter="fictional_video_adapter_v9",
            paired_audio_preprocessing_adapter="fictional_pair_adapter_v3",
            reference_audio_preprocessing_adapter="fictional_audio_adapter_v2",
        )
        contract["modes"][0]["dynamic_reference_topology"]["mutual_exclusion_rules"] = [{
            "key": "fictional_mutex",
            "label": "Fictional mutex",
            "description": "Configuration-only fictional rule",
            "media_kind": "video",
            "purpose_keys": ["orbit_rhythm_v7", "scene_retarget_x"],
            "maximum_distinct_purposes": 1,
        }]
        return normalize_conditioning_contract(contract)["modes"][0]

    def test_fictional_model_mode_and_purposes_validate_without_h3_names(self):
        validate_reference_bindings_against_mode_contract({
            "videos": [{
                "asset_id": "11111111-1111-4111-8111-111111111111",
                "purpose": "orbit_rhythm_v7",
                "use_embedded_audio": False,
            }],
            "audios": [{
                "asset_id": "22222222-2222-4222-8222-222222222222",
                "purpose": "pulse_track_q",
            }],
        }, self.mode_contract())

    def test_route_contract_rejects_unknown_safe_purpose_and_configured_mutex(self):
        mode = self.mode_contract()
        with self.assertRaises(ReferenceBindingError):
            validate_reference_bindings_against_mode_contract({
                "videos": [{
                    "asset_id": "11111111-1111-4111-8111-111111111111",
                    "purpose": "safe_but_undeclared",
                    "use_embedded_audio": False,
                }],
            }, mode)
        with self.assertRaises(ReferenceBindingError):
            validate_reference_bindings_against_mode_contract({
                "videos": [{
                    "asset_id": "11111111-1111-4111-8111-111111111111",
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }, {
                    "asset_id": "22222222-2222-4222-8222-222222222222",
                    "purpose": "scene_retarget_x",
                    "use_embedded_audio": False,
                }],
            }, mode)
