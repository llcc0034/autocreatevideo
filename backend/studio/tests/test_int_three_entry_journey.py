"""INT-03: one governed generated Asset consumed through all three entrances.

Authors, preparation/compiler, queue, staging and archive writers are real.
Only admission observations and external model replies use isolated fixtures;
the in-memory database and temporary PNGs never claim current-target Smoke.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from studio.models import (Asset, AssetGenerationJob, AssetReferenceVersion, ExecutionPreparation,
    GenerationAttempt, ProductionRun, PropBible, SeriesProject)
from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.task_queue import claim_next_task
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests import test_int_mixed_journey as mixed
from studio.tests import test_int_production_journey as journey
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationThreeEntryJourneyTests(IntegrationContracts, TestCase):
    def setUp(self):
        journey.IntegrationProductionJourneyTests.setUp(self)
        # Compiler dependency fixtures import inactive templates. The asset
        # entrance correctly requires explicit execution availability too.
        template = self.primary_workflow.template
        template.is_active = True
        template.save(update_fields=("is_active",))
        self.assertTrue(self.actor.is_staff)
    advance = journey.IntegrationProductionJourneyTests.advance
    state = journey.IntegrationProductionJourneyTests.state
    collect = journey.IntegrationProductionJourneyTests.collect
    approve = mixed.IntegrationMixedJourneyTests.approve
    upload = mixed.IntegrationMixedJourneyTests.upload
    fulfill_primaries = mixed.IntegrationMixedJourneyTests.fulfill_primaries

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def request(self, target, inputs):
        version, contract = target
        return {"execution_instance_version_id": str(version.pk), "expected_definition_sha256": contract["sha256"],
            "parameters": self.image_parameters, "inputs": inputs, "manual_channels": {},
            "subject_facts": [], "source_language": "English", "target_language": "English"}

    def independent(self, key, target, inputs):
        body = {**self.request(target, inputs), "job_type": "asset", "media_type": "image",
            "image_category": "reference", "name": key, "field_values": [],
            "appearance_target": {"role": "primary", "setting_revision_id": None,
                "primary_version_id": None, "primary_input": None}}
        created = self.api("post", "/api/v1/asset-generation-jobs/", body, status=202, HTTP_IDEMPOTENCY_KEY=key)
        job = AssetGenerationJob.objects.get(pk=created["id"])
        prep = freeze(job, self.actor)
        peer, upload = Mock(), RecordingUploadClient()
        def cycle():
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            claim = claim_next_comfy_asset_job("int-three-entry-isolated")
            self.assertIsNotNone(claim)
            self.assertEqual(claim.pk, job.pk)
            return process_comfy_asset_job(claim)
        with patch("studio.services.asset_generation.driver_for", return_value=peer), patch(
                "studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=upload), patch(
                "studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations):
            dispatch_asset_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
            peer.invoke.return_value = {"request_id": str(prep.pk)}
            first = cycle()
            self.assertEqual(first.status, "submitted", first.error_message)
            peer.invoke.return_value = {"status": "succeeded"}
            self.assertEqual(cycle().status, "running")
            compiled = prep.contract.prepared_request["transport"]["payload"]["compiled_workflow"]
            prefix = PurePosixPath(compiled["resolved_semantics"]["output.prefix"])
            output = Path(settings.COMFYUI_OUTPUT_DIR) / Path(*prefix.parent.parts) / f"{prefix.name}_00001.png"
            self.assertTrue(output.resolve().is_relative_to(self.int_root))
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (512, 320), "blue").save(output)
            peer.invoke.return_value = {"request_id": str(prep.pk), "outputs": [{"node_id": "7",
                "filename": output.name, "subfolder": str(prefix.parent), "type": "output"}]}
            result = cycle()
            self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual([call.args[1] for call in peer.invoke.call_args_list], ["submit", "poll", "collect"])
        before = (Asset.objects.count(), AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        replay = self.api("post", "/api/v1/asset-generation-jobs/", body, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay["id"], str(job.pk))
        self.assertEqual((Asset.objects.count(), AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)
        version = AssetReferenceVersion.objects.select_related("asset").get(generation_job=job)
        self.assertEqual(version.source, "generated")
        self.assertEqual(Path(version.asset.file.path).read_bytes(), output.read_bytes())
        return version, prep, upload.files

    def raw_inputs(self, unit_key):
        return {"request.subject": {"kind": "literal", "value": "Keep the shape and change the coat to orange."},
            "basis": {"selection": {"source": "asset_library", "items": [
                {"kind": "asset", "item_key": "basis", "asset_id": str(self.existing.pk), "purpose": None}]}},
            "units": {"kind": "literal", "value": {"schema_version": 1, "ordered_units": [{
                "unit_key": unit_key, "duration_ms": 1000, "subject_keys": [], "structured_input": {"direction": ""},
                "dialogue": [], "voice_profiles": [], "continuity_notes": "", "requires_first_frame_continuity": False,
                "requires_last_frame_continuity": False, "ambient_sound": ""}], "color_script": "", "extra_direction": "",
                "overall_soundscape": "", "non_diegetic_music": ""}}}

    def test_same_generated_asset_and_exact_instance_cross_all_three_entrances(self):
        original, _, _ = self.independent("int-source", self.primary,
            {"request.subject": {"kind": "literal", "value": "A blue coat on a plain background."}})
        self.existing = original.asset
        original_fact = deepcopy(Asset.objects.values().get(pk=self.existing.pk))
        original_metadata = deepcopy(self.existing.metadata)
        original_bytes = Path(self.existing.file.path).read_bytes()
        self.assertFalse(ProductionRun.objects.exists())
        _, independent_prep, independent_uploads = self.independent("int-library-consumer", self.variant, self.raw_inputs("asset"))
        self.assertFalse(ProductionRun.objects.exists())

        # Formal recipe authoring and material fulfillment reuse the governed
        # Asset. Only exact reference versions are new; the Asset is not copied.
        self.sample = self.int_sample(mode="mixed")
        self.url = f"/api/v1/production-runs/{self.sample.run.pk}/"
        self.advance()
        self.approve("bible_review")
        self.fulfill_primaries()
        for _ in range(8):
            run = self.advance()
            if run.current_stage_key == "bible_image_review":
                break
            task = claim_next_task()
            self.assertIsNotNone(task, self.state())
            self.collect(task)
        else:
            self.fail(self.state())
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual({row["kind"] for row in self.submissions}, {"image"})
        self.assertEqual(run.status, "awaiting_approval")
        recipe_preps = list(ExecutionPreparation.objects.filter(stage_run__production_run=run, execution_instance_version=self.variant[0]))
        self.assertEqual(len(recipe_preps), 2)

        # The manual entrance creates its own governed Run, not a hidden
        # recipe or an independent-job proxy. Bible-image scope avoids videos.
        project = SeriesProject.objects.create(owner=self.actor, title="INT manual consumer", **PROJECT_CONFIG)
        prop = PropBible.objects.create(project=project, slug="coat", name="同一件外套", description="A coat")
        catalog = production_stage_catalog()
        manual = self.api("post", "/api/v1/production-runs/", {"project_id": str(project.pk), "plan_source": "manual",
            "idempotency_key": "int-manual-consumer", "allow_external_calls": True, "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, status=201)
        manual_run = ProductionRun.objects.get(pk=manual["id"])
        unit_key = f"prop:{prop.pk}"
        body = {"action": "generate", "stage_key": "bible_image_generation", "unit_key": unit_key,
            "scope": {"kind": "prop", "id": str(prop.pk)}, "expected_run_plan_sha256": manual_run.execution_plan.definition_sha256,
            "expected_selection_revision": None, "idempotency_key": "int-manual-image",
            "payload": self.request(self.variant, self.raw_inputs(unit_key))}
        action_url = f"/api/v1/production-runs/{manual_run.pk}/manual-actions/"
        created = self.api("post", action_url, body, status=202)
        manual_prep = ExecutionPreparation.objects.get(pk=created["target_id"])
        manual_prep = finalize_execution(preparation_id=manual_prep.pk, expected_preparation_revision=manual_prep.revision,
            actor=self.actor, collect_admission_facts=production_observations)
        attempt = dispatch_production_execution(preparation_id=manual_prep.pk,
            expected_preparation_revision=manual_prep.revision, actor=self.actor)
        task = claim_next_task()
        self.assertEqual(task.pk, attempt.generation_task_id)
        self.collect(task)
        self.assertEqual(self.api("post", action_url, body)["target_id"], str(manual_prep.pk))
        self.assertEqual(manual_run.recipe_version_id, None)

        for prep in [independent_prep, manual_prep, *recipe_preps]:
            self.assertEqual(prep.execution_instance_version_id, self.variant[0].pk)
            self.assertIn(self.existing.checksum, str(prep.contract.resolved_inputs))
            self.assertIn(str(self.existing.pk), str(prep.contract.resolved_inputs))
            self.assertEqual(prep.contract.intent["request"]["definition_sha256"], self.variant[1]["sha256"])
        for uploads in [independent_uploads, *[row["uploads"] for row in self.submissions]]:
            self.assertEqual(len(uploads), 1)
            self.assertEqual(next(iter(uploads.values())), original_bytes)
        self.assertEqual(sha256(original_bytes).hexdigest(), self.existing.checksum)
        self.assertEqual(Asset.objects.values().get(pk=self.existing.pk), original_fact)
        source_after = Asset.objects.get(pk=self.existing.pk)
        self.assertEqual(source_after.metadata, original_metadata)
        self.assertEqual(Path(source_after.file.path).read_bytes(), original_bytes)
        self.assertEqual(Asset.objects.filter(checksum=self.existing.checksum).count(), 2)  # explicit peer outputs share blue bytes
        self.assertEqual(ProductionRun.objects.count(), 2)
        self.assertEqual(AssetGenerationJob.objects.count(), 2)
        self.assertEqual(GenerationAttempt.objects.count(), 3)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=manual_run).count(), 1)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__stage_key="media_generation").exists())
