"""M3 receipts -> original M4 stage inputs; no model, adoption or delivery.

The source/review writers are real; later media route IDs are explicit isolated
storage fixtures and must never be resolved or dispatched in these tests.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import BibleVariant, CharacterBible, Episode, ExecutionPreparation, GenerationAttempt, Shot, ShotCharacterRelation
from studio.services.bible_material_requirements import read_run_bible_material_requirements
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_recipe_media_stages import recipe_media_input_contract, advance_recipe_media_stage
from studio.services.production_runs import advance_production_run, _freeze_stage_input, StageBlocked
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
from studio.tests import test_bible_policy_continuation as fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source, entities_fixture


class RecipeMaterialRequirementTests(TestCase):
    setUp = fixtures.BiblePolicyContinuationTests.setUp
    root = fixtures.BiblePolicyContinuationTests.root
    recipe = fixtures.BiblePolicyContinuationTests.recipe
    build = fixtures.BiblePolicyContinuationTests.build

    def approved(self, *, entities=None, episode=None, with_variant_routing=True):
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("No model or external service in demand tests")))
        if entities is not None:
            self.revision = confirmed_structured_bible_source(self, entities=entities)
        if episode is not None:
            self.request["scope"] = {"kind": "episodes", "episode_ids": [str(episode.pk)]}
        route = {"execution_instance_version_id": str(uuid4()), "expected_definition_sha256": "a" * 64,
            "input_sources": {}, "parameter_policies": []}
        stages = []
        for key, mode in (("bible_image_generation", "existing_pipeline"), ("bible_image_review", "review"),
                ("storyboard_review", "review"), ("media_generation", "existing_pipeline"),
                ("artifact_selection", "review"), ("assembly", "existing_pipeline"), ("delivery", "existing_pipeline")):
            declaration = {"key": key, "mode": mode, "approval": "policy" if mode == "review" else "auto"}
            if mode == "review":
                declaration["decision_policy"] = UNATTENDED_DECISION_POLICY_REGISTRY[key]
            if key == "bible_image_generation":
                declaration["routing"] = {"mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": None}
                if with_variant_routing:
                    declaration["variant_routing"] = {"mode": "fixed", "variant_count": 3,
                        "routes": [deepcopy(route)], "evaluator": None}
            stages.append(declaration)
        self.run = advance_production_run(self.build(following_stages=stages), automatic=True)
        self.assertEqual(self.run.stages.get(stage_key="bible_review").status, "succeeded", self.run.blocked_reason)
        self.assertEqual(self.run.blocked_code, "isolated_next_stage")
        self.stage = self.run.stages.get(stage_key="bible_image_generation")
        return self.run

    def assert_no_call(self):
        self.external.assert_not_called()
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_stage_freezes_exact_reviewed_targets_and_dependencies_and_replays(self):
        from studio.models import Asset, AssetReferenceVersion
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.approved()
        receipt = self.run.stages.get(stage_key="bible_review").output_snapshot["policy_evidence"]
        first = recipe_media_input_contract(self.stage)
        self.assertEqual(first.value["material_requirements"]["targets"], receipt["material_requirements"]["targets"])
        self.assertEqual(len(first.value["material_requirements"]["targets"]), 4)
        self.assertEqual(len(first.value["units"]), 4)
        self.assertEqual(len({unit["unit_key"] for unit in first.value["units"]}), 4)
        for unit, target in zip(first.value["units"], first.value["material_requirements"]["targets"]):
            self.assertEqual(unit["unit_key"], f'appearance:{target["setting"]["appearance_id"]}')
            self.assertEqual(unit["requirement_key"], target["requirement_key"])
            self.assertEqual(unit["scope"], target["scope"])
        self.assertEqual(first.value["run_plan_sha256"], first.value["material_requirements"]["run_plan_sha256"])
        _freeze_stage_input(self.run, self.stage)
        _freeze_stage_input(self.run, self.stage)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot_sha256, first.sha256)
        self.assertEqual(recipe_media_input_contract(self.stage), first)
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        public = next(row for row in response.data["stages"] if row["stage_key"] == self.stage.stage_key)
        self.assertEqual(public["input_snapshot"], first.value)
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assert_no_call()

    def test_entity_without_appearance_is_not_a_primary_target(self):
        entities = entities_fixture()
        entities.append({"key": "no_look", "kind": "prop", "name": "No visual declaration"})
        self.approved(entities=entities)
        value = recipe_media_input_contract(self.stage).value
        self.assertEqual(len(value["units"]), 4)
        self.assertEqual(len(value["material_requirements"]["targets"]), 4)
        self.assert_no_call()

    def test_empty_demand_does_not_fall_back_to_existing_entities_or_success(self):
        self.approved(entities=[{"key": "no_look", "kind": "character", "name": "No image requested"}])
        with self.assertRaisesMessage(ValidationError, "不能把空生成阶段标为成功"):
            _freeze_stage_input(self.run, self.stage)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "pending")
        self.assertEqual(self.stage.input_snapshot_sha256, "")
        self.assert_no_call()

    def test_bound_counts_appearances_times_candidates_not_entity_count(self):
        self.approved()
        # Two primary appearances x 2 plus two variant appearances x 3.
        with patch("studio.services.production_runs._effective_generation_task_limit", return_value=9):
            with self.assertRaisesMessage(ValidationError, "素材需求乘以候选数"):
                recipe_media_input_contract(self.stage)
        with patch("studio.services.production_runs._effective_generation_task_limit", return_value=10):
            self.assertEqual(len(recipe_media_input_contract(self.stage).value["material_requirements"]["targets"]), 4)
        self.assert_no_call()

    def test_variant_demand_requires_its_own_routing_without_primary_inheritance(self):
        self.approved(with_variant_routing=False)
        with self.assertRaises(ValidationError) as blocked:
            _freeze_stage_input(self.run, self.stage)
        self.assertIn("variant_routing", blocked.exception.message_dict)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot_sha256, "")
        self.assert_no_call()

    def test_primary_only_demand_needs_no_variant_config(self):
        entities = entities_fixture()
        for entity in entities:
            entity["variants"] = []
        self.approved(entities=entities, with_variant_routing=False)
        with patch("studio.services.production_runs._effective_generation_task_limit", return_value=4):
            self.assertEqual(len(recipe_media_input_contract(self.stage).value["units"]), 2)
        self.assert_no_call()

    def test_missing_variant_contract_blocks_before_any_primary_dispatch_or_preparation(self):
        self.approved()
        _freeze_stage_input(self.run, self.stage)
        before = deepcopy(self.stage.input_snapshot)
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("Do not partially prepare primaries")), patch(
                "studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=AssertionError("Do not dispatch")):
            with self.assertRaisesMessage(ValidationError, "缺少冻结合同") as error:
                advance_recipe_media_stage(self.run, self.stage)
            self.assertEqual(error.exception.code, "execution_definition_conflict")
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot, before)
        self.assert_no_call()

    def test_changed_reviewed_setting_cannot_replace_frozen_requirements(self):
        self.approved()
        _freeze_stage_input(self.run, self.stage)
        before = deepcopy(self.stage.input_snapshot)
        primary = BibleVariant.objects.get(character__isnull=False, appearance_role="primary").revisions.get()
        revise_variant(actor=self.actor, variant=primary.variant, expected_revision=1,
            value={**setting_payload(primary), "appearance_anchor": "Changed after the original review"})
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            recipe_media_input_contract(self.stage)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.input_snapshot, before)
        self.assert_no_call()

    def test_missing_review_does_not_enumerate_project_entities(self):
        self.approved()
        self.run.stages.filter(stage_key="bible_review").update(status="pending")
        with self.assertRaisesMessage(ValidationError, "唯一前置审核"):
            recipe_media_input_contract(self.stage)
        self.assert_no_call()

    def test_run_plan_episode_intersection_keeps_only_declared_scoped_targets(self):
        episode = Episode.objects.create(project=self.project, number=1, title="Chosen episode")
        self.approved(episode=episode)
        review = self.run.stages.get(stage_key="bible_review")
        self.assertEqual(read_run_bible_material_requirements(run=self.run, eligible_stage_ids=[review.pk]).value["targets"], [])
        character = CharacterBible.objects.get(project=self.project)
        shot = Shot.objects.create(episode=episode, number=1, title="Assigned character",
            duration_ms=1000, media_type="image", include_audio=False)
        ShotCharacterRelation.objects.create(shot=shot, character=character, role="visible", position=0)
        value = recipe_media_input_contract(self.stage).value
        self.assertEqual(len(value["units"]), 3)
        self.assertTrue(all(unit["scope"] == {"kind": "character", "id": str(character.pk)} for unit in value["units"]))
        self.assertEqual(len(value["material_requirements"]["targets"]), 3)
        self.assert_no_call()
