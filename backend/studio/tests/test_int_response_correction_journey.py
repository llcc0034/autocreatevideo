"""INT F09: authored script, retained response and explicit correction.

Only in-memory SQLite/temporary media. Provider response and admission are
declared isolated peers; original author, worker, storage and correction run.
Correction creates a preview candidate, never adopts it as the Run's script.
"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase, override_settings

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation,
    ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt, ScriptRevision)
from studio.services.script_semantic_candidates import read_script_semantic_candidate
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests import test_production_manual_preparations as manual
from studio.tests import test_script_semantic_native_compilation as author
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class IntegrationResponseCorrectionTests(IntegrationContracts, TestCase):
    setUpTestData = classmethod(manual.ManualPreparationTests.setUpTestData.__func__)
    post = manual.ManualPreparationTests.post
    preparation = manual.ManualPreparationTests.preparation
    select_output = author.ScriptSemanticNativeCompilationTests.select_output

    def make_output(self, layout="a"):
        # A separate source project is not the target Run's adopted script.
        _, output, _, _, raw, self.fixed_text = self.int_script(layout, "mixed")
        return output, raw

    def setUp(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.assertIn("memory", str(connection.settings_dict["NAME"]))
        self.network_guards = [self.enterContext(patch(target, side_effect=AssertionError("INT F09 must stay offline")))
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "requests.sessions.Session.request")]
        manual.ManualPreparationTests.setUp(self)
        self.api_trace = []
        self.versions_url = "/api/v1/model-execution-instance-versions/"
        self.base_definition = self.definition
        self.select_output()
        self.body["payload"].update(execution_instance_version_id=str(self.version.pk),
            expected_definition_sha256=self.definition.sha256)
        for module in ("execution_production_admission", "execution_production_dispatch"):
            self.enterContext(patch(f"studio.services.{module}.collect_production_execution_admission",
                side_effect=production_observations))
        self.api("post", f"/api/v1/production-runs/{self.run.pk}/authorize-cost/",
            {"allow_external_calls": True, "currency": "USD"})

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_authored_response_correction_keeps_original_without_implicit_adoption_or_recall(self):
        prep = self.preparation()
        url = f"/api/v1/execution-preparations/{prep.pk}/"
        self.api("post", url + "continue/", {"expected_preparation_revision": prep.revision})
        claim = claim_next_text_attempt("int-f09")
        self.assertIsNotNone(claim)
        self.assertEqual((claim.preparation_id, claim.role), (prep.pk, "primary"))
        original = ' \r\n{"incomplete": '
        peer = Mock()
        peer.invoke.return_value = {"request_id": "int-f09-response", "text": original}
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            attempt = process_text_attempt(claim)
        self.assertEqual(attempt.status, "awaiting_review", attempt.error_message)
        before = self.api("get", url)
        review = before["text_response_review"]
        self.assertEqual(before["presentation"]["kind"], "primary")
        self.assertIsNone(before["presentation"]["parent"])
        self.assertEqual(before["presentation"]["task"]["status"], "awaiting_review")
        self.assertTrue(review["editable"])
        self.assertEqual(review["attempt_id"], str(attempt.pk))
        self.assertIsNone(review["asset_job_id"])
        self.assertEqual(review["original_text"], original)
        output = self.api("get", f"/api/v1/task-center/preparation/{prep.pk}/")["output"]
        self.assertEqual((output["state"], output["text"]), ("awaiting_review", original))
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=attempt).exists())
        prep.refresh_from_db()
        frozen = deepcopy(prep.contract.prepared_request)
        identity = (prep.request_sha256, attempt.request_snapshot_key)
        revision_count = ScriptRevision.objects.count()
        payload = {"expected_preparation_revision": before["revision"], "review_id": review["id"],
            "original_sha256": review["original_sha256"], "text": self.fixed_text}
        with patch("studio.services.execution_production_text_worker.driver_for",
                side_effect=AssertionError("correction/replay must not call a model")):
            fixed = self.api("post", url + "complete-text-response/", payload)
            self.assertEqual(fixed["revision"], before["revision"] + 1)
            self.assertEqual(fixed["text_response_review"]["corrected_text"], self.fixed_text)
            self.assertFalse(fixed["text_response_review"]["editable"])
            models = (ExecutionPreparation, GenerationAttempt, GeneratedArtifact, ScriptRevision)
            facts = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
            repeated = self.post()
            self.assertEqual(repeated.status_code, 200, repeated.data)
            self.assertEqual(str(repeated.data["target_id"]), str(prep.pk))
            with patch("studio.services.execution_response_storage.store_response",
                    side_effect=AssertionError("no duplicate response storage")), patch(
                    "studio.services.execution_production_text_worker._candidate",
                    side_effect=AssertionError("no duplicate candidate")):
                replay = self.api("post", url + "complete-text-response/", payload)
                self.assertEqual(replay["revision"], fixed["revision"])
            process_text_attempt(claim)
            self.assertEqual(facts, {model.__name__: list(model.objects.order_by("pk").values()) for model in models})
        peer.invoke.assert_called_once()
        attempt.refresh_from_db()
        prep.refresh_from_db()
        row = ExecutionTextResponseReview.objects.get(attempt=attempt)
        self.assertEqual(attempt.status, "succeeded")
        self.assertEqual((row.original_text, row.corrected_text), (original, self.fixed_text))
        self.assertEqual(row.confirmed_by_id, self.actor.pk)
        self.assertEqual((prep.request_sha256, attempt.request_snapshot_key), identity)
        self.assertEqual(prep.contract.prepared_request, frozen)
        artifact = GeneratedArtifact.objects.get(generation_attempt=attempt)
        detail = read_script_semantic_candidate(artifact, max_bytes=1_000_000)
        self.assertEqual(detail["output_contract"], self.output)
        self.assertEqual(detail["candidate"], self.raw)
        self.assertEqual(detail["text"], self.fixed_text)
        self.assertEqual(detail["human_response_recovery"]["corrected_sha256"], row.corrected_sha256)
        revision = ScriptRevision.objects.get(origin_generation_attempt=attempt)
        self.assertEqual((revision.source.source_type, revision.status, artifact.status), ("paste", "preview", "available"))
        self.assertIsNone(revision.confirmed_at)
        self.assertEqual(ScriptRevision.objects.count(), revision_count + 1)
        self.run.refresh_from_db()
        self.project.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertEqual(self.project.master_script, "")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run__production_run=self.run).exists())
