"""Original worker over exact published text instances and saved Preparations.

Fake provider responses and isolated admission observations exercise the local
protocol only. These tests make no paid call and do not certify model quality.
"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings
from django.db import transaction
from django.utils import timezone

from studio.models import Asset, AssetGenerationJob
from studio.services.asset_generation import (
    create_asset_generation_job, claim_next_text_asset_job, process_text_asset_job,
    recover_stale_asset_generation_jobs, request_asset_generation_cancel,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import publish_execution_version
from studio.services.provider_registry import ProviderDriverError
from studio.tests import test_execution_text_contracts as fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionAssetTextWorkerTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionTextContractTests.setUpTestData.__func__)
    version = fixtures.ExecutionTextContractTests.version

    def setUp(self):
        self.media = self.enterContext(TemporaryDirectory(prefix="asset-text-worker-"))
        self.enterContext(override_settings(MEDIA_ROOT=self.media))
        fixtures.ExecutionTextContractTests.setUp(self)
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))
        self.instance_version = self.version()
        self.definition = build_execution_definition(self.instance_version.pk)
        publish_execution_version(version_id=self.instance_version.pk, expected_revision=self.instance_version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="text-worker",
            execution_instance_version_id=self.instance_version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="prompt", media_type="video", name="Draft prompt", parameters={"seed": 20},
            inputs={}, manual_channels={"positive_prompt": "Describe a blue parcel.", "system_instruction": "Use only supplied facts."},
            subject_facts=[], source_language="", target_language="", field_values=[])
        self.prep = freeze(self.job, self.actor)
        # This worker fixture isolates dispatch. Root budget and combination
        # admission are exercised by the native dispatch tests.
        self.job.status = "queued"
        self.job.save(update_fields=["status"])
        self.driver = Mock()
        self.driver.invoke.return_value = {"text": "A blue parcel rests on a table.", "request_id": "text-fixture-1"}

    def invoke(self):
        self.claim = claim_next_text_asset_job("isolated-text-worker")
        self.assertIsNotNone(self.claim)
        with patch("studio.services.asset_generation.driver_for", return_value=self.driver):
            return process_text_asset_job(self.claim)

    def test_prompt_draft_reaches_original_worker_with_exact_channels_not_builtin_script_prompt(self):
        before = deepcopy(self.prep.contract.prepared_request)
        result = self.invoke()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result_text, self.driver.invoke.return_value["text"])
        self.assertEqual(result.operation, "prompt.draft")
        self.assertIsNone(result.output_asset_id)
        self.assertEqual(Asset.objects.count(), 0)
        self.driver.invoke.assert_called_once()
        self.assertEqual(self.driver.invoke.call_args.args[1:], ("chat_completion", before["transport"]["payload"]))
        self.assertEqual(self.driver.invoke.call_args.args[2]["messages"], [
            {"role": "system", "content": "Use only supplied facts."}, {"role": "user", "content": "Describe a blue parcel."}])
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.prepared_request, before)
        self.assertIsNone(claim_next_text_asset_job("second-worker"))

    def test_invalid_response_is_not_success_or_automatic_retry(self):
        self.driver.invoke.return_value = {"text": "", "request_id": "invalid-fixture"}
        result = self.invoke()
        self.assertEqual((result.status, result.error_code), ("failed", "asset_text_response_invalid"))
        self.assertEqual(result.remote_status, "response_rejected")
        self.assertEqual(result.provider_request_id, "invalid-fixture")
        self.assertEqual(result.result_text, "")
        self.assertIsNone(result.output_asset_id)
        self.assertEqual(Asset.objects.count(), 0)
        self.assertIsNone(claim_next_text_asset_job("second-worker"))
        self.driver.invoke.assert_called_once()

    def test_overlong_request_identity_is_hashed_not_truncated(self):
        self.driver.invoke.return_value["request_id"] = "r" * 192
        result = self.invoke()
        self.assertEqual((result.status, result.error_code), ("uncertain", "provider_request_id_too_long"))
        self.assertEqual(result.provider_request_id, "")
        self.assertEqual(len(result.provider_request_id_sha256), 64)

    def test_invalid_request_identity_is_not_coerced_into_a_successful_fact(self):
        for invalid in (None, False, 0, 123, [], {"id": "untrusted"}):
            with self.subTest(identity=invalid), transaction.atomic():
                self.driver.invoke.reset_mock()
                self.driver.invoke.return_value["request_id"] = invalid
                result = self.invoke()
                self.assertEqual((result.status, result.error_code), ("uncertain", "invalid_provider_request_id"))
                self.assertEqual((result.provider_request_id, result.provider_request_id_sha256, result.result_text), ("", "", ""))
                self.assertIsNone(result.output_asset_id)
                self.assertIsNone(claim_next_text_asset_job("no-duplicate"))
                self.driver.invoke.assert_called_once()
                transaction.set_rollback(True)

    def test_unknown_submission_result_stays_uncertain(self):
        self.driver.invoke.side_effect = ProviderDriverError("isolated transport disconnect", code="transport_error", submission_uncertain=True)
        self.assertEqual(self.invoke().status, "uncertain")
        self.assertIsNone(claim_next_text_asset_job("second-worker"))

    def receipt(self, result):
        from studio.services.execution_text_response_usage import read_text_response_usage
        return read_text_response_usage(result, preparation_id=self.prep.pk, request_sha256=self.prep.request_sha256)

    def test_usage_is_saved_once_on_original_job_and_visible_without_settling_cost(self):
        from rest_framework.test import APIClient
        self.driver.invoke.return_value["usage"] = {"prompt_tokens": 12, "completion_tokens": 0,
            "total_tokens": 12, "secret": "PRIVATE", "cached_tokens": 8}
        result = self.invoke()
        receipt = result.resolutions.get(outcome="response_received")
        self.assertEqual(receipt.evidence["usage"], {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12})
        self.assertNotIn("PRIVATE", str(receipt.evidence))
        self.assertIsNone(receipt.decided_by)
        self.assertIsNone(result.actual_cost)
        client = APIClient(); client.force_authenticate(self.actor)
        detail = client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["output"]["result"]["provider_response"]["usage"], receipt.evidence["usage"])
        self.assertEqual(detail.data["output"]["result"]["provider_response"]["task_id"], str(result.pk))
        self.assertEqual(detail.data["output"]["state"], "available")
        self.assertIsNone(detail.data["actual_cost"])
        self.assertIn("不是实际扣费", detail.data["output"]["notes"][-1])
        # A retained worker object cannot submit the completed request again.
        with patch("studio.services.asset_generation.driver_for", return_value=self.driver):
            process_text_asset_job(self.claim)
        self.driver.invoke.assert_called_once()
        self.assertEqual(result.resolutions.filter(outcome="response_received").count(), 1)

    def test_usage_survives_parser_rejection_and_invalid_request_identity(self):
        for response, status in (({"text": "", "request_id": "rejected"}, "failed"),
                ({"text": "answer", "request_id": False}, "uncertain")):
            with self.subTest(response=response), transaction.atomic():
                self.driver.invoke.reset_mock()
                self.driver.invoke.return_value = {**response, "usage": {"total_tokens": 24}}
                result = self.invoke()
                self.assertEqual(result.status, status)
                self.assertEqual(self.receipt(result)[0]["usage"], {"total_tokens": 24})
                self.assertEqual(result.result_text, "")
                self.assertIsNone(result.actual_cost)
                self.assertFalse(Asset.objects.exists())
                self.driver.invoke.assert_called_once()
                transaction.set_rollback(True)

    def test_missing_and_invalid_usage_are_unknown_not_zero(self):
        for usage in (None, {}, {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 10 ** 50}):
            with self.subTest(usage=usage), transaction.atomic():
                self.driver.invoke.return_value["usage"] = usage
                result = self.invoke()
                receipt, note = self.receipt(result)
                self.assertEqual(receipt["state"], "recorded")
                self.assertEqual(receipt["usage"], {})
                self.assertIn("未知", note)
                self.assertIsNone(result.actual_cost)
                transaction.set_rollback(True)

    def test_historical_absence_and_foreign_or_damaged_receipt_are_not_repaired(self):
        from studio.models import AssetGenerationJobResolution
        from studio.services.json_contracts import json_contract
        self.assertEqual(self.receipt(self.job)[0]["state"], "unavailable")
        result = self.invoke()
        row = result.resolutions.get(outcome="response_received")
        for values in ({"preparation_id": "foreign"}, {"request_sha256": "0" * 64}, {"schema_version": True}):
            evidence = json_contract({**row.evidence, **values})
            # Isolated corruption fixture, never a production repair path.
            AssetGenerationJobResolution._base_manager.filter(pk=row.pk).update(evidence=evidence.value,
                evidence_size_bytes=evidence.size_bytes, evidence_sha256=evidence.sha256)
            self.assertEqual(self.receipt(result)[0]["state"], "unverifiable")
            self.assertEqual(self.receipt(result)[0]["usage"], {})
        AssetGenerationJobResolution._base_manager.filter(pk=row.pk).update(evidence_sha256="0" * 64)
        self.assertEqual(self.receipt(result)[0]["state"], "unverifiable")
        row.refresh_from_db()
        self.assertEqual(row.evidence_sha256, "0" * 64)
        self.assertEqual(result.status, "succeeded")

    def test_late_cancelled_response_retains_usage_not_candidate(self):
        def late_response(*args, **kwargs):
            request_asset_generation_cancel(self.claim)
            return {"text": "late", "request_id": "late", "usage": {"total_tokens": 9}}
        self.driver.invoke.side_effect = late_response
        result = self.invoke()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.result_text, "")
        self.assertEqual(self.receipt(result)[0]["usage"], {"total_tokens": 9})
        self.assertIsNone(result.actual_cost)

    def test_usage_receipt_is_bounded_append_only_and_not_an_operator_choice(self):
        from django.core.exceptions import ValidationError
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.exceptions import ValidationError as ApiValidationError
        from studio.asset_generation_serializers import AssetGenerationResolutionInputSerializer
        from studio.models import AssetGenerationJobResolution
        from studio.services.execution_text_response_usage import record_asset_text_response_usage
        from studio.services.security_boundaries import security_boundary_resolution_cache
        result = self.invoke()
        row = result.resolutions.get(outcome="response_received")
        self.assertFalse(record_asset_text_response_usage(self.claim, self.prep.pk, self.prep.request_sha256, {"total_tokens": 99}))
        with self.assertRaises(ApiValidationError):
            AssetGenerationResolutionInputSerializer().fields["outcome"].run_validation("response_received")
        with self.assertRaises(ValidationError):
            row.save()
        with security_boundary_resolution_cache(), CaptureQueriesContext(connection) as queries:
            receipt, _ = self.receipt(result)
        sql = [q["sql"] for q in queries if 'FROM "studio_assetgenerationjobresolution"' in q["sql"]]
        self.assertEqual(len(sql), 1)
        self.assertIn("LIMIT 2", sql[0])
        self.assertNotIn('"note"', sql[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql[0])
                self.assertIn("INDEX", str(cursor.fetchall()).upper())
        self.assertEqual(receipt["state"], "recorded")
        AssetGenerationJobResolution.objects.create(job=result, revision=row.revision + 1,
            outcome="response_received", idempotency_key="isolated-duplicate", request_sha256=row.request_sha256,
            evidence=row.evidence, evidence_schema_version=row.evidence_schema_version,
            evidence_size_bytes=row.evidence_size_bytes, evidence_sha256=row.evidence_sha256)
        self.assertEqual(self.receipt(result)[0]["state"], "unverifiable")
        self.assertEqual(result.resolutions.count(), 2)

    def test_known_provider_rejection_keeps_safe_usage_but_transport_uncertainty_does_not(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), transaction.atomic():
                error = ProviderDriverError("isolated", code="provider_response_invalid", submission_uncertain=uncertain)
                error.metadata = {"usage": {"prompt_tokens": 3, "completion_tokens": 8, "secret": "PRIVATE"}}
                self.driver.invoke.side_effect = error
                result = self.invoke()
                receipt, _ = self.receipt(result)
                self.assertEqual(receipt["state"], "unavailable" if uncertain else "recorded")
                self.assertEqual(receipt["usage"], {} if uncertain else {"prompt_tokens": 3, "completion_tokens": 8})
                self.assertIsNone(result.actual_cost)
                transaction.set_rollback(True)

    def test_lost_claim_does_not_append_receipt_or_success(self):
        import uuid
        def stale_response(*args, **kwargs):
            AssetGenerationJob.objects.filter(pk=self.job.pk).update(claim_token=uuid.uuid4())
            return {"text": "stale", "usage": {"total_tokens": 9}}
        self.driver.invoke.side_effect = stale_response
        result = self.invoke()
        self.assertEqual(result.status, "submitting")
        self.assertFalse(result.resolutions.exists())
        self.assertEqual(result.result_text, "")

    def test_audit_failure_does_not_publish_success_or_resend(self):
        from django.db import OperationalError
        with patch("studio.services.execution_text_response_usage.record_asset_text_response_usage",
                side_effect=OperationalError("isolated audit failure")):
            with self.assertRaises(OperationalError):
                self.invoke()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "submitting")
        self.assertFalse(self.job.resolutions.exists())
        self.assertEqual(self.job.result_text, "")
        with patch("studio.services.asset_generation.driver_for", return_value=self.driver):
            process_text_asset_job(self.claim)
        self.driver.invoke.assert_called_once()

    def test_cancel_after_claim_does_not_invoke_model(self):
        claim = claim_next_text_asset_job("isolated-text-worker")
        first = request_asset_generation_cancel(claim)
        self.assertIsNotNone(first.cancel_requested_at)
        repeated = request_asset_generation_cancel(first)
        self.assertEqual(repeated.cancel_requested_at, first.cancel_requested_at)
        with patch("studio.services.asset_generation.driver_for", return_value=self.driver):
            result = process_text_asset_job(claim)
        self.assertEqual(result.status, "cancelled")
        self.driver.invoke.assert_not_called()

    def test_recovery_uses_frozen_timeout_for_new_operation_and_never_resubmits(self):
        claim = claim_next_text_asset_job("isolated-text-worker")
        now = timezone.now()
        AssetGenerationJob.objects.filter(pk=claim.pk).update(status="submitting", submission_started_at=now - timedelta(seconds=35),
            heartbeat_at=now - timedelta(minutes=2))
        self.assertEqual(recover_stale_asset_generation_jobs(minutes=1, text=True, text_submission_grace_seconds=15),
            {"released": 0, "uncertain": 0})
        AssetGenerationJob.objects.filter(pk=claim.pk).update(submission_started_at=now - timedelta(seconds=60))
        self.assertEqual(recover_stale_asset_generation_jobs(minutes=1, text=True, text_submission_grace_seconds=15),
            {"released": 0, "uncertain": 1})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "uncertain")
        self.assertIsNone(self.job.claim_token)
