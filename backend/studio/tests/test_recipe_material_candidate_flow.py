"""M3 receipt -> original recipe queue -> archived appearance candidate.

Admission observations, provider replies and HTTP transfer are isolated. Review,
preparation, queue transitions, PNG verification and candidate APIs are real
services on a test database. This is not live Smoke or full M4 acceptance.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (ArtifactSelectionDecision, AssetGenerationJob, ExecutionPreparation,
    GeneratedArtifact, GenerationAttempt, ProductionRun)
from studio.services.async_media_queue import claim_next_async_media_attempt, process_async_media_attempt
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import assign_json_contract
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_routing import require_current_recipe_preparation
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.tests import test_production_recipe_bible_regeneration as fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMaterialCandidateFlowTests(TestCase):
    prepare_initial = False
    freeze_input = False
    setUpTestData = classmethod(fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare = fixtures.RecipeBibleRegenerationTests.prepare

    def setUp(self):
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Isolated recipe candidate tests cannot use the network")))
        directory = self.enterContext(TemporaryDirectory())
        self.media_root = Path(directory)
        self.enterContext(self.settings(MEDIA_ROOT=directory))
        fixtures.RecipeBibleRegenerationTests.setUp(self)
        self.run.allow_external_calls = True  # Only this test row; network remains forbidden.
        self.run.save(update_fields=("allow_external_calls",))
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/projects/{self.project.pk}/bible-image-candidates/"

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)

    @staticmethod
    def download_image(**kwargs):
        path = kwargs["target"]
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (512, 512), "blue").save(path, format="PNG")
        return verified_local_media_facts(path, expected_kind="image", max_bytes=kwargs["max_bytes"])

    def queue_step(self, attempt, operation, response):
        GenerationAttempt.objects.filter(pk=attempt.pk).update(next_poll_at=timezone.now())
        claimed = claim_next_async_media_attempt("isolated-recipe-material-worker")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, attempt.pk)
        peer = Mock()
        peer.invoke.return_value = response
        with patch("studio.services.async_media_queue.driver_for", return_value=peer):
            result = process_async_media_attempt(claimed)
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[1], operation)
        return result

    def submit_candidate(self, number=1):
        preparation = self.prepare(number)
        # The writer locks and freezes its own StageRun instance in the database.
        self.stage.refresh_from_db()
        # Do not replace the original M3/current-route check with an admission fixture.
        require_current_recipe_preparation(preparation, stage=self.stage)
        preparation = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.actor)
        self.assertEqual(preparation.status, "dispatched", preparation.reason)
        attempt = GenerationAttempt.objects.get(preparation=preparation)
        attempt = self.queue_step(attempt, "submit", {
            "request_id": f"isolated-{preparation.pk}", "status": "submitted"})
        self.assertEqual(attempt.status, "submitted", attempt.error_message)
        return preparation, attempt

    def collect_candidate(self, attempt):
        polled = self.queue_step(attempt, "poll", {
            "status": self.provider.driver_config["terminal_success"][0], "terminal_success": True})
        self.assertEqual(polled.status, "submitted", polled.error_message)
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=attempt).exists())
        with patch("studio.services.remote_artifacts._download_one", side_effect=self.download_image) as transfer:
            result = self.queue_step(attempt, "collect", {
                "request_id": attempt.provider_request_id,
                "artifacts": [{"kind": "image", "url": "https://cdn.example.com/isolated.png"}]})
        self.assertEqual(result.status, "succeeded", result.error_message)
        transfer.assert_called_once()
        self.assertIsNone(result.actual_cost)  # No bill was supplied by the isolated reply.
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual(artifact.status, "available")
        self.assertIsNone(artifact.asset_id)
        return artifact

    def history(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        entity, = response.json()["candidates"]
        self.assertEqual(entity["entity_id"], str(self.character.pk))
        return entity["candidate_history"]

    def test_two_candidates_keep_one_appearance_identity_and_original_receipt_without_adoption(self):
        artifacts = []
        unit_key = f"appearance:{self.setting.variant_id}"
        for number in (1, 2):
            preparation, attempt = self.submit_candidate(number)
            frozen = deepcopy(preparation.contract.resolved_inputs)
            artifact = self.collect_candidate(attempt)
            preparation.refresh_from_db()
            artifacts.append(artifact)
            self.assertEqual((preparation.unit_key, attempt.unit_key,
                artifact.manifest_metadata["unit_key"]), (unit_key,) * 3)
            self.assertEqual(artifact.variant_group,
                f"stage:{self.stage.pk}:unit:{sha256(unit_key.encode()).hexdigest()}")
            media = read_production_media_candidate(artifact, max_media_bytes=65536)
            self.assertEqual(media["unit_key"], unit_key)
            self.assertEqual(media["production_unit"]["scope"], self.scope)
            self.assertEqual(media["media_facts"]["sha256"], artifact.sha256)
            with Image.open(media["path"]) as image:
                self.assertEqual(image.size, (512, 512))
            self.stage.refresh_from_db()
            requirement = frozen["recipe_material_requirement"]
            self.assertEqual(requirement["bible_review"], self.stage.input_snapshot["material_requirements"]["bible_review"])
            self.assertEqual(requirement["target"]["setting"], read_media_generation_target(preparation).value["setting"])
            with patch("studio.services.bible_material_requirements.read_bible_material_requirements",
                    side_effect=AssertionError("Successful replay cannot resolve current M3 demand")), patch(
                    "studio.services.execution_parameter_preparation.randbelow",
                    side_effect=AssertionError("Successful replay cannot redraw seed")):
                self.assertEqual(self.prepare(number).pk, preparation.pk)
            preparation.contract.refresh_from_db()
            self.assertEqual(preparation.contract.resolved_inputs, frozen)
            detail = self.client.get(f"/api/v1/execution-preparations/{preparation.pk}/")
            self.assertEqual(detail.status_code, 200, detail.data)
            self.assertEqual(detail.data["resolved_inputs"]["recipe_material_requirement"], requirement)
        history = self.history()
        self.assertEqual({row["artifact_id"] for row in history}, {str(row.pk) for row in artifacts})
        self.assertTrue(all(row["source_current"] and row["integrity_status"] == "valid" and row["preview_url"] for row in history))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(ProductionRun.objects.count(), 1)
        self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
        self.assertEqual(self.stage.reason_code, "waiting_material_selection")
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_setting_edit_after_submit_preserves_archive_history_but_blocks_new_generation(self):
        preparation, attempt = self.submit_candidate()
        frozen = deepcopy(preparation.contract.resolved_inputs)
        revise_variant(actor=self.actor, variant=self.setting.variant, expected_revision=1,
            value={**setting_payload(self.setting), "appearance_anchor": "Unreviewed blue coat"})
        artifact = self.collect_candidate(attempt)
        self.assertEqual(read_production_media_candidate(artifact, max_media_bytes=65536)["preparation_id"], str(preparation.pk))
        candidate, = self.history()
        self.assertFalse(candidate["source_current"])
        self.assertEqual(candidate["integrity_status"], "valid")
        self.assertTrue(candidate["preview_url"])
        self.assertEqual(self.prepare(1).pk, preparation.pk)
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            self.prepare(2)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, frozen)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_unrecognized_primary_request_key_blocks_reallocation_and_resubmission(self):
        preparation, attempt = self.submit_candidate()
        frozen = deepcopy(preparation.contract.prepared_request)
        events = self.stage.route_events.count()
        # Only an isolated corruption fixture: ordinary writes are immutable.
        ExecutionPreparation._base_manager.filter(pk=preparation.pk).update(request_key="unrecognized-primary")
        with self.assertRaises(ValidationError):
            self.prepare(1)
        with self.assertRaises(ValidationError):
            require_current_recipe_preparation(preparation, stage=self.stage)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertEqual(self.stage.route_events.count(), events)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitted")
        preparation.refresh_from_db()
        self.assertEqual(preparation.request_key, "unrecognized-primary")
        self.assertEqual(preparation.contract.prepared_request, frozen)

    def test_changed_manifest_unit_is_not_reinterpreted_as_entity_identity(self):
        preparation, attempt = self.submit_candidate()
        artifact = self.collect_candidate(attempt)
        metadata = deepcopy(artifact.manifest_metadata)
        metadata["unit_key"] = f"character:{self.character.pk}"
        # Keep JSON checksum valid to exercise semantic identity, not only the digest guard.
        assign_json_contract(artifact, "manifest_metadata", metadata)
        artifact.save()
        with self.assertRaisesMessage(ValidationError, "媒体候选清单与实际执行准备不一致"):
            read_production_media_candidate(artifact, max_media_bytes=65536)
        candidate, = self.history()
        self.assertEqual(candidate["integrity_status"], "invalid")
        self.assertEqual((candidate["preview_url"], candidate["download_url"]), ("", ""))
        preparation.refresh_from_db()
        self.assertEqual(preparation.unit_key, f"appearance:{self.setting.variant_id}")
        artifact.refresh_from_db()
        self.assertEqual(artifact.manifest_metadata["unit_key"], metadata["unit_key"])
