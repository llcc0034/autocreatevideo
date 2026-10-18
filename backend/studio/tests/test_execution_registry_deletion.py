"""Deletion through the original review API; isolated registry/storage fixtures.

These fixtures verify reference integrity, not model or production acceptance.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ExecutionAuxiliaryBinding, ExecutionInputSpec, ExecutionInputTarget,
    ExecutionParameterSpec, ExecutionPreparation, ExecutionVersionContract,
    ModelExecutionInstance, ModelExecutionInstanceVersion, PlatformLifecycleAuditEvent,
    ProductionRecipeVersion, ProductionRun, ProductionRunPlan, SeriesProject, WorkflowSmokeAttempt,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.platform_lifecycle_audit import lifecycle_summary
from studio.tests import test_workflow_configuration_links as resource_fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ExecutionRegistryDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        resource_fixtures.WorkflowConfigurationLinksTests._resource_data.__func__(cls)
        cls.instance = ModelExecutionInstance.objects.create(key="deletion-owner", name="删除复核实例")
        cls.version = ModelExecutionInstanceVersion.objects.create(instance=cls.instance, version=1,
            deployment=cls.deployment, workflow_version=cls.workflow, operation="image.generate",
            prompt_mode="profile", prompt_profile_version=cls.prompt)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def preview(self, obj=None, kind="model_execution_instance_version"):
        response = self.client.post("/api/v1/registry-deletion-preview/", {
            "resource_type": kind, "object_id": str((obj or self.version).pk),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def execute(self, impact, selected=()):
        return self.client.post("/api/v1/registry-deletion-execute/", {
            "resource_type": impact["target"]["resource_type"], "object_id": impact["target"]["object_id"],
            "expected_impact_sha256": impact["impact_sha256"], "selected_dependency_keys": list(selected),
            "confirmation_phrase": impact["confirmation_phrase"],
        }, format="json", HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup")

    def child_configuration(self):
        spec = ExecutionInputSpec.objects.create(version=self.version, key="reference", position=0, kind="image",
            required=True, min_items=1, max_items=2, semantic_contract={"schema_version": 1})
        target = ExecutionInputTarget.objects.create(input_spec=spec, target_kind="workflow_slot", target_key="images", is_primary=True)
        parameter = ExecutionParameterSpec.objects.create(version=self.version, key="cfg", value_type="number",
            policy="overridable", minimum=1, maximum=8, default_value=3)
        auxiliary = ModelExecutionInstanceVersion.objects.create(instance=self.instance, version=2,
            deployment=self.deployment, operation="prompt.normalize", prompt_mode="none")
        binding = ExecutionAuxiliaryBinding.objects.create(version=self.version, purpose="generate_fields", target_version=auxiliary)
        contract = json_contract({"schema_version": 1})
        snapshot = ExecutionVersionContract.objects.create(version=self.version, definition=contract.value,
            schema_version=contract.schema_version, size_bytes=contract.size_bytes, sha256=contract.sha256, target_fingerprint="a" * 64)
        return spec, target, parameter, auxiliary, binding, snapshot

    def recipe(self, *, literal=False, status="draft"):
        route = {"execution_instance_version_id": str(self.version.id)}
        if literal:
            route = {"input_mapping": {"literal": route}}
        value = {"schema_version": 3, "stages": [{"routing": {"routes": [route]}}]}
        recipe = ProductionRecipeVersion(recipe_key="delete-recipe", version=1, name="明确引用", status=status)
        assign_json_contract(recipe, "definition", value, schema_version=3)
        recipe.save()
        return recipe

    def test_draft_review_deletes_owned_configuration_not_shared_resources_and_audits_original(self):
        spec, target, parameter, auxiliary, binding, snapshot = self.child_configuration()
        before = lifecycle_summary(self.version)
        impact = self.preview()
        self.assertTrue(impact["can_execute"], impact)
        self.assertEqual(impact["summary"]["automatic_count"], 5)
        self.assertEqual({r["object_id"] for r in impact["dependencies"]},
            {str(r.pk) for r in (spec, target, parameter, binding, snapshot)})
        response = self.execute(impact)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).exists())
        for obj in (spec, target, parameter, binding, snapshot):
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
        for obj in (self.instance, self.deployment, self.workflow, self.prompt, auxiliary):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        audit = PlatformLifecycleAuditEvent.objects.get(object_type="model_execution_instance_version", object_id=self.version.pk, action="delete")
        self.assertEqual(audit.before_summary, before)
        self.assertEqual(len(response.data["deleted_dependencies"]), 5)

    def test_changed_child_contract_invalidates_preview_without_any_deletion(self):
        spec, target, parameter, auxiliary, binding, snapshot = self.child_configuration()
        impact = self.preview()
        # Even a change without updated_at is visible through the canonical configuration digest.
        ExecutionParameterSpec.objects.filter(pk=parameter.pk).update(maximum=9)
        response = self.execute(impact)
        self.assertEqual(response.status_code, 409, response.data)
        for obj in (self.version, spec, target, parameter, auxiliary, binding, snapshot):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())

    def test_parent_resource_previews_include_explicit_execution_reference(self):
        for obj, kind in ((self.deployment, "model_deployment"), (self.workflow, "workflow_template_version"),
                (self.prompt, "prompt_profile_version"), (self.instance, "model_execution_instance")):
            with self.subTest(kind=kind):
                impact = self.preview(obj, kind)
                self.assertFalse(impact["can_execute"])
                refs = [r for r in impact["dependencies"] if r["relationship"] == "execution_version_reference"]
                self.assertEqual([r["object_id"] for r in refs], [str(self.version.pk)])
                self.assertEqual(refs[0]["disposition"], "blocking_reference")

    def test_auxiliary_reference_is_listed_before_protected_fk_failure(self):
        *_, auxiliary, binding, snapshot = self.child_configuration()
        impact = self.preview(auxiliary)
        self.assertFalse(impact["can_execute"])
        refs = [r for r in impact["dependencies"] if r["reason_code"] == "execution_auxiliary_in_use"]
        self.assertEqual([r["object_id"] for r in refs], [str(self.version.pk)])

    def test_each_persisted_preparation_blocks_draft_deletion(self):
        smoke = WorkflowSmokeAttempt.objects.create(scope="instance", idempotency_key="deletion-smoke",
            workflow_version=self.workflow, deployment=self.deployment, created_by=self.user,
            execution_instance_version=self.version, status="preparing")
        preparation = ExecutionPreparation.objects.create(execution_instance_version=self.version,
            workflow_smoke=smoke,
            purpose="instance_smoke", unit_key="test", request_key="delete-test", status="awaiting_input",
            created_by=self.user, intent_sha256="b" * 64)
        impact = self.preview()
        self.assertFalse(impact["can_execute"])
        self.assertIn(str(preparation.pk), [r["object_id"] for r in impact["dependencies"]])
        self.assertEqual(self.execute(impact).status_code, 409)

    def test_frozen_recipe_is_a_blocking_reference(self):
        recipe = self.recipe(status="published")
        impact = self.preview()
        self.assertFalse(impact["can_execute"])
        ref = next(r for r in impact["dependencies"] if r["object_id"] == str(recipe.pk))
        self.assertEqual(ref["reference_paths"], ["$.stages[0].routing.routes[0].execution_instance_version_id"])

    def test_literal_uuid_is_not_an_execution_reference(self):
        recipe = self.recipe(literal=True)
        impact = self.preview()
        self.assertTrue(impact["can_execute"])
        self.assertFalse(impact["dependencies"])
        self.assertEqual(self.execute(impact).status_code, 200)
        self.assertTrue(ProductionRecipeVersion.objects.filter(pk=recipe.pk).exists())

    def test_draft_recipe_must_be_explicitly_selected_for_deletion(self):
        recipe = self.recipe()
        impact = self.preview()
        self.assertTrue(impact["can_execute"])
        self.assertEqual(impact["summary"]["selectable_count"], 1)
        self.assertEqual(self.execute(impact).status_code, 409)
        self.assertEqual(self.execute(impact, [r["dependency_key"] for r in impact["dependencies"]]).status_code, 200)
        self.assertFalse(ProductionRecipeVersion.objects.filter(pk=recipe.pk).exists())

    def test_published_and_retired_versions_cannot_use_daily_draft_delete(self):
        for status in ("published", "retired"):
            ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(
                status=status, published_at=timezone.now(), published_by=self.user)
            impact = self.preview()
            self.assertEqual(impact["target_policy"]["reason_code"], "execution_version_frozen")
            self.assertEqual(self.execute(impact).status_code, 409)

    def test_inactive_empty_instance_is_removable_but_an_active_one_is_not(self):
        empty = ModelExecutionInstance.objects.create(key="empty-deletion", name="空实例")
        self.assertFalse(self.preview(empty, "model_execution_instance")["can_execute"])
        empty.is_active = False
        empty.save()
        impact = self.preview(empty, "model_execution_instance")
        self.assertTrue(impact["can_execute"])
        self.assertEqual(self.execute(impact).status_code, 200)

    def test_deployment_retirement_checks_actual_published_execution_consumer(self):
        self.deployment.status = "disabled"
        self.deployment.save(update_fields=["status"])
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(
            status="published", published_at=timezone.now(), published_by=self.user)
        url = f"/api/v1/model-deployments/{self.deployment.pk}/retire/"
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("执行实例", str(response.data))
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, "disabled")
        self.instance.is_active = False
        self.instance.save(update_fields=["is_active"])
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, "retired")

    def test_workflow_retirement_checks_execution_consumer_not_old_inline_recipe(self):
        self.workflow.status = "published"
        self.workflow.save(update_fields=["status"])
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(
            status="published", published_at=timezone.now(), published_by=self.user)
        url = f"/api/v1/workflow-template-versions/{self.workflow.pk}/retire/"
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("执行实例", str(response.data))
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.status, "published")
        self.instance.is_active = False
        self.instance.save(update_fields=["is_active"])
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.status, "retired")

    def test_frozen_plan_reference_is_retained_after_current_recipe_changes(self):
        recipe = self.recipe(status="published")
        project = SeriesProject.objects.create(owner=self.user, title="Deletion plan", **PROJECT_CONFIG)
        run = ProductionRun(owner=self.user, project=project, plan_source="recipe", recipe_version=recipe,
            status="awaiting_approval", idempotency_key="deletion-plan", request_sha256="a" * 64)
        for key in ("config_snapshot", "input_snapshot"):
            assign_json_contract(run, key, {"schema_version": 1})
        run.save()
        plan = ProductionRunPlan(run=run, source="recipe", stage_catalog_key="test", stage_catalog_version=1,
            stage_catalog_sha256="a" * 64, created_by=self.user)
        assign_json_contract(plan, "definition", {"schema_version": 1, "recipe": {"definition": recipe.definition}})
        plan.save()
        # Corruption fixture: even when the mutable source no longer exposes it,
        # the independent frozen plan is a real reference, not a literal hit.
        ProductionRecipeVersion.objects.filter(pk=recipe.pk).update(definition={"schema_version": 3, "stages": []})
        impact = self.preview()
        self.assertFalse(impact["can_execute"])
        self.assertIn(str(plan.pk), [r["object_id"] for r in impact["dependencies"] if r["reason_code"] == "frozen_run_plan"])
