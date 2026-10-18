"""Original API and worker, isolated DB/storage and fake providers only."""
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, ExecutionCapacityObservation, ExecutionPreparation, ExecutionTextResponseReview, GenerationAttempt
from studio.services.execution_text_response_review import complete_text_response
from studio.tests import test_asset_text_response_retention as chat
from studio.tests import test_comfy_asset_text_response_retention as comfy


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class AssetTextResponseCorrectionTests(TestCase):
    setUpTestData = classmethod(chat.AssetTextResponseRetentionTests.setUpTestData.__func__)
    setUp = chat.AssetTextResponseRetentionTests.setUp
    version = chat.AssetTextResponseRetentionTests.version
    invoke = chat.AssetTextResponseRetentionTests.invoke
    budget_error = chat.AssetTextResponseRetentionTests.budget_error

    def failed(self):
        self.driver.invoke.return_value["text"] = "原文\x00不能直接使用\r\n"
        result = self.invoke()
        self.assertEqual(result.status, "failed")
        self.prep.refresh_from_db()
        self.row = ExecutionTextResponseReview.objects.get(asset_job=result)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/execution-preparations/{self.prep.pk}/"
        self.payload = {"expected_preparation_revision": self.prep.revision,
            "review_id": str(self.row.pk), "original_sha256": self.row.original_sha256, "text": "人工修订\r\n正文"}
        return result

    def post(self, **changes):
        return self.client.post(self.url + "complete-text-response/", {**self.payload, **changes}, format="json")

    def test_original_api_exposes_exact_owner_and_records_human_correction_without_call_or_capacity(self):
        job = self.failed()
        request_hash = self.prep.request_sha256
        with CaptureQueriesContext(connection) as queries:
            detail = self.client.get(self.url)
        self.assertEqual(detail.status_code, 200, detail.data)
        review = detail.data["text_response_review"]
        self.assertEqual((review["asset_job_id"], review["attempt_id"], review["editable"]), (str(job.pk), None, True))
        self.assertIsNone(review["corrected_text"])
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        with patch("studio.services.asset_generation.driver_for", side_effect=AssertionError("correction cannot call")):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        job.refresh_from_db(); self.row.refresh_from_db(); self.prep.refresh_from_db()
        self.assertEqual((job.status, job.remote_status, job.result_text), ("succeeded", "response_rejected", self.payload["text"]))
        self.assertIsNone(job.actual_cost)
        self.assertEqual(self.row.original_text, "原文\x00不能直接使用\r\n")
        self.assertEqual(self.row.corrected_text, self.payload["text"])
        self.assertEqual(self.row.confirmed_by_id, self.actor.pk)
        self.assertEqual(self.prep.request_sha256, request_hash)
        self.assertEqual(self.prep.revision, self.payload["expected_preparation_revision"] + 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionCapacityObservation.objects.exists())
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(response.data["text_response_review"]["editable"])
        self.assertEqual(response.data["text_response_review"]["corrected_text"], self.payload["text"])
        output = self.client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/").data["output"]
        self.assertEqual(output["text"], self.row.original_text)
        self.assertEqual(output["result"]["human_correction"]["corrected_text"], self.payload["text"])
        self.assertIn("人工修订", "\n".join(output["notes"]))
        self.assertIn("人工补正", response.data["outcome"]["message"])
        self.driver.invoke.assert_called_once()

    def test_identical_confirmation_replays_and_different_text_conflicts(self):
        self.failed()
        self.assertEqual(self.post().status_code, 200)
        before = {p for p in Path(self.media).rglob("*") if p.is_file()}
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post(text="另一个版本").status_code, 409)
        self.assertEqual({p for p in Path(self.media).rglob("*") if p.is_file()}, before)
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)

    def test_nul_rejection_explains_control_character_without_call_or_write(self):
        self.failed()
        response = self.post(text="正文\x00仍有空字符")
        self.assertEqual(response.status_code, 400)
        self.assertIn("NUL", str(response.data))
        self.assertIn("控制字符", str(response.data))
        self.row.refresh_from_db(); self.prep.refresh_from_db(); self.job.refresh_from_db()
        self.assertIsNone(self.row.confirmed_at)
        self.assertEqual(self.prep.revision, self.payload["expected_preparation_revision"])
        self.assertEqual(self.job.status, "failed")
        self.driver.invoke.assert_called_once()

    def test_stale_revision_hash_wrong_owner_and_invalid_text_write_nothing(self):
        self.failed()
        before = {p for p in Path(self.media).rglob("*") if p.is_file()}
        for change, status in (({"expected_preparation_revision": 999}, 409), ({"original_sha256": "0" * 64}, 409),
                ({"review_id": str(uuid4())}, 409), ({"text": "仍然\x00非法"}, 400), ({"text": "  "}, 400)):
            with self.subTest(change=change):
                self.assertEqual(self.post(**change).status_code, status)
        self.row.refresh_from_db()
        self.assertIsNone(self.row.confirmed_at)
        self.assertEqual({p for p in Path(self.media).rglob("*") if p.is_file()}, before)

    def test_foreign_actor_has_no_detail_or_write_access(self):
        from django.contrib.auth import get_user_model
        self.failed()
        self.client.force_authenticate(get_user_model().objects.create_user(username="correction-outsider"))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.post().status_code, 404)

    def test_cancel_uncertainty_claim_success_and_ignore_are_readonly(self):
        self.failed()
        for changes in ({"status": "cancelled"}, {"status": "uncertain"}, {"status": "succeeded"},
                {"claim_token": uuid4()}, {"ignore_late_result": True}):
            with self.subTest(changes=changes), transaction.atomic():
                AssetGenerationJob.objects.filter(pk=self.job.pk).update(**changes)
                detail = self.client.get(self.url)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertFalse(detail.data["text_response_review"]["editable"])
                self.assertEqual(self.post().status_code, 409)
                transaction.set_rollback(True)

    def test_new_retry_blocks_old_response_correction(self):
        from studio.services.asset_generation import retry_asset_generation_job
        job = self.failed()
        child, _ = retry_asset_generation_job(job, client_idempotency_key="isolated-explicit-retry")
        self.assertEqual(child.retry_of_id, job.pk)
        self.assertFalse(self.client.get(self.url).data["text_response_review"]["editable"])
        self.assertEqual(self.post().status_code, 409)

    def test_file_corruption_or_transaction_failure_never_replaces_original(self):
        self.failed()
        before = {p: p.read_bytes() for p in Path(self.media).rglob("*") if p.is_file()}
        with patch.object(ExecutionPreparation, "save", side_effect=ValidationError("isolated rollback")):
            self.assertEqual(self.post().status_code, 400)
        self.row.refresh_from_db(); self.job.refresh_from_db()
        self.assertIsNone(self.row.confirmed_at)
        self.assertEqual(self.job.status, "failed")
        self.assertEqual({p: p.read_bytes() for p in Path(self.media).rglob("*") if p.is_file()}, before)
        with patch("studio.services.execution_response_storage.read_response", side_effect=ValidationError("isolated corrupt")):
            self.assertEqual(self.post().status_code, 400)

    def test_known_partial_body_can_be_completed_without_new_call(self):
        self.driver.invoke.side_effect = self.budget_error()
        self.failed()
        self.assertEqual(self.post().status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.original_text, self.budget_error().partial_text)
        self.driver.invoke.assert_called_once()


