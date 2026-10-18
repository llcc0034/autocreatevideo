from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ExecutionParameterSpec, ModelDeployment, ModelProfileVersion, ProviderProfile
from studio.services.execution_api_contracts import (
    async_execution_interface, validate_async_execution_parameters, validate_async_prompt_configuration,
)
from studio.services.async_media_schema import api_parameter_rule
from studio.services.execution_media_parameters import model_media_parameter_layers
from studio.services.execution_parameters import resolve_execution_parameters
from studio.services.json_contracts import assign_json_contract


class ExecutionApiContractTests(TestCase):
    def setUp(self):
        self.provider = ProviderProfile(key="api-contract", driver_type="async_rest")
        self.profile = ModelProfileVersion(
            provider=self.provider, model_identifier="configured-model", modality="video", status="published",
        )
        self.properties = {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "negative_prompt": {"type": "string", "maxLength": 400},
            "model": {"type": "string", "enum": ["configured-model"]},
            "width": {"type": "integer", "minimum": 64, "maximum": 2048},
            "height": {"type": "integer", "minimum": 64, "maximum": 2048},
            "fps": {"type": "integer", "minimum": 1, "maximum": 120},
            "duration_seconds": {"type": "number", "minimum": 0.1, "maximum": 60},
            "seed": {"type": "integer", "minimum": 0, "maximum": 1000},
            "audio": {"type": "boolean"},
        }
        self.specification = {
            "schema_version": 1, "capabilities": ["video.text_to_video"],
            "media_constraints": {
                "schema_version": 1,
                "resolution": {"minimum_width": 128, "maximum_width": 1024, "width_multiple": 16, "maximum_short_side": 768},
                "fps": {"allowed": [24, 30]}, "duration_ms": {"minimum": 500, "maximum": 30000},
            },
            "async_media": {"schema_version": 1, "operations": {
                "video.text_to_video": {
                    "output_kind": "video",
                    "request_schema": {"type": "object", "additionalProperties": False,
                                       "required": ["prompt", "model", "width", "height"], "properties": self.properties},
                    "smoke_inputs": {"prompt": "fixture only", "model": "configured-model", "width": 512, "height": 512},
                },
            }},
        }
        self.freeze()
        self.version = SimpleNamespace(
            pk="execution-version", deployment=ModelDeployment(profile_version=self.profile),
            operation="video.text_to_video", workflow_version_id=None, parameters=Mock(all=Mock(return_value=[])),
        )
        network = patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No external requests"))
        network.start()
        self.addCleanup(network.stop)

    def freeze(self):
        assign_json_contract(self.profile, "specification", deepcopy(self.specification))

    def test_exact_interface_excludes_fixture_and_protected_fields(self):
        interface = async_execution_interface(self.version)
        self.assertEqual(interface["input_targets"], {})
        self.assertEqual(interface["operation"], "video.text_to_video")
        self.assertEqual(interface["prompt_channels"]["positive_prompt"]["semantic_key"], "prompt")
        self.assertEqual(interface["prompt_channels"]["negative_prompt"]["rule"]["required"], False)
        self.assertNotIn("prompt", interface["parameters"])
        self.assertNotIn("model", interface["parameters"])
        self.assertNotIn("smoke_inputs", interface)
        self.assertNotIn("default", interface["parameters"]["width"]["rule"])
        self.assertEqual(interface["media_constraints"]["resolution"]["maximum_short_side"], 768)

    def test_model_and_api_restrictions_are_intersected_and_sourced(self):
        result = async_execution_interface(self.version)["parameters"]
        self.assertEqual(result["width"]["rule"], {
            "type": "integer", "required": True, "minimum": 128, "maximum": 1024,
            "alignments": [{"modulus": 16, "remainder": 0}],
        })
        self.assertEqual(result["fps"]["rule"]["enum"], [24, 30])
        self.assertEqual(result["duration_seconds"]["rule"]["minimum"], 0.5)
        self.assertEqual(result["duration_seconds"]["rule"]["maximum"], 30)
        self.assertEqual(len(result["width"]["layers"]), 2)

    def test_smoke_values_do_not_complete_a_missing_runtime_parameter(self):
        params = async_execution_interface(self.version)["parameters"]
        with self.assertRaisesMessage(ValidationError, "缺少必需参数"):
            resolve_execution_parameters({key: row["layers"] for key, row in params.items()}, {})

    def test_model_selector_cannot_disagree_with_the_exact_profile(self):
        self.properties["model"]["enum"] = ["different-model"]
        self.specification["async_media"]["operations"]["video.text_to_video"]["smoke_inputs"]["model"] = "different-model"
        self.freeze()
        with self.assertRaises(ValidationError):
            async_execution_interface(self.version)

    def test_operation_is_not_inferred_or_aliased(self):
        self.version.operation = "video.generate"
        with self.assertRaises(ValidationError):
            async_execution_interface(self.version)

    def test_wrong_transport_or_workflow_combination_is_rejected(self):
        self.provider.driver_type = "openai_compatible"
        with self.assertRaises(ValidationError):
            async_execution_interface(self.version)
        self.provider.driver_type = "async_rest"
        self.version.workflow_version_id = "workflow"
        with self.assertRaises(ValidationError):
            async_execution_interface(self.version)

    def test_corrupt_model_contract_is_not_projected(self):
        self.profile.specification["capabilities"] = []
        with self.assertRaises(ValidationError):
            async_execution_interface(self.version)

    def test_disjoint_model_and_api_domain_is_rejected(self):
        self.properties["fps"]["enum"] = [60]
        self.freeze()
        with self.assertRaisesMessage(ValidationError, "没有交集"):
            async_execution_interface(self.version)

    def test_schema_unknown_fields_bad_types_and_impossible_ranges_rejected(self):
        for rule in ({"type": "string", "default": "implicit"},
                     {"type": "integer", "minimum": True},
                     {"type": "number", "minimum": float("nan")},
                     {"type": "integer", "minimum": 5, "maximum": 2},
                     {"type": "array"}):
            with self.subTest(rule=rule), self.assertRaises(ValidationError):
                api_parameter_rule("value", rule, required=True)

    def test_instance_restriction_is_applied_without_dropping_other_parameters(self):
        self.version.parameters.all.return_value = [ExecutionParameterSpec(
            key="width", value_type="integer", policy="overridable", minimum=256, maximum=768, step=32, default_value=512,
        )]
        result = validate_async_execution_parameters(self.version)
        self.assertEqual(result["width"]["rule"]["minimum"], 256)
        self.assertEqual(result["width"]["rule"]["default"], 512)
        self.assertIn("height", result)
        self.assertEqual(len(result["width"]["rule"]["alignments"]), 2)

    def test_protected_and_undeclared_parameters_cannot_be_authored(self):
        for key in ("model", "prompt", "negative_prompt", "lora_strength"):
            self.version.parameters.all.return_value = [ExecutionParameterSpec(key=key, value_type="string", policy="overridable")]
            with self.subTest(key=key), self.assertRaisesMessage(ValidationError, "不是该执行目标的可配置参数"):
                validate_async_execution_parameters(self.version)

    def test_instance_cannot_widen_model_or_api_bounds(self):
        self.version.parameters.all.return_value = [ExecutionParameterSpec(
            key="width", value_type="integer", policy="overridable", maximum=1500,
        )]
        with self.assertRaisesMessage(ValidationError, "不能放宽"):
            validate_async_execution_parameters(self.version)

    def test_no_media_constraint_does_not_manufacture_a_default(self):
        self.specification.pop("media_constraints")
        self.freeze()
        self.assertEqual(model_media_parameter_layers(self.profile, {"width": "image.width"})["layers"], {})
        with self.assertRaises(ValidationError):
            model_media_parameter_layers(self.profile, {"width": "shared", "height": "shared"})

    def test_async_manual_channels_do_not_use_a_hidden_strategy(self):
        self.version.prompt_mode = "manual"
        self.version.prompt_profile_version_id = None
        with patch("studio.services.execution_prompts.verify_prompt_profile_contract", side_effect=AssertionError("No hidden strategy")):
            result = validate_async_prompt_configuration(self.version)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(result["channels"]["positive_prompt"]["rule"]["max_length"], 2000)

    def test_async_profile_preserves_its_own_purpose_and_intersects_channels(self):
        self.version.prompt_mode = "profile"
        self.version.prompt_profile_version_id = "profile"
        self.version.prompt_profile_version = SimpleNamespace(pk="profile", contract_sha256="a" * 64)
        contract = {"operation": "video.generate", "mode": "", "rules": [{"channel": "positive_prompt", "generation_mode": "passthrough"}],
                    "channel_limits": {"positive_prompt": {"max_chars": 1000}}}
        with patch("studio.services.execution_prompts.verify_prompt_profile_contract", return_value=contract):
            result = validate_async_prompt_configuration(self.version)
            self.assertEqual(result["prompt_operation"], "video.generate")
            self.assertEqual(result["channels"]["positive_prompt"]["semantic_key"], "prompt")
            self.assertEqual(result["channels"]["positive_prompt"]["rule"]["max_length"], 1000)
            contract["rules"][0]["channel"] = "system_instruction"
            with self.assertRaisesMessage(ValidationError, "未接入API"):
                validate_async_prompt_configuration(self.version)

    def test_async_none_cannot_skip_required_prompt_schema(self):
        self.version.prompt_mode = "none"
        with self.assertRaisesMessage(ValidationError, "仍声明提示词"):
            validate_async_prompt_configuration(self.version)
