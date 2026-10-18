"""Original queue + current admission + controlled peer, never live Smoke."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob
from studio.services.asset_generation import claim_next_text_asset_job, process_text_asset_job, request_asset_generation_cancel, resolve_uncertain_asset_generation_job, _collected_actual_cost
from studio.services.provider_registry import ProviderDriverError
from studio.services.execution_asset_dispatch import dispatch_asset_execution, begin_asset_execution_submission, check_asset_execution_staging
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_asset_admission as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionAssetDispatchTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetAdmissionTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionAssetAdmissionTests.setUp
    version = fixtures.ExecutionAssetAdmissionTests.version
    attempt = fixtures.ExecutionAssetAdmissionTests.attempt
    finish = fixtures.ExecutionAssetAdmissionTests.finish
    finalize = fixtures.ExecutionAssetAdmissionTests.finalize
    passed_fixture = fixtures.ExecutionAssetAdmissionTests.passed_fixture

    def dispatch(self):
        self.prep.refresh_from_db()
        return dispatch_asset_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)

    def policy(self, amount):
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
        values = deepcopy(read_resource_policy().value["values"])
        values["budget_by_currency"] = {"USD": amount}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: values}, created_by=self.actor, change_summary="isolated resource review")
        publish_config_revision(revision, published_by=self.actor)

    def ready(self):
        self.passed_fixture()
        self.finalize()
        self.prep.refresh_from_db()
        return deepcopy(self.prep.contract.prepared_request)

    def test_dispatch_reserves_only_current_call_once_without_starting_remote_clock(self):
        payload = self.ready()
        before = self.prep.revision
        queued = self.dispatch()
        self.assertEqual(queued.status, "queued")
        self.assertEqual(queued.estimated_cost, Decimal("0.1"))
        self.assertIsNone(queued.claim_token)
        self.assertIsNone(queued.submission_started_at)
        replay = dispatch_asset_execution(preparation_id=self.prep.pk, expected_preparation_revision=before, actor=self.actor)
        self.assertEqual(replay.pk, queued.pk)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, before + 1)
        self.assertEqual(self.prep.status, "dispatched")
        self.assertEqual(self.prep.contract.prepared_request, payload)

    def test_dispatch_uses_current_budget_and_keeps_frozen_fields_when_blocked(self):
        payload = self.ready()
        self.policy("0.01")
        blocked = self.dispatch()
        self.assertEqual((blocked.status, blocked.error_code), ("preparing", "execution_resource_confirmation_required"))
        self.assertIsNone(claim_next_text_asset_job("not-ready"))
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "ready")
        self.assertEqual(self.prep.contract.evaluation["status"], "blocked")
        self.assertEqual(self.prep.contract.prepared_request, payload)
        self.policy("1")
        self.assertEqual(self.dispatch().status, "queued")

    def test_worker_rechecks_newer_failure_and_does_not_spin_or_call_provider(self):
        payload = self.ready()
        self.dispatch()
        attempt, _ = self.attempt()
        self.finish(attempt, "failed")
        claim = claim_next_text_asset_job("blocked-worker")
        peer = Mock()
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            blocked = process_text_asset_job(claim)
        peer.invoke.assert_not_called()
        self.assertEqual((blocked.status, blocked.error_code), ("queued", "execution_smoke_failed"))
        self.assertIsNone(blocked.claim_token)
        self.assertIsNone(blocked.submission_started_at)
        self.assertGreater(blocked.next_poll_at, timezone.now())
        self.assertIsNone(claim_next_text_asset_job("must-wait"))
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.prepared_request, payload)

    def test_original_worker_uses_actual_gate_and_frozen_payload_then_only_one_submission(self):
        payload = self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("actual-root-worker")
        peer = Mock()
        peer.invoke.return_value = {"text": "A blue box.", "request_id": "isolated-peer-request"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            ended = process_text_asset_job(claim)
        self.assertEqual(ended.status, "succeeded", ended.error_message)
        self.assertIsNone(ended.actual_cost)
        self.assertEqual(ended.estimated_cost, Decimal("0.1"))
        self.assertEqual(peer.invoke.call_args.args[1:], ("chat_completion", payload["transport"]["payload"]))
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertIsNone(claim_next_text_asset_job("no-duplicate"))
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.evaluation["phase"], "submission")
        self.assertEqual(self.prep.contract.prepared_request, payload)

    def test_cancel_during_chat_preserves_unknown_actual_cost_and_does_not_keep_result(self):
        self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("cancel-during-chat")
        def reply(*args):
            request_asset_generation_cancel(AssetGenerationJob.objects.get(pk=claim.pk))
            return {"text": "A blue box.", "request_id": "cancelled-peer"}
        peer = Mock()
        peer.invoke.side_effect = reply
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            ended = process_text_asset_job(claim)
        self.assertEqual(ended.status, "cancelled", ended.error_message)
        self.assertIsNone(ended.actual_cost)
        self.assertEqual(ended.estimated_cost, Decimal("0.1"))
        self.assertEqual(ended.result_text, "")
        self.assertIsNone(ended.output_asset_id)
        self.assertEqual(peer.invoke.call_count, 1)

    def test_operator_remote_success_without_billing_is_not_estimate_settlement(self):
        self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("uncertain-chat")
        peer = Mock()
        peer.invoke.side_effect = ProviderDriverError("isolated lost response", code="lost_response", submission_uncertain=True)
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            ended = process_text_asset_job(claim)
        self.assertEqual(ended.status, "uncertain")
        resolution, created = resolve_uncertain_asset_generation_job(ended,
            outcome="confirmed_remote_succeeded", note="已核对远端成功但未提供费用凭证", user=self.actor,
            client_idempotency_key="unknown-billing")
        self.assertTrue(created)
        self.assertIsNone(resolution.evidence["actual_cost"])
        self.assertEqual(resolution.evidence["actual_cost_source"], "unknown")
        ended.refresh_from_db()
        self.assertIsNone(ended.actual_cost)
        self.assertEqual(ended.estimated_cost, Decimal("0.1"))
        self.assertEqual(_collected_actual_cost(ended, {}), (None, ""))

    def test_verified_operator_cost_is_kept_and_corrupt_receipt_is_not_ignored(self):
        self.ready()
        self.dispatch()
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain", submission_started_at=timezone.now())
        self.job.refresh_from_db()
        resolution, _ = resolve_uncertain_asset_generation_job(self.job,
            outcome="confirmed_remote_succeeded", note="核对隔离远端账单金额", user=self.actor,
            actual_cost="0.025", cost_currency="USD", client_idempotency_key="known-billing")
        self.job.refresh_from_db()
        self.assertEqual(_collected_actual_cost(self.job, {}), (Decimal("0.025"), "USD"))
        resolution.evidence["actual_cost"] = "0.01"
        # Keep immutable audit writes protected; inject corruption at the
        # read boundary to exercise digest verification without bypassing it.
        from types import SimpleNamespace
        damaged = SimpleNamespace(pk=self.job.pk, cost_currency="USD", resolutions=Mock())
        damaged.resolutions.filter.return_value.order_by.return_value.first.return_value = resolution
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            _collected_actual_cost(damaged, {})

    def test_increased_price_before_submission_blocks_without_rewriting_request(self):
        self.ready()
        self.dispatch()
        provider = self.deployment.provider
        config = deepcopy(provider.driver_config)
        config["pricing"] = {"currency": "USD", "fixed_per_request": "2"}
        assign_json_contract(provider, "driver_config", config)
        provider.save()
        claim = claim_next_text_asset_job("price-change")
        permitted, result = begin_asset_execution_submission(claim)
        self.assertFalse(permitted)
        self.assertEqual(result.error_code, "execution_resource_confirmation_required")
        self.assertIsNone(result.submission_started_at)
        self.assertEqual(result.estimated_cost, Decimal("0.1"))

    def test_same_claim_cannot_obtain_two_submission_permits(self):
        self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("same-claim")
        first, started = begin_asset_execution_submission(claim)
        second, current = begin_asset_execution_submission(claim)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(current.status, "submitting")
        self.assertEqual(current.submission_started_at, started.submission_started_at)

    def test_staging_gate_does_not_start_main_clock_and_does_not_replace_final_gate(self):
        self.ready()
        self.dispatch()
        claim = claim_next_text_asset_job("upload-phase")
        permitted, staged = check_asset_execution_staging(claim)
        self.assertTrue(permitted)
        self.assertEqual(staged.status, "queued")
        self.assertIsNone(staged.submission_started_at)
        self.policy("0.01")
        permitted, result = begin_asset_execution_submission(staged)
        self.assertFalse(permitted)
        self.assertEqual(result.error_code, "execution_resource_confirmation_required")
        self.assertIsNone(result.submission_started_at)

    def test_cancelled_queued_root_never_reenters_dispatch_on_replay(self):
        self.ready()
        self.dispatch()
        self.job.refresh_from_db()
        request_asset_generation_cancel(self.job)
        result = self.dispatch()
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(claim_next_text_asset_job("cancelled"))
