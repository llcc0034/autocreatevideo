"""Original REST production queue and private receipts; no live peers or data."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt, ProductionRun
from studio.services.async_media_queue import claim_next_async_media_attempt, process_async_media_attempt, recover_stale_async_media_attempts
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response, describe_text_response_review
from studio.services.provider_registry import RestrictedAsyncRestDriver
from studio.tests import test_execution_production_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProductionRestCollectReceiptTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionMediaTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ProductionMediaTests.write_contract)
    job_row = fixtures.ProductionMediaTests.job_row
    resolve = fixtures.ProductionMediaTests.resolve
    submit = fixtures.ProductionMediaTests.submit
    image_attempt = fixtures.ProductionMediaTests.image_attempt
    image_collect_claim = fixtures.ProductionMediaTests.image_collect_claim
    download_image = staticmethod(fixtures.ProductionMediaTests.download_image)

    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No real network")))
        fixtures.ProductionMediaTests.setUp(self)
        self.preparation, self.collecting = self.image_collect_claim()
        self.peer = Mock()
        self.enterContext(patch("studio.services.async_media_queue.driver_for", return_value=self.peer))
        self.download = self.enterContext(patch("studio.services.remote_artifacts._download_one", side_effect=self.download_image))
        self.receipt = {"request_id": "remote-fixture", "artifacts": [{"kind": "image", "url": "https://cdn.example.com/isolated.png"}],
            "actual_cost": "0.02", "currency": "USD", "extra": {"secret": "private-value", "filename": "private.png"}}
        self.peer.invoke.return_value = self.receipt
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def cycle(self, claimed=None):
        return process_async_media_attempt(claimed or self.collecting)

    def stored(self):
        row = ExecutionTextResponseReview.objects.get(attempt=self.collecting)
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(diagnostics["response_source"], "async_rest_collect")
        self.assertEqual(diagnostics["provider_request_id"], "remote-fixture")
        self.assertEqual(diagnostics["preparation_id"], str(self.preparation.pk))
        self.assertIsNone(row.confirmed_at)
        return row, json.loads(original)

    def detail(self):
        return self.client.get(f"/api/v1/task-center/preparation/{self.preparation.pk}/", {"attempt_id": str(self.collecting.pk)})

    def test_original_driver_success_keeps_raw_response_and_real_media_once(self):
        self.peer.invoke.side_effect = RestrictedAsyncRestDriver().invoke
        body = {"status": self.provider.driver_config["terminal_success"][0], **deepcopy(self.receipt)}
        body.pop("request_id")
        with patch("studio.services.provider_registry._request_json", return_value=body) as request:
            result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        row, receipt = self.stored()
        self.assertEqual(receipt["source_response"], body)
        self.assertNotIn("request_id", body)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual(artifact.status, "available")
        self.assertTrue((Path(self.media_root) / artifact.object_key).is_file())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        with CaptureQueriesContext(connection) as queries:
            response = self.detail()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["output"]["media"]), 1)
        for private in ("private-value", "private.png", "cdn.example.com"):
            self.assertNotIn(private, response.data["output"]["text"])
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertIsNone(describe_text_response_review(self.preparation))
        self.assertIsNone(claim_next_async_media_attempt("no-repeat"))
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).pk, row.pk)
        lookup = ExecutionTextResponseReview.objects.filter(attempt=result).order_by("pk")[:1]
        self.assertNotIn("SELECT *", str(lookup.query).upper())
        self.assertIn("USING INDEX", lookup.explain().upper())
        self.client.force_authenticate(get_user_model().objects.create_user(username="unrelated-rest-reader"))
        self.assertEqual(self.detail().status_code, 404)

    def test_driver_rejected_body_is_saved_before_artifact_validation(self):
        self.peer.invoke.side_effect = RestrictedAsyncRestDriver().invoke
        for artifacts in (None, [], [4], [{"kind": "image"}]):
            body = {"status": "succeeded", "artifacts": artifacts, "note": "完整坏回执"}
            with self.subTest(artifacts=artifacts), transaction.atomic(), patch(
                    "studio.services.provider_registry._request_json", return_value=body):
                result = self.cycle()
                self.assertEqual((result.status, result.error_code), ("failed", "provider_response_invalid"), result.error_message)
                _, receipt = self.stored()
                self.assertEqual(receipt, {"schema_version": 1, "request_id": "remote-fixture", "source_response": body})
                self.assertIsNone(result.actual_cost)
                self.assertFalse(GeneratedArtifact.objects.exists())
                self.assertEqual(self.detail().status_code, 200)
                transaction.set_rollback(True)
        self.download.assert_not_called()

    def test_invalid_manifest_preserves_receipt_and_known_bill_not_success(self):
        self.peer.invoke.return_value = {**self.receipt, "artifacts": [4]}
        result = self.cycle()
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertEqual(result.actual_cost, Decimal("0.02"))
        self.stored()
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.download.assert_not_called()

    def test_missing_wrong_identity_and_nonjson_never_invent_receipt(self):
        for receipt in ({**self.receipt, "request_id": None}, {**self.receipt, "request_id": str(uuid4())},
                {**self.receipt, "bad": float("nan")}):
            with self.subTest(receipt=receipt), transaction.atomic():
                self.peer.invoke.return_value = receipt
                result = self.cycle()
                self.assertEqual(result.status, "failed")
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(GeneratedArtifact.objects.exists())
                transaction.set_rollback(True)
        self.download.assert_not_called()

    def test_store_failure_explicitly_stops_without_recollect(self):
        with patch("studio.services.execution_response_storage.store_response", side_effect=ExecutionResponseStorageError("disk fixture")):
            result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.run.refresh_from_db()
        self.assertEqual((result.actual_cost, self.run.actual_cost_total, self.run.committed_cost_total),
            (Decimal("0.02"), Decimal("0.02"), Decimal("0")))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.download.assert_not_called()
        self.assertIsNone(claim_next_async_media_attempt("no-repeat"))

    def test_cancelled_call_with_invalid_bill_stays_cancelled_and_cost_unknown(self):
        for costs in ({"actual_cost": "invalid"}, {"currency": "EUR"}):
            with self.subTest(costs=costs), transaction.atomic():
                def collect(*_):
                    GenerationAttempt.objects.filter(pk=self.collecting.pk).update(status="cancel_requested", cancel_requested_at=timezone.now())
                    return {**self.receipt, **costs}
                self.peer.invoke.side_effect = collect
                result = self.cycle()
                self.assertEqual(result.status, "cancelled", result.error_message)
                self.assertIn(result.error_code, {"actual_cost_invalid", "actual_cost_currency_mismatch"})
                self.assertIsNone(result.actual_cost)
                self.run.refresh_from_db()
                self.assertEqual(self.run.actual_cost_total, Decimal("0"))
                self.assertGreater(self.run.committed_cost_total, Decimal("0"))
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(GeneratedArtifact.objects.exists())
                transaction.set_rollback(True)
        self.download.assert_not_called()

    def test_cancel_or_lost_claim_during_collect_cannot_attach_late_receipt(self):
        for action in ("run_cancel", "attempt_cancel", "claim"):
            with self.subTest(action=action), transaction.atomic():
                def collect(*_):
                    if action == "run_cancel":
                        ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
                    elif action == "attempt_cancel":
                        GenerationAttempt.objects.filter(pk=self.collecting.pk).update(status="cancel_requested", cancel_requested_at=timezone.now())
                    else:
                        GenerationAttempt.objects.filter(pk=self.collecting.pk).update(claim_token=uuid4())
                    return deepcopy(self.receipt)
                self.peer.invoke.side_effect = collect
                result = self.cycle()
                self.assertEqual(result.status, "submitted" if action == "claim" else "cancelled")
                self.assertEqual(result.actual_cost, None if action == "claim" else Decimal("0.02"))
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(GeneratedArtifact.objects.exists())
                transaction.set_rollback(True)
        self.download.assert_not_called()

    def recover(self):
        GenerationAttempt.objects.filter(pk=self.collecting.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(recover_stale_async_media_attempts(minutes=1)["recovered"], 1)
        result = claim_next_async_media_attempt("receipt-recovery")
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.collecting.pk)
        self.peer.reset_mock()
        self.peer.invoke.side_effect = AssertionError("Never recollect or resubmit")
        return result

    def test_archive_crash_recovery_reuses_receipt_and_bill_without_network(self):
        self.download.side_effect = SystemExit("isolated archive crash")
        with self.assertRaises(SystemExit):
            self.cycle()
        row, original = self.stored()
        self.assertEqual(original, self.receipt)
        claim = self.recover()
        self.download.side_effect = self.download_image
        result = self.cycle(claim)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).pk, row.pk)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.actual_cost_total, Decimal("0.02"))
        self.peer.invoke.assert_not_called()

    def test_lost_receipt_file_after_crash_is_explicit_no_recollect(self):
        self.download.side_effect = SystemExit("isolated archive crash")
        with self.assertRaises(SystemExit):
            self.cycle()
        self.stored()
        claim = self.recover()
        with patch("studio.services.execution_response_storage.read_response", side_effect=ExecutionResponseStorageError("missing fixture")):
            result = self.cycle(claim)
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.peer.invoke.assert_not_called()

    def test_missing_bill_stays_unknown_and_receipt_reader_rejects_identity_drift(self):
        self.peer.invoke.return_value = {key: value for key, value in self.receipt.items() if key not in {"actual_cost", "currency"}}
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertIsNone(result.actual_cost)
        row, _ = self.stored()
        GenerationAttempt.objects.filter(pk=result.pk).update(provider_request_id="different-call")
        row = ExecutionTextResponseReview.objects.get(pk=row.pk)
        with self.assertRaises(ValidationError):
            read_retained_text_response(row)
