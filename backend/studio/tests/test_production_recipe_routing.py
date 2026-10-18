"""RunPlan route choice -> original preparation/worker, isolated provider only."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ExecutionPreparation, ExecutionTextResponseReview, GenerationAttempt
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.production_recipe_routing import read_recipe_candidate_route, require_current_recipe_preparation
from studio.services.production_runs import advance_production_run, select_stage_route
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class RecipeRoutingTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    setUp = fixtures.ProductionDispatchTests.setUp
    def root(self, **kwargs):
        from studio.tests.test_production_automatic_retry import freeze_retry_policy
        run = fixtures.ProductionDispatchTests.root(self, **kwargs)
        freeze_retry_policy(run, 1)  # This route suite explicitly tests zero same-route retries.
        return run
    prepare = fixtures.ProductionDispatchTests.prepare
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process

    def routed(self, mode, *, variants=2):
        route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0])
        second = {**deepcopy(route), "parameter_policies": [{"key": "seed", "policy": "locked_value", "value": 99}]}
        self.run = self.root(routing={"mode": mode, "variant_count": variants, "evaluator": None, "routes": [route, second]})
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))

    def choice(self, number=1):
        return read_recipe_candidate_route(stage=self.stage, plan=self.plan,
            declaration=self.plan.value["recipe"]["definition"]["stages"][0],
            unit_key=f"project:{self.project.pk}", candidate_number=number)

    def missing_text_failure(self, *, settled=True):
        # A returned body that fails parsing must be retained for review instead.
        # Exercise the original worker with a definitive missing-body response.
        response = {"text": None, "request_id": "isolated-missing-text"}
        if settled:
            response.update(actual_cost="0.01", currency="USD")
        failed, peer = self.process(response=response)
        peer.invoke.assert_called_once()
        self.assertEqual((failed.status, failed.error_code), ("failed", "execution_response_text_missing"))
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=failed).exists())
        self.assertFalse(failed.artifacts.exists())
        if settled:
            self.assertEqual((failed.actual_cost, failed.committed_cost), (Decimal("0.01"), Decimal("0")))
        else:
            self.assertIsNone(failed.actual_cost)
            self.assertEqual(failed.committed_cost, failed.estimated_cost)
            self.assertGreater(failed.committed_cost, 0)
        return failed

    def test_manual_gate_then_exact_choice_and_replay(self):
        self.routed("manual_choice")
        result = advance_production_run(self.run)
        self.assertEqual(result.blocked_code, "route_selection_required", result.blocked_reason)
        self.assertEqual(result.status, "awaiting_approval")
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        with self.assertRaises(ValidationError):
            select_stage_route(result, stage_key=self.stage.stage_key, route_index=2, user=self.actor)
        result = select_stage_route(result, stage_key=self.stage.stage_key, route_index=1, user=self.actor)
        self.stage.refresh_from_db()
        self.assertEqual(result.status, "running", result.blocked_reason)
        preparations = list(ExecutionPreparation.objects.filter(stage_run=self.stage))
        self.assertEqual(len(preparations), 2)
        self.assertEqual({p.request_key for p in preparations}, {"route:1:candidate:1", "route:1:candidate:2"})
        self.assertEqual({p.contract.resolved_inputs["parameters"]["values"]["seed"] for p in preparations}, {99})
        count = self.stage.route_events.count()
        self.assertEqual(self.prepare().pk, self.choice().preparation.pk)
        self.assertEqual(self.stage.route_events.count(), count)
        self.stage.status = "awaiting_approval"; self.stage.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "已创建执行准备"):
            select_stage_route(result, stage_key=self.stage.stage_key, route_index=0, user=self.actor)

    def test_ordered_fallback_keeps_other_candidate_and_failed_request(self):
        self.routed("ordered_fallback")
        self.pass_smoke()
        first = self.advance(self.prepare())
        original = deepcopy(first.contract.prepared_request)
        failed = self.missing_text_failure()
        next_preparation = self.prepare()
        self.assertNotEqual(first.pk, next_preparation.pk)
        self.assertEqual(next_preparation.request_key, "route:1:candidate:1")
        from studio.services.production_execution_retry import stage_retry_attempts
        self.assertFalse(stage_retry_attempts(self.stage).filter(pk=failed.pk).exists())
        self.assertEqual(self.prepare(candidate_number=2).request_key, "route:0:candidate:2")
        first.refresh_from_db()
        self.assertEqual(first.contract.prepared_request, original)
        self.assertTrue(self.stage.route_events.filter(event_type="route_fallback", route_index=1,
            code=f"{first.pk}:{failed.pk}").exists())
        self.assertEqual(self.choice().preparation.pk, next_preparation.pk)
        with patch("studio.services.execution_production_attempts.require_unfulfilled_material_unit",
                side_effect=AssertionError("Historical route reads must not apply current call authority")):
            self.assertEqual(self.choice().preparation.pk, next_preparation.pk)
        self.assertEqual(self.prepare().pk, next_preparation.pk)
        with self.assertRaisesMessage(ValidationError, "另一条"):
            require_current_recipe_preparation(first, stage=self.stage)
        with self.assertRaisesMessage(ValidationError, "另一条"):
            dispatch_production_execution(preparation_id=first.pk, expected_preparation_revision=first.revision,
                retry_of_id=failed.pk, actor=self.actor)
        advanced = self.advance(next_preparation)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        succeeded, _ = self.process(response={**self.response(), "actual_cost": "0.01", "currency": "USD"})
        self.assertEqual(succeeded.status, "succeeded", succeeded.error_code)
        self.assertEqual(succeeded.route_index, 1)

    def test_blocked_preparation_does_not_fallback_or_allocate_more_candidates(self):
        self.routed("ordered_fallback", variants=1)
        first = self.advance(self.prepare())
        self.assertEqual(first.status, "blocked")
        self.assertEqual(self.prepare().pk, first.pk)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run=self.stage).exists())
        self.assertFalse(self.stage.route_events.filter(event_type="route_fallback").exists())

    def test_exhausted_fallback_only_retries_current_route_after_human_handoff(self):
        from studio.services.production_runs import retry_production_run
        from studio.services.production_execution_retry import stage_retry_context
        from studio.services.production_recipe_routing import superseded_recipe_preparation_ids
        self.routed("ordered_fallback", variants=1)
        self.pass_smoke()
        first = self.advance(self.prepare())
        failed_a = self.missing_text_failure()
        second = self.advance(self.prepare())
        failed_b = self.missing_text_failure()
        result = advance_production_run(self.run)
        self.assertEqual((result.status, result.blocked_code), ("awaiting_input", "execution_retry_exhausted"))
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(superseded_recipe_preparation_ids(self.stage), {first.pk})
        self.assertEqual(len(queries), 2)
        context = stage_retry_context(self.stage)
        self.assertEqual(context["expected_attempt_ids"], [str(failed_b.pk)])
        retry_production_run(result, user=self.actor, **context)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of=failed_a).exists())
        replacement = GenerationAttempt.objects.get(retry_of=failed_b)
        self.assertEqual(replacement.preparation_id, second.pk)
        self.assertEqual(replacement.route_index, 1)

    def test_original_scheduler_falls_back_only_failed_candidate_then_completes_manifest(self):
        self.routed("ordered_fallback")
        self.pass_smoke()
        self.assertEqual(advance_production_run(self.run).status, "running")
        failed = self.missing_text_failure()
        good, _ = self.process(response={**self.response(), "actual_cost": "0.01", "currency": "USD"})
        self.assertEqual(good.status, "succeeded")
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 3)
        replacement = GenerationAttempt.objects.get(stage_run=self.stage, status="queued")
        self.assertEqual(replacement.route_index, 1)
        self.assertNotEqual(replacement.preparation_id, failed.preparation_id)
        self.assertIsNone(replacement.retry_of_id)
        self.process(response={**self.response(), "actual_cost": "0.01", "currency": "USD"})
        advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        self.assertEqual(self.stage.output_snapshot["review_payload"]["candidate_count"], 2)

    def test_fallback_uses_next_exact_instance_even_when_failed_instance_is_now_inactive(self):
        first_route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0])
        first_version = self.version
        self.pass_smoke()
        self.profile_instance()
        self.pass_smoke()
        second_route = {**deepcopy(first_route), "execution_instance_version_id": str(self.version.pk),
            "expected_definition_sha256": self.definition.sha256}
        self.run = self.root(routing={"mode": "ordered_fallback", "variant_count": 1,
            "evaluator": None, "routes": [first_route, second_route]})
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        first = self.advance(self.prepare())
        self.assertEqual(first.execution_instance_version_id, first_version.pk)
        self.missing_text_failure()
        first_version.instance.is_active = False; first_version.instance.save(update_fields=("is_active",))
        replacement = self.advance(self.prepare())
        self.assertEqual(replacement.execution_instance_version_id, self.version.pk)
        self.assertEqual(replacement.status, "dispatched", replacement.reason)

    def test_unknown_cost_failure_does_not_change_route(self):
        self.routed("ordered_fallback", variants=1)
        self.pass_smoke()
        first = self.advance(self.prepare())
        failed = self.missing_text_failure(settled=False)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, failed.committed_cost)
        self.assertEqual(self.run.actual_cost_total, 0)
        self.assertEqual(self.prepare().pk, first.pk)
        from studio.services.production_execution_recovery import production_retry_decision
        decision = production_retry_decision(failed)
        self.assertEqual(decision["state"], "manual")
        self.assertIn("外部调用还需核对费用", decision["reason"])
        self.assertEqual(self.choice().preparation.pk, first.pk)
        self.assertFalse(self.stage.route_events.filter(event_type="route_fallback").exists())
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)

    def test_retention_failure_does_not_automatically_fallback_even_with_settled_cost(self):
        self.routed("ordered_fallback", variants=1)
        self.pass_smoke()
        first = self.advance(self.prepare())
        frozen = deepcopy(first.contract.prepared_request)
        with patch("studio.services.execution_response_storage.default_storage.save", side_effect=OSError("disk unavailable")):
            failed, peer = self.process(response={**self.response(), "actual_cost": "0.01", "currency": "USD"})
        self.assertEqual((failed.error_code, failed.actual_cost, failed.committed_cost),
            ("execution_response_storage_failed", Decimal("0.01"), Decimal("0")))
        self.assertEqual(self.prepare().pk, first.pk)
        with patch("studio.services.execution_production_text_worker.driver_for",
                side_effect=AssertionError("Storage failure must not invoke another model")):
            result = advance_production_run(self.run)
            self.assertNotEqual(result.status, "succeeded")
            self.assertIsNone(claim_next_text_attempt("storage-no-fallback"))
        self.assertEqual(self.choice().preparation.pk, first.pk)
        self.assertFalse(self.stage.route_events.filter(event_type="route_fallback").exists())
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.contract.prepared_request, frozen)
        peer.invoke.assert_called_once()

    def test_invalid_returned_text_preserves_review_and_never_falls_back(self):
        self.routed("ordered_fallback", variants=1)
        self.pass_smoke()
        first = self.advance(self.prepare())
        frozen = deepcopy(first.contract.prepared_request)
        incomplete, peer = self.process(response={"text": "invalid", "actual_cost": "0.01", "currency": "USD"})
        self.assertEqual((incomplete.status, incomplete.error_code), ("awaiting_review", "execution_output_review_required"))
        self.assertEqual((incomplete.actual_cost, incomplete.committed_cost), (Decimal("0.01"), Decimal("0")))
        review = ExecutionTextResponseReview.objects.get(attempt=incomplete)
        original_sha = review.original_sha256
        self.assertEqual(review.original_text, "invalid")
        self.assertEqual(self.prepare().pk, first.pk)
        # Neither scheduler advancement, a read, nor replay can submit another call.
        with patch("studio.services.execution_production_text_worker.driver_for",
                side_effect=AssertionError("Pending review must not invoke a provider")):
            result = advance_production_run(self.run)
            self.assertEqual((result.status, result.blocked_code), ("awaiting_input", "execution_output_review_required"))
            self.assertEqual(self.choice().preparation.pk, first.pk)
            self.assertEqual(process_text_attempt(incomplete).status, "awaiting_review")
            self.assertIsNone(claim_next_text_attempt("pending-review-no-repeat"))
        first.refresh_from_db(); review.refresh_from_db()
        self.assertEqual(first.contract.prepared_request, frozen)
        self.assertEqual(review.original_sha256, original_sha)
        self.assertIsNone(review.confirmed_at)
        self.assertFalse(incomplete.artifacts.exists())
        self.assertIsNone(result.script_revision_id)
        self.assertEqual(result.actual_cost_total, Decimal("0.01"))
        self.assertFalse(self.stage.route_events.filter(event_type="route_fallback").exists())
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)
        peer.invoke.assert_called_once()

    def test_missing_fallback_event_is_not_reconstructed_on_read(self):
        self.routed("ordered_fallback", variants=1)
        self.pass_smoke()
        first = self.advance(self.prepare())
        self.missing_text_failure()
        self.prepare()
        self.stage.route_events.filter(event_type="route_fallback").delete()
        with self.assertRaisesMessage(ValidationError, "正式选路记录"):
            self.choice()
        first.refresh_from_db()
        self.assertEqual(first.status, "dispatched")

    def test_unrecognized_request_key_cannot_hide_a_preparation_and_allocate_again(self):
        self.routed("ordered_fallback")
        original = self.prepare()
        count = self.stage.route_events.count()
        for key in ("unexpected", "route:0:candidate:01", "route:0:candidate:0", "route:9:candidate:1",
                "route:0:candidate:2", "route:1:candidate:1"):
            # Deliberately corrupt only this isolated fixture, bypassing the
            # normal immutable writer; the reader must not turn it into absence.
            ExecutionPreparation._base_manager.filter(pk=original.pk).update(request_key=key)
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    self.choice()
                with self.assertRaises(ValidationError):
                    self.prepare()
                self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
                self.assertEqual(self.stage.route_events.count(), count)
                self.assertFalse(GenerationAttempt.objects.filter(stage_run=self.stage).exists())
                original.refresh_from_db()
                self.assertEqual(original.request_key, key)  # No read-side repair.

    def test_other_units_are_not_scanned_or_repaired_for_candidate_lookup(self):
        self.routed("ordered_fallback")
        first = self.prepare()
        peer = self.prepare(candidate_number=2)
        ExecutionPreparation._base_manager.filter(pk=peer.pk).update(unit_key="appearance:isolated-other-unit", request_key="broken")
        self.assertEqual(self.choice().preparation.pk, first.pk)
        self.assertEqual(self.prepare().pk, first.pk)
        peer.refresh_from_db()
        self.assertEqual(peer.request_key, "broken")
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)

    def test_candidate_lookup_checks_narrow_unit_keys_and_only_loads_selected_detail(self):
        self.routed("ordered_fallback")
        first = self.prepare()
        peer = self.prepare(candidate_number=2)
        # A different candidate's detail is not this read's source of truth.
        # Its own reader still detects damage; we never reinterpret or repair it.
        type(peer.contract)._base_manager.filter(pk=peer.contract.pk).update(intent_sha256="f" * 64)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.choice().preparation.pk, first.pk)
        reads = [row["sql"] for row in queries if 'FROM "studio_executionpreparation"' in row["sql"]]
        self.assertEqual(len(reads), 2, reads)
        headers = [sql for sql in reads if '"resolved_inputs"' not in sql]
        self.assertEqual(len(headers), 1, reads)
        if connection.vendor == "sqlite":
            self.assertIn("JSON_EXTRACT", headers[0])
        self.assertIn('"request_key"', headers[0])
        self.assertTrue(any('JOIN "studio_executionpreparationcontract"' in sql for sql in reads), reads)
        with self.assertRaises(ValidationError):
            self.choice(2)
        query = ExecutionPreparation.objects.filter(stage_run=self.stage,
            unit_key=f"project:{self.project.pk}").order_by().values_list("request_key", "contract__intent__request__request_key")
        if connection.vendor == "sqlite":
            explanation = query.explain()
            self.assertIn("SEARCH", explanation)
            self.assertIn("INDEX", explanation)
            self.assertIn("uk_exec_prep_stage_request", explanation)
            self.assertNotIn("SCAN", explanation)
