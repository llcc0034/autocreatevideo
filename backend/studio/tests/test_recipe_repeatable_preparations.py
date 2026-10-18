"""Frozen repeatable-step preparation using the formal stage catalog.

Worker submission admission and its provider are isolated; preparation, compilation, storage,
worker result writing and candidate readers are exercised, not live execution.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import (ArtifactSelectionDecision, ExecutionPreparation, GenerationAttempt,
    ProductionRecipeVersion, ProductionRun, PromptProfileVersion, SeriesProject, StageRun)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_request_storage import reference_prepared_request, store_prepared_request
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_project_text_stages import is_project_text_stage, project_text_candidate_manifest, project_text_stage_contract
from studio.services.production_recipe_preparations import prepare_recipe_project_stage, recipe_project_stage_context
from studio.services.production_recipe_routing import verify_recipe_preparation_route
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan, verify_run_execution_plan
from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS, production_stage_catalog
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_execution_structured_text as fixtures
from studio.tests import test_structured_text_worker as worker_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class RepeatableRecipePreparationTests(TestCase):
    setUpTestData = classmethod(fixtures.StructuredTextExecutionTests.setUpTestData.__func__)
    node = fixtures.StructuredTextExecutionTests.node
    create = fixtures.StructuredTextExecutionTests.create
    publish = fixtures.StructuredTextExecutionTests.publish
    output = fixtures.StructuredTextExecutionTests.output
    process = worker_fixtures.StructuredTextWorkerTests.process

    def setUp(self):
        fixtures.StructuredTextExecutionTests.setUp(self)
        self.enterContext(patch("studio.services.comfyui.ComfyUIClient", side_effect=AssertionError("no media calls")))
        self.catalog = production_stage_catalog()

        profile = PromptProfileVersion.objects.create(profile_key="repeatable-structured", version=1,
            name="Isolated structured instruction", model_family="text-protocol", operation="text.structured",
            mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="repeatable-instruction", label="Instruction", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, fixed_value="Return the supplied count.",
            source_selector="fixed.value", generation_source_selector="fixed.value", generation_mode="server_locked",
            channel="positive_prompt", position=1, required=True, language_policy="inherit")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        output = self.output([self.node("root", "integer")])
        instance = create_execution_instance(key="repeatable-structured", name="Isolated text", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "text.structured", "mode": "", "prompt_mode": "profile",
                "workflow_version": None, "prompt_profile_version": profile, "output_version": output})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": {}, "parameter_policies": []}
        self.declarations = [{"key": key, "kind": "structured_text", "name": "同名步骤", "mode": "ai",
            "approval": "auto", "routing": {"mode": "fixed", "variant_count": getattr(self, "candidate_count", 1),
                "evaluator": getattr(self, "evaluator", None),
                "routes": [deepcopy(route)]}} for key in ("step-one", "step-two")]
        self.recipe = ProductionRecipeVersion(recipe_key="repeatable-storage-fixture", version=1,
            name="Isolated plan, not published through business API", status="published")
        assign_json_contract(self.recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted",
            "stages": deepcopy(self.declarations)}, schema_version=3)
        self.recipe.save()
        self.project = SeriesProject.objects.create(owner=self.actor, title="Isolated repeatable steps", **PROJECT_CONFIG)
        self.run = ProductionRun(owner=self.actor, project=self.project, plan_source="recipe", recipe_version=self.recipe,
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        for name in ("input_snapshot", "config_snapshot"):
            assign_json_contract(self.run, name, {"schema_version": 1})
        self.run.save()
        self.record = freeze_run_execution_plan(run=self.run, scope={"kind": "project"},
            delivery={"kind": "episode_masters"}, initial_content=None, stage_catalog_key=self.catalog.value["key"],
            expected_stage_catalog_sha256=self.catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        self.steps = [StageRun.objects.create(production_run=self.run, stage_key=row["key"], mode="ai", ordinal=index,
            status="running", idempotency_key=str(uuid4())) for index, row in enumerate(self.declarations, 1)]

    def prepare(self, step, number=1):
        return prepare_recipe_project_stage(stage_run_id=step.pk, expected_run_plan_sha256=self.plan.sha256,
            candidate_number=number, actor=self.actor)

    def queue_fixture(self, preparation):
        # Submission is explicit isolated storage, not a claim of scheduler or
        # automatic-selection completion. Everything after it is the original worker.
        prep = evaluate_preparation_fields(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        detail = prep.contract
        frozen = store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(prep.pk),
            "basis_preparation_revision": prep.revision, "intent_sha256": prep.intent_sha256,
            "field_selections": preview.field_selections.value, "transport": preview.transport.value})
        detail.save()
        prep.status, prep.revision, prep.request_sha256, prep.request_frozen_at = "dispatched", prep.revision + 1, frozen.sha256, timezone.now()
        prep.save()
        attempt = GenerationAttempt(stage_run_id=prep.stage_run_id, preparation=prep, execution_instance_version=self.version,
            model_deployment=self.deployment, role="primary", operation="text.structured", unit_key=prep.unit_key,
            prompt_profile_version=self.version.prompt_profile_version,
            prompt_profile_contract_sha256=self.version.prompt_profile_version.contract_sha256, route_index=0,
            attempt_number=GenerationAttempt.objects.filter(stage_run_id=prep.stage_run_id, unit_key=prep.unit_key).count() + 1,
            status="queued", request_sha256=prep.request_sha256, estimated_cost=0, committed_cost=0)
        reference_prepared_request(attempt, detail)
        attempt.save()

    def test_frozen_steps_prepare_independently_and_replay_without_current_catalog(self):
        with patch("studio.services.production_run_plans.production_stage_catalog", side_effect=AssertionError("no current catalog")):
            first, second = [self.prepare(step) for step in self.steps]
            self.assertNotEqual(first.pk, second.pk)
            self.assertEqual(first.request_key, second.request_key)
            self.assertNotEqual(first.intent_sha256, second.intent_sha256)
            for step, prep in zip(self.steps, (first, second)):
                self.assertTrue(is_project_text_stage(step))
                self.assertEqual(prep.stage_run_id, step.pk)
                self.assertEqual(prep.contract.intent["request"]["requested"]["stage_key"], step.stage_key)
                self.assertEqual(project_text_stage_contract(step).value["stage_key"], step.stage_key)
        self.recipe.definition["stages"][0]["name"] = "Changed current authoring only"
        assign_json_contract(self.recipe, "definition", self.recipe.definition, schema_version=3)
        self.recipe.save()
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with patch("studio.services.production_recipe_preparations.resolve_execution_request_facts",
                side_effect=AssertionError("Replay must not re-resolve")):
            self.assertEqual(self.prepare(self.steps[0]).pk, first.pk)
            self.assertEqual(self.prepare(self.steps[1]).pk, second.pk)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(get_run_execution_plan(self.run).value["recipe"]["definition"]["stages"], self.declarations)

    def test_frozen_order_and_exact_step_ownership_control_source_and_route_reads(self):
        first, second = [self.prepare(step) for step in self.steps]
        self.steps[0].status = "succeeded"
        self.steps[0].save(update_fields=("status",))
        self.assertEqual(execution_source_stage_ids(self.steps[0]), ())
        self.assertEqual(execution_source_stage_ids(self.steps[1]), (str(self.steps[0].pk),))
        plan, declaration = recipe_project_stage_context(self.steps[1])
        self.assertEqual(verify_recipe_preparation_route(second, stage=self.steps[1], plan=plan, declaration=declaration), (0, 1))
        with self.assertRaisesMessage(ValidationError, "冻结路线"):
            verify_recipe_preparation_route(first, stage=self.steps[1], plan=plan, declaration=declaration)
        for changes in ({"ordinal": 1}, {"mode": "manual"}, {"stage_key": "not-declared"}):
            step = deepcopy(self.steps[1])
            for key, value in changes.items():
                setattr(step, key, value)
            with self.subTest(changes=changes), self.assertRaisesMessage(ValidationError, "冻结计划"):
                recipe_project_stage_context(step)

    def test_worker_candidates_remain_in_exact_step_manifest_not_same_operation(self):
        preparations = [self.prepare(step) for step in self.steps]
        for count, (step, prep) in enumerate(zip(self.steps, preparations)):
            self.queue_fixture(prep)
            result = self.process(str(count))
            self.assertEqual(result.status, "succeeded", result.error_message)
            self.assertEqual(result.stage_run_id, step.pk)
            with CaptureQueriesContext(connection) as queries:
                manifest = project_text_candidate_manifest(step, [prep.pk])
            self.assertEqual(manifest["stage_key"], step.stage_key)
            self.assertEqual(manifest["candidate_count"], 1)
            self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        for step, other in zip(self.steps, reversed(preparations)):
            with self.assertRaises(ValidationError):
                project_text_candidate_manifest(step, [other.pk])
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_repeatable_catalog_metadata_is_verified_not_silently_coerced(self):
        for marker in (False, 1, "true", None):
            value = deepcopy(self.plan.value)
            catalog = deepcopy(value["stage_catalog"]["value"])
            catalog["stages"][-1]["repeatable"] = marker
            contract = json_contract(catalog)
            value["stage_catalog"] = {"value": catalog, "schema_version": contract.schema_version,
                "size_bytes": contract.size_bytes, "sha256": contract.sha256}
            record = deepcopy(self.record)
            record.stage_catalog_sha256 = contract.sha256
            assign_json_contract(record, "definition", value)
            with self.subTest(marker=marker), self.assertRaisesMessage(ValidationError, "可重复职责"):
                verify_run_execution_plan(record, self.run)

    def test_unknown_or_wrong_execution_duty_cannot_prepare_or_dispatch(self):
        with patch.dict(STAGE_EXECUTION_OPERATIONS, {"structured_text": "script.generate"}):
            with self.assertRaises(ValidationError):
                self.prepare(self.steps[0])
        with patch.dict(STAGE_EXECUTION_OPERATIONS, {}, clear=True):
            self.assertFalse(is_project_text_stage(self.steps[0]))
            with self.assertRaises(ValidationError):
                self.prepare(self.steps[0])
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
