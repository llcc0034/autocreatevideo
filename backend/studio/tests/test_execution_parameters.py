"""Parameter composition is deterministic and does not perform a model call."""
from copy import deepcopy
from fractions import Fraction
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.models import ExecutionParameterSpec
from studio.services.execution_parameters import (
    compile_execution_parameter_contract, instance_parameter_rule,
    resolve_execution_parameters, workflow_parameter_rule,
)


class ExecutionParameterCompositionTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        patcher.start()
        self.addCleanup(patcher.stop)

    def layers(self, *rules):
        return {"value": [
            {"source": f"source.{index}", "rule": {"type": "integer", "required": True, **rule}}
            for index, rule in enumerate(rules)
        ]}

    def test_explicit_defaults_then_request_preserve_sources_and_input(self):
        layers = self.layers({"minimum": 1, "maximum": 10, "default": 2}, {"default": 3}, {"default": 4})
        before = deepcopy(layers)
        result = resolve_execution_parameters(layers, {})
        self.assertEqual(result["values"], {"value": 4})
        self.assertEqual(result["sources"], {"value": "source.2"})
        self.assertEqual(resolve_execution_parameters(layers, {"value": 5})["sources"], {"value": "request"})
        self.assertEqual(layers, before)

    def test_no_fallback_for_invalid_request_or_required_missing_value(self):
        for value in (None, True, "3", 2.5, -1, 11):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                resolve_execution_parameters(self.layers({"minimum": 0, "maximum": 10, "default": 3}), {"value": value})
        with self.assertRaisesMessage(ValidationError, "缺少必需参数"):
            resolve_execution_parameters(self.layers({}), {})
        self.assertEqual(resolve_execution_parameters(self.layers({"required": False}), {})["values"], {})

    def test_unknown_request_key_rejected(self):
        with self.assertRaisesMessage(ValidationError, "未声明"):
            resolve_execution_parameters(self.layers({"default": 1}), {"unknown": 1})

    def test_fixed_values_conflict_instead_of_overwriting(self):
        with self.assertRaisesMessage(ValidationError, "固定值冲突"):
            compile_execution_parameter_contract(self.layers({"const": 1}, {"const": 2}))
        result = resolve_execution_parameters(self.layers({"const": 1}, {"default": 2}), {})
        self.assertEqual(result["values"], {"value": 1})
        with self.assertRaisesMessage(ValidationError, "固定值冲突"):
            resolve_execution_parameters(self.layers({"const": 1}), {"value": 2})
        self.assertEqual(resolve_execution_parameters(self.layers({"const": 1}), {"value": 1})["values"], {"value": 1})

    def test_zero_false_and_empty_string_are_real_values(self):
        for kind, value in (("integer", 0), ("number", 0.0), ("boolean", False), ("string", "")):
            with self.subTest(kind=kind):
                result = resolve_execution_parameters(self.layers({"type": kind, "const": value}), {})
                self.assertEqual(result["values"], {"value": value})
                if kind == "boolean":
                    with self.assertRaises(ValidationError):
                        resolve_execution_parameters(self.layers({"type": kind, "const": value}), {"value": 0})

    def test_multiple_alignments_are_all_kept(self):
        layers = self.layers(
            {"minimum": 0, "maximum": 100, "alignments": [{"modulus": 4, "remainder": 1}]},
            {"alignments": [{"modulus": 6, "remainder": 3}]},
        )
        compiled = compile_execution_parameter_contract(layers)
        self.assertEqual(len(compiled["value"]["rule"]["alignments"]), 2)
        self.assertEqual(resolve_execution_parameters(layers, {"value": 9})["values"]["value"], 9)
        # 3 satisfies the larger step only, and must not be admitted.
        with self.assertRaises(ValidationError):
            resolve_execution_parameters(layers, {"value": 3})

    def test_incompatible_alignments_fail_before_request_resolution(self):
        with self.assertRaisesMessage(ValidationError, "没有共同解"):
            compile_execution_parameter_contract(self.layers(
                {"alignments": [{"modulus": 4, "remainder": 1}]},
                {"alignments": [{"modulus": 6, "remainder": 2}]},
            ))

    def test_alignment_solution_outside_interval_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, "当前数值范围"):
            compile_execution_parameter_contract(self.layers({
                "minimum": 10, "maximum": 15,
                "alignments": [{"modulus": 17, "remainder": 5}],
            }))

    def test_fractional_alignment_has_no_binary_float_tolerance(self):
        layers = self.layers({"type": "number", "minimum": 0, "maximum": 1,
                              "alignments": [{"modulus": 0.1, "remainder": 0}]})
        self.assertEqual(resolve_execution_parameters(layers, {"value": 0.3})["values"]["value"], 0.3)
        for value in (0.31, 0.30000000000000004):
            with self.assertRaises(ValidationError):
                resolve_execution_parameters(layers, {"value": value})

    def test_mixed_numeric_type_narrows_to_integers_and_ceil_floor_bounds(self):
        contract = compile_execution_parameter_contract(self.layers(
            {"type": "number", "minimum": 1.2, "maximum": 5.8}, {},
        ))["value"]["rule"]
        self.assertEqual((contract["type"], contract["minimum"], contract["maximum"]), ("integer", 2, 5))
        with self.assertRaises(ValidationError):
            compile_execution_parameter_contract(self.layers({"type": "number", "minimum": 1.2, "maximum": 1.8}, {}))

    def test_integer_fractional_alignment_intersection(self):
        layers = self.layers(
            {"type": "number", "minimum": -5, "maximum": 5, "alignments": [{"modulus": 1.5, "remainder": 0.5}]},
            {},
        )
        self.assertEqual(resolve_execution_parameters(layers, {"value": 2})["values"]["value"], 2)
        with self.assertRaises(ValidationError):
            resolve_execution_parameters(layers, {"value": 1})

    def test_small_alignment_domains_match_brute_force(self):
        # Deterministic independent oracle, including negative and singleton ranges.
        for m in range(1, 6):
            for n in range(1, 6):
                for r in range(m):
                    for s in range(n):
                        for lower, upper in ((-9, -1), (0, 0), (2, 7)):
                            layers = self.layers({"minimum": lower, "maximum": upper, "alignments": [
                                {"modulus": m, "remainder": r}, {"modulus": n, "remainder": s},
                            ]})
                            expected = any(value % m == r and value % n == s for value in range(lower, upper + 1))
                            with self.subTest(m=m, n=n, r=r, s=s, interval=(lower, upper)):
                                if expected:
                                    compile_execution_parameter_contract(layers)
                                else:
                                    with self.assertRaises(ValidationError):
                                        compile_execution_parameter_contract(layers)

    def test_fractional_domains_match_scaled_integer_oracle(self):
        for lower, upper in ((-1, 0), (0.1, 0.3), (0.4, 0.4), (0.4, 0.5)):
            rules = {"type": "number", "minimum": lower, "maximum": upper, "alignments": [
                {"modulus": 0.2, "remainder": 0.1}, {"modulus": 0.3, "remainder": 0.2},
            ]}
            expected = any(Fraction(str(lower)) <= Fraction(x, 10) <= Fraction(str(upper)) and x % 2 == 1 and x % 3 == 2 for x in range(-10, 11))
            if expected:
                compile_execution_parameter_contract(self.layers(rules))
            else:
                with self.assertRaises(ValidationError):
                    compile_execution_parameter_contract(self.layers(rules))

    def test_enum_is_intersected_with_ranges_and_all_alignment_rules(self):
        layers = self.layers({"enum": [1, 3, 5]}, {"enum": [3, 5, 7]}, {"minimum": 4})
        self.assertEqual(compile_execution_parameter_contract(layers)["value"]["rule"]["enum"], [5])
        with self.assertRaises(ValidationError):
            compile_execution_parameter_contract(self.layers({"enum": [1]}, {"enum": [2]}))

    def test_string_length_intersection(self):
        layers = self.layers({"type": "string", "min_length": 2}, {"type": "string", "max_length": 4})
        self.assertEqual(resolve_execution_parameters(layers, {"value": "abc"})["values"]["value"], "abc")
        for value in ("a", "abcde"):
            with self.assertRaises(ValidationError):
                resolve_execution_parameters(layers, {"value": value})
        with self.assertRaises(ValidationError):
            compile_execution_parameter_contract(self.layers({"type": "string", "min_length": 5}, {"type": "string", "max_length": 4}))

    def test_malformed_contracts_and_nonfinite_values_fail_explicitly(self):
        changes = [
            {"type": {}}, {"type": "unknown"}, {"required": 1}, {"enum": []}, {"enum": [1, 1]},
            {"minimum": True}, {"minimum": 5, "maximum": 4}, {"minimum": float("nan")},
            {"alignments": [{"modulus": 0, "remainder": 0}]},
            {"alignments": [{"modulus": 2, "remainder": 2}]},
            {"default": True}, {"expression": "computed"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                compile_execution_parameter_contract(self.layers(change))
        with self.assertRaises(ValidationError):
            compile_execution_parameter_contract(self.layers({}, {"type": "string"}))

    def test_duplicate_sources_rejected(self):
        data = self.layers({}, {})
        data["value"][1]["source"] = "source.0"
        with self.assertRaises(ValidationError):
            compile_execution_parameter_contract(data)

    def test_workflow_adapter_keeps_fixed_values_and_both_alignments(self):
        rule = workflow_parameter_rule("sampling.value", {
            "type": "integer", "min": 0, "max": 100, "default": 9, "const": 9,
            "multiple_of": 3, "alignment_modulus": 4, "alignment_remainder": 1,
        }, required=True)
        self.assertEqual(rule["const"], 9)
        self.assertEqual(len(rule["alignments"]), 2)

    def test_workflow_adapter_does_not_turn_media_or_models_into_parameters(self):
        for kind in ("asset_image", "asset_video", "asset_audio", "model_artifact"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                workflow_parameter_rule("value", {"type": kind}, required=True)

    def test_workflow_enum_has_explicit_supported_values(self):
        rule = workflow_parameter_rule("value", {"type": "enum", "options": ["fast", "slow"]}, required=True)
        self.assertEqual(rule["type"], "string")
        self.assertEqual(rule["enum"], ["fast", "slow"])
        with self.assertRaises(ValidationError):
            workflow_parameter_rule("value", {"type": "enum", "options": [False, 0]}, required=True)

    def test_workflow_null_bounds_mean_no_bound_not_zero(self):
        rule = workflow_parameter_rule("value", {
            "type": "enum", "options": ["default", "cpu"], "default": "default",
            "min": None, "max": None,
        }, required=True)
        self.assertNotIn("minimum", rule)
        self.assertNotIn("maximum", rule)
        self.assertEqual(rule["enum"], ["default", "cpu"])

    def test_workflow_mixed_enum_explicit_constant_keeps_its_scalar_type(self):
        for value, kind in ((8, "integer"), ("auto", "string"), (False, "boolean")):
            with self.subTest(value=value):
                definition = {"type": "enum", "options": ["auto", 8, 10, False], "const": value, "default": value}
                before = deepcopy(definition)
                rule = workflow_parameter_rule("output.option", definition, required=True)
                self.assertEqual(rule, {"type": kind, "required": True, "enum": [value], "const": value, "default": value})
                self.assertEqual(definition, before)
                resolved = resolve_execution_parameters({"output.option": [{"source": "workflow", "rule": rule}]}, {})
                self.assertIs(type(resolved["values"]["output.option"]), type(value))

    def test_workflow_mixed_enum_default_does_not_narrow_the_domain(self):
        with self.assertRaises(ValidationError):
            workflow_parameter_rule("output.option", {"type": "enum", "options": ["auto", 8], "default": 8}, required=True)

    def test_workflow_mixed_enum_invalid_constant_and_conflicting_default_rejected(self):
        for extra in ({"const": 12}, {"const": "8"}, {"const": 8, "default": "auto"}, {"const": False}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                workflow_parameter_rule("output.option", {"type": "enum", "options": ["auto", 0, 8], **extra}, required=True)

    def test_workflow_mixed_enum_constant_does_not_hide_non_scalar_options(self):
        for invalid in (None, [], {}, float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                workflow_parameter_rule("output.option", {"type": "enum", "options": ["auto", 8, invalid], "const": 8}, required=True)

    def test_instance_adapter_preserves_locked_false(self):
        spec = ExecutionParameterSpec(key="flag", value_type="boolean", policy="locked", locked_value=False)
        self.assertIs(instance_parameter_rule(spec)["const"], False)
