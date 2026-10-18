"""Release sealing over real prepared/adopted PNG identities and test masters."""
from copy import deepcopy
from pathlib import Path
import hashlib
import subprocess
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings

from studio.models import GeneratedArtifact
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.production_manual_actions import perform_manual_action
from studio.services.production_manual_media_review import manual_media_review_detail
from studio.services.release_bundles import create_release_draft, seal_release_bundle, live_verified_release_bundle, ReleaseBundleError
from studio.tests import test_production_manual_assembly as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionReleaseSourceTests(TestCase):
    setUp = fixtures.ManualAssemblyTests.setUp
    mark_submitted = fixtures.ManualAssemblyTests.mark_submitted
    archive = fixtures.ManualAssemblyTests.archive
    resolve = fixtures.ManualAssemblyTests.resolve
    approve = fixtures.ManualAssemblyTests.approve
    request = fixtures.ManualAssemblyTests.request
    detail = fixtures.ManualAssemblyTests.detail
    post = fixtures.ManualAssemblyTests.post

    def adopt_library(self):
        scope = {"kind": "shot", "id": str(self.shot.pk)}
        perform_manual_action(run_id=self.run.pk, actor=self.user, request={"action": "adopt", "stage_key": "media_generation",
            "scope": scope, "unit_key": f"shot:{self.shot.pk}", "expected_run_plan_sha256": self.plan.sha256,
            "expected_selection_revision": 0, "idempotency_key": str(uuid4()), "payload": {"source": {"kind": "asset",
                "asset_id": str(self.asset.pk), "expected_sha256": self.asset.checksum, "media_kind": "image"}}})
        self.artifact = GeneratedArtifact.objects.get(production_run=self.run, status="selected")
        detail = manual_media_review_detail(self.run)
        perform_manual_action(run_id=self.run.pk, actor=self.user, request={"action": "approve", "stage_key": "artifact_selection",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "unit_key": f"project:{self.project.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": detail["approval"]["revision"],
            "idempotency_key": str(uuid4()), "payload": {"selection": detail["selection"], "expected_selection_sha256": detail["selection_sha256"], "comment": "Reviewed library source"}})

    def complete_master(self):
        response = self.post(self.request())
        self.assertEqual(response.status_code, 202, response.data)
        def encode(command, **kwargs):
            Path(command[-1]).write_bytes(b"synthetic release-test encoder output")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with self.captureOnCommitCallbacks(execute=True), patch("studio.services.assembly_queue._run_ffmpeg", side_effect=encode):
            job = process_assembly_job(claim_next_assembly(worker_id="release-source-test"))
        self.assertEqual(job.status, "succeeded")
        master = GeneratedArtifact.objects.get(production_run=self.run, kind="assembly")
        self.master = master
        self.selection = {"shots": [{"shot_id": str(self.shot.pk), "artifact_id": str(self.artifact.pk)}],
            "episodes": [{"episode_id": str(self.episode.pk), "artifact_id": str(master.pk)}], "series_artifact_id": None}
        # Only the synthetic encoder output probe is replaced. PNG, source,
        # preparation, signature, adoption and selection verification are real.
        def media_facts(path, **kwargs):
            if Path(path) == Path(job.output_path):
                data = Path(path).read_bytes()
                return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "mime_type": "video/mp4"}
            return verified_local_media_facts(path, **kwargs)
        self.enterContext(patch("studio.services.release_bundles.verified_local_media_facts", side_effect=media_facts))
        return job

    def draft(self):
        return create_release_draft(production_run=self.run, selection=self.selection, project=self.project,
            owner=self.user, created_by=self.user)

    def test_adopted_library_source_seals_without_fabricated_model_or_distribution(self):
        self.adopt_library(); job = self.complete_master()
        from studio.services.provenance import verified_assembly_manifest
        self.assertIsNone(verified_assembly_manifest(job)["disclosure"]["ai_generated"])
        self.assertIsNone(self.client.get(f"/api/v1/assemblies/{job.pk}/").data["ai_generated"])
        with patch("studio.services.release_bundles.verified_distribution_manifest", side_effect=AssertionError("Not a model call")):
            bundle = seal_release_bundle(self.draft())
            document = live_verified_release_bundle(bundle)
        row, = document["artifacts"]
        self.assertEqual(row["source_kind"], "asset")
        self.assertEqual(row["source_asset_id"], str(self.asset.pk))
        self.assertIsNone(row["preparation_id"]); self.assertIsNone(row["request_sha256"])
        self.assertIsNone(row["distribution_model"]); self.assertIsNone(row["distribution_label"])
        self.assertEqual(document["production_run_id"], str(self.run.pk))
        self.assertEqual(document["verification"]["live_verified"], True)

    def test_comfy_source_preserves_public_copy_gate_and_exact_preparation(self):
        from studio.services.distribution import DistributionError
        self.approve(); job = self.complete_master()
        from studio.services.provenance import verified_assembly_manifest
        self.assertIs(verified_assembly_manifest(job)["disclosure"]["ai_generated"], True)
        self.assertIs(self.client.get(f"/api/v1/assemblies/{job.pk}/").data["ai_generated"], True)
        bundle = self.draft()
        with patch("studio.services.release_bundles.verified_distribution_manifest", side_effect=DistributionError("Missing test public copy")):
            with self.assertRaises(ReleaseBundleError) as failure:
                seal_release_bundle(bundle)
        self.assertIn("managed_distribution_actions", failure.exception.details)
        distribution = {"artifact": {"sha256": self.artifact.sha256}, "integrity": {"payload_sha256": "d" * 64},
            "disclosure": {"label": "Isolated distribution fixture", "version": "test"},
            "release": {"release_id": str(uuid4()), "model_key": "fixture", "model": "fixture"}}
        with patch("studio.services.release_bundles.verified_distribution_manifest", return_value=distribution):
            document = live_verified_release_bundle(seal_release_bundle(bundle))
        row, = document["artifacts"]
        self.assertEqual(row["source_kind"], "execution")
        self.assertEqual(row["preparation_id"], str(self.preparation.pk))
        self.assertEqual(row["request_sha256"], self.attempt.request_sha256)
        self.assertIsNone(row["source_asset_id"])

    def test_master_source_drift_and_different_run_are_not_silently_replaced(self):
        self.adopt_library(); job = self.complete_master()
        bundle = self.draft()
        source = job.source_items.get()
        source.sha256 = "0" * 64; source.save(update_fields=("sha256",))
        with self.assertRaises(ReleaseBundleError):
            seal_release_bundle(bundle)
        source.sha256 = self.artifact.sha256; source.save(update_fields=("sha256",))
        sealed = seal_release_bundle(bundle)
        altered = deepcopy(self.selection); altered["shots"][0]["artifact_id"] = str(uuid4())
        sealed.selection = altered
        with self.assertRaises(ReleaseBundleError):
            live_verified_release_bundle(sealed)

    def test_master_requires_its_exact_run_assembly_link_even_with_valid_signed_bytes(self):
        from studio.models import ProductionAssemblyLink
        self.adopt_library(); job = self.complete_master()
        bundle = self.draft()
        link = ProductionAssemblyLink.objects.get(production_run=self.run, assembly_job=job)
        original_stage = link.stage_run_id
        ProductionAssemblyLink.objects.filter(pk=link.pk).update(stage_run=self.run.stages.get(stage_key="delivery"))
        with self.assertRaises(ReleaseBundleError) as failure:
            seal_release_bundle(bundle)
        self.assertIn("assembly_run_link_invalid", failure.exception.details["reasons"])
        bundle.refresh_from_db(); self.assertEqual(bundle.status, "draft")
        ProductionAssemblyLink.objects.filter(pk=link.pk).update(stage_run_id=original_stage)
        sealed = seal_release_bundle(bundle)
        ProductionAssemblyLink.objects.filter(pk=link.pk).delete()
        with self.assertRaises(ReleaseBundleError):
            live_verified_release_bundle(sealed)
