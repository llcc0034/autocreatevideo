"""Restricted async instance Smoke through the original queue, isolated peers."""
from datetime import timedelta
from decimal import Decimal
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ModelDeploymentSmokeAttempt, ModelDeploymentCapabilitySnapshot
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke, request_deployment_smoke_cancel
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_smoke_evidence import read_instance_smoke_facts
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
from studio.tests import test_execution_preparation_creation as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAsyncSmokeTests(TestCase):
    setUpTestData = fixtures.ExecutionPreparationCreationTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionPreparationCreationTests.write_contract)
    job_row = fixtures.ExecutionPreparationCreationTests.job_row
    resolve = fixtures.ExecutionPreparationCreationTests.resolve

    def setUp(self):
        fixtures.ExecutionPreparationCreationTests.setUp(self)
        # These worker tests start from an explicitly configured, admitted
        # request. Missing currency policy still requires human confirmation;
        # never bypass that guard or revive a per-request budget input.
        policy = deepcopy(read_resource_policy().value["values"])
        policy["budget_by_currency"] = {"USD": "1.00"}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: policy},
            created_by=self.actor, change_summary="Isolated async worker resource policy")
        publish_config_revision(revision, published_by=self.actor)
        self.attempt, self.prep, _ = create_instance_smoke(version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, fixture=self.intent.value, field_values=[],
            client_idempotency_key="async-combination", allow_external_calls=True, budget_limit=None, actor=self.actor)
        ready = advance_execution_preparation(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.peer = Mock()
        self.enterContext(patch("studio.services.execution_smoke_worker.driver_for", return_value=self.peer))

    def cycle(self):
        ModelDeploymentSmokeAttempt.objects.filter(pk=self.attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_deployment_smoke("async-instance")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.pk, self.attempt.pk)
        return process_deployment_smoke(claim)

    def submit(self):
        self.peer.invoke.return_value = {"request_id": "remote-combination-id", "status": "queued"}
        result = self.cycle()
        self.assertEqual(result.status, "submitted", result.error_code)
        self.assertEqual(self.peer.invoke.call_args.args[1], "submit")
        self.assertEqual(self.peer.invoke.call_args.args[2]["idempotency_key"], str(self.prep.pk))
        return result

    def test_submit_poll_collect_probes_actual_output_and_separately_settles_own_cost(self):
        self.submit()
        self.peer.invoke.side_effect = [
            {"terminal_success": True, "status": "succeeded"},
            {"artifacts": [{"kind": "video", "url": "https://cdn.example.com/isolated.mp4"}], "actual_cost": "0.08", "currency": "USD"},
        ]
        # Media verifier itself has byte/probe tests; this isolates its exact
        # call and prevents network/media writes, not a fabricated live pass.
        with patch("studio.services.remote_artifacts.verify_remote_artifacts_for_smoke", return_value=[{"kind": "video", "sha256": "a" * 64}]) as verify:
            result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_code)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["expected_kind"], "video")
        self.assertEqual((result.actual_cost, result.cost_currency), (Decimal("0.08"), "USD"))
        self.assertEqual(read_instance_smoke_facts(self.version, self.definition)[0].status, "succeeded")
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

    def test_cancel_acknowledgement_does_not_pretend_remote_terminal(self):
        submitted = self.submit()
        cancelled = request_deployment_smoke_cancel(submitted, actor=self.actor)
        self.assertEqual(cancelled.status, "cancel_requested")
        self.peer.invoke.return_value = {"status": "cancelling"}
        result = self.cycle()
        self.assertEqual(result.status, "cancel_requested")
        self.assertIsNone(result.finished_at)
        self.assertEqual(self.peer.invoke.call_args.args[1], "cancel")
        self.peer.invoke.return_value = {"terminal_cancelled": True, "status": "cancelled"}
        result = self.cycle()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.peer.invoke.call_args.args[1], "poll")
        self.assertEqual(read_instance_smoke_facts(self.version, self.definition), ())

    def test_cancel_timeout_keeps_uncertainty_and_does_not_repeat_cancel_or_submit(self):
        submitted = self.submit()
        request_deployment_smoke_cancel(submitted, actor=self.actor)
        ModelDeploymentSmokeAttempt.objects.filter(pk=submitted.pk).update(cancel_requested_at=timezone.now() - timedelta(days=1))
        self.peer.invoke.reset_mock()
        result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("uncertain", "smoke_cancel_timeout"))
        self.peer.invoke.assert_not_called()
        self.assertIsNotNone(result.estimated_cost)
        self.assertIsNone(result.actual_cost)

    def test_missing_remote_receipt_never_reposts(self):
        self.peer.invoke.return_value = {"status": "queued"}
        result = self.cycle()
        self.assertEqual(result.status, "uncertain")
        self.assertIsNone(claim_next_deployment_smoke("no-repost"))
        self.peer.invoke.assert_called_once()
