"""Formal recipe authoring and sequential handler integration, no live I/O.

The scheduler tests retain the storage fixture's Run/Stage identity and patch
only root-creation verification and submission admission. They do not claim a
complete delivery run, native business writers or G04 downstream consumption.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, ExecutionPreparation, GenerationAttempt
from studio.services.production_recipe_contracts import validate_recipe_stage_policies
from studio.services.production_runs import advance_production_run
from studio.tests import test_structured_text_selection as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StructuredTextRecipeIntegrationTests(TestCase):
    setUpTestData = fixtures.StructuredTextSelectionTests.__dict__["setUpTestData"]
    setUp = fixtures.StructuredTextSelectionTests.setUp
    node = fixtures.StructuredTextSelectionTests.node
    create = fixtures.StructuredTextSelectionTests.create
    publish = fixtures.StructuredTextSelectionTests.publish
    output = fixtures.StructuredTextSelectionTests.output
    process = fixtures.StructuredTextSelectionTests.process
    prepare = fixtures.StructuredTextSelectionTests.prepare
    queue_fixture = fixtures.StructuredTextSelectionTests.queue_fixture
    candidate = fixtures.StructuredTextSelectionTests.candidate
    candidate_count = 1
    evaluator = "first_succeeded"

    def test_formal_api_saves_multiple_positions_same_name_and_clones_stable_keys(self):
        client = APIClient()
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client.force_authenticate(self.actor)
        options = client.get("/api/v1/production-recipes/options/")
        self.assertEqual(options.status_code, 200)
        meta = next(row for row in options.data["stage_options"] if row["value"] == "structured_text")
        self.assertTrue(meta["repeatable"])
        self.assertEqual(meta["modes"], ["ai"])
        self.assertEqual([row["value"] for row in meta["candidate_evaluator_options"]], [None, "first_succeeded"])
        original = [{"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"}]
        for position in range(3):
            stages = deepcopy(original)
            stages.insert(position, deepcopy(self.declarations[0]))
            stages.insert(position + 1, deepcopy(self.declarations[1]))
            definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": stages}
            response = client.post("/api/v1/production-recipes/", {"recipe_key": "generic-" + uuid4().hex,
                "name": "Isolated generic draft", "definition": definition}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            detail = f"/api/v1/production-recipes/{response.data['id']}/"
            self.assertEqual(client.get(detail).data["definition"], definition)
            stages[position]["name"] = "显示名称已修改"
            changed = client.patch(detail, {"definition": definition}, format="json")
            self.assertEqual(changed.status_code, 200, changed.data)
            self.assertEqual(changed.data["definition"]["stages"][position]["key"], "step-one")
            clone = client.post(detail + "clone-successor/", {
                "expected_definition_sha256": changed.data["definition_sha256"]}, format="json")
            self.assertEqual(clone.status_code, 400, "Drafts are edited, not cloned as published successors")
            # Incomplete native delivery still cannot masquerade as publishable.
            self.assertEqual(client.post(detail + "publish/", {}, format="json").status_code, 400)
        # This fixture is explicitly a stored published source, not a claim of
        # full native-chain publication. The public clone keeps its step IDs.
        clone = client.post(f"/api/v1/production-recipes/{self.recipe.pk}/clone-successor/", {
            "expected_definition_sha256": self.recipe.definition_sha256}, format="json")
        self.assertEqual(clone.status_code, 201, clone.data)
        self.assertEqual(clone.data["definition"]["stages"], self.declarations)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_generic_candidate_selection_is_the_only_stage_progression_choice(self):
        stage = deepcopy(self.declarations[0])
        validate_recipe_stage_policies(stage, interaction_mode="unattended")
        for approval in ("required", "on_warning", "policy"):
            with self.subTest(approval=approval), self.assertRaises(ValidationError):
                validate_recipe_stage_policies({**stage, "approval": approval}, interaction_mode="assisted")
        stage["routing"]["evaluator"] = None
        validate_recipe_stage_policies(stage, interaction_mode="assisted")
        with self.assertRaisesMessage(ValidationError, "自动选择候选"):
            validate_recipe_stage_policies(stage, interaction_mode="unattended")

    def test_readiness_and_non_model_snapshot_use_duty_not_custom_step_key(self):
        from studio.services.execution_catalog_readiness import execution_requirements
        from studio.services.production_recipe_stage_snapshots import recipe_non_model_config_contract
        requirements = execution_requirements(self.declarations)
        self.assertEqual(requirements.primary_version_ids, {str(self.version.pk)})
        self.assertEqual(len(requirements.route_groups), 2)
        self.assertTrue(requirements.needs_text_worker)
        with self.assertRaisesMessage(ValidationError, "模型调用阶段"):
            recipe_non_model_config_contract(self.steps[0])

    def test_original_root_initializer_freezes_repeatable_steps_before_and_after_content(self):
        from studio.services.json_contracts import assign_json_contract
        from studio.services.production_runs import create_production_run
        from studio.services.production_recipe_creation import create_recipe_run_root
        from studio.services.production_run_plans import get_run_execution_plan
        from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="隔离测试原稿")
        revision = confirm_script_revision(revision, user=self.actor)
        declarations = [self.declarations[0], {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"}, self.declarations[1]]
        assign_json_contract(self.recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": declarations}, schema_version=3)
        self.recipe.save()
        # The incomplete fixture cannot pass public delivery admission. Exercise
        # the original atomic initializer without mocking its frozen facts.
        with self.assertRaisesMessage(ValidationError, "交付闭包"):
            create_production_run(user=self.actor, project=self.project, plan_source="recipe", recipe=self.recipe,
                script_revision=revision, idempotency_key="generic-root-fixture", allow_external_calls=False)
        run, created = create_recipe_run_root(user=self.actor, project=self.project, recipe=self.recipe,
            script_revision=revision, idempotency_key="generic-root-fixture", allow_external_calls=False,
            config_overrides={}, idea="", style_hint="", episode_count=None, shots_per_episode=None,
            episode_duration_seconds=None, episode_ids=[], unattended_confirmed=False)
        self.assertTrue(created)
        self.assertEqual(get_run_execution_plan(run).value["recipe"]["definition"]["stages"], declarations)
        self.assertEqual(list(run.stages.order_by("ordinal").values_list("stage_key", flat=True)), [row["key"] for row in declarations])
        replay, created = create_production_run(user=self.actor, project=self.project, plan_source="recipe", recipe=self.recipe,
            script_revision=revision, idempotency_key="generic-root-fixture", allow_external_calls=False)
        self.assertFalse(created)
        self.assertEqual(replay.pk, run.pk)
        with patch("studio.services.production_project_text_stages.advance_execution_preparation",
                side_effect=lambda **kwargs: ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])):
            advanced = advance_production_run(run)
        self.assertEqual(advanced.status, "running", advanced.blocked_reason)
        self.assertEqual(advanced.current_stage_key, "step-one")
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def tick(self):
        with patch("studio.services.production_runs._verify_run_snapshots"), patch(
                "studio.services.production_project_text_stages.advance_execution_preparation",
                side_effect=lambda **kwargs: ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])):
            return advance_production_run(self.run)

    def test_scheduler_runs_exact_step_order_freezes_output_and_never_replays_success(self):
        self.run.stages.update(status="pending")
        result = self.tick()
        self.assertEqual(result.current_stage_key, "step-one")
        self.assertEqual(list(ExecutionPreparation.objects.values_list("stage_run_id", flat=True)), [self.steps[0].pk])
        _, first = self.candidate(1)
        result = self.tick()
        self.assertEqual(result.current_stage_key, "step-two", result.blocked_reason)
        self.steps[0].refresh_from_db()
        self.assertEqual(self.steps[0].status, "succeeded")
        self.assertEqual(self.steps[0].output_snapshot["review_payload"]["selection"]["artifact_id"], str(first.pk))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        before = self.steps[0].output_snapshot_sha256
        with patch("studio.services.production_run_plans.production_stage_catalog", side_effect=AssertionError("frozen only")):
            self.tick()
        self.steps[0].refresh_from_db()
        self.assertEqual(self.steps[0].output_snapshot_sha256, before)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        _, second = self.candidate(1, step=self.steps[1])
        result = self.tick()
        self.assertEqual(self.run.stages.filter(status="succeeded").count(), 2, result.blocked_reason)
        self.assertEqual(set(ArtifactSelectionDecision.objects.values_list("artifact_id", flat=True)), {first.pk, second.pk})
        self.assertNotEqual(result.status, "succeeded", "Text results are not a completed media delivery")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StructuredTextManualRecipeIntegrationTests(TestCase):
    setUpTestData = StructuredTextRecipeIntegrationTests.__dict__["setUpTestData"]
    setUp = StructuredTextRecipeIntegrationTests.setUp
    node = StructuredTextRecipeIntegrationTests.node
    create = StructuredTextRecipeIntegrationTests.create
    publish = StructuredTextRecipeIntegrationTests.publish
    output = StructuredTextRecipeIntegrationTests.output
    process = StructuredTextRecipeIntegrationTests.process
    prepare = StructuredTextRecipeIntegrationTests.prepare
    queue_fixture = StructuredTextRecipeIntegrationTests.queue_fixture
    candidate = StructuredTextRecipeIntegrationTests.candidate
    tick = StructuredTextRecipeIntegrationTests.tick
    candidate_count = 1
    evaluator = None

    def test_scheduler_waits_then_continues_from_explicit_selection(self):
        self.run.stages.update(status="pending")
        self.tick()
        _, artifact = self.candidate(1)
        result = self.tick()
        self.assertEqual(result.status, "awaiting_input", result.blocked_reason)
        self.assertEqual(result.current_stage_key, "step-one")
        self.assertEqual(result.blocked_code, "execution_candidate_selection_required")
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.steps[1]).exists())
        from studio.services.production_runs import select_run_artifact
        select_run_artifact(self.run, artifact, user=self.actor)
        result = self.tick()
        self.assertEqual(result.current_stage_key, "step-two", result.blocked_reason)
