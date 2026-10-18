"""M7-05 actual Comfy text collection on isolated original worker/queues."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, ExecutionTextResponseReview, GenerationAttempt
from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job, request_asset_generation_cancel
from studio.services.comfyui import ComfyUIClient
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import read_retained_text_response
from studio.tests import test_execution_comfy_text_worker as fixtures


@override_settings(COMFYUI_OUTPUT_DIR="", AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ComfyAssetTextResponseRetentionTests(TestCase):
    setUp = fixtures.ExecutionComfyTextWorkerTests.setUp
    output = fixtures.ExecutionComfyTextWorkerTests.output
    smoke = fixtures.ExecutionComfyTextWorkerTests.smoke
    job = fixtures.ExecutionComfyTextWorkerTests.job
    cycle = fixtures.ExecutionComfyTextWorkerTests.cycle

    def detail(self, prep, actor=None):
        client = APIClient()
        client.force_authenticate(actor or self.user)
        return client.get(f"/api/v1/task-center/preparation/{prep.pk}/")

    def assert_no_candidate(self, job):
        self.assertEqual(job.result_text, "")
        self.assertIsNone(job.output_asset_id)
        self.assertIsNone(job.actual_cost)
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_collector_to_worker_retains_actual_whitespace_and_reads_without_writes(self):
        job, prep = self.job()
        self.peer.reset_mock()
        text = "  实际收到的正文\r\nBonjour。  "
        # Exercise the real extraction too, not a pre-trimmed driver fixture.
        self.peer.invoke.return_value = {"request_id": str(prep.pk), "outputs":
            ComfyUIClient.extract_outputs({"outputs": {"2": {"text": [text]}}})}
        result = self.cycle(job)
        self.assertEqual(result.status, "succeeded", result.error_message)
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        original, diagnostics, _, _ = read_retained_text_response(row)
        self.assertEqual(original, text)
        self.assertEqual(diagnostics["response_source"], "comfyui_collect")
        self.assertEqual(diagnostics["provider_request_id"], str(prep.pk))
        self.assertEqual(diagnostics["output_node_id"], "2")
        self.assertIsNone(row.confirmed_at)
        self.assertIsNone(row.attempt_id)
        with CaptureQueriesContext(connection) as queries:
            response = self.detail(prep)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["output"]["text"], text)
        self.assertEqual(response.data["output"]["result"]["candidate_text"], text)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual([c.args[1] for c in self.peer.invoke.call_args_list], ["collect"])
        self.assertIsNone(claim_next_comfy_asset_job("no-repeat"))

    def test_invalid_content_remains_readable_without_candidate_or_automatic_retry(self):
        job, prep = self.job()
        self.peer.reset_mock()
        text = "真实返回\x00不是合法候选"
        self.peer.invoke.return_value = self.output(text)
        result = self.cycle(job)
        self.assertEqual((result.status, result.remote_status, result.error_code),
            ("failed", "succeeded", "comfyui_asset_text_response_invalid"))
        self.assert_no_candidate(result)
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        self.assertEqual(row.original_text, text)
        self.assertEqual(row.diagnostics["contract_error_code"], "response_contract_invalid")
        response = self.detail(prep)
        self.assertEqual(response.data["output"]["text"], text)
        self.assertNotIn("candidate_text", response.data["output"]["result"])
        self.peer.invoke.assert_called_once()
        self.assertIsNone(claim_next_comfy_asset_job("no-paid-repeat"))

    def test_script_asset_file_and_original_are_separate_verified_facts(self):
        from studio.services.asset_generation import asset_generation_options
        self.assertEqual(list(asset_generation_options(user=self.user, job_type="asset", media_type="script")
            .values_list("pk", flat=True)), [self.version.pk])
        job, prep = self.job(job_type="asset", media_type="script")
        text = "  剧本原始正文\r\n第二行  "
        self.peer.invoke.return_value = self.output(text)
        result = self.cycle(job)
        self.assertEqual(result.status, "succeeded", result.error_message)
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        self.assertEqual(row.original_text, text)
        self.assertEqual(row.diagnostics["contract_error_code"], "response_contract_valid")
        asset = result.output_asset
        self.assertEqual(asset.kind, "script")
        with asset.file.open("rb") as stream:
            self.assertEqual(stream.read().decode("utf-8"), text.replace("\r\n", "\n"))
        self.assertEqual(asset.metadata["generation_job_id"], str(job.pk))
        self.assertEqual(asset.metadata["preparation_id"], str(prep.pk))
        self.assertEqual(result.result_text, "")
        self.assertIsNone(result.actual_cost)
        self.assertIsNone(row.confirmed_at)
        self.assertEqual(Asset.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_rejected_script_body_never_materializes_a_script_asset(self):
        job, prep = self.job(job_type="asset", media_type="script")
        text = "收到的无效剧本\x00"
        self.peer.invoke.return_value = self.output(text)
        result = self.cycle(job)
        self.assertEqual(result.status, "failed")
        self.assert_no_candidate(result)
        self.assertEqual(ExecutionTextResponseReview.objects.get(asset_job=job).original_text, text)

    def test_text_version_cannot_create_or_appear_as_an_audio_or_video_asset(self):
        from studio.services.asset_generation import asset_generation_options, create_asset_generation_job
        for media_type in ("audio", "video", "image"):
            with self.subTest(media_type=media_type):
                self.assertFalse(asset_generation_options(user=self.user, job_type="asset", media_type=media_type).exists())
                with self.assertRaisesMessage(ValidationError, "实际输出合同与请求类型不一致"):
                    create_asset_generation_job(owner=self.user, execution_instance_version_id=self.version.pk,
                        expected_definition_sha256=self.definition.sha256, job_type="asset", media_type=media_type,
                        image_category="reference" if media_type == "image" else "", name="Rejected wrong kind",
                        **self.intent, field_values=[], client_idempotency_key="mismatched-output-kind")
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.peer.invoke.assert_not_called()

    def test_empty_and_whitespace_responses_are_retained_but_never_succeed(self):
        job, prep = self.job()
        for text in ("", " \r\n "):
            with self.subTest(text=text), transaction.atomic():
                self.peer.invoke.return_value = {"request_id": str(prep.pk), "outputs":
                    ComfyUIClient.extract_outputs({"outputs": {"2": {"text": text}}})}
                result = self.cycle(job)
                self.assertEqual(result.status, "failed")
                self.assert_no_candidate(result)
                row = ExecutionTextResponseReview.objects.get(asset_job=job)
                self.assertEqual(row.original_text, text)
                self.assertEqual(self.detail(prep).data["output"]["state"], "available")
                transaction.set_rollback(True)

    def test_bad_receipt_digest_or_size_is_retained_as_rejected_not_validated(self):
        job, prep = self.job()
        for changes in ({"sha256": "0" * 64}, {"size_bytes": 99999}):
            with self.subTest(changes=changes), transaction.atomic():
                receipt = self.output("实际收到但摘要不符")
                receipt["outputs"][0].update(changes)
                self.peer.invoke.return_value = receipt
                result = self.cycle(job)
                self.assertEqual(result.status, "failed")
                self.assert_no_candidate(result)
                row = ExecutionTextResponseReview.objects.get(asset_job=job)
                self.assertEqual(row.original_text, "实际收到但摘要不符")
                self.assertEqual(row.diagnostics["contract_error_code"], "comfyui_text_output_invalid")
                transaction.set_rollback(True)

    def test_missing_non_text_ambiguous_or_wrong_node_does_not_fabricate_a_response(self):
        job, prep = self.job()
        item = self.output()["outputs"][0]
        for outputs in ([], [{**item, "text": None}], [item, deepcopy(item)], [{**item, "node_id": "other"}]):
            with self.subTest(outputs=outputs), transaction.atomic():
                self.peer.invoke.return_value = {"request_id": str(prep.pk), "outputs": outputs}
                result = self.cycle(job)
                self.assertEqual(result.status, "failed")
                self.assert_no_candidate(result)
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                self.assertEqual(self.detail(prep).data["output"]["state"], "unavailable")
                transaction.set_rollback(True)

    def test_wrong_or_absent_collection_identity_does_not_attach_body_to_job(self):
        job, prep = self.job()
        for request_id in (str(uuid4()), None):
            with self.subTest(request_id=request_id), transaction.atomic():
                self.peer.invoke.return_value = {**self.output(), "request_id": request_id}
                result = self.cycle(job)
                self.assertEqual(result.status, "failed")
                self.assert_no_candidate(result)
                self.assertFalse(ExecutionTextResponseReview.objects.exists())
                transaction.set_rollback(True)

    def test_invalid_persisted_submission_identity_blocks_collection_itself(self):
        job, prep = self.job()
        self.peer.reset_mock()
        AssetGenerationJob.objects.filter(pk=job.pk).update(provider_request_id_sha256="0" * 64)
        result = self.cycle(job)
        self.assertEqual(result.status, "failed")
        self.assert_no_candidate(result)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.peer.invoke.assert_not_called()

    def test_storage_failure_keeps_known_remote_success_without_candidate_or_retry(self):
        job, prep = self.job()
        self.peer.reset_mock()
        self.peer.invoke.return_value = self.output()
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated storage failure")):
            result = self.cycle(job)
        self.assertEqual((result.status, result.remote_status, result.error_code),
            ("failed", "succeeded", "execution_response_storage_failed"))
        self.assert_no_candidate(result)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.peer.invoke.assert_called_once()
        self.assertIsNone(claim_next_comfy_asset_job("no-repeat"))

    def test_cancel_during_collection_cannot_retain_or_publish_late_body(self):
        job, prep = self.job()
        def late(*args, **kwargs):
            job.refresh_from_db()
            request_asset_generation_cancel(job)
            return self.output()
        self.peer.invoke.side_effect = late
        result = self.cycle(job)
        self.assertEqual(result.status, "cancelled")
        self.assert_no_candidate(result)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_lost_claim_during_collection_does_not_adopt_new_claim(self):
        job, prep = self.job()
        new_claim = uuid4()
        def late(*args, **kwargs):
            AssetGenerationJob.objects.filter(pk=job.pk).update(claim_token=new_claim)
            return self.output()
        self.peer.invoke.side_effect = late
        result = self.cycle(job)
        self.assertEqual(result.claim_token, new_claim)
        self.assertEqual(result.status, "running")
        self.assert_no_candidate(result)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_ignore_late_result_during_collection_does_not_retain(self):
        job, prep = self.job()
        def late(*args, **kwargs):
            AssetGenerationJob.objects.filter(pk=job.pk).update(ignore_late_result=True)
            return self.output()
        self.peer.invoke.side_effect = late
        result = self.cycle(job)
        self.assertTrue(result.ignore_late_result)
        self.assert_no_candidate(result)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_damaged_retained_bytes_are_explicit_error_not_candidate_fallback(self):
        job, prep = self.job()
        self.peer.invoke.return_value = self.output()
        self.cycle(job)
        with patch("studio.services.execution_response_storage.read_response", side_effect=ValidationError("isolated damaged bytes")):
            response = self.detail(prep)
        self.assertEqual(response.data["output"]["state"], "error")
        self.assertEqual(response.data["output"]["text"], "")

    def test_retained_request_identity_drift_is_error_and_foreign_owner_is_hidden(self):
        from django.contrib.auth import get_user_model
        job, prep = self.job()
        self.peer.invoke.return_value = self.output()
        self.cycle(job)
        outsider = get_user_model().objects.create_user(username="comfy-response-outsider")
        self.assertEqual(self.detail(prep, outsider).status_code, 404)
        AssetGenerationJob.objects.filter(pk=job.pk).update(provider_request_id=str(uuid4()))
        self.assertEqual(self.detail(prep).data["output"]["state"], "error")

    def test_replayed_old_claim_cannot_collect_or_overwrite_original(self):
        job, prep = self.job()
        self.peer.invoke.return_value = self.output()
        claim = claim_next_comfy_asset_job("original-worker")
        # job() scheduled a future poll; make the existing job due if needed.
        if claim is None:
            from django.utils import timezone
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            claim = claim_next_comfy_asset_job("original-worker")
        self.assertIsNotNone(claim)
        result = process_comfy_asset_job(claim)
        self.assertEqual(result.status, "succeeded")
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        original = row.original_text
        self.peer.reset_mock()
        process_comfy_asset_job(claim)
        self.assertEqual(ExecutionTextResponseReview.objects.get(asset_job=job).original_text, original)
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)
        self.peer.invoke.assert_not_called()
