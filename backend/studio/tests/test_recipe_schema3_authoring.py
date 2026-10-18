"""Real draft API/storage; no production dispatch or remote Smoke claims."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, ProductionRecipeVersion, ProductionRun
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_contracts import validate_recipe_definition
from studio.services.production_runs import _recipe_stages, production_recipe_editor_options
from studio.services.registry_deletion import recipe_references
from studio.tests import test_production_recipe_contracts as route_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeSchema3AuthoringTests(TestCase):
    def test_retired_recipe_allowances_are_rejected_and_not_cloned(self):
        response = self.post()
        recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        url = reverse("production-recipe-detail", args=[recipe.pk])
        retired = {"default_budget_limit": "3.5000", "default_budget_currency": "USD", "default_max_generation_tasks": 12}
        for key, value in retired.items():
            for supplied in (value, None):
                response = self.client.patch(url, {key: supplied}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        recipe.status = "published"; recipe.save(update_fields=("status",))
        self.assertEqual(self.client.patch(url, {"name": "Changed"}, format="json").status_code, 400)
        response = self.client.post(url + "clone-successor/", {"expected_definition_sha256": recipe.definition_sha256}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(set(retired).isdisjoint(response.data))
        self.assertTrue(set(retired).isdisjoint({field.name for field in ProductionRecipeVersion._meta.fields}))

    setUpTestData = classmethod(route_fixtures.RecipeExecutionRouteTests.setUpTestData.__func__)
    write_contract = staticmethod(route_fixtures.RecipeExecutionRouteTests.write_contract)

    def setUp(self):
        route_fixtures.RecipeExecutionRouteTests.setUp(self)
        self.client = APIClient()
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        self.client.force_authenticate(self.actor)
        self.recipe_count = ProductionRecipeVersion.objects.count()
        self.definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": [{
            "key": "media_generation", "mode": "existing_pipeline", "approval": "required",
            "routing": {"mode": "fixed", "routes": [deepcopy(self.route)], "variant_count": 1, "evaluator": None},
        }]}

    def post(self, definition=None):
        return self.client.post(reverse("production-recipe-list"), {"recipe_key": "recipe-" + uuid4().hex,
            "name": "Isolated schema3 recipe", "definition": self.definition if definition is None else definition}, format="json")

    def published_recipe(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        recipe.status = "published"
        recipe.save(update_fields=("status",))
        return recipe

    def clone_recipe(self, recipe):
        return self.client.post(reverse("production-recipe-clone-successor", args=[recipe.pk]),
            {"expected_definition_sha256": recipe.definition_sha256}, format="json")

    def test_successor_copies_frozen_contract_despite_current_interface_drift(self):
        from studio.services.execution_definitions import stored_execution_contract
        from studio.services.json_contracts import json_contract
        recipe = self.published_recipe()
        original = deepcopy(recipe.definition)
        contract = stored_execution_contract(self.version.pk)
        current = deepcopy(contract.definition)
        current["interface"]["input_targets"]["new_language"] = {"kind": "text", "required": True}
        with patch("studio.services.execution_definitions.build_execution_definition",
                return_value=json_contract(current)) as rebuild:
            response = self.clone_recipe(recipe)
            self.assertEqual(response.status_code, 201, response.data)
            rebuild.assert_not_called()
            successor = ProductionRecipeVersion.objects.get(pk=response.data["id"])
            self.assertEqual(successor.definition, original)
            self.assertEqual(successor.definition_sha256, recipe.definition_sha256)
            self.assertEqual(successor.version, recipe.version + 1)
            self.assertEqual(successor.status, "draft")
            self.assertIsNone(successor.published_at)
            self.assertIsNone(successor.published_by_id)
            url = reverse("production-recipe-detail", args=[successor.pk])
            for action in (lambda: self.client.patch(url, {"definition": original}, format="json"),
                    lambda: self.client.post(url + "publish/")):
                rejected = action()
                self.assertEqual(rejected.status_code, 400, rejected.data)
                self.assertIn("当前完整合同与发布定义不一致", str(rejected.data))
            self.assertTrue(rebuild.called)
        recipe.refresh_from_db()
        successor.refresh_from_db()
        self.assertEqual(recipe.definition, original)
        self.assertEqual(recipe.status, "published")
        self.assertEqual(successor.status, "draft")
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())

    def test_successor_retains_retired_inactive_reference_for_explicit_replacement(self):
        from studio.models import ModelExecutionInstance, ModelExecutionInstanceVersion
        recipe = self.published_recipe()
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(status="retired")
        ModelExecutionInstance.objects.filter(pk=self.instance.pk).update(is_active=False)
        response = self.clone_recipe(recipe)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["definition"], recipe.definition)
        self.assertEqual(self.client.post(reverse("production-recipe-publish", args=[response.data["id"]])).status_code, 400)

    def test_successor_rejects_missing_or_wrong_digest_execution_reference_without_writes(self):
        recipe = self.published_recipe()
        original = deepcopy(recipe.definition)
        for change in ({"execution_instance_version_id": str(uuid4())}, {"expected_definition_sha256": "0" * 64}):
            value = deepcopy(original)
            value["stages"][0]["routing"]["routes"][0].update(change)
            assign_json_contract(recipe, "definition", value, schema_version=3)
            recipe.save()
            before = ProductionRecipeVersion.objects.count()
            response = self.clone_recipe(recipe)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(ProductionRecipeVersion.objects.count(), before)

    def test_successor_checks_variant_route_frozen_references_too(self):
        recipe = self.published_recipe()
        value = deepcopy(recipe.definition)
        stage = value["stages"][0]
        stage["key"] = "bible_image_generation"
        stage["variant_routing"] = deepcopy(stage["routing"])
        stage["variant_routing"]["routes"][0]["expected_definition_sha256"] = "0" * 64
        assign_json_contract(recipe, "definition", value, schema_version=3)
        recipe.save()
        before = ProductionRecipeVersion.objects.count()
        response = self.clone_recipe(recipe)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ProductionRecipeVersion.objects.count(), before)

    def test_successor_rejects_tampered_execution_body_or_author_configuration(self):
        from studio.models import ExecutionVersionContract, ModelExecutionInstanceVersion
        recipe = self.published_recipe()
        contract = ExecutionVersionContract.objects.get(version=self.version)
        original = deepcopy(contract.definition)
        ExecutionVersionContract.objects.filter(pk=contract.pk).update(definition={**original, "tampered": True})
        before = ProductionRecipeVersion.objects.count()
        self.assertEqual(self.clone_recipe(recipe).status_code, 400)
        ExecutionVersionContract.objects.filter(pk=contract.pk).update(definition=original)
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(mode="Ref2VA")
        self.assertEqual(self.clone_recipe(recipe).status_code, 400)
        self.assertEqual(ProductionRecipeVersion.objects.count(), before)

    def test_formal_save_retrieve_and_reference_share_exact_execution_contract(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.data)
        recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        self.assertEqual(recipe.definition, self.definition)
        self.assertEqual(recipe.definition_schema_version, 3)
        self.assertEqual(recipe.status, "draft")
        response = self.client.get(reverse("production-recipe-detail", args=[recipe.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["definition"], self.definition)
        self.assertEqual(recipe_references(self.version.pk)[0][0].pk, recipe.pk)
        self.assertEqual(_recipe_stages(recipe), self.definition["stages"])
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())

    def test_draft_can_remain_incomplete_without_inventing_route_or_input(self):
        value = deepcopy(self.definition)
        value["stages"][0]["routing"]["routes"] = []
        self.assertEqual(self.post(value).status_code, 201)
        with self.assertRaises(ValidationError):
            validate_recipe_definition(value, require_complete=True)
        del value["stages"][0]["routing"]
        self.assertEqual(validate_recipe_definition(value), value["stages"])
        with self.assertRaises(ValidationError):
            validate_recipe_definition(value, require_complete=True)

    def test_shot_source_rule_roundtrips_original_draft_api_without_creating_run_or_choice(self):
        from studio.tests.test_recipe_shot_reference_policy import REVIEW
        from studio.models import ShotReferenceUse
        value = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto",
             "material_sources": {"primary": "existing", "variant": "existing"}}, deepcopy(REVIEW)]}
        response = self.post(value)
        self.assertEqual(response.status_code, 201, response.data)
        url = reverse("production-recipe-detail", args=[response.data["id"]])
        self.assertEqual(self.client.get(url).data["definition"], value)
        value["stages"][1]["shot_reference_selection"]["allow_override"] = True
        response = self.client.patch(url, {"definition": value}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["definition"], value)
        value["stages"][1]["shot_reference_selection"]["source_stage_key"] = "future"
        self.assertEqual(self.client.patch(url, {"definition": value}, format="json").status_code, 400)
        self.assertEqual(self.client.get(url).data["definition"]["stages"][1]["shot_reference_selection"]["source_stage_key"], "bible_image_generation")
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ShotReferenceUse.objects.exists())

    def test_old_schema_and_old_resource_route_never_convert_or_write(self):
        values = [{**self.definition, "schema_version": v} for v in (1, 2, True)]
        value = deepcopy(self.definition)
        value["stages"][0]["routing"]["routes"] = [{"model_deployment_id": str(self.deployment.pk), "workflow_version_id": None}]
        values.append(value)
        for key, mode in (("script_normalize", "ai"), ("script_normalize_review", "review"),
                ("prompt_compile", "existing_pipeline")):
            value = deepcopy(self.definition)
            value["stages"] = [{"key": key, "mode": mode, "approval": "required"}]
            values.append(value)
        for value in values:
            with self.subTest(value=value):
                response = self.post(value)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ProductionRecipeVersion.objects.count(), self.recipe_count)

    def test_supplied_invalid_reference_and_parameter_are_not_draft_placeholders(self):
        for changes in ({"expected_definition_sha256": "0" * 64}, {"execution_instance_version_id": str(uuid4())},
                {"parameter_policies": [{"key": "fps", "policy": "locked_value", "value": 25}]},
                {"input_sources": {"undeclared": {"kind": "literal", "value": "not silently removed"}}}):
            value = deepcopy(self.definition)
            value["stages"][0]["routing"]["routes"][0].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.post(value).status_code, 400)
        self.assertEqual(ProductionRecipeVersion.objects.count(), self.recipe_count)

    def test_alias_uuid_is_rejected_before_saving_a_route_that_execution_cannot_match(self):
        identifier = str(self.version.pk)
        for alias in (identifier.replace("-", ""), "{" + identifier + "}", "urn:uuid:" + identifier):
            value = deepcopy(self.definition)
            value["stages"][0]["routing"]["routes"][0]["execution_instance_version_id"] = alias
            with self.subTest(alias=alias):
                response = self.post(value)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("execution_instance_version_id", str(response.data))
                self.assertEqual(value["stages"][0]["routing"]["routes"][0]["execution_instance_version_id"], alias)
        self.assertEqual(ProductionRecipeVersion.objects.count(), self.recipe_count)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())

    def test_stage_and_field_review_authorities_are_not_interchangeable(self):
        stage = self.definition["stages"][0]
        for changes in ({"approval": "policy", "decision_policy": "approve_single_frozen_script_v1"},
                {"field_review": {"mode": "auto"}}, {"field_review": {"mode": "policy", "policy": {"key": "auto"}}}):
            value = {**self.definition, "stages": [{**stage, **changes}]}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_recipe_definition(value)
        unattended = {"schema_version": 3, "interaction_mode": "unattended", "stages": [{
            "key": "script_review", "mode": "review", "approval": "policy", "decision_policy": "approve_single_frozen_script_v1"}]}
        self.assertEqual(validate_recipe_definition(unattended), unattended["stages"])
        for change in ({"approval": "required"}, {"decision_policy": "execution_fields.accept_valid_v1"}):
            with self.assertRaises(ValidationError):
                validate_recipe_definition({**unattended, "stages": [{**unattended["stages"][0], **change}]})

    def test_non_model_stage_cannot_carry_model_route(self):
        value = {**self.definition, "stages": [{**self.definition["stages"][0], "key": "assembly", "mode": "existing_pipeline"}]}
        with self.assertRaises(ValidationError):
            validate_recipe_definition(value)
        value = deepcopy(self.definition)
        value["interaction_mode"] = "unattended"
        value["stages"][0]["approval"] = "auto"
        value["stages"][0]["routing"]["mode"] = "manual_choice"
        with self.assertRaisesMessage(ValidationError, "不能等待人工选路"):
            validate_recipe_definition(value)

    def test_text_review_requires_a_candidate_producer_when_completing_a_recipe(self):
        value = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "script_review", "mode": "review", "approval": "required"}]}
        # Incomplete authoring is still possible; completion must identify an
        # actual candidate source, not reinterpret a confirmed import as AI.
        self.assertEqual(validate_recipe_definition(value), value["stages"])
        with self.assertRaisesMessage(ValidationError, "缺少对应的候选生成阶段"):
            validate_recipe_definition(value, require_complete=True)

    def test_media_instance_cannot_stand_in_for_script_business_output(self):
        value = deepcopy(self.definition)
        value["stages"][0].update(key="script_generate", mode="ai")
        with self.assertRaisesMessage(ValidationError, "script.generate"):
            validate_recipe_definition(value)

    def test_literal_content_is_not_a_registry_reference(self):
        from studio.services.registry_deletion import _recipe_registry_references
        value = deepcopy(self.definition)
        value["stages"][0]["routing"]["routes"][0]["input_sources"] = {
            "literal": {"kind": "literal", "value": {"execution_instance_version_id": str(uuid4()), "model_deployment_id": str(uuid4())}}}
        references = list(_recipe_registry_references(value))
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0][0], str(self.version.pk))

    def test_create_rechecks_current_reference_inside_registry_write_guard(self):
        from studio.models import ModelExecutionInstance
        from studio.platform_views import ProductionRecipeVersionViewSet
        original = ProductionRecipeVersionViewSet.perform_create
        def changed_before_write(view, serializer):
            ModelExecutionInstance.objects.filter(pk=self.instance.pk).update(is_active=False)
            return original(view, serializer)
        with patch.object(ProductionRecipeVersionViewSet, "perform_create", changed_before_write):
            response = self.post()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ProductionRecipeVersion.objects.count(), self.recipe_count)

    def test_valid_partial_draft_cannot_be_published_without_delivery_closure(self):
        response = self.post()
        recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        response = self.client.post(reverse("production-recipe-publish", args=[recipe.pk]), {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("缺少统一交付闭包阶段", str(response.data))
        recipe.refresh_from_db()
        self.assertEqual(recipe.status, "draft")
        self.assertIsNone(recipe.published_at)
        self.assertFalse(ProductionRun.objects.exists())

    def test_published_and_old_snapshot_read_do_not_convert(self):
        recipe = ProductionRecipeVersion(recipe_key="old-row", version=1, name="old isolated", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 1, "stages": [{"key": "delivery", "mode": "existing_pipeline"}]})
        recipe.save()
        with self.assertRaisesMessage(ValidationError, "schema3"):
            _recipe_stages(recipe)
        response = self.client.patch(reverse("production-recipe-detail", args=[recipe.pk]), {"definition": self.definition}, format="json")
        self.assertEqual(response.status_code, 400)
        recipe.refresh_from_db()
        self.assertEqual(recipe.definition_schema_version, 1)

    def test_editor_options_expose_registered_modes_and_no_model_resource_defaults(self):
        response = self.client.get(reverse("production-recipe-editor-options"))
        self.assertEqual(response.status_code, 200, response.data)
        options = response.data
        self.assertEqual(options["definition_schema_version"], 3)
        from studio.services.security_boundaries import boundary_int
        from studio.services.production_budget_configuration import generation_call_limit
        self.assertEqual(options["effective_limits"], {
            "max_variants": boundary_int("security.production.max_variants"),
            "max_routing_routes": boundary_int("security.production.max_routing_routes"),
            "max_generation_tasks": generation_call_limit(),
        })
        self.assertEqual(options, production_recipe_editor_options())
        from studio.services.production_parameter_sources import recipe_parameter_source_options
        self.assertEqual(options["parameter_source_options"], recipe_parameter_source_options())
        self.assertIn("locked_source", {row["value"] for row in options["parameter_policy_options"]})
        self.assertEqual({row["value"] for row in options["interaction_mode_options"]}, {"assisted", "unattended"})
        generate = next(row for row in options["stage_options"] if row["value"] == "script_generate")
        self.assertEqual(generate["routing_modes"], ["ai"])
        self.assertTrue({"script_normalize", "script_normalize_review"}.isdisjoint(row["value"] for row in options["stage_options"]))
        self.assertEqual(next(row for row in options["stage_options"] if row["value"] == "assembly")["routing_modes"], [])
        self.assertNotIn("model_deployment_id", str(options["presets"]))

    def test_recipe_limits_follow_own_catalog_without_comfy_capability_lookup(self):
        with patch("studio.services.production_runs.boundary_int", side_effect={
                "security.production.max_variants": 7, "security.production.max_routing_routes": 2}.__getitem__):
            response = self.client.get(reverse("production-recipe-editor-options"))
        self.assertEqual(response.status_code, 200, response.data)
        from studio.services.production_budget_configuration import generation_call_limit
        self.assertEqual(response.data["effective_limits"], {"max_variants": 7, "max_routing_routes": 2,
            "max_generation_tasks": generation_call_limit()})
