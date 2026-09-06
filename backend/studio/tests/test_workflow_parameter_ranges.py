from copy import deepcopy
from django.test import TestCase, SimpleTestCase
from studio.services.workflow_parameter_ranges import (
    validate_parameter_range, narrow_parameter_range, apply_projector_parameter_ranges, ParameterRangeError,
)
from studio.services.workflow_semantics import _validate_value, WorkflowSemanticError


class ParameterRangeTests(SimpleTestCase):
    def test_numeric_ranges_and_decimal_steps_are_enforced(self):
        base = {"type": "number", "min": -10, "max": 10, "default": 1}
        frozen = narrow_parameter_range("lora", base, {"min": 0, "max": 1, "multiple_of": 0.05, "default": 0.3})
        self.assertEqual(_validate_value("lora", 0.3, frozen, "number"), 0.3)
        for value in (-0.05, 1.05, 0.32, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(WorkflowSemanticError):
                _validate_value("lora", value, frozen, "number")
        self.assertEqual(base["min"], -10)

    def test_cannot_widen_node_bounds_or_step(self):
        base = {"type": "integer", "min": 0, "max": 100, "multiple_of": 2, "default": 20}
        for constraints in ({"min": -1}, {"max": 101}, {"multiple_of": 3}, {"min": 40}, {"multiple_of": 0.5}, {"max": -2}):
            with self.subTest(constraints=constraints), self.assertRaises(ParameterRangeError):
                narrow_parameter_range("steps", base, constraints)
        self.assertEqual(narrow_parameter_range("steps", base, {"min": 40, "default": 40})["default"], 40)

    def test_integer_alignment_is_preserved_and_can_only_narrow(self):
        base = {"type": "integer", "min": 5, "max": 3600, "default": 22, "alignment_modulus": 17, "alignment_remainder": 5}
        result = narrow_parameter_range("video.frames", base, {"min": 22, "max": 90})
        _validate_value("video.frames", 39, result, "integer")
        for value in (5, 40, 107):
            with self.assertRaises(WorkflowSemanticError):
                _validate_value("video.frames", value, result, "integer")
        for constraints in ({"alignment_modulus": 1, "alignment_remainder": 0}, {"default": 40}):
            with self.assertRaises(ParameterRangeError):
                narrow_parameter_range("video.frames", base, constraints)

    def test_invalid_fields_and_non_numeric_targets_fail(self):
        for constraints in ({"unknown": 1}, {"min": True}, {"max": float("nan")}, {"multiple_of": 0}, {"alignment_modulus": 17}, {"min": 2, "max": 1}, {"default": 2**53}):
            with self.subTest(constraints=constraints), self.assertRaises(ParameterRangeError):
                validate_parameter_range(constraints)
        with self.assertRaises(ParameterRangeError):
            narrow_parameter_range("prompt", {"type": "string", "default": "x"}, {"min": 1})

    def test_failure_does_not_partially_rewrite_source_contract(self):
        contract = {"parameters": {"cfg": {"type": "number", "min": 0, "max": 10, "default": 1}}}
        before = deepcopy(contract)
        with self.assertRaises(ParameterRangeError):
            apply_projector_parameter_ranges(contract, [{"semantic_key": "cfg", "constraints": {"max": 2}}, {"semantic_key": "missing", "constraints": {"min": 0}}])
        self.assertEqual(contract, before)


class ParameterRangeProjectionTests(TestCase):
    def test_projector_definition_and_generic_projection(self):
        from studio.services.workflow_projectors import current_projector_catalog, validate_workflow_projector_definition, WorkflowProjectorError
        from studio.services.comfyui_workflow_imports import _apply_generic_projector_configuration
        projector = deepcopy(next(row for row in current_projector_catalog() if row.key == "generic.comfy.fixed"))
        definition = projector.definition
        definition["node_roles"] = {"sampler": {"node_type": "Sampler", "cardinality": "one"}}
        policy = {"semantic_key": "sampling.cfg_scale", "node_role": "sampler", "input_name": "cfg", "policy": "runtime", "value": None, "constraints": {"min": 1, "max": 5, "multiple_of": 0.5, "default": 2}}
        definition["parameter_policies"] = [policy]
        validate_workflow_projector_definition(definition, engine_key=projector.engine_key, engine_version=1)
        graph = {"1": {"class_type": "Sampler", "inputs": {"cfg": 2}}}
        source = {"parameters": {"sampling.cfg_scale": {"type": "number", "min": 0, "max": 100, "default": 2}}}
        bindings = [{"semantic_key": "sampling.cfg_scale", "node_id": "1", "input_name": "cfg", "value_type": "number", "is_required": True}]
        args = dict(projector=projector, graph=graph, contract=source, bindings=bindings, requirements=[], blockers=[], configurable_parameters=[])
        result = _apply_generic_projector_configuration(**args)
        self.assertFalse(result[3])
        self.assertEqual(result[0]["parameters"]["sampling.cfg_scale"]["max"], 5)
        policy["constraints"]["max"] = 200
        self.assertEqual(_apply_generic_projector_configuration(**args)[3][0]["code"], "workflow_projector_parameter_range_invalid")
        policy["policy"] = "fixed"
        with self.assertRaises(WorkflowProjectorError):
            validate_workflow_projector_definition(definition, engine_key=projector.engine_key, engine_version=1)

    def test_model_editor_catalog_is_same_validator_contract(self):
        from studio.services.media_capabilities import conditioning_contract_editor_options, normalize_conditioning_contract
        from studio.services.model_profile_options import model_profile_registry_options
        options = conditioning_contract_editor_options()
        mode = deepcopy(options["mode_template"])
        mode.update(key="multi_image", label="多图", input_kind="classified_references")
        mode["inputs"]["entity_images"] = {"minimum_items": 1, "maximum_items": 3}
        mode["inputs"]["total_images"] = {"minimum_items": 1, "maximum_items": 3}
        value = {"schema_version": options["schema_version"], "adapter_key": options["adapter_key"], "modes": [mode]}
        self.assertEqual(normalize_conditioning_contract(value)["modes"][0]["inputs"]["total_images"]["maximum_items"], 3)
        self.assertEqual(model_profile_registry_options()["conditioning_contract"], options)
