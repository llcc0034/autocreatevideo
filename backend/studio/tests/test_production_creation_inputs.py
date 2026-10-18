"""Recipe-owned run-input mapping through actual preparation and compiler.

Root receipt fixtures are isolated storage, not public Recipe acceptance. No
transport, automatic publication of a Recipe, or actual database switch.
"""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRun, ProductionRunPlan, StageRun
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_creation_inputs import creation_input_options, verified_creation_inputs
from studio.tests import test_production_recipe_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionCreationInputTests(TestCase):
    def test_resources_exist_only_in_system_policy_and_never_grant_external_consent(self):
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
        run, created = self.create_root(allow_external_calls=None)
        self.assertTrue(created)
        self.assertFalse(run.allow_external_calls)
        self.assertEqual(run.cost_currency, "")
        self.assertEqual(run.config_snapshot["values"][RESOURCE_POLICY], read_resource_policy().value["values"])
        self.assertNotIn("budget_limit", {field.name for field in ProductionRun._meta.fields})
        self.assertNotIn("default_budget_limit", {field.name for field in self.run.recipe_version._meta.fields})
        again, created = self.create_root(allow_external_calls=None)
        self.assertFalse(created)
        self.assertEqual(again.pk, run.pk)
        with self.assertRaises(TypeError):
            self.create_root(budget_limit="4")
        self.assertFalse(GenerationAttempt.objects.exists())

    setUpTestData = classmethod(fixtures.RecipePreparationTests.setUpTestData.__func__)

    def test_recipe_without_call_default_inherits_effective_configuration(self):
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy

        expected = read_resource_policy().value["values"]["call_count"]
        run, created = self.create_root()
        self.assertTrue(created)
        self.assertEqual(run.config_snapshot["values"][RESOURCE_POLICY]["call_count"], expected)
        self.assertFalse(GenerationAttempt.objects.exists())
    profile_instance = fixtures.RecipePreparationTests.profile_instance
    root = fixtures.RecipePreparationTests.root
    prepare = fixtures.RecipePreparationTests.prepare

    def setUp(self):
        fixtures.RecipePreparationTests.setUp(self)
        # Create a new exact configuration that explicitly opens project scope;
        # never widen the already published instance's stored definition.
        for item in self.definition.value["configuration"]["inputs"]:
            item["semantic_contract"]["allowed_scope_kinds"] = ["project"]
        self.profile_instance()
        self.sources = {row["key"]: {"kind": "context_field", "selector_key": f"run.{row['key']}",
            "scope": {"kind": "project"}} for row in self.definition.value["configuration"]["inputs"]}
        self.run = self.root(source=self.sources)
        self.values = {"idea": "本轮新创意：旅人打开盒子。", "style_hint": "暖色", "episode_count": 1,
            "shots_per_episode": 2, "episode_duration_seconds": 4}
        self.intent = {"schema_version": 4, "plan_source": "recipe", "project_id": str(self.project.pk),
            "recipe_version_id": str(self.run.recipe_version_id), "creation_inputs": deepcopy(self.values)}
        self.receipt()

    def receipt(self):
        self.run.request_sha256 = json_contract(self.intent).sha256
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 4, "request_intent": self.intent,
            "run_plan_sha256": self.plan.sha256}, schema_version=4)
        self.run.save()

    def test_explicit_root_values_reach_native_request_and_preserve_source_evidence(self):
        prepared = self.prepare()
        evaluated = evaluate_preparation_fields(preparation_id=prepared.pk,
            expected_preparation_revision=prepared.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prepared.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        plan = preview.transport.value["operation_plan"]["plan"]
        request = plan["request"]
        for key, value in self.values.items():
            self.assertEqual(request[key] if key in {"idea", "style_hint"} else plan[key], value)
        provenance = prepared.contract.resolved_inputs["inputs"]["input_provenance"]
        self.assertEqual(len(provenance), 5)
        self.assertTrue(all(row["creation_request"]["request_sha256"] == self.run.request_sha256 for row in provenance))
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_unprovided_value_and_corrupt_receipt_cannot_fall_back_to_project_defaults(self):
        self.intent["creation_inputs"]["episode_count"] = None
        self.receipt()
        with self.assertRaisesMessage(ValidationError, "未提供"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.intent["creation_inputs"]["episode_count"] = 1
        self.receipt()
        self.run.input_snapshot["request_intent"]["creation_inputs"]["idea"] = "unsigned"
        self.run.save(update_fields=("input_snapshot",))
        with self.assertRaises(ValidationError):
            self.prepare()

    def test_replay_keeps_original_facts_and_does_not_reread_changed_project(self):
        prepared = self.prepare()
        self.project.title = "Changed project, not the original idea"
        self.project.save(update_fields=("title",))
        self.assertEqual(self.prepare().pk, prepared.pk)
        second = self.prepare(candidate_number=2)
        # A new candidate can have a new preparation-context identity. Its
        # creation input values and exact request provenance must remain equal.
        self.assertEqual(second.contract.resolved_inputs["inputs"]["input_provenance"], prepared.contract.resolved_inputs["inputs"]["input_provenance"])
        self.assertEqual(second.contract.resolved_inputs["inputs"]["operation_inputs"], prepared.contract.resolved_inputs["inputs"]["operation_inputs"])

    def test_other_run_or_missing_run_context_cannot_read_creation_inputs(self):
        bound = {key: {**source, "scope": {"kind": "project", "id": str(self.project.pk)},
            "content_revision_id": str(self.run.pk)} for key, source in self.sources.items()}
        for run_id in (None, str(uuid4())):
            with self.subTest(run_id=run_id), self.assertRaises(ValidationError):
                resolve_execution_input_sources(self.definition, bound, owner=self.actor, subject_facts=[],
                    max_media_bytes=1024, project_id=self.project.pk, production_run_id=run_id)
        self.run.request_sha256 = "0" * 64
        with self.assertRaises(ValidationError):
            verified_creation_inputs(self.run)

    def test_catalog_is_typed_and_unknown_or_boolean_creation_values_are_rejected(self):
        options = creation_input_options()
        self.assertEqual(len(options), 5)
        self.assertTrue(all(row["revision_kind"] == "production_request" for row in options))
        for changes in ({"episode_count": True}, {"idea": 1}, {"episode_count": 0}, {"unknown": "x"}):
            self.intent["creation_inputs"] = {**self.values, **changes}
            self.receipt()
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                verified_creation_inputs(self.run)

    def create_root(self, **changes):
        from studio.services.production_recipe_creation import create_recipe_run_root
        return create_recipe_run_root(**{"user": self.actor, "project": self.project, "recipe": self.run.recipe_version,
            "script_revision": None, "idempotency_key": "new-recipe-root", "allow_external_calls": False,
            "config_overrides": {},
            **self.values, "episode_ids": [], "unattended_confirmed": False, **changes})

    def test_original_root_initializer_freezes_plan_configuration_and_exact_stages(self):
        run, created = self.create_root()
        self.assertTrue(created)
        self.assertEqual(run.status, "queued")
        self.assertEqual(verified_creation_inputs(run)[0], self.values)
        self.assertEqual(list(run.stages.values_list("ordinal", "stage_key", "mode", "status")),
            [(1, "script_generate", "ai", "pending")])
        self.assertEqual(run.config_snapshot["values"]["production.allow_external_calls"], False)
        stage = run.stages.get()
        self.assertFalse(stage.input_snapshot_sha256)
        self.assertNotIn("routes", stage.routing)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input
        _freeze_stage_config(run, stage)
        _freeze_stage_input(run, stage)
        run.status = "running"
        run.save(update_fields=("status",))
        stage.status = "running"
        stage.save(update_fields=("status",))
        from studio.services.production_recipe_preparations import prepare_recipe_project_stage
        prep = prepare_recipe_project_stage(stage_run_id=stage.pk,
            expected_run_plan_sha256=run.execution_plan.definition_sha256, candidate_number=1, actor=self.actor)
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["operation_inputs"]["idea"], self.values["idea"])

    def test_root_replay_keeps_original_rows_and_rejects_different_input_or_missing_stage(self):
        run, _ = self.create_root()
        run.recipe_version.status = "retired"
        run.recipe_version.save(update_fields=("status",))
        repeated, created = self.create_root()
        self.assertFalse(created)
        self.assertEqual(repeated.pk, run.pk)
        with self.assertRaises(ValidationError):
            self.create_root(idea="Another request")
        run.stages.all().delete()
        with self.assertRaises(ValidationError):
            self.create_root()
        self.assertFalse(run.stages.exists())

    def test_root_failure_is_atomic_and_public_create_rejects_incomplete_pipeline(self):
        before = (ProductionRun.objects.count(), ProductionRunPlan.objects.count(), StageRun.objects.count())
        with patch("studio.services.production_recipe_creation.freeze_run_configuration", side_effect=ValidationError("isolated freeze failure")):
            with self.assertRaisesMessage(ValidationError, "freeze failure"):
                self.create_root()
        self.assertEqual(before, (ProductionRun.objects.count(), ProductionRunPlan.objects.count(), StageRun.objects.count()))
        from studio.services.production_runs import create_production_run
        with self.assertRaises(ValidationError) as rejected:
            create_production_run(user=self.actor, project=self.project, plan_source="recipe", script_revision=None,
                recipe=self.run.recipe_version, idempotency_key="public-incomplete", allow_external_calls=False,
                **self.values)
        self.assertIn("缺少统一交付闭包阶段", str(rejected.exception))
        self.assertEqual(before, (ProductionRun.objects.count(), ProductionRunPlan.objects.count(), StageRun.objects.count()))

    def test_scheduler_rejects_old_receipt_or_missing_stage_without_reconstruction(self):
        from studio.services.production_runs import advance_production_run
        run, _ = self.create_root()
        original = deepcopy(run.input_snapshot)
        assign_json_contract(run, "input_snapshot", {"schema_version": 1, "episode_count": 1,
            "project_execution_contract": {}, "structured_shot_duration_contract": {}}, schema_version=1)
        run.save()
        with self.assertRaisesMessage(ValidationError, "创建请求回执"):
            advance_production_run(run)
        assign_json_contract(run, "input_snapshot", original, schema_version=4)
        run.save()
        run.stages.all().delete()
        with self.assertRaisesMessage(ValidationError, "不能通过重放补造"):
            advance_production_run(run)
        self.assertFalse(run.stages.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_root_rejects_ignored_input_and_each_fallback_route_must_map_it(self):
        from studio.services.production_creation_inputs import validate_recipe_creation_mappings
        stage = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0])
        validate_recipe_creation_mappings([stage], self.values)
        stage["routing"]["mode"] = "ordered_fallback"
        fallback = deepcopy(stage["routing"]["routes"][0])
        fallback["input_sources"]["idea"] = {"kind": "literal", "value": "Unrelated configured idea"}
        stage["routing"]["routes"].append(fallback)
        with self.assertRaisesMessage(ValidationError, "未映射：idea"):
            validate_recipe_creation_mappings([stage], self.values)

    def test_imported_script_and_creation_plan_reach_the_original_stage_preparation(self):
        from studio.services.production_runs import advance_production_run
        from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
        fixtures.RecipePreparationTests.structure_instance(self, scopes=("project",), planning=True)
        self.profile_instance()
        _, revision = create_pasted_script_source(project=self.project, user=self.actor,
            content="旅人来到房间。\n旅人放下盒子。")
        revision = confirm_script_revision(revision, user=self.actor)
        values = {**self.values, "idea": "", "style_hint": ""}
        rules = {"script": {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}},
            "plan": {"kind": "structured", "template": {"kind": "object", "fields": {
                key: {"kind": "context_field", "selector_key": f"run.{key}", "scope": {"kind": "project"}}
                for key in ("episode_count", "shots_per_episode", "episode_duration_seconds")}}}}
        self.run = self.root(stage_key="script_structure", source=rules,
            prior=[{"key": "script_input", "mode": "confirmed_revision", "approval": "auto"}],
            initial={"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256})
        run, created = self.create_root(script_revision=revision, **values)
        self.assertTrue(created)
        run = advance_production_run(run)
        self.assertEqual(run.stages.get(stage_key="script_input").status, "succeeded", run.blocked_reason)
        preparations = list(ExecutionPreparation.objects.filter(stage_run__production_run=run).order_by("request_key"))
        self.assertEqual(len(preparations), 2, run.blocked_reason)
        planning = {key: values[key] for key in ("episode_count", "shots_per_episode", "episode_duration_seconds")}
        for prep in preparations:
            self.assertEqual(prep.contract.resolved_inputs["inputs"]["operation_inputs"],
                {"source_text": revision.content, "planning": planning})
            transport = compile_prepared_execution_transport(preparation_id=prep.pk,
                expected_preparation_revision=prep.revision, actor=self.actor).transport.value
            self.assertEqual(transport["operation_plan"]["plan"]["request"]["planning"], planning)
            self.assertEqual(transport["operation_plan"]["plan"]["source_revision"]["id"], str(revision.pk))
        self.assertFalse(GenerationAttempt.objects.exists())
