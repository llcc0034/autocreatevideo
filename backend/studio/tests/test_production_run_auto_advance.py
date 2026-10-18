from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import GenerationAttempt, ProductionRun
from studio.services.production_run_auto_advance import (
    active_recipe_runs, advance_active_recipe_runs, schedule_recipe_run_advance,
)
from studio.services.production_runs import advance_production_run
from studio.tests import test_production_runs_api as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeAutomaticAdvanceTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionRunApiTests.setUpTestData.__func__)
    setUp = fixtures.ProductionRunApiTests.setUp
    write_contract = staticmethod(fixtures.ProductionRunApiTests.write_contract)
    _recipe_definition = fixtures.ProductionRunApiTests._recipe_definition
    _confirmed_script = fixtures.ProductionRunApiTests._confirmed_script

    def create(self, key="automatic-run"):
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.pk), "plan_source": "recipe",
            "recipe_version_id": str(self.recipe.pk), "script_revision_id": self._confirmed_script(),
            "idempotency_key": key, "allow_external_calls": False,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return ProductionRun.objects.get(pk=response.data["id"])

    def test_creation_wakes_only_after_commit_and_stops_at_real_review(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            run = self.create()
            self.assertEqual(run.status, "queued")
            self.assertFalse(run.stages.exclude(status="pending").exists())
        self.assertEqual(len(callbacks), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.current_stage_key, "bible_review")
        self.assertEqual(run.stages.get(stage_key="script_input").status, "succeeded")
        self.assertEqual(run.stages.get(stage_key="storyboard").status, "pending")
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=run).exists())

    def test_rollback_discards_wakeup(self):
        with patch("studio.services.production_run_auto_advance._advance_active_recipe_run") as advance:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(ValueError), transaction.atomic():
                    schedule_recipe_run_advance("unused-rolled-back-id")
                    raise ValueError("rolled back")
        self.assertEqual(callbacks, [])
        advance.assert_not_called()

    def test_worker_completion_callback_cannot_resume_recipe_waiting_for_human(self):
        from studio.services.production_run_auto_advance import schedule_production_run_advance
        run = self.create()
        ProductionRun.objects.filter(pk=run.pk).update(status="awaiting_input", blocked_code="execution_preparation_review_required")
        with patch("studio.services.production_runs._verify_run_snapshots") as verify:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_production_run_advance(run.pk)
        verify.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "awaiting_input")
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=run).exists())

    def test_worklist_reads_only_ids_using_its_index(self):
        self.create()
        query = active_recipe_runs().values_list("id", flat=True)
        self.assertEqual(query.query.values_select, ("id",))
        if connection.vendor == "sqlite":
            explanation = query.explain()
            self.assertIn("USING COVERING INDEX idx_run_auto_advance", explanation)
            self.assertNotIn("TEMP B-TREE", explanation)

    def test_maintenance_recovers_lost_creation_callback_without_page_or_provider(self):
        run = self.create()
        output = StringIO()
        target = "studio.management.commands.run_platform_maintenance."
        with patch(target + "recover_stale_runtime_host_probes", return_value=0), \
             patch(target + "enqueue_due_runtime_host_probes", return_value=0), \
             patch(target + "claim_next_runtime_host_probe", return_value=None):
            call_command("run_platform_maintenance", once=True, stdout=output)
        run.refresh_from_db()
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIn("recipe-runs checked=1", output.getvalue())
        with patch("studio.services.production_runs.advance_production_run") as execute:
            self.assertEqual(advance_active_recipe_runs(heartbeat=lambda: None), 0)
        execute.assert_not_called()

    def test_background_guard_rechecks_status_and_never_touches_manual_or_gates(self):
        run = self.create()
        for status in ("awaiting_input", "awaiting_approval", "blocked", "failed", "cancelled", "succeeded"):
            ProductionRun.objects.filter(pk=run.pk).update(status=status)
            with patch("studio.services.production_runs._verify_run_snapshots") as verify:
                result = advance_production_run(run, automatic=True)
                self.assertEqual(result.status, status)
            verify.assert_not_called()
        ProductionRun.objects.filter(pk=run.pk).update(plan_source="manual", recipe_version=None, status="queued")
        with patch("studio.services.production_manual_assembly.refresh_manual_assembly") as refresh:
            self.assertEqual(advance_production_run(run, automatic=True).plan_source, "manual")
            with self.captureOnCommitCallbacks(execute=True):
                schedule_recipe_run_advance(run.pk)
        refresh.assert_not_called()

    def test_pre_stage_failure_is_visible_and_not_retried_on_every_sweep(self):
        run = self.create()
        with patch("studio.services.production_runs._verify_run_snapshots", side_effect=ValueError("invalid snapshot")), \
             self.assertLogs("studio.services.production_run_auto_advance", level="ERROR"):
            advance_active_recipe_runs(heartbeat=lambda: None)
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        self.assertEqual(run.blocked_code, "production_auto_advance_failed")
        self.assertEqual(advance_active_recipe_runs(heartbeat=lambda: None), 0)
        self.assertFalse(run.stages.exclude(status="pending").exists())

    def test_failure_does_not_replace_a_concurrent_operator_decision(self):
        run = self.create()
        def changed_then_failed(*args, **kwargs):
            ProductionRun.objects.filter(pk=run.pk).update(status="cancelled", updated_at=timezone.now())
            raise ValueError("late callback")
        with patch("studio.services.production_runs.advance_production_run", side_effect=changed_then_failed), \
             self.assertLogs("studio.services.production_run_auto_advance", level="ERROR"):
            advance_active_recipe_runs(heartbeat=lambda: None)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_running_work_is_polled_once_without_resetting_attempts(self):
        run = self.create()
        ProductionRun.objects.filter(pk=run.pk).update(status="running")
        with patch("studio.services.production_runs.advance_production_run") as advance:
            self.assertEqual(advance_active_recipe_runs(heartbeat=lambda: None), 1)
        advance.assert_called_once()
        self.assertTrue(advance.call_args.kwargs["automatic"])
        self.assertEqual(list(active_recipe_runs().values_list("id", flat=True)), [run.pk])
