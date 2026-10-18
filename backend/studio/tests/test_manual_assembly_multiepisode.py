"""Actual isolated two-episode existing-video author/encode/seal/download chain.

Only source clips are synthetic. The Run, adoption, review, assembly worker,
media probe and signed delivery use the original services and public API.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from studio.models import (
    ApprovalDecision, ArtifactSelectionDecision, Asset, AssemblyJob,
    BusinessOutputVersion, Episode, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, GenerationTask, ProductionManualCommand, ReleaseBundle, Shot,
)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.release_bundles import live_verified_release_bundle
from studio.tests import test_production_adopted_media as adopted
from studio.tests import test_production_manual_delivery as delivery_fixtures
from studio.tests import test_recipe_assembly_output as output
from studio.tests.test_structure_mapping_preview import field


class ManualMultiEpisodeAssemblyTests(TestCase):
    create = adopted.AdoptedMediaTests.create
    post = adopted.AdoptedMediaTests.post
    delivery = delivery_fixtures.ManualDeliveryTests.delivery
    delivery_request = delivery_fixtures.ManualDeliveryTests.delivery_request
    rule = output.RecipeAssemblyOutputTests.rule

    def setUp(self):
        adopted.AdoptedMediaTests.setUp(self)
        self.user = self.actor
        self.plan = get_run_execution_plan(self.run)
        self.read_url = f"/api/v1/production-runs/{self.run.pk}/manual-assembly/"
        font = next((path for path in (Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")) if path.is_file()), None)
        if font is None:
            self.skipTest("Real installed font required for isolated assembly")
        self.enterContext(override_settings(ASSEMBLY_AI_LABEL_FONTFILE=str(font)))
        self.clips = [self.clip("red", 440), self.clip("blue", 880)]
        self.episodes, self.shots, self.artifacts = [], [], []
        # Insert episode and shot rows in reverse order, so PK/insertion order
        # cannot accidentally satisfy the authored sequential contract.
        for number in (2, 1):
            episode = Episode.objects.create(project=self.project, number=number, title=f"Episode {number}")
            self.episodes.append(episode)
            for shot_number in (2, 1):
                self.shots.append(Shot.objects.create(episode=episode, number=shot_number, media_type="video",
                    duration_ms=1000, episode_start_ms=(shot_number - 1) * 1000,
                    episode_end_ms=shot_number * 1000, include_audio=True))
        self.episodes.sort(key=lambda row: row.number)
        self.shots.sort(key=lambda row: (row.episode.number, row.number))
        for shot in self.shots:
            asset = self.clips[(shot.number + shot.episode.number) % 2]
            body = {"action": "adopt", "stage_key": "media_generation",
                "scope": {"kind": "shot", "id": str(shot.pk)}, "unit_key": f"shot:{shot.pk}",
                "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": 0,
                "idempotency_key": str(uuid4()), "payload": {"source": {"kind": "asset",
                    "asset_id": str(asset.pk), "expected_sha256": asset.checksum, "media_kind": "video"}}}
            response = self.post(body)
            self.assertEqual(response.status_code, 201, response.data)
            decision = ArtifactSelectionDecision.objects.get(pk=response.data["target_id"])
            self.artifacts.append(decision.artifact)
        review = self.client.get(self.inventory_url)
        self.assertEqual(review.status_code, 200, review.data)
        response = self.post({"action": "approve", "stage_key": "artifact_selection",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "unit_key": f"project:{self.project.pk}",
            "expected_run_plan_sha256": self.plan.sha256,
            "expected_selection_revision": review.data["approval"]["revision"], "idempotency_key": str(uuid4()),
            "payload": {"selection": review.data["selection"], "expected_selection_sha256": review.data["selection_sha256"],
                "comment": "Explicit isolated test actor review, not user acceptance"}})
        self.assertEqual(response.status_code, 201, response.data)

    def clip(self, color, frequency):
        path = Path(self.workspace, f"{color}.mp4")
        subprocess.run([settings.FFMPEG_BINARY, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=160x96:r=24",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", "1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True, timeout=30)
        data = path.read_bytes()
        asset = Asset(owner=self.actor, name=path.name, kind="reference_video", mime_type="video/mp4",
            checksum=sha256(data).hexdigest())
        asset.file.save(path.name, ContentFile(data), save=True)
        return asset

    def authors(self):
        timing, media = [], []
        for episode in self.episodes:
            pairs = [(shot, artifact) for shot, artifact in zip(self.shots, self.artifacts) if shot.episode_id == episode.pk]
            timing.append({"number": episode.number, "shots": [{key: getattr(shot, key)
                for key in ("number", "duration_ms", "episode_start_ms", "episode_end_ms")} for shot, _ in pairs]})
            media.append({"number": episode.number, "shots": [
                {"number": shot.number, "artifact_id": str(artifact.pk)} for shot, artifact in pairs]})
        return {"assembly_output": self.rule({"布局": {"width": 160, "height": 96, "fps": 24}}, field("布局")),
            "assembly_timing": self.rule({"时序": {"episodes": timing}}, field("时序")),
            "assembly_media": self.rule({"片段": {"episodes": media}, "说明": "  两集原文\r\n不裁剪  "}, field("片段"))}

    def preview(self, authors):
        return self.client.post(self.read_url, {"expected_run_plan_sha256": self.plan.sha256, "authors": authors}, format="json")

    def enqueue_request(self, detail, index):
        episode = detail["episodes"][index]
        return {"action": "assemble", "stage_key": "assembly",
            "scope": {"kind": "episode", "id": episode["episode_id"]}, "unit_key": f"episode:{episode['episode_id']}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": detail["approval"]["revision"],
            "idempotency_key": str(uuid4()), "payload": {"expected_contract_sha256": detail["contract_sha256"],
                "expected_assembly_job_id": episode["job"]["id"] if episode["job"] else None, "authors": detail["authors"]}}

    def assert_no_generation(self):
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.exclude(generation_attempt=None, generation_task=None).exists())

    def assert_movie(self, job, number):
        probe = subprocess.run([settings.FFPROBE_BINARY, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", job.output_path], check=True, capture_output=True, timeout=20)
        data = json.loads(probe.stdout)
        video = next(row for row in data["streams"] if row["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["r_frame_rate"]), (160, 96, "24/1"))
        self.assertTrue(any(row["codec_type"] == "audio" for row in data["streams"]))
        self.assertAlmostEqual(float(data["format"]["duration"]), 2, delta=0.15)
        # Decode the center of each shot, away from the original AI label.
        for index, moment in enumerate((0.5, 1.5), start=1):
            frame = subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-ss", str(moment), "-i", job.output_path,
                "-frames:v", "1", "-vf", "crop=16:16:(iw-16)/2:(ih-16)/2,scale=1:1",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], check=True, capture_output=True, timeout=20).stdout
            self.assertEqual(len(frame), 3)
            expected = 0 if (number + index) % 2 == 0 else 2
            self.assertGreater(frame[expected], 180)
            self.assertLess(frame[2 - expected], 70)

    def test_two_episodes_authored_once_real_encode_seal_and_download_without_generation(self):
        authors = self.authors()
        preview = self.preview(authors)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["blockers"], [])
        self.assertEqual([row["number"] for row in preview.data["episodes"]], [1, 2])
        self.assert_no_generation()
        bodies = [self.enqueue_request(preview.data, index) for index in range(2)]
        # Author freeze covers both episodes even though each queue action is explicit.
        for index, body in enumerate(bodies):
            result = self.post(body)
            self.assertEqual(result.status_code, 202, result.data)
            stage = self.run.stages.get(stage_key="assembly")
            if index == 0:
                frozen = deepcopy(stage.input_snapshot)
                BusinessOutputVersion.objects.filter(pk__in=[row["output_version_id"] for row in authors.values()]).update(status="retired")
            self.assertEqual(stage.input_snapshot, frozen)
            reread = self.client.get(self.read_url)
            self.assertEqual(reread.status_code, 200, reread.data)
            self.assertEqual(reread.data["authors"], authors)
            self.assertEqual(reread.data["contract_sha256"], preview.data["contract_sha256"])
            job = claim_next_assembly(worker_id="isolated-m8-multiepisode")
            self.assertEqual(job.episode_id, self.episodes[index].pk)
            sources = list(job.source_items.order_by("ordinal"))
            self.assertEqual([row.generated_artifact_id for row in sources], [row.pk for row in self.artifacts[index * 2:index * 2 + 2]])
            with self.captureOnCommitCallbacks(execute=True):
                result = process_assembly_job(job)
            self.assertEqual(result.status, "succeeded", result.error_message)
            self.assert_movie(result, index + 1)
            if index == 0:
                incomplete = self.delivery()
                self.assertTrue(any("第 2 集" in text for text in incomplete["blockers"]))
                self.assertEqual(self.post(self.delivery_request(incomplete)).status_code, 400)
                self.assertFalse(ReleaseBundle.objects.exists())
        detail = self.delivery()
        self.assertEqual(detail["blockers"], [])
        body = self.delivery_request(detail)
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.post(body).status_code, 200)
        for request in bodies:
            self.assertEqual(self.post(request).status_code, 200)
        bundle = ReleaseBundle.objects.get()
        document = live_verified_release_bundle(bundle)
        self.assertEqual(document["verification"]["status"], "verified")
        self.assertEqual(len(document["artifacts"]), 4)
        self.assertEqual(len(document["assemblies"]), 2)
        self.assertTrue(all(row["source_kind"] == "asset" and row["preparation_id"] is None
            and row["request_sha256"] is None for row in document["artifacts"]))
        for artifact in GeneratedArtifact.objects.filter(production_run=self.run, role="episode_master"):
            response = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
            self.assertEqual(response.status_code, 200, response.data)
            for name in ("preview_url", "download_url"):
                download = self.client.get(response.data[name])
                try:
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(sha256(b"".join(download.streaming_content)).hexdigest(), artifact.sha256)
                finally:
                    # Django's test-client iterator already closes exhausted
                    # responses with its connection-signal guard. Do not emit
                    # request_finished twice inside this TestCase transaction.
                    if not download.closed:
                        download.close()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "succeeded")
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(stage_run__stage_key="media_generation").count(), 4)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run__stage_key="delivery").count(), 1)
        self.assertEqual(ProductionManualCommand.objects.filter(action="assemble").count(), 2)
        self.assertEqual(self.run.stages.get(stage_key="assembly").input_snapshot, frozen)
        self.assert_no_generation()

    def test_wrong_second_episode_media_or_timing_cannot_partially_freeze_or_enqueue(self):
        authors = self.authors()
        before = deepcopy(self.run.stages.get(stage_key="assembly").input_snapshot)
        for key, field_name, replacement in (("assembly_media", "artifact_id", str(self.artifacts[0].pk)),
                ("assembly_timing", "duration_ms", 900)):
            bad = deepcopy(authors)
            raw = json.loads(bad[key]["source_text"])
            root = "片段" if key == "assembly_media" else "时序"
            raw[root]["episodes"][1]["shots"][0][field_name] = replacement
            bad[key]["source_text"] = json.dumps(raw, ensure_ascii=False)
            response = self.preview(bad)
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertEqual(self.run.stages.get(stage_key="assembly").input_snapshot, before)
        self.assertFalse(ProductionManualCommand.objects.filter(action="assemble").exists())
        self.assert_no_generation()
