"""Media purpose choices use the model contract's labels/keys and real ports."""
from copy import deepcopy
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_input_catalog import execution_input_form_options, execution_input_purpose_options, validate_execution_input_purposes
from studio.tests.factories import conditioning_contract


class ExecutionReferenceContextFormTests(SimpleTestCase):
    def definition(self, *, selector="target_shot.core", domain="unit_context"):
        return {"configuration": {"inputs": [{"key": "renamed_context", "kind": "json", "targets": [
            {"target_kind": "profile_input", "target_key": "context.target", "is_primary": False}]}]},
            "interface": {"profile_inputs": {"context.target": {"value_domain": domain,
                "fields": [{"usage": "reference_generation_context", "selector": selector}]}}, "output": {}}}

    def test_registered_labels_and_per_unit_instructions_follow_target_not_input_name(self):
        from studio.models import PromptProfileFieldRule
        for selector, label in PromptProfileFieldRule.GenerationContextSelector.choices:
            with self.subTest(selector=selector):
                definition = self.definition(selector=selector)
                before = deepcopy(definition)
                form = execution_input_form_options(definition)["renamed_context"]
                self.assertIn(label, form["label"])
                self.assertIn("unit_key", form["description"])
                self.assertIn("不能遗漏或多填", form["description"])
                self.assertIn("不会自动读取项目", form["description"])
                self.assertIn("不代表项目已审核", form["description"])
                self.assertEqual(form["suggestions"], [])
                self.assertEqual(form["schemas"], [])
                self.assertEqual(definition, before)

    def test_unknown_selector_or_other_domain_does_not_acquire_a_context_protocol(self):
        for definition in (self.definition(selector="invented"), self.definition(domain="json")):
            form = execution_input_form_options(definition)["renamed_context"]
            self.assertEqual(form["label"], "renamed_context")
            self.assertEqual(form["description"], "")

    def test_exact_target_metadata_remains_authoritative(self):
        definition = self.definition()
        target = definition["interface"]["profile_inputs"]["context.target"]
        target.update(label="精确名称", description="精确说明", value_schema={"type": "object"})
        form = execution_input_form_options(definition)["renamed_context"]
        self.assertEqual(form["label"], "精确名称")
        self.assertEqual(form["description"], "精确说明")
        self.assertEqual(form["schemas"], [{"type": "object"}])


class ExecutionInputPurposeTests(SimpleTestCase):
    def setUp(self):
        self.mode = conditioning_contract(
            "arbitrary_mode", reference_videos=(0, 2), reference_audios=(0, 1),
            video_purposes=("reviewed_motion", "preserve_timing"), audio_purposes=("reviewed_sound",),
        )["modes"][0]
        self.resources = {"model_profile_version_id": "exact-model-version", "conditioning_mode": self.mode}
        self.targets = {"video.port": {"kind": "video"}, "audio.port": {"kind": "audio"}, "image.port": {"kind": "image"}}

    def test_keys_labels_limits_and_source_come_from_exact_model(self):
        before = deepcopy(self.resources)
        options = execution_input_purpose_options(self.resources, self.targets)
        self.assertEqual([row["value"] for row in options["video"]], ["reviewed_motion", "preserve_timing"])
        self.assertEqual(options["audio"][0]["value"], "reviewed_sound")
        self.assertEqual(options["video"][0]["maximum_items"], 2)
        self.assertIn("exact-model-version", options["video"][0]["source"])
        self.assertEqual(self.resources, before)
        options["video"][0]["label"] = "Local mutation"
        self.assertNotEqual(self.mode["dynamic_reference_topology"]["videos"]["purposes"][0]["label"], "Local mutation")

    def test_missing_model_topology_or_actual_kind_has_no_invented_option(self):
        self.assertEqual(execution_input_purpose_options({**self.resources, "conditioning_mode": None}, self.targets), {})
        options = execution_input_purpose_options(self.resources, {"image.port": {"kind": "image"}})
        self.assertEqual(options, {})
        self.mode["dynamic_reference_topology"]["videos"]["purposes"][0]["maximum_items"] = 0
        self.assertEqual([row["value"] for row in execution_input_purpose_options(self.resources, self.targets)["video"]], ["preserve_timing"])

    def test_author_restrictions_only_select_registered_kind_specific_purposes(self):
        options = execution_input_purpose_options(self.resources, self.targets)
        def spec(kind, purposes):
            return SimpleNamespace(key="media", kind=kind, semantic_contract={"allowed_purposes": purposes})
        validate_execution_input_purposes([spec("video", ["reviewed_motion"]), spec("audio", ["reviewed_sound"])], options)
        validate_execution_input_purposes([spec("image", [])], options)
        for kind, purposes in (("video", ["invented"]), ("audio", ["reviewed_motion"]), ("image", ["reviewed_sound"]), ("text", ["reviewed_motion"])):
            with self.subTest(kind=kind, purposes=purposes), self.assertRaises(ValidationError):
                validate_execution_input_purposes([spec(kind, purposes)], options)
