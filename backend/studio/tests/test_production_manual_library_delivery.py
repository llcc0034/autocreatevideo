"""Formal no-recipe library-adoption chain with real local FFmpeg/FFprobe.

Isolated database and media only; no model, GPU, browser or paid acceptance.
Missing configured tools/fonts are an explicit skip, never a fake encode.
"""
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings

from studio.models import (
    Asset, AssemblyJob, ExecutionPreparation, GenerationAttempt, GenerationTask,
    ProductionManualCommand, ProductionRecipeVersion, ReleaseBundle,
)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.release_bundles import live_verified_release_bundle
from studio.services.production_runs import _assert_delivery_complete, StageBlocked
from studio.tests import test_production_manual_storyboard as storyboard_fixtures
from studio.tests import test_execution_task_distribution as distribution_fixtures
from studio.tests.test_asset_management_api import image_upload


class ManualLibraryDeliveryTests(TestCase):
    create = storyboard_fixtures.ProductionManualStoryboardTests.create
    read = storyboard_fixtures.ProductionManualStoryboardTests.read
    request_body = storyboard_fixtures.ProductionManualStoryboardTests.request_body
    post = storyboard_fixtures.ProductionManualStoryboardTests.post
    require_font = distribution_fixtures.PreparedTaskDistributionTests.require_font

    def setUp(self):
        for key in ("FFMPEG_BINARY", "FFPROBE_BINARY"):
            binary = str(getattr(settings, key))
            if not (shutil.which(binary) or Path(binary).is_file()):
                self.skipTest(f"Real configured {key} is required")
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(override_settings(MEDIA_ROOT=root / "media", ASSET_LIBRARY_ROOT=root / "library"))
        self.require_font()
        self.enterContext(override_settings(ASSEMBLY_AI_LABEL_FONTFILE=settings.TASK_DISTRIBUTION_FONTFILE))
        self.recipe_count = ProductionRecipeVersion.objects.count()
        storyboard_fixtures.ProductionManualStoryboardTests.setUp(self)
        for shot in self.shots:
            shot.duration_ms = 500
            shot.save(update_fields=("duration_ms",))

    def get_detail(self, suffix):
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/{suffix}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def command(self, stage, action, payload, *, revision, scope=None):
        scope = scope or {"kind": "project", "id": str(self.project.pk)}
        return {"action": action, "stage_key": stage, "scope": scope,
            "unit_key": f"{scope['kind']}:{scope['id']}", "expected_run_plan_sha256": self.body["expected_run_plan_sha256"],
            "expected_selection_revision": revision, "idempotency_key": str(uuid4()), "payload": payload}

    def test_formal_manual_run_adopts_library_media_and_seals_real_episode_without_hidden_ai(self):
        self.assertEqual(self.post(self.request_body()).status_code, 201)
        for shot in self.shots:
            upload = self.client.post("/api/v1/assets/", {"kind": "reference", "name": f"Fixture shot {shot.number}",
                "description": "Isolated generated color image, not production content",
                "file": image_upload(f"shot-{shot.number}.png")}, format="multipart")
            self.assertEqual(upload.status_code, 201, upload.data)
            asset = Asset.objects.get(pk=upload.data["id"])
            adopted = self.post(self.command("media_generation", "adopt", {"source": {"kind": "asset",
                "asset_id": str(asset.pk), "expected_sha256": asset.checksum, "media_kind": "image"}},
                revision=0, scope={"kind": "shot", "id": str(shot.pk)}))
            self.assertEqual(adopted.status_code, 201, adopted.data)
        review = self.get_detail("manual-media-review")
        approved = self.post(self.command("artifact_selection", "approve", {"selection": review["selection"],
            "expected_selection_sha256": review["selection_sha256"], "comment": "Reviewed both fixture shots"},
            revision=review["approval"]["revision"]))
        self.assertEqual(approved.status_code, 201, approved.data)
        assembly = self.get_detail("manual-assembly")
        self.assertEqual(assembly["blockers"], [])
        self.assertEqual(len(assembly["episodes"]), 1, "The outside episode is not silently included")
        queued = self.post(self.command("assembly", "assemble", {"expected_contract_sha256": assembly["contract_sha256"],
            "expected_assembly_job_id": None}, revision=assembly["approval"]["revision"],
            scope={"kind": "episode", "id": str(self.episode.pk)}))
        self.assertEqual(queued.status_code, 202, queued.data)
        with self.captureOnCommitCallbacks(execute=True):
            job = process_assembly_job(claim_next_assembly(worker_id="manual-library-real-encoder"))
        self.assertEqual(job.status, "succeeded", job.error_message)
        media = verified_local_media_facts(Path(job.output_path), expected_kind="video", max_bytes=Path(job.output_path).stat().st_size)
        self.assertEqual(media["mime_type"], "video/mp4")
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "awaiting_input")
        delivery = self.get_detail("manual-delivery")
        self.assertEqual(delivery["blockers"], [])
        body = self.command("delivery", "deliver", {"expected_contract_sha256": delivery["contract_sha256"],
            "comment": "Real isolated media verified by FFprobe"}, revision=delivery["contract"]["selection_revision"])
        with patch("studio.services.assembly_queue._run_ffmpeg", side_effect=AssertionError("Seal must not encode")), \
                patch("studio.services.distribution.prepare_task_distribution", side_effect=AssertionError("Library media is not a model call")):
            sealed = self.post(body)
            self.assertEqual(sealed.status_code, 201, sealed.data)
            self.assertEqual(self.post(body).data, sealed.data)
        bundle = ReleaseBundle.objects.get(pk=sealed.data["target_id"])
        document = live_verified_release_bundle(bundle)
        self.assertEqual(document["verification"]["status"], "verified")
        self.assertEqual(len(document["artifacts"]), 2)
        self.assertTrue(all(row["source_kind"] == "asset" and row["preparation_id"] is None for row in document["artifacts"]))
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "succeeded")
        self.assertIsNone(self.run.recipe_version_id)
        self.assertEqual(self.run.stages.get(stage_key="script_generate").status, "pending")
        self.assertEqual(ProductionRecipeVersion.objects.count(), self.recipe_count, "No hidden Recipe is created")
        for model in (ExecutionPreparation, GenerationAttempt, GenerationTask):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(ProductionManualCommand.objects.filter(action="deliver").count(), 1)
        # The formal release reader and signed file route deliver the exact
        # locally encoded master, not a selected shot or a regenerated copy.
        verified = self.client.get(f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(verified.status_code, 200, verified.data)
        master_id = document["selection"]["episodes"][0]["artifact_id"]
        artifact = self.client.get(f"/api/v1/generated-artifacts/{master_id}/")
        self.assertEqual(artifact.status_code, 200, artifact.data)
        output = Path(job.output_path)
        original_bytes = output.read_bytes()
        downloaded = self.client.get(artifact.data["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(b"".join(downloaded.streaming_content), original_bytes)
        downloaded.close()
        partial = self.client.get(artifact.data["preview_url"], HTTP_RANGE="bytes=2-31")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(b"".join(partial.streaming_content), original_bytes[2:32])
        partial.close()
        self.assertEqual(_assert_delivery_complete(self.run).pk, bundle.pk)
        # Signature-only checks cannot authorize final success after bytes have
        # disappeared or changed since sealing. Never rewrite old Run status.
        for invalid in (b"", b"not the sealed master", None):
            with self.subTest(invalid=invalid), self.assertRaises(StageBlocked):
                try:
                    if invalid is None:
                        output.unlink()
                    else:
                        output.write_bytes(invalid)
                    self.assertEqual(self.client.get(f"/api/v1/release-bundles/{bundle.pk}/verify/").status_code, 400)
                    self.assertEqual(self.client.get(artifact.data["download_url"]).status_code, 404)
                    _assert_delivery_complete(self.run)
                finally:
                    output.write_bytes(original_bytes)
