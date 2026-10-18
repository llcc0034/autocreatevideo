"""Persisted due times/claims recover without notifications or remote calls."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.utils import timezone

from studio.models import AssetGenerationJob, ExecutionPreparation
from studio.services.asset_generation import request_asset_generation_cancel
from studio.services.execution_preparation_queue import (
    advance_next_execution_preparation, claim_next_execution_preparation, process_claimed_execution_preparation,
)
from studio.tests import test_execution_asset_admission as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionPreparationQueueTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetAdmissionTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionAssetAdmissionTests.setUp
    version = fixtures.ExecutionAssetAdmissionTests.version
    attempt = fixtures.ExecutionAssetAdmissionTests.attempt
    finish = fixtures.ExecutionAssetAdmissionTests.finish
    passed_fixture = fixtures.ExecutionAssetAdmissionTests.passed_fixture

    def test_due_claim_is_exclusive_and_expired_lease_is_recoverable(self):
        self.passed_fixture()
        first = claim_next_execution_preparation()
        self.assertEqual(first.pk, self.prep.pk)
        self.assertIsNone(claim_next_execution_preparation())
        self.job.refresh_from_db()
        self.assertIsNone(self.job.submission_started_at)
        ExecutionPreparation.objects.filter(pk=first.pk).update(claim_until=timezone.now() - timedelta(seconds=1))
        second = claim_next_execution_preparation()
        self.assertNotEqual(first.claim_token, second.claim_token)
        late = process_claimed_execution_preparation(first)
        self.assertEqual(late.claim_token, second.claim_token)
        self.assertEqual(late.status, "preparing")
        result = process_claimed_execution_preparation(second)
        self.assertEqual(result.status, "dispatched")
        self.assertIsNone(result.claim_token)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertIsNone(advance_next_execution_preparation())
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_blocked_preparation_keeps_due_time_and_resumes_without_a_notification(self):
        blocked = advance_next_execution_preparation()
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.reason_code, "execution_smoke_missing")
        self.assertGreater(blocked.next_check_at, timezone.now())
        self.assertIsNone(claim_next_execution_preparation())
        self.passed_fixture()
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(next_check_at=timezone.now() - timedelta(seconds=1))
        resumed = advance_next_execution_preparation()
        self.assertEqual(resumed.status, "dispatched")
        self.assertIsNone(resumed.claim_until)

    def test_missing_smoke_rechecks_do_not_churn_the_reviewed_preparation_revision(self):
        blocked = advance_next_execution_preparation()
        revision = blocked.revision
        evaluation = blocked.contract.evaluation_sha256
        evaluated_at = blocked.evaluated_at
        for _ in range(3):
            ExecutionPreparation.objects.filter(pk=blocked.pk).update(next_check_at=timezone.now() - timedelta(seconds=1))
            current = advance_next_execution_preparation()
            self.assertEqual(current.status, "blocked")
            self.assertEqual(current.reason_code, "execution_smoke_missing")
            self.assertEqual(current.revision, revision)
            self.assertEqual(current.contract.evaluation_sha256, evaluation)
            self.assertEqual(current.evaluated_at, evaluated_at)
            self.assertGreater(current.next_check_at, timezone.now())
            self.assertIsNone(current.claim_token)
            self.assertIsNone(current.request_frozen_at)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_missing_smoke_wait_does_not_mask_new_failed_evidence(self):
        blocked = advance_next_execution_preparation()
        attempt, _ = self.attempt()
        self.finish(attempt, status="failed")
        ExecutionPreparation.objects.filter(pk=blocked.pk).update(next_check_at=timezone.now() - timedelta(seconds=1))
        current = advance_next_execution_preparation()
        self.assertEqual(current.reason_code, "execution_smoke_failed")
        self.assertGreater(current.revision, blocked.revision)
        self.assertIsNone(current.request_frozen_at)

    def test_missing_smoke_wait_rechecks_changed_policy(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        blocked = advance_next_execution_preparation()
        revision = create_config_revision(scope_type="system", values={"execution.admission.capacity_policy": "require_evidence"},
            created_by=self.actor, change_summary="changed policy while missing smoke")
        publish_config_revision(revision, published_by=self.actor)
        ExecutionPreparation.objects.filter(pk=blocked.pk).update(next_check_at=timezone.now() - timedelta(seconds=1))
        current = advance_next_execution_preparation()
        self.assertGreater(current.revision, blocked.revision)
        self.assertIn("execution_capacity_unknown", {row["code"] for row in current.contract.evaluation["admission"]["blockers"]})

    def test_human_wait_or_cancelled_root_is_not_automatically_restarted(self):
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(status="awaiting_review", next_check_at=None)
        self.assertIsNone(advance_next_execution_preparation())
        request_asset_generation_cancel(self.job)
        self.assertIsNone(advance_next_execution_preparation())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "cancelled")

    def test_disabled_root_owner_is_not_replaced_with_a_system_administrator(self):
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        result = advance_next_execution_preparation()
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.request_frozen_at)
        self.assertIsNone(result.claim_token)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")

    def test_unexpected_local_error_is_visible_without_killing_worker_or_leaking_inputs(self):
        with patch("studio.services.execution_preparation_queue.advance_execution_preparation", side_effect=RuntimeError("private-content")):
            result = advance_next_execution_preparation()
        self.assertEqual((result.status, result.reason_code), ("blocked", "execution_preparation_error"))
        self.assertNotIn("private-content", result.reason)
        self.assertIsNone(result.claim_until)
        self.assertIsNone(result.request_frozen_at)

    def test_database_contention_preserves_claim_for_original_worker_retry(self):
        claim = claim_next_execution_preparation()
        with patch("studio.services.execution_preparation_queue.advance_execution_preparation", side_effect=OperationalError("database locked")):
            with self.assertRaises(OperationalError):
                process_claimed_execution_preparation(claim)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.claim_token, claim.claim_token)
        self.assertEqual(self.prep.status, "preparing")
        self.assertNotEqual(self.prep.reason_code, "execution_preparation_error")

    def test_compile_failure_after_field_evaluation_is_visible_and_waits_for_human(self):
        from studio.services.execution_advancement import advance_execution_preparation
        from studio.services.execution_preparations import read_execution_preparation_detail
        self.passed_fixture()
        basis = self.prep.revision
        with patch("studio.services.execution_advancement.finalize_execution",
                side_effect=ValidationError("private source text")) as finalize:
            result = advance_next_execution_preparation()
            self.assertEqual((result.status, result.reason_code), ("blocked", "execution_request_invalid"))
            self.assertGreater(result.revision, basis)
            self.assertNotIn("private source text", result.reason)
            self.assertIsNone(result.next_check_at)
            self.assertIsNone(result.claim_token)
            self.assertIsNone(result.request_frozen_at)
            evaluation = read_execution_preparation_detail(result).evaluation
            self.assertEqual(evaluation["phase"], "compilation")
            self.assertEqual(evaluation["preparation_revision"], result.revision)
            self.assertIsNone(advance_next_execution_preparation())
            unchanged = advance_execution_preparation(preparation_id=result.pk,
                expected_preparation_revision=result.revision, actor=self.actor)
            self.assertEqual(unchanged.revision, result.revision)
            self.assertEqual(finalize.call_count, 1)
        resumed = advance_execution_preparation(preparation_id=result.pk,
            expected_preparation_revision=result.revision, actor=self.actor, human_initiated=True)
        self.assertEqual(resumed.status, "dispatched")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertIsNone(self.job.submission_started_at)

    def test_profile_failure_records_safe_field_identity_not_exception_text(self):
        from studio.services.prompt_profiles import PromptProfileError
        from studio.services.execution_preparations import read_execution_preparation_detail
        self.passed_fixture()
        error = PromptProfileError("private source https://secret.invalid/?token=secret",
            code="prompt_profile_generation_language_invalid")
        error.field_key = "continuity_rules"
        with patch("studio.services.execution_advancement.finalize_execution", side_effect=error):
            result = advance_next_execution_preparation()
        self.assertEqual(result.reason_code, "execution_request_invalid")
        self.assertIn("continuity_rules", result.reason)
        self.assertNotIn("secret", result.reason)
        self.assertNotIn("private", result.reason)
        diagnostic = read_execution_preparation_detail(result).evaluation["diagnostic"]
        self.assertEqual(diagnostic["code"], error.code)
        self.assertEqual(diagnostic["field_key"], "continuity_rules")
        self.assertIsNone(result.request_frozen_at)
        self.assertIsNone(result.next_check_at)
        from studio.services.execution_advancement import _compilation_diagnostic
        error.field_key = "secret.invalid/?token=secret"
        self.assertIsNone(_compilation_diagnostic(error))

    def test_compile_failure_cannot_replace_a_concurrently_changed_preparation(self):
        from studio.services.execution_advancement import _finalize_or_record_invalid_request
        def changed(**kwargs):
            ExecutionPreparation.objects.filter(pk=self.prep.pk).update(revision=self.prep.revision + 1)
            raise ValidationError("obsolete compiler observation")
        with patch("studio.services.execution_advancement.finalize_execution", side_effect=changed):
            with self.assertRaisesMessage(ValidationError, "obsolete compiler observation"):
                _finalize_or_record_invalid_request(self.prep, self.actor, None)
        self.prep.refresh_from_db()
        self.assertNotEqual(self.prep.reason_code, "execution_request_invalid")

    def test_revision_conflict_is_not_recorded_as_a_compile_failure(self):
        from studio.services.execution_advancement import _finalize_or_record_invalid_request
        basis = self.prep.revision
        with patch("studio.services.execution_advancement.finalize_execution",
                side_effect=ValidationError("changed", code="execution_preparation_conflict")):
            with self.assertRaises(ValidationError):
                _finalize_or_record_invalid_request(self.prep, self.actor, None)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, basis)
        self.assertNotEqual(self.prep.reason_code, "execution_request_invalid")
