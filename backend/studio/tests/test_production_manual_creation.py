"""Formal Run API: blank manual root, no hidden recipe or external work."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion, ProductionRun, ProductionRunPlan, SeriesProject, StageRun
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_runs import advance_production_run, create_production_run
from studio.services.json_contracts import assign_json_contract
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision, create_script_revision
from studio.tests import test_production_run_plans as plan_fixtures


class ProductionManualCreationTests(TestCase):
    def test_detail_reports_invalid_stored_plan_without_repair_or_history_rewrite(self):
        from django.test.utils import CaptureQueriesContext

        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        record = ProductionRunPlan.objects.get(run_id=created.data["id"])
        original = deepcopy(record.definition)
        url = f"/api/v1/production-runs/{record.run_id}/"
        for kind in ("missing_contract_field", "invalid_digest"):
            with self.subTest(kind=kind):
                value = deepcopy(original)
                if kind == "missing_contract_field":
                    value.pop("structure_source_schemas")
                assign_json_contract(record, "definition", value)
                if kind == "invalid_digest":
                    record.definition_sha256 = "0" * 64
                # Deliberately corrupt this isolated fixture below the immutable
                # writer, as a stored contract error, not a supported edit path.
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE studio_productionrunplan SET definition=%s, definition_size_bytes=%s, "
                        "definition_sha256=%s WHERE id=%s", [json.dumps(value), record.definition_size_bytes,
                            record.definition_sha256, record.pk.hex])
                before = list(ProductionRunPlan.objects.filter(pk=record.pk).values())
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["status_code"], 400)
                if kind == "missing_contract_field":
                    self.assertEqual(response.data["error"]["code"], "run_plan_source_conflict")
                    self.assertIn("run_plan", response.data["error"]["details"])
                self.assertNotIn("execution_plan", response.data)
                self.assertEqual(list(ProductionRunPlan.objects.filter(pk=record.pk).values()), before)
                self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
                self.assertFalse(GenerationAttempt.objects.exists())
                self.assertFalse(ExecutionPreparation.objects.exists())
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_detail_batches_relations_and_reuses_boundaries_only_within_each_read(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.services import configuration

        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        url = f"/api/v1/production-runs/{created.data['id']}/"
        original = configuration.resolve_security_boundaries
        for _ in range(2):
            with patch.object(configuration, "resolve_security_boundaries", wraps=original) as resolver, CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data, created.data)
            keys = [tuple(call.kwargs["required_keys"]) for call in resolver.call_args_list]
            self.assertEqual(keys.count(("security.prompt.max_usage_count",)), 1)
            self.assertEqual(keys.count(("security.snapshot.stage_max_bytes",)), 1)
            # Includes request permissions, unlike the serializer-only count.
            self.assertLessEqual(len(queries), 23)
            for table in ("studio_approvaldecision", "studio_artifactselectiondecision", "studio_stagerouteevent"):
                self.assertEqual(sum(f'FROM "{table}"' in q["sql"] for q in queries), 1, table)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_list_omits_stage_relations_and_preserves_read_facts(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/production-runs/", {"project": str(self.project.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["results"][0]["id"], created.data["id"])
        for field in ("config_snapshot_sha256", "input_snapshot_sha256", "release_bundle_id", "status"):
            self.assertEqual(response.data["results"][0][field], created.data[field])
        for table in ("studio_stagerun", "studio_approvaldecision", "studio_artifactselectiondecision", "studio_stagerouteevent"):
            self.assertFalse(any(f'FROM "{table}"' in q["sql"] for q in queries), table)

    def test_detail_refreshes_redaction_policy_without_mutating_snapshots(self):
        from types import SimpleNamespace
        from studio.services import configuration
        created = self.create()
        run = ProductionRun.objects.get(pk=created.data["id"])
        stage = run.stages.first()
        snapshot = {"usage": {"prompt_tokens": 8, "total_tokens": 8}, "credential_id": "test-secret", "nested": {"api_key": "test-secret"}}
        assign_json_contract(stage, "output_snapshot", snapshot)
        stage.save()
        original = configuration.resolve_security_boundaries
        maximum = 10
        def resolve(*, required_keys):
            result = original(required_keys=required_keys)
            return SimpleNamespace(values={**result.values, "security.prompt.max_usage_count": maximum}) if tuple(required_keys) == ("security.prompt.max_usage_count",) else result
        with patch.object(configuration, "resolve_security_boundaries", side_effect=resolve):
            first = self.client.get(f"/api/v1/production-runs/{run.pk}/")
            maximum = 4
            second = self.client.get(f"/api/v1/production-runs/{run.pk}/")
        for response in (first, second):
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("test-secret", str(response.data))
        a = next(row for row in first.data["stages"] if row["id"] == str(stage.pk))
        b = next(row for row in second.data["stages"] if row["id"] == str(stage.pk))
        self.assertEqual(a["output_snapshot"]["usage"]["prompt_tokens"], 8)
        self.assertEqual(b["output_snapshot"]["usage"], {})
        self.assertNotEqual(a["output_snapshot_sha256"], b["output_snapshot_sha256"])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, snapshot)

    def test_external_calls_can_be_authorized_without_per_run_budget_or_advancing(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        run = ProductionRun.objects.get(pk=response.data["id"])
        response = self.client.post(f"/api/v1/production-runs/{run.pk}/authorize-cost/", {
            "allow_external_calls": True, "currency": "USD",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertTrue(run.allow_external_calls)
        self.assertNotIn("budget_limit", response.data)
        self.assertEqual(run.cost_currency, "USD")
        self.assertEqual(run.status, "awaiting_input")
        self.assertFalse(run.stages.exclude(status="pending").exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        from studio.models import PlatformLifecycleAuditEvent
        event = PlatformLifecycleAuditEvent.objects.get(object_id=run.pk, action="authorize_budget")
        self.assertEqual(event.after_summary["cost_currency"], "USD")
        self.assertFalse(event.before_summary["allow_external_calls"])

    def test_authorization_cannot_change_consumed_currency_or_accept_retired_budget(self):
        response = self.create()
        run = ProductionRun.objects.get(pk=response.data["id"])
        run.cost_currency = "USD"
        run.actual_cost_total = "2"
        run.save()
        for changes in ({"currency": "CNY"}, {"budget_limit": "1", "currency": "USD"}):
            response = self.client.post(f"/api/v1/production-runs/{run.pk}/authorize-cost/", changes, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        run.refresh_from_db()
        self.assertEqual(run.cost_currency, "USD")
        self.assertNotIn("budget_limit", {field.name for field in ProductionRun._meta.fields})

    def test_manual_creation_omitted_permission_does_not_authorize_external_calls(self):
        payload = {key: value for key, value in self.payload.items() if key != "allow_external_calls"}
        response = self.create(payload)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data["allow_external_calls"])
        self.assertNotIn("budget_limit", response.data)
        response = self.create({**self.payload, "idempotency_key": "no-currency", "budget_limit": "1"})
        self.assertEqual(response.status_code, 400, response.data)

    def test_call_count_uses_live_system_policy_not_the_frozen_run_limit(self):
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
        from studio.services.execution_production_admission import production_task_budget_state

        expected = read_resource_policy().value["values"]["call_count"]
        payload = {key: value for key, value in self.payload.items() if key != "max_generation_tasks"}
        response = self.create(payload)
        self.assertEqual(response.status_code, 201, response.data)
        run = ProductionRun.objects.get(pk=response.data["id"])
        self.assertNotIn("max_generation_tasks", response.data)
        self.assertEqual(run.config_snapshot["values"][RESOURCE_POLICY]["call_count"], expected)
        run.generation_task_count = expected
        state = production_task_budget_state(run, None)
        self.assertEqual(state["limit"], expected)
        self.assertEqual(state["blockers"], [])
        self.assertGreater(state["allocated"] + state["requested"], state["limit"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def setUp(self):
        plan_fixtures.ProductionRunPlanTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.payload = {"project_id": str(self.project.pk), "plan_source": "manual", "idempotency_key": "create-manual",
            "allow_external_calls": False, "config_overrides": {},
            "manual_plan": {key: value for key, value in self.request.items() if key != "actor"}}
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No model call")))

    def create(self, payload=None):
        return self.client.post("/api/v1/production-runs/", self.payload if payload is None else payload, format="json")

    def test_blank_project_onboarding_wire_payload_creates_once_without_model_calls(self):
        # Match the browser: resource policy is system-owned, not sent by this form.
        payload = dict(self.payload)
        project_count = SeriesProject.objects.count()
        first = self.create(payload)
        self.assertEqual(first.status_code, 201, first.data)
        second = self.create(payload)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        run = ProductionRun.objects.get(pk=first.data["id"])
        self.assertEqual(run.project_id, self.project.pk)
        self.assertEqual(run.status, "awaiting_input")
        self.assertEqual(run.cost_currency, "")
        self.assertNotIn("budget_limit", first.data)
        self.assertFalse(run.allow_external_calls)
        self.assertEqual(SeriesProject.objects.count(), project_count)
        self.assertEqual(ProductionRun.objects.count(), 1)
        self.assertEqual(ProductionRunPlan.objects.count(), 1)
        self.assertFalse(run.stages.exclude(status="pending").exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_per_run_budget_fields_are_rejected_without_creating_or_advancing(self):
        response = self.create({**self.payload, "budget_currency": "USD", "budget_limit": "1.0000"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_invalid_manual_budget_currency_does_not_create_a_run(self):
        for currency, amount in (("", "1"), ("USD", None), ("usd", "1"), ("$", "1")):
            with self.subTest(currency=currency, amount=amount):
                response = self.create({**self.payload, "budget_currency": currency, "budget_limit": amount})
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionRun.objects.exists())

    def test_public_creator_acquires_registry_guard_before_source_validation(self):
        from studio.services.registry_reference_lock import registry_reference_write_guard
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard", wraps=registry_reference_write_guard) as guard:
            with self.assertRaises(ValidationError):
                create_production_run(user=self.actor, project=self.project, plan_source="recipe", recipe=None,
                    script_revision=None, idempotency_key="invalid-source", allow_external_calls=False)
        guard.assert_called_once()
        self.assertFalse(ProductionRun.objects.exists())

    def test_options_show_real_missing_facts_and_blank_creation_waits_without_a_recipe(self):
        count = ProductionRecipeVersion.objects.count()
        options = self.client.get("/api/v1/production-runs/manual-options/", {"project_id": str(self.project.pk)})
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["stage_catalog"]["sha256"], self.catalog.sha256)
        self.assertEqual(options.data["facts"], {"confirmed_script_revisions": 0, "episodes": 0, "shots": 0})
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        run = ProductionRun.objects.get(pk=response.data["id"])
        self.assertEqual(response.data["plan_source"], "manual")
        self.assertEqual(response.data["status"], "awaiting_input")
        self.assertIsNone(response.data["recipe_version"])
        self.assertIsNone(response.data["script_revision"])
        self.assertEqual(response.data["execution_plan"]["sha256"], get_run_execution_plan(run).sha256)
        self.assertEqual(run.stages.count(), sum(bool(row["manual_actions"]) for row in self.catalog.value["stages"]))
        self.assertFalse(run.stages.filter(stage_key="structured_text").exists())
        actions = self.client.get(f"/api/v1/production-runs/{run.pk}/manual-action-options/")
        self.assertEqual(actions.status_code, 200, actions.data)
        self.assertFalse(any(row["stage_key"] == "structured_text" for row in actions.data["actions"]))
        self.assertFalse(run.stages.exclude(status="pending").exists())
        self.assertFalse(run.stages.exclude(mode="manual").exists())
        self.assertEqual(ProductionRecipeVersion.objects.count(), count)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(run.assembly_links.exists())
        self.assertIsNone(run.started_at)
        self.assertEqual(advance_production_run(run).status, "awaiting_input")
        self.assertFalse(run.stages.exclude(status="pending").exists())

    def test_replay_never_reresolves_catalog_content_or_configuration(self):
        first = self.create()
        self.assertEqual(first.status_code, 201, first.data)
        with patch("studio.services.production_manual_creation.resolve_configuration", side_effect=AssertionError("no new configuration")), \
                patch("studio.services.production_manual_creation.freeze_run_execution_plan", side_effect=AssertionError("no new plan")):
            second = self.create()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(ProductionRun.objects.count(), 1)
        self.assertEqual(ProductionRunPlan.objects.count(), 1)
        self.assertEqual(StageRun.objects.count(), sum(bool(row["manual_actions"]) for row in self.catalog.value["stages"]))
        changed = self.create({**self.payload, "allow_external_calls": True})
        self.assertEqual(changed.status_code, 409, changed.data)

    def test_source_union_rejects_old_or_mixed_payloads(self):
        cases = [{key: value for key, value in self.payload.items() if key != "plan_source"},
            {**self.payload, "plan_source": "recipe"}, {**self.payload, "manual_plan": None},
            {**self.payload, "workflow_selection": None}, {**self.payload, "idea": "AI idea"},
            {**self.payload, "script_revision_id": None}, {**self.payload, "plan_source": "unknown"},
            {**self.payload, "extra": "unknown"}]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.create(payload)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionRun.objects.exists())

    def test_plan_failure_or_stage_write_failure_leaves_no_partial_root_or_config(self):
        wrong = deepcopy(self.payload)
        wrong["manual_plan"]["expected_stage_catalog_sha256"] = "f" * 64
        response = self.create(wrong)
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertFalse(ProductionRun.objects.exists())
        with patch("studio.services.production_manual_creation.StageRun.objects.bulk_create", side_effect=ValidationError("stage write failure")):
            response = self.create()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(ProductionRunPlan.objects.exists())

    def test_recipe_request_cannot_add_independent_resource_or_root_reference_choices(self):
        payload = {"project_id": str(self.project.pk), "plan_source": "recipe",
            "recipe_version_id": "11111111-1111-4111-8111-111111111111", "idempotency_key": "obsolete-fields"}
        for key in ("workflow_selection", "bible_image_prompt_profile_binding_id", "reference_inputs"):
            with self.subTest(field=key):
                response = self.create({**payload, key: None})
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(key, str(response.data))
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_incomplete_recipe_is_rejected_before_configuration_or_root_creation(self):
        # Storage fixture, not a claim that this incomplete recipe was admitted.
        recipe = ProductionRecipeVersion(recipe_key="closed-source-union", version=1, name="Isolated recipe", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "assembly", "mode": "existing_pipeline", "approval": "required"}]}, schema_version=3)
        recipe.save()
        payload = {"project_id": str(self.project.pk), "plan_source": "recipe", "recipe_version_id": str(recipe.pk),
            "idempotency_key": "closed-recipe"}
        with patch("studio.services.production_run_configuration.prepare_run_configuration", side_effect=AssertionError("no configuration before admission")):
            response = self.create(payload)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("缺少统一交付闭包阶段", str(response.data))
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(ProductionRunPlan.objects.exists())
        self.assertFalse(StageRun.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        for key in ("workflow_selection", "bible_image_prompt_profile_binding_id", "reference_inputs"):
            with self.subTest(field=key), self.assertRaises(TypeError):
                create_production_run(user=self.actor, project=self.project, plan_source="recipe", recipe=recipe,
                    script_revision=None, idempotency_key="old-private", allow_external_calls=False,
                    **{key: None})

    def test_initial_confirmed_content_remains_explicit_source_not_automatic_adoption(self):
        source, preview = create_pasted_script_source(project=self.project, user=self.actor, content="显式初始内容")
        revision = confirm_script_revision(preview, user=self.actor)
        self.payload["manual_plan"]["initial_content"] = {"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}
        first = self.create()
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["execution_plan"]["value"]["initial_content"]["id"], str(revision.pk))
        self.assertIsNone(first.data["script_revision"])
        run = ProductionRun.objects.get(pk=first.data["id"])
        self.assertFalse(run.artifacts.exists())
        self.assertFalse(run.stages.filter(approval_decisions__isnull=False).exists())
        successor = create_script_revision(source=source, user=self.actor, content="下一版")
        confirm_script_revision(successor, user=self.actor)
        second = self.create()
        self.assertEqual(second.status_code, 200, second.data)

    def test_permission_and_external_budget_controls_are_not_bypassed(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.create().status_code, 404)
        self.assertEqual(self.client.get("/api/v1/production-runs/manual-options/", {"project_id": str(self.project.pk)}).status_code, 404)
        self.client.force_authenticate(self.actor)
        for changes in ({"max_generation_tasks": True},
                {"budget_limit": "NaN"}, {"config_overrides": {"production.allow_external_calls": True}}):
            response = self.create({**self.payload, **changes})
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionRun.objects.exists())
        created = self.create({**self.payload, "allow_external_calls": True})
        self.assertEqual(created.status_code, 201, created.data)
        self.assertTrue(created.data["allow_external_calls"])
        self.assertFalse(GenerationAttempt.objects.exists())
