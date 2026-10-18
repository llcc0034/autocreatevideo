"""INT-02 assisted journey: original uploads/reuse and generated appearances.

Reviews are explicit isolated test-actor operations, not user acceptance.
External peer replies remain synthetic; all source/queue/archive writers and
FFmpeg assembly are the original implementations.
"""
from copy import deepcopy
from pathlib import Path

from django.test import TestCase, override_settings

from studio.models import (ApprovalDecision, Asset, AssetReferenceVersion, BibleVariantRevision,
    GeneratedArtifact, GenerationAttempt, ProductionMaterialFulfillment, ReleaseBundle)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.production_recipe_material_review import material_review_context
from studio.services.task_queue import claim_next_task
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests.test_asset_management_api import image_upload
from studio.tests import test_int_production_journey as journey


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationMixedJourneyTests(IntegrationContracts, TestCase):
    setUp = journey.IntegrationProductionJourneyTests.setUp
    tearDown = journey.IntegrationProductionJourneyTests.tearDown
    advance = journey.IntegrationProductionJourneyTests.advance
    state = journey.IntegrationProductionJourneyTests.state
    collect = journey.IntegrationProductionJourneyTests.collect
    assert_media_lineage_and_delivery = journey.IntegrationProductionJourneyTests.assert_media_lineage_and_delivery

    def upload(self, name):
        response = self.client.post("/api/v1/assets/", {
            "name": name, "kind": "reference", "file": image_upload(name + ".png")}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"])

    def approve(self, key):
        body = {"stage_key": key, "decision": "approve", "comment": "INT isolated test actor"}
        if key == "bible_review":
            detail = self.api("get", self.url + "bible-review/")
            body["bible_review"] = {
                "expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                "expected_project_bible_sha256": detail["project_bible_sha256"],
                "reviewed_project_bible": True,
                "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]}
        elif key == "bible_image_review":
            stage = self.sample.run.stages.get(stage_key="bible_image_generation")
            context = material_review_context(self.sample.run, stage.output_snapshot["canonical"])
            body.update(expected_source_snapshot_sha256=stage.output_snapshot_sha256,
                bible_image_bindings=[{"entity_type": kind, "entity_id": identity,
                    "selected_artifact_ids": [], "anchor_artifact_id": None,
                    "anchor_library_asset_id": row["primary_asset_id"],
                    "selected_library_assets": [{"asset_id": asset_id, "expected_sha256": fact["checksum"]}
                        for asset_id, fact in sorted(row["assets"].items())]}
                    for (kind, identity), row in context.items()])
        elif key == "storyboard_review":
            detail = self.api("get", self.url + "storyboard-reference-selection/")
            self.assertEqual(detail["shot_count"], 2)
            body.update(expected_source_snapshot_sha256=detail["source_snapshot_sha256"],
                expected_selection_sha256=detail["selection_sha256"])
        elif key == "artifact_selection":
            for row in self.submissions:
                if row["kind"] == "video":
                    self.api("post", self.url + "select-artifact/", {"artifact_id": str(row["artifact"].pk)},
                        HTTP_IDEMPOTENCY_KEY=f"int-mixed:{row['artifact'].pk}")
        else:
            self.fail(f"Unexpected review: {key}")
        self.api("post", self.url + "approve/", body)

    def fulfill_primaries(self):
        stage = self.sample.run.stages.get(stage_key="bible_image_generation")
        revisions = list(BibleVariantRevision.objects.filter(variant__project=self.sample.project,
            variant__appearance_role="primary").select_related("variant").order_by("variant__source_variant_key", "pk"))
        self.assertEqual(len(revisions), 3)
        self.fulfillments = []
        for index, setting in enumerate(revisions):
            series = self.api("post", "/api/v1/asset-reference-series/", {
                "name": f"INT mixed primary {index}", "role": "primary", "bible_variant_id": str(setting.variant_id)}, status=201)
            is_existing = setting.variant.character_id is not None
            asset = self.existing if is_existing else self.upload(f"INT new upload {index}")
            # The recipe reuses exact library versions. Newly uploaded bytes
            # first enter the same formal series/version API and keep upload
            # provenance; they are not relabelled as an existing origin.
            version = self.api("post", "/api/v1/asset-reference-versions/", {
                "series_id": series["id"], "asset_id": str(asset.pk),
                "setting_revision_id": str(setting.pk),
                "source": "existing" if is_existing else "upload"}, status=201)
            context = self.api("get", self.url + "material-fulfillment-context/", {
                "stage_run_id": str(stage.pk), "unit_key": f"appearance:{setting.variant_id}"})
            body = {"stage_run_id": str(stage.pk), "unit_key": f"appearance:{setting.variant_id}",
                "requirement_key": f"setting:{setting.pk}", "expected_run_plan_sha256": context["run_plan_sha256"],
                "reference_version_id": version["id"]}
            choice = self.api("post", self.url + "fulfill-material/", body, status=201)
            self.assertEqual(self.api("post", self.url + "fulfill-material/", body), choice)
            self.fulfillments.append(ProductionMaterialFulfillment.objects.get(pk=choice["id"]))
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_mixed_sources_reuse_without_primary_calls_then_variants_video_and_delivery(self):
        self.existing = self.upload("INT already in asset library")
        existing_fact = (self.existing.checksum, Path(self.existing.file.path).read_bytes())
        self.sample = self.int_sample(layout="b", mode="mixed")
        self.url = f"/api/v1/production-runs/{self.sample.run.pk}/"
        original_plan = deepcopy(self.sample.run.execution_plan.definition)
        self.assertEqual(self.advance().current_stage_key, "bible_review")
        self.approve("bible_review")
        self.sample.run.refresh_from_db()
        self.assertEqual(self.sample.run.current_stage_key, "bible_image_generation", self.state())
        self.fulfill_primaries()
        for _ in range(24):
            run = self.advance()
            if run.status == "succeeded":
                break
            self.assertNotIn(run.status, ("failed", "cancelled", "blocked"), self.state())
            if run.status == "awaiting_approval":
                self.approve(run.current_stage_key)
                continue
            task = claim_next_task()
            if task is not None:
                self.collect(task)
                continue
            assembly = claim_next_assembly(worker_id="int-mixed-isolated")
            if assembly is not None:
                with self.captureOnCommitCallbacks(execute=True):
                    finished = process_assembly_job(assembly)
                self.assertEqual(finished.status, "succeeded", finished.error_message)
                continue
            self.fail(f"No runnable mixed work: {self.state()}")
        else:
            self.fail(f"Mixed integration did not finish: {self.state()}")
        self.assertEqual([row["kind"] for row in self.submissions].count("image"), 2)
        self.assertEqual([row["kind"] for row in self.submissions].count("video"), 2)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=run).count(), 4)
        versions = [row.reference_version for row in self.fulfillments]
        self.assertEqual(sorted(row.source for row in versions), ["existing", "upload", "upload"])
        self.assertTrue(all(row.selection_mode == "manual" and row.selected_by_id == self.actor.pk for row in self.fulfillments))
        primary = next(row for row in versions if row.asset_id == self.existing.pk)
        variants = list(AssetReferenceVersion.objects.filter(series__bible_variant__project=self.sample.project, source="generated"))
        self.assertEqual(len(variants), 2)
        self.assertEqual({row.generated_from_id for row in variants}, {primary.pk})
        self.existing.refresh_from_db()
        self.assertEqual((self.existing.checksum, Path(self.existing.file.path).read_bytes()), existing_fact)
        self.assertEqual(self.sample.run.execution_plan.definition, original_plan)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run__production_run=run, actor="human", decided_by=self.actor).count(), 4)
        bundle = ReleaseBundle.objects.get(production_run=run)
        verified = self.api("get", f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(verified["verification"]["status"], "verified")
        self.assertEqual({row["generated_artifact_id"] for row in verified["artifacts"]},
            {str(row["artifact"].pk) for row in self.submissions if row["kind"] == "video"})
        self.assert_media_lineage_and_delivery(run)
        before = list(GenerationAttempt.objects.filter(stage_run__production_run=run).values())
        self.advance()
        self.assertEqual(list(GenerationAttempt.objects.filter(stage_run__production_run=run).values()), before)
