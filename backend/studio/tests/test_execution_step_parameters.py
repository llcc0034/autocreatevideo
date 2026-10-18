from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_parameters import resolve_execution_parameters
from studio.services.execution_step_parameters import compose_execution_step_parameters


class ExecutionStepParameterTests(SimpleTestCase):
    def setUp(self):
        guard = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        guard.start()
        self.addCleanup(guard.stop)
        self.parameters = {"some.value": [{"source": "instance", "rule": {
            "type": "integer", "required": True, "minimum": 0, "maximum": 10, "default": 2,
        }}]}
        self.resolver = Mock(side_effect=AssertionError("No source should be resolved"))

    def compose(self, policies):
        return compose_execution_step_parameters(self.parameters, policies, resolve_source=self.resolver)

    def resolve(self, result, supplied):
        return resolve_execution_parameters({key: row["layers"] for key, row in result["parameters"].items()}, supplied)["values"]

    def test_step_default_then_request_with_no_mutation_or_source_read(self):
        result = self.compose([{"key": "some.value", "policy": "default", "value": 3}])
        self.assertEqual(self.resolve(result, {}), {"some.value": 3})
        self.assertEqual(self.resolve(result, {"some.value": 4}), {"some.value": 4})
        self.assertEqual(self.parameters["some.value"][0]["rule"]["default"], 2)
        self.resolver.assert_not_called()

    def test_locked_value_does_not_allow_request_override(self):
        result = self.compose([{"key": "some.value", "policy": "locked_value", "value": 4}])
        self.assertEqual(self.resolve(result, {"some.value": 4}), {"some.value": 4})
        with self.assertRaises(ValidationError):
            self.resolve(result, {"some.value": 5})

    def test_locked_source_is_resolved_exactly_once_not_revisited_on_replay(self):
        self.resolver.side_effect = None
        self.resolver.return_value = 6
        source = {"kind": "context_field", "selector_key": "approved.numeric.input", "content_revision_id": "exact-fixture-revision"}
        result = self.compose([{"key": "some.value", "policy": "locked_source", "source": source}])
        self.assertEqual(result["resolved_source_contracts"], {"some.value": source})
        self.resolver.assert_called_once_with(source)
        self.resolver.reset_mock()
        for _ in range(2):
            self.assertEqual(self.resolve(result, {}), {"some.value": 6})
        self.resolver.assert_not_called()

    def test_source_failure_is_not_replaced_by_instance_default(self):
        self.resolver.side_effect = ValidationError("Source is not authorized")
        with self.assertRaisesMessage(ValidationError, "not authorized"):
            self.compose([{"key": "some.value", "policy": "locked_source", "source": {"kind": "context_field"}}])

    def test_all_shapes_checked_before_source_resolution(self):
        source = {"key": "some.value", "policy": "locked_source", "source": {"kind": "context_field"}}
        for invalid in ({**source, "value": 3}, {**source, "source": "arbitrary.path"},
                        {"key": "unknown", "policy": "default", "value": 3},
                        {"key": "some.value", "policy": "default"},
                        {"key": "some.value", "policy": "fallback", "value": 3}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.compose([invalid])
        with self.assertRaisesMessage(ValidationError, "重复"):
            self.compose([source, source])
        self.resolver.assert_not_called()

    def test_values_must_fit_same_hard_rules_and_type(self):
        for value in (True, "3", None, [], 11, -1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.compose([{"key": "some.value", "policy": "locked_value", "value": value}])
        self.parameters["some.value"][0]["rule"] = {"type": "integer", "required": True, "const": 2}
        with self.assertRaisesMessage(ValidationError, "固定值冲突"):
            self.compose([{"key": "some.value", "policy": "locked_value", "value": 3}])

    def test_step_cannot_hide_or_override_explicit_seed_generation(self):
        self.parameters["some.value"][0]["rule"].pop("default")
        self.parameters["some.value"][0]["rule"]["seed_policy"] = "generate_once"
        for policy in ("default", "locked_value"):
            with self.subTest(policy=policy), self.assertRaises(ValidationError):
                self.compose([{"key": "some.value", "policy": policy, "value": 3}])
        with self.assertRaises(ValidationError):
            self.compose([{"key": "some.value", "policy": "locked_source", "source": {"kind": "context_field"}}])
        self.resolver.assert_not_called()

    def test_multi_value_source_is_not_silently_reduced_to_first_item(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [2, 3]
        with self.assertRaises(ValidationError):
            self.compose([{"key": "some.value", "policy": "locked_source", "source": {"kind": "selected_output"}}])
