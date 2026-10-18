"""Smoke parents use real original child Jobs, never a surrogate asset root."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob
from studio.services.asset_generation import claim_next_text_asset_job
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_asset_budget import read_preparation_budget_state
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy, resource_review
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_smoke_dispatch import cancel_prepared_smoke
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_asset_auxiliaries as fixtures
from studio.tests import test_execution_profile_preparation as requests
from studio.tests.test_execution_asset_requests import isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionSmokeAuxiliaryTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetAuxiliaryTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionAssetAuxiliaryTests.write_contract)
    input = staticmethod(fixtures.ExecutionAssetAuxiliaryTests.input)
    bind = fixtures.ExecutionAssetAuxiliaryTests.bind

    def setUp(self):
        fixtures.ExecutionAssetAuxiliaryTests.setUp(self)
        self.fixture_asset = self.root
        self.initial_asset_count = AssetGenerationJob.objects.count()
        self.attempt, self.smoke_prep, _ = create_instance_smoke(version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, fixture=requests.request_for(self), field_values=[],
            client_idempotency_key="smoke-parent-fields", allow_external_calls=True, budget_limit=None, actor=self.actor)

    def advance(self):
        self.smoke_prep.refresh_from_db()
        # This test isolates orchestration. Original budget facts are checked
        # directly below; real no-bypass admission is in smoke_actions tests.
        with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=isolated_observations):
            return advance_execution_preparation(preparation_id=self.smoke_prep.pk,
                expected_preparation_revision=self.smoke_prep.revision, actor=self.actor)

    def test_smoke_uses_original_child_with_exact_parent_and_does_not_submit_main(self):
        result = self.advance()
        self.assertEqual(result.reason_code, "execution_auxiliary_pending")
        child = self.smoke_prep.auxiliary_asset_jobs.get()
        self.assertEqual(child.owner_id, self.smoke_prep.created_by_id)
        self.assertEqual(child.parent_preparation_id, self.smoke_prep.pk)
        self.assertIsNone(self.smoke_prep.asset_job_id)
        self.assertEqual(child.status, "queued")
        self.assertIsNone(self.attempt.deadline_started_at)
        self.assertIsNone(self.attempt.submission_started_at)
        self.advance()
        self.assertEqual(self.smoke_prep.auxiliary_asset_jobs.count(), 1)
        self.assertEqual(AssetGenerationJob.objects.count(), self.initial_asset_count + 1)

    def test_smoke_parent_preserves_missing_child_evidence_wait(self):
        from dataclasses import replace

        def missing_smoke(preparation, definition, request):
            return replace(isolated_observations(preparation, definition, request), smoke_facts=())

        with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=missing_smoke):
            for index in range(3):
                self.smoke_prep.refresh_from_db()
                advance_execution_preparation(preparation_id=self.smoke_prep.pk,
                    expected_preparation_revision=self.smoke_prep.revision, actor=self.actor)
                child = self.smoke_prep.auxiliary_asset_jobs.get()
                prep = child.execution_preparations.get()
                self.assertEqual(prep.reason_code, "execution_smoke_missing")
                snapshot = (prep.revision, prep.contract.evaluation_sha256, prep.evaluated_at)
                if index == 0:
                    original = snapshot
                self.assertEqual(snapshot, original)
                self.assertIsNone(child.submission_started_at)
                self.assertIsNone(prep.request_frozen_at)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submission_started_at)
        self.assertEqual(self.smoke_prep.auxiliary_asset_jobs.count(), 1)

    def test_smoke_and_children_share_budget_without_copying_child_actual_upward(self):
        policy = deepcopy(read_resource_policy().value["values"])
        policy["budget_by_currency"] = {"USD": "1.00"}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: policy},
            created_by=self.actor, change_summary="Isolated Smoke parent budget review")
        publish_config_revision(revision, published_by=self.actor)
        self.advance()
        child = self.smoke_prep.auxiliary_asset_jobs.get()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="succeeded", estimated_cost=Decimal("0.2"), actual_cost=Decimal("0.15"), cost_currency="USD")
        estimate = json_contract({"schema_version": 1, "transport_sha256": "a" * 64, "amount": "0.9", "currency": "USD", "basis": "explicit_fixture"})
        state = read_preparation_budget_state(self.smoke_prep, estimate).value
        self.assertEqual(Decimal(state["projected_total"]), Decimal("1.05"))
        self.assertEqual(state["root_smoke_attempt_id"], str(self.attempt.pk))
        self.assertIsNone(state["root_asset_job_id"])
        self.assertEqual(Decimal(state["budget_limit"]), Decimal("1.00"))
        self.assertEqual(state["blockers"], [])
        # Unit-level review request, not a compiled or submitted provider request.
        # Over-budget exposure requires consent; it is not an accounting failure.
        request = json_contract({"transport": {
            "execution_instance_version_id": str(self.version.pk), "target_fingerprint": self.definition.value["target_fingerprint"],
            "operation": self.version.operation, "driver_type": "isolated-review",
            "idempotency_key": "isolated-smoke-budget-review", "parameters": {"values": {}}, "payload": {}, "output": {}}})
        review = resource_review(self.smoke_prep, request, {"budget": state}, read_resource_policy())
        self.assertEqual([issue["code"] for issue in review["issues"]], ["budget_exceeded"])
        self.assertFalse(review["confirmed"])
        self.assertIsNone(review["confirmation_id"])
        self.assertEqual(Decimal(review["basis"]["projected_total"]), Decimal("1.05"))
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.actual_cost)
        self.assertIsNone(self.attempt.submission_started_at)
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="uncertain", actual_cost=None)
        state = read_preparation_budget_state(self.smoke_prep, estimate).value
        self.assertEqual(Decimal(state["projected_total"]), Decimal("1.1"))
        uncertain_review = resource_review(self.smoke_prep, request, {"budget": state}, read_resource_policy())
        self.assertEqual([issue["code"] for issue in uncertain_review["issues"]], ["budget_exceeded"])
        self.assertNotEqual(uncertain_review["fingerprint"], review["fingerprint"])
        self.assertFalse(uncertain_review["confirmed"])
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.actual_cost)
        self.assertIsNone(self.attempt.submission_started_at)

    def test_new_auxiliary_does_not_inherit_main_smoke_capacity_exemption(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        revision = create_config_revision(scope_type="system", values={"execution.admission.capacity_policy": "require_evidence"},
            created_by=self.actor, change_summary="Isolated strict auxiliary policy")
        publish_config_revision(revision, published_by=self.actor)
        self.advance()
        child = self.smoke_prep.auxiliary_asset_jobs.get()
        child_prep = child.execution_preparations.get()
        self.assertEqual((self.smoke_prep.purpose, child_prep.purpose), ("instance_smoke", "production"))
        self.assertEqual(child_prep.reason_code, "execution_capacity_unknown")
        self.assertFalse(child_prep.contract.evaluation["admission"]["own_capacity_gate_exempt"])
        self.assertIsNone(child_prep.request_frozen_at)
        self.assertEqual(child.status, "preparing")
        self.assertIsNone(claim_next_text_asset_job("no-uncovered-auxiliary"))

    def test_cancellation_cancels_queued_child_and_does_not_restore_parent(self):
        self.advance()
        child = self.smoke_prep.auxiliary_asset_jobs.get()
        result = cancel_prepared_smoke(self.smoke_prep, self.actor)
        self.assertEqual(result.status, "cancelled")
        child.refresh_from_db()
        self.assertEqual(child.status, "cancelled")
        self.assertIsNone(claim_next_text_asset_job("no-child-call"))
        self.assertEqual(self.advance().status, "cancelled")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "cancelled")

    def test_uncertain_child_keeps_parent_cancel_pending_and_budget_reserved(self):
        self.advance()
        child = self.smoke_prep.auxiliary_asset_jobs.get()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="uncertain")
        result = cancel_prepared_smoke(self.smoke_prep, self.actor)
        self.assertEqual(result.status, "cancel_requested")
        self.assertEqual(self.advance().status, "cancelled")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "cancel_requested")
        self.assertEqual(self.smoke_prep.auxiliary_asset_jobs.count(), 1)

    def test_authorized_uncertain_successor_continues_same_smoke_through_generated_field(self):
        from rest_framework.test import APIClient
        from studio.services.execution_preparation_fields import confirm_execution_field
        self.advance()
        old = self.smoke_prep.auxiliary_asset_jobs.get()
        AssetGenerationJob.objects.filter(pk=old.pk).update(status="uncertain", error_code="uncertain_remote_response",
            estimated_cost=Decimal("0.01"), actual_cost=None, cost_currency="USD")
        self.advance(); self.smoke_prep.refresh_from_db()
        client = APIClient(); client.force_authenticate(self.actor)
        payload = {"expected_preparation_revision": self.smoke_prep.revision, "field_batch_key": old.field_batch_key,
            "attempt_number": 2, "acknowledged_uncertain_task_id": str(old.pk)}
        with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=isolated_observations):
            response = client.post(f"/api/v1/execution-preparations/{self.smoke_prep.pk}/retry-field-batch/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        new = self.smoke_prep.auxiliary_asset_jobs.get(attempt_number=2)
        self.batch = self.smoke_prep.contract.resolved_inputs["field_requests"][0]
        fixtures.ExecutionAssetAuxiliaryTests.execute(self, new)
        self.assertEqual(self.advance().status, "awaiting_review")
        field = self.smoke_prep.field_values.get()
        self.assertEqual((field.origin_kind, field.origin_asset_job_id), ("model", new.pk))
        self.smoke_prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.smoke_prep.pk, field_value_id=field.pk,
            expected_preparation_revision=self.smoke_prep.revision, expected_text_sha256=field.text_sha256,
            expected_source_sha256=field.source_sha256, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)
        # The fixture uses manual field review. After approving the generated
        # value, use the same explicit continuation as the formal page.
        self.smoke_prep.refresh_from_db()
        response = client.post(f"/api/v1/execution-preparations/{self.smoke_prep.pk}/continue/",
            {"expected_preparation_revision": self.smoke_prep.revision}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.smoke_prep.refresh_from_db()
        self.assertEqual(self.smoke_prep.reason_code, "execution_resource_confirmation_required")
        response = client.post(f"/api/v1/execution-preparations/{self.smoke_prep.pk}/confirm-resources/",
            {"expected_preparation_revision": self.smoke_prep.revision,
             "fingerprint": self.smoke_prep.contract.evaluation["root_checks"]["resource_review"]["fingerprint"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.smoke_prep.refresh_from_db()
        response = client.post(f"/api/v1/execution-preparations/{self.smoke_prep.pk}/continue/",
            {"expected_preparation_revision": self.smoke_prep.revision}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.smoke_prep.refresh_from_db()
        self.assertEqual(self.smoke_prep.status, "dispatched", self.smoke_prep.reason)
        self.attempt.refresh_from_db(); old.refresh_from_db()
        self.assertEqual(self.attempt.status, "queued")
        self.assertEqual(self.smoke_prep.workflow_smoke_id or self.smoke_prep.deployment_smoke_id, self.attempt.pk)
        self.assertEqual(old.status, "uncertain")
        self.assertIsNone(old.actual_cost)
        self.assertEqual(AssetGenerationJob.objects.count(), self.initial_asset_count + 2)
