from studio.tests.field_definition_fixtures import create_field_definition
"""Real isolated RunPlan -> Preparation -> prompt compilation, no dispatch."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (AssetGenerationJob, ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion,
    ProductionRun, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion, SeriesProject, StageRun)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft, publish_execution_version
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_preparations import prepare_recipe_project_stage
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.script_sources import create_pasted_script_source, create_script_revision, confirm_script_revision
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_execution_native_text as native_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class RecipePreparationTests(TestCase):
    setUpTestData = classmethod(native_fixtures.ExecutionNativeTextTests.setUpTestData.__func__)
    structure_instance = native_fixtures.ExecutionNativeTextTests.structure_instance

    def setUp(self):
        native_fixtures.ExecutionNativeTextTests.setUp(self)
        self.manual_version = self.version
        self.project = SeriesProject.objects.create(owner=self.actor, title="Preparation project", **PROJECT_CONFIG)
        # Reading the isolated local keyring is a real admission check, not
        # provider I/O. Do not disable it in integration fixture consumers.
        for target in ("studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("Preparation cannot invoke a provider")))
        self.profile_instance()

    def profile_instance(self, *, language_policy="inherit", project_selector=None):
        key = "recipe-profile-" + str(uuid4())
        model = self.version.deployment.profile_version
        # This isolated model fixture must declare its family before publishing
        # the new Profile-backed instance; no production inference/backfill.
        if "model_family" not in model.specification:
            assign_json_contract(model, "specification", {**model.specification, "model_family": "text-protocol"})
            model.save()
        profile = PromptProfileVersion.objects.create(profile_key=key, version=1, name="Frozen rule",
            model_family="text-protocol", operation=self.version.operation, mode="",
            review_state="ready", contract_schema_version=8)
        field = create_field_definition(key=key, label="Instruction", value_type="long_text")
        create_profile_field_rule(fixed_value="Only use the supplied facts.", profile_version=profile, field_definition=field,
            source_selector="fixed.value", generation_source_selector="fixed.value",
            generation_mode="server_locked", channel="positive_prompt", position=1, required=True,
            language_policy=language_policy)
        if project_selector:
            project_field = create_field_definition(key=key + "-project", label="Project fact", value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=project_field,
                source_selector=project_selector, generation_source_selector=project_selector,
                generation_mode="passthrough", channel="positive_prompt", position=2, required=True,
                language_policy="inherit")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        instance = create_execution_instance(key=key, name="Recipe script", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.version.deployment, "operation": self.version.operation, "mode": "",
                "prompt_mode": "profile", "prompt_profile_version": profile, "workflow_version": None,
                "output_version": self.version.output_version})
        self.version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": deepcopy(self.definition.value["configuration"]["inputs"])})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)

    def root(self, *, source=None, stage_key="script_generate", initial=None, variants=2, prior=None, following=None, interaction_mode="assisted", parameter_policies=None, routing=None, stage_approval="required"):
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": deepcopy(self.fixture["inputs"] if source is None else source),
            "parameter_policies": deepcopy(parameter_policies) if parameter_policies is not None else [
                {"key": "seed", "policy": "locked_value", "value": 42}]}
        declaration = {"key": stage_key, "mode": "ai", "approval": stage_approval, "routing": {
            "mode": "fixed", "variant_count": variants, "evaluator": None, "routes": [route]}}
        if routing is not None:
            declaration["routing"] = deepcopy(routing)
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Isolated source fixture", status="published")
        # Storage fixture, not a claim that the staged publication gate passed.
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": interaction_mode,
            "stages": (prior or []) + [declaration] + (following or [])}, schema_version=3)
        recipe.save()
        # Explicit isolated fixture budget; zero remains a real dispatch blocker.
        run = ProductionRun(owner=self.actor, project=self.project, plan_source="recipe", recipe_version=recipe,
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        for name in ("input_snapshot", "config_snapshot"):
            assign_json_contract(run, name, {"schema_version": 1})
        run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=initial, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.stage = StageRun.objects.create(production_run=run, stage_key=stage_key, ordinal=len(prior or []) + 1, mode="ai",
            status="running", idempotency_key=str(uuid4()))
        self.plan = get_run_execution_plan(run)
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(run, authorized=interaction_mode == "unattended")
        return run

    def prepare(self, **changes):
        return prepare_recipe_project_stage(**{"stage_run_id": self.stage.pk, "expected_run_plan_sha256": self.plan.sha256,
            "candidate_number": 1, "actor": self.actor, **changes})

    def test_literal_inputs_and_exact_profile_compile_without_queue_or_stage_success(self):
        self.root()
        prep = self.prepare()
        self.assertEqual(prep.stage_run_id, self.stage.pk)
        self.assertEqual(prep.request_key, "route:0:candidate:1")
        self.assertEqual(prep.unit_key, f"project:{self.project.pk}")
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["operation_plan"]["plan"]["inputs"]["idea"], "旅人放下一个盒子。")
        self.assertIn("Only use the supplied facts.", preview.transport.value["payload"]["messages"][1]["content"])
        self.assertEqual(prep.contract.resolved_inputs["parameters"]["values"]["seed"], 42)
        self.assertIsNone(prep.request_frozen_at)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "running")
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_source_eligibility_uses_recipe_order_and_mode_not_complete_manual_catalog(self):
        run = self.root(following=[{"key": "script_review", "mode": "review", "approval": "required"}])
        self.assertEqual(execution_source_stage_ids(self.stage), ())
        review = StageRun.objects.create(production_run=run, stage_key="script_review", ordinal=2,
            mode="review", status="pending", idempotency_key=str(uuid4()))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        self.assertEqual(execution_source_stage_ids(review), (str(self.stage.pk),))
        review.mode = "manual"
        with self.assertRaisesMessage(ValidationError, "冻结运行计划"):
            execution_source_stage_ids(review)

    def test_frozen_plan_not_current_recipe_and_replay_preserves_first_facts(self):
        run = self.root()
        first = self.prepare()
        recipe = run.recipe_version
        recipe.definition["stages"][0]["routing"]["routes"][0]["input_sources"]["idea"]["value"] = "Replacement not in this RunPlan"
        assign_json_contract(recipe, "definition", recipe.definition, schema_version=3)
        recipe.save()
        second = self.prepare(candidate_number=2)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.contract.resolved_inputs, second.contract.resolved_inputs)
        with patch("studio.services.production_recipe_preparations.resolve_execution_request_facts",
                side_effect=AssertionError("Replay cannot resolve sources or draw seed")):
            run.status = "cancelled"
            run.save(update_fields=("status",))
            self.assertEqual(self.prepare().pk, first.pk)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)

    def test_identity_scope_and_permissions_reject_without_writes(self):
        self.root()
        other = get_user_model().objects.create_user(username="recipe-prep-other")
        for changes in ({"candidate_number": True}, {"candidate_number": 0}, {"candidate_number": 3},
                {"expected_run_plan_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.prepare(**changes)
        with self.assertRaises(PermissionDenied):
            self.prepare(actor=other)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_parameter_source_freezes_once_and_new_candidate_reads_new_project_fact(self):
        # Seed is this isolated native fixture's only variable integer. This is
        # an explicit test mapping, not a recommended/default business policy.
        source = {"kind": "context_scalar", "selector_key": "project.width", "scope": {"kind": "project"}}
        run = self.root(parameter_policies=[{"key": "seed", "policy": "locked_source", "source": source}])
        first = self.prepare()
        facts = first.contract.resolved_inputs["parameters"]
        self.assertEqual(facts["values"]["seed"], self.project.width)
        self.assertEqual(facts["resolved_source_contracts"]["seed"], {
            "schema_version": 1, **source, "scope": {"kind": "project", "id": str(self.project.pk)},
            "observed_updated_at": self.project.updated_at.isoformat(), "value": self.project.width})
        self.assertEqual(run.recipe_version.definition["stages"][0]["routing"]["routes"][0]["parameter_policies"][0]["source"], source)
        self.project.width = 1024
        self.project.save(update_fields=("width", "updated_at"))
        with patch("studio.services.production_recipe_preparations.bind_recipe_parameter_sources",
                side_effect=AssertionError("Replay cannot reread project parameters")):
            replay = self.prepare()
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.contract.resolved_inputs["parameters"], facts)
        second = self.prepare(candidate_number=2)
        self.assertEqual(second.contract.resolved_inputs["parameters"]["values"]["seed"], 1024)
        self.assertNotEqual(second.contract.resolved_inputs["parameters"]["resolved_source_contracts"], facts["resolved_source_contracts"])
        evaluated = evaluate_preparation_fields(preparation_id=first.pk, expected_preparation_revision=first.revision, actor=self.actor)
        transport = compile_prepared_execution_transport(preparation_id=first.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor).transport.value
        self.assertEqual(transport["payload"]["seed"], facts["values"]["seed"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_parameter_source_cannot_override_system_output_length(self):
        from studio.services.production_recipe_contracts import validate_recipe_definition
        run = self.root(parameter_policies=[{"key": "max_tokens", "policy": "locked_source", "source": {
            "kind": "context_scalar", "selector_key": "project.height", "scope": {"kind": "project"}}},
            {"key": "seed", "policy": "locked_value", "value": 42}])
        with self.assertRaises(ValidationError):
            validate_recipe_definition(run.recipe_version.definition)
        with self.assertRaises(ValidationError):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_new_preparation_requires_active_stage(self):
        run = self.root()
        self.prepare()
        for status in ("cancelled", "failed", "succeeded", "queued", "awaiting_approval"):
            run.status = status
            run.save(update_fields=("status",))
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.prepare(candidate_number=2)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_project_language_is_frozen_per_preparation_and_cannot_be_overridden(self):
        self.root()
        first = self.prepare()
        self.assertEqual(first.contract.intent["request"]["requested"]["language_source"],
            {"kind": "project", "project_id": str(self.project.pk)})
        for key in ("source_language", "target_language"):
            self.assertEqual(first.contract.resolved_inputs["profile_sources"][key], self.project.language)
            with self.assertRaises(TypeError):
                self.prepare(**{key: "Chinese"})
        self.project.language = "Chinese"
        self.project.save(update_fields=("language",))
        self.assertEqual(self.prepare().pk, first.pk)
        self.assertEqual(self.prepare().contract.resolved_inputs["profile_sources"]["target_language"], "English")
        second = self.prepare(candidate_number=2)
        self.assertEqual(second.contract.resolved_inputs["profile_sources"]["target_language"], "Chinese")
        self.project.language = ""
        self.project.save(update_fields=("language",))
        self.assertEqual(self.prepare().pk, first.pk)

    def test_missing_project_language_rejects_new_preparation_without_default(self):
        self.root()
        for language in ("", "   "):
            self.project.language = language
            self.project.save(update_fields=("language",))
            with self.subTest(language=language), self.assertRaisesMessage(ValidationError, "请先配置项目语言"):
                self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_profile_fixed_language_takes_precedence_over_project_context(self):
        self.profile_instance(language_policy="en")
        self.project.language = "Chinese"
        self.project.save(update_fields=("language",))
        self.root()
        prep = self.prepare()
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertEqual(prep.contract.resolved_inputs["profile_sources"]["target_language"], "Chinese")
        self.assertIn("Only use the supplied facts.", preview.transport.value["payload"]["messages"][1]["content"])

    def test_automatic_recipe_does_not_invent_manual_channels(self):
        self.version = self.manual_version
        self.definition = build_execution_definition(self.version.pk)
        self.root()
        with self.assertRaisesMessage(ValidationError, "第三套"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_context_uses_frozen_confirmed_revision_not_latest_or_run_fk(self):
        self.structure_instance(scopes=("request", "project"))
        self.profile_instance()
        source, revision = create_pasted_script_source(project=self.project, user=self.actor, content="旅人来到房间。\n旅人放下盒子。")
        revision = confirm_script_revision(revision, user=self.actor)
        run = self.root(stage_key="script_structure", source={"script": {"kind": "context_field",
            "selector_key": "script.content", "scope": {"kind": "project"}}},
            initial={"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256})
        prep = self.prepare()
        later = confirm_script_revision(create_script_revision(source=source, user=self.actor, content="Later content"), user=self.actor)
        run.script_revision = later
        run.save(update_fields=("script_revision",))
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["operation_inputs"]["source_text"], revision.content)
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor).transport.value
        self.assertEqual(transport["operation_plan"]["plan"]["source_revision"]["id"], str(revision.pk))
        self.assertEqual(self.prepare().pk, prep.pk)
        with self.assertRaisesMessage(ValidationError, "尚未正式确认"):
            self.prepare(candidate_number=2)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_preparation_rejects_stage_identity_drift_without_new_requests(self):
        run = self.root(following=[{"key": "script_review", "mode": "review", "approval": "required"}])
        StageRun.objects.filter(pk=self.stage.pk).update(ordinal=3)
        with self.assertRaisesMessage(ValidationError, "阶段职责与本轮冻结计划不一致"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())
        StageRun.objects.filter(pk=self.stage.pk).update(ordinal=1)
        extra = StageRun.objects.create(production_run=run, stage_key="script_review", ordinal=2,
            mode="manual", status="succeeded", idempotency_key=str(uuid4()))
        with self.assertRaisesMessage(ValidationError, "来源步骤与冻结运行计划不一致"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())
        extra.mode = "review"
        extra.save(update_fields=["mode"])
        prep = self.prepare()
        self.assertEqual(prep.stage_run_id, self.stage.pk)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_and_future_selection_are_explicit_not_empty_source_fallback(self):
        self.structure_instance(scopes=("request", "project"))
        self.profile_instance()
        self.root(stage_key="script_structure", source={"script": {"kind": "context_field",
            "selector_key": "script.content", "scope": {"kind": "project"}}})
        with self.assertRaisesMessage(ValidationError, "未选择已确认剧本"):
            self.prepare()
        self.root(stage_key="script_structure", source={"script": {"kind": "selected_output", "stage_key": "script_generate",
            "output_key": "generated_script.script.content", "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}},
            prior=[{"key": "script_generate", "mode": "ai", "approval": "required"}])
        with self.assertRaisesMessage(ValidationError, "指定上游尚未完成"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())
