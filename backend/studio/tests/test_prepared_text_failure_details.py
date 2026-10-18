"""Transport diagnostics on original prepared tasks, with isolated replies."""
import hashlib
import json
from unittest.mock import patch

from django.db import connection, OperationalError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import GeneratedArtifact, GenerationAttempt, GenerationAttemptResolution
from studio.services.provider_registry import ProviderDriverError, safe_chat_failure_details
from studio.tests import test_execution_production_dispatch as fixtures


def response_failure(*, uncertain=False):
    error = ProviderDriverError("TOP-SECRET raw response", code="provider_response_invalid", submission_uncertain=uncertain)
    error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length",
        "usage": {"prompt_tokens": 321, "completion_tokens": 8192, "total_tokens": 8513, "cached_tokens": 77},
        "response_id_sha256": hashlib.sha256(b"isolated-request-id").hexdigest(),
        "raw_message": "TOP-SECRET metadata", "provider_payload": {"secret": "must-not-persist"}}
    return error


class ChatFailureProjectionTests(TestCase):
    def test_only_registered_diagnostics_and_bounded_usage_survive(self):
        error = response_failure()
        result = safe_chat_failure_details(error)
        self.assertEqual(set(result), {"contract_error_code", "finish_reason", "usage", "response_id_sha256"})
        self.assertEqual(result["usage"], {"prompt_tokens": 321, "completion_tokens": 8192, "total_tokens": 8513})
        self.assertEqual(result["response_id_sha256"], error.metadata["response_id_sha256"])
        error.metadata = {"contract_error_code": [], "finish_reason": {"secret": "value"},
            "usage": {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 10 ** 50}, "response_id_sha256": "raw-id"}
        self.assertEqual(safe_chat_failure_details(error), {})
        error.metadata = ["untrusted"]
        self.assertEqual(safe_chat_failure_details(error), {})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class PreparedProductionFailureTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    setUp = fixtures.ProductionDispatchTests.setUp
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    prepare = fixtures.ProductionDispatchTests.prepare
    structure_instance = fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process
    queued = fixtures.ProductionDispatchTests.queued
    visual_root = fixtures.ProductionDispatchTests.visual_root

    def assert_rejected_evidence(self):
        prep, _ = self.queued()
        result, peer = self.process(effect=response_failure())
        self.assertEqual((result.status, result.error_code), ("failed", "execution_provider_failed"))
        resolution = result.resolutions.get()
        self.assertEqual(resolution.outcome, "known_response_rejected")
        self.assertEqual(resolution.evidence, {"schema_version": 1, "preparation_id": str(prep.pk),
            "request_sha256": result.request_sha256, **safe_chat_failure_details(response_failure())})
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertIsNone(result.actual_cost)
        persisted = json.dumps({"evidence": resolution.evidence, "note": resolution.note, "message": result.error_message})
        for text in ("TOP-SECRET", "must-not-persist", "cached_tokens", "raw_message", "provider_payload"):
            self.assertNotIn(text, persisted)
        peer.invoke.assert_called_once()
        from studio.services.execution_production_text_worker import process_prepared_production_text
        process_prepared_production_text(result)
        self.assertEqual(result.resolutions.count(), 1)
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        diagnostic = response.data["response_diagnostic"]
        self.assertEqual(diagnostic["state"], "recorded")
        self.assertEqual(str(diagnostic["task_id"]), str(result.pk))
        self.assertIn("输出长度额度", diagnostic["summary"])
        self.assertIn({"label": "输出 token", "value": "8192"}, diagnostic["details"])
        for text in ("TOP-SECRET", "must-not-persist", "provider_request_id"):
            self.assertNotIn(text, json.dumps(diagnostic))
        from studio.services.execution_response_diagnostics import read_preparation_response_diagnostic
        from studio.services.security_boundaries import security_boundary_resolution_cache
        prep.refresh_from_db()
        with security_boundary_resolution_cache(), CaptureQueriesContext(connection) as captured:
            exact = read_preparation_response_diagnostic(prep)
        self.assertEqual(exact["task_id"], result.pk)
        task_queries = [row["sql"] for row in captured if 'FROM "studio_generationattempt' in row["sql"]]
        self.assertEqual(len(task_queries), 2)
        for sql in task_queries:
            for column in ("request_snapshot", "provider_request_id", '"note"'):
                self.assertNotIn(column, sql)
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    self.assertIn("INDEX", str(cursor.fetchall()).upper())
        # Damaged original evidence must not be repaired or hidden as absence.
        GenerationAttemptResolution.objects.filter(pk=resolution.pk).update(evidence_sha256="0" * 64)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["response_diagnostic"]["state"], "unverifiable")
        self.assertEqual(response.data["response_diagnostic"]["details"], [])
        resolution.refresh_from_db()
        self.assertEqual(resolution.evidence_sha256, "0" * 64)
        peer.invoke.assert_called_once()

    def test_script_generation_preserves_safe_failure_on_original_attempt(self):
        self.assert_rejected_evidence()

    def test_success_usage_is_original_audit_not_billing_or_content_approval(self):
        from decimal import Decimal
        from rest_framework.test import APIClient
        from studio.services.execution_text_response_usage import read_text_response_usage
        from studio.services.security_boundaries import security_boundary_resolution_cache
        from studio.services.text_generation_queue import process_text_attempt
        prep, _ = self.queued()
        response = self.response()
        response.update(usage={"prompt_tokens": 321, "completion_tokens": 0, "total_tokens": 321,
            "cached_tokens": 77, "secret": "TOP-SECRET"}, raw_response="TOP-SECRET")
        result, peer = self.process(response=response)
        self.assertEqual(result.status, "succeeded")
        receipt = result.resolutions.get(outcome="response_received")
        self.assertEqual(receipt.evidence["usage"], {"prompt_tokens": 321, "completion_tokens": 0, "total_tokens": 321})
        self.assertNotIn("TOP-SECRET", json.dumps(receipt.evidence))
        self.assertIsNone(result.actual_cost)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        self.assertFalse(self.run.stages.filter(approval_decisions__isnull=False).exists())
        process_text_attempt(result)
        self.assertEqual(result.resolutions.count(), 1)
        peer.invoke.assert_called_once()
        with security_boundary_resolution_cache(), CaptureQueriesContext(connection) as queries:
            value, note = read_text_response_usage(result)
        self.assertEqual(value["usage"]["completion_tokens"], 0)
        self.assertIn("不是实际扣费", note)
        sql = [q["sql"] for q in queries if 'FROM "studio_generationattemptresolution"' in q["sql"]]
        self.assertEqual(len(sql), 1)
        self.assertNotIn('"note"', sql[0]); self.assertNotIn('"provider_request_id"', sql[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql[0])
                self.assertIn("INDEX", str(cursor.fetchall()).upper())
        client = APIClient(); client.force_authenticate(self.actor)
        detail = client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["output"]["result"]["provider_response"]["usage"], receipt.evidence["usage"])
        self.assertEqual(detail.data["output"]["state"], "available")
        self.assertIsNone(detail.data["actual_cost"])
        from studio.services.json_contracts import json_contract
        for envelope_version, body_version in ((2, 1), (1, 2), (1, True)):
            with self.subTest(envelope_version=envelope_version, body_version=body_version):
                unsupported = json_contract({**receipt.evidence, "schema_version": body_version},
                    schema_version=envelope_version)
                GenerationAttemptResolution.objects.filter(pk=receipt.pk).update(evidence=unsupported.value,
                    evidence_schema_version=unsupported.schema_version,
                    evidence_size_bytes=unsupported.size_bytes, evidence_sha256=unsupported.sha256)
                value, _ = read_text_response_usage(result)
                self.assertEqual(value["state"], "unverifiable")
                self.assertEqual(value["usage"], {})
        foreign = json_contract({**receipt.evidence, "preparation_id": "foreign-preparation"})
        GenerationAttemptResolution.objects.filter(pk=receipt.pk).update(evidence=foreign.value,
            evidence_schema_version=foreign.schema_version,
            evidence_size_bytes=foreign.size_bytes, evidence_sha256=foreign.sha256)
        value, _ = read_text_response_usage(result)
        self.assertEqual(value["state"], "unverifiable")
        GenerationAttemptResolution.objects.filter(pk=receipt.pk).update(evidence_sha256="0" * 64)
        corrupt = client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
        self.assertEqual(corrupt.data["output"]["result"]["provider_response"]["state"], "unverifiable")
        self.assertEqual(corrupt.data["output"]["state"], "available")
        self.assertEqual(corrupt.data["output"]["result"]["provider_response"]["usage"], {})

    def test_invalid_response_retains_usage_without_success_or_automatic_retry(self):
        self.queued()
        result, peer = self.process(response={"text": "invalid json", "request_id": "isolated-rejected",
            "usage": {"prompt_tokens": 21, "completion_tokens": 3, "total_tokens": 24}})
        self.assertEqual(result.status, "awaiting_review")
        self.assertEqual(result.resolutions.get().evidence["usage"]["total_tokens"], 24)
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertIsNone(result.actual_cost)
        peer.invoke.assert_called_once()

    def test_missing_or_untrusted_usage_stays_unknown_and_duplicate_receipts_are_not_selected(self):
        from studio.services.execution_text_response_usage import read_text_response_usage
        from studio.services.text_generation_queue import append_attempt_resolution
        self.queued()
        response = self.response()
        response["usage"] = {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 10 ** 50}
        result, _ = self.process(response=response)
        value, note = read_text_response_usage(result)
        self.assertEqual(value["state"], "recorded"); self.assertEqual(value["usage"], {})
        self.assertIn("未知", note)
        receipt = result.resolutions.get()
        append_attempt_resolution(result, outcome="response_received", evidence=receipt.evidence)
        value, _ = read_text_response_usage(result)
        self.assertEqual(value["state"], "unverifiable")
        self.assertEqual(result.resolutions.count(), 2)  # Read did not repair originals.

    def test_absent_receipt_is_unknown_and_success_audit_failure_rolls_back_without_resending(self):
        from studio.services.execution_text_response_usage import read_text_response_usage
        _, attempt = self.queued()
        value, note = read_text_response_usage(attempt)
        self.assertEqual(value["state"], "unavailable"); self.assertEqual(value["usage"], {})
        self.assertIn("未知", note)
        with patch("studio.services.text_generation_queue.append_attempt_resolution", side_effect=OperationalError("isolated audit outage")):
            with self.assertRaises(OperationalError):
                self.process()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitting")
        self.assertIsNotNone(attempt.submission_started_at)
        self.assertFalse(attempt.resolutions.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_script_structure_uses_same_failure_projection(self):
        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.assert_rejected_evidence()

    def test_visual_rules_uses_same_failure_projection(self):
        self.visual_root()
        self.assert_rejected_evidence()

    def test_uncertain_response_cannot_claim_known_rejection(self):
        prep, _ = self.queued()
        result, _ = self.process(effect=response_failure(uncertain=True))
        self.assertEqual(result.error_code, "uncertain_remote_response")
        self.assertFalse(result.resolutions.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())
        from studio.services.execution_response_diagnostics import read_preparation_response_diagnostic
        self.assertIsNone(read_preparation_response_diagnostic(prep))

    def test_evidence_write_failure_rolls_back_settlement_without_resubmission(self):
        _, attempt = self.queued()
        with patch("studio.services.text_generation_queue.append_attempt_resolution", side_effect=OperationalError("isolated database unavailable")):
            with self.assertRaises(OperationalError):
                self.process(effect=response_failure())
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitting")
        self.assertIsNotNone(attempt.submission_started_at)
        self.assertIsNotNone(attempt.claim_token)
        self.assertFalse(attempt.resolutions.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 1)
