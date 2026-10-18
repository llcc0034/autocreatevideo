"""The same non-mutating route shape for authoring and frozen execution."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.production_recipe_contracts import recipe_execution_reference, validate_recipe_execution_routing
from studio.services.production_recipe_routing import recipe_routing


def route():
    return {"execution_instance_version_id": "abcdef12-3456-4789-abcd-0123456789ab",
        "expected_definition_sha256": "a" * 64, "input_sources": {}, "parameter_policies": []}


def routing():
    return {"mode": "fixed", "variant_count": 1, "evaluator": None, "routes": [route()]}


class RecipeRoutingContractTests(SimpleTestCase):
    def test_route_identity_must_not_be_normalized_only_for_lookup(self):
        identifier = route()["execution_instance_version_id"]
        for alias in (identifier.upper(), identifier.replace("-", ""), "{" + identifier + "}", "urn:uuid:" + identifier):
            value = {**route(), "execution_instance_version_id": alias}
            with self.subTest(alias=alias):
                for read in (recipe_execution_reference, lambda row: recipe_routing({"routing": {
                        **routing(), "routes": [row]}})):
                    with self.assertRaises(ValidationError):
                        read(value)
                self.assertEqual(value["execution_instance_version_id"], alias)

    def test_frozen_reader_rejects_invalid_reference_and_containers_without_io(self):
        changes = [{"execution_instance_version_id": value} for value in (None, True, 12, {}, [], "bad")]
        changes += [{"expected_definition_sha256": value} for value in (None, True, {}, "A" * 64, "a" * 63)]
        changes += [{"input_sources": value} for value in (None, [], "")]
        changes += [{"parameter_policies": value} for value in (None, {}, "")]
        for change in changes:
            value = {**routing(), "routes": [{**route(), **change}]}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                recipe_routing({"routing": value})

    def test_frozen_reader_accepts_valid_choices_without_current_config_or_rewriting(self):
        for mode in ("fixed", "manual_choice", "ordered_fallback"):
            value = {**routing(), "mode": mode}
            if mode != "fixed":
                value["routes"].append({**route(), "parameter_policies": [{"key": "seed", "policy": "default", "value": 9}]})
            before = deepcopy(value)
            with self.subTest(mode=mode), patch("studio.services.production_recipe_contracts.boundary_int",
                    side_effect=AssertionError("Historical shape cannot read today's limits")), patch(
                    "studio.services.production_recipe_contracts.stored_execution_contract",
                    side_effect=AssertionError("Shape does not resolve today's instance")):
                self.assertEqual(recipe_routing({"routing": value}), value)
            self.assertEqual(value, before)

    def test_author_and_runtime_reject_same_malformed_complete_shape(self):
        malformed = [None, [], {}, {**routing(), "extra": True}]
        for key, values in (("mode", (None, True, [], "first")),
                ("variant_count", (None, True, False, 1.0, 0, -1, "2")),
                ("evaluator", (False, [], {}, "latest")), ("routes", (None, {}, [], [None], [route(), route()]))):
            malformed.extend({**routing(), key: value} for value in values)
        for value in malformed:
            with self.subTest(value=value), patch("studio.services.production_recipe_contracts.boundary_int", return_value=20):
                with self.assertRaises(ValidationError):
                    validate_recipe_execution_routing(value, prior_stages=[])
                with self.assertRaises(ValidationError):
                    recipe_routing({"routing": value})

    def test_draft_empty_routes_are_explicit_but_not_executable(self):
        value = {**routing(), "routes": []}
        with patch("studio.services.production_recipe_contracts.boundary_int", return_value=20):
            self.assertEqual(validate_recipe_execution_routing(value, prior_stages=[], require_complete=False), value)
        with self.assertRaises(ValidationError):
            recipe_routing({"routing": value})

    def test_current_authoring_limits_do_not_reinterpret_a_valid_frozen_plan(self):
        value = {**routing(), "variant_count": 2}
        with patch("studio.services.production_recipe_contracts.boundary_int", return_value=1):
            with self.assertRaises(ValidationError):
                validate_recipe_execution_routing(value, prior_stages=[])
            self.assertEqual(recipe_routing({"routing": value}), value)

