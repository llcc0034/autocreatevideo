"""Manual adoption through the real API and a real archived PNG candidate."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, AssemblyJob, Episode, GenerationAttempt, GenerationTask, ProductionAssemblyLink, ProductionManualCommand
from studio.tests import test_execution_production_workflow_artifacts as fixtures
from studio.services.json_contracts import assign_json_contract


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualMediaAdoptionTests(TestCase):
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted
    archive = fixtures.ProductionWorkflowArtifactTests.archive

    def resolve(self, definition, stage):
        stage.mode = "manual"
        stage.save(update_fields=("mode",))
        return fixtures.ProductionWorkflowArtifactTests.resolve(self, definition, stage)

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.artifact, = self.archive()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.detail_url = f"/api/v1/generated-artifacts/{self.artifact.pk}/"
        detail = self.client.get(self.detail_url)
        self.assertEqual(detail.status_code, 200, detail.data)
        candidate = detail.data["manual_media_candidate"]
        self.body = {"action": "adopt", "stage_key": "media_generation", "scope": candidate["scope"],
            "unit_key": candidate["unit_key"], "expected_run_plan_sha256": candidate["run_plan_sha256"],
            "expected_selection_revision": candidate["selection_revision"], "idempotency_key": "adopt-media",
            "payload": {"source": {"kind": "artifact", "artifact_id": str(self.artifact.pk), "expected_sha256": self.artifact.sha256,
                "expected_manifest_sha256": self.artifact.manifest_metadata_sha256}}}
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("Adoption cannot call a model")))

    def post(self, body=None):
        return self.client.post(self.url, self.body if body is None else body, format="json")

    def test_adoption_and_replay_record_one_original_decision_without_advancing_stage(self):
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        first = self.post()
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["result_kind"], "artifact_selection")
        self.artifact.refresh_from_db(); self.stage.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.artifact.status, "selected")
        self.assertEqual(self.stage.status, "running")
        self.assertEqual(self.run.status, "running")
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)
        replay = self.post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["target_id"], first.data["target_id"])
        detail = self.client.get(self.detail_url).data["manual_media_candidate"]
        self.assertEqual(detail["selection_revision"], 1)
        self.assertEqual(detail["selected_artifact_id"], str(self.artifact.pk))
        stale = {**self.body, "idempotency_key": "stale"}
        self.assertEqual(self.post(stale).status_code, 409)
        repeated = self.post({**stale, "expected_selection_revision": 1})
        self.assertEqual(repeated.status_code, 201, repeated.data)
        self.assertEqual(repeated.data["target_id"], first.data["target_id"])
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)

    def test_wrong_scope_hash_unknown_fields_and_missing_candidate_cannot_select(self):
        bodies = [{**self.body, "unit_key": "another-target"}, {**self.body, "expected_selection_revision": None}]
        for key, value in (("expected_sha256", "0" * 64), ("expected_manifest_sha256", "0" * 64),
                ("artifact_id", str(uuid4())), ("workflow_version_id", str(self.workflow.pk))):
            body = deepcopy(self.body); body["payload"]["source"][key] = value; bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                self.assertIn(self.post(body).status_code, (400, 409))
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_real_candidate_bytes_and_source_must_match_before_adoption(self):
        path = Path(settings.MEDIA_ROOT) / self.artifact.object_key
        original = path.read_bytes()
        manifest = deepcopy(self.artifact.manifest_metadata)
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        for bad in (b"", b"broken bytes", None):
            try:
                if bad is None:
                    path.unlink()
                else:
                    path.write_bytes(bad)
                response = self.post()
                self.assertEqual(response.status_code, 400, response.data)
                self.assertFalse(ArtifactSelectionDecision.objects.exists())
                self.assertFalse(ProductionManualCommand.objects.exists())
            finally:
                path.write_bytes(original)
        changed = deepcopy(manifest); changed["request_sha256"] = "0" * 64
        assign_json_contract(self.artifact, "manifest_metadata", changed); self.artifact.save()
        body = deepcopy(self.body)
        body["payload"]["source"]["expected_manifest_sha256"] = self.artifact.manifest_metadata_sha256
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
        assign_json_contract(self.artifact, "manifest_metadata", manifest); self.artifact.save()
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)

    def test_receipt_failure_rolls_back_selection_and_corrupted_state_is_not_repaired(self):
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.post()
        self.artifact.refresh_from_db()
        self.assertEqual(self.artifact.status, "available")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.artifact.status = "selected"
        self.artifact.save(update_fields=("status",))
        self.assertEqual(self.client.get(self.detail_url).status_code, 400)
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_other_owner_and_frozen_assembly_cannot_adopt(self):
        other = get_user_model().objects.create_user(username="other-adoption-owner")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertEqual(self.post().status_code, 404)
        self.client.force_authenticate(self.user)
        episode = Episode.objects.create(project=self.project, number=1, title="Frozen episode")
        job = AssemblyJob.objects.create(owner=self.user, project=self.project, episode=episode, scope="episode", max_attempts=1)
        ProductionAssemblyLink.objects.create(production_run=self.run, stage_run=self.stage, episode=episode, assembly_job=job)
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
