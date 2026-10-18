"""Atomic receipt boundary using isolated original decision rows, no AI calls."""
from uuid import uuid4
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, Episode, ProductionManualCommand, ProductionRun, StageRun
from studio.services.json_contracts import assign_json_contract
from studio.services.production_manual_commands import execute_manual_command, ManualActionResult, read_manual_command_result
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.tests import test_production_run_plans as plan_fixtures


class ProductionManualCommandTests(TestCase):
    root = plan_fixtures.ProductionRunPlanTests.root
    recipe = plan_fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        plan_fixtures.ProductionRunPlanTests.setUp(self)
        self.run = self.root()
        self.plan = freeze_run_execution_plan(run=self.run, **self.request)
        self.stage = StageRun(production_run=self.run, stage_key="script_review", ordinal=4,
            mode="review", approval_required=True, status="awaiting_approval", idempotency_key=str(uuid4()))
        for field in ("routing", "routing_decision", "config_snapshot", "input_snapshot", "output_snapshot"):
            assign_json_contract(self.stage, field, {"schema_version": 1})
        self.stage.save()
        self.body = {"action": "approve", "stage_key": "script_review", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": self.plan.definition_sha256,
            "expected_selection_revision": 0, "idempotency_key": "explicit-one-action", "payload": {"comment": "Human decision"}}

    def operation(self, *, run, stage, plan, request, actor):
        # This test adapter creates an original human decision. Business stage
        # acceptance remains in the original handler, outside receipt tests.
        self.assertEqual(run.pk, self.run.pk)
        row = ApprovalDecision.objects.create(stage_run=stage, revision=stage.approval_decisions.count() + 1,
            decision="approve", comment=request["payload"]["comment"], decided_by=actor,
            output_snapshot_sha256=stage.output_snapshot_sha256)
        return ManualActionResult("approval_decision", row.pk)

    def execute(self, body=None, **changes):
        kwargs = dict(run_id=self.run.pk, request=self.body if body is None else body, actor=self.actor, execute=self.operation)
        kwargs.update(changes)
        return execute_manual_command(**kwargs)

    def test_one_receipt_and_original_decision_are_replayed_without_another_action(self):
        first, created = self.execute()
        self.assertTrue(created)
        forbidden = Mock(side_effect=AssertionError("replay cannot execute twice"))
        # A network retry after the original operation finished is still a read.
        ProductionRun.objects.filter(pk=self.run.pk).update(status="succeeded")
        again, created = self.execute(execute=forbidden)
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.stage).count(), 1)
        self.assertEqual(ProductionManualCommand.objects.filter(run=self.run).count(), 1)
        self.assertEqual(read_manual_command_result(first), {"kind": "approval_decision", "id": str(first.target_id)})
        forbidden.assert_not_called()

    def test_same_key_different_body_or_selection_revision_conflicts(self):
        self.execute()
        for field, value in (("payload", {"comment": "Different"}), ("expected_selection_revision", 1), ("unit_key", "another-unit")):
            with self.subTest(field=field), self.assertRaisesMessage(ValidationError, "幂等键"):
                self.execute({**self.body, field: value})
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.stage).count(), 1)

    def test_receipt_cannot_claim_replay_success_when_its_original_result_is_missing(self):
        receipt, _ = self.execute()
        ApprovalDecision.objects.filter(pk=receipt.target_id).delete()
        forbidden = Mock(side_effect=AssertionError("replay must not recreate a deleted decision"))
        with self.assertRaisesMessage(ValidationError, "结果不存在"):
            self.execute(execute=forbidden)
        forbidden.assert_not_called()

    def test_wrong_actor_cannot_invoke_or_replay_authorized_action(self):
        forbidden = Mock(side_effect=AssertionError("unauthorized action"))
        for exists in (False, True):
            if exists:
                self.execute()
            with self.assertRaises(PermissionDenied):
                self.execute(actor=self.other, execute=forbidden)
        forbidden.assert_not_called()

    def test_catalog_scope_plan_and_terminal_guards_precede_adapter(self):
        forbidden = Mock(side_effect=AssertionError("must fail before execution"))
        for changes in ({"expected_run_plan_sha256": "f" * 64}, {"stage_key": "unknown"}, {"action": "generate"},
                {"scope": {"kind": "project", "id": str(uuid4())}}, {"scope": {"kind": "shot", "id": str(uuid4())}},
                {"stage_key": "bible_review"}, {"expected_selection_revision": True}, {"handler": "shell"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.execute({**self.body, **changes}, execute=forbidden)
        for status in ("succeeded", "failed", "cancelled"):
            ProductionRun.objects.filter(pk=self.run.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.execute(execute=forbidden)
        forbidden.assert_not_called()

    def test_recipe_roots_cannot_use_manual_actions(self):
        recipe_run = self.root(plan_source="recipe", recipe_version=self.recipe())
        with self.assertRaises(ValidationError):
            self.execute(run_id=recipe_run.pk)

    def test_real_episode_scope_respects_frozen_subset(self):
        one = Episode.objects.create(project=self.project, number=1, title="One")
        two = Episode.objects.create(project=self.project, number=2, title="Two")
        # A fresh explicit manual round, not a rewritten plan.
        self.run = self.root()
        self.plan = freeze_run_execution_plan(run=self.run, **{**self.request,
            "scope": {"kind": "episodes", "episode_ids": [str(one.pk)]}})
        self.stage.production_run = self.run
        self.stage.pk = uuid4()
        self.stage._state.adding = True
        self.stage.save()
        body = {**self.body, "expected_run_plan_sha256": self.plan.definition_sha256,
            "scope": {"kind": "episode", "id": str(two.pk)}}
        with self.assertRaises(ValidationError):
            self.execute(body)
        receipt, created = self.execute({**body, "scope": {"kind": "episode", "id": str(one.pk)}})
        self.assertTrue(created)
        self.assertEqual(receipt.run_id, self.run.pk)

    def test_receipt_failure_rolls_back_original_decision_and_operation_failure_leaves_no_receipt(self):
        with patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("receipt storage failed")):
            with self.assertRaisesMessage(RuntimeError, "receipt storage"):
                self.execute()
        self.assertFalse(self.stage.approval_decisions.exists())
        self.assertFalse(self.run.manual_commands.exists())
        def fail(**kwargs):
            self.operation(**kwargs)
            raise ValidationError("original operation failed")
        with self.assertRaisesMessage(ValidationError, "original operation failed"):
            self.execute(execute=fail)
        self.assertFalse(self.stage.approval_decisions.exists())
        self.assertFalse(self.run.manual_commands.exists())

    def test_result_must_exist_and_belong_to_original_action_root(self):
        for result in (ManualActionResult("approval_decision", uuid4()), ManualActionResult("release_bundle", uuid4()),
                {"status": "succeeded", "text": "not a durable result"}):
            def bad(**kwargs):
                self.operation(**kwargs)
                return result
            with self.subTest(result=result), self.assertRaises(ValidationError):
                self.execute(execute=bad)
            self.assertFalse(self.stage.approval_decisions.exists())
            self.assertFalse(self.run.manual_commands.exists())

    def test_replay_uses_indexed_root_and_receipt_reads_without_plan_or_output_bodies(self):
        self.execute()
        with CaptureQueriesContext(connection) as captured:
            self.execute(execute=Mock(side_effect=AssertionError("no action")))
        queries = [row["sql"] for row in captured if row["sql"].lstrip().upper().startswith("SELECT")]
        # Root + receipt + indexed existence/ownership check of the original
        # result. A receipt must not turn a deleted result into a successful replay.
        self.assertEqual(len(queries), 3)
        self.assertNotIn("input_snapshot", queries[0])
        self.assertNotIn("config_snapshot", queries[0])
        self.assertNotIn("output_snapshot", " ".join(queries))
        if connection.vendor == "sqlite":
            for sql in queries:
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    explain = str(cursor.fetchall()).upper()
                self.assertIn("SEARCH", explain)
                self.assertNotIn("TEMP B-TREE", explain)
