"""Exact catalog checks with real local prepared Smoke records, no provider I/O."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import ProductionRecipeVersion, ModelDeploymentSmokeAttempt
from studio.services.execution_catalog_readiness import (
    ExecutionRequirements, execution_requirements, published_execution_requirements, catalog_capability_readiness,
)
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.json_contracts import assign_json_contract
from studio.services.readiness import collect_production_readiness, _text_generation_worker_required
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionCatalogReadinessTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    structure_instance = fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response

    def setUp(self):
        fixtures.ProductionDispatchTests.setUp(self)
        ProductionRecipeVersion.objects.exclude(pk=self.run.recipe_version_id).update(status="retired")
        self.stages = self.run.recipe_version.definition["stages"]
        self.worker = Mock(return_value={"ok": True})
        self.tools = Mock(return_value={"ok": True})

    def report(self, requirements=None):
        return catalog_capability_readiness(requirements if requirements is not None else published_execution_requirements(),
            worker_check=self.worker, toolchain_check=self.tools)

    def test_real_schema3_reference_and_duplicate_routes_use_exact_instance_not_old_binding(self):
        stages = deepcopy(self.stages)
        stages[0]["routing"].update(mode="ordered_fallback", routes=stages[0]["routing"]["routes"] * 2)
        with CaptureQueriesContext(connection) as queries:
            requirements = execution_requirements(stages)
        self.assertEqual(set(requirements.versions), {str(self.version.pk)})
        self.assertEqual(requirements.primary_version_ids, {str(self.version.pk)})
        self.assertTrue(requirements.needs_text_worker)
        self.assertFalse(any("promptprofilebinding" in row["sql"].lower() for row in queries))
        report = self.report(requirements)
        self.assertEqual(report["script"]["state"], "blocked")
        self.assertEqual(report["script"]["instance_count"], 1)
        self.assertIn("execution_smoke_missing", report["script"]["reason_codes"])
        self.assertFalse(report["script"]["request_checked"])
        self.assertEqual(report["audio"]["state"], "not_required")
        self.worker.assert_called_once_with("text_generation")

    def test_real_combination_result_uses_configured_ttl_policy(self):
        self.pass_smoke()
        done = ModelDeploymentSmokeAttempt.objects.get(scope="instance", execution_instance_version=self.version)
        self.assertEqual(self.report()["script"]["state"], "ready")
        with patch("studio.services.execution_catalog_readiness.timezone.now", return_value=done.evidence_expires_at + timedelta(seconds=1)):
            expired = self.report()["script"]
            self.assertEqual(expired["state"], "ready")
            self.assertIn("execution_smoke_expired", expired["warning_codes"])
            policies = replace(read_execution_policies(), smoke_policy="require_fresh")
            with patch("studio.services.execution_policy_configuration.read_execution_policies", return_value=policies):
                self.assertIn("execution_smoke_expired", self.report()["script"]["reason_codes"])
        ModelDeploymentSmokeAttempt.objects.filter(pk=done.pk).update(result_sha256="0" * 64)
        damaged = self.report()["script"]
        self.assertIn("execution_evidence_unverifiable", damaged["reason_codes"])
        self.assertEqual(damaged["state"], "blocked")

    def test_scope_cannot_be_rewritten_and_catalog_reads_only_instance_facts(self):
        self.pass_smoke()
        with self.assertRaises(ValidationError):
            ModelDeploymentSmokeAttempt.objects.filter(execution_instance_version=self.version).update(scope="resource")
        with CaptureQueriesContext(connection) as queries:
            row = self.report()["script"]
        self.assertEqual(row["state"], "ready")
        evidence_queries = [item["sql"] for item in queries if 'FROM "studio_modeldeploymentsmokeattempt"' in item["sql"]]
        self.assertTrue(any('"scope" = \'instance\'' in sql for sql in evidence_queries))

    def test_capacity_and_budget_are_not_manufactured_without_a_request(self):
        self.pass_smoke()
        row = self.report()["script"]
        self.assertEqual(row["scope"], "configuration_only")
        self.assertIn("execution_capacity_unknown", row["warning_codes"])
        self.assertNotIn("can_submit", row)
        policies = replace(read_execution_policies(), capacity_policy="require_evidence")
        with patch("studio.services.execution_policy_configuration.read_execution_policies", return_value=policies):
            self.assertIn("execution_capacity_unknown", self.report()["script"]["reason_codes"])

    def test_old_or_damaged_recipe_is_not_treated_as_no_requirements(self):
        recipe = self.run.recipe_version
        assign_json_contract(recipe, "definition", {"schema_version": 2, "stages": []}, schema_version=2)
        recipe.save()
        requirements = published_execution_requirements()
        self.assertEqual(requirements.invalid_recipe_ids, {str(recipe.pk)})
        report = self.report(requirements)
        for kind in ("text", "script", "image", "video", "audio", "assembly"):
            self.assertEqual(report[kind]["state"], "blocked")
            self.assertIn("recipe_contract_invalid", report[kind]["reason_codes"])
        values = {"text_generation_worker_required": True}
        self.assertTrue(_text_generation_worker_required(runtime_values=values, requirements=requirements))
        recipe.refresh_from_db()
        self.assertEqual(recipe.definition_schema_version, 2)

    def test_missing_old_extra_or_drifted_route_is_rejected_instead_of_ignored(self):
        for replacement in ({"model_deployment_id": str(self.version.deployment_id)},
                {**self.stages[0]["routing"]["routes"][0], "workflow_version_id": "old"},
                {**self.stages[0]["routing"]["routes"][0], "expected_definition_sha256": "0" * 64}):
            stages = deepcopy(self.stages)
            stages[0]["routing"]["routes"] = [replacement]
            with self.subTest(route=replacement), self.assertRaises(ValidationError):
                execution_requirements(stages)
        stages = deepcopy(self.stages)
        stages[0].pop("routing")
        with self.assertRaises(ValidationError):
            execution_requirements(stages)

    def test_worker_and_pipeline_remain_separate_explicit_checks(self):
        self.pass_smoke()
        self.worker.return_value = {"ok": False}
        self.assertIn("text_generation_worker_unavailable", self.report()["script"]["reason_codes"])
        with patch("studio.services.readiness._worker_check", return_value={"ok": True}):
            report = collect_production_readiness()
        self.assertFalse(report["request_checked"])
        self.assertEqual(report["status"], "blocked")
        row = report["recipes"][0]
        self.assertIn("recipe_contract_invalid", row["reason_codes"])
        self.assertEqual(row["capabilities"]["script"]["state"], "ready")

    def test_empty_catalog_and_manual_stage_do_not_require_models_or_probe_providers(self):
        report = self.report(ExecutionRequirements(assembly=True))
        self.assertEqual(report["script"]["state"], "not_required")
        self.assertEqual(report["assembly"]["state"], "ready")
        self.worker.assert_called_once_with("assembly")
        self.tools.assert_called_once()
        self.assertNotIn("routes", str(report))

    def test_potential_auxiliary_does_not_become_a_mandatory_call_without_request_fields(self):
        requirements = published_execution_requirements()
        self.assertIn("execution_smoke_missing", self.report(requirements)["script"]["reason_codes"])
        # Isolate the role projection: the same real untested configuration
        # is conditional here, not proof of an admitted production dependency.
        requirements.primary_version_ids.clear()
        requirements.route_groups.clear()
        row = self.report(requirements)["script"]
        self.assertEqual(row["state"], "ready")
        self.assertIn("execution_auxiliary_attention", row["warning_codes"])
        self.assertFalse(row["request_checked"])
        self.assertEqual(row["viable_instance_count"], 0)

    def test_explicit_alternative_is_not_an_additional_mandatory_call(self):
        self.pass_smoke()
        stages = deepcopy(self.stages)
        self.profile_instance()  # Another exact published version, without Smoke.
        alternative = {**stages[0]["routing"]["routes"][0],
            "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256}
        stages[0]["routing"]["mode"] = "ordered_fallback"
        stages[0]["routing"]["routes"].append(alternative)
        row = self.report(execution_requirements(stages))["script"]
        self.assertEqual((row["state"], row["instance_count"], row["viable_instance_count"]), ("ready", 2, 1))
        self.assertIn("execution_route_alternative_attention", row["warning_codes"])
        stages[0]["routing"].update(mode="fixed", routes=[alternative])
        self.assertIn("execution_smoke_missing", self.report(execution_requirements(stages))["script"]["reason_codes"])

    def test_recipe_catalog_query_is_narrow_indexed_and_has_no_runtime_writes(self):
        with CaptureQueriesContext(connection) as queries:
            requirements = published_execution_requirements()
        self.assertFalse(requirements.invalid_recipe_ids)
        recipe_queries = [row["sql"] for row in queries if 'FROM "studio_productionrecipeversion"' in row["sql"]]
        self.assertEqual(len(recipe_queries), 1)
        self.assertNotIn('"description"', recipe_queries[0])
        self.assertNotIn('"name"', recipe_queries[0])
        query = ProductionRecipeVersion.objects.filter(status="published").only("id").order_by("id")
        self.assertIn("INDEX", query.explain().upper())
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
