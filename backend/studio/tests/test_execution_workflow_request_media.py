"""Real frozen graph and local Asset resolution; no network or fake roots."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from studio.models import Asset, ModelDeployment
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_media_tokens import is_workflow_media_use_token
from studio.services.workflow_versions import workflow_version_execution_snapshot
from studio.tests import test_dynamic_ref2va_workflow as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionWorkflowMediaRequestTests(TestCase):
    _create_version = fixtures.DynamicRef2VAWorkflowTests._create_version

    def setUp(self):
        fixtures.DynamicRef2VAWorkflowTests.setUp(self)
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        setting = override_settings(MEDIA_ROOT=root.name)
        setting.enable()
        self.addCleanup(setting.disable)
        for target in ("studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient"):
            guard = patch(target, side_effect=AssertionError("No remote execution"))
            guard.start()
            self.addCleanup(guard.stop)
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188",
            "capability_ttl_seconds": 3600, "timeout_seconds": 30, "poll_interval_seconds": 1, "max_poll_seconds": 60})
        self.provider.save()
        self.deployment = ModelDeployment(key="media-request", name="Media request", profile_version=self.profile)
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.workflow = self.version
        self.workflow.status = "published"
        self.workflow.validated_deployment = self.deployment
        self.workflow.save()
        parent = create_execution_instance(key="media-request", name="Media request", description="", actor=self.user)
        version = create_execution_draft(instance_id=parent.pk, expected_revision=parent.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "video.reference_to_video", "mode": "Ref2VA",
                "workflow_version": self.workflow, "prompt_mode": "manual", "prompt_profile_version": None})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.user,
            changes={"inputs": [{"key": "references", "position": 0, "kind": "image", "required": True,
                "min_items": 1, "max_items": 2, "semantic_contract": {"schema_version": 1,
                    "allowed_purposes": [], "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"],
                    "paired_with_input_key": None, "allow_repeated_asset": True},
                "targets": [{"target_kind": "workflow_slot", "target_key": "images", "is_primary": True}]}]})
        self.definition = build_execution_definition(version.pk)
        self.parameters = initialize_execution_definition_parameters(self.definition,
            {"image.width": 320, "image.height": 512, "video.frames": 124, "video.fps": 24,
                "sampling.seed": 37, "sampling.steps": 4, "parameter.lora.strength": 1},
            policies=[], resolve_source=lambda *_: self.fail("No implicit parameter source"))
        self.snapshot = workflow_version_execution_snapshot(self.workflow)
        buffer = BytesIO()
        Image.new("RGB", (12, 18), "blue").save(buffer, format="PNG")
        self.asset = Asset(owner=self.user, kind="reference", name="A parcel", checksum=sha256(buffer.getvalue()).hexdigest())
        self.asset.file.save("parcel.png", ContentFile(buffer.getvalue()))
        self.inputs = resolve_execution_input_sources(self.definition,
            {"references": {"items": [{"kind": "asset", "item_key": key, "asset_id": str(self.asset.pk), "purpose": None}
                for key in ("second", "first")]}}, owner=self.user, subject_facts=[], max_media_bytes=65536)

    def compile(self, **changes):
        return compile_execution_transport_request(self.definition, **{
            "prepared_parameters": self.parameters, "manual_channels": {"positive_prompt": "A parcel opens."},
            "workflow_inputs": self.inputs, "workflow_snapshot": self.snapshot, "total_duration_ms": 5000,
            "idempotency_key": "frozen-media-request", **changes})

    def test_real_materials_and_order_reach_original_frozen_graph_with_repeatable_tokens(self):
        original = deepcopy(self.snapshot)
        result = self.compile()
        payload = result.value["payload"]
        media, compiled = payload["media_preparation"], payload["compiled_workflow"]
        self.assertEqual([row["item_key"] for row in media["bindings"]], ["second", "first"])
        tokens = [row["token"] for row in media["bindings"]]
        self.assertEqual(len(set(tokens)), 2)
        self.assertTrue(all(is_workflow_media_use_token(token) for token in tokens))
        self.assertEqual(compiled["graph"]["cr_images_0"]["inputs"]["image"], tokens[0])
        self.assertEqual(compiled["graph"]["cr_images_1"]["inputs"]["image"], tokens[1])
        self.assertEqual({row["asset_id"] for row in compiled["asset_bindings"]}, {str(self.asset.pk)})
        self.assertEqual(media["definition_sha256"], self.definition.sha256)
        self.assertEqual(media["resolved_inputs_sha256"], self.inputs.sha256)
        self.assertEqual(self.snapshot, original)
        self.assertEqual(result, self.compile())

    def test_manual_explicit_frames_do_not_require_a_fabricated_shot_duration(self):
        expected = self.compile()
        actual = self.compile(total_duration_ms=None)
        self.assertEqual(actual.value["payload"]["compiled_workflow"], expected.value["payload"]["compiled_workflow"])
        for value in (True, -1, "5000"):
            with self.assertRaises(ValidationError):
                self.compile(total_duration_ms=value)

    def test_missing_or_changed_material_facts_cannot_use_imported_examples(self):
        with self.assertRaisesMessage(ValidationError, "已声明素材"):
            self.compile(workflow_inputs=None)
        bad = deepcopy(self.inputs.value)
        bad["workflow_slots"]["images"][0]["target_port_ref"]["input_name"] = "undeclared-port"
        with self.assertRaises(ValidationError):
            self.compile(workflow_inputs=json_contract(bad))
        bad = deepcopy(self.inputs.value)
        bad["logical_values"]["references"][0]["asset_id"] = "unrecorded-asset"
        with self.assertRaises(ValidationError):
            self.compile(workflow_inputs=json_contract(bad))

    def test_manual_labels_come_from_actual_uses_and_extra_markers_are_rejected(self):
        prompt = {"positive_prompt": "<Picture 1> beside <Picture 2>."}
        result = self.compile(manual_channels=prompt)
        self.assertEqual(result.value["payload"]["media_preparation"]["declared_reference_labels"], ["<Picture 1>", "<Picture 2>"])
        self.assertEqual(result.value["prompt_channels"]["values"]["positive_prompt"], prompt["positive_prompt"])
        with self.assertRaises(ValidationError):
            self.compile(manual_channels={"positive_prompt": "<Picture 3>"})
