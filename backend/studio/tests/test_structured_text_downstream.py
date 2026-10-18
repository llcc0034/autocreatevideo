"""G04 exact step output -> original selection -> shared paths, no live calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ArtifactSelectionDecision, GeneratedArtifact
from studio.services.execution_input_catalog import execution_text_output_options
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_selected_text import resolve_selected_execution_text
from studio.services.execution_structure_mapping import structure_source_option
from studio.services.execution_text_output_catalog import plan_text_output_options, text_output_schema_key
from studio.services.json_contracts import assign_json_contract, canonical_json_bytes, json_contract
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_preparations import _project_inputs
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_structured_text_selection import choose_structured_text_candidate
from studio.tests import test_structured_text_selection as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StructuredTextDownstreamTests(TestCase):
    setUpTestData = fixtures.StructuredTextSelectionTests.__dict__["setUpTestData"]
    setUp = fixtures.StructuredTextSelectionTests.setUp
    node = fixtures.StructuredTextSelectionTests.node
    create = fixtures.StructuredTextSelectionTests.create
    publish = fixtures.StructuredTextSelectionTests.publish
    output = fixtures.StructuredTextSelectionTests.output
    process = fixtures.StructuredTextSelectionTests.process
    prepare = fixtures.StructuredTextSelectionTests.prepare
    queue_fixture = fixtures.StructuredTextSelectionTests.queue_fixture
    advance = fixtures.StructuredTextSelectionTests.advance
    candidate_count = 1
    evaluator = "first_succeeded"

    def reset_empty_plan(self):
        # Fixture construction only, before any preparation/result exists.
        from studio.models import ExecutionPreparation
        self.assertFalse(ExecutionPreparation.objects.exists())
        assign_json_contract(self.recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted",
            "stages": self.declarations}, schema_version=3)
        self.recipe.save()
        from studio.models import ProductionRun, StageRun
        self.run = ProductionRun(owner=self.actor, project=self.project, plan_source="recipe", recipe_version=self.recipe,
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        for name in ("input_snapshot", "config_snapshot"):
            assign_json_contract(self.run, name, {"schema_version": 1})
        self.run.save()
        self.record = freeze_run_execution_plan(run=self.run,
            scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=self.catalog.value["key"], expected_stage_catalog_sha256=self.catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        self.steps = [StageRun.objects.create(production_run=self.run, stage_key=row["key"], mode="ai", ordinal=index,
            status="running", idempotency_key=str(uuid4())) for index, row in enumerate(self.declarations, 1)]

    def output_layout(self, nodes):
        from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
        from studio.services.execution_definitions import build_execution_definition
        output = self.output(nodes)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.actor, changes={"output_version": output})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        for stage in self.declarations:
            stage["routing"]["routes"][0].update(execution_instance_version_id=str(self.version.pk),
                expected_definition_sha256=self.definition.sha256)
        self.reset_empty_plan()

    def produce(self, value, *, select=True):
        prep = self.prepare(self.steps[0])
        self.queue_fixture(prep)
        result = self.process(canonical_json_bytes(value).decode("utf-8"))
        self.assertEqual(result.status, "succeeded", result.error_message)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        if select:
            self.complete_first_step()
        return artifact

    def complete_first_step(self):
        from studio.models import ExecutionPreparation
        from studio.services.production_runs import advance_production_run
        # Root receipt and submission are isolated for this bounded fixture,
        # but the scheduler must create the real completed-stage envelope.
        with patch("studio.services.production_runs._verify_run_snapshots"), patch(
                "studio.services.production_project_text_stages.advance_execution_preparation",
                side_effect=lambda **kwargs: ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])):
            result = advance_production_run(self.run)
        self.steps[0].refresh_from_db()
        self.assertEqual(self.steps[0].status, "succeeded", result.blocked_reason)
        self.assertEqual(self.steps[0].output_snapshot["review_payload"]["kind"], "execution_candidates")

    def source(self, step="step-one"):
        option = next(row for row in plan_text_output_options(self.plan) if row["stage_key"] == step)
        return {"kind": "selected_output", "stage_key": step, "output_key": option["output_key"],
            "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}

    def consumer(self, kind="json", schema=None):
        spec = {"key": "input", "kind": kind, "required": True, "min_items": 1, "max_items": 1,
            "targets": [{"target_kind": "profile_input", "target_key": "root", "is_primary": False}],
            "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["project"],
                "allowed_subject_kinds": [], "allowed_purposes": [], "paired_with_input_key": None, "allow_repeated_asset": False}}
        return json_contract({"configuration": {"inputs": [spec]}, "resources": {"conditioning_mode": None},
            "interface": {"input_targets": {}, "profile_inputs": {"root": {"value_schema": schema or {}}}}})

    def resolve(self, source, schema=None):
        definition = self.consumer(schema=schema)
        rules = {"input": source}
        validate_recipe_input_sources(definition, rules, prior_stages=["step-one"],
            text_outputs=plan_text_output_options(self.plan))
        bound, eligible = _project_inputs(rules, self.plan, self.steps[1])
        result = resolve_execution_input_sources(definition, bound, owner=self.actor, subject_facts=[],
            max_media_bytes=65536, project_id=self.project.pk, production_run_id=str(self.run.pk),
            eligible_stage_ids=eligible, production_scope={"kind": "project", "id": str(self.project.pk)},
            unit_key=f"project:{self.project.pk}")
        return bound, result.value

    def test_catalog_distinguishes_same_name_steps_and_freezes_schema_per_identity(self):
        with CaptureQueriesContext(connection) as queries:
            options = [row for row in plan_text_output_options(self.plan) if row.get("reader") == "structured_text"]
        self.assertEqual([row["stage_key"] for row in options], ["step-one", "step-two"])
        self.assertEqual(options[0]["output_key"], options[1]["output_key"])
        self.assertNotEqual(text_output_schema_key(options[0]), text_output_schema_key(options[1]))
        self.assertTrue(all(text_output_schema_key(row) in self.plan.value["structure_source_schemas"]["selected_output"] for row in options))
        self.assertEqual(sum('FROM "studio_executionversioncontract"' in row["sql"] for row in queries), 1)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        with self.assertRaisesMessage(ValidationError, "未在服务器目录"):
            structure_source_option({"kind": "selected_output", "output_key": options[0]["output_key"]}, "input", options)
        self.assertEqual(structure_source_option(self.source(), "input", options), options[0])

    def test_authoring_source_api_is_read_only_and_keeps_exact_step_outputs(self):
        from rest_framework.test import APIClient
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient(); client.force_authenticate(self.actor)
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": deepcopy(self.declarations)}
        # Source configuration is still unfinished; a directory read must not require it.
        definition["stages"][1]["routing"]["routes"][0]["input_sources"] = {"unfinished": {}}
        with CaptureQueriesContext(connection) as queries:
            result = client.post("/api/v1/production-recipes/source-options/", {"definition": definition}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        outputs = result.data["text_output_options"]
        self.assertEqual([row["stage_key"] for row in outputs], ["step-one", "step-two"])
        self.assertEqual(outputs[0]["output_key"], outputs[1]["output_key"])
        self.assertTrue(outputs[0]["label"].startswith("1 · "))
        self.assertTrue(outputs[1]["label"].startswith("2 · "))
        self.assertIn("value_schema", outputs[0])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        for invalid in (None, {}, {**definition, "stages": [None]},
                {**definition, "stages": [definition["stages"][0]] * 2}):
            response = client.post("/api/v1/production-recipes/source-options/", {"definition": invalid}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        definition["stages"][0]["routing"]["routes"][0]["expected_definition_sha256"] = "f" * 64
        response = client.post("/api/v1/production-recipes/source-options/", {"definition": definition}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.actor.is_staff = False
        self.actor.save(update_fields=("is_staff",))
        self.assertEqual(client.post("/api/v1/production-recipes/source-options/", {"definition": definition}, format="json").status_code, 403)
        client.force_authenticate(None)
        self.assertIn(client.post("/api/v1/production-recipes/source-options/", {}, format="json").status_code, (401, 403))
    def test_zero_is_original_selected_value_without_business_review_or_read_writes(self):
        artifact = self.produce(0)
        count = ArtifactSelectionDecision.objects.count()
        with CaptureQueriesContext(connection) as queries:
            _, result = self.resolve(self.source())
        self.assertEqual(result["logical_values"]["input"], 0)
        origin = result["input_provenance"][0]
        self.assertEqual(origin["artifact_id"], str(artifact.pk))
        self.assertEqual(origin["selection_mode"], "automatic")
        self.assertIsNone(origin["selected_by"])
        self.assertNotIn("approval_id", origin)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertEqual(ArtifactSelectionDecision.objects.count(), count)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))

    def test_false_is_not_treated_as_missing(self):
        self.output_layout([self.node("root", "boolean")])
        self.produce(False)
        _, result = self.resolve(self.source())
        self.assertIs(result["logical_values"]["input"], False)

    def test_null_is_not_replaced_with_default_or_empty_text(self):
        self.output_layout([self.node("root", "text", allow_null=True)])
        self.produce(None)
        _, result = self.resolve(self.source())
        self.assertIsNone(result["logical_values"]["input"])

    def layout_mapping(self, property_name):
        containers = ([self.node("root", "object"), self.node("rows", "list", "root", property_name=property_name, required=True)]
            if property_name else [self.node("root", "list")])
        self.output_layout([*containers,
            self.node("item", "object", "rows" if property_name else "root"), self.node("n", "integer", "item", property_name="count", required=True),
            self.node("flag", "boolean", "item", property_name="enabled", required=True, position=1),
            self.node("nullable", "text", "item", property_name="note", allow_null=True, required=True, position=2)])
        rows = [{"count": 0, "enabled": False, "note": None}]
        self.produce({property_name: rows} if property_name else rows)
        template = {"kind": "each", "source": {**self.source(), "path": [property_name] if property_name else []},
            "item": {"kind": "object", "fields": {"quantity": {"kind": "item_field", "path": ["count"]},
                "active": {"kind": "item_field", "path": ["enabled"]}, "text": {"kind": "item_field", "path": ["note"]}}}}
        _, result = self.resolve({"kind": "structured", "template": template}, schema={"type": "array", "items": {
            "type": "object", "properties": {"quantity": {"type": "integer"}, "active": {"type": "boolean"},
                "text": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}})
        self.assertEqual(result["logical_values"]["input"], [{"quantity": 0, "active": False, "text": None}])
        self.assertEqual(result["input_provenance"][0]["leaves"][0]["stage_key"], "step-one")
        with self.assertRaises(ValidationError):
            self.resolve({"kind": "structured", "template": {**self.source(), "path": ["not-in-contract"]}})

    def test_layout_a_paths(self):
        self.layout_mapping("first.layout")

    def test_layout_b_paths(self):
        self.layout_mapping(None)

    def test_no_selection_wrong_stage_wrong_run_or_output_never_falls_back(self):
        artifact = self.produce(0, select=False)
        with self.assertRaises(ValidationError):
            self.resolve(self.source())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        choose_structured_text_candidate(self.steps[0])
        self.complete_first_step()
        bound, _ = _project_inputs({"input": self.source()}, self.plan, self.steps[1])
        args = {"spec": self.consumer().value["configuration"]["inputs"][0], "owner": self.actor,
            "project_id": self.project.pk, "production_run_id": str(self.run.pk), "eligible_stage_ids": [self.steps[0].pk]}
        for change in ({"stage_key": "step-two"}, {"output_key": "structured_text." + str(uuid4())},
                {"decision_id": str(uuid4())}, {"artifact_id": str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                resolve_selected_execution_text({**bound["input"], **change}, **args)
        with self.assertRaises(PermissionDenied):
            resolve_selected_execution_text(bound["input"], **{**args, "production_run_id": str(uuid4())})
        for prior in ([], ["unrelated"]):
            with self.assertRaisesMessage(ValidationError, "更早阶段"):
                validate_recipe_input_sources(self.consumer(), {"input": self.source()}, prior_stages=prior,
                    text_outputs=plan_text_output_options(self.plan))
        self.assertEqual(ArtifactSelectionDecision.objects.get().artifact_id, artifact.pk)

    def test_new_current_recipe_does_not_change_frozen_source_and_output_identity(self):
        expected = plan_text_output_options(self.plan)
        self.produce(0)
        assign_json_contract(self.recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": []}, schema_version=3)
        self.recipe.save()
        with patch("studio.services.production_run_plans.production_stage_catalog", side_effect=AssertionError("no current catalog")):
            self.assertEqual(plan_text_output_options(get_run_execution_plan(self.run)), expected)
            self.assertEqual(self.resolve(self.source())[1]["logical_values"]["input"], 0)

    def test_routes_with_equal_shape_but_different_output_versions_cannot_form_one_catalog(self):
        old = deepcopy(self.declarations[0]["routing"]["routes"][0])
        self.output_layout([self.node("root", "integer")])
        stages = deepcopy(self.declarations)
        stages[0]["routing"]["routes"].append(old)
        with self.assertRaisesMessage(ValidationError, "确切输出版本"):
            execution_text_output_options(stages)

    def test_malformed_draft_catalog_fails_as_validation_not_unhandled_key_or_type_error(self):
        from studio.services.production_recipe_contracts import validate_recipe_definition
        for routing in (None, [], {}, {"routes": None}, {"routes": [None]}, {"routes": [{}]},
                {"routes": [{"execution_instance_version_id": [], "expected_definition_sha256": "x"}]}):
            stages = deepcopy(self.declarations)
            stages[0]["routing"] = routing
            with self.subTest(routing=routing), self.assertRaises(ValidationError):
                validate_recipe_definition({"schema_version": 3, "interaction_mode": "assisted", "stages": stages})

    def test_configured_profile_consumes_selected_value_in_original_preparation_and_replay(self):
        from studio.models import PromptProfileVersion
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import create_execution_draft, publish_execution_version
        from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as source_key
        from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
        from studio.services.production_recipe_contracts import validate_recipe_definition
        from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule

        structure = self.publish(self.create([self.node("root", "object"),
            self.node("n", "integer", "root", property_name="quantity", required=True)]))
        profile = PromptProfileVersion.objects.create(profile_key="downstream-generic", version=1,
            name="Isolated downstream consumer", model_family="text-protocol", operation="text.structured",
            mode="", review_state="ready", input_structure_version=structure)
        field = create_field_definition(key="quantity-in-prompt", label="Quantity", value_type="integer")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=source_key,
            generation_source_selector=source_key, source_path=["quantity"], channel="positive_prompt",
            position=1, required=True, language_policy="any")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        version = create_execution_draft(instance_id=self.version.instance_id,
            expected_revision=self.version.instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "text.structured", "mode": "", "prompt_mode": "profile",
                "workflow_version": None, "prompt_profile_version": profile, "output_version": self.version.output_version})
        definition = build_execution_definition(version.pk)
        self.assertIn("project", definition.value["configuration"]["inputs"][0]["semantic_contract"]["allowed_scope_kinds"])
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        self.declarations[1]["routing"]["routes"][0].update(execution_instance_version_id=str(version.pk),
            expected_definition_sha256=definition.sha256, input_sources={source_key: {"kind": "structured", "template": {
                "kind": "object", "fields": {"quantity": self.source()}}}})
        validate_recipe_definition({"schema_version": 3, "interaction_mode": "assisted", "stages": self.declarations}, require_complete=True)
        from rest_framework.test import APIClient
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient(); client.force_authenticate(self.actor)
        saved = client.post("/api/v1/production-recipes/", {"recipe_key": "generic-consumer-" + uuid4().hex,
            "name": "Isolated downstream wiring", "definition": {"schema_version": 3,
                "interaction_mode": "assisted", "stages": self.declarations}}, format="json")
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual(saved.data["definition"]["stages"], self.declarations)
        self.reset_empty_plan()
        self.produce(0)
        prep = self.prepare(self.steps[1])
        original = deepcopy(prep.contract.resolved_inputs)
        self.assertEqual(original["inputs"]["logical_values"][source_key], {"quantity": 0})
        prep = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertIn("0", preview.transport.value["prompt_channels"]["values"]["positive_prompt"])
        with patch("studio.services.production_recipe_preparations.resolve_execution_request_facts",
                side_effect=AssertionError("replay must not reread upstream")):
            replay = self.prepare(self.steps[1])
        self.assertEqual(replay.pk, prep.pk)
        self.assertEqual(replay.contract.resolved_inputs, original)
