"""INT-03: the new authored contract respects the complete request byte cap."""
from unittest.mock import Mock, patch

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob, GenerationAttempt, GenerationTask
from studio.services.execution_advancement import _finalize_or_record_invalid_request, advance_execution_preparation
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests import test_int_three_entry_journey as entries


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationRequestBoundaryTests(IntegrationContracts, TestCase):
    setUp = entries.IntegrationThreeEntryJourneyTests.setUp
    tearDown = entries.IntegrationThreeEntryJourneyTests.tearDown
    request = entries.IntegrationThreeEntryJourneyTests.request

    def test_new_author_contract_full_envelope_overflow_keeps_text_without_call(self):
        source = "Original Café 😀 / 原始正文\r\n" * 8
        created = self.api("post", "/api/v1/asset-generation-jobs/", {
            **self.request(self.primary, {"request.subject": {"kind": "literal", "value": source}}),
            "job_type": "asset", "media_type": "image", "name": "INT envelope limit",
            "image_category": "reference", "field_values": []}, status=202, HTTP_IDEMPOTENCY_KEY="int-envelope-limit")
        job = AssetGenerationJob.objects.get(pk=created["id"])
        prep = job.execution_preparations.get()
        prep = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        base = f"/api/v1/execution-preparations/{prep.pk}/"
        before = self.api("get", base + "prompt-display/", {"expected_preparation_revision": prep.revision})
        text = next(row["text"] for row in before["channels"] if row["key"] == "positive_prompt")
        self.assertIn(source, text)
        self.assertGreater(len(text.encode("utf-8")), len(text))
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        request = json_contract({"schema_version": 1, "preparation_id": str(prep.pk),
            "basis_preparation_revision": prep.revision, "intent_sha256": prep.intent_sha256,
            "field_selections": preview.field_selections.value, "transport": preview.transport.value})
        maximum = request.size_bytes - 1
        self.assertLess(preview.transport.size_bytes, maximum)
        admission = Mock(side_effect=AssertionError("Full-envelope overflow precedes admission"))
        with patch("studio.services.execution_request_storage.boundary_int",
                side_effect=lambda key: maximum if key == "security.snapshot.stage_max_bytes" else boundary_int(key)), patch.object(
                default_storage, "save", side_effect=AssertionError("Rejected envelope creates no frozen file")) as storage:
            prep = _finalize_or_record_invalid_request(prep, self.actor, admission)
            self.assertEqual((prep.status, prep.reason_code), ("blocked", "execution_request_storage_invalid"))
            after = self.api("get", base + "prompt-display/", {"expected_preparation_revision": prep.revision})
            for key in ("channels", "field_basis", "length_diagnostics"):
                self.assertEqual(after[key], before[key])
            metrics = self.api("get", base + "storage-metrics/", {"expected_preparation_revision": prep.revision})
            item = next(row for row in metrics["items"] if row["key"] == "prepared_request")
            self.assertEqual(item["current_limit_bytes"], maximum)
            self.assertEqual(item["state"], "not_generated")
            self.assertIsNone(item["sha256"])
            revision = prep.revision
            for _ in range(2):
                prep = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
                self.assertEqual(prep.revision, revision)
            admission.assert_not_called()
            storage.assert_not_called()
        prep.refresh_from_db()
        job.refresh_from_db()
        self.assertIsNone(prep.request_frozen_at)
        self.assertIsNone(prep.request_sha256)
        self.assertIsNone(job.started_at)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
