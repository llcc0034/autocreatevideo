"""Exact image-instance API, three real image slots and the original worker.

The graph is an isolated configuration fixture. No live Comfy server or GPU is
used; a local PNG stands in for the peer result and is probed without mocks.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob
from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.tests import test_execution_definitions as fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionComfyImageAssetTests(TestCase):
    def setUp(self):
        fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        # Register the local fixture's image-compositing node through the
        # existing server extension setting; do not weaken the node validator.
        self.enterContext(patch.dict(os.environ, {"COMFYUI_EXTRA_ALLOWED_NODE_TYPES": "ImageBlend"}))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(override_settings(MEDIA_ROOT=str(self.root / "media"), COMFYUI_OUTPUT_DIR=str(self.root / "output")))
        assign_json_contract(self.profile, "specification", {**self.profile.specification, "capabilities": ["image.image_to_image"]})
        self.profile.save()
        snapshot = workflow_version_execution_snapshot(self.workflow)
        graph = deepcopy(snapshot["graph"])
        del graph["4"]
        graph.update({str(10 + index): {"class_type": "LoadImage", "inputs": {"image": f"import-example-{index}.png"}}
            for index in range(3)})
        graph.update({
            "13": {"class_type": "ImageBlend", "inputs": {"image1": ["10", 0], "image2": ["11", 0], "blend_factor": 0.5, "blend_mode": "normal"}},
            "14": {"class_type": "ImageBlend", "inputs": {"image1": ["13", 0], "image2": ["12", 0], "blend_factor": 0.5, "blend_mode": "normal"}},
            "15": {"class_type": "ImageScale", "inputs": {"image": ["14", 0], "upscale_method": "nearest-exact", "width": 512, "height": 512, "crop": "disabled"}},
            "16": {"class_type": "VAEEncode", "inputs": {"pixels": ["15", 0], "vae": ["1", 2]}},
        })
        graph["5"]["inputs"]["latent_image"] = ["16", 0]
        bindings = deepcopy(snapshot["semantic_bindings"])
        for binding in bindings:
            if binding["semantic_key"] in {"image.width", "image.height"}:
                binding["node_id"] = "15"
        parameter_rules = {**snapshot["input_contract"]["parameters"],
            "sampling.cfg_scale": {"type": "number", "required": True, "min": 0, "max": 20},
            "sampling.scheduler": {"type": "enum", "required": True, "options": ["normal", "karras"]}}
        bindings.extend({"semantic_key": key, "node_id": "5", "input_name": name, "value_type": kind}
            for key, name, kind in (("sampling.cfg_scale", "cfg", "number"), ("sampling.scheduler", "scheduler", "enum")))
        self.assets, logical_inputs = [], []
        for index in range(3):
            semantic = f"asset.image.source_{index + 1}"
            parameter_rules[semantic] = {"type": "asset_image", "required": True, "input_slot": index + 1,
                "asset_category": "image", "min_items": 1, "max_items": 1}
            bindings.append({"semantic_key": semantic, "node_id": str(10 + index), "input_name": "image", "value_type": "asset_image"})
            buffer = BytesIO()
            Image.new("RGB", (16, 24), (30 * index, 40, 50)).save(buffer, format="PNG")
            asset = Asset(owner=self.user, kind="reference", name=f"Input {index + 1}", checksum=sha256(buffer.getvalue()).hexdigest())
            asset.file.save(f"input-{index + 1}.png", ContentFile(buffer.getvalue()))
            self.assets.append(asset)
            logical_inputs.append({"key": f"reference_{index + 1}", "position": index, "kind": "image", "required": True,
                "min_items": 1, "max_items": 1, "semantic_contract": {"schema_version": 1, "allowed_purposes": [],
                    "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "workflow_slot", "target_key": semantic, "is_primary": True}]})
        workflow = import_workflow_version(template=self.workflow.template, graph=graph,
            input_contract={**snapshot["input_contract"], "capability": "image.image_to_image", "parameters": parameter_rules,
                "resolution_strategy": {"mode": "explicit_width_height", "node_ids": ["15"]}},
            semantic_bindings=bindings, model_requirements=snapshot["model_requirements"], provider=self.provider, model_profile_version=self.profile)
        workflow.status, workflow.validated_deployment = "published", self.deployment
        workflow.save()
        workflow.template.is_active = True
        workflow.template.save(update_fields=["is_active"])
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.user,
            changes={"workflow_version": workflow, "operation": "image.image_to_image", "inputs": logical_inputs})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.inputs = {f"reference_{index + 1}": {"items": [{"kind": "asset", "item_key": f"image_{index + 1}", "asset_id": str(asset.pk), "purpose": None}]}
            for index, asset in enumerate(self.assets)}

    def test_formal_image_job_freezes_and_stages_every_declared_slot_and_scalar(self):
        original_graph = deepcopy(self.version.workflow_version.graph)
        response = self.client.post("/api/v1/asset-generation-jobs/", {
            "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "job_type": "asset", "media_type": "image", "image_category": "reference", "name": "Three input fixture",
            "parameters": {"image.width": 16, "image.height": 24, "sampling.seed": 42, "sampling.cfg_scale": 6.5, "sampling.scheduler": "karras"},
            "inputs": self.inputs, "manual_channels": {"positive_prompt": "Three supplied visual references.", "negative_prompt": ""},
            "subject_facts": [], "source_language": "", "target_language": "", "field_values": [],
        }, format="json", HTTP_IDEMPOTENCY_KEY="native-multi-image")
        self.assertEqual(response.status_code, 202, response.data)
        job = AssetGenerationJob.objects.get(pk=response.data["id"])
        prep = freeze(job, self.user)
        peer, uploads = Mock(), RecordingUploadClient()
        with patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations), \
                patch("studio.services.asset_generation.driver_for", return_value=peer), \
                patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=uploads):
            dispatch_asset_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
            peer.invoke.return_value = {"request_id": str(prep.pk)}
            submitted = process_comfy_asset_job(claim_next_comfy_asset_job("image-fixture-worker"))
            self.assertEqual(submitted.status, "submitted", submitted.error_message)
            submitted_graph = peer.invoke.call_args.args[2]["workflow"]
            self.assertEqual(len(uploads.uploads), 3)
            for index in range(3):
                self.assertEqual(submitted_graph[str(10 + index)]["inputs"]["image"], uploads.uploads[index])
            self.assertEqual({key: submitted_graph["5"]["inputs"][key] for key in ("seed", "cfg", "scheduler")},
                {"seed": 42, "cfg": 6.5, "scheduler": "karras"})
            compiled = prep.contract.prepared_request["transport"]["payload"]["compiled_workflow"]
            prefix = PurePosixPath(compiled["resolved_semantics"]["output.prefix"])
            output = self.root / "output" / Path(*prefix.parent.parts) / f"{prefix.name}_00001.png"
            self.assertTrue(output.resolve().is_relative_to(self.root))
            output.parent.mkdir(parents=True)
            Image.new("RGB", (16, 24), "blue").save(output)
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            peer.invoke.return_value = {"status": "succeeded"}
            self.assertEqual(process_comfy_asset_job(claim_next_comfy_asset_job("image-fixture-worker")).status, "running")
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            peer.invoke.return_value = {"outputs": [{"node_id": "7", "filename": output.name, "subfolder": str(prefix.parent), "type": "output"}]}
            result = process_comfy_asset_job(claim_next_comfy_asset_job("image-fixture-worker"))
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(result.output_asset.kind, "reference")
        self.assertEqual(result.output_asset.checksum, sha256(output.read_bytes()).hexdigest())
        self.version.workflow_version.refresh_from_db()
        self.assertEqual(self.version.workflow_version.graph, original_graph)
        self.assertEqual(result.output_asset.metadata["preparation_id"], str(prep.pk))
