"""Original M3 receipt bounds new recipe preparations and call admission.

All rows/files are isolated. No provider, Smoke, live review or adoption occurs.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit, require_current_recipe_material_requirement
from studio.services.production_recipe_routing import require_current_recipe_preparation
from studio.tests import test_production_recipe_bible_regeneration as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeRequirementPreparationTests(TestCase):
    prepare_initial = False
    setUpTestData = classmethod(fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare = fixtures.RecipeBibleRegenerationTests.prepare

    def setUp(self):
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Recipe requirement tests cannot call external services")))
        fixtures.RecipeBibleRegenerationTests.setUp(self)

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_original_requirement_is_bound_once_and_visible_on_original_detail_api(self):
        preparation = self.prepare(1)
        value = preparation.contract.resolved_inputs["recipe_material_requirement"]
        expected = self.stage.input_snapshot["material_requirements"]
        self.assertEqual(value, {"schema_version": 1, "run_plan_sha256": self.plan.sha256,
            "bible_review": expected["bible_review"], "target": expected["targets"][0]})
        self.assertEqual(value["target"]["setting"], read_media_generation_target(preparation).value["setting"])
        self.assertEqual(preparation.unit_key, f"appearance:{self.setting.variant_id}")
        self.assertEqual(preparation.unit_key, self.stage.input_snapshot["units"][0]["unit_key"])
        self.assertEqual(preparation.contract.resolved_inputs["production_unit"]["scope"], self.scope)
        require_current_recipe_preparation(preparation, stage=self.stage)
        # Whole-stage and direct admission consume the same verified receipt.
        require_current_recipe_material_requirement(preparation, stage=self.stage, requirements=expected)
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{preparation.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["resolved_inputs"]["recipe_material_requirement"], value)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_replay_never_reresolves_material_requirements(self):
        preparation = self.prepare(1)
        before = deepcopy(preparation.contract.resolved_inputs)
        with patch("studio.services.bible_material_requirements.read_bible_material_requirements",
                side_effect=AssertionError("Replay cannot bind today's requirement")), patch(
                "studio.services.bible_material_requirements.read_run_bible_material_requirements",
                side_effect=AssertionError("Replay cannot refreeze stage inputs")):
            self.assertEqual(self.prepare(1).pk, preparation.pk)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, before)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_missing_approval_rejects_new_preparation_and_rolls_back_route_event(self):
        self.content_review.status = "pending"
        self.content_review.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "唯一前置审核"):
            self.prepare(1)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(self.stage.route_events.exists())

    def test_changed_setting_preserves_history_but_blocks_new_candidate_and_submission(self):
        preparation = self.prepare(1)
        before = deepcopy(preparation.contract.resolved_inputs)
        revise_variant(actor=self.actor, variant=self.setting.variant, expected_revision=1,
            value={**setting_payload(self.setting), "appearance_anchor": "Not reviewed in this run"})
        self.assertEqual(self.prepare(1).pk, preparation.pk)
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            self.prepare(2)
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            require_current_recipe_preparation(preparation, stage=self.stage)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, before)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_removed_approval_blocks_direct_admission_even_if_route_is_current(self):
        preparation = self.prepare(1)
        self.content_review.status = "pending"
        self.content_review.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "唯一前置审核"):
            require_current_recipe_preparation(preparation, stage=self.stage)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_missing_or_mismatched_saved_basis_is_not_reconstructed_on_admission(self):
        preparation = self.prepare(1)
        detail = read_execution_preparation_detail(preparation)
        original = deepcopy(detail.resolved_inputs)
        basis = original["recipe_material_requirement"]
        for replacement in (None, {**basis, "schema_version": True}, {**basis, "schema_version": 1.0},
                {**basis, "run_plan_sha256": "0" * 64},
                {**basis, "bible_review": {}}, {**basis, "target": {**basis["target"], "requirement_key": "invented"}}):
            with self.subTest(replacement=replacement):
                detail.resolved_inputs = deepcopy(original)
                if replacement is None:
                    detail.resolved_inputs.pop("recipe_material_requirement")
                else:
                    detail.resolved_inputs["recipe_material_requirement"] = replacement
                # Isolate the semantic verifier from the storage hash checks;
                # do not rewrite or reseal any original persisted fact.
                with patch("studio.services.execution_preparations.read_execution_preparation_detail", return_value=detail):
                    with self.assertRaisesMessage(ValidationError, "缺少匹配的本轮素材需求依据"):
                        require_current_recipe_material_requirement(preparation, stage=self.stage)
        detail.refresh_from_db()
        self.assertEqual(detail.resolved_inputs, original)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMissingRequirementTests(TestCase):
    prepare_initial = False
    with_primary_setting = False
    setUpTestData = classmethod(fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare = fixtures.RecipeBibleRegenerationTests.prepare
    setUp = RecipeRequirementPreparationTests.setUp
    tearDown = RecipeRequirementPreparationTests.tearDown

    def test_reviewed_entity_without_appearance_cannot_create_a_primary_candidate(self):
        with self.assertRaisesMessage(ValidationError, "不能把空生成阶段标为成功"):
            self.prepare(1)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(self.stage.route_events.exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeFirstMaterialUnitTests(TestCase):
    prepare_initial = False
    freeze_input = False
    setUpTestData = classmethod(fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare = fixtures.RecipeBibleRegenerationTests.prepare
    setUp = RecipeRequirementPreparationTests.setUp
    tearDown = RecipeRequirementPreparationTests.tearDown

    def test_first_preparation_freezes_original_stage_units_and_candidates_share_appearance(self):
        self.assertEqual(self.stage.input_snapshot_sha256, "")
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        with patch("studio.services.execution_request_preparation.resolve_execution_input_sources",
                wraps=resolve_execution_input_sources) as resolver:
            first = self.prepare(1)
        self.assertEqual(resolver.call_args.kwargs["unit_key"], first.unit_key)
        self.stage.refresh_from_db()
        saved = deepcopy(self.stage.input_snapshot)
        second = self.prepare(2)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot, saved)
        self.assertEqual(first.unit_key, f"appearance:{self.setting.variant_id}")
        self.assertEqual(second.unit_key, first.unit_key)
        self.assertNotEqual(second.request_key, first.request_key)
        self.assertEqual(len(saved["units"]), 1)
        self.assertEqual(saved["units"][0]["requirement_key"], f"setting:{self.setting.pk}")
        from studio.services.production_runs import _next_bible_regeneration_variants
        self.assertEqual(_next_bible_regeneration_variants(self.stage, entities=self.entities),
            {(self.scope["kind"], self.scope["id"]): [3]})

    def test_old_entity_unit_projection_is_rejected_without_rewriting_snapshot(self):
        first = self.prepare(1)
        self.stage.refresh_from_db()
        original = deepcopy(self.stage.input_snapshot)
        invalid = {**original, "units": [self.scope]}
        from studio.services.production_recipe_material_units import frozen_recipe_material_unit
        # Isolate semantic verification; do not reseal persisted history.
        with patch("studio.services.production_runs._verify_stage_snapshot", return_value=invalid):
            with self.assertRaisesMessage(ValidationError, "不能回填实体单元"):
                frozen_recipe_material_unit(self.stage, self.plan, self.scope)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot, original)
        self.assertEqual(self.prepare(1).pk, first.pk)

    def test_failed_first_preparation_rolls_back_stage_freeze_and_allocation(self):
        with self.assertRaises(ValidationError):
            prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope=self.scope, candidate_number=1, actor=self.actor, appearance_target={
                    "role": "primary", "setting_revision_id": str(uuid4()),
                    "primary_version_id": None, "primary_input": None})
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot_sha256, "")
        self.assertEqual(self.stage.input_snapshot, {})
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(self.stage.route_events.exists())

    def test_reviewed_primary_cannot_be_overridden_with_an_unclassified_target(self):
        with self.assertRaisesMessage(ValidationError, "确切主图设定目标"):
            prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope=self.scope, candidate_number=1, actor=self.actor, appearance_target={
                    "role": "primary", "setting_revision_id": None,
                    "primary_version_id": None, "primary_input": None})
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot_sha256, "")
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(self.stage.route_events.exists())
