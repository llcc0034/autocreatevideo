"""Formal preparation cancellation with real original queues, no provider I/O."""
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ExecutionFieldValue, GenerationAttempt
from studio.services.execution_production_cancellation import cancel_production_preparation
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.production_runs import cancel_production_run
from studio.services.provider_registry import ProviderDriverError
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_execution_production_auxiliaries as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProductionCancellationTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionAuxiliaryTests.setUpTestData.__func__)
    setUp = fixtures.ProductionAuxiliaryTests.setUp
    write_contract = staticmethod(fixtures.ProductionAuxiliaryTests.write_contract)
    input = staticmethod(fixtures.ProductionAuxiliaryTests.input)
    bind = fixtures.ProductionAuxiliaryTests.bind
    stage = fixtures.ProductionAuxiliaryTests.stage
    prepare = fixtures.ProductionAuxiliaryTests.prepare
    child = fixtures.ProductionAuxiliaryTests.child
    advance = fixtures.ProductionAuxiliaryTests.advance
    execute = fixtures.ProductionAuxiliaryTests.execute

    def cancel(self, preparation=None, revision=None):
        preparation = preparation or self.parent
        preparation.refresh_from_db()
        return cancel_production_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=revision if revision is not None else preparation.revision, actor=self.actor)

    def queued(self):
        child = self.child()
        self.advance(child)
        return child, GenerationAttempt.objects.get(preparation=child)

    def test_preparation_only_cancel_has_receipt_replay_and_no_model_call(self):
        self.parent.refresh_from_db()
        previous = self.parent.revision
        frozen = (self.parent.intent_sha256, self.parent.contract.resolved_inputs_sha256)
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{self.parent.pk}/cancel/"
        with patch("studio.services.execution_production_text_worker.driver_for", side_effect=AssertionError("No provider")):
            response = client.post(url, {"expected_preparation_revision": previous}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["cancellation"]["allowed"])
        self.assertEqual(str(response.data["cancellation"]["cancelled_by"]), str(self.actor.pk))
        self.parent.refresh_from_db()
        self.assertEqual((self.parent.status, self.parent.revision), ("cancelled", previous + 1))
        self.assertEqual(self.parent.cancelled_by_id, self.actor.pk)
        self.assertIsNotNone(self.parent.cancelled_at)
        self.assertIsNone(self.parent.next_check_at)
        self.assertEqual(frozen, (self.parent.intent_sha256, self.parent.contract.resolved_inputs_sha256))
        self.assertEqual(client.post(url, {"expected_preparation_revision": previous}, format="json").status_code, 200)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.revision, previous + 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_queued_and_claimed_before_submission_release_only_their_reservations(self):
        child, attempt = self.queued()
        claim = claim_next_text_attempt("cancel-before-boundary")
        self.assertEqual(claim.pk, attempt.pk)
        self.cancel()
        child.refresh_from_db(); attempt.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((child.status, attempt.status), ("cancelled", "cancelled"))
        self.assertEqual(attempt.committed_cost, Decimal("0"))
        self.assertIsNone(attempt.actual_cost)  # No fake remote bill.
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        self.assertIsNone(attempt.claim_token)
        self.assertEqual(attempt.resolutions.count(), 1)
        peer = Mock()
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claim)
        self.assertEqual(result.status, "cancelled")
        peer.invoke.assert_not_called()

    def test_other_candidate_and_its_auxiliary_are_not_cancelled(self):
        child, first = self.queued()
        parent = self.parent
        def second(**kwargs):
            return create_execution_instance_preparation(**{**kwargs, "request_key": "candidate-2"})
        with patch("studio.tests.test_execution_field_review.create_execution_instance_preparation", side_effect=second):
            self.parent = self.prepare(stage=self.root_stage)
        sibling, untouched = self.queued()
        self.cancel(parent)
        sibling.refresh_from_db(); untouched.refresh_from_db(); self.parent.refresh_from_db(); self.run.refresh_from_db()
        self.assertNotEqual(self.parent.status, "cancelled")
        self.assertEqual((sibling.status, untouched.status), ("dispatched", "queued"))
        self.assertEqual(self.run.committed_cost_total, untouched.committed_cost)
        self.assertEqual(self.run.status, "running")
        self.root_stage.refresh_from_db()
        self.assertEqual(self.root_stage.status, "running")

    def test_cancellation_during_remote_call_ignores_fields_but_settles_bill(self):
        before = ExecutionFieldValue.objects.count()
        def response(*args):
            self.cancel()
            import json
            return {"text": json.dumps({"items": [{"request_id": self.batch["value"]["items"][0]["request_id"],
                "text": "A courier opens a blue parcel in soft daylight."}]}),
                "request_id": "isolated-late", "actual_cost": "0.03", "currency": "USD"}
        _, result, peer = self.execute(effect=response)
        self.assertEqual((result.status, result.error_code), ("cancelled", "late_result_ignored"))
        self.assertEqual(result.actual_cost, Decimal("0.03"))
        self.assertEqual(ExecutionFieldValue.objects.count(), before)
        self.run.refresh_from_db()
        self.assertEqual(self.run.actual_cost_total, Decimal("0.03"))
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        peer.invoke.assert_called_once()

    def test_uncertain_failure_keeps_original_state_identity_and_reservation(self):
        child, attempt = self.queued()
        GenerationAttempt.objects.filter(pk=attempt.pk).update(status="failed", submission_started_at=timezone.now(),
            error_code="uncertain_remote_response", finished_at=timezone.now())
        before = attempt.committed_cost
        self.cancel()
        attempt.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((attempt.status, attempt.error_code), ("failed", "uncertain_remote_response"))
        self.assertEqual((attempt.committed_cost, self.run.committed_cost_total), (before, before))
        self.assertTrue(attempt.ignore_late_result)
        self.assertEqual(attempt.resolutions.count(), 1)

    def test_bad_ledger_rolls_back_entire_cancellation(self):
        child, attempt = self.queued()
        self.run.committed_cost_total = Decimal("0")
        self.run.save(update_fields=("committed_cost_total",))
        with self.assertRaisesMessage(ValidationError, "账目不一致"):
            self.cancel()
        self.parent.refresh_from_db(); child.refresh_from_db(); attempt.refresh_from_db()
        self.assertNotEqual(self.parent.status, "cancelled")
        self.assertIsNone(self.parent.cancelled_at)
        self.assertEqual((child.status, attempt.status), ("dispatched", "queued"))
        self.assertFalse(attempt.resolutions.exists())

    def test_stale_revision_child_and_unauthorized_cannot_cancel(self):
        child = self.child()
        self.parent.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "准备已变化"):
            self.cancel(revision=self.parent.revision + 1)
        with self.assertRaisesMessage(ValidationError, "辅助准备"):
            self.cancel(child)
        client = APIClient()
        url = f"/api/v1/execution-preparations/{self.parent.pk}/cancel/"
        self.assertIn(client.post(url, {}, format="json").status_code, (401, 403))
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username="cancellation-other")
        client.force_authenticate(other)
        self.assertEqual(client.post(url, {"expected_preparation_revision": self.parent.revision}, format="json").status_code, 404)

    def test_whole_run_uses_same_cancellation_and_releases_queued_text(self):
        child, attempt = self.queued()
        cancel_production_run(self.run, user=self.actor)
        self.run.refresh_from_db(); child.refresh_from_db(); self.parent.refresh_from_db(); attempt.refresh_from_db()
        self.assertEqual((self.run.status, self.parent.status, child.status, attempt.status), ("cancelled",) * 4)
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        self.assertEqual(attempt.resolutions.count(), 1)

    def test_lost_response_during_cancellation_is_not_confirmed_cancelled(self):
        def lost(*_):
            self.cancel()
            raise ProviderDriverError("isolated lost response", submission_uncertain=True)
        _, result, peer = self.execute(effect=lost)
        self.assertEqual((result.status, result.error_code), ("failed", "uncertain_remote_response"))
        self.assertTrue(result.ignore_late_result)
        self.assertIsNotNone(result.submission_started_at)
        self.assertIsNone(result.actual_cost)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, result.committed_cost)
        self.assertGreater(result.committed_cost, Decimal("0"))
        peer.invoke.assert_called_once()

    def test_lost_worker_after_cancel_keeps_unknown_remote_result(self):
        from datetime import timedelta
        from studio.services.execution_production_dispatch import begin_production_execution_submission
        from studio.services.text_generation_queue import recover_stale_text_attempts
        child, attempt = self.queued()
        claim = claim_next_text_attempt("lost-worker")
        allowed, submitted = begin_production_execution_submission(claim)
        self.assertTrue(allowed)
        self.cancel()
        GenerationAttempt.objects.filter(pk=attempt.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=20))
        self.assertEqual(recover_stale_text_attempts(5), {"requeued": 0, "uncertain": 1})
        attempt.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((attempt.status, attempt.error_code), ("failed", "uncertain_remote_response"))
        self.assertEqual(self.run.committed_cost_total, attempt.committed_cost)
        self.assertIsNone(attempt.claim_token)
        self.assertEqual(recover_stale_text_attempts(5), {"requeued": 0, "uncertain": 0})

    def test_terminal_run_does_not_offer_cancellation(self):
        from studio.services.execution_production_cancellation import production_cancellation_state
        self.run.status = "succeeded"
        self.run.save(update_fields=("status",))
        self.parent.refresh_from_db()
        self.assertFalse(production_cancellation_state(self.parent)["allowed"])
        with self.assertRaisesMessage(ValidationError, "已结束"):
            self.cancel()

    def test_cancellation_reads_indexed_state_not_request_snapshots(self):
        from studio.services.execution_production_cancellation import cancellation_attempts
        child, attempt = self.queued()
        query = cancellation_attempts(GenerationAttempt.objects.filter(preparation=child))
        row = query.get()
        self.assertIn("request_snapshot_key", row.get_deferred_fields())
        self.assertIn("output_snapshot", row.stage_run.get_deferred_fields())
        self.assertIn("INDEX", query.explain().upper())
        from studio.models import ExecutionPreparation
        self.assertFalse(ExecutionPreparation._meta.get_field("cancelled_by").db_index)
        self.cancel()
        with self.assertRaisesMessage(ValidationError, "冻结事实不能批量修改"):
            ExecutionPreparation.objects.filter(pk=self.parent.pk).update(cancelled_by=None)
