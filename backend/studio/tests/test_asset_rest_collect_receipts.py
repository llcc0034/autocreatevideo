"""M4-07 original REST job/driver -> private receipt, with isolated WAV/HTTP."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
import wave

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import Asset, AssetGenerationJob, ExecutionTextResponseReview
from studio.services.asset_generation import (
    claim_next_media_asset_job, create_asset_generation_job, process_media_asset_job,
    recover_stale_asset_generation_jobs, request_asset_generation_cancel,
)
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response
from studio.services.provider_registry import RestrictedAsyncRestDriver
from studio.services.provenance import ProvenanceError
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests import test_execution_audio_assets as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssetRestCollectReceiptTests(TestCase):
    setUp = fixtures.ExecutionAudioAssetTests.setUp

    def submit(self):
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission",
            side_effect=isolated_observations))
        self.job, _ = create_asset_generation_job(owner=self.user, client_idempotency_key="rest-receipt",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="audio", name="隔离回执", parameters={"duration_seconds": 2},
            inputs={}, manual_channels={"positive_prompt": "两秒雨声"}, subject_facts=[],
            source_language="", target_language="", field_values=[], allow_external_calls=True, budget_limit=None)
        self.prep = freeze(self.job, self.user)
        dispatch_asset_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.peer = Mock()
        self.enterContext(patch("studio.services.asset_generation.driver_for", return_value=self.peer))
        self.download = self.enterContext(patch("studio.services.asset_generation.download_remote_artifact",
            side_effect=AssertionError("Invalid receipt must not download")))
        self.peer.invoke.return_value = {"request_id": "original-rest-call", "status": "submitted"}
        self.assertEqual(self.cycle().status, "submitted")
        self.peer.invoke.return_value = {"status": "succeeded", "terminal_success": True}
        self.assertEqual(self.cycle().status, "running")
        self.peer.reset_mock()
        self.receipt = {"request_id": "original-rest-call", "artifacts": [
            {"kind": "audio", "url": "http://127.0.0.1/private-audio.wav"}], "actual_cost": "0.02", "currency": "USD"}
        self.peer.invoke.return_value = self.receipt

    def cycle(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(next_poll_at=timezone.now())
        claim = claim_next_media_asset_job("isolated-rest-receipt")
        self.assertIsNotNone(claim)
        return process_media_asset_job(claim)

    def detail(self, **query):
        return self.client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/", query)

    def retained(self):
        row = ExecutionTextResponseReview.objects.get(asset_job=self.job)
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(diagnostics["response_source"], "async_rest_collect")
        self.assertEqual(diagnostics["body_kind"], "collect_receipt_json")
        self.assertEqual(diagnostics["provider_request_id"], "original-rest-call")
        self.assertIsNone(row.confirmed_at)
        return row, json.loads(original)

    def test_actual_driver_rejected_bodies_are_retained_before_normalization(self):
        self.submit()
        cases = [{"status": "succeeded", "artifacts": value} for value in (None, [], "invalid", [4], [{"url": "http://127.0.0.1/a"}])]
        cases.append({"status": "x" * 10000, "artifacts": [], "note": "原响应未截断"})
        self.peer.invoke.side_effect = RestrictedAsyncRestDriver().invoke
        for body in cases:
            with self.subTest(body=body), transaction.atomic(), patch(
                    "studio.services.provider_registry._request_json", return_value=body) as request:
                result = self.cycle()
                self.assertEqual((result.status, result.remote_status, result.error_code),
                    ("failed", "succeeded", "provider_response_invalid"), result.error_message)
                _, receipt = self.retained()
                self.assertEqual(receipt, {"schema_version": 1, "request_id": "original-rest-call", "source_response": body})
                self.assertNotIn("request_id", receipt["source_response"])
                self.assertIsNone(result.actual_cost)
                self.assertIsNone(result.output_asset_id)
                self.assertFalse(Asset.objects.exists())
                self.assertIsNone(claim_next_media_asset_job("no-repeat"))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[0], "GET")
                transaction.set_rollback(True)
        self.download.assert_not_called()

    def test_worker_rejects_nonobject_artifact_and_reads_private_body_without_writes(self):
        self.submit()
        receipt = {**self.receipt, "artifacts": ["not an artifact"], "nested": {"secret": "hidden-secret",
            "url": "http://127.0.0.1/private", "filename": "private.wav"}}
        self.peer.invoke.return_value = receipt
        result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("failed", "artifact_manifest_invalid"), result.error_message)
        row, raw = self.retained()
        self.assertEqual(raw, receipt)
        with CaptureQueriesContext(connection) as queries:
            detail = self.detail()
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["output"]["state"], "available")
        self.assertEqual(detail.data["output"]["media"], [])
        body = detail.data["output"]["text"]
        self.assertNotIn("hidden-secret", body)
        self.assertNotIn("private.wav", body)
        self.assertNotIn("127.0.0.1", body)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        prep = self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/")
        self.assertFalse(prep.data["text_response_review"]["editable"])
        self.prep.refresh_from_db()
        response = self.client.post(f"/api/v1/execution-preparations/{self.prep.pk}/complete-text-response/", {
            "expected_preparation_revision": self.prep.revision, "review_id": str(row.pk),
            "original_sha256": row.original_sha256, "text": "not a text correction"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.client.force_authenticate(get_user_model().objects.create_user(username="unrelated-receipt-reader"))
        self.assertEqual(self.detail().status_code, 404)
        self.assertEqual([call.args[1] for call in self.peer.invoke.call_args_list], ["collect"])

    def test_missing_wrong_identity_or_nonjson_body_is_not_invented(self):
        self.submit()
        cases = [{**self.receipt, "request_id": value} for value in (None, str(uuid4()))]
        cases.append({**self.receipt, "invalid_number": float("nan")})
        for receipt in cases:
            with self.subTest(receipt=receipt), transaction.atomic():
                self.peer.invoke.return_value = receipt
                result = self.cycle()
                self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"))
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertIsNone(result.output_asset_id)
                self.assertIsNone(claim_next_media_asset_job("no-repeat"))
                transaction.set_rollback(True)

    def test_storage_failure_stops_without_receipt_candidate_or_new_call(self):
        self.submit()
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated disk error")):
            result = self.cycle()
        self.assertEqual((result.status, result.remote_status, result.error_code),
            ("failed", "succeeded", "execution_response_storage_failed"))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertFalse(Asset.objects.exists())
        self.download.assert_not_called()
        self.assertIsNone(claim_next_media_asset_job("no-repeat"))

    def test_collect_cancel_and_lost_claim_cannot_attach_late_receipt(self):
        self.submit()
        for action in ("cancel", "claim"):
            with self.subTest(action=action), transaction.atomic():
                def collect(*_):
                    if action == "cancel":
                        self.job.refresh_from_db()
                        request_asset_generation_cancel(self.job)
                    else:
                        AssetGenerationJob.objects.filter(pk=self.job.pk).update(claim_token=uuid4())
                    return deepcopy(self.receipt)
                self.peer.invoke.side_effect = collect
                result = self.cycle()
                self.assertIn(result.status, {"cancelled", "running"})
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertFalse(Asset.objects.exists())
                self.download.assert_not_called()
                transaction.set_rollback(True)

    def download_wav(self, *, target, **_):
        self.assertTrue(Path(target).resolve().is_relative_to(Path(self.media_root).resolve()))
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 32000)

    def test_archive_crash_recovery_reuses_receipt_without_second_collect(self):
        self.submit()
        self.download.side_effect = SystemExit("isolated crash after receipt commit")
        with self.assertRaises(SystemExit):
            self.cycle()
        original, receipt = self.retained()
        self.assertEqual(receipt, self.receipt)
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        recover_stale_asset_generation_jobs(1, text=False, text_submission_grace_seconds=30)
        self.peer.reset_mock()
        self.peer.invoke.side_effect = AssertionError("Do not recollect or resubmit")
        self.download.side_effect = self.download_wav
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(str(result.actual_cost), "0.020000")
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(ExecutionTextResponseReview.objects.get(asset_job=result).pk, original.pk)
        self.peer.invoke.assert_not_called()

    def test_actual_successful_collect_keeps_raw_body_and_materializes_once(self):
        self.submit()
        self.peer.invoke.side_effect = RestrictedAsyncRestDriver().invoke
        self.download.side_effect = self.download_wav
        body = {"status": "succeeded", "artifacts": [{"kind": "audio", "url": "http://127.0.0.1/result.wav", "id": "wav-1"}],
            "actual_cost": "0.02", "currency": "USD", "extra": {"note": "实际回执"}}
        with patch("studio.services.provider_registry._request_json", return_value=body) as request:
            result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        _, receipt = self.retained()
        self.assertEqual(receipt["source_response"], body)
        self.assertEqual(receipt["artifacts"][0]["artifact_id"], "wav-1")
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.download.call_count, 1)
        self.assertIsNone(claim_next_media_asset_job("no-repeat"))

    def test_probe_failure_preserves_receipt_without_fake_asset_or_unhandled_crash(self):
        self.submit()
        self.download.side_effect = self.download_wav
        with patch("studio.services.asset_generation.verified_local_media_facts",
                side_effect=ProvenanceError("isolated unsupported media")):
            result = self.cycle()
        self.assertEqual((result.status, result.remote_status, result.error_code),
            ("failed", "succeeded", "asset_media_job_invalid"))
        self.assertEqual(self.retained()[1], self.receipt)
        self.assertFalse(Asset.objects.exists())
        self.assertIsNone(claim_next_media_asset_job("no-repeat"))

    def test_damaged_receipt_prevents_local_recovery_from_recollecting(self):
        self.submit()
        self.download.side_effect = SystemExit("isolated interruption")
        with self.assertRaises(SystemExit):
            self.cycle()
        row, _ = self.retained()
        from django.core.files.storage import default_storage
        with default_storage.open(row.original_file, "wb") as handle:
            handle.write(b"damaged receipt")
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        recover_stale_asset_generation_jobs(1, text=False, text_submission_grace_seconds=30)
        self.peer.reset_mock()
        self.peer.invoke.side_effect = AssertionError("Missing/corrupt saved bytes cannot trigger new collect")
        result = self.cycle()
        self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"))
        self.assertFalse(Asset.objects.exists())
        self.peer.invoke.assert_not_called()
