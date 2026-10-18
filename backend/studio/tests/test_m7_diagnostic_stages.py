"""M7 stage evidence through original workers and the read-only detail API.

Admission/remote replies are labelled isolated fixtures. No live network,
credentials, persistent database, production retry or adoption is permitted.
Submission markers are not HTTP delivery proof; collect receipts are the
driver's retained object, not raw wire bytes. Writer errors remain distinct.
"""
from copy import deepcopy
import json
import os
import sys
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionTextResponseReview, GeneratedArtifact, GenerationAttempt
from studio.services.async_media_queue import claim_next_async_media_attempt
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.provider_registry import ProviderDriverError, RestrictedAsyncRestDriver
from studio.tests import test_execution_production_media as media
from studio.tests import test_production_rest_collect_receipts as rest
from studio.tests import test_production_media_collect_receipts as comfy


class IsolatedDiagnosticReads:
    @classmethod
    def setUpClass(cls):
        if (not sys.dont_write_bytecode or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"
                or os.environ.get("DATABASE_URL") != "sqlite:///:memory:"
                or connection.vendor != "sqlite" or "memory" not in str(connection.settings_dict["NAME"])):
            raise AssertionError("Use python -B, disabled dotenv and explicit in-memory SQLite")
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                "socket.getaddrinfo", "requests.sessions.Session.request", "studio.services.provider_registry.resolve_credential"):
            guard = cls.enterClassContext(patch(target, side_effect=AssertionError("No external IO in M7 diagnostics")))
            cls.addClassCleanup(guard.assert_not_called)
        super().setUpClass()

    def evidence(self, preparation, attempt):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/v1/task-center/preparation/{preparation.pk}/",
                {"attempt_id": str(attempt.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["source_id"], str(attempt.pk))
        self.assertEqual(response.data["source_kind"], "generation_attempt")
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        return response.data

    def no_candidate(self, detail, code, *, receipt=False):
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["validation"]["error_code"], code)
        self.assertEqual(detail["output"]["state"], "available" if receipt else "unavailable")
        self.assertEqual((detail["output_count"], detail["output"]["media"]), (0, []))
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertEqual(bool(detail["output"]["text"]), receipt)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RestSubmissionDiagnosticTests(IsolatedDiagnosticReads, TestCase):
    setUpTestData = classmethod(media.ProductionMediaTests.setUpTestData.__func__)
    write_contract = staticmethod(media.ProductionMediaTests.write_contract)
    job_row = media.ProductionMediaTests.job_row
    resolve = media.ProductionMediaTests.resolve
    submit = media.ProductionMediaTests.submit

    def setUp(self):
        media.ProductionMediaTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def test_frozen_preparation_and_rejected_request_have_no_submission_or_result(self):
        before = self.evidence(self.preparation, self.attempt)
        self.assertEqual((before["preparation_status"], before["status"], before["input"]["state"]),
            ("dispatched", "queued", "frozen"))
        self.assertIsNone(before["submission"]["started_at"])
        self.assertIsNone(before["submission"]["registered_at"])
        self.assertEqual(before["output"]["state"], "unavailable")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_snapshot_sha256="0" * 64)
        failed, peer, _ = self.submit()
        peer.invoke.assert_not_called()
        detail = self.evidence(self.preparation, failed)
        self.no_candidate(detail, "async_attempt_invalid")
        self.assertIsNone(detail["submission"]["started_at"])
        self.assertIsNone(detail["submission"]["registered_at"])
        self.assertEqual(detail["input"], before["input"])
        self.assertIsNone(claim_next_async_media_attempt("must-not-resubmit"))

    def test_unknown_submission_retains_original_input_without_inventing_a_receipt(self):
        original = deepcopy(self.evidence(self.preparation, self.attempt)["input"])
        failed, peer, _ = self.submit(effect=ProviderDriverError("isolated lost acknowledgement", submission_uncertain=True))
        peer.invoke.assert_called_once()
        detail = self.evidence(self.preparation, failed)
        self.no_candidate(detail, "uncertain_remote_response")
        self.assertIsNotNone(detail["submission"]["started_at"])
        self.assertIsNone(detail["submission"]["registered_at"])
        self.assertEqual(detail["submission"]["identifier_state"], "not_recorded")
        self.assertIn("尚不确定", detail["outcome"]["message"])
        self.assertEqual(detail["input"], original)
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertIsNone(claim_next_async_media_attempt("must-not-resubmit"))

    def test_registered_submission_then_remote_failure_is_not_local_parse_or_store_success(self):
        submitted, submitter, _ = self.submit()
        middle = self.evidence(self.preparation, submitted)
        self.assertEqual(middle["status"], "submitted")
        self.assertIsNotNone(middle["submission"]["started_at"])
        self.assertIsNotNone(middle["submission"]["registered_at"])
        self.assertEqual(middle["submission"]["identifier_state"], "recorded")
        self.assertEqual(middle["output"]["state"], "unavailable")
        failed, poller, _ = self.submit(response={"status": "failed", "terminal_failure": True})
        self.assertEqual(poller.invoke.call_args.args[1], "poll")
        submitter.invoke.assert_called_once()
        poller.invoke.assert_called_once()
        detail = self.evidence(self.preparation, failed)
        self.no_candidate(detail, "remote_generation_failed")
        self.assertEqual(detail["submission"], middle["submission"])
        self.assertEqual(detail["input"], middle["input"])
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertIsNone(claim_next_async_media_attempt("must-not-resubmit"))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RestCollectDiagnosticTests(IsolatedDiagnosticReads, TestCase):
    setUpTestData = classmethod(rest.ProductionRestCollectReceiptTests.setUpTestData.__func__)
    write_contract = staticmethod(rest.ProductionRestCollectReceiptTests.write_contract)
    job_row = rest.ProductionRestCollectReceiptTests.job_row
    resolve = rest.ProductionRestCollectReceiptTests.resolve
    submit = rest.ProductionRestCollectReceiptTests.submit
    image_attempt = rest.ProductionRestCollectReceiptTests.image_attempt
    image_collect_claim = rest.ProductionRestCollectReceiptTests.image_collect_claim
    download_image = staticmethod(rest.ProductionRestCollectReceiptTests.download_image)
    setUp = rest.ProductionRestCollectReceiptTests.setUp
    cycle = rest.ProductionRestCollectReceiptTests.cycle

    def test_actual_driver_parse_failure_preserves_receipt_not_candidate(self):
        self.peer.invoke.side_effect = RestrictedAsyncRestDriver().invoke
        body = {"status": "succeeded", "artifacts": [], "note": "isolated malformed result"}
        with patch("studio.services.provider_registry._request_json", return_value=body) as transport:
            result = self.cycle()
        detail = self.evidence(self.preparation, result)
        self.no_candidate(detail, "provider_response_invalid", receipt=True)
        self.assertEqual(json.loads(detail["output"]["text"])["source_response"], body)
        self.assertIsNotNone(detail["submission"]["registered_at"])
        self.assertEqual(detail["output"]["result"]["response_review"]["validation_code"], "provider_response_invalid")
        self.download.assert_not_called()
        transport.assert_called_once()
        self.assertIsNone(claim_next_async_media_attempt("must-not-recollect"))

    def test_receipt_storage_failure_cannot_be_reported_as_missing_remote_result(self):
        with patch("studio.services.execution_response_storage.store_response", side_effect=ExecutionResponseStorageError("isolated disk failure")):
            result = self.cycle()
        detail = self.evidence(self.preparation, result)
        self.no_candidate(detail, "execution_response_storage_failed")
        self.assertIsNotNone(detail["submission"]["registered_at"])
        self.assertIn("存储", detail["validation"]["error_message"])
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.peer.invoke.assert_called_once()
        self.download.assert_not_called()
        self.assertIsNone(claim_next_async_media_attempt("must-not-recollect"))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ComfyCollectDiagnosticTests(IsolatedDiagnosticReads, TestCase):
    resolve = comfy.ProductionMediaCollectReceiptTests.resolve
    mark_submitted = comfy.ProductionMediaCollectReceiptTests.mark_submitted
    setUp = comfy.ProductionMediaCollectReceiptTests.setUp
    cycle = comfy.ProductionMediaCollectReceiptTests.cycle

    def test_remote_failure_and_local_output_validation_keep_distinct_original_history(self):
        for remote_failed in (False, True):
            with self.subTest(remote_failed=remote_failed), transaction.atomic():
                self.task.refresh_from_db()
                history = deepcopy(self.history)
                history["outputs"] = {}
                if remote_failed:
                    history["status"] = {"status_str": "error", "completed": False,
                        "messages": [["execution_error", {"exception_message": "isolated node failure"}]]}
                self.peer.history.return_value = history
                result = self.cycle()
                detail = self.evidence(self.preparation, self.attempt)
                self.no_candidate(detail, "execution_workflow_failed" if remote_failed else "execution_output_invalid", receipt=True)
                self.assertEqual(json.loads(detail["output"]["text"])["history"]["status"], history["status"])
                self.assertEqual(result.submission_state, "terminal_failed" if remote_failed else "terminal_succeeded")
                self.assertEqual(detail["links"]["generation_task_id"], str(self.task.pk))
                self.assertIsNotNone(detail["submission"]["registered_at"])
                self.peer.queue_prompt.assert_not_called()
                transaction.set_rollback(True)

    def test_remote_success_with_receipt_storage_failure_remains_failed(self):
        with patch("studio.services.execution_response_storage.store_response", side_effect=ExecutionResponseStorageError("isolated disk failure")):
            result = self.cycle()
        detail = self.evidence(self.preparation, self.attempt)
        self.no_candidate(detail, "execution_response_storage_failed")
        self.assertEqual(result.submission_state, "terminal_succeeded")
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
        self.assertIsNotNone(detail["submission"]["registered_at"])
        self.peer.queue_prompt.assert_not_called()
