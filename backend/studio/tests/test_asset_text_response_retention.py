"""M7-05 shared retained text on the original asset worker; no live calls."""
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, ExecutionTextResponseReview, GenerationAttempt
from studio.services.asset_generation import process_text_asset_job, claim_next_text_asset_job
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response, retain_asset_text_response
from studio.services.provider_registry import ProviderDriverError
from studio.tests import test_execution_asset_text_worker as worker


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class AssetTextResponseRetentionTests(TestCase):
    setUpTestData = classmethod(worker.ExecutionAssetTextWorkerTests.setUpTestData.__func__)
    setUp = worker.ExecutionAssetTextWorkerTests.setUp
    version = worker.ExecutionAssetTextWorkerTests.version
    invoke = worker.ExecutionAssetTextWorkerTests.invoke

    def detail(self, actor=None):
        client = APIClient()
        client.force_authenticate(actor or self.actor)
        return client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/")

    def test_success_keeps_exact_original_and_existing_candidate_separate_read_only(self):
        text = "  Actual original\r\nBonjour。  "
        self.driver.invoke.return_value["text"] = text
        result = self.invoke()
        self.assertEqual(result.status, "succeeded")
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        self.assertIsNone(row.attempt_id)
        self.assertIsNone(row.confirmed_at)
        self.assertIsNone(row.corrected_file)
        self.assertEqual(read_retained_text_response(row)[0], text)
        self.assertEqual(row.original_size, len(text.encode("utf-8")))
        with CaptureQueriesContext(connection) as queries:
            response = self.detail()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["output"]["state"], "available")
        self.assertEqual(response.data["output"]["text"], text)
        self.assertEqual(response.data["output"]["result"]["candidate_text"], result.result_text)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(Asset.objects.exists())
        if connection.vendor == "sqlite":
            self.assertIn("INDEX", ExecutionTextResponseReview.objects.filter(asset_job_id=result.pk).explain().upper())
        with patch("studio.services.asset_generation.driver_for", return_value=self.driver):
            process_text_asset_job(self.claim)
        self.driver.invoke.assert_called_once()
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)

    def test_rejected_actual_text_is_not_candidate_and_remains_visible(self):
        text = "实际返回\x00未通过接口校验"
        self.driver.invoke.return_value["text"] = text
        result = self.invoke()
        self.assertEqual((result.status, result.error_code, result.result_text), ("failed", "asset_text_response_invalid", ""))
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        self.assertEqual(row.original_text, text)
        self.assertEqual(row.diagnostics["contract_error_code"], "response_contract_invalid")
        response = self.detail()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["output"]["text"], text)
        self.assertEqual(response.data["output"]["media"], [])
        self.assertIn("只读", response.data["output"]["notes"][0])
        self.assertNotIn("candidate_text", response.data["output"]["result"])
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(claim_next_text_asset_job("not-a-retry"))
        self.driver.invoke.assert_called_once()

    def test_empty_received_text_and_missing_text_are_not_conflated(self):
        self.driver.invoke.return_value["text"] = ""
        result = self.invoke()
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        self.assertEqual((row.original_size, row.original_text), (0, ""))
        self.assertEqual(self.detail().data["output"]["state"], "available")
        self.assertEqual(result.status, "failed")

    def test_missing_text_never_fabricates_retained_output_from_error(self):
        self.driver.invoke.return_value = {"request_id": "actual-no-body"}
        result = self.invoke()
        self.assertEqual(result.status, "failed")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        detail = self.detail().data
        self.assertEqual(detail["output"]["state"], "unavailable")
        self.assertEqual(detail["output"]["text"], "")
        self.assertIn("不根据错误消息补造", detail["output"]["notes"][0])

    def budget_error(self):
        error = ProviderDriverError("isolated length stop", code="provider_response_invalid")
        error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length",
            "usage": {"total_tokens": 9}, "secret": "not-for-diagnostics"}
        error.partial_text = "  Actual partial\r\n未完成的句子"
        return error

    def test_known_length_stop_retains_exact_partial_body_without_candidate_or_retry(self):
        error = self.budget_error()
        self.driver.invoke.side_effect = error
        result = self.invoke()
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        self.assertEqual((result.status, result.result_text, result.output_asset_id), ("failed", "", None))
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(original, error.partial_text)
        self.assertEqual(diagnostics["contract_error_code"], "response_output_budget_exhausted")
        self.assertNotIn("secret", diagnostics)
        self.assertEqual(self.detail().data["output"]["text"], error.partial_text)
        self.assertNotIn("candidate_text", self.detail().data["output"]["result"])
        self.assertIsNone(result.actual_cost)
        self.assertIsNone(claim_next_text_asset_job("no-automatic-retry"))
        self.driver.invoke.assert_called_once()

    def test_uncertain_or_non_body_provider_errors_cannot_create_original_text(self):
        for code, uncertain in (("response_output_budget_exhausted", True),
                ("request_text_capacity_rejected", False), ("response_content_filtered", False)):
            with self.subTest(code=code, uncertain=uncertain), transaction.atomic():
                error = self.budget_error()
                error.metadata["contract_error_code"] = code
                error.submission_uncertain = uncertain
                self.driver.invoke.side_effect = error
                result = self.invoke()
                self.assertEqual(result.status, "uncertain" if uncertain else "failed")
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertEqual(result.result_text, "")
                transaction.set_rollback(True)

    def test_partial_body_storage_failure_does_not_publish_candidate(self):
        self.driver.invoke.side_effect = self.budget_error()
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated failed store")):
            result = self.invoke()
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertEqual(result.result_text, "")
        self.driver.invoke.assert_called_once()

    def test_cancelled_late_partial_body_is_not_retained_as_active_output(self):
        from studio.services.asset_generation import request_asset_generation_cancel
        def late_error(*args, **kwargs):
            request_asset_generation_cancel(self.claim)
            raise self.budget_error()
        self.driver.invoke.side_effect = late_error
        result = self.invoke()
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertEqual(result.result_text, "")

    def test_storage_failure_has_no_candidate_no_retention_and_no_second_call(self):
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated failed store")):
            result = self.invoke()
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertEqual(result.result_text, "")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertFalse(Asset.objects.exists())
        self.driver.invoke.assert_called_once()

    def test_corrupt_file_is_explicit_error_not_result_text_fallback(self):
        self.invoke()
        with patch("studio.services.execution_response_storage.read_response", side_effect=ValidationError("isolated corrupt file")):
            response = self.detail()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["output"]["state"], "error")
        self.assertEqual(response.data["output"]["text"], "")
        self.assertEqual(response.data["validation"]["error_code"], "task_center_output_invalid")

    def test_owner_constraint_immutability_and_foreign_visibility(self):
        from django.contrib.auth import get_user_model
        result = self.invoke()
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        row.asset_job_id = uuid4()
        with self.assertRaises(ValidationError): row.save()
        with self.assertRaises(ValidationError):
            ExecutionTextResponseReview.objects.filter(pk=row.pk).update(original_sha256="0" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionTextResponseReview.objects.create(original_file="not-written", original_size=0,
                original_sha256="0" * 64, diagnostics={})
        outsider = get_user_model().objects.create_user(username="asset-response-outsider")
        self.assertEqual(self.detail(actor=outsider).status_code, 404)

    def test_lost_claim_does_not_store_and_failed_row_insert_removes_only_its_new_file(self):
        self.claim = claim_next_text_asset_job("isolated-claim")
        self.assertIsNotNone(self.claim)
        # Original worker submission gate is separately exercised by invoke.
        from studio.services.execution_asset_dispatch import begin_asset_execution_submission
        permitted, self.claim = begin_asset_execution_submission(self.claim)
        self.assertTrue(permitted)
        kwargs = {"job": self.claim, "preparation_id": self.prep.pk, "request_sha256": self.prep.request_sha256,
            "text": "actual test response", "diagnostics": {"schema_version": 1, "summary": "isolated"}}
        stale = AssetGenerationJob.objects.get(pk=self.claim.pk)
        stale.claim_token = uuid4()
        self.assertFalse(retain_asset_text_response(**{**kwargs, "job": stale}))
        before = set(Path(self.media).rglob("*"))
        with patch.object(ExecutionTextResponseReview, "save", side_effect=ValidationError("isolated row failure")):
            with self.assertRaises(ValidationError): retain_asset_text_response(**kwargs)
        self.assertEqual({path for path in Path(self.media).rglob("*") if path.is_file()},
            {path for path in before if path.is_file()})
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
