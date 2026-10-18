"""Full stage retry journey on the original APIs; synthetic protocol only."""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GeneratedArtifact, GenerationAttempt
from studio.services.production_runs import advance_production_run
from studio.tests.test_m7_diagnostic_stages import IsolatedDiagnosticReads
from studio.tests import test_production_execution_retry as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class StageRecoveryJourneyTests(IsolatedDiagnosticReads, TestCase):
    setUpTestData = fixtures.ProductionRetryTests.__dict__["setUpTestData"]
    profile_instance = fixtures.ProductionRetryTests.profile_instance
    root = fixtures.ProductionRetryTests.root
    prepare = fixtures.ProductionRetryTests.prepare
    structure_instance = fixtures.ProductionRetryTests.structure_instance
    pass_smoke = fixtures.ProductionRetryTests.pass_smoke
    advance = fixtures.ProductionRetryTests.advance
    response = fixtures.ProductionRetryTests.response
    process = fixtures.ProductionRetryTests.process
    failed_stage = fixtures.ProductionRetryTests.failed_stage

    def setUp(self):
        fixtures.ProductionRetryTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.enterContext(patch(
            "studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials",
            return_value=None))

    def test_full_retry_scope_replay_and_failed_successor_need_explicit_new_target(self):
        failed, good = self.failed_stage()
        path = f"/api/v1/production-runs/{self.run.pk}/"
        frozen = deepcopy(failed.request_snapshot)
        artifacts = list(GeneratedArtifact.objects.values())
        preparations = set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True))
        with CaptureQueriesContext(connection) as queries:
            pages = [self.client.get(path + "retry-preview/", {
                "expected_stage_id": str(self.stage.pk), "page_size": 1, "page": page,
            }) for page in (1, 2)]
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        for page in pages:
            self.assertEqual(page.status_code, 200, page.data)
        rows = {row["attempt_id"]: row for page in pages for row in page.data["results"]}
        self.assertEqual(rows[str(failed.pk)]["disposition"], "retry")
        self.assertEqual(rows[str(good.pk)]["disposition"], "skip")
        original_body = pages[0].data["retry_context"]
        self.assertEqual(original_body["expected_attempt_ids"], [str(failed.pk)])
        for _ in range(2):
            response = self.client.post(path + "retry/", original_body, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        successor = GenerationAttempt.objects.get(retry_of=failed)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 3)
        result, peer = self.process({"text": None, "actual_cost": "0.01", "currency": "USD"})
        peer.invoke.assert_called_once()
        self.assertEqual((result.pk, result.status), (successor.pk, "failed"))
        self.run = advance_production_run(self.run)
        # A replay of the old confirmed set is harmless, not consent for a third call.
        self.assertEqual(self.client.post(path + "retry/", original_body, format="json").status_code, 200)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 3)
        current = self.client.get(path).data["retry_context"]
        self.assertEqual(current["expected_attempt_ids"], [str(successor.pk)])
        self.assertEqual(self.client.post(path + "retry/", current, format="json").status_code, 200)
        final, peer = self.process({**self.response(), "actual_cost": "0.04", "currency": "USD"})
        peer.invoke.assert_called_once()
        self.assertEqual((final.retry_of_id, final.status), (successor.pk, "succeeded"))
        self.assertEqual(final.request_snapshot, frozen)
        self.assertEqual(final.request_sha256, failed.request_sha256)
        self.assertEqual(final.preparation_id, failed.preparation_id)
        self.assertEqual(final.retry_requested_by_id, self.actor.pk)
        self.assertEqual(set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True)), preparations)
        for before in artifacts:
            self.assertEqual(GeneratedArtifact.objects.values().get(pk=before["id"]), before)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=good.preparation).count(), 1)
        for attempt in (failed, successor, final):
            attempt.refresh_from_db()
            detail = self.evidence(failed.preparation, attempt)
            self.assertEqual(detail["status"], attempt.status)
            self.assertEqual(detail["output_count"], 1 if attempt.pk == final.pk else 0)
