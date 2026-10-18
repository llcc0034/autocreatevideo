"""Manual recovery through the original command and queue; isolated peers only."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from studio.models import (AssetGenerationJob, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, ProductionManualCommand, ProductionRun)
from studio.tests import test_execution_production_dispatch as dispatch
from studio.tests import test_production_manual_preparations as manual
from studio.tests import test_production_execution_retry as retry_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualRetryTests(TestCase):
    setUpTestData = classmethod(manual.ManualPreparationTests.setUpTestData.__func__)
    setUp = manual.ManualPreparationTests.setUp
    profile_instance = manual.ManualPreparationTests.profile_instance
    post = manual.ManualPreparationTests.post
    preparation = manual.ManualPreparationTests.preparation
    pass_smoke = dispatch.ProductionDispatchTests.pass_smoke
    advance = dispatch.ProductionDispatchTests.advance
    response = dispatch.ProductionDispatchTests.response
    process = dispatch.ProductionDispatchTests.process
    policy = retry_fixtures.ProductionRetryTests.policy
    confirm_waiting_retry = retry_fixtures.ProductionRetryTests.confirm_waiting_retry

    def failed(self):
        self.profile_instance()
        self.body["payload"].update(execution_instance_version_id=str(self.version.pk),
            expected_definition_sha256=self.definition.sha256, manual_channels={},
            source_language="English", target_language="English")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.pass_smoke()
        preparation = self.advance(self.preparation())
        self.assertEqual(preparation.status, "dispatched", preparation.reason)
        attempt, _ = self.process({"text": None, "actual_cost": "0.02", "currency": "USD"})
        self.assertEqual(attempt.status, "failed", attempt.error_message)
        return preparation, attempt

    def retry_body(self, preparation, attempt, key="manual-retry"):
        preparation.refresh_from_db()
        return {**deepcopy(self.body), "action": "retry", "idempotency_key": key, "payload": {
            "preparation_id": str(preparation.pk), "expected_preparation_revision": preparation.revision,
            "expected_attempt_id": str(attempt.pk)}}

    def test_retry_preserves_original_request_and_successful_candidates_with_receipt_replay(self):
        prep, failed = self.failed()
        another = deepcopy(self.body); another["idempotency_key"] = "another-candidate"
        other_prep = ExecutionPreparation.objects.get(pk=self.post(another).data["target_id"])
        self.advance(other_prep)
        good, _ = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.assertEqual(good.status, "succeeded", good.error_message)
        before_artifacts = list(GeneratedArtifact.objects.values())
        before_preparations = set(ExecutionPreparation.objects.values_list("pk", flat=True))
        original = deepcopy(failed.request_snapshot)
        body = self.retry_body(prep, failed)
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport",
                side_effect=AssertionError("Retry must not recompile")), patch(
                "studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No seed redraw")):
            response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["result_kind"], "preparation")
        self.assertEqual(response.data["target_id"], str(prep.pk))
        self.assertTrue(response["Location"].endswith(f"/execution-preparations/{prep.pk}/"))
        retry = GenerationAttempt.objects.get(retry_of=failed)
        self.assertEqual(retry.preparation_id, prep.pk)
        self.assertEqual(retry.request_snapshot, original)
        self.assertEqual(retry.request_sha256, failed.request_sha256)
        self.assertEqual(retry.retry_requested_by_id, self.actor.pk)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post({**body, "idempotency_key": "double-click-other-key"}).status_code, 202)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)
        self.assertEqual(set(ExecutionPreparation.objects.values_list("pk", flat=True)), before_preparations)
        self.assertEqual(list(GeneratedArtifact.objects.values()), before_artifacts)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.run.refresh_from_db(); good.refresh_from_db(); failed.refresh_from_db()
        self.assertIsNone(self.run.recipe_version_id)
        self.assertEqual((good.status, failed.status), ("succeeded", "failed"))
        self.assertFalse(self.run.stages.exclude(pk=self.stage.pk).exclude(status="pending").exists())

    def test_new_failed_successor_requires_explicit_new_target(self):
        prep, failed = self.failed()
        body = self.retry_body(prep, failed)
        self.assertEqual(self.post(body).status_code, 202)
        second, _ = self.process({"text": None, "actual_cost": "0.02", "currency": "USD"})
        self.assertEqual(second.status, "failed")
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post({**body, "idempotency_key": "old-target-new-key"}).status_code, 202)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=second).exists())
        response = self.post(self.retry_body(prep, second, "retry-second"))
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(GenerationAttempt.objects.get(retry_of=second).request_sha256, failed.request_sha256)

    def test_current_admission_block_is_a_preparation_receipt_not_a_submitted_call(self):
        prep, failed = self.failed()
        self.policy(calls=1)
        body = self.retry_body(prep, failed)
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        prep.refresh_from_db()
        # The preparation was dispatched before the original failure. The
        # current gate belongs to its evaluation, not a rewrite of that fact.
        self.assertEqual(prep.status, "dispatched")
        self.assertEqual(prep.contract.evaluation["status"], "blocked")
        self.assertEqual(prep.reason_code, "execution_resource_confirmation_required")
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())
        self.confirm_waiting_retry(failed)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())
        response = self.post(self.retry_body(prep, failed, "explicit-after-handling-block"))
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def test_retry_rejects_wrong_scope_revision_extra_input_and_non_failed_target(self):
        prep, failed = self.failed()
        body = self.retry_body(prep, failed)
        variants = [
            {**body, "expected_run_plan_sha256": "f" * 64},
            {**body, "scope": {"kind": "project", "id": str(uuid4())}},
            {**body, "payload": {**body["payload"], "preparation_id": str(uuid4())}},
            {**body, "payload": {**body["payload"], "expected_attempt_id": str(uuid4())}},
            {**body, "payload": {**body["payload"], "expected_preparation_revision": prep.revision + 1}},
            {**body, "payload": {**body["payload"], "inputs": {}}},
            {**body, "expected_selection_revision": 0},
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertIn(self.post(value).status_code, (400, 409))
        for status in ("queued", "running", "succeeded", "cancelled", "awaiting_review"):
            GenerationAttempt.objects.filter(pk=failed.pk).update(status=status)
            with self.subTest(status=status):
                self.assertEqual(self.post(body).status_code, 400)
        GenerationAttempt.objects.filter(pk=failed.pk).update(status="failed", error_code="uncertain_remote_response")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="retry").exists())

    def test_terminal_root_and_adopted_stage_cannot_be_reopened_but_failed_root_can(self):
        prep, failed = self.failed()
        body = self.retry_body(prep, failed)
        for status in ("succeeded", "cancelled", "cancel_requested"):
            ProductionRun.objects.filter(pk=self.run.pk).update(status=status)
            self.assertEqual(self.post(body).status_code, 400)
        ProductionRun.objects.filter(pk=self.run.pk).update(status="failed")
        self.stage.status = "succeeded"; self.stage.save(update_fields=("status",))
        self.assertEqual(self.post(body).status_code, 400)
        self.stage.status = "failed"; self.stage.save(update_fields=("status",))
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def test_receipt_write_failure_rolls_back_attempt_and_original_run(self):
        prep, failed = self.failed()
        self.run.refresh_from_db()
        before = (self.run.generation_task_count, self.run.committed_cost_total)
        with patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("isolated receipt failure")):
            with self.assertRaisesMessage(RuntimeError, "isolated receipt failure"):
                self.post(self.retry_body(prep, failed))
        self.run.refresh_from_db()
        self.assertEqual((self.run.generation_task_count, self.run.committed_cost_total), before)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())

    def test_retry_options_are_original_frozen_actions_and_anonymous_cannot_submit(self):
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(options.status_code, 200, options.data)
        actions = [(row["stage_key"], row["action"]) for row in options.data["actions"]]
        self.assertIn(("script_generate", "retry"), actions)
        self.assertNotIn(("script_review", "retry"), actions)
        self.client.force_authenticate(None)
        self.assertIn(self.post({**self.body, "action": "retry", "payload": {}}).status_code, (401, 403))
