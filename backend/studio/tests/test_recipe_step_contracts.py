"""G03 contracts use the formal repeatable catalog without provider calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import ExecutionPreparation, GenerationAttempt, ModelExecutionInstanceVersion
from studio.services.business_outputs import write_output
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, publish_execution_version, update_execution_draft
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import (
    recipe_stage_declarations, recipe_stage_kind, validate_recipe_execution_routing,
    validate_recipe_route_output_identity, verify_recipe_routing_output_identity,
)
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_execution_text_contracts as text_fixture


class RecipeStepIdentityTests(SimpleTestCase):
    def setUp(self):
        self.catalog = deepcopy(production_stage_catalog().value)
        self.first = {"key": "step-a", "kind": "structured_text", "name": "同名步骤",
            "mode": "ai", "approval": "auto"}
        self.second = {**self.first, "key": "step-b"}
        self.input = {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"}
        self.assembly = {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"}

    def parse(self, stages, **changes):
        return recipe_stage_declarations({"schema_version": 3, "interaction_mode": "assisted", "stages": stages},
            changes.get("catalog", self.catalog))

    def test_same_duty_and_label_retain_independent_identities_and_configurations(self):
        second = {**self.second, "approval": "required"}
        stages = [self.first, self.input, second, self.assembly]
        parsed = self.parse(stages)
        self.assertEqual(parsed, stages)
        self.assertEqual([recipe_stage_kind(row) for row in parsed],
            ["structured_text", "script_input", "structured_text", "assembly"])
        parsed[0]["name"] = "rename only"
        self.assertEqual(stages[0]["name"], "同名步骤")
        self.assertEqual(parsed[0]["key"], "step-a")

    def test_repeatable_steps_can_appear_in_each_position_without_reordering_singletons(self):
        for position in range(3):
            stages = [self.input, self.assembly]
            stages.insert(position, self.first)
            self.assertEqual(self.parse(stages), stages)
        with self.assertRaises(ValidationError):
            self.parse([self.assembly, self.first, self.input])
        repeated = [{**self.first, "key": f"step-{index}"} for index in range(20)]
        self.assertEqual(len(self.parse(repeated)), 20)

    def test_duplicate_reserved_missing_or_unregistered_identity_is_rejected(self):
        invalid = [
            [self.first, self.first], [self.input, self.input],
            [{**self.first, "key": "script_input"}], [{**self.first, "key": "structured_text"}],
            [{**self.first, "key": " "}], [{**self.first, "key": "x" * 101}],
            [{**self.first, "kind": "unknown"}], [{**self.first, "kind": []}],
            [{**self.first, "name": " "}], [{**self.first, "mode": "deterministic"}],
            [{**self.input, "kind": "script_input"}],
            [{key: value for key, value in self.first.items() if key != "kind"}],
        ]
        for stages in invalid:
            with self.subTest(stages=stages), self.assertRaises(ValidationError):
                self.parse(stages)

    def test_formal_registration_without_database_or_arbitrary_graph(self):
        self.assertEqual(self.parse([self.first], catalog=production_stage_catalog().value), [self.first])
        for extra in ({"branches": []}, {"loop": True}, {"depends_on": ["step-b"]}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.parse([{**self.first, **extra}])
        with patch("studio.services.production_recipe_contracts.production_stage_catalog",
                side_effect=AssertionError("Do not replace supplied catalog with current registration")):
            self.assertEqual(self.parse([self.first, self.second]), [self.first, self.second])


class RecipeRouteOutputIdentityTests(SimpleTestCase):
    def definition(self, *, operation="text.structured", identifier="output-a", digest="a" * 64):
        return json_contract({"configuration": {"operation": operation}, "interface": {
            "business_output": {"id": identifier, "sha256": digest}}})

    def test_exact_identity_not_field_shape_controls_route_interchangeability(self):
        first = self.definition()
        validate_recipe_route_output_identity([first, deepcopy(first)])
        for other in (self.definition(identifier="output-b"), self.definition(digest="b" * 64),
                self.definition(operation="visual_rules.generate")):
            with self.subTest(other=other), self.assertRaises(ValidationError) as raised:
                validate_recipe_route_output_identity([first, other])
            self.assertIn("routing.routes[1].execution_instance_version_id", raised.exception.message_dict)

    def test_typed_and_untyped_routes_cannot_mix_in_either_order(self):
        untyped = json_contract({"configuration": {"operation": "prompt.draft"}, "interface": {}})
        for definitions in ([self.definition(), untyped], [untyped, self.definition()]):
            with self.assertRaises(ValidationError):
                validate_recipe_route_output_identity(definitions)
        validate_recipe_route_output_identity([untyped, untyped])
        validate_recipe_route_output_identity([])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class RecipeStructuredRoutingTests(TestCase):
    version = text_fixture.ExecutionTextContractTests.version

    @classmethod
    def setUpTestData(cls):
        text_fixture.ExecutionTextContractTests.setUpTestData.__func__(cls)

    def setUp(self):
        text_fixture.ExecutionTextContractTests.setUp(self)
        self.enterContext(patch("studio.services.comfyui.ComfyUIClient", side_effect=AssertionError("no media call")))
        self.first = self.version("text.structured")
        self.first_route = self.publish_route(self.first)
        self.second = clone_execution_draft(source_id=self.first.pk, expected_revision=self.first.revision,
            expected_definition_sha256=self.first_route["expected_definition_sha256"], actor=self.actor)

    def publish_route(self, version):
        definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        version.refresh_from_db()
        return {"execution_instance_version_id": str(version.pk), "expected_definition_sha256": definition.sha256,
            "input_sources": {}, "parameter_policies": []}

    def routing(self, second):
        return {"mode": "ordered_fallback", "variant_count": 2, "evaluator": "first_succeeded",
            "routes": [self.first_route, second]}

    def assert_no_work(self):
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_authoring_and_frozen_alternatives_accept_exact_shared_output(self):
        routing = self.routing(self.publish_route(self.second))
        for complete in (False, True):
            self.assertEqual(validate_recipe_execution_routing(routing, prior_stages=[], require_complete=complete), routing)
        verify_recipe_routing_output_identity(routing)
        self.assert_no_work()

    def test_distinct_output_with_same_structure_is_not_interchangeable(self):
        original = self.first.output_version
        output = write_output(actor=self.actor, data={"name": "Same layout, distinct output identity",
            "parser_key": "structured_json.v1", "data_structure_version": original.data_structure_version_id})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=1, action="publish")
        self.second = update_execution_draft(version_id=self.second.pk, expected_revision=self.second.revision,
            actor=self.actor, changes={"output_version": output})
        routing = self.routing(self.publish_route(self.second))
        for complete in (False, True):
            with self.assertRaises(ValidationError):
                validate_recipe_execution_routing(routing, prior_stages=[], require_complete=complete)
        with self.assertRaises(ValidationError):
            verify_recipe_routing_output_identity(routing)
        self.assert_no_work()

    def test_frozen_read_checks_digest_without_live_readiness_or_reselection(self):
        routing = self.routing(self.publish_route(self.second))
        ModelExecutionInstanceVersion.objects.filter(pk=self.second.pk).update(status="retired")
        with patch("studio.services.production_recipe_contracts.verify_current_execution_definition",
                side_effect=AssertionError("Historical alternatives do not require current readiness")):
            verify_recipe_routing_output_identity(routing)
        routing["routes"][1]["expected_definition_sha256"] = "0" * 64
        with self.assertRaises(ValidationError) as raised:
            verify_recipe_routing_output_identity(routing)
        self.assertIn("routing.routes[1].expected_definition_sha256", raised.exception.message_dict)
        self.assert_no_work()
