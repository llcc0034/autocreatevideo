"""Derived samples on real original tasks; peers/measurement are isolated.

The complete-token test uses an explicit measurement boundary fixture. It is
not a tokenizer implementation or real model/GPU capacity certification.
"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionCapacityObservation
from studio.services.asset_generation import claim_next_text_asset_job, process_text_asset_job
from studio.services.execution_capacity_observations import capacity_observation_request, record_completed_capacity_observation, read_preparation_capacity_observation, read_capacity_coverage, capacity_sample_candidates
from studio.services.execution_capacity_signatures import describe_execution_capacity
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_asset_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionCapacityObservationTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetDispatchTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionAssetDispatchTests.setUp
    version = fixtures.ExecutionAssetDispatchTests.version
    attempt = fixtures.ExecutionAssetDispatchTests.attempt
    finish = fixtures.ExecutionAssetDispatchTests.finish
    finalize = fixtures.ExecutionAssetDispatchTests.finalize
    passed_fixture = fixtures.ExecutionAssetDispatchTests.passed_fixture
    ready = fixtures.ExecutionAssetDispatchTests.ready
    dispatch = fixtures.ExecutionAssetDispatchTests.dispatch

    def execute(self):
        self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("capacity-business-worker")
        peer = Mock()
        peer.invoke.return_value = {"text": "A blue box.", "request_id": "isolated-capacity"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            ended = process_text_asset_job(claim)
        self.assertEqual(ended.status, "succeeded", ended.error_message)
        return ended

    def test_success_records_partial_dimensions_once_without_becoming_a_smoke(self):
        ended = self.execute()
        sample = ExecutionCapacityObservation.objects.get()
        self.assertEqual(sample.preparation_id, self.prep.pk)
        self.assertEqual(sample.observed_at, ended.finished_at)
        self.assertGreaterEqual(sample.elapsed_ms, 0)
        self.assertIsNone(sample.signature_sha256)
        self.assertEqual(sample.details["capacity"]["state"], "unknown")
        self.assertEqual(sample.preparation.purpose, "production")
        with transaction.atomic():
            repeated = record_completed_capacity_observation(ended, capacity_observation_request(ended))
        self.assertEqual(repeated.pk, sample.pk)
        self.assertEqual(ExecutionCapacityObservation.objects.count(), 1)
        self.assertIsNone(ended.actual_cost)

    def test_preparation_http_returns_actual_sample_and_never_approves_partial_capacity(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{self.prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["capacity_sample"])
        self.execute()
        response = client.get(f"/api/v1/execution-preparations/{self.prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()["capacity_sample"]
        self.assertFalse(data["dimensions_complete"])
        self.assertIsNone(data["signature_sha256"])
        self.assertIn("text.input_tokens_and_tokenizer", data["missing_dimensions"])
        self.assertNotIn("can_submit", data)

    def test_original_terminal_and_snapshot_integrity_cannot_be_replaced(self):
        ended = self.execute()
        sample = ExecutionCapacityObservation.objects.get()
        sample.elapsed_ms += 1
        with self.assertRaises(ValidationError):
            sample.save()
        with self.assertRaises(ValidationError):
            ExecutionCapacityObservation.objects.filter(pk=sample.pk).update(elapsed_ms=1)
        request = capacity_observation_request(ended)
        ended.status = "cancelled"
        with self.assertRaises(ValidationError):
            record_completed_capacity_observation(ended, request)

    def test_partial_signature_does_not_scan_other_samples_and_exact_lookup_uses_index(self):
        self.execute()
        description = json_contract(ExecutionCapacityObservation.objects.get().details["capacity"])
        with patch("studio.services.execution_capacity_observations.execution_configuration_max_bytes", return_value=65536):
            with self.assertNumQueries(0):
                result = read_capacity_coverage(self.execution.pk, description)
        self.assertEqual(result.value["state"], "unknown")
        self.assertIsNone(result.value["observation_id"])
        query = capacity_sample_candidates(self.execution.pk, "a" * 64)
        self.assertNotIn('"details"', str(query.query))
        if connection.vendor == "sqlite":
            plan = query.explain()
            self.assertIn("idx_exec_capacity_sample", plan)
            self.assertNotIn("TEMP B-TREE", plan)

    @staticmethod
    def measured(definition, transport, *, runtime):
        result = deepcopy(describe_execution_capacity(definition, transport, runtime=runtime).value)
        result["known_dimensions"]["input_tokens"] = 128  # Explicit isolated tokenizer observation fixture.
        result["missing_dimensions"] = [key for key in result["missing_dimensions"] if key != "text.input_tokens_and_tokenizer"]
        if result["missing_dimensions"]:
            raise AssertionError("Fixture did not supply all non-token dimensions")
        result["state"], result["reason"] = "uncovered", "capacity_sample_missing"
        result["signature_sha256"] = json_contract({"schema_version": 1, "signature_key": "capacity_signature_v1",
            "definition_sha256": definition.sha256, "target_fingerprint": transport.value["target_fingerprint"],
            "dimensions": result["known_dimensions"]}).sha256
        return json_contract(result)

    def test_complete_fixture_matches_only_exact_spec_and_keeps_business_scope(self):
        # Both submission and original completion see the same explicitly
        # measured token fixture. Production code still reports tokens unknown.
        with patch("studio.services.execution_asset_admission.describe_execution_capacity", side_effect=self.measured), \
                patch("studio.services.execution_capacity_observations.describe_execution_capacity", side_effect=self.measured):
            self.execute()
            sample = ExecutionCapacityObservation.objects.get()
            description = json_contract(sample.details["capacity"])
            matched = read_capacity_coverage(self.execution.pk, description).value
            self.assertEqual((matched["state"], matched["observation_id"]), ("covered", str(sample.pk)))
            self.assertEqual(sample.preparation.purpose, "production")
            different = deepcopy(description.value)
            different["known_dimensions"]["parameters"]["max_tokens"] = 64
            different["signature_sha256"] = "b" * 64
            self.assertEqual(read_capacity_coverage(self.execution.pk, json_contract(different)).value["state"], "uncovered")
            self.assertEqual(read_capacity_coverage(self.execution.pk, description).value["state"], "covered")
            different["signature_sha256"] = description.value["signature_sha256"]
            with self.assertRaises(ValidationError):
                read_capacity_coverage(self.execution.pk, json_contract(different))

    def test_deleting_source_preparation_cascades_derived_sample(self):
        self.execute()
        self.prep.delete()
        self.assertFalse(ExecutionCapacityObservation.objects.exists())
