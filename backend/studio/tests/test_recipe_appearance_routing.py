"""Role configuration -> frozen plan -> formal choice -> original primary preparation.

Only isolated database facts; variant dispatch/media effects are not asserted.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion
from studio.services.execution_catalog_readiness import execution_requirements
from studio.services.production_recipe_contracts import validate_recipe_definition
from studio.services.production_recipe_preparations import recipe_stage_context
from studio.services.production_recipe_routing import initial_recipe_route_index, recipe_routing
from studio.services.registry_deletion import recipe_references
from studio.tests import test_production_recipe_bible_regeneration as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeAppearanceRoutingTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare_initial = False
    prepare = fixtures.RecipeBibleRegenerationTests.prepare

    def image_stage(self, route):
        second = deepcopy(route)
        second["parameter_policies"][0]["value"] = 768
        return {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required",
            "routing": {"mode": "manual_choice", "variant_count": 2, "evaluator": None, "routes": [route, second]},
            "variant_routing": {"mode": "manual_choice", "variant_count": 3, "evaluator": "first_succeeded", "routes": [second, route]}}

    def setUp(self):
        fixtures.RecipeBibleRegenerationTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.run.current_stage_key = "bible_review"
        self.run.status = "awaiting_approval"
        self.run.save(update_fields=("current_stage_key", "status"))
        self.stage.status = "pending"
        self.stage.save(update_fields=("status",))
        self.value = deepcopy(self.plan.value["recipe"]["definition"])
        self.declaration = self.value["stages"][1]

    def choose(self, role, index):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-route/",
            {"stage_key": self.stage.stage_key, "appearance_role": role, "route_index": index}, format="json")

    def test_formal_draft_save_reads_both_groups_and_registers_both_references(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        response = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "recipe-" + uuid4().hex, "name": "Isolated role configuration", "definition": self.value}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        self.assertEqual(recipe.definition, self.value)
        self.assertEqual(self.client.get(f"/api/v1/production-recipes/{recipe.pk}/").data["definition"], self.value)
        paths = next(paths for row, paths in recipe_references(self.version.pk) if row.pk == recipe.pk)
        self.assertTrue(any(".variant_routing.routes[" in path for path in paths))
        self.assertTrue(any(".routing.routes[" in path for path in paths))
        self.assertEqual(len(execution_requirements(self.value["stages"]).route_groups), 2)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_frozen_role_choices_are_independent_and_primary_preparation_uses_its_own_config(self):
        with patch("studio.services.production_runs.advance_production_run", side_effect=AssertionError("Future choice cannot advance review")):
            response = self.choose("variant", 0)
            self.assertEqual(response.status_code, 200, response.data)
            self.stage.refresh_from_db()
            self.assertIsNone(self.stage.selected_route_index)
            self.assertEqual(self.stage.selected_variant_route_index, 0)
            with self.assertRaises(ValidationError):
                initial_recipe_route_index(self.stage, self.declaration)
            self.assertEqual(self.choose("primary", 1).status_code, 200)
        self.stage.refresh_from_db()
        plan, declaration = recipe_stage_context(self.stage)
        self.assertEqual(plan.sha256, self.plan.sha256)
        self.assertEqual(declaration, self.declaration)
        self.assertEqual(initial_recipe_route_index(self.stage, declaration), 1)
        self.assertEqual(initial_recipe_route_index(self.stage, declaration, appearance_role="variant"), 0)
        self.assertEqual(recipe_routing(declaration, appearance_role="variant")["variant_count"], 3)
        self.assertEqual(self.stage.route_events.count(), 2)
        self.assertEqual(self.choose("variant", 0).status_code, 200)
        self.assertEqual(self.stage.route_events.count(), 2)
        self.run.status = "running"; self.run.current_stage_key = self.stage.stage_key
        self.run.save(update_fields=("status", "current_stage_key"))
        self.stage.status = "running"; self.stage.save(update_fields=("status",))
        preparation = self.prepare(1)
        self.assertEqual(preparation.contract.intent["request"]["requested"]["route_index"], 1)
        self.assertEqual(preparation.contract.resolved_inputs["parameters"]["values"]["width"], 768)
        self.assertEqual(self.prepare(1).pk, preparation.pk)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_wrong_role_stage_missing_variant_and_unattended_manual_choice_are_rejected(self):
        self.assertEqual(self.choose("unknown", 0).status_code, 400)
        self.assertEqual(self.choose("variant", 99).status_code, 400)
        no_variant = deepcopy(self.declaration)
        del no_variant["variant_routing"]
        with self.assertRaises(ValidationError):
            recipe_routing(no_variant, appearance_role="variant")
        wrong_stage = deepcopy(self.value)
        wrong_stage["stages"][1]["key"] = "media_generation"
        with self.assertRaises(ValidationError):
            validate_recipe_definition(wrong_stage)
        unattended = deepcopy(self.value)
        unattended["interaction_mode"] = "unattended"
        unattended["stages"] = [{**self.declaration, "approval": "auto",
            "routing": {**self.declaration["routing"], "mode": "fixed", "routes": self.declaration["routing"]["routes"][:1]}}]
        with self.assertRaisesMessage(ValidationError, "无人值守不能等待人工选路"):
            validate_recipe_definition(unattended)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
