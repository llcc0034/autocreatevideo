"""Schema3 authoring proofs, not publication/production/remote acceptance."""
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, SimpleTestCase, override_settings

from studio.models import ExecutionPreparation, ModelExecutionInstance, ModelExecutionInstanceVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, publish_execution_version
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import (
    recipe_stage_declarations, validate_recipe_execution_route,
    validate_recipe_execution_routing, validate_recipe_input_sources,
)
from studio.tests import test_execution_definitions as definition_fixtures


class RecipeStageDeclarationTests(SimpleTestCase):
    def test_only_schema3_explicit_interaction_and_registered_order(self):
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "script_review", "mode": "review", "approval": "required"},
        ]}
        self.assertEqual(recipe_stage_declarations(definition), definition["stages"])
        invalid = [{**definition, "schema_version": version} for version in (True, 1, 2, 4)]
        invalid += [{**definition, "interaction_mode": None}, {**definition, "deployment_id": str(uuid4())},
            {**definition, "stages": list(reversed(definition["stages"]))},
            {**definition, "stages": [definition["stages"][0]] * 2},
            {**definition, "stages": [{"key": "assembly", "mode": "ai", "approval": "auto"}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                recipe_stage_declarations(value)

    def source_definition(self, *, required=True, allowed=None):
        target = {"kind": "text"}
        if allowed:
            target.update(source_kinds=["context_field"], context_selectors=allowed)
        return json_contract({"configuration": {"inputs": [{"key": "source", "kind": "text", "required": required,
            "min_items": int(required), "max_items": 1, "semantic_contract": {"allowed_scope_kinds": ["project"]},
            "targets": [{"target_kind": "operation_input", "target_key": "text"}]}]},
            "interface": {"input_targets": {"text": target}}})

    def test_future_selection_is_not_resolved_and_cannot_reference_future_stage(self):
        source = {"kind": "selected_output", "stage_key": "script_generate", "output_key": "generated_script.script.content",
            "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}
        validate_recipe_input_sources(self.source_definition(), {"source": source}, prior_stages=["script_generate"])
        for changed in ({"stage_key": "script_review"}, {"selection_policy": "latest"}, {"output_key": "unknown"},
                {"output_key": "generated_script.script.visual_context"},
                {"scope": {"kind": "project", "id": str(uuid4())}}, {"decision_id": str(uuid4())}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                validate_recipe_input_sources(self.source_definition(), {"source": {**source, **changed}}, prior_stages=["script_generate"])

    def test_context_target_rejects_literal_and_unknown_selector_without_reading_database(self):
        definition = self.source_definition(allowed=["script.content"])
        source = {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}}
        validate_recipe_input_sources(definition, {"source": source}, prior_stages=[])
        for sources in ({}, {"unknown": source}, {"source": {"kind": "literal", "value": "pretend confirmed"}},
                {"source": {**source, "selector_key": "$.project.secret"}}, {"source": {**source, "content_revision_id": str(uuid4())}}):
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                validate_recipe_input_sources(definition, sources, prior_stages=[])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeExecutionRouteTests(TestCase):
    # Reuse the original real, isolated resource fixture without inheriting and
    # rerunning the whole definition test class or mocking a published contract.
    write_contract = staticmethod(definition_fixtures.ExecutionDefinitionTests.write_contract)

    @classmethod
    def setUpTestData(cls):
        definition_fixtures.ExecutionDefinitionTests.setUpTestData.__func__(cls)

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential",
                "studio.services.comfyui.ComfyUIClient", "studio.services.execution_parameter_preparation.randbelow"):
            guard = patch(target, side_effect=AssertionError("Authoring cannot call providers or allocate seed"))
            guard.start()
            self.addCleanup(guard.stop)
        self.definition = build_execution_definition(self.version.pk)
        publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": {}, "parameter_policies": [{"key": "width", "policy": "locked_value", "value": 512}]}

    def test_exact_route_reuses_parameter_intersection_without_creating_preparation(self):
        self.assertEqual(validate_recipe_execution_route(self.route, prior_stages=[]), self.route)
        self.assertFalse(ExecutionPreparation.objects.exists())
        for policies in ([{"key": "fps", "policy": "locked_value", "value": 25}],
                [{"key": "width", "policy": "default", "value": True}], [{"key": "secret", "policy": "default", "value": 1}]):
            with self.subTest(policies=policies), self.assertRaises(ValidationError):
                validate_recipe_execution_route({**self.route, "parameter_policies": policies}, prior_stages=[])

    def test_stale_digest_deleted_draft_and_retired_references_rejected(self):
        for change in ({"expected_definition_sha256": "0" * 64}, {"execution_instance_version_id": str(uuid4())},
                {"workflow_version_id": str(uuid4())}, {"deployment_id": str(self.deployment.pk)}, {"expected_definition_sha256": None}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_recipe_execution_route({**self.route, **change}, prior_stages=[])
        self.version.refresh_from_db()
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route({**self.route, "execution_instance_version_id": str(draft.pk)}, prior_stages=[])
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(status="retired")
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(self.route, prior_stages=[])
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(status="published")
        ModelExecutionInstance.objects.filter(pk=self.instance.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(self.route, prior_stages=[])

    def test_registered_parameter_source_is_authored_without_reading_project_or_allocating_seed(self):
        source = {"kind": "context_scalar", "selector_key": "project.width", "scope": {"kind": "project"}}
        route = {**self.route, "parameter_policies": [{"key": "width", "policy": "locked_source", "source": source}]}
        with patch("studio.services.production_parameter_sources.bind_recipe_parameter_sources",
                side_effect=AssertionError("Authoring cannot read actual project")):
            self.assertEqual(validate_recipe_execution_route(route, prior_stages=[]), route)
        incomplete = {**route, "parameter_policies": [{"key": "width", "policy": "locked_source", "source": {**source, "selector_key": ""}}]}
        validate_recipe_execution_route(incomplete, prior_stages=[], require_complete=False)
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(incomplete, prior_stages=[], require_complete=True)
        for change in ({"selector_key": "$.width"}, {"scope": {"kind": "project", "id": str(uuid4())}},
                {"value": 512}, {"kind": "context_field"}):
            bad = {**route, "parameter_policies": [{"key": "width", "policy": "locked_source", "source": {**source, **change}}]}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_recipe_execution_route(bad, prior_stages=[], require_complete=False)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_routing_preserves_explicit_fallback_order_and_platform_limits(self):
        routing = {"mode": "fixed", "routes": [self.route], "variant_count": 1, "evaluator": None}
        self.assertEqual(validate_recipe_execution_routing(routing, prior_stages=[]), routing)
        with self.assertRaises(ValidationError):
            validate_recipe_execution_routing({key: value for key, value in routing.items() if key != "variant_count"}, prior_stages=[])
        other = {**self.route, "parameter_policies": []}
        fallback = {**routing, "mode": "ordered_fallback", "routes": [self.route, other]}
        self.assertEqual(validate_recipe_execution_routing(fallback, prior_stages=[])['routes'], [self.route, other])
        for changed in ({"routes": []}, {"routes": [self.route, self.route]}, {"variant_count": True},
                {"evaluator": "auto_pick_best"}, {"mode": "latest"}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                validate_recipe_execution_routing({**routing, **changed}, prior_stages=[])

    def test_raw_model_contract_drift_cannot_be_frozen_into_recipe(self):
        type(self.model).objects.filter(pk=self.model.pk).update(specification={"unverified": True})
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(self.route, prior_stages=[])
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_raw_provider_contract_drift_cannot_be_frozen_into_recipe(self):
        type(self.provider).objects.filter(pk=self.provider.pk).update(driver_config={"unverified": True})
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(self.route, prior_stages=[])
        self.assertFalse(ExecutionPreparation.objects.exists())