class ComfyAssetTextCorrectionTests(TestCase):
    setUp = comfy.ComfyAssetTextResponseRetentionTests.setUp
    smoke = comfy.ComfyAssetTextResponseRetentionTests.smoke
    job = comfy.ComfyAssetTextResponseRetentionTests.job
    cycle = comfy.ComfyAssetTextResponseRetentionTests.cycle
    output = comfy.ComfyAssetTextResponseRetentionTests.output

    def test_script_asset_uses_verified_human_provenance_not_model_output(self):
        job, prep = self.job(job_type="asset", media_type="script")
        self.peer.invoke.return_value = self.output("原文\x00不可直接保存")
        result = self.cycle(job)
        self.assertEqual(result.status, "failed", result.error_message)
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        prep.refresh_from_db()
        kwargs = dict(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            review_id=row.pk, original_sha256=row.original_sha256, text="人工剧本\r\n第二行", actor=self.user)
        with patch("studio.services.asset_generation.driver_for", side_effect=AssertionError("no external call")):
            complete_text_response(**kwargs)
        job.refresh_from_db(); row.refresh_from_db()
        asset = job.output_asset
        self.assertEqual(asset.metadata["source"], "human_correction")
        self.assertEqual(asset.metadata["text_response_correction"]["review_id"], str(row.pk))
        self.assertNotIn("generated_sha256", asset.metadata)
        self.assertIn("人工补正", asset.source_info)
        with asset.file.open("rb") as stream:
            self.assertEqual(stream.read().decode(), "人工剧本\n第二行")
        self.assertEqual(row.corrected_text, kwargs["text"])
        self.assertFalse(ExecutionCapacityObservation.objects.filter(preparation=prep).exists())
        complete_text_response(**kwargs)
        self.assertEqual(Asset.objects.count(), 1)

    def test_script_file_and_confirmation_roll_back_together(self):
        job, prep = self.job(job_type="asset", media_type="script")
        self.peer.invoke.return_value = self.output("")
        self.cycle(job)
        row = ExecutionTextResponseReview.objects.get(asset_job=job)
        prep.refresh_from_db()
        from django.conf import settings
        before = {p for p in Path(settings.MEDIA_ROOT).rglob("*") if p.is_file()}
        with patch.object(ExecutionPreparation, "save", side_effect=ValidationError("isolated rollback")):
            with self.assertRaises(ValidationError):
                complete_text_response(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
                    review_id=row.pk, original_sha256=row.original_sha256, text="人工正文", actor=self.user)
        self.assertFalse(Asset.objects.exists())
        row.refresh_from_db(); self.assertIsNone(row.confirmed_at)
        self.assertEqual({p for p in Path(settings.MEDIA_ROOT).rglob("*") if p.is_file()}, before)
