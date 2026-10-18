"""M8 full combination using freshly archived M7 worker outputs.

Only provider replies/download transport are synthetic. Formal recipe publication,
Run APIs, M7 queues, selection, missing-file failure, exact retry, FFmpeg, signed
release and media reads stay real in the test database and temporary storage.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from django.conf import settings
from django.test import TestCase, override_settings

from studio.models import (ArtifactSelectionDecision, AssemblyJob, GeneratedArtifact,
    GenerationAttempt, ProductionAssemblyLink, ReleaseBundle, Shot)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.production_runs import advance_production_run, decide_stage
from studio.services.production_storyboard_policy import read_recipe_storyboard_reference_selection
from studio.tests import test_production_recipe_delivery_chain as recipe


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class M8FullDeliveryJourneyTests(TestCase):
    setUpTestData = classmethod(recipe.RecipeDeliveryChainTests.setUpTestData.__func__)
    setUp = recipe.RecipeDeliveryChainTests.setUp
    profile_instance = recipe.RecipeDeliveryChainTests.profile_instance
    root = recipe.RecipeDeliveryChainTests.root
    receipt = recipe.RecipeDeliveryChainTests.receipt
    pass_smoke = recipe.RecipeDeliveryChainTests.pass_smoke
    advance = recipe.RecipeDeliveryChainTests.advance
    response = recipe.RecipeDeliveryChainTests.response
    process = recipe.RecipeDeliveryChainTests.process
    finish_content_chain = recipe.RecipeDeliveryChainTests.finish_content_chain
    media_instance = recipe.RecipeDeliveryChainTests.media_instance
    media_bytes = recipe.RecipeDeliveryChainTests.media_bytes
    transfer = recipe.RecipeDeliveryChainTests.transfer
    pass_media_smoke = recipe.RecipeDeliveryChainTests.pass_media_smoke
    collected = staticmethod(recipe.RecipeDeliveryChainTests.collected)
    finish_media = recipe.RecipeDeliveryChainTests.finish_media
    start_complete_run = recipe.RecipeDeliveryChainTests.start_complete_run

    def seed_new_candidates(self):
        self.start_complete_run(episode_count=2, shots_per_episode=2)
        self.run = advance_production_run(self.run)
        self.finish_content_chain(revision=None)
        storyboard = self.run.stages.get(stage_key="storyboard")
        refs = read_recipe_storyboard_reference_selection(self.run)
        self.run = decide_stage(self.run, stage_key="storyboard_review", decision="approve", user=self.actor,
            comment="Isolated test actor; not live user acceptance",
            expected_source_snapshot_sha256=storyboard.output_snapshot_sha256,
            expected_selection_sha256=refs["selection_sha256"])
        self.assertEqual(self.run.current_stage_key, "media_generation", self.run.blocked_reason)
        attempts = list(GenerationAttempt.objects.filter(stage_run__production_run=self.run,
            stage_run__stage_key="media_generation").order_by("created_at"))
        self.assertEqual(len(attempts), 4)
        self.original_requests = {str(row.pk): deepcopy(row.request_snapshot) for row in attempts}
        self.candidates = []
        self.candidate_shots = {}
        for attempt in attempts:
            shot = Shot.objects.select_related("episode").get(pk=attempt.unit_key.split(":")[1])
            self.output = Path(self.media_root) / f"provider-{shot.episode.number}-{shot.number}.mp4"
            color = "red" if shot.number == 1 else "blue"
            subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=64x64:r=24:d=5", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(self.output)], check=True, capture_output=True, timeout=30)
            artifact = self.finish_media(attempt)
            self.assertEqual(artifact.generation_attempt_id, attempt.pk)
            self.candidates.append(artifact)
            self.candidate_shots[artifact.pk] = shot.pk
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.status, self.run.current_stage_key), ("awaiting_approval", "artifact_selection"))

    def select_and_queue(self):
        url = f"/api/v1/production-runs/{self.run.pk}/select-artifact/"
        for artifact in self.candidates:
            response = self.client.post(url, {"artifact_id": str(artifact.pk)}, format="json",
                HTTP_IDEMPOTENCY_KEY=f"m8-full:{artifact.pk}")
            self.assertEqual(response.status_code, 200, response.data)
        self.selection_facts = list(ArtifactSelectionDecision.objects.filter(
            stage_run__production_run=self.run).order_by("pk").values())
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/approve/",
            {"stage_key": "artifact_selection", "decision": "approve", "comment": "Isolated exact four clips"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.current_stage_key, "assembly", self.run.blocked_reason)
        self.assembly = self.run.stages.get(stage_key="assembly")
        self.input_sha = self.assembly.input_snapshot_sha256
        self.assertEqual(AssemblyJob.objects.count(), 2)

    def execute(self):
        job = claim_next_assembly(worker_id="m8-full-isolated")
        self.assertIsNotNone(job)
        with self.captureOnCommitCallbacks(execute=True):
            result = process_assembly_job(job)
        return result

    def assert_sealed_delivery(self):
        # Read only: a verifier must not finish work missed by the actual worker.
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "succeeded", self.run.blocked_reason)
        self.failed_job.refresh_from_db()
        self.assertEqual(self.failed_job.status, "failed")
        self.assertEqual(self.failed_job.consumer_input_sha256, self.input_sha)
        self.good_job.refresh_from_db()
        self.assertEqual((self.good_job.pk, self.good_job.output_path,
            Path(self.good_job.output_path).read_bytes(), self.good_job.consumer_input_sha256), self.good_facts)
        self.assertEqual(AssemblyJob.objects.count(), 3)
        bundle = ReleaseBundle.objects.get(production_run=self.run)
        response = self.client.get(f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(response.status_code, 200, response.data)
        self.document = response.data
        self.assertEqual(self.document["verification"]["status"], "verified")
        self.assertEqual(len(self.document["artifacts"]), 4)
        self.assertEqual(len(self.document["assemblies"]), 2)
        self.assertTrue(all(row["source_kind"] == "execution" and row["preparation_id"]
            and row["request_sha256"] for row in self.document["artifacts"]))
        expected_sources = {(str(self.candidate_shots[row.pk]), str(row.pk),
            str(row.generation_attempt.preparation_id), row.generation_attempt.request_sha256) for row in self.candidates}
        self.assertEqual({(row["shot_id"], row["generated_artifact_id"], row["preparation_id"],
            row["request_sha256"]) for row in self.document["artifacts"]}, expected_sources)
        run_detail = self.client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(run_detail.status_code, 200, run_detail.data)
        self.assertEqual(run_detail.data["release_bundle_id"], str(bundle.pk))
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=self.run).count(), 5)
        for attempt_id, original in self.original_requests.items():
            self.assertEqual(GenerationAttempt.objects.get(pk=attempt_id).request_snapshot, original)
        self.assertEqual(list(ArtifactSelectionDecision.objects.filter(stage_run__production_run=self.run)
            .order_by("pk").values()), self.selection_facts)
        self.assembly.refresh_from_db()
        self.assertEqual(self.assembly.input_snapshot_sha256, self.input_sha)
        self.download_facts = []
        delivered_assemblies = set()
        for link in ProductionAssemblyLink.objects.filter(production_run=self.run).select_related("episode", "assembly_job").order_by("episode__number"):
            job = link.assembly_job
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.consumer_stage_id, self.assembly.pk)
            self.assertEqual(job.consumer_input_sha256, self.input_sha)
            sources = list(job.source_items.select_related("shot").order_by("ordinal"))
            self.assertEqual([source.shot.number for source in sources], [1, 2])
            self.assertEqual({source.generated_artifact_id for source in sources},
                {row.pk for row in self.candidates if self.candidate_shots[row.pk] in {source.shot_id for source in sources}})
            movie = Path(job.output_path)
            probe = json.loads(subprocess.run([settings.FFPROBE_BINARY, "-v", "error", "-show_format",
                "-show_streams", "-of", "json", str(movie)], check=True, capture_output=True, timeout=20).stdout)
            self.assertAlmostEqual(float(probe["format"]["duration"]), 10, delta=.2)
            for moment, channel in ((2.5, 0), (7.5, 2)):
                pixel = subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-ss", str(moment), "-i", str(movie),
                    "-frames:v", "1", "-vf", "crop=8:8:(iw-8)/2:(ih-8)/2,scale=1:1", "-pix_fmt", "rgb24",
                    "-f", "rawvideo", "pipe:1"], check=True, capture_output=True, timeout=20).stdout
                self.assertEqual(len(pixel), 3)
                self.assertGreater(pixel[channel], 170)
                self.assertLess(pixel[2 - channel], 80)
            master = GeneratedArtifact.objects.get(production_run=self.run, role="episode_master", manifest_key=f"assembly:{job.pk}")
            delivered_assemblies.add((str(job.pk), str(master.pk), master.sha256, str(link.episode_id)))
            detail = self.client.get(f"/api/v1/generated-artifacts/{master.pk}/")
            self.assertEqual(detail.status_code, 200, detail.data)
            original = movie.read_bytes()
            for key, headers, status, expected in (("download_url", {}, 200, original),
                    ("preview_url", {"HTTP_RANGE": "bytes=5-127"}, 206, original[5:128])):
                downloaded = self.client.get(detail.data[key], **headers)
                try:
                    self.assertEqual(downloaded.status_code, status)
                    self.assertEqual(b"".join(downloaded.streaming_content), expected)
                finally:
                    if not downloaded.closed:
                        downloaded.close()
            self.assertEqual(sha256(original).hexdigest(), master.sha256)
            self.download_facts.append({"episode": link.episode.number, "artifact_id": str(master.pk),
                "bytes": len(original), "sha256": master.sha256})
        self.assertEqual({(row["assembly_id"], row["generated_artifact_id"], row["artifact_sha256"], row["episode_id"])
            for row in self.document["assemblies"]}, delivered_assemblies)
        self.assertNotIn(str(self.failed_job.pk), {row["assembly_id"] for row in self.document["assemblies"]})

    def prepare_failed_episode(self):
        self.seed_new_candidates()
        self.select_and_queue()
        good = self.execute()
        self.assertEqual(good.status, "succeeded", good.error_message)
        second = AssemblyJob.objects.exclude(pk=good.pk).get()
        missing_source = second.source_items.order_by("ordinal").first()
        missing = GeneratedArtifact.objects.get(pk=missing_source.generated_artifact_id)
        path = Path(settings.MEDIA_ROOT) / missing.object_key
        original = path.read_bytes()
        path.unlink()  # Only isolated managed test media, restored below.
        try:
            for _ in range(second.max_attempts):
                failed = self.execute()
                self.assertEqual(failed.pk, second.pk)
                if failed.status == "failed":
                    break
            self.assertEqual(failed.status, "failed", failed.error_message)
            self.assertIn(str(missing.pk), failed.error_message)
            self.assertFalse(ReleaseBundle.objects.filter(production_run=self.run).exists())
            self.run = advance_production_run(self.run)
            url = f"/api/v1/production-runs/{self.run.pk}/retry-preview/"
            invalid = self.client.get(url, {"expected_stage_id": str(self.assembly.pk)})
            self.assertEqual(invalid.status_code, 200, invalid.data)
            self.assertFalse(invalid.data["can_submit"])
            self.assertEqual(AssemblyJob.objects.count(), 2)
        finally:
            path.write_bytes(original)
        self.good_job, self.failed_job = good, failed
        self.good_facts = (good.pk, good.output_path, Path(good.output_path).read_bytes(), good.consumer_input_sha256)
        return url

    def test_new_worker_clips_missing_second_episode_retry_only_failure_then_export(self):
        url = self.prepare_failed_episode()
        good, failed = self.good_job, self.failed_job
        preview = self.client.get(url, {"expected_stage_id": str(self.assembly.pk)})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data["can_submit"], preview.data)
        context = deepcopy(preview.data["retry_context"])
        self.assertEqual(context["expected_assembly_job_ids"], [str(failed.pk)])
        self.assertEqual(context["expected_input_sha256"], self.input_sha)
        retry_url = f"/api/v1/production-runs/{self.run.pk}/retry/"
        retried = self.client.post(retry_url, context, format="json")
        self.assertEqual(retried.status_code, 200, retried.data)
        successor = self.execute()
        self.assertNotEqual(successor.pk, failed.pk)
        self.assertEqual(successor.episode_id, failed.episode_id)
        self.assertEqual(successor.status, "succeeded", successor.error_message)
        failed.refresh_from_db(); self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.consumer_input_sha256, self.input_sha)
        good.refresh_from_db()
        self.assertEqual((good.pk, good.output_path, Path(good.output_path).read_bytes(), good.consumer_input_sha256), self.good_facts)
        self.assertEqual(AssemblyJob.objects.count(), 3)
        self.assertEqual(self.client.post(retry_url, context, format="json").status_code, 400)
        self.assert_sealed_delivery()
