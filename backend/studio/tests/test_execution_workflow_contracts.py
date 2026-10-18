from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ModelDeployment, ModelProfileVersion, ProviderProfile
from studio.services.execution_workflow_contracts import workflow_execution_interface
from studio.services.json_contracts import assign_json_contract


class WorkflowExecutionInterfaceTests(TestCase):
    def setUp(self):
        self.model = ModelProfileVersion(provider=ProviderProfile(driver_type="comfyui"))
        assign_json_contract(self.model, "specification", {
            "capabilities": ["video.reference_to_video"],
            "media_constraints": {"schema_version": 1, "resolution": {"maximum_width": 1024}, "fps": {"allowed": [24]}},
        })
        self.definitions = {
            "prompt.positive": {"type": "string", "required": True},
            "output.prefix": {"type": "string", "required": True, "default": "fixture-prefix"},
            "model.diffusion.filename": {"type": "model_artifact", "required": True},
            "sampling.seed": {"type": "integer", "required": True, "min": 0, "max": 1000},
            "sampling.steps": {"type": "integer", "required": True, "default": 4, "const": 4},
            "image.width": {"type": "integer", "required": True, "min": 64, "max": 2048, "multiple_of": 16, "default": 512},
            "video.frames": {"type": "integer", "required": True, "min": 5, "max": 175, "alignment_modulus": 17, "alignment_remainder": 5},
            "video.fps": {"type": "number", "required": True, "const": 24},
        }
        self.bindings = [{"semantic_key": key, "value_type": row["type"], "is_required": row["required"]} for key, row in self.definitions.items()]
        self.workflow = SimpleNamespace(
            pk="workflow", input_contract={"parameters": self.definitions},
            semantic_bindings=Mock(values=Mock(side_effect=lambda *args: self.bindings)),
        )
        self.version = SimpleNamespace(workflow_version=self.workflow, deployment=ModelDeployment(profile_version=self.model))

    def test_registered_categories_cannot_leak_into_ordinary_parameters(self):
        result = workflow_execution_interface(self.version)
        self.assertEqual(set(result["parameters"]), {"sampling.seed", "sampling.steps", "image.width", "video.frames", "video.fps"})
        self.assertEqual(result["model_inputs"], ["model.diffusion.filename"])
        self.assertEqual(result["server_inputs"], {"output.prefix": {"type": "string", "required": True}})
        self.assertEqual(set(result["prompt_channels"]), {"positive_prompt"})
        self.assertNotIn("fixture-prefix", str(result))

    def test_all_declared_scalars_exist_without_instance_overrides(self):
        rules = workflow_execution_interface(self.version)["parameters"]
        self.assertEqual(rules["sampling.steps"]["rule"]["const"], 4)
        self.assertNotIn("default", rules["sampling.seed"]["rule"])
        self.assertEqual(rules["image.width"]["rule"]["maximum"], 1024)
        self.assertEqual(rules["video.fps"]["rule"]["enum"], [24])
        self.assertEqual(rules["video.frames"]["rule"]["alignments"], [{"modulus": 17, "remainder": 5}])

    def test_static_facts_are_preserved_without_exposing_widgets(self):
        self.workflow.input_contract["static_semantics"] = {"audio.enabled": True}
        interface = workflow_execution_interface(self.version)
        self.assertEqual(interface["static_semantics"], {"audio.enabled": True})
        self.assertNotIn("audio.enabled", interface["parameters"])
        self.assertNotIn("audio.enabled", interface["template_constants"])

    def test_static_facts_do_not_bypass_type_model_bounds_or_conflicting_binding(self):
        for values in ({"audio.enabled": "true"}, {"sampling.steps": 4}, {"invented.model.flag": True}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.workflow.input_contract["static_semantics"] = values
                workflow_execution_interface(self.version)
        self.definitions.pop("image.width")
        self.bindings[:] = [row for row in self.bindings if row["semantic_key"] != "image.width"]
        self.workflow.input_contract["static_semantics"] = {"image.width": 2048}
        with self.assertRaises(ValidationError):
            workflow_execution_interface(self.version)
        self.workflow.input_contract["static_semantics"] = {"image.width": 512}
        interface = workflow_execution_interface(self.version)
        self.assertEqual(interface["static_semantics"], {"image.width": 512})
        self.assertEqual(interface["media_semantic_keys"]["width"], "image.width")
        self.assertNotIn("image.width", interface["parameters"])

    def test_bound_required_cannot_be_overridden_by_optional_definition(self):
        self.definitions["sampling.seed"]["required"] = False
        self.assertTrue(workflow_execution_interface(self.version)["parameters"]["sampling.seed"]["rule"]["required"])

    def test_missing_definition_or_binding_is_an_error_not_graph_fallback(self):
        self.definitions["parameter.undeclared"] = {"type": "integer", "default": 3}
        with self.assertRaisesMessage(ValidationError, "未精确对应"):
            workflow_execution_interface(self.version)
        self.definitions.pop("parameter.undeclared")
        self.definitions.pop("sampling.seed")
        with self.assertRaisesMessage(ValidationError, "未精确对应"):
            workflow_execution_interface(self.version)

    def test_type_and_required_corruption_is_not_coerced(self):
        self.definitions["sampling.seed"]["type"] = "boolean"
        with self.assertRaises(ValidationError):
            workflow_execution_interface(self.version)
        self.definitions["sampling.seed"]["type"] = "integer"
        self.definitions["sampling.seed"]["required"] = "false"
        with self.assertRaises(ValidationError):
            workflow_execution_interface(self.version)

    def test_template_constants_are_preserved_but_not_editable(self):
        self.definitions["sampling.steps"]["asset_group_constant"] = True
        result = workflow_execution_interface(self.version)
        self.assertNotIn("sampling.steps", result["parameters"])
        self.assertEqual(result["template_constants"]["sampling.steps"]["rule"]["const"], 4)
        self.definitions["sampling.steps"].pop("default")
        with self.assertRaisesMessage(ValidationError, "明确提供值"):
            workflow_execution_interface(self.version)

    def test_conflicting_model_and_workflow_rules_are_rejected(self):
        self.definitions["video.fps"]["const"] = 30
        with self.assertRaises(ValidationError):
            workflow_execution_interface(self.version)

    def test_coupled_contracts_are_retained_without_mutation(self):
        contracts = {
            "resolution_strategy": {"mode": "explicit_width_height", "node_ids": ["1"]},
            "frame_policy": {"fps": 24, "minimum_frames": 5, "maximum_frames": 175, "alignment_modulus": 17, "alignment_remainder": 5},
        }
        self.workflow.input_contract.update(deepcopy(contracts))
        result = workflow_execution_interface(self.version)
        for key, value in contracts.items():
            self.assertEqual(result[key], value)
            self.assertEqual(self.workflow.input_contract[key], value)

    def test_model_names_do_not_change_semantic_projection(self):
        before = workflow_execution_interface(self.version)
        self.model.model_identifier = "a-different-display-identifier"
        self.assertEqual(workflow_execution_interface(self.version), before)

    def test_fixed_mixed_enum_projects_as_typed_constant_without_rewriting_workflow(self):
        key = "parameter.writer.option"
        definition = {"type": "enum", "options": ["auto", 8, 10], "required": True, "default": 8, "const": 8}
        self.definitions[key] = deepcopy(definition)
        self.bindings.append({"semantic_key": key, "value_type": "enum", "is_required": True})
        result = workflow_execution_interface(self.version)["parameters"][key]
        self.assertEqual(result["rule"], {"type": "integer", "required": True, "enum": [8], "const": 8})
        self.assertEqual(result["layers"][0]["source"], "workflow.workflow.parameters")
        self.assertEqual(self.definitions[key], definition)
