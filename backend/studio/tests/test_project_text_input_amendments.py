"""Isolated project text source correction; no real model calls."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionStageInputAmendment
from studio.services.production_stage_input_amendments import amend_stage_inputs, TEXT_SOURCE_POLICY, SOURCE_POLICY
from studio.services.production_project_text_stages import project_text_stage_contract
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_runs import advance_production_run, retry_production_run
from studio.tests import test_production_recipe_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProjectTextInputAmendmentTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipePreparationTests.setUpTestData.__func__)
    profile_instance = fixtures.RecipePreparationTests.profile_instance
    structure_instance = fixtures.RecipePreparationTests.structure_instance
    root = fixtures.RecipePreparationTests.root
    prepare = fixtures.RecipePreparationTests.prepare

    def setUp(self):
        fixtures.RecipePreparationTests.setUp(self)
        self.structure_instance(scopes=("request", "project"))
        self.profile_instance()
        self.run = self.root(stage_key="script_structure", variants=1, source={"script": {
            "kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}}})
        self.run.current_stage_key = self.stage.stage_key
        self.run.save(update_fields=("current_stage_key",))
        self.changes = [{"route_index": 0, "input_key": "script", "source": {"kind": "literal", "value": "A traveller opens a box."}}]

    def amend(self, action="preview", sha=None, **changes):
        return amend_stage_inputs(**{"stage_run_id": self.stage.pk, "expected_run_plan_sha256": self.plan.sha256,
            "policy": TEXT_SOURCE_POLICY, "source_changes": self.changes, "action": action,
            "expected_amendment_sha256": sha, "actor": self.actor, **changes})

    def test_blocked_scheduler_retry_preserves_plan_and_freezes_amendment_once(self):
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.status, "blocked", self.run.blocked_reason)
        self.assertIn("未选择已确认剧本", self.run.blocked_reason)
        self.stage.refresh_from_db()
        snapshots = deepcopy((self.stage.config_snapshot, self.stage.input_snapshot))
        base = project_text_stage_contract(self.stage)
        preview = self.amend()
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        applied = self.amend("apply", preview["sha256"])
        self.assertEqual(self.amend("apply", preview["sha256"]), applied)
        self.assertEqual(project_text_stage_contract(self.stage), base)
        self.assertEqual(get_run_execution_plan(self.run), self.plan)
        def no_dispatch(**kwargs):
            return ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])
        with patch("studio.services.production_project_text_stages.advance_execution_preparation", side_effect=no_dispatch):
            self.run = retry_production_run(self.run, user=self.actor, expected_stage_id=self.stage.pk, expected_attempt_ids=[])
            self.assertEqual(self.run.status, "running", self.run.blocked_reason)
            prep = ExecutionPreparation.objects.get(stage_run=self.stage)
            advance_production_run(self.run)
            self.assertEqual(ExecutionPreparation.objects.get(stage_run=self.stage).pk, prep.pk)
        self.stage.refresh_from_db()
        self.assertEqual((self.stage.config_snapshot, self.stage.input_snapshot), snapshots)
        self.assertEqual(prep.contract.intent["request"]["requested"]["input_amendment"], {"id": applied["id"], "sha256": applied["sha256"]})
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["operation_inputs"]["source_text"], self.changes[0]["source"]["value"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_wrong_duty_permissions_stale_preview_and_terminal_fail_without_writes(self):
        other = get_user_model().objects.create_user(username="text-amend-other")
        with self.assertRaises(PermissionDenied): self.amend(actor=other)
        for values in ({"policy": SOURCE_POLICY}, {"expected_run_plan_sha256": "0"*64},
                {"action": "apply", "sha": "0"*64}):
            with self.subTest(values=values), self.assertRaises(ValidationError): self.amend(**values)
        self.run.status = "succeeded"
        self.run.save(update_fields=("status",))
        with self.assertRaises(ValidationError): self.amend()
        self.assertFalse(ProductionStageInputAmendment.objects.exists())

    def test_unavailable_or_future_sources_are_rejected_at_preview(self):
        for source in ({"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "episode"}},
                {"kind": "selected_output", "stage_key": "script_generate", "output_key": "generated_script.script.content",
                 "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.amend(source_changes=[{**self.changes[0], "source": source}])
        with self.assertRaises(ValidationError):
            self.amend(source_changes=[{**self.changes[0], "input_key": "undeclared"}])
        self.assertFalse(ProductionStageInputAmendment.objects.exists())

    def test_any_preparation_or_call_blocks_even_unsubmitted(self):
        # Create a real preparation through an unamended isolated fixture.
        self.run = self.root(stage_key="script_structure", source={"script": self.changes[0]["source"]})
        self.run.current_stage_key = self.stage.stage_key
        self.run.save(update_fields=("current_stage_key",))
        self.prepare()
        self.changes[0]["source"]["value"] = "A different explicit input."
        with self.assertRaisesMessage(ValidationError, "已有执行准备或调用"): self.amend()
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
        # Independently exercise the call guard; no provider is invoked.
        self.run = self.root(stage_key="script_structure", source={"script": {"kind": "literal", "value": "Original"}})
        self.run.current_stage_key = self.stage.stage_key
        self.run.save(update_fields=("current_stage_key",))
        with patch("studio.services.production_stage_input_amendments.GenerationAttempt.objects.filter") as calls:
            calls.return_value.exists.return_value = True
            with self.assertRaisesMessage(ValidationError, "已有执行准备或调用"): self.amend()

    def test_http_policy_and_capability_do_not_offer_media_conversion(self):
        self.run.status = "blocked"
        self.run.save(update_fields=("status",))
        client = APIClient(); client.force_authenticate(self.actor)
        detail = client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["stages"][0]["input_amendment_policy"], TEXT_SOURCE_POLICY)
        payload = {"stage_run_id": str(self.stage.pk), "expected_run_plan_sha256": self.plan.sha256,
            "policy": TEXT_SOURCE_POLICY, "source_changes": self.changes, "action": "preview"}
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/stage-input-amendment/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "proposed")
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
