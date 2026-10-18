"""Recovery commands -> original worker -> exact task evidence, in memory only.

These are isolated protocol replies, not real model, Smoke or user acceptance.
No status fixtures turn an unknown/cancelled call into a successful result.
"""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from studio.models import (
    ApprovalDecision, ArtifactSelectionDecision, AssetGenerationJob,
    ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt,
)
from studio.services.provider_registry import ProviderDriverError
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_m7_diagnostic_stages as diagnostics
from studio.tests import test_production_manual_retry as manual


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualRecoveryJourneyTests(diagnostics.IsolatedDiagnosticReads, TestCase):
    setUpTestData = manual.ManualRetryTests.__dict__["setUpTestData"]
    profile_instance = manual.ManualRetryTests.profile_instance
    post = manual.ManualRetryTests.post
    preparation = manual.ManualRetryTests.preparation
    pass_smoke = manual.ManualRetryTests.pass_smoke
    advance = manual.ManualRetryTests.advance
    response = manual.ManualRetryTests.response
    process = manual.ManualRetryTests.process
    failed = manual.ManualRetryTests.failed
    retry_body = manual.ManualRetryTests.retry_body
    policy = manual.ManualRetryTests.policy

    def setUp(self):
        manual.ManualRetryTests.setUp(self)
        # Only synthetic admission is accepted here. The real credential resolver
        # stays denied by IsolatedDiagnosticReads; no host environment is read.
        self.enterContext(patch(
            "studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials",
            return_value=None))

    def retry(self):
        preparation, failed = self.failed()
        original = self.evidence(preparation, failed)
        body = self.retry_body(preparation, failed)
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        successor = GenerationAttempt.objects.get(retry_of=failed)
        self.assertEqual(successor.request_snapshot, failed.request_snapshot)
        self.assertEqual(successor.request_sha256, failed.request_sha256)
        self.assertEqual(successor.preparation_id, preparation.pk)
        return preparation, failed, successor, body, original

    def cancel(self, preparation):
        preparation.refresh_from_db()
        body = {"expected_preparation_revision": preparation.revision}
        path = f"/api/v1/execution-preparations/{preparation.pk}/cancel/"
        response = self.client.post(path, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.client.post(path, body, format="json").status_code, 200)
        return response

    def no_extra_call_or_adoption(self, failed, successor):
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 2)
        self.assertEqual(GenerationAttempt.objects.get(retry_of=failed).pk, successor.pk)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=successor).exists())
        self.assertIsNone(claim_next_text_attempt("no-third-call"))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.stage).exists())

    def test_success_after_retry_keeps_failed_detail_and_replays_only_the_command(self):
        prep, failed, successor, body, original = self.retry()
        result, peer = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.assertEqual((result.pk, result.status), (successor.pk, "succeeded"))
        peer.invoke.assert_called_once()
        artifacts = list(GeneratedArtifact.objects.filter(production_run=self.run).values())
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(self.post(body).status_code, 200)
        # Another key targeting the same old failure cannot fork its retry chain.
        self.assertEqual(self.post({**body, "idempotency_key": "lost-receipt-new-key"}).status_code, 202)
        old = self.evidence(prep, failed)
        current = self.evidence(prep, successor)
        self.assertEqual((old["status"], current["status"]), ("failed", "succeeded"))
        self.assertEqual(old["input"], original["input"])
        self.assertEqual(old["output_count"], 0)
        self.assertEqual(current["output_count"], 1)
        self.assertEqual(list(GeneratedArtifact.objects.filter(production_run=self.run).values()), artifacts)
        self.no_extra_call_or_adoption(failed, successor)

    def test_cancelled_retry_claim_cannot_invoke_and_old_command_does_not_reopen_it(self):
        prep, failed, successor, body, original = self.retry()
        claim = claim_next_text_attempt("cancel-claimed-retry")
        self.assertEqual(claim.pk, successor.pk)
        self.cancel(prep)
        peer = Mock()
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claim)
        peer.invoke.assert_not_called()
        self.assertEqual((result.status, result.error_code), ("cancelled", "execution_cancelled_before_submit"))
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post({**body, "idempotency_key": "new-command-after-stop"}).status_code, 400)
        stopped = self.evidence(prep, successor)
        self.assertEqual((stopped["preparation_status"], stopped["status"], stopped["output_count"]),
            ("cancelled", "cancelled", 0))
        self.assertIsNone(stopped["submission"]["started_at"])
        self.assertEqual(self.evidence(prep, failed)["input"], original["input"])
        self.no_extra_call_or_adoption(failed, successor)

    def test_resource_confirmation_requires_new_explicit_retry_and_preserves_frozen_input(self):
        prep, failed = self.failed()
        original = self.evidence(prep, failed)
        self.policy(calls=1)
        body = self.retry_body(prep, failed, "resource-blocked-command")
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport",
                side_effect=AssertionError("Retry must not recompile")) as compile_guard, patch(
                "studio.services.execution_parameter_preparation.randbelow",
                side_effect=AssertionError("Retry must not redraw seed")) as seed_guard:
            blocked = self.post(body)
            self.assertEqual(blocked.status_code, 202, blocked.data)
            self.assertEqual(blocked.data["target_id"], str(prep.pk))
            prep.refresh_from_db()
            self.assertEqual(prep.status, "dispatched")
            self.assertEqual(prep.contract.evaluation["status"], "blocked")
            self.assertEqual(prep.reason_code, "execution_resource_confirmation_required")
            self.assertIsNone(prep.next_check_at)
            review = prep.contract.evaluation["root_checks"]["resource_review"]
            self.assertIn("call_count_exceeded", [item["code"] for item in review["issues"]])
            self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)
            self.assertIsNone(claim_next_text_attempt("blocked-not-submitted"))
            confirmed = self.client.post(f"/api/v1/execution-preparations/{prep.pk}/confirm-resources/", {
                "expected_preparation_revision": prep.revision, "fingerprint": review["fingerprint"],
            }, format="json")
            self.assertEqual(confirmed.status_code, 200, confirmed.data)
            self.assertEqual(self.post(body).status_code, 200)
            self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)
            self.assertIsNone(claim_next_text_attempt("confirmation-not-submission"))
            retried = self.post(self.retry_body(prep, failed, "explicit-after-resource-confirmation"))
            self.assertEqual(retried.status_code, 202, retried.data)
            successor = GenerationAttempt.objects.get(retry_of=failed)
            self.assertEqual(successor.preparation_id, prep.pk)
            self.assertEqual(successor.request_snapshot, failed.request_snapshot)
            self.assertEqual(successor.request_sha256, failed.request_sha256)
            result, peer = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
            self.assertEqual((result.pk, result.status), (successor.pk, "succeeded"))
            peer.invoke.assert_called_once()
            compile_guard.assert_not_called(); seed_guard.assert_not_called()
        self.assertEqual(self.evidence(prep, failed)["input"], original["input"])
        self.assertEqual(self.evidence(prep, failed)["status"], "failed")
        self.assertEqual(self.evidence(prep, successor)["output_count"], 1)
        self.no_extra_call_or_adoption(failed, successor)

    def test_unknown_retry_then_cancel_preserves_unknown_fact_without_a_third_call(self):
        prep, failed, successor, body, _ = self.retry()
        result, peer = self.process(effect=ProviderDriverError("isolated lost response", submission_uncertain=True))
        self.assertEqual((result.status, result.error_code), ("failed", "uncertain_remote_response"))
        page = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(page.status_code, 200, page.data)
        visible_call = page.data["presentation"]["task"]
        self.assertEqual((visible_call["id"], visible_call["status"], visible_call["error_code"]),
                         (str(successor.pk), "failed", "uncertain_remote_response"))
        self.assertIn("不能按明确失败重新提交", page.data["outcome"]["message"])
        self.assertEqual(self.post(body).status_code, 200)
        new = self.retry_body(prep, result, "unknown-is-not-retryable")
        self.assertEqual(self.post(new).status_code, 400)
        self.cancel(prep)
        detail = self.evidence(prep, successor)
        self.assertEqual((detail["preparation_status"], detail["status"]), ("cancelled", "failed"))
        self.assertEqual(detail["validation"]["error_code"], "uncertain_remote_response")
        self.assertIsNotNone(detail["submission"]["started_at"])
        self.assertIsNone(detail["submission"]["registered_at"])
        self.assertEqual(detail["output_count"], 0)
        result.refresh_from_db(); self.run.refresh_from_db()
        self.assertTrue(result.ignore_late_result)
        self.assertIsNone(result.actual_cost)
        self.assertEqual(result.committed_cost, self.run.committed_cost_total)
        peer.invoke.assert_called_once()
        self.no_extra_call_or_adoption(failed, successor)

    def test_cancel_during_retry_response_never_publishes_the_late_success(self):
        prep, failed, successor, body, _ = self.retry()
        response = {**self.response(), "actual_cost": "0.03", "currency": "USD"}
        def late_response(*_):
            self.cancel(prep)
            return deepcopy(response)
        result, peer = self.process(effect=late_response)
        self.assertEqual((result.status, result.error_code), ("cancelled", "late_result_ignored"))
        detail = self.evidence(prep, successor)
        self.assertEqual((detail["status"], detail["output_count"]), ("cancelled", 0))
        self.assertFalse(GeneratedArtifact.objects.filter(production_run=self.run).exists())
        self.assertEqual(result.actual_cost, Decimal("0.03"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.actual_cost_total, Decimal("0.05"))
        self.assertEqual(self.post(body).status_code, 200)
        peer.invoke.assert_called_once()
        self.no_extra_call_or_adoption(failed, successor)

    def test_invalid_returned_text_stays_original_review_not_a_retry_or_adoption(self):
        prep, failed, successor, body, _ = self.retry()
        result, peer = self.process({"text": "isolated invalid response", "actual_cost": "0.03", "currency": "USD"})
        self.assertEqual(result.status, "awaiting_review")
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        original_hash = review.original_sha256
        self.assertEqual(self.post(self.retry_body(prep, result, "review-is-not-failure")).status_code, 400)
        self.assertEqual(self.post(body).status_code, 200)
        detail = self.evidence(prep, successor)
        # output_count pages retained responses too; only outcome/artifact rows
        # represent generated candidates. A retained invalid body is not one.
        self.assertEqual((detail["status"], detail["output_count"]), ("awaiting_review", 1))
        self.assertEqual(detail["outcome"]["artifact_count"], 0)
        self.assertFalse(GeneratedArtifact.objects.filter(production_run=self.run).exists())
        self.assertEqual(detail["output"]["text"], "isolated invalid response")
        review.refresh_from_db()
        self.assertEqual(review.original_sha256, original_hash)
        self.assertIsNone(review.confirmed_at)
        peer.invoke.assert_called_once()
        self.no_extra_call_or_adoption(failed, successor)
