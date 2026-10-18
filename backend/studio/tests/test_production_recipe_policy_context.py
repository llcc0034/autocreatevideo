"""Frozen schema3 policy authorization, without granting production admission."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from studio.models import ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import StageBlocked, _sha256, _verified_unattended_decision_policy
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY, production_stage_catalog
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture


class RecipeDecisionPolicyContextTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="policy-context-owner")
        self.project = SeriesProject.objects.create(owner=self.user, title="Policy context", **PROJECT_CONFIG)
        self.policy = UNATTENDED_DECISION_POLICY_REGISTRY["bible_image_review"]
        # This is a policy-context fixture, not a complete publishable recipe.
        self.recipe = ProductionRecipeVersion(recipe_key="policy-context-fixture", name="Policy context", version=1, status="published")
        assign_json_contract(self.recipe, "definition", {"schema_version": 3, "interaction_mode": "unattended", "stages": [
            {"key": "bible_image_review", "mode": "review", "approval": "policy", "decision_policy": self.policy},
        ]}, schema_version=3)
        self.recipe.save()
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, recipe_version=self.recipe,
            plan_source="recipe", status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        freeze_recipe_request_fixture(self.run, authorized=True)
        self.policy_sha = _sha256({"schema_version": 1, "interaction_mode": "unattended", "stage_key": "bible_image_review",
            "decision_policy": self.policy, "recipe_definition_sha256": self.recipe.definition_sha256})
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="bible_image_review", ordinal=1, mode="review",
            status="running", decision_policy=self.policy, decision_policy_sha256=self.policy_sha, idempotency_key=str(uuid4()))

    def test_policy_reads_frozen_recipe_and_not_current_recipe_configuration(self):
        self.assertEqual(_verified_unattended_decision_policy(self.run, self.stage), (self.policy, self.policy_sha))
        changed = deepcopy(self.recipe.definition)
        changed["interaction_mode"] = "assisted"
        changed["stages"][0] = {"key": "bible_image_review", "mode": "review", "approval": "required"}
        # Deliberately corrupt the mutable DB row to prove it is not the reader.
        assign_json_contract(self.recipe, "definition", changed, schema_version=3)
        self.recipe.save()
        self.assertEqual(_verified_unattended_decision_policy(self.run, self.stage), (self.policy, self.policy_sha))

    def test_changed_policy_or_hash_cannot_replace_the_frozen_decision(self):
        self.stage.decision_policy = "unregistered-policy"
        with self.assertRaises(StageBlocked) as error:
            _verified_unattended_decision_policy(self.run, self.stage)
        self.assertEqual(error.exception.code, "unattended_policy_context_invalid")
        self.stage.decision_policy, self.stage.decision_policy_sha256 = self.policy, "0" * 64
        with self.assertRaises(StageBlocked) as error:
            _verified_unattended_decision_policy(self.run, self.stage)
        self.assertEqual(error.exception.code, "unattended_policy_snapshot_drift")

    def test_no_user_confirmation_cannot_be_treated_as_policy_authority(self):
        freeze_recipe_request_fixture(self.run, authorized=False)
        with self.assertRaises(StageBlocked) as error:
            _verified_unattended_decision_policy(self.run, self.stage)
        self.assertEqual(error.exception.code, "unattended_policy_context_invalid")
