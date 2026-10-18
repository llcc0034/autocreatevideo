"""Formal Smoke API and original text worker with an isolated peer only.

These tests exercise real persisted preparation/admission/queue/evidence. They
do not contact the configured provider or certify any live deployment.
"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionPreparation, ModelDeploymentCapabilitySnapshot, ModelDeploymentSmokeAttempt
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_preparation_queue import advance_next_execution_preparation
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_smoke_evidence import read_instance_smoke_facts
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_smoke_evidence as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionSmokeActionTests(TestCase):
    setUpTestData = fixtures.ExecutionSmokeEvidenceTests.__dict__["setUpTestData"]
    version = fixtures.ExecutionSmokeEvidenceTests.version

    def setUp(self):
        fixtures.ExecutionSmokeEvidenceTests.setUp(self)
        self.enterContext(patch("studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials"))
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config, "pricing": {"fixed_per_request": "0.1", "currency": "USD"}})
        provider.save()
        self.body = {"expected_definition_sha256": self.definition.sha256,
            "fixture": {"inputs": {}, "parameters": {"seed": 20},
                "manual_channels": {"positive_prompt": "Describe a blue parcel."}, "subject_facts": [], "source_language": "", "target_language": ""},
            "field_values": [], "allow_external_calls": True}
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/model-execution-instance-versions/{self.execution.pk}/smoke-test/"

    def create(self, key="smoke-fixture", **changes):
        return create_instance_smoke(version_id=self.execution.pk, actor=self.actor, client_idempotency_key=key, **{**deepcopy(self.body), **changes})

    def advance(self, prep):
        prep.refresh_from_db()
        return advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor, human_initiated=True)

    def test_create_and_replay_use_one_original_attempt_without_a_fake_asset_or_deadline(self):
        attempt, prep, created = self.create()
        self.assertTrue(created)
        self.assertEqual((attempt.scope, attempt.status, prep.purpose), ("instance", "preparing", "instance_smoke"))
        self.assertEqual(prep.deployment_smoke_id, attempt.pk)
        self.assertIsNone(attempt.deadline_started_at)
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.estimated_cost)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        original = deepcopy(prep.contract.resolved_inputs)
        again, replay, created_again = self.create()
        self.assertFalse(created_again)
        self.assertEqual((again.pk, replay.pk, replay.contract.resolved_inputs), (attempt.pk, prep.pk, original))
        with self.assertRaises(ValidationError) as caught:
            self.create(allow_external_calls=False)
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")

    def test_formal_api_returns_location_and_rejects_unknown_bypass_or_non_admin(self):
        response = self.client.post(self.url, self.body, format="json", HTTP_IDEMPOTENCY_KEY="api-smoke")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertIn("/execution-preparations/", response["Location"])
        self.assertEqual(response.data["status"], "preparing")
        replay = self.client.post(self.url, self.body, format="json", HTTP_IDEMPOTENCY_KEY="api-smoke")
        self.assertEqual((replay.status_code, replay.data["attempt_id"]), (200, response.data["attempt_id"]))
        for extra in ({"scope": "resource"}, {"skip_checks": True}, {"allow_external_calls": "true"}):
            rejected = self.client.post(self.url, {**self.body, **extra}, format="json", HTTP_IDEMPOTENCY_KEY="rejected")
            self.assertEqual(rejected.status_code, 400, rejected.data)
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.assertEqual(self.client.post(self.url, self.body, format="json", HTTP_IDEMPOTENCY_KEY="non-admin").status_code, 403)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)

    def test_common_scanner_queues_without_own_pass_then_original_worker_writes_only_combo_result(self):
        attempt, prep, _ = self.create()
        self.assertEqual(advance_next_execution_preparation().status, "dispatched")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "queued")
        self.assertIsNotNone(attempt.deadline_started_at)
        self.assertIsNone(attempt.submission_started_at)
        peer = Mock()
        peer.invoke.return_value = {"text": "A blue parcel lies on the table.", "request_id": "isolated-text-request"}
        claim = claim_next_deployment_smoke("instance-worker")
        self.assertEqual(claim.pk, attempt.pk)
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[1], "chat_completion")
        self.assertEqual(result.result["evidence"]["result"]["text"], peer.invoke.return_value["text"])
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition)[0].attempt_id, str(attempt.pk))
        self.assertIsNone(result.final_snapshot_id)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertEqual(self.advance(prep).status, "dispatched")
        self.assertIsNone(claim_next_deployment_smoke("no-repeat"))

    def test_authorization_or_budget_block_preserves_prepared_inputs(self):
        attempt, prep, _ = self.create(allow_external_calls=False)
        original = deepcopy(prep.contract.resolved_inputs)
        blocked = self.advance(prep)
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.reason_code, "execution_external_authorization_required")
        self.assertIsNone(blocked.request_frozen_at)
        attempt.allow_external_calls = True
        attempt.save()
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
        values = deepcopy(read_resource_policy().value["values"])
        values["budget_by_currency"] = {"USD": "0.01"}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: values}, created_by=self.actor, change_summary="isolated review policy")
        publish_config_revision(revision, published_by=self.actor)
        waiting = self.advance(prep)
        self.assertEqual(waiting.reason_code, "execution_resource_confirmation_required")
        from studio.services.execution_resource_policy import confirm_resource_review
        confirm_resource_review(preparation_id=waiting.pk, expected_preparation_revision=waiting.revision,
            fingerprint=waiting.contract.evaluation["root_checks"]["resource_review"]["fingerprint"], actor=self.actor)
        self.assertEqual(self.advance(prep).status, "dispatched")
        prep.refresh_from_db()
        self.assertEqual(prep.contract.resolved_inputs, original)

    def test_strict_capacity_allows_only_formal_main_smoke_and_records_warning(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        revision = create_config_revision(scope_type="system", values={"execution.admission.capacity_policy": "require_evidence"},
            created_by=self.actor, change_summary="Isolated strict main Smoke policy")
        publish_config_revision(revision, published_by=self.actor)
        attempt, prep, _ = self.create()
        self.assertEqual(self.advance(prep).status, "dispatched")
        prep.refresh_from_db()
        decision = prep.contract.evaluation["admission"]
        self.assertTrue(decision["own_capacity_gate_exempt"])
        self.assertEqual(decision["capacity_state"], "unknown")
        self.assertIn("execution_capacity_unknown", {row["code"] for row in decision["warnings"]})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "queued")
        self.assertIsNone(attempt.submission_started_at)
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())

    def test_recovered_submitting_task_records_uncertainty_without_calling_again(self):
        attempt, prep, _ = self.create()
        self.advance(prep)
        claim = claim_next_deployment_smoke("recovered")
        from studio.services.execution_smoke_dispatch import begin_smoke_execution_submission
        permitted, submitting = begin_smoke_execution_submission(claim)
        self.assertTrue(permitted)
        with patch("studio.services.execution_smoke_worker.driver_for") as peer:
            result = process_deployment_smoke(submitting)
        peer.assert_not_called()
        self.assertEqual((result.status, result.error_code), ("uncertain", "submission_uncertain"))
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())
        with self.assertRaises(ValidationError):
            self.create(key="no-automatic-retry")

    def test_invalid_response_is_failed_not_a_pass_or_automatic_retry(self):
        attempt, prep, _ = self.create()
        self.advance(prep)
        claim = claim_next_deployment_smoke("invalid-response")
        peer = Mock()
        peer.invoke.return_value = {"text": "", "request_id": "invalid-fixture"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "failed")
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition)[0].status, "failed")
        self.assertIsNone(claim_next_deployment_smoke("no-retry"))
