"""Original queue and retry API, using isolated protocol responses only."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.production_runs import advance_production_run, retry_production_run
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionRetryTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    prepare = fixtures.ProductionDispatchTests.prepare
    structure_instance = fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process
    setUp = fixtures.ProductionDispatchTests.setUp

    def failed_stage(self):
        self.pass_smoke()
        advance_production_run(self.run)
        # No returned text is a definitive failed response; invalid returned text
        # is now preserved for human correction, not a retryable failure.
        failed, _ = self.process({"text": None, "request_id": "isolated-failed", "actual_cost": "0.02", "currency": "USD"})
        good, _ = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.assertEqual((failed.status, good.status), ("failed", "succeeded"))
        self.assertEqual(failed.error_code, "execution_response_text_missing")
        self.run = advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "blocked", self.stage.reason)
        return failed, good

    def retry(self, attempt, **overrides):
        return retry_production_run(self.run, user=self.actor, **{
            "expected_stage_id": self.stage.pk, "expected_attempt_ids": [attempt.pk], **overrides})

    def test_explicit_retry_reuses_payload_fields_and_budget_with_idempotent_replay(self):
        failed, good = self.failed_stage()
        frozen = deepcopy(failed.request_snapshot)
        prep_ids = set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True))
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport",
                side_effect=AssertionError("Retry must not recompile")), \
                patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("Retry must not redraw seed")):
            self.retry(failed)
        retried = GenerationAttempt.objects.get(retry_of=failed)
        self.assertEqual(retried.preparation_id, failed.preparation_id)
        self.assertEqual(retried.request_snapshot, frozen)
        self.assertEqual(retried.retry_requested_by_id, self.actor.pk)
        self.assertEqual(retried.request_sha256, failed.request_sha256)
        self.assertEqual(set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True)), prep_ids)
        replay = dispatch_production_execution(preparation_id=failed.preparation_id,
            expected_preparation_revision=1, actor=self.actor)
        self.assertEqual(replay.pk, retried.pk)
        self.retry(failed)
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, 3)
        self.assertEqual(self.run.committed_cost_total, retried.committed_cost)
        self.assertEqual(self.run.actual_cost_total, Decimal("0.05"))
        result, _ = self.process({**self.response(), "actual_cost": "0.04", "currency": "USD"})
        self.assertEqual(result.status, "succeeded", result.error_code)
        advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        self.assertEqual(self.stage.output_snapshot["review_payload"]["candidate_count"], 2)
        good.refresh_from_db()
        self.assertEqual(good.status, "succeeded")

    def test_failed_successor_needs_a_new_explicit_target_not_replay_of_old_action(self):
        failed, _ = self.failed_stage()
        self.retry(failed)
        second, _ = self.process({"text": None, "actual_cost": "0.01", "currency": "USD"})
        self.run = advance_production_run(self.run)
        self.assertEqual(second.status, "failed")
        self.retry(failed)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 3)
        self.retry(second)
        third = GenerationAttempt.objects.get(retry_of=second)
        self.assertEqual(third.request_snapshot, failed.request_snapshot)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 4)

    def test_call_overage_waits_for_confirmation_and_can_resume_same_target(self):
        failed, _ = self.failed_stage()
        self.policy(calls=self.run.generation_task_count)
        result = self.retry(failed)
        self.assertEqual(result.status, "awaiting_approval")
        self.assertEqual(result.blocked_code, "execution_resource_confirmation_required")
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())
        self.confirm_waiting_retry(failed)
        self.retry(failed)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def test_changed_range_auxiliary_and_uncertain_or_unsettled_are_rejected(self):
        failed, good = self.failed_stage()
        for args in ({"expected_stage_id": uuid4()}, {"expected_attempt_ids": []},
                {"expected_attempt_ids": [good.pk]}, {"expected_attempt_ids": [failed.pk, failed.pk]}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                self.retry(failed, **args)
        for values in ({"error_code": "uncertain_remote_response"}, {"actual_cost": None},
                {"committed_cost": Decimal("0.1")}, {"ignore_late_result": True}):
            before = {key: getattr(failed, key) for key in values}
            GenerationAttempt.objects.filter(pk=failed.pk).update(**values)
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.retry(failed)
            GenerationAttempt.objects.filter(pk=failed.pk).update(**before)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())

    def test_formal_api_uses_server_scope_and_rejects_empty_body(self):
        failed, _ = self.failed_stage()
        client = APIClient()
        client.force_authenticate(self.actor)
        path = f"/api/v1/production-runs/{self.run.pk}/"
        detail = client.get(path)
        self.assertEqual(detail.status_code, 200, detail.data)
        body = detail.data["retry_context"]
        self.assertEqual(body["expected_attempt_ids"], [str(failed.pk)])
        self.assertEqual(client.post(path + "retry/", {}, format="json").status_code, 400)
        response = client.post(path + "retry/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        response = client.post(path + "retry/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def test_retry_preview_is_paginated_read_only_and_matches_original_retry(self):
        from studio.models import GeneratedArtifact
        from studio.services.production_retry_preview import retry_preview_sources, retry_preview_rows
        failed, good = self.failed_stage()
        artifact = GeneratedArtifact.objects.get(generation_attempt=good)
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(status="selected")
        before_artifacts = list(GeneratedArtifact.objects.filter(production_run=self.run).values())
        before_attempts = list(GenerationAttempt.objects.filter(stage_run=self.stage).values())
        client = APIClient(); client.force_authenticate(self.actor)
        path = f"/api/v1/production-runs/{self.run.pk}/retry-preview/"
        params = {"expected_stage_id": str(self.stage.pk), "page_size": 1}
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.production_runs.advance_production_run", side_effect=AssertionError("GET must not advance")):
            first = client.get(path, params)
            self.assertEqual(first.status_code, 200, first.data)
            second = client.get(path, {**params, "page": 2})
            self.assertEqual(second.status_code, 200, second.data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(first.data["count"], 2)
        self.assertTrue(first.data["next"])
        self.assertIsNone(second.data["next"])
        self.assertTrue(first.data["can_submit"])
        self.assertEqual(first.data["retry_context"]["expected_attempt_ids"], [str(failed.pk)])
        rows = {item["attempt_id"]: item for page in (first, second) for item in page.data["results"]}
        self.assertEqual(rows[str(failed.pk)]["disposition"], "retry")
        self.assertEqual(rows[str(good.pk)]["disposition"], "skip")
        self.assertEqual(before_attempts, list(GenerationAttempt.objects.filter(stage_run=self.stage).values()))
        self.assertEqual(before_artifacts, list(GeneratedArtifact.objects.filter(production_run=self.run).values()))
        calls, preparations = retry_preview_sources(self.stage)
        self.assertIn("INDEX", retry_preview_rows(calls, preparations).explain().upper())
        sql = " ".join(q["sql"] for q in queries)
        self.assertNotIn('"request_snapshot"', sql)
        self.assertNotIn('"prepared_request"', sql)
        self.retry(failed)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(preparation_id=good.preparation_id).count(), 1)
        self.assertEqual(before_artifacts, list(GeneratedArtifact.objects.filter(production_run=self.run).values()))

    def test_retry_preview_explains_unknown_cancelled_and_rejects_scope_drift(self):
        failed, good = self.failed_stage()
        client = APIClient(); client.force_authenticate(self.actor)
        path = f"/api/v1/production-runs/{self.run.pk}/retry-preview/"
        params = {"expected_stage_id": str(self.stage.pk)}
        for change in ({"error_code": "uncertain_remote_response"}, {"actual_cost": None},
                {"ignore_late_result": True}, {"status": "awaiting_review"}):
            before = {key: getattr(failed, key) for key in change}
            GenerationAttempt.objects.filter(pk=failed.pk).update(**change)
            response = client.get(path, params)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertFalse(response.data["can_submit"], change)
            rows = {item["attempt_id"]: item for item in response.data["results"]}
            self.assertEqual(rows[str(failed.pk)]["disposition"], "attention")
            self.assertEqual(rows[str(good.pk)]["disposition"], "skip")
            self.assertTrue(response.data["reason"])
            GenerationAttempt.objects.filter(pk=failed.pk).update(**before)
        self.assertEqual(client.get(path, {"expected_stage_id": str(uuid4())}).status_code, 400)
        self.assertEqual(client.get(path).status_code, 400)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())

    def test_retry_preview_unsubmitted_units_and_owner_scope(self):
        from django.contrib.auth import get_user_model
        preparation = self.prepare()
        ExecutionPreparation.objects.filter(pk=preparation.pk).update(status="blocked", reason_code="isolated_input_missing")
        self.run.current_stage_key = self.stage.stage_key
        self.run.status = self.stage.status = "blocked"
        self.run.save(update_fields=("status", "current_stage_key")); self.stage.save(update_fields=("status",))
        client = APIClient(); client.force_authenticate(self.actor)
        path = f"/api/v1/production-runs/{self.run.pk}/retry-preview/"
        params = {"expected_stage_id": str(self.stage.pk)}
        response = client.get(path, params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["retry_context"]["expected_attempt_ids"], [])
        self.assertEqual(response.data["mode"], "stage_flow")
        self.assertEqual(response.data["preparation_count"], 1)
        self.assertEqual(response.data["results"][0]["disposition"], "attention")
        self.assertEqual(response.data["results"][0]["error_code"], "isolated_input_missing")
        self.assertTrue(response.data["can_submit"])
        ExecutionPreparation.objects.filter(pk=preparation.pk).update(status="cancelled")
        stopped = client.get(path, params)
        self.assertFalse(stopped.data["can_submit"])
        self.assertIn("停止", stopped.data["reason"])
        self.assertFalse(GenerationAttempt.objects.filter(stage_run=self.stage).exists())
        other = get_user_model().objects.create_user(username="isolated-retry-preview-other")
        client.force_authenticate(other)
        self.assertEqual(client.get(path, params).status_code, 404)

    def test_retry_preview_annotation_preserves_media_failure_checks(self):
        from types import SimpleNamespace
        from studio.services.production_runs import _definitive_media_failure
        from studio.services.production_retry_preview import retry_call_impact
        from studio.services.execution_production_attempts import retry_check_queryset
        failed, _ = self.failed_stage()
        with CaptureQueriesContext(connection) as queries:
            rows = list(retry_check_queryset(GenerationAttempt.objects.filter(pk=failed.pk)))
            self.assertEqual(retry_call_impact(rows[0])[0], "retry")
        self.assertEqual(len(queries), 1)
        self.assertNotIn('"outputs" AS', queries[0]["sql"])
        # Recorded ComfyUI facts only; no task polling, output body or artifact relation query.
        task = SimpleNamespace(status="failed", submission_state="terminal_failed", submission_token="", comfy_prompt_id="")
        attempt = SimpleNamespace(status="failed", operation="isolated.comfy", generation_task=task,
            retry_task_has_outputs=False, retry_has_artifacts=False, retry_task_has_artifacts=False)
        self.assertTrue(_definitive_media_failure(attempt)[0])
        attempt.retry_task_has_outputs = True
        self.assertFalse(_definitive_media_failure(attempt)[0])
        attempt.retry_task_has_outputs = False
        task.submission_state = "submitting"
        self.assertFalse(_definitive_media_failure(attempt)[0])

    def test_batch_call_review_preserves_confirmation_but_rolls_back_allocations(self):
        self.pass_smoke()
        advance_production_run(self.run)
        failures = [self.process({"text": None, "actual_cost": "0.01", "currency": "USD"})[0] for _ in range(2)]
        self.run = advance_production_run(self.run)
        self.policy(calls=3)  # Two explicit retries will require review.
        result = self.retry(failures[0], expected_attempt_ids=[row.pk for row in failures])
        self.assertEqual(result.status, "awaiting_approval")
        self.assertEqual(result.blocked_code, "execution_resource_confirmation_required")
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, 2)
        self.assertEqual(self.run.committed_cost_total, 0)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of__isnull=False).exists())
        for failed in failures:
            prep = ExecutionPreparation.objects.get(pk=failed.preparation_id)
            if prep.reason_code == "execution_resource_confirmation_required":
                self.confirm_waiting_retry(failed)
        self.retry(failures[0], expected_attempt_ids=[row.pk for row in failures])
        self.assertEqual(GenerationAttempt.objects.filter(retry_of__isnull=False).count(), 2)

    def test_async_delivery_identity_changes_only_for_a_new_attempt(self):
        from types import SimpleNamespace
        from studio.services.execution_production_media import production_submission_payload
        prep_id, first_id, second_id = uuid4(), uuid4(), uuid4()
        snapshot = {"payload": {"idempotency_key": str(prep_id), "seed": 42, "prompt": "unchanged",
            "reference_inputs": [{"slot": "subject", "asset_id": "isolated-asset"}]}}
        first = SimpleNamespace(pk=first_id, preparation_id=prep_id)
        second = SimpleNamespace(pk=second_id, preparation_id=prep_id)
        payload = production_submission_payload(first, snapshot)
        self.assertEqual(payload, production_submission_payload(first, snapshot))
        other = production_submission_payload(second, snapshot)
        self.assertNotEqual(payload.pop("idempotency_key"), other.pop("idempotency_key"))
        self.assertEqual(payload, other)
        self.assertEqual(snapshot["payload"]["idempotency_key"], str(prep_id))
        with self.assertRaises(ValidationError):
            production_submission_payload(first, {"payload": {"idempotency_key": "wrong"}})

    def test_budget_review_confirm_does_not_implicitly_retry_or_modify_system_policy(self):
        failed, _ = self.failed_stage()
        self.policy(budget="0.1")
        self.assertEqual(self.retry(failed).blocked_code, "execution_resource_confirmation_required")
        self.confirm_waiting_retry(failed)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed).exists())
        from studio.services.execution_resource_policy import read_resource_policy
        self.assertEqual(read_resource_policy().value["values"]["budget_by_currency"]["USD"], "0.1")
        self.retry(failed)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def policy(self, *, budget="1", calls=100):
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: {
            "budget_by_currency": {"USD": budget}, "call_count": calls, "output_length": 4096}},
            created_by=self.actor, change_summary="isolated retry review")
        publish_config_revision(revision, published_by=self.actor)

    def confirm_waiting_retry(self, failed):
        from studio.services.execution_resource_policy import confirm_resource_review
        prep = ExecutionPreparation.objects.get(pk=failed.preparation_id)
        review = prep.contract.evaluation["root_checks"]["resource_review"]
        confirm_resource_review(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            fingerprint=review["fingerprint"], actor=self.actor)

    def test_retry_does_not_duplicate_another_candidates_queued_call(self):
        self.pass_smoke()
        advance_production_run(self.run)
        failed, _ = self.process({"text": None, "actual_cost": "0.01", "currency": "USD"})
        waiting = GenerationAttempt.objects.get(stage_run=self.stage, status="queued")
        # Media stages may report the failed unit while a sibling is queued.
        self.stage.status, self.run.status = "blocked", "blocked"
        self.stage.save(update_fields=("status",))
        self.run.save(update_fields=("status",))
        self.retry(failed)
        self.assertEqual(GenerationAttempt.objects.filter(preparation_id=waiting.preparation_id).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(retry_of=failed).count(), 1)

    def test_invalid_returned_text_is_preserved_for_review_not_stage_retry(self):
        from studio.models import ExecutionTextResponseReview
        self.pass_smoke()
        advance_production_run(self.run)
        incomplete, peer = self.process({"text": "invalid", "actual_cost": "0.02", "currency": "USD"})
        self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.run = advance_production_run(self.run)
        self.assertEqual((incomplete.status, incomplete.error_code), ("awaiting_review", "execution_output_review_required"))
        review = ExecutionTextResponseReview.objects.get(attempt=incomplete)
        self.assertEqual(review.original_size, len(b"invalid"))
        original_sha = review.original_sha256
        with self.assertRaises(ValidationError):
            self.retry(incomplete)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 2)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=incomplete).exists())
        self.run.refresh_from_db(); review.refresh_from_db()
        self.assertEqual(self.run.actual_cost_total, Decimal("0.05"))
        self.assertEqual(review.original_sha256, original_sha)
        self.assertIsNone(review.confirmed_at)
        peer.invoke.assert_called_once()
