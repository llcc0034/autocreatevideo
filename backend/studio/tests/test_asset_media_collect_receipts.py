"""M4-07: real queue/API with isolated media and collect peers, no real calls."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, AssetReferenceVersion, ExecutionTextResponseReview
from studio.services.asset_generation import claim_next_comfy_asset_job
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response
from studio.services.provider_registry import ComfyUIDriver
from studio.tests import test_asset_material_registration as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssetMediaCollectReceiptTests(TestCase):
    setUp = fixtures.AssetMaterialRegistrationTests.setUp
    tearDown = fixtures.AssetMaterialRegistrationTests.tearDown
    create_job = fixtures.AssetMaterialRegistrationTests.create_job
    primary_version = fixtures.AssetMaterialRegistrationTests.primary_version
    http_payload = fixtures.AssetMaterialRegistrationTests.http_payload
    post_target = fixtures.AssetMaterialRegistrationTests.post_target
    submit = fixtures.AssetMaterialRegistrationTests.submit
    cycle = fixtures.AssetMaterialRegistrationTests.cycle

    def detail(self, **query):
        return self.client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/", query)

    def assert_failed_receipt(self, result, expected):
        self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"), result.error_message)
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)
        self.assertEqual(Asset.objects.count(), 3)  # existing fixture references only
        self.assertFalse(AssetReferenceVersion.objects.exists())
        row = ExecutionTextResponseReview.objects.get(asset_job=result)
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(json.loads(original), expected)
        self.assertEqual(diagnostics["body_kind"], "collect_receipt_json")
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), row.original_sha256)
        self.assertIsNone(row.confirmed_at)
        self.assertIsNone(claim_next_comfy_asset_job("terminal-no-repeat"))
        return row

    def test_invalid_empty_duplicate_wrong_node_receipts_remain_readable_without_candidate(self):
        self.submit(None)
        item = self.receipt["outputs"][0]
        for outputs in ([], [item, deepcopy(item)], [{**item, "node_id": "wrong-node"}]):
            with self.subTest(outputs=outputs), transaction.atomic():
                receipt = {"request_id": str(self.prep.pk), "outputs": outputs}
                self.peer.reset_mock()
                self.peer.invoke.return_value = receipt
                result = self.cycle()
                self.assert_failed_receipt(result, receipt)
                with CaptureQueriesContext(connection) as queries:
                    response = self.detail()
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["output"]["state"], "available")
                self.assertEqual(json.loads(response.data["output"]["text"])["request_id"], str(self.prep.pk))
                self.assertEqual(response.data["output"]["media"], [])
                self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
                self.assertEqual([c.args[1] for c in self.peer.invoke.call_args_list], ["collect"])
                transaction.set_rollback(True)

    def test_real_driver_returns_completed_empty_receipt_for_worker_validation(self):
        self.submit(None)
        local = Mock()
        local.history.return_value = {"status": {"completed": True}, "outputs": {}}
        local.extract_outputs.return_value = []
        self.peer.invoke.side_effect = ComfyUIDriver().invoke
        with patch("studio.services.comfyui.ComfyUIClient", return_value=local):
            result = self.cycle()
        expected = {"schema_version": 1, "request_id": str(self.prep.pk), "status": "succeeded", "outputs": []}
        self.assert_failed_receipt(result, expected)
        local.history.assert_called_once_with(str(self.prep.pk))

    def test_nested_receipt_is_redacted_in_task_and_preparation_and_cannot_be_corrected(self):
        self.submit(None)
        receipt = {**self.receipt, "outputs": [{**self.receipt["outputs"][0], "node_id": "wrong-node",
            "secret": "hidden-credential", "url": "https://private.invalid/media"}], "note": "原回执"}
        self.peer.invoke.return_value = receipt
        row = self.assert_failed_receipt(self.cycle(), receipt)
        self.prep.refresh_from_db()
        for response, text in ((self.detail(), "task"),
                (self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/"), "preparation")):
            self.assertEqual(response.status_code, 200, response.data)
            body = response.data["output"]["text"] if text == "task" else response.data["text_response_review"]["original_text"]
            safe = json.loads(body)
            self.assertEqual(safe["outputs"][0]["filename"], "[redacted]")
            self.assertEqual(safe["outputs"][0]["subfolder"], "[redacted]")
            self.assertNotIn("hidden-credential", body)
            self.assertNotIn("private.invalid", body)
            self.assertEqual(safe["note"], "原回执")
            if text == "preparation":
                self.assertFalse(response.data["text_response_review"]["editable"])
        self.peer.reset_mock()
        response = self.client.post(f"/api/v1/execution-preparations/{self.prep.pk}/complete-text-response/", {
            "expected_preparation_revision": self.prep.revision, "review_id": str(row.pk),
            "original_sha256": row.original_sha256, "text": "Cannot correct a receipt"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        row.refresh_from_db()
        self.assertIsNone(row.confirmed_at)
        self.peer.invoke.assert_not_called()

    def test_wrong_or_missing_request_identity_is_not_filled_in_or_retained(self):
        self.submit(None)
        for request_id in (str(uuid4()), None):
            with self.subTest(request_id=request_id), transaction.atomic():
                self.peer.invoke.return_value = {**self.receipt, "request_id": request_id}
                result = self.cycle()
                self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"))
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertIsNone(result.output_asset_id)
                transaction.set_rollback(True)

    def test_non_json_receipt_fails_explicitly_without_inventing_body(self):
        self.submit(None)
        self.peer.invoke.return_value = {**self.receipt, "invalid_number": float("nan")}
        result = self.cycle()
        self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"))
        self.assertIn("不是可保存的 JSON", result.error_message)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(claim_next_comfy_asset_job("no-repeat"))

    def test_storage_failure_is_terminal_without_false_receipt_or_output(self):
        self.submit(None)
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated receipt storage failure")):
            result = self.cycle()
        self.assertEqual((result.status, result.remote_status, result.error_code),
            ("failed", "succeeded", "execution_response_storage_failed"))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(claim_next_comfy_asset_job("no-repeat"))

    def test_cancel_or_lost_claim_during_collect_cannot_retain_late_receipt(self):
        self.submit(None)
        for change in ({"status": "cancel_requested"}, {"claim_token": uuid4()}, {"ignore_late_result": True}):
            with self.subTest(change=change), transaction.atomic():
                def late(*args, **kwargs):
                    AssetGenerationJob.objects.filter(pk=self.job.pk).update(**change)
                    return self.receipt
                self.peer.invoke.side_effect = late
                result = self.cycle()
                self.assertIsNone(result.output_asset_id)
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                transaction.set_rollback(True)

    def test_local_archive_retry_reads_same_receipt_without_another_collect(self):
        self.submit(None)
        with patch("studio.services.asset_generation._finish_media_asset_result", side_effect=OSError("isolated archive outage")):
            result = self.cycle()
        self.assertEqual((result.status, result.remote_status), ("running", "succeeded"))
        row = ExecutionTextResponseReview.objects.get(asset_job=self.job)
        original = row.original_text
        self.peer.reset_mock()
        self.peer.invoke.side_effect = AssertionError("must reuse received receipt")
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertIsNotNone(result.output_asset_id)
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)
        self.assertEqual(ExecutionTextResponseReview.objects.get(pk=row.pk).original_text, original)
        self.peer.invoke.assert_not_called()

    def test_permissions_integrity_and_chunk_read_use_original_retained_receipt(self):
        self.submit(None)
        receipt = {"request_id": str(self.prep.pk), "outputs": [], "note": "保留实际回执" * 6000}
        self.peer.invoke.return_value = receipt
        row = self.assert_failed_receipt(self.cycle(), receipt)
        plan = ExecutionTextResponseReview.objects.filter(asset_job=self.job).values("id").explain()
        self.assertIn("SEARCH", plan)
        self.assertIn("INDEX", plan)
        response = self.detail(content_field="output.text", content_offset=0, content_limit=1024)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["content"]["next_offset"])
        from studio.services.json_contracts import assign_json_contract
        broken = {key: value for key, value in row.diagnostics.items() if key != "body_kind"}
        assign_json_contract(row, "diagnostics", broken)
        with self.assertRaisesMessage(ValidationError, "缺少确切正文类型"):
            read_retained_text_response(row)  # in-memory corruption only, never overwrite the saved fact
        outsider = APIClient()
        outsider.force_authenticate(get_user_model().objects.create_user(username="receipt-outsider"))
        self.assertEqual(outsider.get(f"/api/v1/task-center/preparation/{self.prep.pk}/").status_code, 404)
        with patch("studio.services.execution_response_storage.read_response", side_effect=ExecutionResponseStorageError("damaged bytes")):
            response = self.detail()
        self.assertEqual(response.data["output"]["state"], "error")
        self.assertEqual(response.data["output"]["text"], "")
