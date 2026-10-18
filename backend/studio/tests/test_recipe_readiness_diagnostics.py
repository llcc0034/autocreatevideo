"""Read-only recipe diagnosis with real isolated authoring contracts, no calls."""
from copy import deepcopy
from unittest.mock import patch
import traceback

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ProductionRecipeVersion, PromptProfileVersion
from studio.services.business_outputs import write_output
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_catalog import execution_text_output_options
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.execution_catalog_readiness import execution_requirements, published_execution_requirements
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_contracts import validate_recipe_definition
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
from studio.services.readiness import collect_production_readiness
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests import test_recipe_repeatable_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class RecipeReadinessDiagnosticsTests(TestCase):
    setUpTestData = fixtures.RepeatableRecipePreparationTests.__dict__["setUpTestData"]
    node = fixtures.RepeatableRecipePreparationTests.node
    create = fixtures.RepeatableRecipePreparationTests.create
    publish = fixtures.RepeatableRecipePreparationTests.publish
    output = fixtures.RepeatableRecipePreparationTests.output
    evaluator = "first_succeeded"

    def setUp(self):
        fixtures.RepeatableRecipePreparationTests.setUp(self)
        ProductionRecipeVersion.objects.exclude(pk=self.recipe.pk).update(status="retired")

    def save_definition(self, value):
        assign_json_contract(self.recipe, "definition", value, schema_version=value["schema_version"])
        self.recipe.save()

    def upstream_pair(self):
        structure = self.publish(self.create([self.node("root", "object"),
            self.node("count", "integer", "root", property_name="count", required=True)]))
        output = write_output(actor=self.actor, data={"name": "Isolated upstream", "parser_key": "structured_json.v1",
            "data_structure_version": structure.pk})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=1, action="publish")
        profile = PromptProfileVersion.objects.create(profile_key="readiness-structured-input", version=1,
            name="Exact root", model_family="text-protocol", operation="text.structured", mode="", review_state="ready",
            input_structure_version=structure)
        field = create_field_definition(key="readiness-count", label="Count", value_type="integer")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=STRUCTURED_INPUT_SELECTOR,
            source_path=["count"], generation_source_selector=STRUCTURED_INPUT_SELECTOR,
            generation_mode="passthrough", channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        routes = []
        for key, selected_profile in (("readiness-upstream", self.version.prompt_profile_version),
                ("readiness-consumer", profile)):
            instance = create_execution_instance(key=key, name=key, description="", actor=self.actor)
            version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
                selection={"deployment": self.deployment, "operation": "text.structured", "mode": "", "prompt_mode": "profile",
                    "workflow_version": None, "prompt_profile_version": selected_profile, "output_version": output})
            definition = build_execution_definition(version.pk)
            version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
                expected_definition_sha256=definition.sha256, actor=self.actor)
            routes.append({"execution_instance_version_id": str(version.pk), "expected_definition_sha256": definition.sha256,
                "input_sources": {}, "parameter_policies": []})
        value = deepcopy(self.recipe.definition)
        for stage, route in zip(value["stages"], routes):
            stage["routing"]["routes"] = [route]
        option = next(row for row in execution_text_output_options(value["stages"]) if row["stage_key"] == "step-one")
        routes[1]["input_sources"][STRUCTURED_INPUT_SELECTOR] = {"kind": "selected_output", "stage_key": "step-one",
            "output_key": option["output_key"], "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}
        return value

    def test_valid_dynamic_upstream_uses_same_catalog_as_complete_recipe_validation(self):
        value = self.upstream_pair()
        before = deepcopy(value)
        stages = validate_recipe_definition(value, require_complete=True)
        result = execution_requirements(stages)
        self.assertEqual(result.primary_version_ids, {row["routing"]["routes"][0]["execution_instance_version_id"] for row in stages})
        self.assertEqual(value, before)
        for change in ({"stage_key": "step-two"}, {"output_key": "unknown-output"}):
            invalid = deepcopy(value)
            invalid["stages"][1]["routing"]["routes"][0]["input_sources"][STRUCTURED_INPUT_SELECTOR].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                execution_requirements(invalid["stages"])

    def assert_catalog_diagnosis_does_not_enter_registry_write_guard_or_sql_transaction(self):
        transaction_calls = []
        def capture_transaction(execute, sql, params, many, context):
            if not transaction_calls and sql.strip().upper().startswith(("BEGIN", "SAVEPOINT", "RELEASE", "COMMIT")):
                transaction_calls.append((sql, "".join(traceback.format_stack(limit=24))))
            return execute(sql, params, many, context)
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard",
                side_effect=AssertionError("Read-only diagnosis must not acquire a writer guard")) as guard:
            with CaptureQueriesContext(connection) as queries, connection.execute_wrapper(capture_transaction):
                result = published_execution_requirements()
                with patch("studio.services.readiness._worker_check", return_value={"ok": True}):
                    report = collect_production_readiness()
        guard.assert_not_called()
        self.assertFalse(result.invalid_recipe_ids)
        self.assertNotIn("readiness_evaluation_failed", report["reason_codes"])
        for row in queries:
            sql = row["sql"].strip().upper()
            self.assertFalse(sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT")), transaction_calls or sql)
            self.assertNotIn("PG_ADVISORY", sql)
            self.assertNotIn("FOR UPDATE", sql)

    def test_admin_get_preserves_schema_error_and_does_not_rewrite_original_version(self):
        value = {"schema_version": 2, "stages": []}
        self.save_definition(value)
        digest = self.recipe.definition_sha256
        self.actor.is_superuser = True
        self.actor.save(update_fields=("is_superuser",))
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.get("/api/v1/production-readiness/")
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["recipes"][0]
        self.assertEqual(row["id"], str(self.recipe.pk))
        self.assertIn("recipe_contract_invalid", row["reason_codes"])
        error = row["validation_errors"][0]
        self.assertEqual(error["phase"], "definition")
        self.assertEqual(error["field_path"], "definition.schema_version")
        self.assertIn("schema3", error["message"])
        self.assertIsNone(error["stage_index"])
        self.assertIsNone(error["stage_key"])
        self.recipe.refresh_from_db()
        self.assertEqual((self.recipe.definition, self.recipe.definition_sha256), (value, digest))
        self.actor.is_superuser = False; self.actor.save(update_fields=("is_superuser",))
        self.assertEqual(client.get("/api/v1/production-readiness/").status_code, 403)

    def test_admin_route_error_identifies_second_stage_and_exact_route_without_claiming_success(self):
        value = deepcopy(self.recipe.definition)
        value["stages"][1]["routing"]["routes"][0]["expected_definition_sha256"] = "0" * 64
        self.save_definition(value)
        row = collect_production_readiness()["recipes"][0]
        self.assertEqual(row["state"], "blocked")
        error = row["validation_errors"][0]
        self.assertEqual((error["stage_index"], error["stage_key"], error["stage_kind"], error["routing_key"], error["route_index"]),
            (1, "step-two", "structured_text", "routing", 0))
        self.assertEqual(error["field_path"], "route.expected_definition_sha256")
        self.assertIn("定义已变化", error["message"])

    def test_non_validation_exception_stays_redacted_not_recipe_contract_error(self):
        with patch("studio.services.production_runs._recipe_stages", side_effect=RuntimeError("private-internal-marker")):
            row = collect_production_readiness()["recipes"][0]
        self.assertEqual(row["reason_codes"], ["readiness_evaluation_failed"])
        self.assertEqual(row["validation_errors"], [])
        self.assertNotIn("private-internal-marker", str(row))

    def test_existing_material_roles_do_not_invent_a_required_model_route(self):
        value = {"schema_version": 3, "interaction_mode": "assisted", "stages": [{"key": "bible_image_generation",
            "mode": "existing_pipeline", "approval": "auto", "material_sources": {"primary": "existing", "variant": "existing"}}]}
        stages = validate_recipe_definition(value, require_complete=True)
        self.assertEqual(execution_requirements(stages).versions, {})
        invalid = deepcopy(stages)
        invalid[0]["material_sources"]["primary"] = "generate"
        with self.assertRaises(ValidationError):
            execution_requirements(invalid)

    def test_shape_error_has_only_known_stage_identity_and_health_keeps_summary(self):
        value = deepcopy(self.recipe.definition)
        value["stages"][1]["mode"] = "unregistered"
        self.save_definition(value)
        errors = collect_production_readiness()["recipes"][0]["validation_errors"]
        self.assertEqual((errors[0]["stage_index"], errors[0]["stage_key"]), (1, "step-two"))
        self.assertEqual(errors[0]["field_path"], "definition.stages[1]")
        requirements = published_execution_requirements()
        self.assertEqual(set(requirements.invalid_recipes), {str(self.recipe.pk)})
        from studio.services.execution_catalog_readiness import catalog_capability_readiness
        summary = catalog_capability_readiness(requirements, worker_check=lambda _: {"ok": True}, toolchain_check=lambda: {"ok": True})
        self.assertNotIn(str(self.recipe.pk), str(summary))
        self.assertNotIn("step-two", str(summary))
        self.assertNotIn(errors[0]["message"], str(summary))
        self.assertNotIn("validation_errors", str(summary))
        client = APIClient()
        with patch("studio.services.readiness._capability_readiness_for_requirements", return_value=summary):
            response = client.get("/api/v1/health/ready/")
        self.assertIn(response.status_code, (200, 503))
        self.assertNotIn("validation_errors", str(response.data))
        self.assertNotIn("step-two", str(response.data))

    def test_author_validation_keeps_reference_write_guard(self):
        from studio.services.registry_reference_lock import registry_reference_write_guard
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard", wraps=registry_reference_write_guard) as guard:
            validate_recipe_definition(self.recipe.definition, require_complete=True)
        guard.assert_called_once()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class RecipeReadinessNoTransactionTests(TransactionTestCase):
    """No outer TestCase transaction: exercise the actual diagnostic read context.

    Django CheckConstraint.validate uses SAVEPOINT only when the caller is
    already in a transaction. Do not remove model checks to hide that behavior.
    """
    node = fixtures.RepeatableRecipePreparationTests.node
    create = fixtures.RepeatableRecipePreparationTests.create
    publish = fixtures.RepeatableRecipePreparationTests.publish
    output = fixtures.RepeatableRecipePreparationTests.output
    evaluator = "first_succeeded"

    def setUp(self):
        fixtures.RepeatableRecipePreparationTests.setUpTestData.__func__(type(self))
        RecipeReadinessDiagnosticsTests.setUp(self)

    test_catalog_diagnosis_does_not_enter_registry_write_guard_or_sql_transaction = RecipeReadinessDiagnosticsTests.assert_catalog_diagnosis_does_not_enter_registry_write_guard_or_sql_transaction
