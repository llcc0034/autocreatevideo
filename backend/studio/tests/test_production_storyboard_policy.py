"""Current per-shot policy selection and real review writes; no model calls."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion, ProductionRun, StageRun
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import StageBlocked, _execute_unattended_policy_review, _formal_snapshot, _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot, _sha256
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY, production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests import test_production_recipe_media_preparations as fixtures
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeStoryboardPolicyTests(StructureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        fixtures.RecipeMediaPreparationTests.setUpTestData.__func__(cls)

    write_contract = staticmethod(fixtures.RecipeMediaPreparationTests.write_contract)
    create_root = fixtures.RecipeMediaPreparationTests.create_root

    def setUp(self):
        fixtures.RecipeMediaPreparationTests.setUp(self)
        route_stage = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0])
        self.create_policy_run(route_stage)

    def create_policy_run(self, route_stage):
        route_stage["approval"] = "auto"
        route_stage["routing"]["variant_count"] = 1
        self.policy = UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"]
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Policy fixture", status="published")
        # This is a review integration fixture, not complete recipe admission.
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "unattended", "stages": [
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "storyboard_review", "mode": "review", "approval": "policy", "decision_policy": self.policy},
            route_stage]}, schema_version=3)
        recipe.save()
        previous = self.run
        self.run = ProductionRun(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", current_stage_key="storyboard_review", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        assign_json_contract(self.run, "config_snapshot", previous.config_snapshot, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=True)
        self.source = StageRun.objects.create(production_run=self.run, stage_key="storyboard", mode="deterministic",
            status="succeeded", ordinal=1, idempotency_key=str(uuid4()))
        policy_sha = _sha256({"schema_version": 1, "interaction_mode": "unattended", "stage_key": "storyboard_review",
            "decision_policy": self.policy, "recipe_definition_sha256": recipe.definition_sha256})
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", mode="review",
            status="running", ordinal=2, decision_policy=self.policy, decision_policy_sha256=policy_sha, idempotency_key=str(uuid4()))
        StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline",
            status="pending", ordinal=3, idempotency_key=str(uuid4()))
        _save_stage_snapshot(self.source, "output_snapshot", _formal_snapshot(self.run, self.source, "isolated-storyboard"))
        _freeze_stage_config(self.run, self.review)
        _freeze_stage_input(self.run, self.review)

    def configure_structured_route(self):
        from studio.models import PromptProfileVersion
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import clone_execution_draft, publish_execution_version, update_execution_draft
        from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
        from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule

        structure = self.publish(self.create([
            self.node("root", "object"), self.node("action", "text", "root", property_name="action", required=True),
        ]))
        profile = PromptProfileVersion.objects.create(profile_key=str(uuid4()), version=1, name="Structured review fixture",
            model_family="fixture-family", operation="video.generate", mode="T2VA", review_state="ready",
            contract_schema_version=8, input_structure_version=structure)
        for position, (key, path) in enumerate((("request.structured_input", ["action"]), ("request.theme", None))):
            field = create_field_definition(key=f"review-structure-{position}", label="Instruction", value_type="text")
            create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=key,
                source_path=path, generation_source_selector=key, channel="positive_prompt", position=position, required=True)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"prompt_profile_version": profile})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        route_stage = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0])
        self.sources = {"request.structured_input": {"kind": "literal", "value": {"action": "Turn."}},
            "request.theme": {"kind": "literal", "value": "Natural light."}}
        route_stage["routing"]["routes"][0].update(execution_instance_version_id=str(version.pk),
            expected_definition_sha256=self.definition.sha256, input_sources=self.sources)
        self.create_policy_run(route_stage)

    def test_reference_review_projects_structures_without_changing_full_contract(self):
        from studio.services.execution_definitions import verify_current_execution_definition
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        self.configure_structured_route()
        original = deepcopy(self.definition.value)
        self.assertEqual(set(original["interface"]["input_structures"]), {"request.structured_input"})
        self.assertTrue(_execute_unattended_policy_review(self.run, self.review).startswith("approval:"))
        approval = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual(approval.actor, "recipe_policy")
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())
        stored = verify_current_execution_definition(self.version.contract)
        self.assertEqual(stored.value, original)
        self.assertEqual(stored.sha256, self.definition.sha256)
        # Reference-only approval does not weaken the later full input boundary.
        for sources in ({}, {**self.sources, "request.structured_input": {"kind": "literal", "value": {}}},
                {**self.sources, "request.theme": {"kind": "literal", "value": 3}}):
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                resolve_execution_input_sources(stored, sources, owner=self.actor, subject_facts=[], max_media_bytes=65536)
        self.assertEqual(resolve_execution_input_sources(stored, self.sources, owner=self.actor,
            subject_facts=[], max_media_bytes=65536).value["logical_values"]["request.structured_input"], {"action": "Turn."})

    def test_invalid_original_structure_pair_is_not_hidden_by_reference_projection(self):
        from studio.services.json_contracts import json_contract
        self.configure_structured_route()
        contract = self.version.contract
        payload = deepcopy(contract.definition)
        payload["interface"]["input_structures"].pop("request.structured_input")
        damaged = json_contract(payload)
        contract.definition, contract.schema_version, contract.size_bytes, contract.sha256 = (
            damaged.value, damaged.schema_version, damaged.size_bytes, damaged.sha256)
        contract.save()
        with self.assertRaises(StageBlocked) as error:
            _execute_unattended_policy_review(self.run, self.review)
        self.assertEqual(error.exception.code, "unattended_storyboard_reference_invalid")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_contract_without_references_records_real_policy_approval_without_calls(self):
        result = _execute_unattended_policy_review(self.run, self.review)
        approval = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual(result, f"approval:{approval.pk}")
        self.assertEqual(approval.actor, "recipe_policy")
        self.assertIsNone(approval.decided_by_id)
        self.assertEqual(approval.decision_policy, self.policy)
        self.assertEqual(self.review.output_snapshot["policy_evidence"]["shot_count"], 1)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ApprovalDecision.objects.create(stage_run=self.review, revision=2, decision="approve",
                actor="human", decided_by=None)

    def test_source_change_cannot_be_approved_or_silently_refrozen(self):
        before = self.source.output_snapshot_sha256
        self.shot.title = "Changed after source freeze"
        self.shot.save(update_fields=("title",))
        with self.assertRaises(StageBlocked) as error:
            _execute_unattended_policy_review(self.run, self.review)
        self.assertEqual(error.exception.code, "unattended_storyboard_reference_invalid")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.output_snapshot_sha256, before)

    def test_no_user_authority_or_retired_target_cannot_approve_empty_references(self):
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=False)
        with self.assertRaises(StageBlocked) as error:
            _execute_unattended_policy_review(self.run, self.review)
        self.assertEqual(error.exception.code, "unattended_policy_context_invalid")
        freeze_recipe_request_fixture(self.run, authorized=True)
        type(self.version).objects.filter(pk=self.version.pk).update(status="retired")
        with self.assertRaises(StageBlocked) as error:
            _execute_unattended_policy_review(self.run, self.review)
        self.assertEqual(error.exception.code, "unattended_storyboard_reference_invalid")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
