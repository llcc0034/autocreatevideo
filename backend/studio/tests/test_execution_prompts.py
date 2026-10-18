from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_prompts import validate_workflow_prompt_configuration, validate_execution_prompt_channels, profile_auxiliary_purposes, normalize_manual_execution_channels


class ExecutionPromptContractTests(SimpleTestCase):
    def setUp(self):
        self.profile = SimpleNamespace(pk="profile", contract_sha256="a" * 64)
        self.contract = {
            "operation": "video.generate", "mode": "Ref2VA",
            "rules": [{"channel": "positive_prompt", "generation_mode": "passthrough"}],
            "channel_limits": {"positive_prompt": {"max_chars": 7000, "warn_at_chars": 6200}},
        }
        self.parameters = {"prompt.positive": {"type": "string", "required": True, "max_length": 5000}}
        self.bindings = [{"semantic_key": "prompt.positive", "value_type": "string", "is_required": True}]
        self.workflow = SimpleNamespace(pk="workflow", input_contract={"parameters": self.parameters}, semantic_bindings=Mock(values=Mock(side_effect=lambda *args: self.bindings)))
        self.version = SimpleNamespace(
            prompt_mode="profile", operation="video.reference_to_video", workflow_version=self.workflow,
            workflow_version_id="workflow", prompt_profile_version=self.profile, prompt_profile_version_id="profile",
        )
        for target, value in (("studio.services.execution_instances.boundary_int", 65536),
                              ("studio.services.execution_prompts.verify_prompt_profile_contract", self.contract)):
            guard = patch(target, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)

    def test_different_prompt_purpose_keeps_channel_wiring_and_intersects_limits(self):
        result = validate_workflow_prompt_configuration(self.version)
        self.assertEqual(result["prompt_operation"], "video.generate")
        self.assertEqual(result["channels"]["positive_prompt"]["semantic_key"], "prompt.positive")
        self.assertEqual(result["channels"]["positive_prompt"]["rule"]["max_length"], 5000)
        self.assertEqual(self.contract["channel_limits"]["positive_prompt"]["max_chars"], 7000)

    def test_same_name_cannot_authorize_an_unwired_channel(self):
        self.contract["operation"] = self.version.operation
        self.contract["rules"][0]["channel"] = "system_instruction"
        with self.assertRaisesMessage(ValidationError, "未接入工作流"):
            validate_workflow_prompt_configuration(self.version)

    def test_required_channel_missing_is_not_filled_from_example(self):
        self.contract["rules"] = []
        self.parameters["prompt.positive"]["default"] = "source example"
        with self.assertRaisesMessage(ValidationError, "必需通道"):
            validate_workflow_prompt_configuration(self.version)

    def test_manual_never_looks_up_a_strategy(self):
        self.version.prompt_mode = "manual"
        self.version.prompt_profile_version_id = None
        with patch("studio.services.execution_prompts.verify_prompt_profile_contract", side_effect=AssertionError("No strategy")):
            result = validate_workflow_prompt_configuration(self.version)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(result["auxiliary_purposes"], [])

    def test_none_cannot_hide_prompt_inputs_or_use_example_defaults(self):
        self.version.prompt_mode = "none"
        self.parameters["prompt.positive"]["default"] = "example"
        with self.assertRaisesMessage(ValidationError, "仍声明提示词"):
            validate_workflow_prompt_configuration(self.version)

    def test_empty_channels_do_not_imply_no_prompt_capability(self):
        self.version.prompt_mode = "none"
        self.version.prompt_profile_version_id = None
        self.parameters.clear()
        self.bindings.clear()
        with self.assertRaisesMessage(ValidationError, "未明确声明"):
            validate_workflow_prompt_configuration(self.version)
        self.assertEqual(validate_execution_prompt_channels(
            self.version, {}, source="reviewed.operation", target_label="执行目标", prompt_not_required=True,
        )["mode"], "none")

    def test_unbound_or_wrong_type_channel_rejected(self):
        for bindings in ([], [{**self.bindings[0], "value_type": "integer"}]):
            self.bindings = bindings
            with self.assertRaises(ValidationError):
                validate_workflow_prompt_configuration(self.version)

    def test_template_allowlist_is_exact_and_not_first_item_fallback(self):
        self.workflow.input_contract["prompt_template_keys"] = ["selected-template"]
        with self.assertRaisesMessage(ValidationError, "模板标识"):
            validate_workflow_prompt_configuration(self.version)
        self.contract["source_projection_config"] = {"primitive_options": {"reference_context_ir_v1": {"rendering": {"prompt_template_key": "selected-template"}}}}
        validate_workflow_prompt_configuration(self.version)

    def test_field_modes_require_separate_auxiliary_purposes(self):
        self.contract["rules"] = [
            {"channel": "positive_prompt", "generation_mode": mode}
            for mode in ("model_generated", "model_generated", "translate_only", "server_locked", "passthrough")
        ]
        self.assertEqual(profile_auxiliary_purposes(self.profile), ["generate_fields", "translate_fields"])

    def test_fixed_prompt_and_profile_length_conflict_rejected(self):
        self.parameters["prompt.positive"]["const"] = "a" * 80
        self.contract["channel_limits"]["positive_prompt"]["max_chars"] = 40
        with self.assertRaises(ValidationError):
            validate_workflow_prompt_configuration(self.version)

    def test_unused_imported_example_does_not_conflict_with_profile_output_limit(self):
        self.parameters["prompt.positive"]["default"] = "x" * 80
        self.contract["channel_limits"]["positive_prompt"]["max_chars"] = 40
        result = validate_workflow_prompt_configuration(self.version)
        rule = result["channels"]["positive_prompt"]["rule"]
        self.assertEqual(rule["max_length"], 40)
        self.assertNotIn("default", rule)

    def manual_configuration(self):
        self.version.prompt_mode = "manual"
        self.version.prompt_profile_version_id = None
        return validate_workflow_prompt_configuration(self.version)

    def test_manual_channels_keep_exact_text_without_lookup_or_ai(self):
        configuration = self.manual_configuration()
        text = "  请保持 <image 1> 中主体的外观。\n"
        with patch("studio.services.execution_prompts.verify_prompt_profile_contract", side_effect=AssertionError("No lookup")):
            result = normalize_manual_execution_channels(configuration, {"positive_prompt": text}, declared_reference_labels=["<image 1>"])
        self.assertEqual(result["values"], {"positive_prompt": text})
        self.assertEqual(result["sources"], {"positive_prompt": "request"})

    def test_manual_required_prompt_is_not_filled_from_imported_example(self):
        self.parameters["prompt.positive"]["default"] = "imported graph example"
        with self.assertRaisesMessage(ValidationError, "缺少必需参数"):
            normalize_manual_execution_channels(self.manual_configuration(), {}, declared_reference_labels=[])

    def test_manual_unknown_channels_bad_types_and_lengths_rejected(self):
        configuration = self.manual_configuration()
        for values in ({"positive_prompt": "valid", "unknown": "text"}, {"positive_prompt": True},
                       {"positive_prompt": "x" * 5001}, {"positive_prompt": "x\u200by"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                normalize_manual_execution_channels(configuration, values, declared_reference_labels=["<image 1>"])

    def test_manual_preserves_frozen_constants_but_does_not_override_them(self):
        self.parameters["prompt.positive"]["const"] = "Fixed authored text."
        configuration = self.manual_configuration()
        result = normalize_manual_execution_channels(configuration, {}, declared_reference_labels=[])
        self.assertEqual(result["values"]["positive_prompt"], "Fixed authored text.")
        with self.assertRaises(ValidationError):
            normalize_manual_execution_channels(configuration, {"positive_prompt": "different"}, declared_reference_labels=[])

    def test_none_rejects_text_and_profile_cannot_use_the_manual_shortcut(self):
        configuration = {"mode": "none", "channels": {}}
        self.assertEqual(normalize_manual_execution_channels(configuration, {}, declared_reference_labels=[])["values"], {})
        with self.assertRaises(ValidationError):
            normalize_manual_execution_channels(configuration, {"positive_prompt": "extra"}, declared_reference_labels=[])
        with self.assertRaises(ValidationError):
            normalize_manual_execution_channels({"mode": "profile", "channels": {}}, {}, declared_reference_labels=[])
        with self.assertRaises(ValidationError):
            normalize_manual_execution_channels({"mode": [], "channels": {}}, {}, declared_reference_labels=[])
