"""Seed policies are explicit and independent of model/parameter names."""
from fractions import Fraction
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.models import ExecutionParameterSpec
from studio.services.execution_parameter_preparation import initialize_execution_parameters
from studio.services.execution_parameters import (
    compile_execution_parameter_contract, execution_seed_domain, instance_parameter_rule,
    resolve_execution_parameters,
)


class ExecutionSeedParameterTests(SimpleTestCase):
    def setUp(self):
        boundary = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        boundary.start()
        self.addCleanup(boundary.stop)
        draw = patch("studio.services.execution_parameter_preparation.randbelow", return_value=0)
        self.draw = draw.start()
        self.addCleanup(draw.stop)

    def layers(self, **changes):
        return {"explicit.integer.input": [{"source": "instance.parameters", "rule": {
            "type": "integer", "required": True, "minimum": 0, "maximum": 100,
            "seed_policy": "generate_once", **changes,
        }}]}

    def test_compile_and_read_do_not_generate_seeds(self):
        layers = self.layers()
        compiled = compile_execution_parameter_contract(layers)
        self.assertNotIn("default", compiled["explicit.integer.input"]["rule"])
        with self.assertRaisesMessage(ValidationError, "首次准备"):
            resolve_execution_parameters(layers, {})
        self.draw.assert_not_called()

    def test_first_request_draws_once_and_replay_uses_exact_values(self):
        self.draw.return_value = 73
        result = initialize_execution_parameters(self.layers(), {})
        self.assertEqual(result["values"], {"explicit.integer.input": 73})
        self.assertEqual(result["sources"], {"explicit.integer.input": "preparation.generated"})
        self.draw.assert_called_once_with(101)
        self.draw.reset_mock()
        for _ in range(3):
            replay = resolve_execution_parameters(self.layers(), {}, generated_values=result["generated_values"])
            self.assertEqual(replay["values"], result["values"])
        self.draw.assert_not_called()

    def test_plain_seed_named_parameter_never_gets_implicit_randomness(self):
        layers = {"sampling.seed": [{"source": "workflow", "rule": {"type": "integer", "required": True}}]}
        with self.assertRaisesMessage(ValidationError, "缺少必需参数"):
            initialize_execution_parameters(layers, {})
        self.draw.assert_not_called()

    def test_fixed_seed_uses_normal_explicit_precedence_and_locks(self):
        for policy in ({"default": 7}, {"const": 7}):
            result = initialize_execution_parameters(self.layers(seed_policy="fixed", **policy), {})
            self.assertEqual(result["values"], {"explicit.integer.input": 7})
        self.assertEqual(result["generated_values"], {})
        self.draw.assert_not_called()

    def test_large_explicit_integer_domain_keeps_its_exact_boundary_and_replays(self):
        maximum = (1 << 53) - 1
        layers = self.layers(minimum=0, maximum=maximum)
        self.draw.return_value = maximum
        first = initialize_execution_parameters(layers, {})
        self.assertEqual(first["values"]["explicit.integer.input"], maximum)
        self.draw.assert_called_once_with(maximum + 1)
        self.draw.reset_mock()
        replay = resolve_execution_parameters(layers, {}, generated_values=first["generated_values"])
        self.assertEqual(replay["values"], first["values"])
        self.draw.assert_not_called()

    def test_generation_replaces_lower_default_but_not_hard_fixed_value(self):
        layers = self.layers(minimum=10, maximum=20)
        layers["explicit.integer.input"].insert(0, {"source": "workflow", "rule": {
            "type": "integer", "required": True, "minimum": 0, "maximum": 100, "default": 1,
        }})
        self.assertEqual(initialize_execution_parameters(layers, {})["values"]["explicit.integer.input"], 10)
        layers["explicit.integer.input"][0]["rule"]["const"] = 10
        layers["explicit.integer.input"][0]["rule"].pop("default")
        with self.assertRaisesMessage(ValidationError, "底层固定"):
            compile_execution_parameter_contract(layers)

    def test_no_conflicting_policy_or_local_defaults(self):
        for changes in ({"default": 2}, {"const": 2}, {"seed_policy": "random"}, {"type": "number"}, {"type": "boolean"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                compile_execution_parameter_contract(self.layers(**changes))
        layers = self.layers()
        layers["explicit.integer.input"].append({"source": "stage", "rule": {
            "type": "integer", "required": True, "seed_policy": "fixed",
        }})
        with self.assertRaisesMessage(ValidationError, "种子策略冲突"):
            compile_execution_parameter_contract(layers)

    def test_generation_requires_finite_explicit_domain(self):
        for bound in ("minimum", "maximum"):
            layers = self.layers()
            del layers["explicit.integer.input"][0]["rule"][bound]
            with self.assertRaisesMessage(ValidationError, "有限范围"):
                compile_execution_parameter_contract(layers)

    def test_random_enum_is_intersected_not_read_from_workflow_examples(self):
        layers = self.layers(enum=[9, 21, 33])
        del layers["explicit.integer.input"][0]["rule"]["minimum"]
        del layers["explicit.integer.input"][0]["rule"]["maximum"]
        self.draw.return_value = 2
        self.assertEqual(initialize_execution_parameters(layers, {})["values"]["explicit.integer.input"], 33)
        self.draw.assert_called_once_with(3)

    def test_uint64_domain_does_not_materialize_all_values(self):
        self.draw.return_value = 2 ** 64 - 1
        result = initialize_execution_parameters(self.layers(maximum=2 ** 64 - 1), {})
        self.assertEqual(result["values"]["explicit.integer.input"], 2 ** 64 - 1)
        self.draw.assert_called_once_with(2 ** 64)

    def test_exclusions_draw_uniformly_from_remaining_aligned_domain(self):
        layers = self.layers(minimum=1, maximum=17, alignments=[{"modulus": 4, "remainder": 1}])
        for draw, expected in enumerate([5, 13, 17]):
            with self.subTest(draw=draw):
                self.draw.reset_mock()
                self.draw.return_value = draw
                result = initialize_execution_parameters(layers, {},
                    excluded_values={"explicit.integer.input": [1, 9, 9, -3, 2, 99]})
                self.assertEqual(result["values"]["explicit.integer.input"], expected)
                self.draw.assert_called_once_with(3)

    def test_enum_and_uint64_exclusions_do_not_redraw_on_collision(self):
        self.draw.return_value = 0
        result = initialize_execution_parameters(self.layers(enum=[9, 21, 33]), {},
            excluded_values={"explicit.integer.input": [9, 21]})
        self.assertEqual(result["values"]["explicit.integer.input"], 33)
        self.draw.assert_called_once_with(1)
        self.draw.reset_mock()
        self.draw.return_value = 2 ** 64 - 3
        result = initialize_execution_parameters(self.layers(maximum=2 ** 64 - 1), {},
            excluded_values={"explicit.integer.input": [0, 2 ** 64 - 1]})
        self.assertEqual(result["values"]["explicit.integer.input"], 2 ** 64 - 2)
        self.draw.assert_called_once_with(2 ** 64 - 2)

    def test_exhausted_or_malformed_exclusions_fail_before_any_draw(self):
        for excluded in ({"explicit.integer.input": [1, 2]}, {"explicit.integer.input": [True]},
                {"explicit.integer.input": [1.5]}, {"unknown": [1]}, []):
            with self.subTest(excluded=excluded), self.assertRaises(ValidationError):
                initialize_execution_parameters(self.layers(minimum=1, maximum=2), {}, excluded_values=excluded)
        self.draw.assert_not_called()

    def test_all_alignments_preserved_in_random_domain(self):
        layers = self.layers(alignments=[{"modulus": 4, "remainder": 1}, {"modulus": 6, "remainder": 3}])
        self.draw.return_value = 7
        result = initialize_execution_parameters(layers, {})
        self.assertEqual(result["values"]["explicit.integer.input"], 93)
        self.draw.assert_called_once_with(8)

    def test_integer_fractional_and_negative_domains_match_brute_force(self):
        for step in (0.5, 1.5, 2, 3):
            for remainder in (0, 0.5):
                if remainder >= step:
                    continue
                layers = {"x": [
                    {"source": "model", "rule": {"type": "number", "required": True,
                        "minimum": -11, "maximum": 13, "alignments": [{"modulus": step, "remainder": remainder}]}},
                    {"source": "instance", "rule": {"type": "integer", "required": True, "seed_policy": "generate_once"}},
                ]}
                expected = [n for n in range(-11, 14) if (Fraction(n) - Fraction(str(remainder))) % Fraction(str(step)) == 0]
                if not expected:
                    with self.assertRaises(ValidationError):
                        compile_execution_parameter_contract(layers)
                    continue
                domain = execution_seed_domain("x", compile_execution_parameter_contract(layers)["x"]["rule"])
                actual = [domain["first"] + n * domain["step"] for n in range(domain["count"])]
                self.assertEqual(actual, expected)

    def test_invalid_request_does_not_draw_or_silently_override_generated_seed(self):
        for supplied in ({"explicit.integer.input": 1}, {"unknown": 1}, {"explicit.integer.input": True}, []):
            with self.subTest(supplied=supplied), self.assertRaises(ValidationError):
                initialize_execution_parameters(self.layers(), supplied)
        self.draw.assert_not_called()

    def test_replayed_generated_values_are_typed_complete_and_not_extra(self):
        for values in ({}, {"unknown": 1}, {"explicit.integer.input": True}, {"explicit.integer.input": 101}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                resolve_execution_parameters(self.layers(), {}, generated_values=values)
        self.draw.assert_not_called()

    def test_authoring_rows_keep_explicit_metadata_and_reject_invalid_union(self):
        spec = ExecutionParameterSpec(key="some.parameter", value_type="integer", policy="overridable", seed_policy="generate_once")
        self.assertEqual(instance_parameter_rule(spec)["seed_policy"], "generate_once")
        for changes in ({"value_type": "number"}, {"seed_policy": ""}, {"default_value": 5}, {"policy": "locked", "locked_value": 5}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                instance_parameter_rule(ExecutionParameterSpec(key="some.parameter", **{
                    "value_type": "integer", "policy": "overridable", "seed_policy": "generate_once", **changes,
                }))
