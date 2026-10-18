"""Exact-mode Bible dependency checks, not full Recipe publication or Smoke."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import ExecutionVersionContract, ModelExecutionInstanceVersion, ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun
from studio.services.execution_instances import publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _assert_frozen_bible_run_closure, _routing_requires_entity_reference_bible, _validate_delivery_recipe_closure, ProductionRunConflict
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_execution_workflow_request_media as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class BibleSourceClosureTests(SimpleTestCase):
    @staticmethod
    def stages(*, manual):
        # Stage-closure unit fixture only; route/resource checks belong to the
        # formal API suite. No retired normalize/compile stages are accepted.
        return [{"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            *([] if manual else [{"key": "bible_materialize", "mode": "deterministic", "approval": "auto"}]),
            {"key": "bible_review", "mode": "manual" if manual else "review", "approval": "required"},
            {"key": "storyboard", "mode": "manual", "approval": "required"},
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "bible_image_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"}]

    def test_explicit_manual_and_materialized_bible_use_the_same_delivery_closure(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                _validate_delivery_recipe_closure(self.stages(manual=manual))

    def test_manual_bible_cannot_hide_a_materialization_step(self):
        stages = self.stages(manual=True)
        stages.insert(1, {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"})
        with self.assertRaisesMessage(ValidationError, "纯人工 Bible 不得同时声明"):
            _validate_delivery_recipe_closure(stages)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class RecipeBibleClosureTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version

    def setUp(self):
        fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        self.instance_version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.route = {"execution_instance_version_id": str(version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": {}, "parameter_policies": []}
        self.routing = {"mode": "fixed", "variant_count": 1, "evaluator": None, "routes": [self.route]}
        self.stages = [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "bible_image_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto", "routing": self.routing},
            {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"}]

    def test_selected_instance_mode_retains_required_bible_generation_and_review(self):
        self.assertTrue(_routing_requires_entity_reference_bible(self.routing))
        _validate_delivery_recipe_closure(self.stages)
        without_images = [row for row in self.stages if row["key"] not in {"bible_image_generation", "bible_image_review"}]
        with self.assertRaisesMessage(ValidationError, "不能作为人物、场景和道具"):
            _validate_delivery_recipe_closure(without_images)
        with self.assertRaisesMessage(ValidationError, "逐镜冻结"):
            _validate_delivery_recipe_closure([row for row in self.stages if row["key"] != "storyboard_review"])

    def test_missing_or_changed_definition_is_not_interpreted_as_no_bible_requirement(self):
        bad = deepcopy(self.routing)
        bad["routes"][0]["expected_definition_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            _routing_requires_entity_reference_bible(bad)
        bad["routes"] = [{"conditioning_contract": {"modes": []}}]
        with self.assertRaises(ValidationError):
            _routing_requires_entity_reference_bible(bad)
        contract = ExecutionVersionContract.objects.get(version=self.instance_version)
        contract.definition["resources"]["conditioning_mode"]["requires_entity_reference_bible"] = False
        contract.save(update_fields=("definition",))
        with self.assertRaises(ValidationError):
            _routing_requires_entity_reference_bible(self.routing)

    def test_run_closure_reads_frozen_declarations_not_empty_legacy_stage_routing(self):
        project = SeriesProject.objects.create(owner=self.user, title="Closure fixture", **PROJECT_CONFIG)
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Closure storage fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": self.stages}, schema_version=3)
        recipe.save()
        run = ProductionRun.objects.create(owner=self.user, project=project, plan_source="recipe", recipe_version=recipe,
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, actor=self.user, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256)
        stages = [StageRun.objects.create(production_run=run, stage_key=row["key"], mode=row["mode"], ordinal=index,
            idempotency_key=str(uuid4())) for index, row in enumerate(self.stages, start=1)]
        self.assertEqual(stages[7].routing, {})
        _assert_frozen_bible_run_closure(stages)
        with self.assertRaises(ProductionRunConflict):
            _assert_frozen_bible_run_closure([row for row in stages if row.stage_key != "bible_image_review"])
        stages[0].ordinal = 99
        with self.assertRaises(ProductionRunConflict):
            _assert_frozen_bible_run_closure(stages)
