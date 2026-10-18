"""Only the selected input contract is preflighted; no provider certification."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.media_generation_targets import resolve_media_generation_target


class AppearanceCapabilityPreflightTests(SimpleTestCase):
    def setUp(self):
        self.definition = {"target": {"driver_type": "async_rest", "operation": "image.image_to_image"},
            "configuration": {"inputs": []}, "interface": {"output": {"kind": "image"},
                "prompt": {"mode": "manual", "channels": {"positive_prompt": {}}},
                "input_targets": {}, "profile_inputs": {}}}
        self.target = {"role": "variant", "setting_revision_id": None, "primary_version_id": str(uuid4()),
            "primary_input": {"input_key": "primary_photo", "item_key": "original-photo"},
            "difference_input": {"kind": "manual_channel", "key": "positive_prompt"}}

    def image_port(self, *, key="primary_photo"):
        self.definition["target"]["driver_type"] = "comfyui"
        self.definition["interface"]["output"] = {"contract": {"primary": {"kind": "image"}}}
        self.definition["configuration"]["inputs"] = [{"key": key, "kind": "image", "position": 0,
            "targets": [{"target_kind": "workflow_slot", "target_key": "image.port", "is_primary": True}]}]
        self.definition["interface"]["input_targets"] = {"image.port": {"target_kind": "workflow_slot", "kind": "image"}}

    def resolve(self, target, **context):
        return resolve_media_generation_target(SimpleNamespace(value=self.definition), {},
            candidate_count=1, candidate_evaluator=None, appearance_target=target, **context)

    def assert_unmapped_before_resolution(self):
        original = deepcopy(self.definition)
        for context in ({}, {"production_run_id": str(uuid4()), "production_scope": {"kind": "character", "id": str(uuid4())}}):
            with self.subTest(context=context), patch("studio.services.media_generation_targets.resolve_execution_request_facts",
                    side_effect=AssertionError("Invalid port reached input resolution")) as resolve:
                with self.assertRaises(ValidationError) as raised:
                    self.resolve(self.target, **context)
                self.assertIn("appearance_target", raised.exception.message_dict)
                self.assertIn("primary_photo", str(raised.exception))
                self.assertIn("主图片端口", str(raised.exception))
                resolve.assert_not_called()
        self.assertEqual(self.definition, original)

    def test_image_output_and_operation_name_do_not_prove_reference_input(self):
        self.assert_unmapped_before_resolution()

    def test_another_real_port_cannot_certify_the_selected_context_input(self):
        self.image_port(key="other_photo")
        self.assert_unmapped_before_resolution()

    def test_secondary_mapping_and_nonimage_ports_cannot_consume_primary(self):
        for change in ("secondary", "logical_video", "physical_video"):
            with self.subTest(change=change):
                self.image_port()
                if change == "secondary":
                    self.definition["configuration"]["inputs"][0]["targets"][0]["is_primary"] = False
                elif change == "logical_video":
                    self.definition["configuration"]["inputs"][0]["kind"] = "video"
                else:
                    self.definition["interface"]["input_targets"]["image.port"]["kind"] = "video"
                self.assert_unmapped_before_resolution()

    def test_valid_variant_and_unreferenced_primary_still_use_original_resolver(self):
        primary = {"role": "primary", "setting_revision_id": None, "primary_version_id": None, "primary_input": None}
        for target in (None, primary, self.target):
            if target is self.target:
                self.image_port()
            with self.subTest(target=target), patch("studio.services.media_generation_targets.resolve_execution_request_facts",
                    side_effect=RuntimeError("original resolver reached")) as resolve:
                with self.assertRaisesMessage(RuntimeError, "original resolver reached"):
                    self.resolve(target)
                resolve.assert_called_once()
