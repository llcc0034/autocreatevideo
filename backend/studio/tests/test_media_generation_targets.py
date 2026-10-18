"""Real original roots share generated target facts; no provider or live DB.

Existing-Asset fulfillment, primary-image mapping and variant orchestration
are deliberately not simulated by these first M4-01 contract tests.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob, ExecutionPreparation, GenerationAttempt, ProductionRun
from studio.services.asset_generation import create_asset_generation_job
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.media_generation_targets import (
    read_media_generation_target, resolve_media_generation_target, verify_media_generation_target,
)
from studio.tests import test_production_recipe_media_preparations as media_fixtures
from studio.tests import test_production_recipe_bible_regeneration as image_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class MediaGenerationTargetTests(TestCase):
    setUpTestData = classmethod(media_fixtures.RecipeMediaPreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(media_fixtures.RecipeMediaPreparationTests.write_contract)
    create_root = media_fixtures.RecipeMediaPreparationTests.create_root
    prepare = media_fixtures.RecipeMediaPreparationTests.prepare

    def setUp(self):
        media_fixtures.RecipeMediaPreparationTests.setUp(self)

    def create_asset(self, **changes):
        return create_asset_generation_job(owner=self.actor, client_idempotency_key="shared-target",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="video", name="Independent target", field_values=[],
            **{"inputs": {}, "parameters": {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3},
                "manual_channels": {}, "subject_facts": [], "source_language": self.project.language,
                "target_language": self.project.language, **changes})

    def test_two_real_roots_share_target_semantics_without_implicit_run(self):
        route = self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0]
        self.create_root({"mode": "fixed", "variant_count": 1, "routes": [route], "evaluator": None})
        recipe = self.prepare()
        count = ProductionRun.objects.count()
        job, created = self.create_asset()
        asset = job.execution_preparations.get()
        self.assertTrue(created)
        self.assertEqual(ProductionRun.objects.count(), count)
        self.assertIsNone(asset.stage_run_id)
        self.assertIsNone(recipe.asset_job_id)
        self.assertNotEqual(asset.unit_key, recipe.unit_key)
        self.assertEqual(read_media_generation_target(asset).value, read_media_generation_target(recipe).value)
        target = read_media_generation_target(asset).value
        self.assertEqual(target["execution_instance_version_id"], str(self.version.pk))
        self.assertEqual(target["definition_sha256"], self.definition.sha256)
        self.assertEqual(target["source_mode"], "generate")
        self.assertEqual(target["candidates"], {"count": 1, "evaluator": None})
        self.assertIsNone(target["setting"])
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertIsNone(job.output_asset_id)

    def test_recipe_keeps_configured_count_and_evaluator_not_fake_adoption(self):
        route = self.plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0]
        self.create_root({"mode": "fixed", "variant_count": 3, "routes": [route], "evaluator": "first_succeeded"})
        target = read_media_generation_target(self.prepare(candidate_number=2)).value
        self.assertEqual(target["candidates"], {"count": 3, "evaluator": "first_succeeded"})
        self.assertFalse(self.stage.artifact_selection_decisions.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_replay_reads_original_target_without_resolving_or_allocating_again(self):
        job, _ = self.create_asset()
        recipe = self.prepare()
        before = read_media_generation_target(recipe).value
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts",
                side_effect=AssertionError("Replay must not resolve a new target")):
            replay, created = self.create_asset()
            self.assertEqual(replay.pk, job.pk)
            self.assertFalse(created)
            self.assertEqual(self.prepare().pk, recipe.pk)
            self.assertEqual(read_media_generation_target(recipe).value, before)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)

    def test_invalid_sources_roll_back_asset_root_and_preparation(self):
        with self.assertRaises(ValidationError):
            self.create_asset(inputs={"unknown": {"kind": "literal", "value": "not declared"}})
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_invalid_candidate_policy_fails_before_source_resolution(self):
        for count, evaluator in [(True, None), (0, None), (-1, None), (1.5, None), (1, "quality_rank"), (1, False)]:
            with self.subTest(count=count, evaluator=evaluator), patch(
                    "studio.services.media_generation_targets.resolve_execution_request_facts",
                    side_effect=AssertionError("Invalid target must fail first")), self.assertRaises(ValidationError):
                resolve_media_generation_target(self.definition, {}, candidate_count=count, candidate_evaluator=evaluator)

    def test_saved_identity_source_digest_and_shape_are_strict(self):
        preparation = self.prepare()
        facts = preparation.contract.resolved_inputs
        target = facts["media_generation_target"]
        for key, value in [("source_mode", "existing"), ("media_type", "image"), ("definition_sha256", "0" * 64),
                ("execution_instance_version_id", "other"), ("inputs_sha256", "0" * 64),
                ("schema_version", True), ("setting", {"setting_revision_id": "invented"}), ("approved", True)]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                verify_media_generation_target(self.definition, {**facts, "media_generation_target": {**target, key: value}})
        for candidate_policy in ({"count": 1}, {"count": 1, "evaluator": None, "selected": True}):
            with self.subTest(policy=candidate_policy), self.assertRaises(ValidationError):
                verify_media_generation_target(self.definition, {**facts,
                    "media_generation_target": {**target, "candidates": candidate_policy}})

    def test_missing_target_is_not_rebuilt_and_read_does_not_mutate_inputs(self):
        preparation = self.prepare()
        before = deepcopy(preparation.contract.resolved_inputs)
        missing = {key: value for key, value in before.items() if key != "media_generation_target"}
        with self.assertRaisesMessage(ValidationError, "不能读取时回填"):
            verify_media_generation_target(self.definition, missing)
        for _ in range(2):
            read_media_generation_target(preparation)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, before)
        altered = deepcopy(before)
        altered["inputs"]["logical_values"]["invented"] = False
        with self.assertRaisesMessage(ValidationError, "解析输入"):
            verify_media_generation_target(self.definition, altered)

    def test_non_media_definition_cannot_resolve_a_media_target(self):
        payload = deepcopy(self.definition.value)
        payload["interface"]["output"]["kind"] = "text"
        definition = json_contract(payload, max_bytes=execution_configuration_max_bytes())
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts",
                side_effect=AssertionError("Text must not enter media resolver")), self.assertRaises(ValidationError):
            resolve_media_generation_target(definition, {}, candidate_count=1, candidate_evaluator=None)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ImageGenerationTargetTests(TestCase):
    setUpTestData = classmethod(image_fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(image_fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = image_fixtures.RecipeBibleRegenerationTests.seed_rule
    prepare = image_fixtures.RecipeBibleRegenerationTests.prepare

    def setUp(self):
        image_fixtures.RecipeBibleRegenerationTests.setUp(self)

    def test_recipe_primary_is_explicit_but_unclassified_asset_does_not_infer_role(self):
        recipe_target = read_media_generation_target(self.initial[0]).value
        before = ProductionRun.objects.count()
        job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="image-target",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="image", image_category="reference", name="Independent image",
            field_values=[], inputs={}, parameters={"width": 512, "height": 512, "seed": 7},
            manual_channels={}, subject_facts=[], source_language=self.project.language, target_language=self.project.language)
        asset_target = read_media_generation_target(job.execution_preparations.get()).value
        self.assertEqual(asset_target["media_type"], "image")
        self.assertIsNone(asset_target["setting"])
        self.assertEqual(recipe_target["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertNotIn("appearance", asset_target)
        self.assertEqual(recipe_target["appearance"], {"role": "primary", "setting": recipe_target["setting"],
            "primary": None, "primary_input": None})
        self.assertEqual({**{key: value for key, value in recipe_target.items() if key != "appearance"},
            "candidates": asset_target["candidates"], "setting": None}, asset_target)
        self.assertEqual(ProductionRun.objects.count(), before)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_original_setting_reference_is_linked_without_copying_anchors(self):
        resolved = self.initial[0].contract.resolved_inputs
        target = read_media_generation_target(self.initial[0]).value
        self.assertEqual(target["setting"], resolved["bible_target"]["primary_setting"])
        self.assertEqual(target["setting"], resolved["recipe_material_requirement"]["target"]["setting"])
        self.assertEqual(target["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertNotIn("anchors", target["setting"])
        self.assertNotIn("primary_asset", target)
        self.assertFalse(GenerationAttempt.objects.exists())
