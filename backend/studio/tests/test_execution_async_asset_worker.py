"""Formal async asset queue and cost collection, with no paid peer or download."""
import hashlib
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, AssetGenerationJobResolution
from studio.services.asset_generation import create_asset_generation_job, claim_next_media_asset_job, process_media_asset_job, request_asset_generation_cancel, recover_stale_asset_generation_jobs
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.provider_registry import ProviderDriverError
from studio.tests import test_execution_smoke_async as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAsyncAssetWorkerTests(TestCase):
    setUpTestData = fixtures.ExecutionAsyncSmokeTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionAsyncSmokeTests.write_contract)
    job_row = fixtures.ExecutionAsyncSmokeTests.job_row
    resolve = fixtures.ExecutionAsyncSmokeTests.resolve
    cycle = fixtures.ExecutionAsyncSmokeTests.cycle
    submit = fixtures.ExecutionAsyncSmokeTests.submit

    def setUp(self):
        fixtures.ExecutionAsyncSmokeTests.setUp(self)
        self.submit()
        self.peer.invoke.side_effect = [{"status": "succeeded", "terminal_success": True}, {"artifacts": []}]
        with patch("studio.services.remote_artifacts.verify_remote_artifacts_for_smoke", return_value=[{"kind": "video", "sha256": "a" * 64}]):
            self.assertEqual(self.cycle().status, "succeeded")
        self.business_job, _ = create_asset_generation_job(owner=self.actor, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video", name="隔离异步视频",
            **self.intent.value, field_values=[], allow_external_calls=True, budget_limit=None,
            client_idempotency_key="async-asset-original-worker")
        prep = self.business_job.execution_preparations.get()
        ready = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.business_peer = Mock()
        self.enterContext(patch("studio.services.asset_generation.driver_for", return_value=self.business_peer))
        self.download = self.enterContext(patch("studio.services.asset_generation.download_remote_artifact", side_effect=AssertionError("Cancelled outputs must not download")))
    def business_submit(self):
        self.business_peer.invoke.return_value = {"request_id": "isolated-asset", "status": "queued"}
        submitted = self.business_cycle()
        self.assertEqual(submitted.status, "submitted", submitted.error_message)

    def business_cycle(self):
        AssetGenerationJob.objects.filter(pk=self.business_job.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_media_asset_job("isolated-async-worker")
        self.assertIsNotNone(claim)
        return process_media_asset_job(claim)

    def late_success(self, receipt):
        self.business_submit()
        self.business_job.refresh_from_db()
        request_asset_generation_cancel(self.business_job)
        self.business_peer.invoke.return_value = {"status": "cancelling"}
        self.assertEqual(self.business_cycle().status, "cancel_requested")
        remote_success = self.provider.driver_config["terminal_success"][0]
        self.business_peer.invoke.return_value = {"status": remote_success, "terminal_success": True}
        self.assertEqual(self.business_cycle().status, "cancel_requested")
        self.business_peer.invoke.return_value = {"artifacts": [], **receipt}
        return self.business_cycle()

    def test_collect_without_bill_finishes_cancel_but_keeps_estimate_reserved(self):
        result = self.late_success({})
        self.assertEqual(result.status, "cancelled", result.error_message)
        self.assertIsNone(result.actual_cost)
        self.assertIsNotNone(result.estimated_cost)
        self.download.assert_not_called()

    def test_zero_receipt_is_explicit_settlement_not_an_unknown_price(self):
        result = self.late_success({"actual_cost": "0", "currency": "USD"})
        self.assertEqual(result.status, "cancelled", result.error_message)
        self.assertEqual((result.actual_cost, result.cost_currency), (Decimal("0"), "USD"))
        self.download.assert_not_called()

    def test_cancel_retains_explicit_remote_failure_without_materializing_any_output(self):
        self.business_submit()
        self.business_job.refresh_from_db()
        request_asset_generation_cancel(self.business_job)
        self.business_peer.invoke.return_value = {"status": "failed", "terminal_failure": True}
        result = self.business_cycle()
        self.assertEqual((result.status, result.error_code, result.remote_status),
            ("failed", "remote_generation_failed_during_cancel", "failed"))
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)
        self.download.assert_not_called()

    def test_cancelled_late_success_keeps_actual_nonzero_receipt_without_an_asset(self):
        result = self.late_success({"actual_cost": "0.02", "currency": "USD"})
        self.assertEqual((result.status, result.error_code), ("cancelled", "late_result_ignored"))
        self.assertEqual((result.actual_cost, result.cost_currency), (Decimal("0.02"), "USD"))
        self.assertIsNone(result.output_asset_id)
        self.download.assert_not_called()

    def test_uncertain_cancel_resolution_keeps_cancel_intent_and_is_idempotent(self):
        self.business_submit()
        self.business_job.refresh_from_db()
        cancelled = request_asset_generation_cancel(self.business_job)
        self.business_peer.invoke.side_effect = ProviderDriverError(
            "Isolated lost cancellation response", code="lost_response", submission_uncertain=True)
        uncertain = self.business_cycle()
        self.assertEqual(uncertain.status, "uncertain", uncertain.error_message)
        self.assertTrue(uncertain.ignore_late_result)
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient()
        client.force_authenticate(self.actor)
        path = f"/api/v1/asset-generation-jobs/{uncertain.pk}/resolve-uncertain/"
        body = {"outcome": "confirmed_remote_succeeded", "provider_request_id": "isolated-asset",
            "actual_cost": "0.02", "cost_currency": "USD", "note": "隔离核验远端成功和账单，用户取消仍生效。"}
        before = self.business_peer.invoke.call_count
        first = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="native-cancel-resolution")
        self.assertEqual(first.status_code, 201, first.data)
        replay = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="native-cancel-resolution")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data["id"], replay.data["id"])
        self.assertEqual(first.data["job_status"], "cancelled")
        uncertain.refresh_from_db()
        self.assertEqual((uncertain.actual_cost, uncertain.cost_currency), (Decimal("0.02"), "USD"))
        self.assertEqual(uncertain.cancel_requested_at, cancelled.cancel_requested_at)
        self.assertIsNone(uncertain.output_asset_id)
        self.assertEqual(self.business_peer.invoke.call_count, before)
        self.download.assert_not_called()

    def test_cancel_crash_cannot_repeat_cancel(self):
        self.business_submit()
        self.business_job.refresh_from_db()
        request_asset_generation_cancel(self.business_job)
        self.business_peer.invoke.side_effect = SystemExit("isolated lost cancel worker")
        with self.assertRaises(SystemExit):
            self.business_cycle()
        self.business_job.refresh_from_db()
        self.assertIsNotNone(self.business_job.cancel_submitted_at)
        AssetGenerationJob.objects.filter(pk=self.business_job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        recover_stale_asset_generation_jobs(1, text=False, text_submission_grace_seconds=30)
        self.business_peer.invoke.side_effect = None
        self.business_peer.invoke.return_value = {"status": "running"}
        self.assertEqual(self.business_cycle().status, "cancel_requested")
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit", "cancel", "poll"])

    def operator_client(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        client = APIClient()
        client.force_authenticate(self.actor)
        return client

    def test_invalid_submit_identity_is_never_coerced_into_a_remote_request(self):
        for request_id in (None, False, 0, 123, [], {"id": "not-a-token"}, "", " "):
            with self.subTest(request_id=request_id), transaction.atomic():
                self.business_peer.invoke.reset_mock()
                self.business_peer.invoke.return_value = {"request_id": request_id, "status": "queued"}
                result = self.business_cycle()
                self.assertEqual(result.status, "uncertain", result.error_message)
                self.assertEqual((result.provider_request_id, result.provider_request_id_sha256), ("", ""))
                self.assertIsNone(result.output_asset_id)
                self.assertIsNone(claim_next_media_asset_job("no-repeat"))
                self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit"])
                transaction.set_rollback(True)

    def test_overlong_identity_keeps_exact_hash_and_can_be_resolved_without_truncation(self):
        request_id = "r" * 192
        self.business_peer.invoke.return_value = {"request_id": request_id, "status": "queued"}
        result = self.business_cycle()
        self.assertEqual((result.status, result.error_code), ("uncertain", "provider_request_id_too_long"))
        self.assertEqual(result.provider_request_id, "")
        self.assertEqual(result.provider_request_id_sha256, hashlib.sha256(request_id.encode()).hexdigest())
        client = self.operator_client()
        resolved = client.post(f"/api/v1/asset-generation-jobs/{result.pk}/resolve-uncertain/",
            {"outcome": "confirmed_remote_failed", "note": "隔离核验完整请求摘要，确认远端失败。"},
            format="json", HTTP_IDEMPOTENCY_KEY="long-identity-resolution")
        self.assertEqual(resolved.status_code, 201, resolved.data)
        result.refresh_from_db()
        self.assertEqual((result.status, result.remote_status), ("failed", "operator_confirmed_remote_failed"))
        self.assertEqual(result.provider_request_id_sha256, hashlib.sha256(request_id.encode()).hexdigest())
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit"])

    def test_resolution_api_keeps_permission_audit_and_replay_without_resubmission(self):
        self.business_peer.invoke.side_effect = ProviderDriverError(
            "Isolated lost submit reply", code="lost_response", submission_uncertain=True)
        uncertain = self.business_cycle()
        self.assertEqual(uncertain.status, "uncertain")
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        client = APIClient()
        client.force_authenticate(self.actor)
        path = f"/api/v1/asset-generation-jobs/{uncertain.pk}/resolve-uncertain/"
        body = {"outcome": "confirmed_not_submitted", "note": "隔离核验对端未创建请求。"}
        self.assertEqual(client.post(path, body, format="json").status_code, 403)
        client = self.operator_client()
        result = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="native-submit-resolution")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["job_status"], "failed")
        self.assertEqual(len(result.data["evidence_sha256"]), 64)
        replay = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="native-submit-resolution")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(result.data["id"], replay.data["id"])
        rows = AssetGenerationJobResolution.objects.filter(pk=result.data["id"])
        with self.assertRaises(ValidationError):
            rows.update(note="forged operator evidence")
        with self.assertRaises(ValidationError):
            rows.delete()
        detail = client.get(f"/api/v1/asset-generation-jobs/{uncertain.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["latest_resolution"]["id"], result.data["id"])
        retry_path = f"/api/v1/asset-generation-jobs/{uncertain.pk}/retry/"
        retry = client.post(retry_path, {}, format="json", HTTP_IDEMPOTENCY_KEY="native-resolved-retry")
        self.assertEqual(retry.status_code, 202, retry.data)
        retry_replay = client.post(retry_path, {}, format="json", HTTP_IDEMPOTENCY_KEY="native-resolved-retry")
        self.assertEqual(retry_replay.status_code, 200, retry_replay.data)
        self.assertEqual(retry.data["id"], retry_replay.data["id"])
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit"])
        self.download.assert_not_called()

    def test_collection_cannot_overwrite_attested_cost_even_after_reconfirmation(self):
        self.business_submit()
        self.business_peer.invoke.side_effect = ProviderDriverError(
            "Isolated lost poll response", code="lost_response", submission_uncertain=True)
        uncertain = self.business_cycle()
        self.assertEqual(uncertain.status, "uncertain", uncertain.error_message)
        client = self.operator_client()
        path = f"/api/v1/asset-generation-jobs/{uncertain.pk}/resolve-uncertain/"
        body = {"outcome": "confirmed_remote_succeeded", "provider_request_id": "isolated-asset",
            "actual_cost": "0.01", "cost_currency": "USD", "note": "隔离核验账单，确认费用为0.01美元。"}
        first = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="native-attested-cost")
        self.assertEqual(first.status_code, 201, first.data)
        self.business_peer.invoke.side_effect = None
        self.business_peer.invoke.return_value = {"request_id": "isolated-asset", "artifacts": [], "actual_cost": "0.02", "currency": "USD"}
        for cycle in range(2):
            result = self.business_cycle()
            self.assertEqual((result.status, result.error_code), ("uncertain", "actual_cost_reconciliation_required"))
            self.assertEqual((result.actual_cost, result.cost_currency), (Decimal("0.01"), "USD"))
            self.assertIsNone(result.output_asset_id)
            from studio.models import ExecutionTextResponseReview
            self.assertEqual(ExecutionTextResponseReview.objects.filter(asset_job=result).count(), 1)
            self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list].count("collect"), 1)
            if cycle == 0:
                carried = client.post(path, {"outcome": "confirmed_remote_succeeded", "provider_request_id": "isolated-asset",
                    "note": "再次核验远端成功，费用沿用已确认账单。"}, format="json",
                    HTTP_IDEMPOTENCY_KEY="native-carried-cost")
                self.assertEqual(carried.status_code, 201, carried.data)
                self.assertEqual(carried.data["evidence"]["actual_cost_source"], "carried_forward")
        self.download.assert_not_called()
