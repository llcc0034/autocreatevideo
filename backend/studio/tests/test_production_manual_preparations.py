"""Manual actions create real preparations, not hidden recipes or model calls."""
from copy import deepcopy
from hashlib import sha256
from decimal import Decimal
import json
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, ArtifactSelectionDecision, AssetGenerationJob, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ProductionRun, SeriesProject
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_queue import due_execution_preparations
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_script_baselines import read_script_adoption_baseline
from studio.tests import test_execution_native_text as native
from studio.tests import test_production_recipe_preparations as recipe
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualPreparationTests(TestCase):
    setUpTestData = classmethod(native.ExecutionNativeTextTests.setUpTestData.__func__)
    profile_instance = recipe.RecipePreparationTests.profile_instance

    def setUp(self):
        native.ExecutionNativeTextTests.setUp(self)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Manual generation", **PROJECT_CONFIG)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        catalog = production_stage_catalog()
        response = self.client.post("/api/v1/production-runs/", {"project_id": str(self.project.pk), "plan_source": "manual",
            "idempotency_key": "manual-root", "allow_external_calls": False,
            "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.stage = self.run.stages.get(stage_key="script_generate")
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.body = {"action": "generate", "stage_key": "script_generate", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": response.data["execution_plan"]["sha256"],
            "expected_selection_revision": None, "idempotency_key": "manual-candidate", "payload": {
                "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256, **deepcopy(self.fixture)}}
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No remote work in a manual action")))

    def post(self, body=None):
        return self.client.post(self.url, self.body if body is None else body, format="json")

    def preparation(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertTrue(response["Location"].endswith(f'/api/v1/execution-preparations/{response.data["target_id"]}/'))
        return ExecutionPreparation.objects.get(pk=response.data["target_id"])

    def test_running_preparation_budget_save_revokes_rechecks_but_preserves_inputs(self):
        from datetime import timedelta
        from uuid import uuid4
        from studio.services.execution_preparation_queue import process_claimed_execution_preparation
        from studio.services.execution_advancement import advance_execution_preparation
        prep = self.preparation()
        original = deepcopy(prep.contract.resolved_inputs)
        prep.status = "blocked"
        prep.next_check_at = timezone.now()
        prep.claim_token, prep.claim_until = uuid4(), timezone.now() + timedelta(minutes=1)
        prep.save()
        old_revision = prep.revision
        url = f"/api/v1/production-runs/{self.run.pk}/authorize-cost/"
        response = self.client.post(url, {"allow_external_calls": True, "currency": "USD"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        stale = process_claimed_execution_preparation(prep)
        self.assertIsNone(stale.claim_token)
        prep.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertNotIn("budget_limit", {field.name for field in ProductionRun._meta.fields})
        self.assertTrue(self.run.allow_external_calls)
        self.assertIsNone(prep.next_check_at)
        self.assertFalse(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        self.assertEqual(prep.contract.resolved_inputs, original)
        self.assertGreater(prep.revision, old_revision)
        with self.assertRaises(ValidationError):
            advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=old_revision, actor=self.actor)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_budget_save_rejects_active_primary_or_auxiliary_calls_and_terminal_runs(self):
        prep = self.preparation()
        attempt = GenerationAttempt.objects.create(stage_run=self.stage, preparation=prep,
            execution_instance_version=self.version, model_deployment=self.version.deployment,
            role="primary", unit_key=prep.unit_key, attempt_number=1, status="queued")
        url = f"/api/v1/production-runs/{self.run.pk}/authorize-cost/"
        payload = {"allow_external_calls": True, "currency": "USD"}
        other = deepcopy(self.body); other["idempotency_key"] = "another-preparation"
        parent_id = self.post(other).data["target_id"]
        for role in ("primary", "auxiliary"):
            for status in ("queued", "running", "submitting", "submitted", "cancel_requested"):
                GenerationAttempt.objects.filter(pk=attempt.pk).update(role=role, status=status,
                    parent_preparation_id=parent_id if role == "auxiliary" else None,
                    auxiliary_purpose="generate_fields" if role == "auxiliary" else "",
                    field_batch_key="a" * 64 if role == "auxiliary" else "")
                response = self.client.post(url, payload, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("进行中的调用", str(response.data))
        GenerationAttempt.objects.filter(pk=attempt.pk).update(status="failed")
        for status in ("succeeded", "cancelled", "cancel_requested"):
            ProductionRun.objects.filter(pk=self.run.pk).update(status=status)
            self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        self.run.refresh_from_db()
        self.assertFalse(self.run.allow_external_calls)
        self.assertNotIn("budget_limit", {field.name for field in ProductionRun._meta.fields})

    def test_currency_options_read_declared_pricing_not_remote_config_or_secrets(self):
        from studio.services.json_contracts import assign_json_contract
        deployment = self.version.deployment
        # Other isolated deployment fixtures inherit this provider too. Scope
        # the catalog fixture so this test specifically verifies precedence.
        from studio.models import ModelDeployment
        ModelDeployment.objects.exclude(pk=deployment.pk).update(status="disabled")
        provider = deployment.profile_version.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config, "pricing": {"currency": "CRD", "fixed_per_request": "0.01"}})
        provider.save()
        assign_json_contract(deployment, "runtime_config", {**deployment.runtime_config, "pricing": {"currency": "CNY", "fixed_per_request": "0"}})
        deployment.save()
        url = "/api/v1/production-runs/budget-options/"
        from studio.services.production_budget_configuration import budget_currency_options
        from studio.services.execution_resource_policy import read_resource_policy
        policy = read_resource_policy()
        # One projection query regardless of deployment count; policy resolution
        # is separately covered by the real API call below.
        with patch("studio.services.execution_resource_policy.read_resource_policy", return_value=policy), self.assertNumQueries(1):
            options = budget_currency_options()
        self.assertIn({"value": "CNY", "label": "人民币（CNY）"}, options["currencies"])
        self.assertNotIn("CRD", [row["value"] for row in options["currencies"]])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {"currencies", "resource_policy"})
        self.assertEqual(response.data["resource_policy"], policy.value)
        self.assertNotIn("driver_config", str(response.data))
        for empty_override in (None, {}):
            assign_json_contract(deployment, "runtime_config", {**deployment.runtime_config, "pricing": empty_override})
            deployment.save()
            self.assertNotIn("CRD", [row["value"] for row in self.client.get(url).data["currencies"]])
        config = dict(deployment.runtime_config); config.pop("pricing")
        assign_json_contract(deployment, "runtime_config", config); deployment.save()
        self.assertIn({"value": "CRD", "label": "CRD"}, self.client.get(url).data["currencies"])
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_manual_channels_use_real_primary_preparation_without_dispatch_adoption_or_due_scan(self):
        prep = self.preparation()
        self.assertEqual((prep.stage_run_id, prep.unit_key, prep.request_key), (self.stage.pk, self.body["unit_key"], self.body["idempotency_key"]))
        self.assertEqual(prep.execution_instance_version_id, self.version.pk)
        self.assertIsNone(prep.next_check_at)
        self.assertFalse(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertIsNone(self.run.recipe_version_id)
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(self.run.stages.exclude(pk=self.stage.pk).exclude(status="pending").exists())
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["prompt_channels"]["values"], self.fixture["manual_channels"])
        self.assertEqual(preview.transport.value["operation_plan"]["plan"]["inputs"]["idea"], self.fixture["inputs"]["idea"]["value"])

    def test_each_script_preparation_keeps_its_own_adoption_baseline_and_replay_does_not_refresh_it(self):
        first = self.preparation()
        original = read_script_adoption_baseline(first)
        self.project.title = "New title after first candidate"
        self.project.master_script = "Manual change before next candidate"
        self.project.save(update_fields=("title", "master_script", "updated_at"))
        replay = self.post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(read_script_adoption_baseline(first), original)
        body = deepcopy(self.body); body["idempotency_key"] = "new-after-project-edit"
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        second = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        current = read_script_adoption_baseline(second)
        self.assertEqual(current["schema_version"], 2)
        self.assertEqual(set(current["fields"]), {"master_script"})
        self.assertNotEqual(current["fields"]["master_script"], original["fields"]["master_script"])
        self.assertEqual(current["fields"]["master_script"], sha256(self.project.master_script.encode("utf-8")).hexdigest())
        self.assertIsNone(current["script_revision_id"])
        self.assertEqual(current["project_id"], str(self.project.pk))
        self.stage.refresh_from_db()
        self.assertNotIn("project_baseline", self.stage.input_snapshot)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_profile_mode_uses_same_preparation_and_manual_review_without_extra_strategy(self):
        self.profile_instance()
        self.body["payload"].update(execution_instance_version_id=str(self.version.pk), expected_definition_sha256=self.definition.sha256,
            manual_channels={}, source_language="English", target_language="English")
        prep = self.preparation()
        self.assertEqual(prep.contract.review_contract["mode"], "manual")
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertIn("Only use the supplied facts.", preview.transport.value["payload"]["messages"][1]["content"])
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_replay_does_not_resolve_current_configuration_or_resume_cancelled_run(self):
        prep = self.preparation()
        frozen = deepcopy(prep.contract.resolved_inputs)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with patch("studio.services.production_manual_preparations.resolve_execution_request_facts", side_effect=AssertionError("No re-resolution")):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["target_id"], str(prep.pk))
        self.assertTrue(response["Location"].endswith(f"/api/v1/execution-preparations/{prep.pk}/"))
        prep.refresh_from_db()
        self.assertEqual(prep.contract.resolved_inputs, frozen)
        self.assertIsNone(prep.next_check_at)
        self.assertEqual(ProductionManualCommand.objects.count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")

    def test_receipt_failure_rolls_back_preparation_and_stage_start(self):
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failure")):
            self.assertEqual(self.post().status_code, 400)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.stage.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((self.stage.status, self.run.status), ("pending", "awaiting_input"))

    def test_changed_intent_conflicts_but_explicit_new_candidate_has_separate_preparation(self):
        first = self.preparation()
        changed = deepcopy(self.body)
        changed["payload"]["inputs"]["idea"]["value"] = "另一个明确候选"
        self.assertEqual(self.post(changed).status_code, 409)
        changed["idempotency_key"] = "second-candidate"
        second = self.post(changed)
        self.assertEqual(second.status_code, 202, second.data)
        self.assertNotEqual(second.data["target_id"], str(first.pk))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_wrong_operation_scope_snapshot_override_and_stale_definition_are_rejected(self):
        cases = [{**self.body, "stage_key": "script_structure"}, {**self.body, "unit_key": "guessed-unit"},
            {**self.body, "expected_selection_revision": 0}, {**self.body, "scope": {"kind": "project", "id": str(self.run.pk)}}]
        for key, value in (("approved", True), ("expected_definition_sha256", "0" * 64), ("execution_instance_version_id", "bad-id")):
            case = deepcopy(self.body); case["payload"][key] = value; cases.append(case)
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(self.post(case).status_code, (400, 409))
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_options_return_actual_stage_operation_and_stopped_stage_is_not_reopened(self):
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(options.status_code, 200)
        self.assertIn({"stage_key": "script_generate", "action": "generate", "operation": "script.generate",
            "output_kinds": [], "scope_kinds": ["project"]}, options.data["actions"])
        self.stage.status = "succeeded"; self.stage.save(update_fields=("status",))
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_explicit_advance_uses_original_worker_and_leaves_candidate_unselected(self):
        from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
        from studio.services.execution_advancement import advance_execution_preparation
        from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
        from studio.services.production_runs import advance_production_run
        # An isolated protocol Smoke, not an existing live resource assertion.
        _, smoke, _ = native.ExecutionNativeTextTests.smoke(self)
        smoke = advance_execution_preparation(preparation_id=smoke.pk, expected_preparation_revision=smoke.revision, actor=self.actor)
        self.assertEqual(smoke.status, "dispatched", smoke.reason)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self.candidate, ensure_ascii=False), "request_id": "isolated-manual-protocol"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            passed = process_deployment_smoke(claim_next_deployment_smoke("manual-protocol"))
        self.assertEqual(passed.status, "succeeded", passed.error_code)
        prep = self.preparation()
        blocked = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(blocked.reason_code, "execution_external_authorization_required")
        self.assertFalse(GenerationAttempt.objects.exists())
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/authorize-cost/", {
            "allow_external_calls": True, "currency": "USD"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        prep.refresh_from_db()
        self.assertIsNone(prep.next_check_at)
        self.assertFalse(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        ready = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        attempt = GenerationAttempt.objects.get(preparation=prep)
        self.assertEqual(attempt.role, "primary")
        self.assertIsNone(attempt.parent_preparation_id)
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            done = process_text_attempt(claim_next_text_attempt("manual-original-worker"))
        self.assertEqual(done.status, "succeeded", done.error_message)
        artifact = GeneratedArtifact.objects.get(generation_attempt=done)
        self.assertEqual(artifact.status, "available")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.run.refresh_from_db()
        advance_production_run(self.run)
        self.assertFalse(self.run.stages.exclude(pk=self.stage.pk).exclude(status="pending").exists())
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(AssetGenerationJob.objects.exists())
