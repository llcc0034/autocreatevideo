"""Frozen interface -> step policies -> one seed -> coupled media constraints."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters, resolve_execution_definition_parameters, verify_prepared_execution_parameters
from studio.services.execution_parameters import compile_execution_parameter_contract
from studio.services.json_contracts import json_contract


class ExecutionDefinitionParameterTests(TestCase):
    def setUp(self):
        self.layers = {
            "width": [{"source": "workflow.width", "rule": {"type": "integer", "required": True, "minimum": 16, "maximum": 1024}}],
            "height": [{"source": "workflow.height", "rule": {"type": "integer", "required": True, "minimum": 16, "maximum": 1024}}],
            "randomness": [{"source": "instance.randomness", "rule": {"type": "integer", "required": True, "minimum": 5, "maximum": 105,
                "alignments": [{"modulus": 10, "remainder": 5}], "seed_policy": "generate_once"}}],
        }
        self.interface = {"parameters": compile_execution_parameter_contract(self.layers),
            "static_semantics": {"audio.enabled": False, "rate": 24, "seconds": 1},
            "output": {"adapter": {"id": "frozen-projector"}, "contract": {"schema_version": 1, "primary": {"node_id": "result", "kind": "video", "required": True}}},
            "media_constraints": {"schema_version": 1, "resolution": {"maximum_short_side": 768}, "fps": {"allowed": [24]}, "duration_ms": {}},
            "media_semantic_keys": {"width": "width", "height": "height", "fps": "rate", "duration_seconds": "seconds"}}

    def definition(self):
        # Pure interface fixture; registry integrity has its own real-model tests.
        return json_contract({"interface": self.interface}, max_bytes=65536)

    @staticmethod
    def no_source(source):
        raise AssertionError(f"Unexpected source lookup: {source}")

    def initial(self, supplied=None, **kwargs):
        return initialize_execution_definition_parameters(self.definition(), supplied if supplied is not None else {"width": 1024, "height": 576},
            policies=kwargs.pop("policies", []), resolve_source=kwargs.pop("resolve_source", self.no_source), **kwargs)

    def test_single_draw_then_replay_retains_static_facts_and_exact_seed(self):
        with patch("studio.services.execution_parameter_preparation.randbelow", return_value=3) as draw:
            result = self.initial()
        draw.assert_called_once_with(11)
        self.assertEqual(result["values"]["randomness"], 35)
        self.assertIs(result["values"]["audio.enabled"], False)
        self.assertEqual(result["values"]["rate"], 24)
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("replay must not draw")):
            replay = resolve_execution_definition_parameters(self.definition(), {"width": 1024, "height": 576},
                policies=[], resolve_source=self.no_source, generated_values=result["generated_values"])
        self.assertEqual(replay, result)
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("saved request must not draw")):
            self.assertEqual(verify_prepared_execution_parameters(self.definition(), result), result)

    def test_joint_failure_is_checked_before_any_seed_draw(self):
        with patch("studio.services.execution_parameter_preparation.randbelow") as draw:
            with self.assertRaisesMessage(ValidationError, "短边"):
                self.initial({"width": 1024, "height": 1024})
        draw.assert_not_called()

    def add_default_seed(self, **rule):
        self.layers["seed"] = [{"source": "model.seed", "rule": {
            "type": "integer", "required": False, "minimum": 0, "maximum": 9, "default": 7, **rule}}]
        self.interface["parameters"] = compile_execution_parameter_contract(self.layers)

    def test_new_candidate_overrides_default_once_and_saved_result_replays(self):
        self.add_default_seed()
        before = deepcopy(self.interface)
        with patch("studio.services.execution_parameter_preparation.randbelow", return_value=0) as draw:
            result = self.initial(new_candidate_seed_exclusions={"seed": [0, 7], "randomness": [5]})
        self.assertEqual(draw.call_count, 2)
        self.assertEqual(result["values"]["seed"], 1)
        self.assertEqual(result["values"]["randomness"], 15)
        self.assertEqual(result["sources"]["seed"], "preparation.generated")
        self.assertEqual(result["contracts"]["seed"]["layers"][-1]["source"], "request.new_candidate_seed.seed")
        self.assertEqual(self.interface, before)
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("no redraw")):
            self.assertEqual(verify_prepared_execution_parameters(self.definition(), result), result)
        self.assertEqual(self.initial()["values"]["seed"], 7)  # Initial requests retain the declared default.

    def test_new_candidate_does_not_bypass_fixed_const_or_locked_step(self):
        for rule, policies in (({"seed_policy": "fixed"}, []), ({"const": 7}, []),
                ({}, [{"key": "seed", "policy": "locked_value", "value": 7}])):
            self.add_default_seed(**rule)
            with self.subTest(rule=rule, policies=policies), patch("studio.services.execution_parameter_preparation.randbelow") as draw:
                with self.assertRaises(ValidationError):
                    self.initial(policies=policies, new_candidate_seed_exclusions={"seed": [7], "randomness": []})
                draw.assert_not_called()

    def test_new_candidate_layer_preserves_step_default_and_rejects_tampering(self):
        self.add_default_seed()
        result = self.initial(policies=[{"key": "seed", "policy": "default", "value": 8}],
            new_candidate_seed_exclusions={"seed": [7, 8], "randomness": []})
        self.assertEqual(verify_prepared_execution_parameters(self.definition(), result), result)
        for key in ("seed", "width"):
            tampered = deepcopy(result)
            tampered["contracts"][key]["layers"].append({"source": f"request.new_candidate_seed.{key}",
                "rule": {"type": "integer", "required": False, "seed_policy": "generate_once"}})
            with self.subTest(key=key), self.assertRaises(ValidationError):
                verify_prepared_execution_parameters(self.definition(), tampered)

    def test_new_candidate_still_validates_media_before_drawing(self):
        self.add_default_seed()
        with patch("studio.services.execution_parameter_preparation.randbelow") as draw:
            with self.assertRaisesMessage(ValidationError, "短边"):
                self.initial({"width": 1024, "height": 1024},
                    new_candidate_seed_exclusions={"seed": [7], "randomness": []})
        draw.assert_not_called()

    def test_out_of_range_resolution_does_not_use_workflow_example_dimensions(self):
        for key, value in (("width", 352), ("height", 608)):
            self.layers[key][0]["rule"].update(minimum=value, maximum=value, default=value)
        self.interface["parameters"] = compile_execution_parameter_contract(self.layers)
        before = deepcopy(self.interface)
        with patch("studio.services.execution_parameter_preparation.randbelow") as draw:
            with self.assertRaises(ValidationError):
                self.initial({"width": 768, "height": 1344})
        draw.assert_not_called()
        self.assertEqual(self.interface, before)

        # Declared defaults apply only when a request omits the controls.
        result = self.initial({})
        self.assertEqual((result["values"]["width"], result["values"]["height"]), (352, 608))
        self.assertEqual(result["sources"]["width"], "workflow.width")

    def test_supported_resolution_preserves_request_and_its_source(self):
        for key in ("width", "height"):
            self.layers[key][0]["rule"].update(maximum=1536, default=512)
        self.interface["parameters"] = compile_execution_parameter_contract(self.layers)
        result = self.initial({"width": 768, "height": 1344})
        self.assertEqual((result["values"]["width"], result["values"]["height"]), (768, 1344))
        self.assertEqual(result["sources"]["width"], "request")
        self.assertEqual(result["sources"]["height"], "request")

    def test_static_values_cannot_be_overridden_or_relabelled_as_controls(self):
        for supplied in ({"width": 1024, "height": 576, "audio.enabled": True}, {"width": 1024, "height": 576, "rate": 24}):
            with self.subTest(supplied=supplied), self.assertRaises(ValidationError):
                self.initial(supplied)
        self.interface["static_semantics"]["width"] = 1024
        with self.assertRaisesMessage(ValidationError, "重名"):
            self.initial()

    def test_step_value_and_authorized_locked_source_keep_their_declared_origins(self):
        source = {"kind": "context_field", "selector_key": "declared.height"}
        policies = [{"key": "height", "policy": "locked_source", "source": source}, {"key": "width", "policy": "default", "value": 768}]
        calls = []
        def resolve(value):
            calls.append(value)
            return 512
        result = self.initial({}, policies=policies, resolve_source=resolve)
        self.assertEqual(calls, [source])
        self.assertEqual(result["values"]["height"], 512)
        self.assertEqual(result["sources"]["height"], "step.parameter_policies.height")
        self.assertEqual(result["resolved_source_contracts"], {"height": source})
        self.assertEqual(verify_prepared_execution_parameters(self.definition(), result), result)
        with self.assertRaises(ValidationError):
            self.initial({"height": 576}, policies=policies, resolve_source=resolve)

    def test_saved_parameters_reject_missing_values_or_changed_provenance(self):
        result = self.initial()
        for key, change in (("values", {**result["values"], "randomness": 999}),
                ("values", {**result["values"], "audio.enabled": True}),
                ("sources", {**result["sources"], "width": "workflow.width"}),
                ("generated_values", {}), ("contracts", {}), ("resolved_source_contracts", {"width": {}})):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                verify_prepared_execution_parameters(self.definition(), {**result, key: change})
        broken = deepcopy(result)
        broken["contracts"]["width"]["layers"][0]["rule"]["maximum"] = 2048
        with self.assertRaises(ValidationError):
            verify_prepared_execution_parameters(self.definition(), broken)

    def test_recomputed_layer_mismatch_or_checksum_tamper_fails(self):
        self.interface["parameters"]["width"]["rule"]["maximum"] = 2048
        with self.assertRaisesMessage(ValidationError, "来源层"):
            self.initial()
        self.interface["parameters"] = compile_execution_parameter_contract(self.layers)
        definition = self.definition()
        definition.value["interface"]["static_semantics"]["rate"] = 30
        with self.assertRaises(ValidationError):
            initialize_execution_definition_parameters(definition, {"width": 1024, "height": 576}, policies=[], resolve_source=self.no_source)

    def test_api_shape_and_frozen_static_media_constraints_are_not_skipped(self):
        self.interface["output"] = {"adapter": "async_rest", "kind": "video"}
        self.initial()
        self.interface["static_semantics"]["rate"] = 25
        with self.assertRaises(ValidationError):
            self.initial()
        self.interface["output"] = {"adapter": "async_rest"}
        with self.assertRaises(ValidationError):
            self.initial()
