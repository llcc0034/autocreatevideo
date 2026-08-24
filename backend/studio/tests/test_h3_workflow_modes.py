import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Asset, GenerationTask, PromptArtifact, Shot, WorkflowTemplate
from studio.services.comfyui import ComfyUIError
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.reference_media import ReferenceMediaNormalizationError
from studio.services.task_queue import prepare_workflow
from studio.services.workflow import WorkflowBuildError, build_workflow
from studio.tests.factories import build_story


class H3WorkflowModeContractTests(TestCase):
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
            self.user, self.project, self.character, self.scene,
            self.episode, self.shot, self.shot2,
        ) = build_story("h3-modes")
        # These tests exercise the controlled minimax_h3 compiler bridge's
        # legacy graph builder in isolation.  Fresh installs keep imported
        # legacy templates paused, so the fixture—not seed_data—must explicitly
        # opt the exact reviewed graphs into execution for this test case.
        first_last = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-first-last-video",
        )
        first_last.is_active = True
        first_last.input_schema = {
            **(first_last.input_schema or {}),
            "workflow_mode": "first_last_to_video",
            "prompt_template_keys": [
                "h3_i2v_zero_frame_v1", "h3_i2v_first_frame_v1",
                "h3_i2v_last_frame_v1", "h3_i2v_first_last_v1",
            ],
        }
        first_last.save(update_fields=("is_active", "input_schema", "updated_at"))
        reference = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-h3-reference-video",
        )
        reference.is_active = True
        reference.input_schema = {
            **(reference.input_schema or {}),
            "workflow_mode": "reference_to_video",
            "prompt_template_keys": [
                "h3_r2v_zero_reference_v1", "h3_r2v_classified_v1",
            ],
        }
        reference.save(update_fields=("is_active", "input_schema", "updated_at"))
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

    def prompt(self, mode):
        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(
                mode=mode,
                duration_overrides={str(self.shot.id): 5000},
                language_gate_mode="warn",
            ),
        )
        return result, PromptArtifact(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type="video",
            model="minimax-h3",
            h3_mode=mode,
            include_audio=True,
            total_duration_ms=result.total_duration_ms,
            prompt_text=result.text,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )

    def assert_ref_fixed_graph(self, workflow):
        """Dynamic reference loaders must never replace the reviewed base graph."""
        expected = {
            "1": "UNETLoader",
            "2": "CLIPLoader",
            "3": "VAELoader",
            "4": "VAELoader",
            "5": "MiniMaxH3ReferenceToVideo",
            "6": "RandomNoise",
            "7": "BasicGuider",
            "8": "KSamplerSelect",
            "9": "BasicScheduler",
            "10": "SamplerCustomAdvanced",
            "11": "VAEDecode",
            "12": "VAEDecodeAudio",
            "13": "CreateVideo",
            "14": "SaveVideo",
        }
        self.assertEqual(
            {node_id: workflow[node_id]["class_type"] for node_id in expected},
            expected,
        )

    def test_workflow_uses_explicit_frozen_duration_policy(self):
        _result, prompt = self.prompt(Shot.H3Mode.T2VA)
        template = WorkflowTemplate.objects.get(
            slug="minimax-h3-base-video", owner=None,
        )
        template.is_active = True

        with self.assertRaisesRegex(WorkflowBuildError, "6000–8000ms"):
            build_workflow(
                template,
                prompt,
                self.project,
                video_duration_policy={
                    "default_duration_ms": 6000,
                    "minimum_duration_ms": 6000,
                    "maximum_duration_ms": 8000,
                },
            )

    def test_first_last_workflow_maps_zero_first_last_and_both_slots_exactly(self):
        first = self.asset(Asset.Kind.REFERENCE, "first.png", "image/png")
        last = self.asset(Asset.Kind.REFERENCE, "last.png", "image/png")
        template = WorkflowTemplate.objects.get(slug="minimax-h3-first-last-video", owner=None)
        cases = [
            ({}, [], "T2VA", "h3_i2v_zero_frame_v1", False, False),
            ({"first_frame_asset_id": str(first.id)}, [first], "I2VA", "h3_i2v_first_frame_v1", True, False),
            ({"last_frame_asset_id": str(last.id)}, [last], "L2VA", "h3_i2v_last_frame_v1", False, True),
            ({
                "first_frame_asset_id": str(first.id),
                "last_frame_asset_id": str(last.id),
            }, [first, last], "FL2VA", "h3_i2v_first_last_v1", True, True),
        ]
        for bindings, assets, effective, prompt_key, has_first, has_last in cases:
            with self.subTest(effective=effective):
                self.shot.h3_mode = Shot.H3Mode.FL2VA
                self.shot.reference_bindings = bindings
                self.shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
                self.shot.reference_assets.set(assets)
                result, prompt = self.prompt(Shot.H3Mode.FL2VA)
                workflow, injected, _ = build_workflow(template, prompt, self.project)
                node = workflow["5"]["inputs"]
                self.assertEqual(result.payload["effective_conditioning_mode"], effective)
                self.assertEqual(result.payload["prompt_template_key"], prompt_key)
                self.assertEqual(injected["REQUESTED_H3_MODE"], Shot.H3Mode.FL2VA)
                self.assertEqual(injected["H3_MODE"], effective)
                self.assertEqual(injected["EFFECTIVE_CONDITIONING_MODE"], effective)
                self.assertEqual("first_frame" in node, has_first)
                self.assertEqual("last_frame" in node, has_last)
                self.assertEqual(
                    len([item for item in workflow.values() if item["class_type"] == "LoadImage"]),
                    int(has_first) + int(has_last),
                )
                self.assertNotIn("5.00-second", result.text)

    def test_shot_api_rejects_text_reference_and_duplicate_first_last(self):
        first = self.asset(Asset.Kind.REFERENCE, "first.png", "image/png")
        denied = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "T2VA",
            "reference_assets": [str(first.id)],
            "reference_bindings": {"first_frame_asset_id": str(first.id)},
        }, format="json")
        self.assertEqual(denied.status_code, 400, denied.data)
        duplicate = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "FL2VA",
            "reference_assets": [str(first.id)],
            "reference_bindings": {
                "first_frame_asset_id": str(first.id),
                "last_frame_asset_id": str(first.id),
            },
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)

    def test_ref2va_zero_reference_keeps_official_six_sections_and_no_loaders(self):
        self.shot.h3_mode = Shot.H3Mode.REF2VA
        self.shot.reference_bindings = {}
        self.shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        result, prompt = self.prompt(Shot.H3Mode.REF2VA)
        template = WorkflowTemplate.objects.get(slug="minimax-h3-reference-video", owner=None)
        workflow, _, _ = build_workflow(template, prompt, self.project)
        section_positions = [
            result.text.index(name) for name in (
                "subject_definitions:", "summary:", "retention_analysis:",
                "detailed_description:", "overall_soundscape:", "non_diegetic_music:",
            )
        ]
        self.assertEqual(section_positions, sorted(section_positions))
        self.assertEqual(result.payload["prompt_template_key"], "h3_r2v_zero_reference_v1")
        self.assertIn("text-only, no Picture binding", result.text)
        self.assertNotRegex(result.text, r"<(?:Picture|Video|Audio)\s+\d+>")
        self.assertFalse(any(
            item["class_type"] in {"LoadImage", "LoadVideo", "LoadAudio"}
            for item in workflow.values()
        ))
        self.assert_ref_fixed_graph(workflow)

    def test_ref2va_standalone_audio_only_uses_audio_one_and_keeps_fixed_graph(self):
        standalone_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO, "standalone-only.wav", "audio/wav",
            metadata={"duration_ms": 5000},
        )
        self.shot.h3_mode = Shot.H3Mode.REF2VA
        self.shot.reference_bindings = {
            "audios": [{"asset_id": str(standalone_audio.id)}],
        }
        self.shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))

        result, prompt = self.prompt(Shot.H3Mode.REF2VA)
        self.assertEqual(result.payload["prompt_template_key"], "h3_r2v_classified_v1")
        self.assertEqual(
            [(item["category"], item["input_label"]) for item in result.payload["reference_bindings"]],
            [("audio", "<Audio 1>")],
        )
        self.assertNotRegex(result.text, r"<(?:Picture|Video)\s+\d+>")
        self.assertEqual(set(__import__("re").findall(r"<Audio\s+\d+>", result.text)), {"<Audio 1>"})

        template = WorkflowTemplate.objects.get(slug="minimax-h3-reference-video", owner=None)
        workflow, _, _ = build_workflow(template, prompt, self.project)
        self.assertEqual(sum(item["class_type"] == "LoadAudio" for item in workflow.values()), 1)
        self.assertFalse(any(item["class_type"] == "LoadVideo" for item in workflow.values()))
        self.assertFalse(any(item["class_type"] == "LoadImage" for item in workflow.values()))
        self.assertIn("ref_audios.ref_audio_0", workflow["5"]["inputs"])
        self.assert_ref_fixed_graph(workflow)

    def test_ref2va_soft_first_frame_reference_reserves_picture_one(self):
        previous_tail = self.asset(
            Asset.Kind.REFERENCE,
            "previous-tail.png",
            "image/png",
            metadata={"generation_purpose": "deferred_generation"},
        )
        portrait = self.asset(Asset.Kind.CHARACTER, "portrait.png", "image/png")
        self.character.anchor_asset = portrait
        self.character.save(update_fields=("anchor_asset", "updated_at"))
        self.shot.h3_mode = Shot.H3Mode.REF2VA
        self.shot.requires_first_frame_continuity = True
        self.shot.reference_bindings = {
            "first_frame_reference_asset_id": str(previous_tail.id),
            "characters": [{
                "entity_id": str(self.character.id),
                "asset_id": str(portrait.id),
            }],
        }
        self.shot.save(update_fields=(
            "h3_mode", "requires_first_frame_continuity",
            "reference_bindings", "updated_at",
        ))
        self.shot.reference_assets.set([previous_tail])

        result, prompt = self.prompt(Shot.H3Mode.REF2VA)

        records = result.payload["reference_bindings"]
        self.assertEqual(
            [(item["category"], item["input_label"]) for item in records],
            [("continuity_frame", "<Picture 1>"), ("character", "<Picture 2>")],
        )
        self.assertIn("previous shot's tail frame", result.text)
        self.assertIn("target video's opening state", result.text)
        template = WorkflowTemplate.objects.get(slug="minimax-h3-reference-video", owner=None)
        workflow, _, _ = build_workflow(template, prompt, self.project)
        self.assertEqual(sum(item["class_type"] == "LoadImage" for item in workflow.values()), 2)
        ref_inputs = workflow["5"]["inputs"]
        self.assertIn("ref_images.ref_image_0", ref_inputs)
        self.assertIn("ref_images.ref_image_1", ref_inputs)
        self.assert_ref_fixed_graph(workflow)

    def test_ref2va_rejects_mixed_embedded_external_audio_and_media_count_overflow(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "mutual.mp4", "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": True},
        )
        external_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO, "mutual.wav", "audio/wav",
            metadata={"duration_ms": 5000},
        )
        mixed = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "Ref2VA",
            "reference_bindings": {
                "videos": [{
                    "asset_id": str(video.id),
                    "use_embedded_audio": True,
                    "audio_asset_id": str(external_audio.id),
                }],
            },
        }, format="json")
        self.assertEqual(mixed.status_code, 400, mixed.data)

        silent_video = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "silent.mp4", "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": False},
        )
        unverified_embedded = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "Ref2VA",
            "reference_bindings": {
                "videos": [{
                    "asset_id": str(silent_video.id),
                    "use_embedded_audio": True,
                }],
            },
        }, format="json")
        self.assertEqual(unverified_embedded.status_code, 400, unverified_embedded.data)

        too_many_videos = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "Ref2VA",
            "reference_bindings": {
                "videos": [
                    {"asset_id": str(uuid4()), "use_embedded_audio": False}
                    for _ in range(4)
                ],
            },
        }, format="json")
        self.assertEqual(too_many_videos.status_code, 400, too_many_videos.data)

        too_many_audios = self.client.patch(f"/api/v1/shots/{self.shot.id}/", {
            "h3_mode": "Ref2VA",
            "reference_bindings": {
                "audios": [{"asset_id": str(uuid4())} for _ in range(4)],
            },
        }, format="json")
        self.assertEqual(too_many_audios.status_code, 400, too_many_audios.data)

    def test_ref2va_images_videos_paired_audio_and_standalone_audio_share_labels_and_graph(self):
        portrait = self.asset(Asset.Kind.CHARACTER, "portrait.png", "image/png")
        self.character.anchor_asset = portrait
        self.character.save(update_fields=("anchor_asset", "updated_at"))
        video_plain = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "plain.mp4", "video/mp4",
            metadata={"duration_ms": 4000, "has_audio": False},
        )
        video_embedded = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "embedded.mp4", "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": True},
        )
        video_external = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "external.mp4", "video/mp4",
            metadata={"duration_ms": 6000, "has_audio": False},
        )
        paired_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO, "paired.wav", "audio/wav",
            metadata={"duration_ms": 5000},
        )
        standalone_audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO, "standalone.wav", "audio/wav",
            metadata={"duration_ms": 5000},
        )
        self.shot.h3_mode = Shot.H3Mode.REF2VA
        self.shot.reference_bindings = {
            "characters": [{"entity_id": str(self.character.id), "asset_id": str(portrait.id)}],
            "videos": [
                {"asset_id": str(video_plain.id), "use_embedded_audio": False},
                {"asset_id": str(video_embedded.id), "use_embedded_audio": True},
                {
                    "asset_id": str(video_external.id), "use_embedded_audio": False,
                    "audio_asset_id": str(paired_audio.id),
                },
            ],
            "audios": [{"asset_id": str(standalone_audio.id)}],
        }
        self.shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        result, prompt = self.prompt(Shot.H3Mode.REF2VA)
        records = result.payload["reference_bindings"]
        self.assertEqual(
            [item["input_label"] for item in records],
            ["<Picture 1>", "<Video 1>", "<Video 2>", "<Video 3>", "<Audio 3>"],
        )
        self.assertEqual(
            [item.get("audio_input_label") for item in records if item["category"] == "video"],
            [None, "<Audio 1>", "<Audio 2>"],
        )
        expected_labels = {
            "<Picture 1>", "<Video 1>", "<Video 2>", "<Video 3>",
            "<Audio 1>", "<Audio 2>", "<Audio 3>",
        }
        self.assertEqual(
            set(__import__("re").findall(r"<(?:Picture|Video|Audio)\s+\d+>", result.text)),
            expected_labels,
        )
        template = WorkflowTemplate.objects.get(slug="minimax-h3-reference-video", owner=None)
        workflow, _, _ = build_workflow(template, prompt, self.project)
        ref_inputs = workflow["5"]["inputs"]
        self.assertIn("ref_videos.ref_video_0", ref_inputs)
        self.assertIn("ref_videos.ref_video_1", ref_inputs)
        self.assertIn("ref_videos.ref_video_2", ref_inputs)
        self.assertNotIn("ref_video_audios.ref_video_audio_0", ref_inputs)
        self.assertIn("ref_video_audios.ref_video_audio_1", ref_inputs)
        self.assertIn("ref_video_audios.ref_video_audio_2", ref_inputs)
        self.assertIn("ref_audios.ref_audio_0", ref_inputs)
        self.assertEqual(sum(item["class_type"] == "LoadVideo" for item in workflow.values()), 3)
        self.assertEqual(sum(item["class_type"] == "GetVideoComponents" for item in workflow.values()), 3)
        self.assertEqual(sum(item["class_type"] == "LoadAudio" for item in workflow.values()), 2)

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
        self.assertEqual(response.data["metadata"]["ref2va_effective_fps"], 24)
        self.assertTrue(response.data["metadata"]["requires_cfr24_staging"])

    def test_reference_video_upload_without_reliable_fps_is_rejected(self):
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
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("FPS", str(response.data))

    def test_prepare_workflow_stages_all_image_video_and_audio_asset_tokens(self):
        video = self.asset(
            Asset.Kind.REFERENCE_VIDEO, "clip.mp4", "video/mp4",
            metadata={"duration_ms": 5000, "has_audio": False},
        )
        audio = self.asset(
            Asset.Kind.REFERENCE_AUDIO, "voice.wav", "audio/wav",
            metadata={"duration_ms": 5000},
        )
        self.assertNotEqual(video.checksum, audio.checksum)
        self.shot.h3_mode = Shot.H3Mode.REF2VA
        self.shot.reference_bindings = {
            "videos": [{
                "asset_id": str(video.id), "use_embedded_audio": False,
                "audio_asset_id": str(audio.id),
            }],
        }
        self.shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        result = build_h3_video_prompt(
            self.project, [self.shot], H3PromptOptions(
                mode=Shot.H3Mode.REF2VA,
                duration_overrides={str(self.shot.id): 5000},
                language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=self.shot, created_by=self.user,
            prompt_type="video", model="minimax-h3", h3_mode=Shot.H3Mode.REF2VA,
            total_duration_ms=result.total_duration_ms, prompt_text=result.text,
            structured_payload=result.payload, consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.get(slug="minimax-h3-reference-video", owner=None)
        graph, injected, digest = build_workflow(template, prompt, self.project)
        from studio.models import GeneratedWorkflow
        generated = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=template, created_by=self.user, workflow_json=graph,
            injected_values=injected, content_hash=digest,
        )
        task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=generated, kind="video",
        )

        class UploadClient:
            def __init__(self):
                self.paths = []

            def upload_input_file(self, path, *, subfolder, filename=None):
                self.paths.append(Path(path).name)
                return {"name": filename, "subfolder": subfolder, "type": "input"}

        client = UploadClient()
        plan = SimpleNamespace(effective_duration_seconds=5.0)
        video_staging = {
            "staging_version": 2, "media_role": "reference_video",
            "source_fps": 30.0, "effective_fps": 24, "effective_frames": 120,
            "effective_duration_ms": 5000, "cfr_guaranteed": True,
            "original_asset_preserved": True,
        }
        audio_staging = {
            "staging_version": 2, "media_role": "paired_reference_audio",
            "paired_video_asset_id": str(video.id), "effective_duration_ms": 5000,
            "duration_aligned": True, "original_asset_preserved": True,
        }

        def stage_video(_source, output, **_kwargs):
            Path(output).write_bytes(b"normalized-video")
            return video_staging

        def stage_audio(_source, output, **_kwargs):
            Path(output).write_bytes(b"normalized-audio")
            return audio_staging

        with (
            patch("studio.services.task_queue.inspect_reference_video", return_value=plan),
            patch(
                "studio.services.task_queue.normalize_reference_video",
                side_effect=stage_video,
            ),
            patch(
                "studio.services.task_queue.normalize_paired_audio",
                side_effect=stage_audio,
            ),
        ):
            prepared = prepare_workflow(task, client)
        # Tenant uploads use randomized storage names; modality and cardinality
        # are the stable worker contract, not the original browser filename.
        self.assertEqual({Path(value).suffix for value in client.paths}, {".mp4", ".wav"})
        self.assertEqual(len(client.paths), 2)
        self.assertNotIn("__AUTOPROMPT_ASSET__", json.dumps(prepared))
        task.refresh_from_db()
        self.assertEqual(len(task.staged_inputs), 2)
        records = {item["asset_id"]: item for item in task.staged_inputs}
        self.assertEqual(records[str(video.id)]["source_fps"], 30.0)
        self.assertEqual(records[str(video.id)]["effective_fps"], 24)
        self.assertTrue(records[str(video.id)]["cfr_guaranteed"])
        self.assertTrue(records[str(audio.id)]["duration_aligned"])
        self.assertEqual(records[str(audio.id)]["paired_video_asset_id"], str(video.id))

        # A valid retry may reuse the remote input path, but it must first
        # re-authorize the current source asset and exact source hash.
        reuse_client = UploadClient()
        prepare_workflow(task, reuse_client)
        self.assertEqual(reuse_client.paths, [])

        video.is_active = False
        video.save(update_fields=("is_active", "updated_at"))
        inactive_client = UploadClient()
        with self.assertRaisesRegex(ComfyUIError, "不存在或已停用"):
            prepare_workflow(task, inactive_client)
        self.assertEqual(inactive_client.paths, [])
        video.is_active = True
        video.save(update_fields=("is_active", "updated_at"))

        original_video_fixture = b"verified-local-fixture:clip.mp4"
        original_audio_fixture = b"verified-local-fixture:voice.wav"
        video_path = Path(video.file.path)
        video_path.write_bytes(b"tampered-retry-source")
        tampered_retry_client = UploadClient()
        with self.assertRaisesRegex(ComfyUIError, "内容哈希.*不一致"):
            prepare_workflow(task, tampered_retry_client)
        self.assertEqual(tampered_retry_client.paths, [])
        video_path.write_bytes(original_video_fixture)

        # Even a replacement whose database checksum was updated is not the
        # byte snapshot authorized by this task's existing staged record.
        audio_path = Path(audio.file.path)
        replacement = b"new-authorized-audio-bytes"
        audio_path.write_bytes(replacement)
        audio.checksum = hashlib.sha256(replacement).hexdigest()
        audio.save(update_fields=("checksum", "updated_at"))
        replaced_client = UploadClient()
        with self.assertRaisesRegex(ComfyUIError, "任务冻结 checksum"):
            prepare_workflow(task, replaced_client)
        self.assertEqual(replaced_client.paths, [])
        audio_path.write_bytes(original_audio_fixture)
        audio.checksum = hashlib.sha256(original_audio_fixture).hexdigest()
        audio.save(update_fields=("checksum", "updated_at"))

        class DigestClient(UploadClient):
            def staged_input_sha256(self, _comfy_path):
                return "0" * 64

        staged_copy_client = DigestClient()
        with self.assertRaisesRegex(ComfyUIError, "已暂存.*哈希不一致"):
            prepare_workflow(task, staged_copy_client)
        self.assertEqual(staged_copy_client.paths, [])

        # A pre-upgrade mapping has no CFR proof and must be replaced rather
        # than reused on a retry.  Its paired audio is also rebuilt because its
        # previous target duration no longer has a trustworthy video proof.
        for row in task.staged_inputs:
            if row["asset_id"] == str(video.id):
                row.pop("cfr_guaranteed", None)
        task.save(update_fields=("staged_inputs", "updated_at"))
        retry_client = UploadClient()
        with (
            patch("studio.services.task_queue.inspect_reference_video", return_value=plan),
            patch(
                "studio.services.task_queue.normalize_reference_video",
                side_effect=stage_video,
            ),
            patch(
                "studio.services.task_queue.normalize_paired_audio",
                side_effect=stage_audio,
            ),
        ):
            prepare_workflow(task, retry_client)
        self.assertEqual(len(retry_client.paths), 2)
        self.assertEqual(
            {Path(value).suffix for value in retry_client.paths}, {".mp4", ".wav"},
        )

        blocked_task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=generated, kind="video",
        )
        blocked_client = UploadClient()
        with patch(
            "studio.services.task_queue.inspect_reference_video",
            side_effect=ReferenceMediaNormalizationError("source FPS unavailable"),
        ):
            with self.assertRaisesRegex(ComfyUIError, "blocked before ComfyUI submission"):
                prepare_workflow(blocked_task, blocked_client)
        self.assertEqual(blocked_client.paths, [])

        Path(video.file.path).write_bytes(b"tampered-after-upload")
        tampered_task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=generated, kind="video",
        )
        tampered_client = UploadClient()
        with self.assertRaisesRegex(ComfyUIError, "内容哈希.*不一致"):
            prepare_workflow(tampered_task, tampered_client)
        self.assertEqual(tampered_client.paths, [])

        # Deleting a source after the first upload must also invalidate reuse.
        video_path.write_bytes(original_video_fixture)
        audio.delete()
        deleted_client = UploadClient()
        with self.assertRaisesRegex(ComfyUIError, "不存在或已停用"):
            prepare_workflow(task, deleted_client)
        self.assertEqual(deleted_client.paths, [])
