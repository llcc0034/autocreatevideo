"""Formal manual API acceptance with real confirmed revisions, no remote AI."""
from copy import deepcopy
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.db import connection
from django.db.models.deletion import RestrictedError
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ExecutionPreparation, ProductionManualCommand, ProductionRun
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests import test_production_manual_creation as creation_fixtures


class ProductionManualActionTests(TestCase):
    create = creation_fixtures.ProductionManualCreationTests.create

    def setUp(self):
        creation_fixtures.ProductionManualCreationTests.setUp(self)
        result = self.create()
        self.assertEqual(result.status_code, 201, result.data)
        self.run = ProductionRun.objects.get(pk=result.data["id"])
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content="人工选择的已确认剧本")
        self.revision = confirm_script_revision(preview, user=self.actor)
        self.body = {"action": "adopt", "stage_key": "script_input", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": result.data["execution_plan"]["sha256"],
            "expected_selection_revision": 0, "idempotency_key": "adopt-script", "payload": {"comment": "采用这一修订",
                "content": {"kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256}}}

    def post(self, body=None):
        return self.client.post(self.url, self.body if body is None else body, format="json")

    def test_explicit_adoption_uses_original_decision_and_does_not_start_downstream(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, self.revision.pk)
        self.assertEqual(self.run.status, "awaiting_input")
        self.assertEqual(self.run.stages.get(stage_key="script_input").status, "succeeded")
        self.assertFalse(self.run.stages.exclude(stage_key="script_input").exclude(status="pending").exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        decision = ApprovalDecision.objects.get(pk=response.data["target_id"])
        self.assertEqual(self.run.script_adoption_decision_id, decision.pk)
        self.assertEqual((decision.actor, decision.decided_by_id, decision.revision), ("human", self.actor.pk, 1))
        again = self.post()
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(response.data, again.data)
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        state = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(state.status_code, 200, state.data)
        self.assertEqual(state.data["script_adoption"]["revision"], 1)
        self.assertEqual(state.data["current_script_decision_id"], str(decision.pk))
        self.assertEqual(state.data["script_adoption"]["content"]["id"], str(self.revision.pk))

    def test_stale_selection_and_changed_key_conflict_without_writes(self):
        self.assertEqual(self.post().status_code, 201)
        stale = {**self.body, "idempotency_key": "another-action"}
        self.assertEqual(self.post(stale).status_code, 409)
        different = {**self.body, "expected_selection_revision": 1}
        self.assertEqual(self.post(different).status_code, 409)
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_failed_receipt_rolls_back_adoption_and_decision(self):
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt write failed")):
            self.assertEqual(self.post().status_code, 400)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertIsNone(self.run.script_adoption_decision_id)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertEqual(self.run.stages.get(stage_key="script_input").status, "pending")

    def test_current_decision_is_required_and_cannot_be_deleted_or_silently_reconstructed(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        decision = ApprovalDecision.objects.get(pk=response.data["target_id"])
        with self.assertRaises(RestrictedError):
            decision.delete()
        ProductionRun.objects.filter(pk=self.run.pk).update(script_adoption_decision=None)
        url = f"/api/v1/production-runs/{self.run.pk}/manual-action-options/"
        result = self.client.get(url)
        self.assertEqual(result.status_code, 400, result.data)
        self.assertIn("缺少当前采用决定", str(result.data))
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_adoption_decision_id)
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_pointer_must_reference_the_current_original_script_step_and_matching_revision(self):
        first = self.post()
        body = {**self.body, "expected_selection_revision": 1, "idempotency_key": "adopt-again"}
        second = self.post(body)
        self.assertEqual(second.status_code, 201, second.data)
        self.run.refresh_from_db()
        self.assertEqual(str(self.run.script_adoption_decision_id), second.data["target_id"])
        # The old decision still exists but cannot be substituted for the
        # latest decision of that same step. Reads do not repair the pointer.
        ProductionRun.objects.filter(pk=self.run.pk).update(script_adoption_decision_id=first.data["target_id"])
        url = f"/api/v1/production-runs/{self.run.pk}/manual-action-options/"
        self.assertEqual(self.client.get(url).status_code, 400)
        unrelated = ApprovalDecision.objects.create(stage_run=self.run.stages.get(stage_key="storyboard"),
            revision=1, actor="human", decision="approve", decided_by=self.actor, output_snapshot_sha256="a" * 64)
        ProductionRun.objects.filter(pk=self.run.pk).update(script_adoption_decision=unrelated)
        self.assertEqual(self.client.get(url).status_code, 400)
        ProductionRun.objects.filter(pk=self.run.pk).update(script_adoption_decision_id=second.data["target_id"], script_revision=None)
        self.assertEqual(self.client.get(url).status_code, 400)


    def test_wrong_source_hash_scope_missing_revision_and_unknown_action_never_adopt(self):
        invalids = []
        for key, value in (("expected_sha256", "f" * 64), ("id", str(self.run.pk))):
            bad = deepcopy(self.body); bad["payload"]["content"][key] = value; invalids.append(bad)
        invalids.extend([{**self.body, "expected_selection_revision": None}, {**self.body, "unit_key": "fake-unit"},
            {**self.body, "stage_key": "media_generation", "action": "generate"}])
        for body in invalids:
            with self.subTest(body=body):
                self.assertIn(self.post(body).status_code, (400, 409))
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_recipe_control_cannot_bypass_manual_receipts_and_other_owner_is_rejected(self):
        from studio.services.production_runs import decide_stage, retry_production_run, select_stage_route
        for action in (lambda: decide_stage(self.run, stage_key="script_review", decision="approve", comment="", user=self.actor),
                       lambda: retry_production_run(self.run, user=self.actor, expected_stage_id=self.run.pk, expected_attempt_ids=[]),
                       lambda: select_stage_route(self.run, stage_key="media_generation", route_index=0, user=self.actor)):
            with self.assertRaisesMessage(ValidationError, "人工动作"):
                action()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.post().status_code, 404)
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_new_adoption_cannot_invalidate_started_downstream(self):
        self.run.stages.filter(stage_key="storyboard").update(status="running")
        response = self.post()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_action_state_is_bounded_read_only_and_uses_indexed_identity_queries(self):
        from studio.services.production_manual_actions import manual_action_options
        self.assertEqual(self.post().status_code, 201)
        self.run.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            state = manual_action_options(self.run)
        self.assertEqual(state["script_adoption"]["revision"], 1)
        self.assertEqual(state["current_script_decision_id"], str(self.run.script_adoption_decision_id))
        self.assertFalse(ProductionRun._meta.get_field("script_adoption_decision").db_index)
        reads = [item for item in queries if item["sql"].startswith("SELECT")]
        # Nine identity/adoption reads plus three batched execution-activity
        # reads. Task count must not turn these aggregates into per-task reads.
        self.assertEqual(len(reads), 12, [item["sql"] for item in queries])
        aggregates = [item["sql"] for item in reads if "COUNT(" in item["sql"]]
        self.assertEqual(len(aggregates), 2)
        self.assertTrue(any('FROM "studio_executionpreparation"' in sql for sql in aggregates))
        self.assertTrue(any('FROM "studio_generationattempt"' in sql for sql in aggregates))
        self.assertTrue(all(not row["preparations"] and not row["calls"] for row in state["execution_activity"]))
        self.assertTrue(all(item["sql"].startswith(("SELECT", "SAVEPOINT", "RELEASE SAVEPOINT")) for item in queries))
        for item in reads:
            sql = item["sql"]
            self.assertTrue(sql.startswith("SELECT"))
            self.assertNotIn('"comment"', sql)
            self.assertNotIn('"content"', sql)  # no ScriptRevision body read
            for field in ("request_snapshot", "prepared_request", "resolved_inputs", "manifest_metadata"):
                self.assertNotIn(f'"{field}"', sql)
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    details = [str(row[-1]).upper() for row in cursor.fetchall()]
                    explanation = " ".join(details)
                self.assertIn("SEARCH", explanation)
                self.assertNotIn("SCAN ", explanation)
                if sql in aggregates:
                    # SQLite groups only the indexed rows for this Run's
                    # fixed stage/status(/role) dimensions; no result ordering.
                    self.assertTrue(all(row == "USE TEMP B-TREE FOR GROUP BY"
                        for row in details if "TEMP B-TREE" in row), details)
                else:
                    self.assertNotIn("TEMP B-TREE", explanation)
