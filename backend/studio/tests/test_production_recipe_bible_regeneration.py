from studio.tests.field_definition_fixtures import create_field_definition
"""Recipe Bible regeneration allocates real preparations, not mock tasks.

Provider admission and completed-candidate source review are isolated where
explicitly patched; these tests are not live Smoke or candidate acceptance.
"""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (CharacterBible, ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion,
    ProductionRun, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion, SeriesProject, StageRun)
from studio.services.configuration import resolve_configuration
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.security_boundaries import boundary_int
from studio.tests import test_execution_definitions as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeBibleRegenerationTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)
    seed_rule = {"type": "integer", "minimum": 0, "maximum": 9, "default": 7}

    def image_stage(self, route):
        return {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
            "mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": None}}

    def setUp(self):
        fixtures.ExecutionDefinitionTests.setUp(self)
        spec = deepcopy(self.model.specification)
        spec.pop("conditioning_contract")
        spec["model_family"] = "fixture-family"
        spec["async_media"]["operations"]["image.text_to_image"]["request_schema"]["properties"]["seed"] = {
            key: value for key, value in self.seed_rule.items() if key != "default"}
        config = deepcopy(self.provider.driver_config)
        config["request_field_map"]["seed"] = "seed"
        self.write_contract(self.provider, "driver_config", config)
        self.model.modality = "image"
        self.write_contract(self.model, "specification", spec)
        profile = PromptProfileVersion.objects.create(profile_key="bible-recipe-fixture", version=1, name="Image fixture",
            model_family="fixture-family", operation="image.generate", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="image-fixture", label="Instruction", value_type="long_text")
        create_profile_field_rule(fixed_value="A red cube.", profile_version=profile, field_definition=field, source_selector="fixed.value",
            generation_source_selector="fixed.value", generation_mode="server_locked", channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"operation": "image.text_to_image", "mode": "", "prompt_mode": "profile", "prompt_profile_version": profile})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Bible regeneration", **PROJECT_CONFIG)
        self.character = CharacterBible.objects.create(project=self.project, name="Courier", description="Red coat")
        if getattr(self, "with_primary_setting", True):
            from studio.services.bible_variants import create_variant
            variant = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="character",
                entity_id=self.character.pk, appearance_role="primary", primary_setting_revision_id=None,
                primary_setting_sha256="", value={"name": "Courier primary", "reference_name": "Courier primary",
                    "identity_anchor": "Same courier", "appearance_anchor": "Red coat",
                    "basis_asset_id": None, "basis_asset_sha256": ""})
            self.setting = variant.revisions.get()
        recipe = ProductionRecipeVersion(recipe_key="bible-regeneration-fixture", version=1, name="Image route", status="published")
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": {}, "parameter_policies": [{"key": key, "policy": "locked_value", "value": 512} for key in ("width", "height")]
                + [{"key": "seed", "policy": "default", "value": self.seed_rule["default"]}]}
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "manual", "approval": "required"},
            getattr(self, "image_stage", lambda route: RecipeBibleRegenerationTests.image_stage(self, route))(route),
            {"key": "bible_image_review", "mode": "review", "approval": "required"}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="a"*64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(), max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1, "interaction_mode": "assisted"})
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        self.content_review = StageRun.objects.create(production_run=self.run, stage_key="bible_review", mode="manual",
            status="awaiting_approval", ordinal=1, approval_required=True, idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="bible_image_generation", mode="existing_pipeline",
            status="running", ordinal=2, idempotency_key=str(uuid4()))
        self.review = StageRun.objects.create(production_run=self.run, stage_key="bible_image_review", mode="review",
            status="awaiting_approval", ordinal=3, idempotency_key=str(uuid4()))
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input
        _freeze_stage_config(self.run, self.stage)
        self.scope = {"kind": "character", "id": str(self.character.pk)}
        self.entities = [{"entity_type": "character", "entity_id": str(self.character.pk)}]
        # Exercise the original human content-review writer on isolated data.
        # Missing-demand tests retain this real review but do not create a
        # preparation or freeze an empty generation stage as successful.
        from studio.services.bible_content_review import recipe_bible_review_detail
        from studio.services.production_runs import decide_stage
        self.run.status = "awaiting_approval"
        self.run.current_stage_key = "bible_review"
        self.run.save(update_fields=("status", "current_stage_key"))
        _freeze_stage_config(self.run, self.content_review)
        _freeze_stage_input(self.run, self.content_review)
        detail = recipe_bible_review_detail(self.run)
        # This stage-only route fixture is not publication/delivery acceptance.
        # Only the unrelated full delivery closure is isolated; review content,
        # hashes, human decision and receipt are written by the real services.
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure"):
            self.run = decide_stage(self.run, stage_key="bible_review", decision="approve", user=self.actor,
                comment="Isolated content review, not live acceptance", bible_review={
                    "expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                    "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
                    "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                    "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]})
        self.run.status = "running"
        self.run.current_stage_key = self.stage.stage_key
        self.run.save(update_fields=("status", "current_stage_key"))
        self.initial = [self.prepare(n) for n in (1, 2)] if getattr(self, "prepare_initial", True) else []
        if getattr(self, "with_primary_setting", True) and getattr(self, "freeze_input", True):
            _freeze_stage_input(self.run, self.stage)

    def prepare(self, number, generation_request=None):
        return prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope=self.scope, candidate_number=number, generation_request=generation_request, actor=self.actor)

    def request(self):
        from studio.services.production_runs import _bible_regeneration_request_contract
        request, digest = _bible_regeneration_request_contract(request_id=uuid4(), expected_source_snapshot_sha256="b"*64, entities=self.entities)
        return {**request, "request_sha256": digest, "requested_by_id": str(self.actor.pk),
            "targets": [{**self.entities[0], "variant_number": 3}]}

    def freeze_review_fixture(self):
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        _save_stage_snapshot(self.stage, "output_snapshot", {"fixture_candidates": [1, 2]})
        _freeze_stage_config(self.run, self.review)
        _freeze_stage_input(self.run, self.review)
        _save_stage_snapshot(self.review, "output_snapshot", {"fixture_review": "not_decided"})

    def test_regeneration_unfreezes_review_then_refreezes_new_upstream(self):
        from studio.services.production_runs import (_freeze_stage_input, _materialize_bible_regeneration,
            _save_stage_snapshot, _verify_stage_snapshot)
        from studio.services.production_recipe_stage_snapshots import recipe_non_model_input_contract
        self.freeze_review_fixture()
        old_input_sha = self.review.input_snapshot_sha256
        config_before = deepcopy(self.review.config_snapshot)
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), transaction.atomic():
            _materialize_bible_regeneration(self.run, self.stage, self.review, intent=self.request())
        self.review.refresh_from_db()
        for field in ("input_snapshot", "output_snapshot"):
            self.assertEqual(getattr(self.review, field), {})
            self.assertEqual(getattr(self.review, field + "_sha256"), "")
            self.assertEqual(getattr(self.review, field + "_size_bytes"), 2)
            self.assertEqual(getattr(self.review, field + "_schema_version"), 1)
        self.assertEqual(self.review.config_snapshot, config_before)
        with self.assertRaisesMessage(ValidationError, "前置阶段尚未完成"):
            _freeze_stage_input(self.run, self.review)
        # Isolated completed upstream fixture, not a live media result.
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        _save_stage_snapshot(self.stage, "output_snapshot", {"fixture_candidates": [1, 2, 3]})
        _freeze_stage_input(self.run, self.review)
        self.review.refresh_from_db()
        self.assertNotEqual(self.review.input_snapshot_sha256, old_input_sha)
        self.assertEqual(_verify_stage_snapshot(self.review, "input_snapshot"), recipe_non_model_input_contract(self.review).value)
        frozen_sha = self.review.input_snapshot_sha256
        _freeze_stage_input(self.run, self.review)
        self.assertEqual(self.review.input_snapshot_sha256, frozen_sha)
        self.assertFalse(self.review.approval_decisions.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 3)

    def test_reset_refuses_decided_review_without_changing_snapshot(self):
        from studio.models import ApprovalDecision
        from studio.services.production_runs import _clear_bible_review_for_regeneration
        self.freeze_review_fixture()
        ApprovalDecision.objects.create(stage_run=self.review, revision=1, decision="reject", decided_by=self.actor)
        before = StageRun.objects.values().get(pk=self.review.pk)
        with self.assertRaises(ValidationError):
            _clear_bible_review_for_regeneration(self.review)
        self.assertEqual(StageRun.objects.values().get(pk=self.review.pk), before)
        self.assertEqual(self.review.approval_decisions.count(), 1)

    def test_existing_frozen_review_still_rejects_changed_upstream(self):
        from studio.services.production_runs import _freeze_stage_input, _save_stage_snapshot
        self.freeze_review_fixture()
        before = self.review.input_snapshot_sha256
        _save_stage_snapshot(self.stage, "output_snapshot", {"fixture_candidates": [1, 2, "unapproved_change"]})
        with self.assertRaisesMessage(ValidationError, "阶段输入与本轮计划或前置阶段已不一致"):
            _freeze_stage_input(self.run, self.review)
        self.review.refresh_from_db()
        self.assertEqual(self.review.input_snapshot_sha256, before)

    def test_extra_candidate_requires_original_action_and_same_request_replays(self):
        from studio.services.production_runs import _materialize_bible_regeneration, _next_bible_regeneration_variants
        intent = self.request()
        for value in (None, intent):
            with self.assertRaises(ValidationError): self.prepare(3, value)
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            with transaction.atomic(): _materialize_bible_regeneration(self.run, self.stage, self.review, intent=intent)
        third = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:0:candidate:3")
        self.assertEqual(third.pk, self.prepare(3, intent).pk)
        self.assertEqual(third.contract.resolved_inputs["parameters"]["values"]["width"], 512)
        self.assertEqual(third.contract.resolved_inputs["production_unit"]["scope"], self.scope)
        self.assertEqual(_next_bible_regeneration_variants(self.stage, entities=self.entities), {("character", str(self.character.pk)): [4]})
        self.review.refresh_from_db(); self.assertEqual(self.review.status, "pending")
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(self.stage.route_events.filter(event_type="bible_regeneration_preparing").count(), 1)
        self.character.refresh_from_db(); self.assertIsNone(self.character.anchor_asset_id)

    def test_new_candidate_excludes_prior_seeds_and_replay_never_draws_again(self):
        from studio.services.production_runs import _materialize_bible_regeneration
        before = {str(row.pk): deepcopy(row.contract.resolved_inputs) for row in self.initial}
        self.assertEqual([value["parameters"]["values"]["seed"] for value in before.values()], [7, 7])
        intent = self.request()
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), \
                patch("studio.services.execution_parameter_preparation.randbelow", return_value=7) as draw, transaction.atomic():
            _materialize_bible_regeneration(self.run, self.stage, self.review, intent=intent)
        draw.assert_called_once_with(9)
        third = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:0:candidate:3")
        facts = third.contract.resolved_inputs
        self.assertEqual(facts["parameters"]["values"]["seed"], 8)
        self.assertEqual(facts["new_candidate_seed_basis"]["excluded_values"], {"seed": [7]})
        self.assertEqual({row["preparation_id"] for row in facts["new_candidate_seed_basis"]["prior_preparations"]}, set(before))
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("replay cannot redraw")):
            self.assertEqual(self.prepare(3, intent).pk, third.pk)
        next_intent = self.request()
        next_intent["targets"][0]["variant_number"] = 4
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), \
                patch("studio.services.execution_parameter_preparation.randbelow", return_value=7) as draw, transaction.atomic():
            _materialize_bible_regeneration(self.run, self.stage, self.review, intent=next_intent)
        draw.assert_called_once_with(8)
        fourth = ExecutionPreparation.objects.get(stage_run=self.stage, request_key="route:0:candidate:4")
        self.assertEqual(fourth.contract.resolved_inputs["parameters"]["values"]["seed"], 9)
        for row in self.initial:
            self.assertEqual(row.contract.resolved_inputs, before[str(row.pk)])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_original_media_stage_advances_added_candidate_and_does_not_drop_initial_candidates(self):
        from studio.services.production_runs import _materialize_bible_regeneration, _poll_media_stage
        from studio.tests.test_execution_production_auxiliaries import production_observations
        intent = self.request()
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), transaction.atomic():
            _materialize_bible_regeneration(self.run, self.stage, self.review, intent=intent)
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage, role="primary").count(), 3)
        self.assertIsNone(_poll_media_stage(self.run, self.stage))
        self.run.refresh_from_db(); self.assertEqual(self.run.generation_task_count, 3)
        from studio.services.production_runs import _next_bible_regeneration_variants, ProductionRunConflict
        with self.assertRaises(ProductionRunConflict): _next_bible_regeneration_variants(self.stage, entities=self.entities)

    def test_failed_addition_rolls_back_event_states_and_preparations(self):
        from studio.services.production_runs import _materialize_bible_regeneration
        before_events = list(self.stage.route_events.order_by("id").values())
        intent = self.request()
        intent["targets"].append({"entity_type": "character", "entity_id": str(uuid4()), "variant_number": 3})
        with self.assertRaises(ValidationError), transaction.atomic():
            _materialize_bible_regeneration(self.run, self.stage, self.review, intent=intent)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)
        self.assertEqual(list(self.stage.route_events.order_by("id").values()), before_events)
        self.review.refresh_from_db(); self.assertEqual(self.review.status, "awaiting_approval")
        self.assertFalse(GenerationAttempt.objects.exists())


    def test_formal_action_replays_original_request_and_rejects_changed_idempotency_body(self):
        from studio.services.production_runs import regenerate_bible_image_candidates, ProductionRunConflict
        self.run.status, self.run.current_stage_key = "awaiting_approval", "bible_image_review"
        self.run.save(update_fields=("status", "current_stage_key"))
        self.stage.status = "succeeded"; self.stage.save(update_fields=("status",))
        request = {"request_id": uuid4(), "expected_source_snapshot_sha256": "b"*64, "entities": self.entities, "user": self.actor}
        # Completion/archive/source approval is covered by the candidate and
        # review tests. Isolate only that prerequisite, not preparation writes.
        with patch("studio.services.production_runs._verify_bible_regeneration_source"), \
                patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            result = regenerate_bible_image_candidates(self.run, **request)
            repeated = regenerate_bible_image_candidates(self.run, **request)
            self.assertEqual(result.pk, repeated.pk)
            self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 3)
            self.assertEqual(self.stage.route_events.filter(event_type="bible_regeneration_preparing").count(), 1)
            self.assertEqual(self.stage.route_events.filter(event_type="route_selected").count(), 3)
            with self.assertRaises(ProductionRunConflict) as conflict:
                regenerate_bible_image_candidates(self.run, **{**request, "expected_source_snapshot_sha256": "c"*64})
            self.assertEqual(conflict.exception.code, "bible_regeneration_idempotency_conflict")
        self.assertFalse(GenerationAttempt.objects.exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeBibleSeedExhaustionTests(TestCase):
    seed_rule = {"type": "integer", "minimum": 7, "maximum": 7, "default": 7}
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)
    setUp = RecipeBibleRegenerationTests.setUp
    prepare = RecipeBibleRegenerationTests.prepare
    request = RecipeBibleRegenerationTests.request

    def test_exhausted_seed_rolls_back_before_creating_any_candidate(self):
        from studio.services.production_runs import _materialize_bible_regeneration
        stages_before = list(StageRun.objects.filter(production_run=self.run).order_by("id").values())
        events_before = list(self.stage.route_events.order_by("id").values())
        with patch("studio.services.execution_parameter_preparation.randbelow") as draw:
            with self.assertRaisesMessage(ValidationError, "合法种子已被"), transaction.atomic():
                _materialize_bible_regeneration(self.run, self.stage, self.review, intent=self.request())
            draw.assert_not_called()
        self.assertEqual(list(StageRun.objects.filter(production_run=self.run).order_by("id").values()), stages_before)
        self.assertEqual(list(self.stage.route_events.order_by("id").values()), events_before)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
