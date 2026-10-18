"""Explicit recheck of a corrected producer stage, never a call retry."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_runs import advance_production_run
from studio.tests import test_production_recipe_media_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProductionValidationContinueTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipeMediaPreparationTests.setUpTestData.__func__)
    setUp = fixtures.RecipeMediaPreparationTests.setUp
    write_contract = staticmethod(fixtures.RecipeMediaPreparationTests.write_contract)
    create_root = fixtures.RecipeMediaPreparationTests.create_root
    prepare = fixtures.RecipeMediaPreparationTests.prepare

    def block(self, *, status="blocked", code="stage_validation_failed"):
        self.stage.refresh_from_db()
        self.stage.status, self.stage.reason_code, self.stage.reason = status, code, "Original blocked reason"
        self.stage.save(update_fields=("status", "reason_code", "reason"))
        self.run.refresh_from_db()
        self.run.status, self.run.blocked_code, self.run.blocked_reason = status, code, self.stage.reason
        self.run.save(update_fields=("status", "blocked_code", "blocked_reason"))

    def test_continue_api_reuses_preparations_and_keeps_real_manual_review(self):
        advance_production_run(self.run)
        before = list(ExecutionPreparation.objects.order_by("id").values_list("pk", "contract__intent_sha256"))
        self.assertEqual(len(before), 2)
        self.block()
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/continue/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.blocked_code), ("awaiting_input", "execution_preparation_review_required"))
        self.assertEqual(list(ExecutionPreparation.objects.order_by("id").values_list("pk", "contract__intent_sha256")), before)
        self.assertEqual(get_run_execution_plan(self.run).sha256, self.plan.sha256)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.stage.approval_decisions.exists())

    def test_continue_reuses_existing_calls_without_resubmission(self):
        from studio.services.production_runs import _freeze_stage_input, _poll_media_stage
        from studio.tests.test_execution_production_auxiliaries import production_observations
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        _freeze_stage_input(self.run, self.stage)
        _poll_media_stage(self.run, self.stage)
        before = list(GenerationAttempt.objects.order_by("id").values_list("id", "preparation_id", "request_sha256", "status"))
        self.assertEqual(len(before), 2)
        self.block()
        result = advance_production_run(self.run, human_initiated=True)
        self.assertEqual(result.status, "running", result.blocked_reason)
        self.assertEqual(list(GenerationAttempt.objects.order_by("id").values_list("id", "preparation_id", "request_sha256", "status")), before)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.attempt_count, 2)
        self.assertEqual(get_run_execution_plan(self.run).sha256, self.plan.sha256)

    def test_background_and_programmatic_checks_do_not_resume_blocked_stage(self):
        self.block()
        for options in ({}, {"automatic": True}, {"automatic": True, "human_initiated": True}):
            with self.subTest(options=options), patch("studio.services.production_runs._execute_stage") as execute:
                self.assertEqual(advance_production_run(self.run, **options).status, "blocked")
                execute.assert_not_called()

    def test_uncorrected_validation_still_blocks_with_current_error(self):
        self.block()
        with patch("studio.services.production_runs._execute_stage", side_effect=ValidationError("Source still invalid")):
            result = advance_production_run(self.run, human_initiated=True)
        self.assertEqual((result.status, result.blocked_code), ("blocked", "stage_validation_failed"))
        self.assertIn("Source still invalid", result.blocked_reason)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_continue_does_not_reopen_approval_cancellation_failure_or_other_blocks(self):
        for status, code in (("awaiting_approval", "stage_validation_failed"), ("cancelled", "stage_validation_failed"),
                ("failed", "stage_validation_failed"), ("blocked", "execution_resource_confirmation_required"),
                ("blocked", "execution_retry_requires_action")):
            self.block(status=status, code=code)
            with self.subTest(status=status, code=code), patch("studio.services.production_runs._execute_stage") as execute:
                self.assertEqual(advance_production_run(self.run, human_initiated=True).status, status)
                execute.assert_not_called()
