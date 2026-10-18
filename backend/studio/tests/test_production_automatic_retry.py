"""Production retry policy exercised via original workers with isolated peers."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import GenerationAttempt
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import advance_production_run
from studio.services.production_execution_recovery import production_retry_decision
from studio.tests import test_production_execution_retry as retry_fixtures


def freeze_retry_policy(run, maximum=3):
    from studio.services.security_boundaries import boundary_int
    snapshot = deepcopy(run.config_snapshot)
    snapshot.setdefault("values", {}).update({"production.task_max_attempts": maximum,
        "execution.preparation.recheck_interval_seconds": 10})
    assign_json_contract(run, "config_snapshot", snapshot, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    run.save()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class AutomaticRetryTests(TestCase):
    Fixtures = retry_fixtures.ProductionRetryTests
    setUpTestData = classmethod(Fixtures.setUpTestData.__func__)
    profile_instance = Fixtures.profile_instance
    root = Fixtures.root
    prepare = Fixtures.prepare
    structure_instance = Fixtures.structure_instance
    pass_smoke = Fixtures.pass_smoke
    advance = Fixtures.advance
    response = Fixtures.response
    process = Fixtures.process

    def setUp(self):
        self.Fixtures.setUp(self)
        freeze_retry_policy(self.run)
        self.pass_smoke()
        advance_production_run(self.run)

    def fail_call(self):
        return self.process({"text": None, "actual_cost": "0.02", "currency": "USD"})[0]

    def due(self, attempt):
        GenerationAttempt.objects.filter(pk=attempt.pk).update(finished_at=timezone.now()-timedelta(seconds=11))

    def test_three_attempts_then_human_preserves_success_and_exact_payload(self):
        first = self.fail_call()
        good = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})[0]
        payload = first.request_snapshot
        result = advance_production_run(self.run)
        self.assertEqual((result.status, result.blocked_code), ("running", "execution_retry_wait"))
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.due(first)
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport",
                side_effect=AssertionError("must reuse frozen payload")):
            advance_production_run(self.run)
            advance_production_run(self.run)
        second = GenerationAttempt.objects.get(retry_of=first)
        self.assertEqual(second.request_snapshot, payload)
        self.assertEqual(second.preparation_id, first.preparation_id)
        self.assertEqual(GenerationAttempt.objects.count(), 3)
        self.assertEqual(self.fail_call().pk, second.pk)
        self.due(second)
        advance_production_run(self.run)
        third = GenerationAttempt.objects.get(retry_of=second)
        self.assertEqual(self.fail_call().pk, third.pk)
        result = advance_production_run(self.run)
        self.assertEqual((result.status, result.blocked_code), ("awaiting_input", "execution_retry_exhausted"))
        self.assertIn("3/3", result.blocked_reason)
        advance_production_run(result)
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        good.refresh_from_db()
        self.assertEqual(good.status, "succeeded")
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=good).exists())
        # Human recovery after exhaustion is still the original exact-target action.
        from studio.services.production_runs import retry_production_run
        result = retry_production_run(result, user=self.actor, expected_stage_id=self.stage.pk,
            expected_attempt_ids=[third.pk])
        self.assertTrue(GenerationAttempt.objects.filter(retry_of=third).exists())
        self.assertEqual(GenerationAttempt.objects.count(), 5)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=good).exists())

    def test_retry_admission_pause_is_not_reported_as_running(self):
        first = self.fail_call()
        self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.due(first)
        self.run.refresh_from_db()
        self.Fixtures.policy(self, calls=self.run.generation_task_count)
        result = advance_production_run(self.run)
        self.assertEqual(result.blocked_code, "execution_resource_confirmation_required")
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=first).exists())

    def test_local_text_definite_failure_releases_reservation_without_fake_bill(self):
        from studio.services.execution_production_dispatch import begin_production_execution_submission
        from studio.services.execution_production_text_worker import finish_production_text
        from studio.services.text_generation_queue import claim_next_text_attempt
        claim = claim_next_text_attempt("isolated-local-text")
        allowed, claim = begin_production_execution_submission(claim)
        self.assertTrue(allowed)
        self.assertIsNotNone(claim.submission_started_at)
        self.assertGreater(claim.committed_cost, 0)
        GenerationAttempt.objects.filter(pk=claim.pk).update(is_external=False)
        result = finish_production_text(claim, status="failed", code="execution_provider_failed")
        self.assertEqual(result.committed_cost, 0)
        self.assertIsNone(result.actual_cost)
        self.assertEqual(production_retry_decision(result)["state"], "retry")

    def test_budget_one_and_two_are_total_attempts_not_stage_ordinals(self):
        first = self.fail_call()
        second_candidate = self.fail_call()
        self.assertEqual(production_retry_decision(second_candidate)["attempts"], 1)
        for maximum in (1, 2):
            freeze_retry_policy(self.run, maximum)
            first.stage_run.production_run = self.run
            decision = production_retry_decision(first)
            self.assertEqual(decision["state"], "manual" if maximum == 1 else "retry")
            self.assertEqual(decision["max_attempts"], maximum)

    def test_primary_unsubmitted_validation_failure_preserves_unknown_cost_and_retry_budget(self):
        from django.core.exceptions import ValidationError
        from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
        claim = claim_next_text_attempt("isolated-unsubmitted-primary")
        with patch("studio.services.execution_production_text_worker.read_production_attempt_request",
                side_effect=ValidationError("冻结请求校验失败")), \
                patch("studio.services.execution_production_text_worker.driver_for") as driver:
            failed = process_text_attempt(claim)
        driver.assert_not_called()
        self.assertIsNone(failed.actual_cost)
        self.assertIsNone(failed.submission_started_at)
        self.assertEqual(failed.committed_cost, 0)
        self.assertEqual(production_retry_decision(failed)["state"], "retry")
        from studio.services.execution_production_attempts import retry_check_queryset, require_retryable_failure
        with self.assertNumQueries(1):
            rows = list(retry_check_queryset(GenerationAttempt.objects.filter(pk=failed.pk)))
            require_retryable_failure(rows[0], automatic=True)
        freeze_retry_policy(self.run, 1)
        failed.stage_run.production_run = self.run
        self.assertEqual(production_retry_decision(failed)["code"], "execution_retry_exhausted")

    def test_unknown_external_cost_cancel_and_storage_failure_never_auto_resend(self):
        first = self.fail_call()
        self.due(first)
        for values in ({"actual_cost": None}, {"error_code": "uncertain_remote_response"},
                {"status": "cancelled"}, {"ignore_late_result": True},
                {"error_code": "execution_response_storage_failed"}):
            before = {key: getattr(first, key) for key in values}
            GenerationAttempt.objects.filter(pk=first.pk).update(**values)
            current = GenerationAttempt.objects.get(pk=first.pk)
            self.assertEqual(production_retry_decision(current)["state"], "manual", values)
            if values.get("error_code") == "uncertain_remote_response":
                self.assertEqual(production_retry_decision(current)["code"], "uncertain_remote_response")
            GenerationAttempt.objects.filter(pk=first.pk).update(**before)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=first).exists())

    def test_missing_policy_goes_to_human_without_default_or_current_config(self):
        first = self.fail_call()
        assign_json_contract(self.run, "config_snapshot", {"schema_version": 1})
        self.run.save()
        current = GenerationAttempt.objects.get(pk=first.pk)
        decision = production_retry_decision(current)
        self.assertEqual(decision["state"], "manual")
        self.assertIn("缺少", decision["reason"])
