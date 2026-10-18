"""Read-only HTTP/query tests; worker outcomes are isolated protocol fixtures."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ModelDeploymentSmokeAttempt
from studio.services.execution_smoke_queries import instance_smoke_history, project_instance_smoke_history
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_smoke_evidence as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionSmokeQueryTests(TestCase):
    setUpTestData = fixtures.ExecutionSmokeEvidenceTests.__dict__["setUpTestData"]
    version = fixtures.ExecutionSmokeEvidenceTests.version
    attempt = fixtures.ExecutionSmokeEvidenceTests.attempt
    finish = fixtures.ExecutionSmokeEvidenceTests.finish

    def setUp(self):
        fixtures.ExecutionSmokeEvidenceTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/model-execution-instance-versions/{self.execution.pk}/evidence/"

    def test_empty_summary_and_original_preparing_attempt_do_not_assert_admission(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["count"], response.data["summary"]["evidence_state"]), (0, "missing"))
        self.assertIsNone(response.data["summary"]["latest_completed_attempt_id"])
        attempt, prep = self.attempt(frozen=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        self.assertEqual((row["attempt_id"], row["preparation_id"]), (str(attempt.pk), str(prep.pk)))
        self.assertEqual((row["status"], row["preparation_status"]), ("preparing", prep.status))
        self.assertIsNone(row["request_sha256"])
        self.assertIsNone(row["actual_cost"])
        self.assertNotIn("can_submit", response.data["summary"])

    def test_filter_and_page_never_hide_latest_failure_or_mix_resource_evidence(self):
        first, _ = self.attempt()
        self.finish(first)
        failed, _ = self.attempt()
        self.finish(failed, "failed")
        ModelDeploymentSmokeAttempt.objects.create(scope="resource", deployment=self.deployment,
            operation=self.execution.operation, status="succeeded")
        self.attempt(frozen=False)
        response = self.client.get(self.url, {"status": "succeeded", "page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["attempt_id"], str(first.pk))
        summary = response.data["summary"]
        self.assertEqual((summary["evidence_state"], summary["latest_completed_attempt_id"]), ("failed", str(failed.pk)))
        self.assertEqual(summary["identity"]["definition_sha256"], self.definition.sha256)
        self.assertEqual(summary["identity"]["operation"], self.execution.operation)
        page = self.client.get(self.url, {"page_size": 1})
        self.assertEqual(page.data["count"], 3)
        self.assertIsNotNone(page.data["next"])
        next_page = self.client.get(self.url, {"page_size": 1, "page": 2})
        self.assertNotEqual(page.data["results"][0]["attempt_id"], next_page.data["results"][0]["attempt_id"])

    def test_expiration_uses_frozen_time_and_queries_never_rewrite_or_call_provider(self):
        attempt, _ = self.attempt()
        finished = self.finish(attempt)
        before = deepcopy(finished.result)
        with patch("studio.services.execution_smoke_queries.timezone.now", return_value=finished.evidence_expires_at + timedelta(seconds=1)), \
                patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("Read must not invoke")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["evidence_state"], "expired")
        self.assertEqual(response.data["results"][0]["status"], "succeeded")
        finished.refresh_from_db()
        self.assertEqual(finished.result, before)

    def test_corrupt_latest_result_is_explicit_error_not_fallback_to_old_success(self):
        first, _ = self.attempt()
        self.finish(first)
        second, _ = self.attempt()
        second = self.finish(second, "failed")
        payload = deepcopy(second.result)
        payload["identity"]["definition_sha256"] = "b" * 64
        assign_json_contract(second, "result", payload)
        second.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotIn("results", response.data)

    def test_history_is_one_narrow_indexed_query_independent_of_row_count(self):
        first, _ = self.attempt()
        self.finish(first)
        second, _ = self.attempt()
        self.finish(second)
        queryset = instance_smoke_history(self.execution)
        with CaptureQueriesContext(connection) as queries:
            rows = project_instance_smoke_history(self.execution, list(queryset[:50]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(queries), 1)
        sql = queries[0]["sql"].lower()
        for large_or_secret in ('"result"', '"request_snapshot"', '"provider_request_id"', '"claim_token"', '"prepared_request"'):
            self.assertNotIn(large_or_secret, sql)
        if connection.vendor == "sqlite":
            plan = queryset.explain()
            # Both indexes share the exact-version prefix; SQLite may prefer
            # the unique idempotency index on this small per-version history.
            self.assertRegex(plan, r"USING INDEX (idx_deploy_instance_smoke|uq_deploy_instance_smoke_key) \(execution_instance_version_id=\?\)")
            self.assertIn("uk_exec_prep_deploy_smoke", plan)

    def test_unknown_duplicate_out_of_range_parameters_and_nonadmin_are_rejected(self):
        for query in ("?status=resource", "?scope=resource", "?page_size=201", "?page=0", "?status=failed&status=succeeded"):
            response = self.client.get(self.url + query)
            self.assertEqual(response.status_code, 400, response.data)
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(self.url).status_code, 403)
