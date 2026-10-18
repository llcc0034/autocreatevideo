from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    ComfyWorkflowContractDraft,
    ComfyWorkflowSource,
    HostCapabilitySnapshot,
    ModelDeployment,
    ModelArtifact,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    RuntimeModelAvailability,
    WorkflowProjector,
    WorkflowProjectorVersion,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
)
from studio.services.comfyui_canvas import (
    api_graph_to_ui_workflow,
    ui_workflow_to_api_graph,
)
from studio.services.comfyui_workflow_imports import (
    _asset_dependency_candidates,
    _converted_graph_sha256,
    _model_dependency_candidates,
    _object_info_sha256,
    _parameter_schema_sha256,
    _profile_candidate_evidence_codes,
    _structural_contract_sha256,
    _suggested_import_contract,
    _validate_model_dependency_coverage,
    saved_workflow_scan_max_items,
    saved_workflow_scan_max_seconds,
    source_deployment_fingerprint,
)
from studio.services.runtime_host_probe_queue import (
    claim_next_runtime_host_probe,
    process_runtime_host_probe,
)
from studio.services.runtime_platform import (
    draft_model_profile_fingerprint,
    probe_runtime_host,
    validate_draft_profile_artifacts_for_publish,
    verified_artifact_availability,
)
from studio.services.workflow_versions import (
    import_workflow_version,
    verify_workflow_version_integrity,
    workflow_version_execution_snapshot,
)
from studio.services.workflow import aligned_frames
from studio.services.workflow_smoke_fixtures import (
    normalize_workflow_execution_inputs,
    workflow_execution_input_schema,
)
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compile_frozen_workflow_values,
)
from studio.services.draft_model_artifact_attestation import (
    _create_or_reuse_attempt,
    _safe_filename,
)
from studio.services.provider_registry import ProviderDriverError
from studio.services.provider_registry import comfyui_workflow_source_scope
from studio.tests.factories import (
    conditioning_contract,
    h3_dual_clock_ref2va_workflow_graph,
)


class ComfySavedWorkflowImportApiTests(TestCase):
    def test_contract_receipt_ready_recovery_and_explicit_promotion(self):
        from django.db import connection
        from studio.models import ComfyWorkflowPreviewReceipt
        driver = self._driver()
        original_invoke = driver.invoke.side_effect
        depth = len(connection.savepoint_ids)
        def invoke(*args, **kwargs):
            self.assertEqual(len(connection.savepoint_ids), depth, "host access must precede receipt write transaction")
            return original_invoke(*args, **kwargs)
        # Candidate registration has its own transaction; this assertion only
        # covers the preview host read introduced by the receipt path.
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            self._admit_candidate()
            driver.invoke.side_effect = invoke
            payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(), "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.pk), "request_key": "ready:proof"}
            original = self.client.post(f"/api/v1/model-deployments/{self.deployment.pk}/saved-workflows/preview/", payload, format="json")
        self.assertEqual(original.status_code, 200, original.data)
        self.assertEqual(original.data["actionability"], "ready_for_executable_import")
        self.assertTrue(original.data["suggested_input_contract"])
        with patch("studio.services.comfyui_workflow_imports.driver_for", side_effect=AssertionError("no host recovery")):
            recovered = self.client.get(f"/api/v1/model-deployments/{self.deployment.pk}/saved-workflows/preview-receipt/", {"request_key": "ready:proof"})
        self.assertEqual(recovered.data, original.data)
        self.assertEqual(recovered.data["request_receipt"]["request"]["model_profile_version"], str(self.profile.pk))
        row = ComfyWorkflowPreviewReceipt.objects.get()
        self.assertEqual(str(row.contract_draft_id), original.data["contract_draft_id"])
        # The recovery itself creates no executable version; promotion remains
        # a separate explicit business action through the existing endpoint.
        count_before = WorkflowTemplateVersion.objects.count()
        item = {**{key: value for key, value in payload.items() if key != "request_key"},
            "import_kind": "template_version", "template": str(self.template.pk),
            "contract_draft": original.data["contract_draft_id"]}
        for name in ("object_info_schema", "converted_graph", "parameter_schema", "structural_contract"):
            item[f"expected_{name}_sha256"] = recovered.data[f"{name}_sha256"]
        with patch("studio.services.comfyui_workflow_imports.driver_for", side_effect=AssertionError("promotion uses frozen contract")):
            promoted = self.client.post(f"/api/v1/model-deployments/{self.deployment.pk}/saved-workflows/batch-import/", {"items": [item]}, format="json")
        self.assertEqual(promoted.status_code, 200, promoted.data)
        self.assertEqual(promoted.data["items"][0]["result"], "created", promoted.data)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), count_before + 1)

    def test_contract_receipt_rechecks_interleaved_completion_before_save_and_after_failure(self):
        from studio.models import ComfyWorkflowPreviewReceipt
        from studio.services.comfy_workflow_preview_receipts import commit_preview_receipt, PreviewReceiptConflict
        payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(), "primary_output_node_ids": [123], "request_key": "baseline"}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=self._driver()):
            self.assertEqual(self._post_managed_preview(payload).status_code, 200)
        baseline = ComfyWorkflowPreviewReceipt.objects.get()
        for key, fail, mismatch in (("before-save", False, False), ("after-error", True, False), ("conflict", True, True)):
            def build(_data, persist):
                response = deepcopy(baseline.response)
                response["request_receipt"]["request_key"] = key
                row = ComfyWorkflowPreviewReceipt(owner=self.admin, deployment=self.deployment,
                    contract_draft=baseline.contract_draft, request_key=key,
                    request_sha256=("0" * 64 if mismatch else baseline.request_sha256))
                assign_json_contract(row, "response", response); row.save()
                if fail:
                    raise RuntimeError("host failed after competing commit")
                return persist(lambda: self.fail("must reuse committed receipt, not save another draft"))
            if mismatch:
                with self.assertRaises(PreviewReceiptConflict):
                    commit_preview_receipt(owner=self.admin, deployment=self.deployment, data={**payload, "request_key": key}, build=build)
            else:
                result = commit_preview_receipt(owner=self.admin, deployment=self.deployment, data={**payload, "request_key": key}, build=build)
                self.assertEqual(result["request_receipt"]["request_key"], key)
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 1)

    def test_contract_receipt_recovers_exact_response_without_host_access(self):
        from studio.models import ComfyWorkflowPreviewReceipt
        payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(), "primary_output_node_ids": [123],
            "request_key": "lost-response:1"}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=self._driver()):
            original = self._post_managed_preview(payload)
        self.assertEqual(original.status_code, 200, original.data)
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", side_effect=AssertionError("must not access host")):
            recovered = self.client.get(url + "preview-receipt/", {"request_key": payload["request_key"]})
            replay = self.client.post(url + "preview/", payload, format="json")
        self.assertEqual(recovered.status_code, 200, recovered.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(recovered.data, original.data)
        self.assertEqual(replay.data, original.data)
        self.assertEqual(recovered["Cache-Control"], "private, no-store")
        self.assertEqual(ComfyWorkflowPreviewReceipt.objects.count(), 1)
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 1)
        self.assertEqual(recovered.data["request_receipt"]["request"]["model_profile_version"], None)
        self.assertNotIn("executable_graph", recovered.data)
        self.assertNotIn("source_document", recovered.data)
        from django.db import connection
        if connection.vendor == "sqlite":
            plan = ComfyWorkflowPreviewReceipt.objects.filter(owner=self.admin,
                deployment=self.deployment, request_key=payload["request_key"]).explain()
            self.assertIn("USING INDEX", plan)
            self.assertNotIn("SCAN studio_comfyworkflowpreviewreceipt", plan)

    def test_contract_receipt_is_scoped_conflict_checked_and_integrity_verified(self):
        from studio.models import ComfyWorkflowPreviewReceipt
        payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(), "primary_output_node_ids": [123], "request_key": "receipt:2"}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=self._driver()):
            original = self._post_managed_preview(payload)
        self.assertEqual(original.status_code, 200, original.data)
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", side_effect=AssertionError("must not access host")):
            changed = self.client.post(url + "preview/", {**payload, "expected_sha256": "0" * 64}, format="json")
            self.assertEqual(changed.status_code, 409, changed.data)
            self.assertEqual(self.client.get(url + "preview-receipt/", {"request_key": "absent"}).status_code, 404)
            self.assertEqual(self.client.get(url + "preview-receipt/").status_code, 400)
            other = get_user_model().objects.create_superuser(username="receipt-other", password="test")
            self.client.force_authenticate(other)
            self.assertEqual(self.client.get(url + "preview-receipt/", {"request_key": "receipt:2"}).status_code, 404)
            self.client.force_authenticate(None)
            self.assertIn(self.client.get(url + "preview-receipt/", {"request_key": "receipt:2"}).status_code, (401, 403))
            self.client.force_authenticate(self.admin)
            row = ComfyWorkflowPreviewReceipt.objects.get()
            row.response["suggested_input_contract"] = {"tampered": True}
            # Deliberate DB corruption proves the read guard, not a supported writer.
            ComfyWorkflowPreviewReceipt.objects.filter(pk=row.pk).update(response=row.response)
            self.assertEqual(self.client.get(url + "preview-receipt/", {"request_key": "receipt:2"}).status_code, 400)

    def test_contract_receipt_reused_draft_has_distinct_request_proof(self):
        from studio.models import ComfyWorkflowPreviewReceipt
        payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(), "primary_output_node_ids": [123], "request_key": "first"}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=self._driver()):
            first = self._post_managed_preview(payload)
            second = self._post_managed_preview({**payload, "request_key": "second"})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["contract_draft_id"], second.data["contract_draft_id"])
        self.assertNotEqual(first.data["request_receipt"]["request_key"], second.data["request_receipt"]["request_key"])
        self.assertEqual(ComfyWorkflowPreviewReceipt.objects.count(), 2)
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 1)

    def test_contract_receipt_write_failure_rolls_back_new_contract(self):
        from studio.models import ComfyWorkflowPreviewReceipt
        payload = {"source_path": "saved/example.json", "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(), "primary_output_node_ids": [123], "request_key": "failed"}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=self._driver()):
            self._admit_candidate()
            with patch.object(ComfyWorkflowPreviewReceipt, "save", side_effect=RuntimeError("receipt persistence failed")):
                with self.assertRaisesRegex(RuntimeError, "receipt persistence failed"):
                    self.client.post(f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/", payload, format="json")
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 0)
        self.assertEqual(ComfyWorkflowPreviewReceipt.objects.count(), 0)

    def _publish_projector_successor(self, current, definition):
        created = self.client.post(
            f"/api/v1/workflow-projector-versions/{current.id}/draft-next/", {}, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        url = f"/api/v1/workflow-projector-versions/{created.data['id']}/"
        saved = self.client.patch(url, {"definition": definition}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        published = self.client.post(url + "publish/", {}, format="json")
        self.assertEqual(published.status_code, 200, published.data)
        return WorkflowProjectorVersion.objects.get(pk=created.data["id"])

    def _validate_projected_definition(self, *, graph, contract, bindings, requirements, output_kind):
        from studio.services.workflow_versions import _validate_projector_model_definition
        try:
            _validate_projector_model_definition(graph, contract, {"conditioning_contract": contract["conditioning_contract"]}, bindings, requirements)
        except ValidationError as exc:
            raise WorkflowCompilerError(str(exc)) from exc

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="comfy-import-admin",
            password="StrongPass123!",
            email="comfy-import@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.provider = ProviderProfile.objects.get(key="local-comfyui")
        self.profile = ModelProfileVersion.objects.get(profile_key="sdxl-image", version=1)
        # The fixture declares image-to-image explicitly instead of inferring
        # an operation from the output kind or the legacy model seed name.
        specification = deepcopy(self.profile.specification)
        specification["capabilities"] = ["image.image_to_image"]
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save()
        projector = WorkflowProjectorVersion.objects.get(projector__key="generic.comfy.fixed", is_current=True)
        definition = deepcopy(projector.definition)
        definition["projection"]["config"]["capability"] = "image.image_to_image"
        projector.is_current = False
        projector.save(update_fields=("is_current", "updated_at"))
        projector = WorkflowProjectorVersion(
            projector=projector.projector, version=2, provider_driver_type=projector.provider_driver_type,
            operation=projector.operation, output_kind=projector.output_kind,
            engine_key=projector.engine_key, engine_version=projector.engine_version,
            status=WorkflowProjectorVersion.Status.PUBLISHED, is_current=True,
            published_at=timezone.now(), published_by=self.admin,
        )
        assign_json_contract(projector, "definition", definition)
        projector.save()
        self.host = RuntimeHost.objects.create(key="comfy-import-host", name="Comfy import host")
        self.deployment = ModelDeployment(
            key="comfy-import-source",
            name="Comfy import source",
            profile_version=self.profile,
            runtime_host=self.host,
        )
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 10,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
        })
        self.deployment.save()
        self.template = WorkflowTemplate.objects.create(
            owner=None,
            slug="saved-workflow-import",
            version=1,
            name="Saved workflow import",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )
        self.canvas = self._canvas()
        self.raw_sha = sha256(b"canonical-saved-workflow").hexdigest()
        self.initial_workflow_version_count = WorkflowTemplateVersion.objects.count()
        self.asset_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.asset_root.cleanup)
        self.asset_settings = override_settings(ASSET_LIBRARY_ROOT=self.asset_root.name)
        self.asset_settings.enable()
        self.addCleanup(self.asset_settings.disable)
        smoke_path = Path(self.asset_root.name) / "saved-import-smoke.png"
        smoke_path.write_bytes(b"saved-workflow-smoke-image")
        self.smoke_asset = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE,
            name="Saved import smoke image",
            local_path=str(smoke_path),
            mime_type="image/png",
            checksum=sha256(smoke_path.read_bytes()).hexdigest(),
            is_active=True,
        )

    @staticmethod
    def _canvas():
        return {
            "id": "11111111-1111-4111-8111-111111111111",
            "revision": 0,
            "last_node_id": 123,
            "last_link_id": 1,
            "nodes": [
                {
                    "id": 1,
                    "type": "LoadImage",
                    "pos": [0, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 0,
                    "mode": 0,
                    "inputs": [
                        {"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None},
                        {"name": "upload", "type": "IMAGEUPLOAD", "widget": {"name": "upload"}, "link": None},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                    "properties": {"Node name for S&R": "LoadImage"},
                    "widgets_values": ["smoke.png", "image"],
                },
                {
                    "id": 123,
                    "type": "SaveImage",
                    "pos": [300, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 1,
                    "mode": 0,
                    "inputs": [
                        {"name": "images", "type": "IMAGE", "link": 1},
                        {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}, "link": None},
                        {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                        {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                        {"name": "seed", "type": "INT", "widget": {"name": "seed"}, "link": None},
                    ],
                    "outputs": [],
                    "properties": {"Node name for S&R": "SaveImage"},
                    "widgets_values": ["saved/output", 512, 512, 1],
                },
            ],
            "links": [[1, 1, 0, 123, 0, "IMAGE"]],
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }

    @staticmethod
    def _object_info():
        return {
            "LoadImage": {
                "input": {"required": {"image": [["smoke.png"], {"image_upload": True}]}},
                "input_order": {"required": ["image"]},
                "output": ["IMAGE", "MASK"],
                "output_name": ["IMAGE", "MASK"],
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    "width": ["INT", {"default": 512, "min": 16, "max": 4096}],
                    "height": ["INT", {"default": 512, "min": 16, "max": 4096}],
                    "seed": ["INT", {"default": 1, "min": 0}],
                }},
                "input_order": {"required": [
                    "images", "filename_prefix", "width", "height", "seed",
                ]},
                "output": [],
                "output_name": [],
                "output_node": True,
            },
        }

    @staticmethod
    def _h3_graph():
        graph = h3_dual_clock_ref2va_workflow_graph(low_vram=True)
        graph["2"]["inputs"]["strength_model"] = 1.0
        graph["6"]["inputs"].update({
            "prompt": "",
            "width": 352,
            "height": 608,
            "length": 56,
        })
        graph["7"]["inputs"]["steps"] = 4
        graph["9"]["inputs"]["noise_seed"] = 1
        graph["13"]["inputs"]["filename_prefix"] = "output"
        return graph

    @staticmethod
    def _h3_object_info():
        unet = "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors"
        lora = (
            "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI/"
            "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
        )
        clip = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
        video_vae = "minimax_h3_video_vae_fp16.safetensors"
        audio_vae = "minimax_h3_audio_vae_fp32.safetensors"

        def autogrow(prefix, child_name, child_type, maximum):
            return ["COMFY_AUTOGROW_V3", {"template": {
                "input": {"required": {child_name: [child_type, {}]}},
                "prefix": prefix,
                "min": 0,
                "max": maximum,
            }}]

        def node(required, outputs, *, optional=None, output_node=False):
            inputs = {"required": required}
            input_order = {"required": list(required)}
            if optional:
                inputs["optional"] = optional
                input_order["optional"] = list(optional)
            row = {
                "input": inputs,
                "input_order": input_order,
                "output": list(outputs),
                "output_name": list(outputs),
            }
            if output_node:
                row["output_node"] = True
            return row

        return {
            "LoadImage": node({"image": [["fixture.png"], {"image_upload": True}]}, ["IMAGE", "MASK"]),
            "LoadVideo": node({"file": ["COMBO", {"options": [], "video_upload": True}]}, ["VIDEO"]),
            "LoadAudio": node({"audio": ["COMBO", {"options": [], "audio_upload": True}]}, ["AUDIO"]),
            "GetVideoComponents": node({"video": ["VIDEO", {}]}, ["IMAGE", "AUDIO", "FLOAT"]),
            "UNETLoader": node({
                "unet_name": [[unet], {}],
                "weight_dtype": [["default"], {}],
            }, ["MODEL"]),
            "LoraLoaderBypassModelOnly": node({
                "model": ["MODEL", {}],
                "lora_name": [[lora], {}],
                "strength_model": ["FLOAT", {
                    "default": 1.0, "min": -10.0, "max": 10.0,
                }],
            }, ["MODEL"]),
            "CLIPLoader": node({
                "clip_name": [[clip], {}],
                "type": [["minimax"], {}],
                "device": [["default"], {}],
            }, ["CLIP"]),
            "VAELoader": node({
                "vae_name": [[video_vae, audio_vae], {}],
            }, ["VAE"]),
            "MiniMaxH3AudioConditioningT8": node({
                "clip": ["CLIP", {}],
                "video_vae": ["VAE", {}],
                "audio_vae": ["VAE", {}],
                "prompt": ["STRING", {"default": "", "multiline": True}],
                "width": ["INT", {
                    "default": 352, "min": 32, "max": 2048, "step": 32,
                }],
                "height": ["INT", {
                    "default": 608, "min": 32, "max": 2048, "step": 32,
                }],
                "length": ["INT", {
                    "default": 56, "min": 5, "max": 3600, "step": 17,
                }],
                "task_type": [["Ref2VA"], {}],
                "audio_mode": [["native"], {}],
                "audio_denoise_strength": ["FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0,
                }],
                "add_source_as_reference": ["BOOLEAN", {"default": False}],
                "prompt_primary_audio_ordinal": ["INT", {
                    "default": 0, "min": 0, "max": 64,
                }],
                "strict_prompt_tags": ["BOOLEAN", {"default": True}],
                "ref_image_size": [["match"], {}],
                "reference_video_policy": [["official_2_to_15s"], {}],
            }, ["CONDITIONING", "AV_LATENT"], optional={
                "ref_images": autogrow("ref_image_", "ref_image", "IMAGE", 9),
                "ref_videos": autogrow("ref_video_", "ref_video", "IMAGE", 3),
                "ref_video_audios": autogrow(
                    "ref_video_audio_", "ref_video_audio", "AUDIO", 3,
                ),
                "ref_audios": autogrow("ref_audio_", "ref_audio", "AUDIO", 3),
            }),
            "MiniMaxH3DualClockSamplerT8": node({
                "model": ["MODEL", {}],
                "av_latent": ["AV_LATENT", {}],
                "steps": ["INT", {"default": 4, "min": 1, "max": 100}],
                "shift_video": ["FLOAT", {
                    "default": 12.0, "min": 0.0, "max": 100.0,
                }],
                "shift_audio": ["FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 100.0,
                }],
            }, ["MODEL", "SAMPLER", "SIGMAS"]),
            "BasicGuider": node({
                "model": ["MODEL", {}],
                "conditioning": ["CONDITIONING", {}],
            }, ["GUIDER"]),
            "RandomNoise": node({
                "noise_seed": ["INT", {
                    "default": 1, "min": 0, "max": 2_147_483_647,
                }],
            }, ["NOISE"]),
            "SamplerCustomAdvanced": node({
                "noise": ["NOISE", {}],
                "guider": ["GUIDER", {}],
                "sampler": ["SAMPLER", {}],
                "sigmas": ["SIGMAS", {}],
                "latent_image": ["AV_LATENT", {}],
            }, ["LATENT"]),
            "MiniMaxH3AVDecodeT8": node({
                "av_latent": ["LATENT", {}],
                "video_vae": ["VAE", {}],
                "audio_vae": ["VAE", {}],
            }, ["IMAGE", "AUDIO"]),
            "CreateVideo": node({
                "images": ["IMAGE", {}],
                "fps": ["FLOAT", {"default": 24.0, "min": 1.0, "max": 60.0}],
                "audio": ["AUDIO", {}],
                "bit_depth": ["INT", {"default": 8, "min": 8, "max": 16}],
            }, ["VIDEO"]),
            "SaveVideo": node({
                "video": ["VIDEO", {}],
                "filename_prefix": ["STRING", {"default": "output"}],
                "format": [["auto"], {}],
                "codec": [["auto"], {}],
            }, [], output_node=True),
            "MiniMaxLowVRAMAttention": node({
                "model": ["MODEL", {}],
                "head_chunks": ["INT", {"default": 4, "min": 1, "max": 64}],
            }, ["MODEL"]),
        }

    def _h3_profile_with_attested_artifacts(
        self,
        *,
        profile_key="saved-h3-ref2va",
        modality=ModelProfileVersion.Modality.VIDEO,
        entity_images=(0, 9),
        reference_videos=(0, 3),
        paired_video_audios=(0, 3),
        reference_audios=(0, 3),
    ):
        profile = ModelProfileVersion(
            profile_key=profile_key,
            version=1,
            provider=self.provider,
            model_identifier="minimax-h3-ref2va",
            modality=modality,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["video.reference_to_video"],
            "conditioning_contract": conditioning_contract(
                "Ref2VA",
                input_kind="classified_references",
                requires_bible=True,
                first_frame=(0, 1),
                entity_images=entity_images,
                reference_videos=reference_videos,
                paired_video_audios=paired_video_audios,
                reference_audios=reference_audios,
            ),
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"{profile_key}-deployment",
            name=f"{profile_key} deployment",
            profile_version=profile,
            runtime_host=self.host,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 10,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
        })
        deployment.save()
        snapshot = HostCapabilitySnapshot(
            host=self.host,
            revision=HostCapabilitySnapshot.objects.filter(host=self.host).count() + 1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        for alias, role, filename, marker in (
            (
                "h3-ref2va",
                ModelArtifact.Role.DIFFUSION_MODEL,
                "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors",
                "1",
            ),
            (
                "h3-turbo-lora",
                ModelArtifact.Role.LORA,
                (
                    "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI/"
                    "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
                ),
                "2",
            ),
            (
                "h3-qwen3vl",
                ModelArtifact.Role.TEXT_ENCODER,
                "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
                "3",
            ),
            (
                "h3-video-vae",
                ModelArtifact.Role.VAE,
                "minimax_h3_video_vae_fp16.safetensors",
                "4",
            ),
            (
                "h3-audio-vae",
                ModelArtifact.Role.VAE,
                "minimax_h3_audio_vae_fp32.safetensors",
                "5",
            ),
        ):
            artifact = ModelArtifact.objects.create(
                profile_version=profile,
                role=role,
                logical_name=alias,
                filename=filename,
                sha256=marker * 64,
                size_bytes=1024,
                status=ModelArtifact.Status.AVAILABLE,
                verified_snapshot=snapshot,
                verified_at=timezone.now(),
                verified_by=self.admin,
            )
            RuntimeModelAvailability.objects.create(
                snapshot=snapshot,
                deployment=deployment,
                artifact=artifact,
                availability_key=f"h3:{artifact.id}",
                status=RuntimeModelAvailability.Status.AVAILABLE,
                observed_size_bytes=artifact.size_bytes,
                observed_sha256=artifact.sha256,
                observation_source=(
                    RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
                ),
            )
        return profile

    def _driver(self, *, paths=None, canvas=None, object_info=None):
        paths = paths or ["saved/example.json"]
        canvas = canvas or self.canvas
        object_info = object_info or self._object_info()
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["workflow_library.list", "workflow_library.read", "object_info"],
        }

        def invoke(_deployment, operation, payload):
            if operation == "workflow_library.list":
                return {"schema_version": 1, "items": paths}
            if operation == "workflow_library.read":
                return {
                    "schema_version": 1,
                    "source_path": payload["source_path"],
                    "source_sha256": self.raw_sha,
                    "size_bytes": 1024,
                    "workflow": canvas,
                }
            if operation == "object_info":
                return object_info
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        return driver

    def _source_scope_fields(self, source_path="saved/example.json"):
        scope = comfyui_workflow_source_scope(self.deployment)
        return {
            "source_scope_schema_version": scope.schema_version,
            "source_scope_sha256": scope.sha256,
            "source_path_sha256": sha256(source_path.encode("utf-8")).hexdigest(),
        }

    def _expected_source_scope_fields(self, deployment=None):
        scope = comfyui_workflow_source_scope(deployment or self.deployment)
        return {
            "expected_source_scope_schema_version": scope.schema_version,
            "expected_source_scope_sha256": scope.sha256,
        }

    def _second_same_scope_deployment(
        self,
        *,
        key="comfy-import-source-secondary",
        modality=ModelProfileVersion.Modality.IMAGE,
        capabilities=("image.image_to_image",),
    ):
        profile = ModelProfileVersion(
            profile_key=f"{key}-profile",
            version=1,
            provider=self.provider,
            model_identifier=key,
            modality=modality,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {"schema_version": 1, "capabilities": list(capabilities)})
        profile.save()
        deployment = ModelDeployment(
            key=key,
            name=key,
            profile_version=profile,
            runtime_host=self.host,
            priority=999,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188/",
            "timeout_seconds": 9,
            "poll_interval_seconds": 3,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
        })
        deployment.save()
        return deployment

    @staticmethod
    def _typed_parameter_canvas(
        *,
        node_type: str,
        input_values: list,
        input_definitions: dict,
        output_kind: str = "image",
    ):
        input_names = list(input_definitions)
        source_output_type = "STRING" if output_kind == "text" else "IMAGE"
        sink_type = "PreviewAny" if output_kind == "text" else "SaveImage"
        sink_input = "source" if output_kind == "text" else "images"
        canvas = {
            "id": "22222222-2222-4222-8222-222222222222",
            "revision": 0,
            "last_node_id": 123,
            "last_link_id": 1,
            "nodes": [
                {
                    "id": 1,
                    "type": node_type,
                    "pos": [0, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 0,
                    "mode": 0,
                    "inputs": [
                        {
                            "name": name,
                            "type": str(definition[0][0] if isinstance(definition[0], list) else definition[0]),
                            "widget": {"name": name},
                            "link": None,
                        }
                        for name, definition in input_definitions.items()
                    ],
                    "outputs": [{"name": source_output_type, "type": source_output_type, "links": [1]}],
                    "properties": {"Node name for S&R": node_type},
                    "widgets_values": input_values,
                },
                {
                    "id": 123,
                    "type": sink_type,
                    "pos": [300, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 1,
                    "mode": 0,
                    "inputs": [{"name": sink_input, "type": source_output_type, "link": 1}],
                    "outputs": [],
                    "properties": {"Node name for S&R": sink_type},
                    "widgets_values": [],
                },
            ],
            "links": [[1, 1, 0, 123, 0, source_output_type]],
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }
        object_info = {
            node_type: {
                "input": {"required": input_definitions},
                "input_order": {"required": input_names},
                "output": [source_output_type],
                "output_name": [source_output_type],
            },
            sink_type: {
                "input": {"required": {sink_input: [source_output_type, {}]}},
                "input_order": {"required": [sink_input]},
                "output": [],
                "output_name": [],
                "output_node": True,
            },
        }
        return canvas, object_info

    def _draft_attestation_context(self, *filenames: str):
        draft = ModelProfileVersion(
            profile_key=f"direct-attestation-{ModelProfileVersion.objects.count()}",
            version=1,
            provider=self.provider,
            model_identifier="direct-attestation",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft, "specification", {"schema_version": 1})
        draft.save()
        artifacts = [
            ModelArtifact.objects.create(
                profile_version=draft,
                role=ModelArtifact.Role.DIFFUSION_MODEL,
                logical_name=f"direct-{index}",
                filename=filename,
                status=ModelArtifact.Status.DECLARED,
            )
            for index, filename in enumerate(filenames, start=1)
        ]
        return {
            "source_deployment": self.deployment,
            "target_profile": draft,
            "artifacts": artifacts,
            "verified_by": self.admin,
        }

    @staticmethod
    def _runtime_driver(*, model_names=(), fail_health=False):
        driver = Mock()

        def invoke(_deployment, operation, payload):
            if operation == "health":
                if fail_health:
                    raise OSError("health unavailable")
                return {"system": {"comfyui_version": "test"}}
            if operation == "object_info":
                return {}
            if operation == "models":
                return {"items": list(model_names)}
            raise AssertionError(operation)

        driver.invoke.side_effect = invoke
        return driver

    def _batch_payload(
        self,
        source_path="saved/example.json",
        *,
        canvas=None,
        object_info=None,
        primary_output_node_ids=None,
    ):
        """Prepare a reviewed contract draft and return its promotion request.

        Template promotion no longer reads the mutable ComfyUI source or accepts
        client-authored contracts.  Test setup therefore follows the production
        path: freeze a candidate, ask the server to project a contract draft,
        then submit only the draft identity plus the reviewed hashes.
        """
        canvas = canvas or self.canvas
        object_info = object_info or self._object_info()
        primary_output_node_ids = primary_output_node_ids or [123]
        driver = self._driver(
            paths=[source_path],
            canvas=canvas,
            object_info=object_info,
        )
        candidate_payload = {
            "items": [{
                "source_path": source_path,
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "candidate",
            }],
        }
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            candidate = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                candidate_payload,
                format="json",
            )
            self.assertEqual(candidate.status_code, 200, candidate.data)
            self.assertIn(candidate.data["items"][0]["result"], {"created", "reused"})
            preview = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": source_path,
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": primary_output_node_ids,
                    "model_profile_version": str(self.profile.id),
                },
                format="json",
            )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["actionability"], "ready_for_executable_import")
        self.assertTrue(preview.data["contract_draft_id"])
        return {
            "items": [{
                "source_path": source_path,
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": primary_output_node_ids,
                "expected_object_info_schema_sha256": preview.data[
                    "object_info_schema_sha256"
                ],
                "expected_converted_graph_sha256": preview.data[
                    "converted_graph_sha256"
                ],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(self.template.id),
                "model_profile_version": str(self.profile.id),
            }],
        }

    def _admit_candidate(
        self,
        *,
        source_path="saved/example.json",
        deployment=None,
        expected_sha256=None,
    ):
        deployment = deployment or self.deployment
        response = self.client.post(
            f"/api/v1/model-deployments/{deployment.id}/saved-workflows/batch-import/",
            {
                "items": [{
                    "source_path": source_path,
                    "expected_sha256": expected_sha256 or self.raw_sha,
                    **self._expected_source_scope_fields(deployment),
                    "import_kind": "candidate",
                }],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(response.data["items"][0]["result"], {"created", "reused"})
        return response

    def _post_managed_preview(self, payload, *, deployment=None):
        """Exercise the public candidate -> contract-draft request sequence."""

        deployment = deployment or self.deployment
        self._admit_candidate(
            source_path=payload["source_path"],
            deployment=deployment,
            expected_sha256=payload["expected_sha256"],
        )
        return self.client.post(
            f"/api/v1/model-deployments/{deployment.id}/saved-workflows/preview/",
            payload,
            format="json",
        )

    def test_scan_and_preview_are_staff_only_read_operations(self):
        driver = self._driver()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            listing = self.client.get(
                "/api/v1/comfy-workflow-sources/discover/",
            )
            candidate = self._admit_candidate()
            preview = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
                format="json",
            )

        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(candidate.data["items"][0]["import_kind"], "candidate")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(
            listing.data["effective_limits"],
            preview.data["effective_limits"],
        )
        self.assertEqual(listing.data["effective_limits"]["scan_max_items"], 250)
        self.assertEqual(listing.data["effective_limits"]["scan_max_seconds"], 30.0)
        self.assertEqual(listing.data["effective_limits"]["request_max_bytes"], 64 * 1024 * 1024)
        self.assertEqual(listing.data["effective_limits"]["batch_max_items"], 20)
        self.assertEqual(listing.data["effective_limits"]["batch_max_seconds"], 60.0)
        self.assertEqual(
            listing.data["effective_limits"]["saved_workflow_directory"],
            "workflows",
        )
        self.assertEqual(listing.data["items"][0]["source_sha256"], self.raw_sha)
        self.assertEqual(preview.data["source_path"], "saved/example.json")
        self.assertEqual(preview.data["primary_output_node_ids"], ["123"])
        self.assertEqual(preview.data["primary_api_node_ids"], ["2"])
        self.assertEqual(
            preview.data["suggested_input_contract"]["outputs"]["primary"],
            {"node_id": "2", "kind": "image", "required": True},
        )
        self.assertEqual(
            preview.data["suggested_input_contract"]["required_dimension_multiple"],
            1,
        )
        self.assertEqual(
            {
                preview.data["suggested_input_contract"]["parameters"][key][
                    "multiple_of"
                ]
                for key in ("image.width", "image.height")
            },
            {1},
        )
        self.assertEqual(
            {
                (row["semantic_key"], row["node_id"], row["input_name"])
                for row in preview.data["suggested_semantic_bindings"]
            },
            {
                ("asset.image.source", "1", "image"),
                ("output.prefix", "2", "filename_prefix"),
                ("image.width", "2", "width"),
                ("image.height", "2", "height"),
                ("sampling.seed", "2", "seed"),
            },
        )
        self.assertEqual(preview.data["suggested_model_requirements"], [])
        self.assertNotIn("workflow", preview.data)
        self.assertNotIn("api_graph", preview.data)
        self.assertEqual(
            ComfyWorkflowSource.objects.filter(
                import_kind=ComfyWorkflowSource.ImportKind.CANDIDATE,
            ).count(),
            1,
        )
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 1)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count,
        )

    def test_object_info_outage_keeps_discovery_candidate_admission_available(self):
        driver = self._driver()
        invoke = driver.invoke.side_effect

        def invoke_with_object_info_outage(deployment, operation, payload):
            if operation == "object_info":
                raise ProviderDriverError(
                    "object_info unavailable",
                    code="provider_object_info_unavailable",
                )
            return invoke(deployment, operation, payload)

        driver.invoke.side_effect = invoke_with_object_info_outage
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")
            admitted = self._admit_candidate()

        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.data["items"][0]
        self.assertEqual(item["analysis_status"], "blocked")
        self.assertEqual(item["analysis_code"], "provider_object_info_unavailable")
        self.assertEqual(item["admission_tier"], "candidate")
        self.assertEqual(item["actionability"], "candidate_only")
        self.assertFalse(item["candidate_managed"])
        self.assertEqual(item["match_reason_codes"], ["workflow_analysis_unavailable"])
        self.assertEqual(admitted.status_code, 200, admitted.data)
        self.assertEqual(admitted.data["status"], "complete")
        self.assertEqual(admitted.data["items"][0]["import_kind"], "candidate")
        self.assertEqual(
            ComfyWorkflowSource.objects.filter(
                import_kind=ComfyWorkflowSource.ImportKind.CANDIDATE,
            ).count(),
            1,
        )
        self.assertEqual(ComfyWorkflowContractDraft.objects.count(), 0)

        ordinary = get_user_model().objects.create_user(
            username="workflow-reader", password="StrongPass123!",
        )
        self.client.force_authenticate(ordinary)
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            denied = self.client.get(
                "/api/v1/comfy-workflow-sources/discover/",
            )
        self.assertEqual(denied.status_code, 403)
        lookup.assert_not_called()

    def test_recommendation_keeps_declared_model_inputs_out_of_runtime_parameters(self):
        canvas = self._canvas()
        canvas["nodes"][1]["inputs"].append({"name": "weights", "type": "COMBO", "widget": {"name": "weights"}, "link": None})
        canvas["nodes"][1]["widgets_values"].append("reviewed.safetensors")
        info = self._object_info()
        info["SaveImage"]["input"]["required"]["weights"] = [["reviewed.safetensors"], {}]
        info["SaveImage"]["input_order"]["required"].append("weights")
        selector = {"node_type": "SaveImage", "input_name": "weights", "role": "checkpoint",
            "inventory_node_type": "SaveImage", "inventory_input_name": "weights", "inventory_folder": ""}
        driver = self._driver(canvas=canvas, object_info=info)
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver), patch(
            "studio.services.comfyui_workflow_imports._projector_recommendation_model_selectors", return_value=([selector], [])
        ):
            admitted = self._admit_candidate()
            response = self.client.post("/api/v1/workflow-projector-versions/recommendation/", {
                "candidate_source_id": admitted.data["items"][0]["source_id"],
                **self._expected_source_scope_fields(), "expected_source_sha256": self.raw_sha,
                "primary_output_node_id": "123", "output_kind": "image",
            }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["definition"]["model_selectors"], [selector])
        parameters = response.data["definition"]["parameter_policies"]
        self.assertNotIn("weights", [row["input_name"] for row in parameters])
        self.assertIn("seed", [row["input_name"] for row in parameters])

    def test_projector_recommendation_uses_managed_candidate_and_returns_unsaved_safe_draft(self):
        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            admitted = self._admit_candidate()
            candidate_id = admitted.data["items"][0]["source_id"]
            family_count = WorkflowProjector.objects.count()
            version_count = WorkflowProjectorVersion.objects.count()
            response = self.client.post(
                "/api/v1/workflow-projector-versions/recommendation/",
                {
                    "candidate_source_id": candidate_id,
                    **self._expected_source_scope_fields(),
                    "expected_source_sha256": self.raw_sha,
                    "primary_output_node_id": "123",
                    "output_kind": "image",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["persistence"], "unsaved_draft")
        self.assertTrue(response.data["requires_human_confirmation"])
        self.assertEqual(response.data["engine_key"], "generic.fixed")
        self.assertEqual(response.data["output_kind"], "image")
        self.assertEqual(
            response.data["definition"]["detector"]["node_counts"],
            {"LoadImage": 1, "SaveImage": 1},
        )
        self.assertEqual(
            response.data["definition"]["output_nodes"],
            {"SaveImage": "image"},
        )
        self.assertEqual(
            response.data["definition"]["projection"],
            {"kind": "generic_fixed", "config": {}},
        )
        self.assertEqual(
            response.data["source_evidence"]["candidate_source_id"],
            candidate_id,
        )
        self.assertEqual(
            response.data["source_evidence"]["source_sha256"],
            self.raw_sha,
        )
        self.assertEqual(len(response.data["recommendation_sha256"]), 64)
        self.assertTrue(response.data["warnings"])
        self.assertIn(
            "images",
            response.data["catalog"]["inputs_by_node_type"]["SaveImage"],
        )
        self.assertTrue(response.data["definition"]["required_edges"])
        self.assertTrue(response.data["definition"]["parameter_policies"])
        self.assertEqual(WorkflowProjector.objects.count(), family_count)
        self.assertEqual(WorkflowProjectorVersion.objects.count(), version_count)
        serialized = str(response.data)
        self.assertNotIn("saved/example.json", serialized)
        self.assertNotIn("smoke.png", serialized)
        self.assertNotIn("saved/output", serialized)
        self.assertNotIn("prompt", [call.args[1] for call in driver.invoke.call_args_list])

        stale = self.client.post(
            "/api/v1/workflow-projector-versions/recommendation/",
            {
                "candidate_source_id": candidate_id,
                **self._expected_source_scope_fields(),
                "expected_source_sha256": "0" * 64,
                "primary_output_node_id": "123",
                "output_kind": "image",
            },
            format="json",
        )
        self.assertEqual(stale.status_code, 400, stale.data)

        ordinary = get_user_model().objects.create_user(
            username="projector-recommendation-reader",
            password="StrongPass123!",
        )
        self.client.force_authenticate(ordinary)
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            denied = self.client.post(
                "/api/v1/workflow-projector-versions/recommendation/",
                {
                    "candidate_source_id": candidate_id,
                    **self._expected_source_scope_fields(),
                    "expected_source_sha256": self.raw_sha,
                    "primary_output_node_id": "123",
                    "output_kind": "image",
                },
                format="json",
            )
        self.assertEqual(denied.status_code, 403)
        lookup.assert_not_called()

    def test_imported_candidate_keeps_primary_output_for_dedicated_projector_recommendation(self):
        promotion = self.client.post(
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
            self._batch_payload(),
            format="json",
        )
        self.assertEqual(promotion.status_code, 200, promotion.data)
        self.assertEqual(promotion.data["items"][0]["result"], "created")

        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")

        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.data["items"][0]
        self.assertTrue(item["candidate_managed"])
        self.assertTrue(item["imported"])
        self.assertEqual(item["match_status"], "matched")
        self.assertEqual(item["matched_primary_output_node_id"], "123")
        self.assertEqual(item["matched_output_kind"], "image")
        self.assertEqual(item["output_candidates"][0]["canvas_node_id"], "123")
        matched_profile = item["model_profile_candidates"][0]
        self.assertEqual(
            matched_profile["model_profile_version_id"],
            str(self.profile.id),
        )
        self.assertEqual(
            matched_profile["target_deployment_id"],
            str(self.deployment.id),
        )
        self.assertTrue(matched_profile["eligible"])
        self.assertEqual(matched_profile["eligible_output_node_ids"], ["123"])

        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            recommendation = self.client.post(
                "/api/v1/workflow-projector-versions/recommendation/",
                {
                    "candidate_source_id": item["candidate_source_id"],
                    **self._expected_source_scope_fields(),
                    "expected_source_sha256": self.raw_sha,
                    "primary_output_node_id": item["matched_primary_output_node_id"],
                    "output_kind": item["matched_output_kind"],
                },
                format="json",
            )

        self.assertEqual(recommendation.status_code, 200, recommendation.data)
        self.assertEqual(recommendation.data["persistence"], "unsaved_draft")
        self.assertTrue(recommendation.data["requires_human_confirmation"])

    def test_imported_candidate_lists_successor_models_without_rebinding_frozen_target(self):
        promotion = self.client.post(
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
            self._batch_payload(), format="json",
        )
        self.assertEqual(promotion.data["items"][0]["result"], "created")
        original_id = promotion.data["items"][0]["workflow_version_id"]
        corrected = ModelProfileVersion(
            profile_key=self.profile.profile_key, version=2,
            provider=self.provider, model_identifier=self.profile.model_identifier,
            modality=self.profile.modality, status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(corrected, "specification", deepcopy(self.profile.specification))
        corrected.save()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=self._driver(),
        ):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")
        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.data["items"][0]
        self.assertEqual(item["match_status"], "matched")
        self.assertEqual(item["matched_model_profile_version_id"], str(self.profile.id))
        self.assertEqual(item["matched_target_deployment_id"], str(self.deployment.id))
        candidates = {
            row["model_profile_version_id"]: row
            for row in item["model_profile_candidates"]
        }
        candidate = candidates[str(corrected.id)]
        # This read/write-image-only canvas contains no model selector. The
        # alternative must be visible without inheriting the frozen admission.
        self.assertFalse(candidate["eligible"], candidate)
        self.assertEqual(candidate["eligible_output_node_ids"], [])
        self.assertIn("model_identity_evidence_missing", candidate["rejection_codes"])
        self.assertFalse(candidate["live_validate_ready"])
        self.assertEqual(candidate["target_deployment_id"], "")
        self.assertIn("model_profile_not_published", candidate["live_validate_rejection_codes"])
        self.assertEqual(
            WorkflowTemplateVersion.objects.get(pk=original_id).model_profile_version_id,
            self.profile.id,
        )
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.profile_version_id, self.profile.id)

    def test_preview_without_model_profile_persists_candidate_only_contract_draft(self):
        """Model selection belongs to contract completion, not source admission."""

        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            admitted = self._admit_candidate()
            first = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                },
                format="json",
            )
            repeated = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                },
                format="json",
            )

        self.assertEqual(admitted.data["items"][0]["import_kind"], "candidate")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(first.data["admission_tier"], "candidate")
        self.assertEqual(first.data["actionability"], "candidate_only")
        self.assertIn(
            "model_profile_selection_required",
            {row["code"] for row in first.data["blockers"]},
        )
        self.assertTrue(first.data["contract_draft_id"])
        self.assertEqual(
            repeated.data["contract_draft_id"],
            first.data["contract_draft_id"],
        )
        self.assertEqual(repeated.data["contract_draft_revision"], 1)
        draft = ComfyWorkflowContractDraft.objects.get(
            pk=first.data["contract_draft_id"],
        )
        self.assertIsNone(draft.model_profile_version_id)
        self.assertIsNotNone(draft.projector_version_id)
        self.assertEqual(
            draft.actionability,
            ComfyWorkflowContractDraft.Actionability.CANDIDATE_ONLY,
        )
        detail = self.client.get(
            f"/api/v1/comfy-workflow-contract-drafts/{draft.id}/",
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("projection", detail.data)
        self.assertEqual(
            detail.data["projection_summary"]["source_sha256"],
            self.raw_sha,
        )
        self.assertEqual(
            detail.data["projection_summary"]["executable_node_count"],
            0,
        )

    def test_missing_current_projector_keeps_discovery_and_draft_candidate_capable(self):
        """A missing interpreter must not reject a safe immutable source."""

        generic = WorkflowProjectorVersion.objects.select_related("projector").get(
            projector__key="generic.comfy.fixed",
            is_current=True,
        )
        generic.status = WorkflowProjectorVersion.Status.RETIRED
        generic.is_current = False
        generic.save(update_fields=("status", "is_current", "updated_at"))

        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")
            admitted = self._admit_candidate()
            preview = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
                format="json",
            )

        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.data["items"][0]
        self.assertEqual(item["admission_tier"], "candidate")
        self.assertEqual(item["actionability"], "candidate_only")
        self.assertEqual(item["analysis_code"], "workflow_projector_unresolved")
        self.assertEqual(item["output_candidates"][0]["suggested_output_kind"], "")
        self.assertEqual(admitted.status_code, 200, admitted.data)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["actionability"], "candidate_only")
        self.assertIn(
            "workflow_projector_catalog_unavailable",
            {row["code"] for row in preview.data["blockers"]},
        )
        draft = ComfyWorkflowContractDraft.objects.get(
            pk=preview.data["contract_draft_id"],
        )
        self.assertIsNone(draft.projector_version_id)
        self.assertEqual(
            draft.actionability,
            ComfyWorkflowContractDraft.Actionability.CANDIDATE_ONLY,
        )

    def test_ambiguous_current_projectors_keep_source_and_draft_candidate_capable(self):
        """Runtime ambiguity is an interpretation gap, never an admission veto."""

        generic = WorkflowProjectorVersion.objects.select_related("projector").get(
            projector__key="generic.comfy.fixed",
            is_current=True,
        )
        duplicate_family = WorkflowProjector.objects.create(
            key="test.generic.comfy.ambiguous",
            name="Ambiguous generic test projector",
        )
        duplicate = WorkflowProjectorVersion(
            projector=duplicate_family,
            version=1,
            provider_driver_type=generic.provider_driver_type,
            operation=generic.operation,
            output_kind=generic.output_kind,
            engine_key=generic.engine_key,
            engine_version=generic.engine_version,
            status=WorkflowProjectorVersion.Status.PUBLISHED,
            is_current=True,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(duplicate, "definition", deepcopy(generic.definition))
        duplicate.save()

        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")
            admitted = self._admit_candidate()
            preview = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
                format="json",
            )

        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.data["items"][0]
        self.assertEqual(item["admission_tier"], "candidate")
        self.assertEqual(item["actionability"], "candidate_only")
        # Discovery exposes one stable unresolved code; the generated draft
        # retains the more specific ambiguity blocker below.
        self.assertIn("workflow_projector_unresolved", item["match_reason_codes"])
        self.assertEqual(admitted.status_code, 200, admitted.data)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["actionability"], "candidate_only")
        self.assertIn(
            "workflow_projector_ambiguous",
            {row["code"] for row in preview.data["blockers"]},
        )
        draft = ComfyWorkflowContractDraft.objects.get(
            pk=preview.data["contract_draft_id"],
        )
        self.assertIsNone(draft.projector_version_id)

    def test_global_discovery_scans_one_source_scope_once_and_never_guesses_profile(self):
        second = self._second_same_scope_deployment()
        self.assertEqual(
            comfyui_workflow_source_scope(self.deployment).sha256,
            comfyui_workflow_source_scope(second).sha256,
        )
        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/comfy-workflow-sources/discover/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "complete")
        self.assertEqual(response.data["source_count"], 1)
        self.assertEqual(response.data["sources"][0]["deployment_count"], 2)
        self.assertEqual(len(response.data["items"]), 1)
        item = response.data["items"][0]
        self.assertEqual(item["match_status"], "unmatched")
        self.assertIn("model_identity_evidence_missing", item["match_reason_codes"])
        self.assertEqual(item["matched_model_profile_version_id"], "")
        operations = [call.args[1] for call in driver.invoke.call_args_list]
        self.assertEqual(operations.count("workflow_library.list"), 1)
        self.assertEqual(operations.count("workflow_library.read"), 1)
        self.assertEqual(operations.count("object_info"), 1)

        retired_detail_scan = self.client.get(
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/",
        )
        self.assertEqual(retired_detail_scan.status_code, 404)

    def test_saved_workflow_directory_participates_in_source_scope_identity(self):
        with patch(
            "studio.services.provider_registry.saved_workflow_directory",
            return_value="workflows/team-a",
        ):
            team_a = comfyui_workflow_source_scope(self.deployment)
        with patch(
            "studio.services.provider_registry.saved_workflow_directory",
            return_value="workflows/team-b",
        ):
            team_b = comfyui_workflow_source_scope(self.deployment)

        self.assertEqual(team_a.schema_version, 2)
        self.assertEqual(team_a.directory, "workflows/team-a")
        self.assertEqual(team_b.directory, "workflows/team-b")
        self.assertNotEqual(team_a.sha256, team_b.sha256)

    def test_global_discovery_auto_matches_the_only_evidence_backed_output_profile_pair(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"].insert(1, {
            "id": 2,
            "type": "UNETLoader",
            "pos": [0, 200],
            "size": [240, 120],
            "flags": {},
            "order": 1,
            "mode": 0,
            "inputs": [
                {"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}},
                {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}},
            ],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [2]}],
            "properties": {"Node name for S&R": "UNETLoader"},
            "widgets_values": ["unique/model.safetensors", "default"],
        })
        canvas["nodes"][2]["inputs"].append({
            "name": "model", "type": "MODEL", "link": 2,
        })
        canvas["nodes"].extend((
            {
                "id": 3,
                "type": "PrimitiveString",
                "pos": [0, 400],
                "size": [240, 120],
                "flags": {},
                "order": 3,
                "mode": 0,
                "inputs": [{"name": "text", "type": "STRING", "widget": {"name": "text"}}],
                "outputs": [{"name": "STRING", "type": "STRING", "links": [3]}],
                "properties": {"Node name for S&R": "PrimitiveString"},
                "widgets_values": ["preview only"],
            },
            {
                "id": 124,
                "type": "PreviewAny",
                "pos": [300, 400],
                "size": [240, 120],
                "flags": {},
                "order": 4,
                "mode": 0,
                "inputs": [{"name": "source", "type": "STRING", "link": 3}],
                "outputs": [],
                "properties": {"Node name for S&R": "PreviewAny"},
                "widgets_values": [],
            },
        ))
        canvas["links"].extend((
            [2, 2, 0, 123, 5, "MODEL"],
            [3, 3, 0, 124, 0, "STRING"],
        ))
        canvas["last_node_id"] = 124
        canvas["last_link_id"] = 3
        object_info = self._object_info()
        object_info["UNETLoader"] = {
            "input": {"required": {
                "unet_name": [["unique/model.safetensors"], {}],
                "weight_dtype": [["default"], {}],
            }},
            "input_order": {"required": ["unet_name", "weight_dtype"]},
            "output": ["MODEL"],
            "output_name": ["MODEL"],
        }
        object_info["SaveImage"]["input"]["optional"] = {"model": ["MODEL", {}]}
        object_info["SaveImage"]["input_order"]["optional"] = ["model"]
        object_info["PrimitiveString"] = {
            "input": {"required": {"text": ["STRING", {"default": ""}]}},
            "input_order": {"required": ["text"]},
            "output": ["STRING"],
            "output_name": ["STRING"],
        }
        object_info["PreviewAny"] = {
            "input": {"required": {"source": ["STRING", {}]}},
            "input_order": {"required": ["source"]},
            "output": [],
            "output_name": [],
            "output_node": True,
        }
        snapshot = HostCapabilitySnapshot(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="unique-discovery-model",
            filename="unique/model.safetensors",
            sha256="f" * 64,
            size_bytes=512,
            status=ModelArtifact.Status.AVAILABLE,
            verified_snapshot=snapshot,
            verified_at=timezone.now(),
            verified_by=self.admin,
        )
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=self.deployment,
            artifact=artifact,
            availability_key=f"deployment:{self.deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        driver = self._driver(canvas=canvas, object_info=object_info)

        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            response = self.client.get("/api/v1/comfy-workflow-sources/discover/")

        self.assertEqual(response.status_code, 200, response.data)
        item = response.data["items"][0]
        self.assertEqual(item["match_status"], "matched", item)
        self.assertEqual(item["matched_primary_output_node_id"], "123")
        self.assertEqual(item["matched_model_profile_version_id"], str(self.profile.id))
        candidate = next(
            row for row in item["model_profile_candidates"]
            if str(row["model_profile_version_id"]) == str(self.profile.id)
        )
        self.assertEqual(candidate["eligible_output_node_ids"], ["123"])

    def test_same_scope_import_through_different_model_deployments_reuses_source(self):
        second = self._second_same_scope_deployment(key="comfy-import-source-reuse")
        driver = self._driver()
        first_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )
        second_url = (
            f"/api/v1/model-deployments/{second.id}/"
            "saved-workflows/batch-import/"
        )
        before_versions = WorkflowTemplateVersion.objects.count()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            first = self.client.post(first_url, self._batch_payload(), format="json")
            repeated = self.client.post(second_url, self._batch_payload(), format="json")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["items"][0]["result"], "created")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["items"][0]["result"], "reused")
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), before_versions + 1)

    def test_batch_rejects_inactive_workflow_template_before_source_read(self):
        self.template.is_active = False
        self.template.save(update_fields=("is_active", "updated_at"))
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                self._batch_payload(),
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("template", str(response.data))
        lookup.assert_not_called()

    def test_preview_rejects_model_profile_from_another_provider_before_source_read(self):
        foreign_provider = ProviderProfile(
            key="foreign-comfy-preview",
            name="Foreign Comfy preview",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="workflow_library.list,workflow_library.read,object_info",
        )
        assign_json_contract(
            foreign_provider,
            "driver_config",
            {"base_url": "http://127.0.0.1:8288"},
        )
        foreign_provider.save()
        foreign_profile = ModelProfileVersion(
            profile_key="foreign-comfy-preview",
            version=1,
            provider=foreign_provider,
            model_identifier="foreign-video",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(foreign_profile, "specification", {"schema_version": 1})
        foreign_profile.save()

        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/"
                "saved-workflows/preview/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(foreign_profile.id),
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("同一", str(response.data))
        lookup.assert_not_called()

    def test_preview_rejects_profile_whose_modality_does_not_match_selected_output(self):
        video_profile = ModelProfileVersion(
            profile_key="saved-preview-video-mismatch",
            version=1,
            provider=self.provider,
            model_identifier="saved-preview-video-mismatch",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(video_profile, "specification", {"schema_version": 1})
        video_profile.save()
        driver = self._driver()

        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            response = self._post_managed_preview(
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(video_profile.id),
                },
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("模态", str(response.data))

    def test_h3_saved_canvas_projects_dynamic_references_and_preserves_prompt_binding(self):
        profile = self._h3_profile_with_attested_artifacts()
        graph = self._h3_graph()
        object_info = self._h3_object_info()
        canvas = api_graph_to_ui_workflow(graph, object_info)
        output_canvas_id = next(
            node["id"] for node in canvas["nodes"] if node["type"] == "SaveVideo"
        )
        template = WorkflowTemplate.objects.create(
            owner=None,
            slug="saved-h3-ref2va",
            version=1,
            name="Saved H3 Ref2VA",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )
        driver = self._driver(canvas=canvas, object_info=object_info)
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            self._admit_candidate()
            preview = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [output_canvas_id],
                "model_profile_version": str(profile.id),
            }, format="json")
            self.assertEqual(preview.status_code, 200, preview.data)
            self.assertEqual(preview.data["blockers"], [])
            contract = preview.data["suggested_input_contract"]
            self.assertEqual(contract["compiler"], {"id": "generic.comfy", "version": 3})
            self.assertEqual(
                contract["contract_projector"]["id"],
                "minimax_h3.classified_ref2va",
            )
            self.assertEqual(contract["contract_projector"]["version"], 1)
            self.assertTrue(contract["contract_projector"]["version_id"])
            self.assertEqual(len(contract["contract_projector"]["definition_sha256"]), 64)
            frames_preview = next(
                row for row in preview.data["configurable_parameters"]
                if row["semantic_key"] == "video.frames"
            )
            self.assertEqual(frames_preview["alignment_modulus"], 17)
            self.assertEqual(frames_preview["alignment_remainder"], 5)
            self.assertIn("prompt.positive", contract["parameters"])
            self.assertEqual(
                contract["classified_references"]["groups"]["images"]["max_items"],
                9,
            )
            self.assertEqual(
                contract["classified_references"]["groups"]["videos"]["max_items"],
                3,
            )
            self.assertEqual(
                contract["classified_references"]["groups"]["audios"]["max_items"],
                3,
            )
            imported = self.client.post(batch_url, {"items": [{
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": [output_canvas_id],
                "expected_object_info_schema_sha256": preview.data[
                    "object_info_schema_sha256"
                ],
                "expected_converted_graph_sha256": preview.data[
                    "converted_graph_sha256"
                ],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(template.id),
                "model_profile_version": str(profile.id),
            }]}, format="json")

        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["failed_count"], 0, imported.data)
        version = WorkflowTemplateVersion.objects.get(
            pk=imported.data["items"][0]["workflow_version_id"],
        )
        self.assertEqual((version.compiler_id, version.compiler_version), ("generic.comfy", 3))
        schema = workflow_execution_input_schema(version)
        # Prompt text remains supplied by the existing AssetGenerationJob or
        # ProductionRun prompt path; workflow_inputs contains only parameters
        # and reference bindings, so importing a workflow must not duplicate it.
        self.assertNotIn("prompt.positive", {
            row["semantic_key"] for row in schema["parameters"]
        })
        self.assertTrue(version.semantic_bindings.filter(
            semantic_key="prompt.positive",
            value_type="string",
        ).exists())
        dynamic = schema["dynamic_reference_modes"]["Ref2VA"]
        frames_parameter = next(
            row for row in schema["parameters"]
            if row["semantic_key"] == "video.frames"
        )
        self.assertEqual(frames_parameter["alignment_modulus"], 17)
        self.assertEqual(frames_parameter["alignment_remainder"], 5)
        self.assertEqual(dynamic["pictures"]["max_items"], 9)
        self.assertEqual(dynamic["videos"]["max_items"], 3)
        self.assertEqual(dynamic["audios"]["max_items"], 3)
        self.assertEqual(dynamic["maximum_physical_assets"], 18)
        source = ComfyWorkflowSource.objects.get(workflow_version=version)
        self.assertEqual(
            source.conversion_report["contract_projector"],
            contract["contract_projector"],
        )
        frozen = normalize_workflow_execution_inputs(
            version,
            {"schema_version": 2, "values": {"video.frames": 56}},
            owner=self.admin,
        )
        self.assertEqual(frozen.values["video.frames"], 56)
        with self.assertRaisesMessage(ValidationError, "5 + 17k"):
            normalize_workflow_execution_inputs(
                version,
                {"schema_version": 2, "values": {"video.frames": 57}},
                owner=self.admin,
            )

    def test_h3_projector_rejects_dynamic_schema_drift_and_never_widens_live_ranges(self):
        profile = self._h3_profile_with_attested_artifacts()
        graph = self._h3_graph()
        object_info = self._h3_object_info()
        dependencies = _model_dependency_candidates(
            graph,
            object_info,
            model_profile_version=profile,
        )

        drifted = deepcopy(object_info)
        drifted["MiniMaxH3AudioConditioningT8"]["input"]["optional"][
            "ref_videos"
        ][1]["template"]["max"] = 2
        fallback, _bindings, _requirements, blockers, _parameters = (
            _suggested_import_contract(
                graph,
                drifted,
                primary_api_node_ids=["13"],
                model_dependencies=dependencies,
                asset_dependencies=[],
                model_profile_version=profile,
            )
        )
        self.assertEqual(fallback["compiler"], {"id": "generic.comfy", "version": 1})
        self.assertIn(
            "workflow_projector_contract_mismatch",
            {row["code"] for row in blockers},
        )

        for label, invalid_definition in (
            ("fractional_int_bound", ["INT", {"default": 56, "min": 22.5, "max": 3600}]),
            ("wrong_widget_type", ["FLOAT", {"default": 56.0, "min": 5.0, "max": 3600.0}]),
        ):
            with self.subTest(label=label):
                malformed = deepcopy(object_info)
                malformed["MiniMaxH3AudioConditioningT8"]["input"]["required"][
                    "length"
                ] = invalid_definition
                fallback, _bindings, _requirements, malformed_blockers, _parameters = (
                    _suggested_import_contract(
                        graph,
                        malformed,
                        primary_api_node_ids=["13"],
                        model_dependencies=dependencies,
                        asset_dependencies=[],
                        model_profile_version=profile,
                    )
                )
                self.assertEqual(
                    fallback["compiler"],
                    {"id": "generic.comfy", "version": 1},
                )
                self.assertIn(
                    "workflow_projector_contract_mismatch",
                    {row["code"] for row in malformed_blockers},
                )

        narrowed = deepcopy(object_info)
        narrowed["MiniMaxH3AudioConditioningT8"]["input"]["required"][
            "length"
        ][1]["min"] = 20
        contract, bindings, requirements, blockers, _parameters = (
            _suggested_import_contract(
                graph,
                narrowed,
                primary_api_node_ids=["13"],
                model_dependencies=dependencies,
                asset_dependencies=[],
                model_profile_version=profile,
            )
        )
        self.assertIn("workflow_projector_parameter_range_invalid", {row["code"] for row in blockers})
        # Explicit Projector bounds cannot silently widen a changed live node.
        # A newly reviewed configuration must narrow its minimum first.
        configured = WorkflowProjectorVersion.objects.get(projector__key="minimax_h3.classified_ref2va", is_current=True)
        definition = deepcopy(configured.definition)
        next(row for row in definition["parameter_policies"] if row["semantic_key"] == "video.frames")["constraints"]["min"] = 22
        self._publish_projector_successor(configured, definition)
        contract, bindings, requirements, blockers, _parameters = _suggested_import_contract(
            graph, narrowed, primary_api_node_ids=["13"], model_dependencies=dependencies,
            asset_dependencies=[], model_profile_version=profile,
        )
        self.assertEqual(blockers, [])
        self.assertEqual(contract["parameters"]["video.frames"]["min"], 22)
        self.assertEqual(contract["frame_policy"]["minimum_frames"], 22)
        self.assertEqual(aligned_frames(1_000, contract["frame_policy"]), 22)
        self._validate_projected_definition(
            graph=graph,
            contract=contract,
            bindings=bindings,
            requirements=requirements,
            output_kind="video",
        )

        base_projector = WorkflowProjectorVersion.objects.select_related(
            "projector",
        ).get(pk=contract["contract_projector"]["version_id"])

        def published_compatible_projector(*, projector, version):
            row = WorkflowProjectorVersion(
                projector=projector,
                version=version,
                provider_driver_type=base_projector.provider_driver_type,
                operation=base_projector.operation,
                output_kind=base_projector.output_kind,
                engine_key=base_projector.engine_key,
                engine_version=base_projector.engine_version,
                status=WorkflowProjectorVersion.Status.PUBLISHED,
                is_current=False,
                published_at=timezone.now(),
                published_by=self.admin,
            )
            assign_json_contract(row, "definition", deepcopy(base_projector.definition))
            row.save()
            return row

        successor = published_compatible_projector(
            projector=base_projector.projector,
            version=base_projector.version + 1,
        )
        alternate_family = WorkflowProjector.objects.create(
            key="test.minimax_h3.compatible_ref2va",
            name="Compatible H3 Ref2VA test projector",
        )
        alternate = published_compatible_projector(
            projector=alternate_family,
            version=1,
        )
        for label, compatible in (
            ("successor_version", successor),
            ("alternate_family", alternate),
        ):
            with self.subTest(label=label):
                compatible_contract = deepcopy(contract)
                compatible_contract["contract_projector"] = {
                    "id": compatible.projector.key,
                    "version": compatible.version,
                    "version_id": str(compatible.id),
                    "definition_sha256": compatible.definition_sha256,
                    "provider_driver_type": compatible.provider_driver_type,
                    "operation": compatible.operation,
                    "output_kind": compatible.output_kind,
                    "engine": {
                        "key": compatible.engine_key,
                        "version": compatible.engine_version,
                    },
                }
                self._validate_projected_definition(
                    graph=graph,
                    contract=compatible_contract,
                    bindings=bindings,
                    requirements=requirements,
                    output_kind="video",
                )
        for label, path, invalid_value in (
            ("projector_version", ("contract_projector", "version"), 0),
            ("topology_schema", ("classified_references", "schema_version"), 0),
            ("image_minimum", ("classified_references", "groups", "images", "min_items"), 1),
            (
                "paired_audio_index",
                ("classified_references", "groups", "video_audios", "input_names"),
                "images",
            ),
            ("frame_minimum", ("frame_policy", "minimum_frames"), 39),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(contract)
                target = tampered
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid_value
                with self.assertRaises(WorkflowCompilerError):
                    self._validate_projected_definition(
                        graph=graph,
                        contract=tampered,
                        bindings=bindings,
                        requirements=requirements,
                        output_kind="video",
                    )

    def test_h3_projector_parameter_range_updates_frozen_frame_policy(self):
        from studio.services.workflow_projectors import select_workflow_projector
        profile = self._h3_profile_with_attested_artifacts(profile_key="saved-h3-frame-range")
        graph, object_info = self._h3_graph(), self._h3_object_info()
        projector = deepcopy(select_workflow_projector(graph))
        for row in projector.definition["parameter_policies"]:
            if row["semantic_key"] == "video.frames":
                row["constraints"].update(min=22, max=90, default=22)
        configured = WorkflowProjectorVersion.objects.get(pk=projector.version_id)
        self._publish_projector_successor(configured, projector.definition)
        projector = select_workflow_projector(graph)
        contract, bindings, requirements, blockers, parameters = _suggested_import_contract(
            graph, object_info, primary_api_node_ids=["13"],
            model_dependencies=_model_dependency_candidates(graph, object_info, model_profile_version=profile),
            asset_dependencies=[], model_profile_version=profile, projector=projector,
        )
        self.assertFalse(blockers)
        self.assertEqual(contract["parameters"]["video.frames"]["max"], 90)
        self.assertEqual(contract["frame_policy"]["minimum_frames"], 22)
        self.assertEqual(contract["frame_policy"]["maximum_frames"], 90)
        self.assertEqual(next(row for row in parameters if row["semantic_key"] == "video.frames")["max"], 90)
        self._validate_projected_definition(graph=graph, contract=contract, bindings=bindings, requirements=requirements, output_kind="video")

    def test_h3_projector_allows_profile_cardinality_inside_live_autogrow_capacity(self):
        profile = self._h3_profile_with_attested_artifacts(
            profile_key="saved-h3-ref2va-narrow",
            modality=ModelProfileVersion.Modality.MULTIMODAL,
            entity_images=(0, 3),
            reference_videos=(0, 2),
            paired_video_audios=(0, 1),
            reference_audios=(0, 0),
        )
        graph = self._h3_graph()
        object_info = self._h3_object_info()
        dependencies = _model_dependency_candidates(
            graph,
            object_info,
            model_profile_version=profile,
        )
        contract, _bindings, _requirements, blockers, _parameters = (
            _suggested_import_contract(
                graph,
                object_info,
                primary_api_node_ids=["13"],
                model_dependencies=dependencies,
                asset_dependencies=[],
                model_profile_version=profile,
            )
        )

        self.assertEqual(blockers, [])
        topology = contract["classified_references"]["groups"]
        self.assertEqual(topology["images"]["max_items"], 3)
        self.assertEqual(topology["videos"]["max_items"], 2)
        self.assertEqual(topology["video_audios"]["max_items"], 1)
        self.assertEqual(topology["audios"]["max_items"], 0)

    def test_h3_scan_requires_current_projector_and_reimport_clears_upgrade_flag(self):
        profile = self._h3_profile_with_attested_artifacts(
            profile_key="saved-h3-projector-upgrade",
        )
        graph = self._h3_graph()
        object_info = self._h3_object_info()
        dependencies = _model_dependency_candidates(
            graph,
            object_info,
            model_profile_version=profile,
        )
        contract, bindings, requirements, blockers, _parameters = (
            _suggested_import_contract(
                graph,
                object_info,
                primary_api_node_ids=["13"],
                model_dependencies=dependencies,
                asset_dependencies=[],
                model_profile_version=profile,
            )
        )
        self.assertEqual(blockers, [])
        legacy_contract = deepcopy(contract)
        legacy_contract.pop("contract_projector")
        self.assertEqual(legacy_contract["schema_version"], 3)
        self.assertNotIn("smoke_inputs", legacy_contract)
        self.assertNotIn("smoke_asset_bindings", legacy_contract)
        template = WorkflowTemplate.objects.create(
            owner=None,
            slug="saved-h3-projector-upgrade",
            version=1,
            name="Saved H3 projector upgrade",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )
        legacy_version = import_workflow_version(
            template=template,
            graph=graph,
            input_contract=contract,
            semantic_bindings=bindings,
            model_requirements=requirements,
            provider=self.provider,
            model_profile_version=profile,
        )
        # Historical missing-reference data may be read, never admitted through
        # the current import service. Construct that historical fact explicitly.
        assign_json_contract(legacy_version, "input_contract", legacy_contract)
        legacy_version.save(update_fields=("input_contract", "input_contract_schema_version", "input_contract_size_bytes", "input_contract_sha256"))
        legacy_source = ComfyWorkflowSource(
            source_deployment=self.deployment,
            **self._source_scope_fields(),
            source_path="saved/example.json",
            source_path_key="saved/example.json",
            source_sha256=self.raw_sha,
            source_size_bytes=1024,
            source_workflow_id="33333333-3333-4333-8333-333333333333",
            import_contract_sha256="e" * 64,
            import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
            converted_graph_sha256=legacy_version.graph_sha256,
            workflow_version=legacy_version,
            imported_by=self.admin,
        )
        assign_json_contract(legacy_source, "conversion_report", {
            "schema_version": 1,
            "import_contract_sha256": "e" * 64,
        })
        legacy_source.full_clean()
        legacy_source.save()

        canvas = api_graph_to_ui_workflow(graph, object_info)
        output_canvas_id = next(
            node["id"] for node in canvas["nodes"] if node["type"] == "SaveVideo"
        )
        scan_url = "/api/v1/comfy-workflow-sources/discover/"
        preview_url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        batch_url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        driver = self._driver(canvas=canvas, object_info=object_info)
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            before = self.client.get(scan_url)
            self._admit_candidate()
            preview = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [output_canvas_id],
                "model_profile_version": str(profile.id),
            }, format="json")
            self.assertEqual(preview.status_code, 200, preview.data)
            payload = {"items": [{
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": [output_canvas_id],
                "expected_object_info_schema_sha256": preview.data[
                    "object_info_schema_sha256"
                ],
                "expected_converted_graph_sha256": preview.data[
                    "converted_graph_sha256"
                ],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(template.id),
                "model_profile_version": str(profile.id),
            }]}
            upgraded = self.client.post(batch_url, payload, format="json")
            after = self.client.get(scan_url)
            repeated = self.client.post(batch_url, payload, format="json")

        self.assertEqual(before.status_code, 200, before.data)
        self.assertTrue(before.data["items"][0]["contract_upgrade_required"])
        self.assertEqual(upgraded.status_code, 200, upgraded.data)
        self.assertEqual(upgraded.data["items"][0]["result"], "created")
        upgraded_version = WorkflowTemplateVersion.objects.get(
            pk=upgraded.data["items"][0]["workflow_version_id"],
        )
        self.assertEqual(
            upgraded_version.input_contract["contract_projector"]["id"],
            "minimax_h3.classified_ref2va",
        )
        self.assertEqual(
            upgraded_version.input_contract["contract_projector"]["version"],
            1,
        )
        self.assertTrue(
            upgraded_version.input_contract["contract_projector"]["version_id"],
        )
        self.assertNotEqual(upgraded_version.id, legacy_version.id)
        self.assertEqual(upgraded_version.template_id, legacy_version.template_id)
        self.assertEqual(after.status_code, 200, after.data)
        self.assertFalse(after.data["items"][0]["contract_upgrade_required"])
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["items"][0]["result"], "reused")
        self.assertEqual(
            repeated.data["items"][0]["workflow_version_id"],
            str(upgraded_version.id),
        )

    def test_execution_snapshot_is_classified_as_provenance_without_creating_a_template_version(self):
        task_id = "11111111-1111-4111-8111-111111111112"
        snapshot_path = f"autoprompt-task-{task_id}-comfyui-ui.json"
        driver = self._driver(paths=[snapshot_path, "saved/example.json"])
        original_version_count = WorkflowTemplateVersion.objects.count()

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            listing = self.client.get(
                "/api/v1/comfy-workflow-sources/discover/",
            )
            imported = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                {"items": [{
                    "source_path": snapshot_path,
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "import_kind": "execution_snapshot",
                }]},
                format="json",
            )

        self.assertEqual(listing.status_code, 200, listing.data)
        classifications = {
            item["source_path"]: item["suggested_import_kind"]
            for item in listing.data["items"]
        }
        self.assertEqual(classifications[snapshot_path], "execution_snapshot")
        self.assertEqual(classifications["saved/example.json"], "template_version")
        self.assertEqual(imported.status_code, 200, imported.data)
        source = ComfyWorkflowSource.objects.get(source_path=snapshot_path)
        self.assertEqual(source.import_kind, ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT)
        self.assertIsNone(source.workflow_version_id)
        self.assertEqual(imported.data["items"][0]["workflow_version_id"], "")
        self.assertEqual(WorkflowTemplateVersion.objects.count(), original_version_count)

    def test_execution_snapshot_links_native_task_without_a_legacy_generated_workflow(self):
        from studio.models import GenerationTask, SeriesProject
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG

        project = SeriesProject.objects.create(owner=self.admin, title="Native task import", **PROJECT_CONFIG)
        task = GenerationTask.objects.create(owner=self.admin, project=project, kind="video", status="succeeded")
        path = f"autoprompt-task-{task.pk}-comfyui-ui.json"
        driver = self._driver(paths=[path])
        version_count = WorkflowTemplateVersion.objects.count()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            listing = self.client.get("/api/v1/comfy-workflow-sources/discover/")
            result = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.pk}/saved-workflows/batch-import/",
                {"items": [{"source_path": path, "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(), "import_kind": "execution_snapshot"}]}, format="json")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(result.status_code, 200, result.data)
        source = ComfyWorkflowSource.objects.get(source_path=path)
        self.assertEqual(source.generation_task_id, task.pk)
        self.assertTrue(source.conversion_report["linked_task"])
        self.assertNotIn("linked_generated_workflow", source.conversion_report)
        self.assertIsNone(source.workflow_version_id)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), version_count)
        detail = self.client.get(f"/api/v1/comfy-workflow-sources/{source.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(str(detail.data["generation_task"]), str(task.pk))
        self.assertNotIn("generated_workflow", detail.data)

    def test_saved_workflow_batch_limit_follows_current_security_boundary(self):
        lowered = create_config_revision(
            scope_type="system",
            values={"security.comfy.import.batch_max_items": 1},
            created_by=self.admin,
        )
        publish_config_revision(lowered, published_by=self.admin)
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )
        items = [{
            "source_path": f"saved/{index}.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "ignored",
        } for index in range(21)]
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            rejected = self.client.post(
                batch_url,
                {"items": items[:2]},
                format="json",
            )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("最多处理 1", str(rejected.data))
        lookup.assert_not_called()

        raised = create_config_revision(
            scope_type="system",
            values={"security.comfy.import.batch_max_items": 21},
            created_by=self.admin,
        )
        publish_config_revision(raised, published_by=self.admin)
        driver = self._driver(paths=[item["source_path"] for item in items])
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            accepted = self.client.post(
                batch_url,
                {"items": items},
                format="json",
            )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(accepted.data["failed_count"], 0)
        self.assertEqual(len(accepted.data["items"]), 21)

    def test_prompt_conditioners_follow_sampler_positive_and_negative_branches(self):
        graph = {
            "1": {
                "class_type": "CLIPTextEncode",
                # Empty prompt widgets are still runtime prompt targets; the
                # importer must not mistake an empty saved default for absence.
                "inputs": {"text": "", "clip": ["9", 0]},
            },
            "8": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "negative prompt", "clip": ["9", 0]},
            },
            "3": {
                "class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["8", 0]},
            },
            "4": {
                "class_type": "KSampler",
                "inputs": {
                    "positive": ["1", 0],
                    "negative": ["3", 0],
                    "latent_image": ["5", 0],
                },
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1},
            },
            "6": {
                "class_type": "SaveImage",
                "inputs": {"images": ["4", 0], "filename_prefix": "saved/output"},
            },
            "9": {"class_type": "CLIPLoader", "inputs": {}},
        }
        object_info = {
            "CLIPTextEncode": {
                "input": {"required": {
                    "text": ["STRING", {"multiline": True}],
                    "clip": ["CLIP", {}],
                }},
            },
            "ConditioningZeroOut": {
                "input": {"required": {"conditioning": ["CONDITIONING", {}]}},
            },
            "KSampler": {
                "input": {"required": {
                    "positive": ["CONDITIONING", {}],
                    "negative": ["CONDITIONING", {}],
                    "latent_image": ["LATENT", {}],
                }},
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {"default": 512, "min": 16, "max": 4096}],
                    "height": ["INT", {"default": 512, "min": 16, "max": 4096}],
                    "batch_size": ["INT", {"default": 1, "min": 1, "max": 64}],
                }},
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }},
                "output_node": True,
            },
            "CLIPLoader": {"input": {"required": {}}},
        }

        contract, bindings, _requirements, _blockers, _parameters = (
            _suggested_import_contract(
                graph,
                object_info,
                primary_api_node_ids=["6"],
                model_dependencies=[],
                asset_dependencies=[],
                model_profile_version=None,
            )
        )

        prompt_bindings = {
            (row["semantic_key"], row["node_id"], row["input_name"])
            for row in bindings
            if row["semantic_key"].startswith("prompt.")
        }
        self.assertEqual(prompt_bindings, {
            ("prompt.positive", "1", "text"),
            ("prompt.negative", "8", "text"),
        })
        self.assertIn("prompt.positive", contract["parameters"])
        self.assertIn("prompt.negative", contract["parameters"])

    def test_preview_exposes_source_aware_typed_smoke_parameters(self):
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/preview/"
        )

        preview_sequence = {"value": 0}

        def preview_for(canvas, object_info, *, model_profile_version=None):
            model_profile_version = model_profile_version or self.profile
            preview_sequence["value"] += 1
            source_path = f"saved/typed-{preview_sequence['value']}.json"
            driver = self._driver(canvas=canvas, object_info=object_info)
            with patch(
                "studio.services.comfyui_workflow_imports.driver_for",
                return_value=driver,
            ):
                response = self._post_managed_preview({
                    "source_path": source_path,
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(model_profile_version.id),
                })
            self.assertEqual(response.status_code, 200, response.data)
            self.assertRegex(response.data["parameter_schema_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(response.data["structural_contract_sha256"], r"^[0-9a-f]{64}$")
            return response.data

        for source_value in (0.1, 0.4, 0.2, 0.4):
            with self.subTest(h3_megapixels=source_value):
                canvas, object_info = self._typed_parameter_canvas(
                    node_type="ResolutionSelector",
                    input_values=["16:9", source_value],
                    input_definitions={
                        "aspect_ratio": [["1:1", "16:9", "9:16"], {"default": "1:1"}],
                        "megapixels": ["FLOAT", {"default": 1.0, "min": 0.1, "max": 4.0, "step": 0.1}],
                    },
                )
                payload = preview_for(canvas, object_info)
                parameter = next(
                    row for row in payload["configurable_parameters"]
                    if row["semantic_key"] == "image.megapixels"
                )
                safe_options = [
                    candidate for candidate in (0.1, 0.2, 0.4)
                    if candidate <= source_value
                ]
                self.assertEqual(parameter, {
                    "semantic_key": "image.megapixels",
                    "source_value": source_value,
                    "recommended_safe_value": source_value,
                    "default": 1.0,
                    "min": 0.1,
                    "max": max(safe_options),
                    "schema_min": 0.1,
                    "schema_max": 4.0,
                    "multiple_of": None,
                    "alignment_modulus": None,
                    "alignment_remainder": None,
                    "options": safe_options,
                    "type": "number",
                    "selection_group": None,
                    "target": {
                        "node_id": "1",
                        "node_type": "ResolutionSelector",
                        "input_name": "megapixels",
                    },
                })
                self.assertEqual(
                    payload["suggested_input_contract"]["parameters"]["image.megapixels"]["default"],
                    source_value,
                )
                self.assertNotIn("smoke_inputs", payload["suggested_input_contract"])
                self.assertNotIn(
                    "required_dimension_multiple",
                    payload["suggested_input_contract"],
                )
                self.assertIn(
                    "resolution_execution_adapter_unavailable",
                    {row["code"] for row in payload["blockers"]},
                )
                recovery_action = next(
                    row for row in payload["recovery_actions"]
                    if row["code"] == "resolution_execution_adapter_unavailable"
                )
                self.assertEqual(
                    recovery_action["label"],
                    "核对主输出的尺寸来源链；支持显式宽高和受管 LoadImage 经 "
                    "FluxKontextImageScale 驱动的图片尺寸。未知缩放/分支需受审适配器，"
                    "不是重新登记模型即可修复；修改来源后重新生成合同草稿",
                )

        canvas, object_info = self._typed_parameter_canvas(
            node_type="EmptySD3LatentImage",
            input_values=[1328, 1328, 1],
            input_definitions={
                "width": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                "height": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                "batch_size": ["INT", {"default": 1, "min": 1, "max": 64}],
            },
        )
        qwen = preview_for(canvas, object_info)
        self.assertEqual(
            qwen["suggested_input_contract"]["required_dimension_multiple"],
            16,
        )
        by_key = {row["semantic_key"]: row for row in qwen["configurable_parameters"]}
        for key in ("image.width", "image.height"):
            self.assertEqual(by_key[key]["source_value"], 1328)
            self.assertEqual(by_key[key]["recommended_safe_value"], 768)
            self.assertEqual(by_key[key]["options"], [512, 768])
            self.assertEqual(
                qwen["suggested_input_contract"]["parameters"][key]["default"],
                1328,
            )
            self.assertEqual(
                qwen["suggested_input_contract"]["parameters"][key]["multiple_of"],
                16,
            )

        for label, width_step, height_step in (
            ("mismatch", 8, 16),
            ("invalid", 0, 0),
        ):
            with self.subTest(dimension_step_contract=label):
                canvas, object_info = self._typed_parameter_canvas(
                    node_type="EmptySD3LatentImage",
                    input_values=[1024, 1024],
                    input_definitions={
                        "width": ["INT", {
                            "default": 1024, "min": 16, "max": 4096,
                            "step": width_step,
                        }],
                        "height": ["INT", {
                            "default": 1024, "min": 16, "max": 4096,
                            "step": height_step,
                        }],
                    },
                )
                converted = ui_workflow_to_api_graph(
                    canvas,
                    object_info,
                    primary_output_node_ids=[123],
                )
                unresolved_contract, bindings, requirements, blockers, _rows = (
                    _suggested_import_contract(
                        converted.api_graph,
                        object_info,
                        primary_api_node_ids=list(converted.primary_output_node_ids),
                        model_dependencies=[],
                        asset_dependencies=[],
                        model_profile_version=self.profile,
                    )
                )
                self.assertNotIn(
                    "required_dimension_multiple",
                    unresolved_contract,
                )
                self.assertIn(
                    "required_dimension_multiple_unresolved",
                    {row["code"] for row in blockers},
                )
                preview = preview_for(canvas, object_info)
                self.assertNotIn(
                    "required_dimension_multiple",
                    preview["suggested_input_contract"],
                )
                self.assertIn(
                    "required_dimension_multiple_unresolved",
                    {row["code"] for row in preview["blockers"]},
                )
                with self.assertRaisesMessage(
                    ValidationError,
                    "required_dimension_multiple",
                ):
                    import_workflow_version(
                        template=self.template,
                        graph=converted.api_graph,
                        input_contract=unresolved_contract,
                        semantic_bindings=bindings,
                        model_requirements=requirements,
                        provider=self.provider,
                        model_profile_version=self.profile,
                    )

        canvas, object_info = self._typed_parameter_canvas(
            node_type="TextGenerate",
            input_values=["safe prompt", 256],
            input_definitions={
                "prompt": ["STRING", {"default": ""}],
                "max_length": ["INT", {"default": 1024, "min": 1, "max": 4096}],
            },
            output_kind="text",
        )
        text_deployment = self._second_same_scope_deployment(
            key="typed-text-preview",
            modality=ModelProfileVersion.Modality.TEXT,
        )
        text = preview_for(
            canvas,
            object_info,
            model_profile_version=text_deployment.profile_version,
        )
        maximum = next(
            row for row in text["configurable_parameters"]
            if row["semantic_key"] == "parameter.text.max_length"
        )
        self.assertEqual(maximum["source_value"], 256)
        self.assertEqual(maximum["recommended_safe_value"], 64)
        self.assertEqual(
            text["suggested_input_contract"]["parameters"][maximum["semantic_key"]]["default"],
            256,
        )

        canvas, object_info = self._typed_parameter_canvas(
            node_type="SeedVR2VideoUpscaler",
            input_values=[2048, 2048],
            input_definitions={
                "resolution": ["INT", {"default": 2048, "min": 256, "max": 4096, "step": 64}],
                "max_resolution": ["INT", {"default": 2048, "min": 256, "max": 4096, "step": 64}],
            },
        )
        seedvr = preview_for(canvas, object_info)
        by_key = {row["semantic_key"]: row for row in seedvr["configurable_parameters"]}
        for key in ("parameter.seedvr2.resolution", "parameter.seedvr2.max_resolution"):
            self.assertEqual(by_key[key]["source_value"], 2048)
            self.assertEqual(by_key[key]["recommended_safe_value"], 1024)
            self.assertEqual(
                seedvr["suggested_input_contract"]["parameters"][key]["default"],
                2048,
            )

    def test_resolution_selector_stays_blocked_until_dimension_lattice_is_defined(self):
        cases = (
            ("16:9 (Widescreen)", 0.1),
            ("16:9 (Widescreen)", 0.4),
            ("9:16 (Portrait Widescreen)", 0.2),
        )
        for index, (source_aspect, source_mp) in enumerate(cases, start=1):
            with self.subTest(h3_case=index, source_mp=source_mp):
                canvas, object_info = self._typed_parameter_canvas(
                    node_type="ResolutionSelector",
                    input_values=[source_aspect, source_mp],
                    input_definitions={
                        "aspect_ratio": [[
                            "16:9 (Widescreen)",
                            "9:16 (Portrait Widescreen)",
                        ], {"default": "16:9 (Widescreen)"}],
                        "megapixels": ["FLOAT", {
                            "default": 1.0, "min": 0.1, "max": 4.0, "step": 0.1,
                        }],
                    },
                )
                converted = ui_workflow_to_api_graph(
                    canvas, object_info, primary_output_node_ids=[123],
                )
                contract, bindings, requirements, blockers, parameters = (
                    _suggested_import_contract(
                        converted.api_graph,
                        object_info,
                        primary_api_node_ids=list(converted.primary_output_node_ids),
                        model_dependencies=[],
                        asset_dependencies=[],
                        model_profile_version=self.profile,
                    )
                )
                self.assertEqual(requirements, [])
                self.assertNotIn("required_dimension_multiple", contract)
                self.assertIn(
                    "resolution_execution_adapter_unavailable",
                    {row["code"] for row in blockers},
                )
                by_key = {row["semantic_key"]: row for row in parameters}
                self.assertEqual(by_key["image.aspect_ratio"]["source_value"], source_aspect)
                self.assertEqual(by_key["image.megapixels"]["source_value"], source_mp)
                self.assertTrue(contract["parameters"]["image.aspect_ratio"]["use_source_default"])
                self.assertTrue(contract["parameters"]["image.megapixels"]["use_source_default"])
                with self.assertRaisesMessage(
                    ValidationError,
                    "aspect_megapixels",
                ):
                    import_workflow_version(
                        template=self.template,
                        graph=converted.api_graph,
                        input_contract=contract,
                        semantic_bindings=bindings,
                        model_requirements=requirements,
                        provider=self.provider,
                        model_profile_version=self.profile,
                    )

    def test_explicit_resolution_presets_preserve_non_square_source_ratio(self):
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        )
        cases = (
            ((1696, 768), ([512, 768], [224, 352])),
            ((768, 1280), ([304, 464], [512, 768])),
        )
        for (source_width, source_height), (width_options, height_options) in cases:
            with self.subTest(source=(source_width, source_height)):
                canvas, object_info = self._typed_parameter_canvas(
                    node_type="EmptySD3LatentImage",
                    input_values=[source_width, source_height, 1],
                    input_definitions={
                        "width": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                        "height": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                        "batch_size": ["INT", {"default": 1, "min": 1, "max": 64}],
                    },
                )
                driver = self._driver(canvas=canvas, object_info=object_info)
                with patch(
                    "studio.services.comfyui_workflow_imports.driver_for",
                    return_value=driver,
                ):
                    response = self._post_managed_preview({
                        "source_path": f"saved/ratio-{source_width}-{source_height}.json",
                        "expected_sha256": self.raw_sha,
                        **self._expected_source_scope_fields(),
                        "primary_output_node_ids": [123],
                        "model_profile_version": str(self.profile.id),
                    })
                self.assertEqual(response.status_code, 200, response.data)
                by_key = {
                    row["semantic_key"]: row
                    for row in response.data["configurable_parameters"]
                }
                self.assertEqual(by_key["image.width"]["options"], width_options)
                self.assertEqual(by_key["image.height"]["options"], height_options)
                self.assertEqual(
                    by_key["image.width"]["selection_group"],
                    by_key["image.height"]["selection_group"],
                )
                recommended_width = by_key["image.width"]["recommended_safe_value"]
                recommended_height = by_key["image.height"]["recommended_safe_value"]
                self.assertEqual(
                    (recommended_width, recommended_height),
                    (width_options[-1], height_options[-1]),
                )
                self.assertLess(
                    abs((recommended_width / recommended_height) - (source_width / source_height)),
                    0.04,
                )

    def test_seedvr_chain_uses_explicit_test_values_and_keeps_source_defaults(self):
        canvas = {
            "id": "33333333-3333-4333-8333-333333333333",
            "revision": 0,
            "last_node_id": 123,
            "last_link_id": 3,
            "nodes": [
                {
                    "id": 1, "type": "EmptySD3LatentImage", "pos": [0, 0],
                    "size": [200, 100], "flags": {}, "order": 0, "mode": 0,
                    "inputs": [
                        {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                        {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                        {"name": "batch_size", "type": "INT", "widget": {"name": "batch_size"}, "link": None},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}],
                    "properties": {}, "widgets_values": [768, 1280, 1],
                },
                {
                    "id": 2, "type": "ImageScaleToTotalPixels", "pos": [220, 0],
                    "size": [200, 100], "flags": {}, "order": 1, "mode": 0,
                    "inputs": [
                        {"name": "image", "type": "IMAGE", "link": 1},
                        {"name": "upscale_method", "type": "COMBO", "widget": {"name": "upscale_method"}, "link": None},
                        {"name": "megapixels", "type": "FLOAT", "widget": {"name": "megapixels"}, "link": None},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2]}],
                    "properties": {}, "widgets_values": ["lanczos", 1.0],
                },
                {
                    "id": 3, "type": "SeedVR2VideoUpscaler", "pos": [440, 0],
                    "size": [200, 100], "flags": {}, "order": 2, "mode": 0,
                    "inputs": [
                        {"name": "image", "type": "IMAGE", "link": 2},
                        {"name": "resolution", "type": "INT", "widget": {"name": "resolution"}, "link": None},
                        {"name": "max_resolution", "type": "INT", "widget": {"name": "max_resolution"}, "link": None},
                    ],
                    "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [3]}],
                    "properties": {}, "widgets_values": [2048, 2048],
                },
                {
                    "id": 123, "type": "SaveImage", "pos": [660, 0],
                    "size": [200, 100], "flags": {}, "order": 3, "mode": 0,
                    "inputs": [{"name": "images", "type": "IMAGE", "link": 3}],
                    "outputs": [], "properties": {}, "widgets_values": [],
                },
            ],
            "links": [
                [1, 1, 0, 2, 0, "IMAGE"],
                [2, 2, 0, 3, 0, "IMAGE"],
                [3, 3, 0, 123, 0, "IMAGE"],
            ],
            "groups": [], "config": {}, "extra": {}, "version": 0.4,
        }
        object_info = {
            "EmptySD3LatentImage": {
                "input": {"required": {
                    "width": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                    "height": ["INT", {"default": 1024, "min": 16, "max": 4096, "step": 16}],
                    "batch_size": ["INT", {"default": 1, "min": 1, "max": 64}],
                }},
                "input_order": {"required": ["width", "height", "batch_size"]},
                "output": ["IMAGE"], "output_name": ["IMAGE"],
            },
            "ImageScaleToTotalPixels": {
                "input": {"required": {
                    "image": ["IMAGE", {}],
                    "upscale_method": [["nearest-exact", "lanczos"], {"default": "nearest-exact"}],
                    "megapixels": ["FLOAT", {"default": 1.0, "min": 0.01, "max": 16.0, "step": 0.01}],
                }},
                "input_order": {"required": ["image", "upscale_method", "megapixels"]},
                "output": ["IMAGE"], "output_name": ["IMAGE"],
            },
            "SeedVR2VideoUpscaler": {
                "input": {"required": {
                    "image": ["IMAGE", {}],
                    "resolution": ["INT", {"default": 1080, "min": 256, "max": 4096, "step": 64}],
                    "max_resolution": ["INT", {"default": 1080, "min": 256, "max": 4096, "step": 64}],
                }},
                "input_order": {"required": ["image", "resolution", "max_resolution"]},
                "output": ["IMAGE"], "output_name": ["IMAGE"],
            },
            "SaveImage": {
                "input": {"required": {"images": ["IMAGE", {}]}},
                "input_order": {"required": ["images"]},
                "output": [], "output_name": [], "output_node": True,
            },
        }
        driver = self._driver(canvas=canvas, object_info=object_info)
        preview_url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        batch_url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            preview = self._post_managed_preview({
                "source_path": "saved/seedvr-chain.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            })
        self.assertEqual(preview.status_code, 200, preview.data)
        input_contract = deepcopy(preview.data["suggested_input_contract"])
        item = {
            "source_path": "saved/seedvr-chain.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "template_version",
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": preview.data["object_info_schema_sha256"],
            "expected_converted_graph_sha256": preview.data["converted_graph_sha256"],
            "expected_parameter_schema_sha256": preview.data["parameter_schema_sha256"],
            "expected_structural_contract_sha256": preview.data["structural_contract_sha256"],
            "contract_draft": preview.data["contract_draft_id"],
            "template": str(self.template.id),
            "model_profile_version": str(self.profile.id),
            "input_contract": input_contract,
            "semantic_bindings": preview.data["suggested_semantic_bindings"],
            "model_requirements": preview.data["suggested_model_requirements"],
        }
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            imported = self.client.post(batch_url, {"items": [item]}, format="json")
        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["status"], "complete", imported.data)
        version = WorkflowTemplateVersion.objects.get(
            pk=imported.data["items"][0]["workflow_version_id"],
        )
        fixture = normalize_workflow_execution_inputs(version, {
            "schema_version": 2, "values": {
                "image.width": 464, "image.height": 768,
                "parameter.image.scale_megapixels": 0.25,
                "parameter.seedvr2.resolution": 1024, "parameter.seedvr2.max_resolution": 1024,
            }, "assets": [],
        }, owner=self.admin)
        smoke = compile_frozen_workflow_values(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            # This import fixture has a SaveImage node without a prefix widget.
            # Pure compilation must not invent an unbound server input.
            scalar_values={**fixture.values, "generation.batch_size": 1},
            resolve_asset=lambda *_: self.fail("This graph declares no source media"),
            classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None,
        ).graph
        self.assertEqual((smoke["1"]["inputs"]["width"], smoke["1"]["inputs"]["height"]), (464, 768))
        self.assertEqual(smoke["2"]["inputs"]["megapixels"], 0.25)
        self.assertEqual(smoke["3"]["inputs"]["resolution"], 1024)
        self.assertEqual(smoke["3"]["inputs"]["max_resolution"], 1024)

        production = compile_frozen_workflow_values(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            scalar_values={**fixture.values, "image.width": 768, "image.height": 1280,
                "generation.batch_size": 1, "parameter.image.scale_megapixels": 1.0,
                "parameter.seedvr2.resolution": 2048, "parameter.seedvr2.max_resolution": 2048},
            resolve_asset=lambda *_: self.fail("No source media"),
            classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None,
        ).graph
        self.assertEqual(production["2"]["inputs"]["megapixels"], 1.0)
        self.assertEqual(production["3"]["inputs"]["resolution"], 2048)
        self.assertEqual(production["3"]["inputs"]["max_resolution"], 2048)

    def test_batch_rederives_typed_parameter_allowlist_and_freezes_selection_hash(self):
        driver = self._driver()
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            self._admit_candidate()
            preview = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            }, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        base_item = {
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "template_version",
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": preview.data["object_info_schema_sha256"],
            "expected_converted_graph_sha256": preview.data["converted_graph_sha256"],
            "expected_parameter_schema_sha256": preview.data["parameter_schema_sha256"],
            "expected_structural_contract_sha256": preview.data["structural_contract_sha256"],
            "contract_draft": preview.data["contract_draft_id"],
            "template": str(self.template.id),
            "model_profile_version": str(self.profile.id),
        }
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            imported = self.client.post(batch_url, {"items": [base_item]}, format="json")
        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["status"], "complete", imported.data)
        self.assertRegex(
            imported.data["items"][0]["structural_contract_sha256"],
            r"^[0-9a-f]{64}$",
        )
        version = WorkflowTemplateVersion.objects.get(
            pk=imported.data["items"][0]["workflow_version_id"],
        )
        self.assertNotIn("smoke_inputs", version.input_contract)
        self.assertNotIn("smoke_asset_bindings", version.input_contract)
        fixture = normalize_workflow_execution_inputs(version, {
            "schema_version": 2,
            "values": {"image.width": 512, "image.height": 512},
            "assets": [{
                "semantic_key": "asset.image.source",
                "asset_id": str(self.smoke_asset.id),
            }],
        }, owner=self.admin)
        source = fixture.asset_facts[0]
        compiled = compile_frozen_workflow_values(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            scalar_values={**fixture.values, "output.prefix": "test/explicit-source-inputs"},
            resolve_asset=lambda key, _rule: (f"__AUTOPROMPT_ASSET__:{source['asset_id']}", source)
                if key == source["semantic_key"] else self.fail("Undeclared media input"),
            classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None,
        )
        self.assertEqual(compiled.graph["2"]["inputs"]["width"], 512)
        self.assertEqual(compiled.graph["2"]["inputs"]["height"], 512)
        self.assertEqual(
            compiled.graph["1"]["inputs"]["image"],
            f"__AUTOPROMPT_ASSET__:{self.smoke_asset.id}",
        )

        wrong_parameter_hash = deepcopy(base_item)
        wrong_parameter_hash["expected_parameter_schema_sha256"] = "f" * 64
        wrong_structural_hash = deepcopy(base_item)
        wrong_structural_hash["expected_structural_contract_sha256"] = "e" * 64
        cases = (
            ("parameter_hash", wrong_parameter_hash),
            ("structural_hash", wrong_structural_hash),
        )
        for label, item in cases:
            with self.subTest(label=label):
                before_sources = ComfyWorkflowSource.objects.count()
                with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
                    response = self.client.post(batch_url, {"items": [item]}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["status"], "failed", response.data)
                self.assertEqual(
                    response.data["items"][0]["code"],
                    "workflow_contract_draft_mismatch",
                    response.data,
                )
                self.assertEqual(ComfyWorkflowSource.objects.count(), before_sources)

    def test_qwen_and_h3_integer_contracts_are_browser_json_safe_and_importable(self):
        json_safe_integer = (1 << 53) - 1
        qwen_canvas = deepcopy(self.canvas)
        qwen_object_info = deepcopy(self._object_info())
        qwen_object_info["SaveImage"]["input"]["required"]["seed"][1]["max"] = (
            (1 << 64) - 1
        )

        h3_canvas = deepcopy(self.canvas)
        h3_sink = h3_canvas["nodes"][1]
        h3_sink["type"] = "SaveVideo"
        h3_sink["properties"]["Node name for S&R"] = "SaveVideo"
        h3_sink["inputs"].append({
            "name": "duration",
            "type": "FLOAT",
            "widget": {"name": "duration"},
            "link": None,
        })
        h3_sink["widgets_values"].append(5.0)
        h3_object_info = deepcopy(self._object_info())
        h3_object_info["SaveVideo"] = h3_object_info.pop("SaveImage")
        h3_object_info["SaveVideo"]["input"]["required"]["seed"][1]["max"] = (
            (1 << 64) - 1
        )
        h3_object_info["SaveVideo"]["input"]["required"]["duration"] = [
            "FLOAT",
            {
                "default": 5.0,
                "min": -((1 << 63) - 1),
                "max": (1 << 63) - 1,
            },
        ]
        h3_object_info["SaveVideo"]["input_order"]["required"].append("duration")

        cases = (
            ("qwen", qwen_canvas, qwen_object_info, "image", "saved/qwen-uint64.json"),
            ("h3", h3_canvas, h3_object_info, "video", "saved/h3-uint64.json"),
        )
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        )
        video_deployment = self._second_same_scope_deployment(
            key="browser-safe-video",
            modality=ModelProfileVersion.Modality.VIDEO,
            capabilities=("video.image_to_video",),
        )
        for label, canvas, object_info, output_kind, source_path in cases:
            with self.subTest(label=label):
                if output_kind == "video":
                    current = WorkflowProjectorVersion.objects.get(projector__key="generic.comfy.fixed", is_current=True)
                    definition = deepcopy(current.definition)
                    definition["projection"]["config"]["capability"] = "video.image_to_video"
                    self._publish_projector_successor(current, definition)
                target_profile = (
                    self.profile
                    if output_kind == "image"
                    else video_deployment.profile_version
                )
                driver = self._driver(canvas=canvas, object_info=object_info)
                with patch(
                    "studio.services.comfyui_workflow_imports.driver_for",
                    return_value=driver,
                ):
                    preview = self._post_managed_preview({
                        "source_path": source_path,
                        "expected_sha256": self.raw_sha,
                        **self._expected_source_scope_fields(),
                        "primary_output_node_ids": [123],
                        "model_profile_version": str(target_profile.id),
                    })
                self.assertEqual(preview.status_code, 200, preview.data)

                unsafe_paths = []

                def collect_unsafe_integers(value, path="$"):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            collect_unsafe_integers(child, f"{path}.{key}")
                    elif isinstance(value, list):
                        for index, child in enumerate(value):
                            collect_unsafe_integers(child, f"{path}[{index}]")
                    elif (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and abs(value) > json_safe_integer
                    ):
                        unsafe_paths.append(path)

                collect_unsafe_integers(preview.data["suggested_input_contract"])
                self.assertEqual(unsafe_paths, [])
                seed_definition = preview.data["suggested_input_contract"]["parameters"][
                    "sampling.seed"
                ]
                self.assertEqual(seed_definition["max"], json_safe_integer)
                if label == "h3":
                    duration_definition = preview.data["suggested_input_contract"][
                        "parameters"
                    ]["parameter.save_video.duration"]
                    self.assertEqual(duration_definition["min"], -json_safe_integer)
                    self.assertEqual(duration_definition["max"], json_safe_integer)

                item = {
                    "source_path": source_path,
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "import_kind": "template_version",
                    "primary_output_node_ids": [123],
                    "expected_object_info_schema_sha256": preview.data[
                        "object_info_schema_sha256"
                    ],
                    "expected_converted_graph_sha256": preview.data[
                        "converted_graph_sha256"
                    ],
                    "expected_parameter_schema_sha256": preview.data[
                        "parameter_schema_sha256"
                    ],
                    "expected_structural_contract_sha256": preview.data[
                        "structural_contract_sha256"
                    ],
                    "contract_draft": preview.data["contract_draft_id"],
                    "model_profile_version": str(target_profile.id),
                }
                if output_kind == "image":
                    item["template"] = str(self.template.id)
                else:
                    item["new_template"] = {
                        "name": "H3 browser-safe integer contract",
                        "slug": "h3-browser-safe-integer-contract",
                        "output_kind": "video",
                        "description": "",
                    }
                with patch(
                    "studio.services.comfyui_workflow_imports.driver_for",
                    return_value=driver,
                ):
                    imported = self.client.post(
                        batch_url,
                        {"items": [item]},
                        format="json",
                    )
                self.assertEqual(imported.status_code, 200, imported.data)
                self.assertEqual(imported.data["status"], "complete", imported.data)

    def test_preview_rejects_non_interoperable_integer_parameter_values(self):
        json_unsafe_integer = 1 << 53
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        )

        cases = []
        source_canvas = deepcopy(self.canvas)
        source_canvas["nodes"][1]["widgets_values"][3] = json_unsafe_integer
        source_object_info = deepcopy(self._object_info())
        source_object_info["SaveImage"]["input"]["required"]["seed"][1]["max"] = (
            (1 << 64) - 1
        )
        cases.append(("source", source_canvas, source_object_info, None))

        standard_source_canvas = deepcopy(self.canvas)
        standard_source_canvas["nodes"][1]["widgets_values"][1] = json_unsafe_integer
        standard_source_object_info = deepcopy(self._object_info())
        standard_source_object_info["SaveImage"]["input"]["required"]["width"][1][
            "max"
        ] = (1 << 64) - 1
        cases.append((
            "standard_source",
            standard_source_canvas,
            standard_source_object_info,
            None,
        ))

        default_object_info = deepcopy(self._object_info())
        default_object_info["SaveImage"]["input"]["required"]["seed"][1].update({
            "default": json_unsafe_integer,
            "max": (1 << 64) - 1,
        })
        cases.append(("default", self.canvas, default_object_info, None))

        options_object_info = deepcopy(self._object_info())
        options_object_info["SaveImage"]["input"]["required"]["seed"][1]["max"] = (
            (1 << 64) - 1
        )
        cases.append((
            "options",
            self.canvas,
            options_object_info,
            patch(
                "studio.services.comfyui_workflow_imports._safe_numeric_options",
                return_value=[1, json_unsafe_integer],
            ),
        ))

        recommended_object_info = deepcopy(self._object_info())
        recommended_object_info["SaveImage"]["input"]["required"]["seed"][1]["max"] = (
            (1 << 64) - 1
        )
        cases.append((
            "recommended",
            self.canvas,
            recommended_object_info,
            patch(
                "studio.services.comfyui_workflow_imports._recommended_parameter_value",
                return_value=json_unsafe_integer,
            ),
        ))

        for label, canvas, object_info, recommended_patch in cases:
            with self.subTest(label=label):
                driver = self._driver(canvas=canvas, object_info=object_info)
                with patch(
                    "studio.services.comfyui_workflow_imports.driver_for",
                    return_value=driver,
                ), recommended_patch or nullcontext():
                    response = self._post_managed_preview({
                        "source_path": f"saved/unsafe-{label}.json",
                        "expected_sha256": self.raw_sha,
                        **self._expected_source_scope_fields(),
                        "primary_output_node_ids": [123],
                        "model_profile_version": str(self.profile.id),
                    })
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["actionability"], "candidate_only")
                self.assertEqual(response.data["converted_graph_sha256"], "")
                self.assertEqual(response.data["parameter_schema_sha256"], "")
                self.assertEqual(response.data["structural_contract_sha256"], "")
                self.assertIn(
                    "workflow_import_failed",
                    {row["code"] for row in response.data["blockers"]},
                )
                self.assertTrue(response.data["contract_draft_id"])
                self.assertNotIn(str(json_unsafe_integer), str(response.data))

    def test_direct_import_rejects_dimension_multiple_contract_drift(self):
        object_info = self._object_info()
        converted = ui_workflow_to_api_graph(
            self.canvas,
            object_info,
            primary_output_node_ids=[123],
        )
        asset_dependencies = _asset_dependency_candidates(
            converted.api_graph,
            object_info,
        )
        (
            contract,
            bindings,
            requirements,
            blockers,
            _configurable_parameters,
        ) = _suggested_import_contract(
            converted.api_graph,
            object_info,
            primary_api_node_ids=list(converted.primary_output_node_ids),
            model_dependencies=[],
            asset_dependencies=asset_dependencies,
            model_profile_version=self.profile,
        )
        self.assertEqual(blockers, [])
        self.assertEqual(contract["required_dimension_multiple"], 1)

        invalid_schema = deepcopy(contract)
        invalid_schema["schema_version"] = "invalid"
        with self.assertRaisesMessage(
            ValidationError,
            "input_contract.schema_version 必须是正整数",
        ):
            import_workflow_version(
                template=self.template,
                graph=converted.api_graph,
                input_contract=invalid_schema,
                semantic_bindings=bindings,
                model_requirements=requirements,
                provider=self.provider,
                model_profile_version=self.profile,
            )

        lattice_contract = deepcopy(contract)
        lattice_contract["required_dimension_multiple"] = 16
        for key in ("image.width", "image.height"):
            lattice_contract["parameters"][key]["multiple_of"] = 16
        invalid_default = deepcopy(lattice_contract)
        invalid_default["parameters"]["image.width"]["default"] = 513
        with self.assertRaisesMessage(
            ValidationError,
            "冻结 default 不满足尺寸倍数合同",
        ):
            import_workflow_version(
                template=self.template,
                graph=converted.api_graph,
                input_contract=invalid_default,
                semantic_bindings=bindings,
                model_requirements=requirements,
                provider=self.provider,
                model_profile_version=self.profile,
            )

        invalid_graph = deepcopy(converted.api_graph)
        width_binding = next(
            row for row in bindings if row["semantic_key"] == "image.width"
        )
        invalid_graph[width_binding["node_id"]]["inputs"][
            width_binding["input_name"]
        ] = 513
        with self.assertRaisesMessage(
            ValidationError,
            "冻结 graph literal 不满足尺寸倍数合同",
        ):
            import_workflow_version(
                template=self.template,
                graph=invalid_graph,
                input_contract=lattice_contract,
                semantic_bindings=bindings,
                model_requirements=requirements,
                provider=self.provider,
                model_profile_version=self.profile,
            )

        invalid_contracts = []
        missing = deepcopy(contract)
        missing.pop("required_dimension_multiple")
        invalid_contracts.append(missing)
        drifted = deepcopy(contract)
        drifted["required_dimension_multiple"] = 8
        invalid_contracts.append(drifted)
        inconsistent_parameters = deepcopy(contract)
        inconsistent_parameters["parameters"]["image.height"]["multiple_of"] = 2
        invalid_contracts.append(inconsistent_parameters)

        for invalid in invalid_contracts:
            with self.subTest(invalid=invalid), self.assertRaisesMessage(
                ValidationError,
                "required_dimension_multiple",
            ):
                import_workflow_version(
                    template=self.template,
                    graph=converted.api_graph,
                    input_contract=invalid,
                    semantic_bindings=bindings,
                    model_requirements=requirements,
                    provider=self.provider,
                    model_profile_version=self.profile,
                )

        version = import_workflow_version(
            template=self.template,
            graph=converted.api_graph,
            input_contract=contract,
            semantic_bindings=bindings,
            model_requirements=requirements,
            provider=self.provider,
            model_profile_version=self.profile,
        )
        verify_workflow_version_integrity(version)
        lifecycle_drift = deepcopy(version.input_contract)
        lifecycle_drift["required_dimension_multiple"] = 8
        assign_json_contract(version, "input_contract", lifecycle_drift)
        version.save(update_fields=(
            "input_contract", "input_contract_schema_version",
            "input_contract_size_bytes", "input_contract_sha256", "updated_at",
        ))
        with self.assertRaisesMessage(ValidationError, "workflow execution contract hash"):
            verify_workflow_version_integrity(version)
        from studio.services.workflow_compiler import workflow_execution_contract
        _, version.execution_contract_sha256 = workflow_execution_contract(
            compiler_id=version.compiler_id, compiler_version=version.compiler_version,
            graph_sha256=version.graph_sha256, input_contract_sha256=version.input_contract_sha256,
            bindings=bindings, requirements=requirements,
        )
        version.save(update_fields=("execution_contract_sha256",))
        with self.assertRaisesMessage(
            ValidationError,
            "required_dimension_multiple",
        ):
            # Live validation and publish both call this lifecycle integrity
            # verifier before accepting a WorkflowTemplateVersion.
            verify_workflow_version_integrity(version)

    def test_preview_rejects_canvas_dimension_outside_frozen_int_lattice(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"][1]["widgets_values"][1] = 513
        object_info = deepcopy(self._object_info())
        for key in ("width", "height"):
            object_info["SaveImage"]["input"]["required"][key][1]["step"] = 16
        driver = self._driver(canvas=canvas, object_info=object_info)

        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            response = self._post_managed_preview(
                {
                    "source_path": "saved/invalid-dimension-lattice.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
            )
            blocked_import = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/"
                "saved-workflows/batch-import/",
                {"items": [{
                    "source_path": "saved/invalid-dimension-lattice.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "import_kind": "template_version",
                    "primary_output_node_ids": [123],
                    "expected_object_info_schema_sha256": response.data[
                        "object_info_schema_sha256"
                    ],
                    "expected_converted_graph_sha256": response.data[
                        "converted_graph_sha256"
                    ],
                    "expected_parameter_schema_sha256": response.data[
                        "parameter_schema_sha256"
                    ],
                    "expected_structural_contract_sha256": response.data[
                        "structural_contract_sha256"
                    ],
                    "contract_draft": response.data["contract_draft_id"],
                    "template": str(self.template.id),
                    "model_profile_version": str(self.profile.id),
                }]},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            "dimension_source_value_misaligned",
            {row["code"] for row in response.data["blockers"]},
        )
        self.assertEqual(blocked_import.status_code, 200, blocked_import.data)
        self.assertEqual(blocked_import.data["failed_count"], 1)
        self.assertEqual(
            blocked_import.data["items"][0]["code"],
            "workflow_contract_blocked",
        )
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(),
            self.initial_workflow_version_count,
        )
        converted = ui_workflow_to_api_graph(
            canvas,
            object_info,
            primary_output_node_ids=[123],
        )
        with self.assertRaisesMessage(
            ValidationError,
            "尺寸倍数合同",
        ):
            import_workflow_version(
                template=self.template,
                graph=converted.api_graph,
                input_contract=response.data["suggested_input_contract"],
                semantic_bindings=response.data["suggested_semantic_bindings"],
                model_requirements=response.data["suggested_model_requirements"],
                provider=self.provider,
                model_profile_version=self.profile,
            )

    def test_source_image_derived_import_freezes_registered_size_path(self):
        object_info = deepcopy(self._object_info())
        object_info["FluxKontextImageScale"] = {
            "input": {"required": {"image": ["IMAGE", {}]}},
            "input_order": {"required": ["image"]},
            "output": ["IMAGE"],
            "output_name": ["IMAGE"],
        }
        graph = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": "smoke.png"},
            },
            "2": {
                "class_type": "FluxKontextImageScale",
                "inputs": {"image": ["1", 0]},
            },
            "3": {
                "class_type": "SaveImage",
                "inputs": {
                    "images": ["2", 0],
                    "filename_prefix": "saved/output",
                },
            },
        }
        asset_dependencies = _asset_dependency_candidates(graph, object_info)
        contract, bindings, requirements, blockers, _parameters = (
            _suggested_import_contract(
                graph,
                object_info,
                primary_api_node_ids=["3"],
                model_dependencies=[],
                asset_dependencies=asset_dependencies,
                model_profile_version=self.profile,
            )
        )

        self.assertEqual(
            contract["resolution_strategy"]["mode"],
            "source_image_derived",
        )
        self.assertEqual(contract["required_dimension_multiple"], 16)
        self.assertEqual(contract["resolution_strategy"]["size_path"], ["3", "2", "1"])
        self.assertNotIn("image.width", contract["parameters"])
        self.assertNotIn(
            "resolution_execution_adapter_unavailable",
            {row["code"] for row in blockers},
        )
        version = import_workflow_version(
            template=self.template, graph=graph, input_contract=contract,
            semantic_bindings=bindings, model_requirements=requirements,
            provider=self.provider, model_profile_version=self.profile,
        )
        self.assertEqual(version.input_contract["resolution_strategy"], contract["resolution_strategy"])
        incomplete = deepcopy(contract)
        incomplete["resolution_strategy"].pop("adapter")
        with self.assertRaisesMessage(
            ValidationError,
            "source_image_derived",
        ):
            import_workflow_version(
                template=self.template,
                graph=graph,
                input_contract=incomplete,
                semantic_bindings=bindings,
                model_requirements=requirements,
                provider=self.provider,
                model_profile_version=self.profile,
            )

    def test_preview_contract_round_trips_through_strict_generic_import(self):
        driver = self._driver()
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            self._admit_candidate()
            preview = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            }, format="json")
            self.assertEqual(preview.status_code, 200, preview.data)
            self.assertEqual(
                preview.data["suggested_input_contract"]["workflow_mode"],
                "semantic",
            )
            imported = self.client.post(batch_url, {"items": [{
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": [123],
                "expected_object_info_schema_sha256": preview.data["object_info_schema_sha256"],
                "expected_converted_graph_sha256": preview.data["converted_graph_sha256"],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(self.template.id),
                "model_profile_version": str(self.profile.id),
            }]}, format="json")

        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["status"], "complete", imported.data)
        self.assertEqual(imported.data["created_count"], 1)
        version = WorkflowTemplateVersion.objects.get(
            pk=imported.data["items"][0]["workflow_version_id"],
        )
        self.assertEqual(version.input_contract["workflow_mode"], "semantic")
        self.assertEqual(
            version.input_contract["resolution_strategy"]["mode"],
            "explicit_width_height",
        )
        self.assertEqual(version.input_contract["required_dimension_multiple"], 1)
        self.assertEqual(
            {
                version.input_contract["parameters"][key]["multiple_of"]
                for key in ("image.width", "image.height")
            },
            {1},
        )
        self.assertEqual(
            {
                (row.semantic_key, row.node_id, row.input_name)
                for row in version.semantic_bindings.all()
                if row.semantic_key in {"image.width", "image.height"}
            },
            {("image.width", "2", "width"), ("image.height", "2", "height")},
        )
        fixture = normalize_workflow_execution_inputs(version, {
            "schema_version": 2,
            "values": {},
            "assets": [{
                "semantic_key": "asset.image.source",
                "asset_id": str(self.smoke_asset.id),
            }],
        }, owner=self.admin)
        source = fixture.asset_facts[0]
        compiled = compile_frozen_workflow_values(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            scalar_values={**fixture.values, "output.prefix": "test/explicit-source-dimensions"},
            resolve_asset=lambda key, _rule: (f"__AUTOPROMPT_ASSET__:{source['asset_id']}", source)
                if key == source["semantic_key"] else self.fail("Undeclared media input"),
            classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None,
        )
        self.assertEqual(
            compiled.graph["1"]["inputs"]["image"],
            f"__AUTOPROMPT_ASSET__:{self.smoke_asset.id}",
        )
        self.assertEqual(compiled.asset_bindings[0]["input_slot"], 1)

    def test_saved_workflow_requests_have_item_byte_and_time_budgets(self):
        scan_url = "/api/v1/comfy-workflow-sources/discover/"
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )

        scan_max_items = saved_workflow_scan_max_items()
        too_many_driver = self._driver(paths=[
            f"saved/{index}.json" for index in range(scan_max_items + 1)
        ])
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=too_many_driver,
        ):
            too_many_scan = self.client.get(scan_url)
        self.assertEqual(too_many_scan.status_code, 400, too_many_scan.data)
        self.assertEqual(
            too_many_scan.data["error"]["code"],
            "workflow_request_budget_exceeded",
        )
        self.assertIn(f"最多读取 {scan_max_items}", str(too_many_scan.data))
        self.assertEqual(too_many_driver.invoke.call_count, 1)

        byte_driver = self._driver(paths=["saved/one.json", "saved/two.json"])
        with (
            patch(
                "studio.services.comfyui_workflow_imports.driver_for",
                return_value=byte_driver,
            ),
            patch(
                "studio.services.comfyui_workflow_imports.saved_workflow_request_max_bytes",
                return_value=1500,
            ),
        ):
            too_many_bytes = self.client.get(scan_url)
        self.assertEqual(too_many_bytes.status_code, 400, too_many_bytes.data)
        self.assertEqual(
            too_many_bytes.data["error"]["code"],
            "workflow_request_budget_exceeded",
        )
        self.assertIn("读取总量", str(too_many_bytes.data))

        time_driver = self._driver()
        scan_max_seconds = saved_workflow_scan_max_seconds()
        with (
            patch(
                "studio.services.comfyui_workflow_imports.driver_for",
                return_value=time_driver,
            ),
            patch(
                "studio.services.comfyui_workflow_imports.time.monotonic",
                side_effect=[0.0, 0.0, scan_max_seconds + 1.0],
            ),
        ):
            timed_out = self.client.get(scan_url)
        self.assertEqual(timed_out.status_code, 400, timed_out.data)
        self.assertEqual(
            timed_out.data["error"]["code"],
            "workflow_request_budget_exceeded",
        )
        self.assertIn("同步请求时限", str(timed_out.data))

        batch_items = [{
            "source_path": f"saved/{index}.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "ignored",
        } for index in range(21)]
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            too_many_batch = self.client.post(
                batch_url,
                {"items": batch_items},
                format="json",
            )
        self.assertEqual(too_many_batch.status_code, 400, too_many_batch.data)
        self.assertIn("20", str(too_many_batch.data))
        lookup.assert_not_called()

    def test_saved_workflow_transport_timeouts_follow_remaining_request_budget(self):
        scan_url = "/api/v1/comfy-workflow-sources/discover/"
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/preview/"
        )
        clock = {"value": 0.0}
        driver = self._driver()
        original_invoke = driver.invoke.side_effect
        observations = []

        def invoke(deployment, operation, payload):
            observations.append((operation, payload.get("_transport_timeout_seconds")))
            result = original_invoke(deployment, operation, payload)
            clock["value"] += 4.0
            return result

        driver.invoke.side_effect = invoke
        with (
            patch(
                "studio.services.comfyui_workflow_imports.driver_for",
                return_value=driver,
            ),
            patch(
                "studio.services.comfyui_workflow_imports.time.monotonic",
                side_effect=lambda: clock["value"],
            ),
        ):
            scan = self.client.get(scan_url)
        self.assertEqual(scan.status_code, 200, scan.data)
        self.assertEqual([row[0] for row in observations], [
            "workflow_library.list", "object_info", "workflow_library.read",
        ])
        self.assertEqual(observations[0][1], 30.0)
        self.assertLessEqual(observations[1][1], 26.0)

        observations.clear()
        clock["value"] = 0.0
        driver = self._driver()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            self._admit_candidate()
        driver.reset_mock()
        original_invoke = driver.invoke.side_effect

        def preview_invoke(deployment, operation, payload):
            observations.append((operation, payload.get("_transport_timeout_seconds")))
            result = original_invoke(deployment, operation, payload)
            clock["value"] += 4.0
            return result

        driver.invoke.side_effect = preview_invoke
        with (
            patch(
                "studio.services.comfyui_workflow_imports.driver_for",
                return_value=driver,
            ),
            patch(
                "studio.services.comfyui_workflow_imports.time.monotonic",
                side_effect=lambda: clock["value"],
            ),
        ):
            preview = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [],
                "model_profile_version": str(self.profile.id),
            }, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual([row[0] for row in observations], ["object_info"])
        self.assertEqual(observations[0][1], 30.0)

    def test_comfy_driver_never_allows_request_budget_to_increase_configured_timeout(self):
        from studio.services.provider_registry import driver_for

        driver = driver_for(self.provider)
        comfy_client = Mock()
        comfy_client.object_info.return_value = {}
        with (
            patch(
                "studio.services.provider_registry.validate_endpoint",
                return_value="http://127.0.0.1:8188",
            ),
            patch(
                "studio.services.comfyui.ComfyUIClient",
                return_value=comfy_client,
            ) as client_factory,
        ):
            driver.invoke(
                self.deployment, "object_info", {"_transport_timeout_seconds": 3.0},
            )
            self.assertEqual(client_factory.call_args.kwargs["timeout"], 3.0)
            driver.invoke(
                self.deployment, "object_info", {"_transport_timeout_seconds": 20.0},
            )
            self.assertEqual(client_factory.call_args.kwargs["timeout"], 10.0)

    def test_saved_workflow_provider_failure_is_explicit_and_old_scan_is_404(self):
        url = "/api/v1/comfy-workflow-sources/discover/"
        driver = self._driver()
        driver.invoke.side_effect = ProviderDriverError(
            "provider library unavailable",
            code="provider_library_unavailable",
            retryable=True,
        )
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            failed = self.client.get(url)
        self.assertEqual(failed.status_code, 200, failed.data)
        self.assertEqual(failed.data["status"], "failed")
        self.assertEqual(
            failed.data["sources"][0]["error_code"],
            "provider_library_unavailable",
        )

        missing = self.client.get(
            "/api/v1/model-deployments/00000000-0000-4000-8000-000000000000/"
            "saved-workflows/",
        )
        self.assertEqual(missing.status_code, 404, missing.content)

    def test_preview_structurally_reports_stale_local_asset_without_disclosing_name(self):
        object_info = self._object_info()
        object_info["LoadImage"]["input"]["required"]["image"][0] = ["server-only.png"]
        driver = self._driver(object_info=object_info)

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self._post_managed_preview(
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["asset_dependencies"], [{
            "node_id": "1",
            "node_type": "LoadImage",
            "input_name": "image",
            "media_kind": "image",
            "value_type": "asset_image",
            "suggested_semantic_key": "asset.image.source",
            "input_slot": 1,
            "asset_category": "image",
            "is_required": True,
            "min_items": 1,
            "max_items": 1,
        }])
        self.assertEqual(response.data["blockers"], [])
        self.assertNotIn("smoke.png", str(response.data))

    def test_preview_rejects_secret_before_live_schema_and_never_echoes_literal(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"][1]["inputs"].append({
            "name": "api_key",
            "type": "STRING",
            "widget": {"name": "api_key"},
            "link": None,
        })
        canvas["nodes"][1]["widgets_values"].append("do-not-echo-secret")
        driver = self._driver(canvas=canvas)

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                {"items": [{
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "import_kind": "candidate",
                }]},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["items"][0]["code"], "secret_field_forbidden")
        self.assertNotIn("do-not-echo-secret", str(response.data))
        self.assertNotIn("api_key", str(response.data))
        self.assertFalse(any(
            call.args[1] == "object_info" for call in driver.invoke.call_args_list
        ))

    def test_preview_live_contract_error_never_echoes_canvas_enum_literal(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"][1]["inputs"].append({
            "name": "format",
            "type": "COMBO",
            "widget": {"name": "format"},
            "link": None,
        })
        canvas["nodes"][1]["widgets_values"].append("do-not-echo-enum")
        object_info = deepcopy(self._object_info())
        object_info["SaveImage"]["input"]["required"]["format"] = [
            ["png", "jpeg"], {"default": "png"},
        ]
        object_info["SaveImage"]["input_order"]["required"].append("format")
        driver = self._driver(canvas=canvas, object_info=object_info)

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self._post_managed_preview(
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "model_profile_version": str(self.profile.id),
                },
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["actionability"], "candidate_only")
        self.assertTrue(response.data["blockers"])
        self.assertNotIn("do-not-echo-enum", str(response.data))

    def test_saved_workflow_diagnostics_never_echo_root_node_type_or_node_id_values(self):
        root_sentinel = "ROOT-WORKFLOW-ID-SENTINEL"
        root_canvas = deepcopy(self.canvas)
        root_canvas["id"] = root_sentinel
        root_driver = self._driver(canvas=root_canvas)
        scan_url = "/api/v1/comfy-workflow-sources/discover/"
        preview_url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=root_driver,
        ):
            scanned = self.client.get(scan_url)
            self._admit_candidate()
            previewed = self.client.post(preview_url, {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [],
            }, format="json")
        self.assertEqual(scanned.status_code, 200, scanned.data)
        self.assertEqual(previewed.status_code, 200, previewed.data)
        self.assertNotIn(root_sentinel, str(scanned.data))
        self.assertNotIn(root_sentinel, str(previewed.data))
        self.assertEqual(len(scanned.data["items"][0]["source_workflow_id"]), 64)
        self.assertEqual(len(previewed.data["source_workflow_id"]), 64)

        type_sentinel = "NODE-TYPE-SENTINEL"
        type_canvas = deepcopy(self.canvas)
        type_canvas["nodes"][1]["type"] = type_sentinel
        type_driver = self._driver(canvas=type_canvas)
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=type_driver,
        ):
            rejected_type = self._post_managed_preview({
                "source_path": "saved/type-sentinel.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
            })
        self.assertEqual(rejected_type.status_code, 200, rejected_type.data)
        self.assertEqual(rejected_type.data["actionability"], "candidate_only")
        self.assertNotIn(type_sentinel, str(rejected_type.data))

        id_sentinel = "NODE-ID-SENTINEL"
        id_canvas = deepcopy(self.canvas)
        id_canvas["nodes"][0]["id"] = id_sentinel
        id_canvas["links"][0][1] = id_sentinel
        id_driver = self._driver(canvas=id_canvas)
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=id_driver,
        ):
            rejected_id = self._post_managed_preview({
                "source_path": "saved/id-sentinel.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
            })
        self.assertEqual(rejected_id.status_code, 200, rejected_id.data)
        self.assertEqual(rejected_id.data["actionability"], "candidate_only")
        self.assertNotIn(id_sentinel, str(rejected_id.data))

        rejected_request_id = self.client.post(preview_url, {
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "primary_output_node_ids": [id_sentinel],
        }, format="json")
        self.assertEqual(rejected_request_id.status_code, 400, rejected_request_id.data)
        self.assertNotIn(id_sentinel, str(rejected_request_id.data))

    def test_draft_model_artifact_attestation_is_durable_and_idempotent(self):
        draft = ModelProfileVersion(
            profile_key="saved-import-draft",
            version=1,
            provider=self.provider,
            model_identifier="saved-import-draft",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft, "specification", {"schema_version": 1})
        draft.save()
        canvas = deepcopy(self.canvas)
        canvas["nodes"].insert(1, {
            "id": 2,
            "type": "UNETLoader",
            "pos": [0, 200],
            "size": [240, 120],
            "flags": {},
            "order": 1,
            "mode": 0,
            "inputs": [
                {"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}},
                {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}},
            ],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [2]}],
            "properties": {"Node name for S&R": "UNETLoader"},
            "widgets_values": ["qwen\\model.safetensors", "default"],
        })
        canvas["nodes"][2]["inputs"].append({
            "name": "model", "type": "MODEL", "link": 2,
        })
        canvas["links"].append([2, 2, 0, 123, 5, "MODEL"])
        object_info = self._object_info()
        object_info["UNETLoader"] = {
            "input": {"required": {
                "unet_name": [["qwen\\model.safetensors"], {}],
                "weight_dtype": [["default"], {}],
            }},
            "input_order": {"required": ["unet_name", "weight_dtype"]},
            "output": ["MODEL"],
            "output_name": ["MODEL"],
        }
        object_info["SaveImage"]["input"]["optional"] = {"model": ["MODEL", {}]}
        object_info["SaveImage"]["input_order"]["optional"] = ["model"]
        driver = self._driver(canvas=canvas, object_info=object_info)
        converted = ui_workflow_to_api_graph(
            canvas, object_info, primary_output_node_ids=[123],
        )
        request_payload = {
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": _object_info_sha256(object_info),
            "expected_converted_graph_sha256": _converted_graph_sha256(converted.api_graph),
            "model_profile_version": str(draft.id),
        }
        existing_artifact = ModelArtifact.objects.create(
            profile_version=draft,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="existing-default-folder-model",
            filename="qwen/model.safetensors",
            inventory_folder="",
            inventory_node_type="",
            inventory_input_name="",
            status=ModelArtifact.Status.DECLARED,
        )
        url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/attest-model-artifacts/"
        )
        other_draft = ModelProfileVersion(
            profile_key="saved-import-other-draft",
            version=1,
            provider=self.provider,
            model_identifier="saved-import-other-draft",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(other_draft, "specification", {"schema_version": 1})
        other_draft.save()

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, request_payload, format="json")
            second = self.client.post(url, request_payload, format="json")
            conflict_payload = {
                **request_payload,
                "model_profile_version": str(other_draft.id),
            }
            conflict = self.client.post(url, conflict_payload, format="json")

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertTrue(first["Location"].endswith(
            f"/api/v1/runtime-host-probe-attempts/{first.data['id']}/",
        ))
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(
            conflict.data["error"]["code"],
            "draft_artifact_attestation_conflict",
        )
        attempt = RuntimeHostProbeAttempt.objects.get(pk=first.data["id"])
        self.assertEqual(
            attempt.probe_kind,
            RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
        )
        self.assertEqual(attempt.request_snapshot_sha256, first.data["request_snapshot_sha256"])
        artifact = ModelArtifact.objects.get(profile_version=draft)
        self.assertEqual(artifact.id, existing_artifact.id)
        self.assertEqual(artifact.filename, "qwen/model.safetensors")
        self.assertEqual(artifact.inventory_folder, "")
        self.assertEqual(artifact.status, ModelArtifact.Status.DECLARED)
        self.assertEqual(artifact.sha256, "")
        self.assertFalse(ModelArtifact.objects.filter(profile_version=other_draft).exists())

    def test_attestation_uses_value_free_canvas_preflight_before_any_write(self):
        draft = ModelProfileVersion(
            profile_key="attestation-preflight-draft",
            version=1,
            provider=self.provider,
            model_identifier="attestation-preflight-draft",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft, "specification", {"schema_version": 1})
        draft.save()
        url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/attest-model-artifacts/"
        )
        baseline_artifacts = ModelArtifact.objects.count()
        baseline_attempts = RuntimeHostProbeAttempt.objects.count()

        secret_sentinel = "ATTEST-SECRET-SENTINEL"
        secret_canvas = deepcopy(self.canvas)
        secret_canvas["nodes"].append({
            "id": 999,
            "type": "UnreachableConfig",
            "pos": [0, 400],
            "size": [200, 100],
            "flags": {},
            "order": 99,
            "mode": 0,
            "inputs": [{
                "name": "api_key",
                "type": "STRING",
                "widget": {"name": "api_key"},
                "link": None,
            }],
            "outputs": [],
            "widgets_values": [secret_sentinel],
        })
        request_payload = {
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": "a" * 64,
            "expected_converted_graph_sha256": "b" * 64,
            "model_profile_version": str(draft.id),
        }
        secret_driver = self._driver(canvas=secret_canvas)
        with (
            patch("studio.services.comfyui_workflow_imports.driver_for", return_value=secret_driver),
            patch("studio.services.draft_model_artifact_attestation.logger.warning") as warning,
        ):
            secret = self.client.post(url, request_payload, format="json")
        self.assertEqual(secret.status_code, 400, secret.data)
        self.assertEqual(secret.data["error"]["code"], "secret_field_forbidden")
        self.assertNotIn(secret_sentinel, str(secret.data))
        self.assertNotIn(secret_sentinel, str(warning.call_args_list))
        self.assertFalse(any(
            call.args[1] == "object_info" for call in secret_driver.invoke.call_args_list
        ))

        type_sentinel = "ATTEST-NODE-TYPE-SENTINEL"
        type_canvas = deepcopy(self.canvas)
        type_canvas["nodes"][1]["type"] = type_sentinel
        type_driver = self._driver(canvas=type_canvas)
        with (
            patch("studio.services.comfyui_workflow_imports.driver_for", return_value=type_driver),
            patch("studio.services.draft_model_artifact_attestation.logger.warning") as warning,
        ):
            invalid = self.client.post(url, request_payload, format="json")
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertEqual(invalid.data["error"]["code"], "workflow_import_failed")
        self.assertNotIn(type_sentinel, str(invalid.data))
        self.assertNotIn(type_sentinel, str(warning.call_args_list))
        self.assertEqual(ModelArtifact.objects.count(), baseline_artifacts)
        self.assertEqual(RuntimeHostProbeAttempt.objects.count(), baseline_attempts)

    def test_attestation_worker_hashes_controlled_file_and_fills_existing_evidence_models(self):
        # Reuse the API-level fixture above; each Django test has its own
        # transaction, so this creates exactly one queued attestation here.
        self.test_draft_model_artifact_attestation_is_durable_and_idempotent()
        attempt = RuntimeHostProbeAttempt.objects.get(
            probe_kind=RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
        )
        artifact = ModelArtifact.objects.get(
            profile_version_id=attempt.request_snapshot["target_model_profile_version_id"],
        )
        with self.assertRaises(Exception):
            validate_draft_profile_artifacts_for_publish(artifact.profile_version)

        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output_dir = comfy_root / "output"
            model_file = comfy_root / "models" / "diffusion_models" / "qwen" / "model.safetensors"
            output_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_bytes = b"attested-qwen-model-bytes"
            model_file.write_bytes(model_bytes)

            driver = Mock()

            def invoke(_deployment, operation, payload):
                if operation == "health":
                    return {"system": {"comfyui_version": "test"}}
                if operation == "object_info":
                    return {
                        "UNETLoader": {"input": {"required": {
                            "unet_name": [[artifact.filename], {}],
                        }}},
                    }
                if operation == "models":
                    return {"items": [artifact.filename]}
                raise AssertionError(operation)

            driver.invoke.side_effect = invoke
            claimed = claim_next_runtime_host_probe("draft-attestation-worker")
            self.assertEqual(claimed.id, attempt.id)
            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=driver,
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ):
                result = process_runtime_host_probe(claimed)

        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.SUCCEEDED)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, ModelArtifact.Status.AVAILABLE)
        self.assertEqual(artifact.size_bytes, len(model_bytes))
        self.assertEqual(artifact.sha256, sha256(model_bytes).hexdigest())
        self.assertEqual(artifact.verified_snapshot_id, result.final_snapshot.id)
        availability = RuntimeModelAvailability.objects.get(
            snapshot=artifact.verified_snapshot,
            artifact=artifact,
        )
        self.assertEqual(
            availability.observation_source,
            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        self.assertEqual(availability.observed_sha256, artifact.sha256)
        validate_draft_profile_artifacts_for_publish(artifact.profile_version)

        # A newer ordinary capability snapshot must not invalidate the exact,
        # still-fresh attestation evidence attached to the artifact.
        capability_attempt = RuntimeHostProbeAttempt(
            host=self.host,
            created_by=self.admin,
            probe_kind=RuntimeHostProbeAttempt.ProbeKind.CAPABILITY,
            status=RuntimeHostProbeAttempt.Status.SUCCEEDED,
            finished_at=timezone.now(),
        )
        assign_json_contract(capability_attempt, "request_snapshot", {
            "schema_version": 1,
            "probe_kind": RuntimeHostProbeAttempt.ProbeKind.CAPABILITY,
            "host_id": str(self.host.id),
        })
        capability_attempt.save()
        newer = HostCapabilitySnapshot(
            host=self.host,
            probe_attempt=capability_attempt,
            revision=result.final_snapshot.revision + 1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        assign_json_contract(newer, "details", {"schema_version": 1})
        newer.save()
        validate_draft_profile_artifacts_for_publish(artifact.profile_version)

        # The evidence remains bound to the frozen source deployment contract;
        # changing the endpoint cannot silently reuse an old local hash.
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8288",
            "timeout_seconds": 10,
        })
        self.deployment.save(update_fields=(
            "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))
        with self.assertRaises(ValidationError):
            validate_draft_profile_artifacts_for_publish(artifact.profile_version)

    def test_later_attestation_reverifies_the_entire_shared_draft_profile_closure(self):
        draft = ModelProfileVersion(
            profile_key="shared-attestation-draft",
            version=1,
            provider=self.provider,
            model_identifier="shared-attestation-draft",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft, "specification", {"schema_version": 1})
        draft.save()

        def intent(filename: str, ordinal: int):
            source_scope = comfyui_workflow_source_scope(self.deployment)
            return {
                "schema_version": 1,
                "probe_kind": (
                    RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
                ),
                "host_id": str(self.host.id),
                "source_deployment_id": str(self.deployment.id),
                "source_deployment_fingerprint_sha256": (
                    source_deployment_fingerprint(self.deployment)
                ),
                "source_scope_schema_version": source_scope.schema_version,
                "source_scope_sha256": source_scope.sha256,
                "source_path": f"saved/shared-{ordinal}.json",
                "source_sha256": str(ordinal) * 64,
                "object_info_schema_sha256": str(ordinal + 2) * 64,
                "converted_graph_sha256": str(ordinal + 4) * 64,
                "primary_output_node_ids": [str(ordinal)],
                "target_model_profile_version_id": str(draft.id),
                "target_profile_fingerprint_sha256": (
                    draft_model_profile_fingerprint(draft)
                ),
                "dependencies": [{
                    "role": ModelArtifact.Role.DIFFUSION_MODEL,
                    "filename": filename,
                    "inventory_folder": "diffusion_models",
                    "inventory_node_type": "",
                    "inventory_input_name": "",
                }],
            }

        first_name = "qwen/shared-first.safetensors"
        second_name = "qwen/shared-second.safetensors"
        first_attempt, created = _create_or_reuse_attempt(
            source=self.deployment,
            target=draft,
            intent=intent(first_name, 1),
            actor=self.admin,
        )
        self.assertTrue(created)
        self.assertEqual(len(first_attempt.request_snapshot["artifacts"]), 1)
        self.assertEqual(
            first_attempt.request_snapshot["dependencies"][0]["inventory_folder"],
            "diffusion_models",
        )

        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output_dir = comfy_root / "output"
            models_root = comfy_root / "models" / "diffusion_models" / "qwen"
            output_dir.mkdir(parents=True)
            models_root.mkdir(parents=True)
            (models_root / "shared-first.safetensors").write_bytes(b"first-model")
            (models_root / "shared-second.safetensors").write_bytes(b"second-model")

            claimed = claim_next_runtime_host_probe("shared-attestation-first")
            self.assertEqual(claimed.id, first_attempt.id)
            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=self._runtime_driver(model_names=(first_name,)),
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ):
                first_result = process_runtime_host_probe(claimed)
            self.assertEqual(
                first_result.status,
                RuntimeHostProbeAttempt.Status.SUCCEEDED,
            )
            first_artifact = draft.artifacts.get(filename=first_name)
            first_snapshot_id = first_artifact.verified_snapshot_id
            (models_root / "shared-first.safetensors").write_bytes(
                b"first-model-replaced-after-proof",
            )

            second_attempt, created = _create_or_reuse_attempt(
                source=self.deployment,
                target=draft,
                intent=intent(second_name, 2),
                actor=self.admin,
            )
            self.assertTrue(created)
            frozen_ids = {
                row["artifact_id"] for row in second_attempt.request_snapshot["artifacts"]
            }
            self.assertEqual(
                frozen_ids,
                {str(item.id) for item in draft.artifacts.all()},
            )
            self.assertEqual(len(frozen_ids), 2)

            claimed = claim_next_runtime_host_probe("shared-attestation-second")
            self.assertEqual(claimed.id, second_attempt.id)
            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=self._runtime_driver(model_names=(first_name, second_name)),
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ):
                second_result = process_runtime_host_probe(claimed)

            # The changed A bytes make the A+B closure incomplete.  No
            # artifact is partially advanced to the new snapshot and publish
            # remains blocked until a complete closure is proved again.
            self.assertEqual(
                second_result.status,
                RuntimeHostProbeAttempt.Status.FAILED,
                (second_result.error_code, second_result.error_message),
            )
            self.assertEqual(
                second_result.error_code,
                "draft_artifact_attestation_failed",
            )
            first_artifact.refresh_from_db()
            second_artifact = draft.artifacts.get(filename=second_name)
            self.assertEqual(first_artifact.verified_snapshot_id, first_snapshot_id)
            self.assertIsNone(second_artifact.verified_snapshot_id)
            with self.assertRaises(ValidationError):
                validate_draft_profile_artifacts_for_publish(draft)

            # Verified artifact history is preserved.  Updating the draft
            # declaration invalidates the old verification and makes the
            # complete closure eligible for a fresh attestation without
            # deleting the registered artifact or its prior snapshot.
            reset_declaration = self.client.patch(
                f"/api/v1/model-artifacts/{first_artifact.id}/",
                {"sha256": "", "size_bytes": None},
                format="json",
            )
            self.assertEqual(
                reset_declaration.status_code,
                200,
                reset_declaration.data,
            )
            first_artifact.refresh_from_db()
            self.assertEqual(first_artifact.status, ModelArtifact.Status.DECLARED)
            self.assertIsNone(first_artifact.verified_snapshot_id)
            self.assertEqual(first_artifact.sha256, "")
            self.assertIsNone(first_artifact.size_bytes)
            third_attempt, created = _create_or_reuse_attempt(
                source=self.deployment,
                target=draft,
                intent=intent(first_name, 3),
                actor=self.admin,
            )
            self.assertTrue(created)
            self.assertEqual(len(third_attempt.request_snapshot["artifacts"]), 2)
            claimed = claim_next_runtime_host_probe("shared-attestation-complete")
            self.assertEqual(claimed.id, third_attempt.id)
            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=self._runtime_driver(model_names=(first_name, second_name)),
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ):
                complete_result = process_runtime_host_probe(claimed)

        self.assertEqual(
            complete_result.status,
            RuntimeHostProbeAttempt.Status.SUCCEEDED,
        )
        artifacts = list(draft.artifacts.order_by("pk"))
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            {item.verified_snapshot_id for item in artifacts},
            {complete_result.final_snapshot.id},
        )
        self.assertEqual(
            RuntimeModelAvailability.objects.filter(
                snapshot_id=complete_result.final_snapshot.id,
                artifact__in=artifacts,
                observation_source=(
                    RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
                ),
            ).count(),
            2,
        )
        validate_draft_profile_artifacts_for_publish(draft)

        with patch(
            "studio.platform_views.validate_version_configuration_defaults",
        ), patch(
            "studio.platform_views.validate_async_model_profile",
        ), patch(
            "studio.platform_views.validate_prompt_normalization_model_profile",
        ), patch(
            "studio.platform_views.validate_script_generation_model_profile",
        ), patch(
            "studio.platform_views.validate_script_structure_model_profile",
        ):
            published = self.client.post(
                f"/api/v1/model-profile-versions/{draft.id}/publish/",
                {},
                format="json",
            )
        self.assertEqual(published.status_code, 200, published.data)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ModelProfileVersion.Status.PUBLISHED)

        # Rebinding the loopback deployment after publication demonstrates
        # that the latest single snapshot contains readiness evidence for both
        # artifacts, rather than two incompatible per-workflow closures.
        self.deployment.profile_version = draft
        self.deployment.save(update_fields=("profile_version", "updated_at"))
        for artifact in artifacts:
            verified_artifact_availability(
                snapshot=complete_result.final_snapshot,
                deployment=self.deployment,
                artifact=artifact,
            )

    def test_attestation_commit_fence_rejects_target_profile_drift_without_snapshot(self):
        self.test_draft_model_artifact_attestation_is_durable_and_idempotent()
        attempt = RuntimeHostProbeAttempt.objects.get(
            probe_kind=RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
        )
        artifact = ModelArtifact.objects.get(
            profile_version_id=attempt.request_snapshot["target_model_profile_version_id"],
        )
        claimed = claim_next_runtime_host_probe("draft-attestation-drift-worker")

        def drift_before_commit(
            _host,
            *,
            lease_renew,
            probe_attempt,
            commit_guard,
            attestation_context,
        ):
            lease_renew()
            ModelProfileVersion.objects.filter(
                pk=attestation_context["target_profile"].pk,
            ).update(status=ModelProfileVersion.Status.PUBLISHED)
            commit_guard()
            raise AssertionError("drifted attestation must not reach snapshot commit")

        with patch(
            "studio.services.runtime_host_probe_queue.probe_runtime_host",
            side_effect=drift_before_commit,
        ):
            result = process_runtime_host_probe(claimed)

        self.assertEqual(result.status, RuntimeHostProbeAttempt.Status.FAILED)
        self.assertEqual(result.error_code, "runtime_host_state_changed")
        self.assertFalse(HostCapabilitySnapshot.objects.filter(probe_attempt=attempt).exists())
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, ModelArtifact.Status.DECLARED)
        self.assertEqual(artifact.sha256, "")

    @override_settings(AI_PROVIDER_ALLOWED_HOSTS=("remote-comfy.example.test",))
    def test_attestation_rejects_remote_source_and_model_path_escape_before_write(self):
        for unsafe in ("../escape.safetensors", "C:\\models\\escape.safetensors", "/abs/model"):
            with self.subTest(unsafe=unsafe), self.assertRaises(Exception):
                _safe_filename(unsafe)

        draft = ModelProfileVersion(
            profile_key="remote-attestation-draft",
            version=1,
            provider=self.provider,
            model_identifier="remote-attestation-draft",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(draft, "specification", {"schema_version": 1})
        draft.save()
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "https://remote-comfy.example.test",
            "timeout_seconds": 10,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3600,
            "capability_ttl_seconds": 3600,
        })
        self.deployment.save(update_fields=(
            "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))
        expected_scope = self._expected_source_scope_fields()
        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/"
                "saved-workflows/attest-model-artifacts/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **expected_scope,
                    "primary_output_node_ids": [123],
                    "expected_object_info_schema_sha256": "a" * 64,
                    "expected_converted_graph_sha256": "b" * 64,
                    "model_profile_version": str(draft.id),
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        lookup.assert_not_called()
        self.assertFalse(RuntimeHostProbeAttempt.objects.exists())
        self.assertFalse(ModelArtifact.objects.filter(profile_version=draft).exists())

    def test_attestation_rejects_mixed_local_and_remote_comfy_host_scope(self):
        target = ModelProfileVersion(
            profile_key="mixed-host-target",
            version=1,
            provider=self.provider,
            model_identifier="mixed-host-target",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(target, "specification", {"schema_version": 1})
        target.save()
        remote_profile = ModelProfileVersion(
            profile_key="mixed-host-remote",
            version=1,
            provider=self.provider,
            model_identifier="mixed-host-remote",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(remote_profile, "specification", {"schema_version": 1})
        remote_profile.save()
        remote = ModelDeployment(
            key="mixed-host-remote",
            name="Mixed host remote deployment",
            profile_version=remote_profile,
            runtime_host=self.host,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(remote, "runtime_config", {
            "base_url": "https://remote-comfy.example.test",
        })
        remote.save()

        with patch("studio.services.comfyui_workflow_imports.driver_for") as lookup:
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/"
                "saved-workflows/attest-model-artifacts/",
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [123],
                    "expected_object_info_schema_sha256": "a" * 64,
                    "expected_converted_graph_sha256": "b" * 64,
                    "model_profile_version": str(target.id),
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        lookup.assert_not_called()
        self.assertFalse(RuntimeHostProbeAttempt.objects.exists())
        self.assertFalse(ModelArtifact.objects.filter(profile_version=target).exists())

    def test_attestation_rechecks_hashed_file_inside_commit_fence(self):
        context = self._draft_attestation_context("qwen/model.safetensors")
        artifact = context["artifacts"][0]
        captured_file_facts = []
        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output_dir = comfy_root / "output"
            model_file = (
                comfy_root / "models" / "diffusion_models" / artifact.filename
            )
            output_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_file.write_bytes(b"facts-before-commit")
            driver = self._runtime_driver(model_names=(artifact.filename,))

            from studio.services import runtime_platform
            original_comfy_probe = runtime_platform._comfy_probe

            def capture_comfy_probe(*args, **kwargs):
                result = original_comfy_probe(*args, **kwargs)
                captured_file_facts.extend(
                    item.get("file_facts")
                    for item in result[1]
                    if item.get("file_facts") is not None
                )
                return result

            def mutate_after_hash():
                model_file.write_bytes(b"facts-changed-after-hash-and-before-commit")

            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=driver,
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ), patch(
                "studio.services.runtime_platform._comfy_probe",
                side_effect=capture_comfy_probe,
            ), self.assertRaises(ValidationError):
                probe_runtime_host(
                    self.host,
                    attestation_context=context,
                    commit_guard=mutate_after_hash,
                )

        self.assertFalse(HostCapabilitySnapshot.objects.filter(host=self.host).exists())
        self.assertTrue(captured_file_facts)
        self.assertTrue(all(item.stream.closed for item in captured_file_facts))
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, ModelArtifact.Status.DECLARED)
        self.assertEqual(artifact.sha256, "")

    def test_attestation_closure_and_database_failures_close_every_held_file(self):
        context = self._draft_attestation_context("qwen/cleanup.safetensors")
        artifact = context["artifacts"][0]
        with tempfile.TemporaryDirectory() as root:
            controlled_root = Path(root) / "models"
            controlled_root.mkdir()
            model_path = controlled_root / "cleanup.safetensors"
            model_path.write_bytes(b"cleanup-model")
            from studio.services.runtime_platform import _observed_file_facts

            closure_facts = _observed_file_facts(
                model_path,
                allowed_root=controlled_root,
                hold_open=True,
            )
            with patch(
                "studio.services.runtime_platform._comfy_probe",
                return_value=({}, [{
                    "artifact": artifact,
                    "is_attestation": False,
                    "file_facts": closure_facts,
                }], []),
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ), self.assertRaises(ValidationError):
                probe_runtime_host(self.host, attestation_context=context)
            self.assertTrue(closure_facts.stream.closed)

        captured_file_facts = []
        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output_dir = comfy_root / "output"
            model_file = (
                comfy_root / "models" / "diffusion_models" / artifact.filename
            )
            output_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_file.write_bytes(b"cleanup-db-failure")
            driver = self._runtime_driver(model_names=(artifact.filename,))
            from studio.services import runtime_platform
            original_comfy_probe = runtime_platform._comfy_probe

            def capture_comfy_probe(*args, **kwargs):
                result = original_comfy_probe(*args, **kwargs)
                captured_file_facts.extend(
                    item.get("file_facts")
                    for item in result[1]
                    if item.get("file_facts") is not None
                )
                return result

            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=driver,
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ), patch(
                "studio.services.runtime_platform._comfy_probe",
                side_effect=capture_comfy_probe,
            ), patch.object(
                RuntimeHost,
                "save",
                side_effect=OSError("injected host save failure"),
            ), self.assertRaises(OSError):
                probe_runtime_host(self.host, attestation_context=context)
            self.assertTrue(captured_file_facts)
            self.assertTrue(all(item.stream.closed for item in captured_file_facts))

    def test_attestation_updates_no_artifact_when_closure_is_partial(self):
        context = self._draft_attestation_context(
            "qwen/present.safetensors",
            "qwen/missing.safetensors",
        )
        present, missing = context["artifacts"]
        with tempfile.TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output_dir = comfy_root / "output"
            model_file = (
                comfy_root / "models" / "diffusion_models" / present.filename
            )
            output_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_file.write_bytes(b"present-but-closure-incomplete")
            driver = self._runtime_driver(model_names=(present.filename,))
            with override_settings(COMFYUI_OUTPUT_DIR=str(output_dir)), patch(
                "studio.services.runtime_platform.driver_for",
                return_value=driver,
            ), patch(
                "studio.services.runtime_platform._gpu_probe",
                return_value=([], ""),
            ):
                snapshot = probe_runtime_host(
                    self.host,
                    attestation_context=context,
                )

        present.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(present.status, ModelArtifact.Status.DECLARED)
        self.assertEqual(missing.status, ModelArtifact.Status.DECLARED)
        self.assertEqual(present.sha256, "")
        self.assertEqual(missing.sha256, "")
        rows = RuntimeModelAvailability.objects.filter(
            snapshot=snapshot,
            artifact__in=(present, missing),
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            rows.get(artifact=present).status,
            RuntimeModelAvailability.Status.AVAILABLE,
        )
        self.assertEqual(
            rows.get(artifact=missing).status,
            RuntimeModelAvailability.Status.MISSING,
        )
        self.assertIn(
            "draft_artifact_attestation_incomplete",
            snapshot.details["probe_errors"],
        )

    def test_attestation_health_failure_records_unknown_for_full_draft_closure(self):
        context = self._draft_attestation_context(
            "qwen/first.safetensors",
            "qwen/second.safetensors",
        )
        driver = self._runtime_driver(fail_health=True)
        with patch(
            "studio.services.runtime_platform.driver_for",
            return_value=driver,
        ), patch(
            "studio.services.runtime_platform._gpu_probe",
            return_value=([], ""),
        ):
            snapshot = probe_runtime_host(
                self.host,
                attestation_context=context,
            )

        rows = RuntimeModelAvailability.objects.filter(
            snapshot=snapshot,
            artifact__in=context["artifacts"],
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("status", flat=True)),
            {RuntimeModelAvailability.Status.UNKNOWN},
        )
        self.assertTrue(all(
            artifact.status == ModelArtifact.Status.DECLARED
            for artifact in context["artifacts"]
        ))

    def test_publish_gate_rejects_normal_probe_and_wrong_attestation_closure(self):
        cases = (
            (RuntimeHostProbeAttempt.ProbeKind.CAPABILITY, False),
            (
                RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
                True,
            ),
        )
        for index, (probe_kind, wrong_closure) in enumerate(cases, start=1):
            with self.subTest(probe_kind=probe_kind, wrong_closure=wrong_closure):
                profile = ModelProfileVersion(
                    profile_key=f"publish-gate-{index}",
                    version=1,
                    provider=self.provider,
                    model_identifier=f"publish-gate-{index}",
                    modality=ModelProfileVersion.Modality.IMAGE,
                    status=ModelProfileVersion.Status.DRAFT,
                )
                assign_json_contract(profile, "specification", {"schema_version": 1})
                profile.save()
                artifact = ModelArtifact.objects.create(
                    profile_version=profile,
                    role=ModelArtifact.Role.DIFFUSION_MODEL,
                    logical_name=f"publish-gate-{index}",
                    filename=f"qwen/publish-gate-{index}.safetensors",
                    sha256="a" * 64,
                    size_bytes=128,
                    status=ModelArtifact.Status.AVAILABLE,
                )
                request = {
                    "schema_version": 1,
                    "probe_kind": probe_kind,
                    "host_id": str(self.host.id),
                }
                if probe_kind == (
                    RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
                ):
                    request.update({
                        "source_deployment_id": str(self.deployment.id),
                        "source_deployment_fingerprint_sha256": (
                            source_deployment_fingerprint(self.deployment)
                        ),
                        "target_model_profile_version_id": str(profile.id),
                        "target_profile_fingerprint_sha256": (
                            draft_model_profile_fingerprint(profile)
                        ),
                        "artifacts": [{
                            "artifact_id": str(artifact.id),
                            "role": artifact.role,
                            "filename": "qwen/different.safetensors",
                            "inventory_folder": artifact.inventory_folder,
                            "inventory_node_type": artifact.inventory_node_type,
                            "inventory_input_name": artifact.inventory_input_name,
                        }],
                    })
                attempt = RuntimeHostProbeAttempt(
                    host=self.host,
                    created_by=self.admin,
                    probe_kind=probe_kind,
                    status=RuntimeHostProbeAttempt.Status.SUCCEEDED,
                    finished_at=timezone.now(),
                )
                assign_json_contract(attempt, "request_snapshot", request)
                attempt.save()
                snapshot = HostCapabilitySnapshot(
                    host=self.host,
                    probe_attempt=attempt,
                    revision=index,
                    probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
                    captured_at=timezone.now(),
                    expires_at=timezone.now() + timedelta(minutes=30),
                )
                assign_json_contract(snapshot, "details", {"schema_version": 1})
                snapshot.save()
                RuntimeModelAvailability.objects.create(
                    snapshot=snapshot,
                    deployment=self.deployment,
                    artifact=artifact,
                    availability_key=f"gate:{artifact.id}",
                    status=RuntimeModelAvailability.Status.AVAILABLE,
                    observed_size_bytes=artifact.size_bytes,
                    observed_sha256=artifact.sha256,
                    observation_source=(
                        RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
                    ),
                )
                artifact.verified_snapshot = snapshot
                artifact.verified_at = timezone.now()
                artifact.verified_by = self.admin
                artifact.save(update_fields=(
                    "verified_snapshot", "verified_at", "verified_by", "updated_at",
                ))

                with self.assertRaises(ValidationError):
                    validate_draft_profile_artifacts_for_publish(profile)

    def test_batch_import_is_idempotent_by_deployment_path_and_raw_hash(self):
        driver = self._driver()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, self._batch_payload(), format="json")
            second = self.client.post(url, self._batch_payload(), format="json")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["status"], "complete")
        self.assertEqual(first.data["items"][0]["result"], "created")
        self.assertEqual(second.data["items"][0]["result"], "reused")
        self.assertEqual(first.data["items"][0]["workflow_version_id"], second.data["items"][0]["workflow_version_id"])
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count + 1,
        )
        version = WorkflowTemplateVersion.objects.get(pk=first.data["items"][0]["workflow_version_id"])
        self.assertEqual(version.input_contract["schema_version"], 3)
        self.assertNotIn("smoke_inputs", version.input_contract)
        self.assertNotIn("smoke_asset_bindings", version.input_contract)
        asset_slot = version.input_contract["parameters"]["asset.image.source"]
        self.assertEqual(
            {key: asset_slot[key] for key in (
                "input_slot", "media_kind", "min_items", "max_items",
            )},
            {"input_slot": 1, "media_kind": "image", "min_items": 1, "max_items": 1},
        )
        persisted = str(version.graph)
        self.assertNotIn("smoke.png", persisted)
        self.assertNotIn("saved/output", persisted)
        self.assertIn("__AUTOPROMPT_ASSET__:managed", persisted)
        source_id = first.data["items"][0]["source_id"]
        detail = self.client.get(f"/api/v1/comfy-workflow-sources/{source_id}/")
        rejected_update = self.client.patch(
            f"/api/v1/comfy-workflow-sources/{source_id}/", {"source_path": "changed.json"}, format="json",
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["source_sha256"], self.raw_sha)
        self.assertEqual(rejected_update.status_code, 405)

    def test_asset_slots_follow_live_node_media_and_cardinality(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "image.png"}},
            "2": {"class_type": "LoadVideo", "inputs": {"video": "video.mp4"}},
            "3": {"class_type": "LoadAudio", "inputs": {"audio": "audio.wav"}},
        }
        object_info = {
            "LoadImage": {
                "input": {"required": {"image": [["image.png"], {"image_upload": True}]}},
            },
            "LoadVideo": {
                "input": {"optional": {"video": [["video.mp4"], {"video_upload": True}]}},
            },
            "LoadAudio": {
                "input": {"required": {"audio": [["audio.wav"], {"audio_upload": True}]}},
            },
        }

        dependencies = _asset_dependency_candidates(graph, object_info)

        self.assertEqual([{
            key: row[key]
            for key in (
                "media_kind", "value_type", "input_slot", "is_required",
                "min_items", "max_items",
            )
        } for row in dependencies], [
            {
                "media_kind": "image", "value_type": "asset_image",
                "input_slot": 1, "is_required": True,
                "min_items": 1, "max_items": 1,
            },
            {
                "media_kind": "video", "value_type": "asset_video",
                "input_slot": 1, "is_required": False,
                "min_items": 0, "max_items": 1,
            },
            {
                "media_kind": "audio", "value_type": "asset_audio",
                "input_slot": 1, "is_required": True,
                "min_items": 1, "max_items": 1,
            },
        ])

    def test_legacy_source_can_upgrade_to_value_free_v3_version(self):
        payload = self._batch_payload()
        item = payload["items"][0]
        draft = ComfyWorkflowContractDraft.objects.get(pk=item["contract_draft"])
        projection = draft.projection
        legacy_contract = deepcopy(projection["input_contract"])
        legacy_contract["schema_version"] = 2
        legacy_contract["smoke_inputs"] = {
            "image.width": 512,
            "image.height": 512,
        }
        legacy_contract["smoke_asset_bindings"] = [{
            "semantic_key": "asset.image.source",
            "input_slot": 1,
            "category": "image",
            "asset_id": str(self.smoke_asset.id),
            "checksum": self.smoke_asset.checksum,
        }]
        legacy_version = import_workflow_version(
            template=self.template,
            graph=projection["executable_graph"],
            input_contract=legacy_contract,
            semantic_bindings=projection["semantic_bindings"],
            model_requirements=projection["model_requirements"],
            provider=self.provider,
            model_profile_version=self.profile,
        )
        legacy_source = ComfyWorkflowSource(
            source_deployment=self.deployment,
            **self._source_scope_fields(),
            source_path="saved/example.json",
            source_path_key="saved/example.json",
            source_sha256=self.raw_sha,
            source_size_bytes=1024,
            source_workflow_id=str(self.canvas["id"]),
            import_contract_sha256="f" * 64,
            import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
            converted_graph_sha256=legacy_version.graph_sha256,
            workflow_version=legacy_version,
            imported_by=self.admin,
        )
        assign_json_contract(legacy_source, "conversion_report", {
            "schema_version": 1,
            "import_contract_sha256": "f" * 64,
        })
        legacy_source.full_clean()
        legacy_source.save()
        driver = self._driver()
        scan_url = "/api/v1/comfy-workflow-sources/discover/"
        import_url = (
            f"/api/v1/model-deployments/{self.deployment.id}"
            "/saved-workflows/batch-import/"
        )
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=driver,
        ):
            before = self.client.get(scan_url)
            upgraded = self.client.post(import_url, payload, format="json")
            repeated = self.client.post(import_url, payload, format="json")

        self.assertEqual(before.status_code, 200, before.data)
        self.assertTrue(before.data["items"][0]["contract_upgrade_required"])
        self.assertEqual(upgraded.status_code, 200, upgraded.data)
        self.assertEqual(upgraded.data["items"][0]["result"], "created")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["items"][0]["result"], "reused")
        upgraded_version = WorkflowTemplateVersion.objects.get(
            pk=upgraded.data["items"][0]["workflow_version_id"],
        )
        self.assertNotEqual(upgraded_version.id, legacy_version.id)
        self.assertEqual(upgraded_version.template_id, legacy_version.template_id)
        self.assertEqual(upgraded_version.input_contract["schema_version"], 3)
        self.assertNotIn("smoke_inputs", upgraded_version.input_contract)
        self.assertNotIn("smoke_asset_bindings", upgraded_version.input_contract)
        self.assertEqual(
            repeated.data["items"][0]["workflow_version_id"],
            str(upgraded_version.id),
        )

    def test_reuse_rejects_retired_or_integrity_damaged_workflow_version(self):
        driver = self._driver()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, self._batch_payload(), format="json")
        version_id = first.data["items"][0]["workflow_version_id"]
        WorkflowTemplateVersion.objects.filter(pk=version_id).update(
            status=WorkflowTemplateVersion.Status.VALIDATED,
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            validated_reuse = self.client.post(
                url,
                self._batch_payload(),
                format="json",
            )
        self.assertEqual(validated_reuse.data["status"], "complete", validated_reuse.data)
        self.assertEqual(validated_reuse.data["items"][0]["result"], "reused")
        WorkflowTemplateVersion.objects.filter(pk=version_id).update(
            status=WorkflowTemplateVersion.Status.RETIRED,
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            retired = self.client.post(url, self._batch_payload(), format="json")
        self.assertEqual(retired.data["status"], "failed", retired.data)
        self.assertEqual(retired.data["items"][0]["code"], "source_already_classified")

        WorkflowTemplateVersion.objects.filter(pk=version_id).update(
            status=WorkflowTemplateVersion.Status.DRAFT,
            graph={"tampered": {"class_type": "PreviewAny", "inputs": {}}},
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            damaged = self.client.post(url, self._batch_payload(), format="json")
        self.assertEqual(damaged.data["status"], "failed", damaged.data)
        self.assertEqual(damaged.data["items"][0]["code"], "source_already_classified")

    def test_source_provenance_rejects_instance_and_queryset_mutation(self):
        driver = self._driver()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            created = self.client.post(url, self._batch_payload(), format="json")
        self.assertEqual(created.data["status"], "complete", created.data)
        source = ComfyWorkflowSource.objects.get(
            import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
        )
        source.source_path = "tampered.json"
        with self.assertRaises(ValidationError):
            source.save(update_fields=("source_path", "updated_at"))
        with self.assertRaises(ValidationError):
            ComfyWorkflowSource.objects.filter(pk=source.pk).update(
                source_path="tampered.json",
            )
        with self.assertRaises(ValidationError):
            ComfyWorkflowSource.objects.filter(pk=source.pk).delete()

    def test_canonical_reuse_skips_retired_version_and_creates_fresh_draft(self):
        driver = self._driver(paths=["saved/a.json", "saved/b.json"])
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, self._batch_payload("saved/a.json"), format="json")
        first_version_id = first.data["items"][0]["workflow_version_id"]
        WorkflowTemplateVersion.objects.filter(pk=first_version_id).update(
            status=WorkflowTemplateVersion.Status.RETIRED,
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            second = self.client.post(url, self._batch_payload("saved/b.json"), format="json")

        self.assertEqual(second.data["status"], "complete", second.data)
        self.assertEqual(second.data["items"][0]["result"], "created")
        self.assertNotEqual(second.data["items"][0]["workflow_version_id"], first_version_id)

    def test_each_template_item_consumes_its_own_frozen_contract_draft(self):
        object_info_a = self._object_info()
        object_info_b = deepcopy(object_info_a)
        object_info_b["SaveImage"]["input"]["required"]["filename_prefix"][1]["default"] = "second"
        driver = self._driver(paths=["saved/a.json", "saved/b.json"])
        payload = {
            "items": [
                self._batch_payload("saved/a.json", object_info=object_info_a)["items"][0],
                self._batch_payload("saved/b.json", object_info=object_info_b)["items"][0],
            ],
        }
        self.assertNotEqual(
            payload["items"][0]["contract_draft"],
            payload["items"][1]["contract_draft"],
        )
        driver.reset_mock()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(response.data["status"], "complete", response.data)
        self.assertEqual([row["result"] for row in response.data["items"]], ["created", "created"])
        self.assertEqual(
            sum(call.args[1] == "object_info" for call in driver.invoke.call_args_list),
            0,
        )

    def test_template_import_requires_one_live_output_node_and_exact_generic_contract(self):
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        multiple = self._batch_payload("saved/multiple-primary.json")
        multiple["items"][0]["primary_output_node_ids"] = [1, 123]
        multiple_response = self.client.post(url, multiple, format="json")
        self.assertEqual(multiple_response.status_code, 400, multiple_response.data)

        video_template = WorkflowTemplate.objects.create(
            owner=None,
            slug="saved-output-kind-mismatch",
            version=1,
            name="Saved output mismatch",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )
        wrong_kind = self._batch_payload("saved/wrong-output-kind.json")
        wrong_kind["items"][0]["template"] = str(video_template.id)
        wrong_kind_response = self.client.post(url, wrong_kind, format="json")
        self.assertEqual(wrong_kind_response.status_code, 200, wrong_kind_response.data)
        self.assertEqual(wrong_kind_response.data["status"], "failed")
        self.assertEqual(
            wrong_kind_response.data["items"][0]["code"],
            "workflow_output_contract_invalid",
        )

        not_output_info = self._object_info()
        not_output_info["SaveImage"]["output_node"] = False
        not_output_driver = self._driver(object_info=not_output_info)
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=not_output_driver,
        ):
            preview = self._post_managed_preview({
                "source_path": "saved/not-live-output.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            })
            not_output_response = self.client.post(url, {"items": [{
                "source_path": "saved/not-live-output.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": [123],
                "expected_object_info_schema_sha256": preview.data[
                    "object_info_schema_sha256"
                ],
                "expected_converted_graph_sha256": preview.data[
                    "converted_graph_sha256"
                ],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(self.template.id),
                "model_profile_version": str(self.profile.id),
            }]}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["actionability"], "candidate_only")
        self.assertEqual(not_output_response.status_code, 200, not_output_response.data)
        self.assertEqual(not_output_response.data["status"], "failed")
        self.assertEqual(
            not_output_response.data["items"][0]["code"],
            "workflow_contract_blocked",
        )
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(),
            self.initial_workflow_version_count,
        )

    def test_explicit_successor_preserves_published_version_and_replays_receipt(self):
        payload = self._batch_payload()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        first = self.client.post(url, payload, format="json")
        original_id = first.data["items"][0]["workflow_version_id"]
        WorkflowTemplateVersion.objects.filter(pk=original_id).update(status="published")
        original = WorkflowTemplateVersion.objects.get(pk=original_id)
        original_source = ComfyWorkflowSource.objects.get(pk=first.data["items"][0]["source_id"])
        original_report = deepcopy(original_source.conversion_report)
        payload["items"][0]["successor_of"] = original_id
        before = WorkflowTemplateVersion.objects.count()
        created = self.client.post(url, payload, format="json")
        self.assertEqual(created.data["status"], "complete", created.data)
        successor_id = created.data["items"][0]["workflow_version_id"]
        self.assertNotEqual(successor_id, original_id)
        successor = WorkflowTemplateVersion.objects.get(pk=successor_id)
        self.assertEqual(successor.version, original.version + 1)
        self.assertEqual(successor.status, "draft")
        self.assertEqual(successor.validation_state, "not_run")
        self.assertEqual(successor.smoke_test_state, "not_run")
        self.assertEqual(successor.validation_evidence, {})
        self.assertEqual(successor.smoke_test_evidence, {})
        self.assertIsNone(successor.validated_deployment_id)
        self.assertEqual(successor.graph_sha256, original.graph_sha256)
        original.refresh_from_db()
        self.assertEqual(original.status, "published")
        original_source.refresh_from_db()
        self.assertEqual(original_source.workflow_version_id, original.pk)
        self.assertEqual(original_source.conversion_report, original_report)
        repeated = self.client.post(url, payload, format="json")
        self.assertEqual(repeated.data["items"][0]["result"], "reused")
        self.assertEqual(repeated.data["items"][0]["workflow_version_id"], successor_id)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), before + 1)

    def test_successor_requires_original_template_and_exact_source_provenance(self):
        payload = self._batch_payload()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        first = self.client.post(url, payload, format="json")
        original_id = first.data["items"][0]["workflow_version_id"]
        unrelated = WorkflowTemplateVersion.objects.exclude(pk=original_id).first()
        self.assertIsNotNone(unrelated)
        payload["items"][0]["successor_of"] = str(unrelated.pk)
        before = WorkflowTemplateVersion.objects.count()
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.data["status"], "failed", response.data)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), before)

    def test_successor_cannot_fork_old_predecessor_with_another_contract(self):
        payload = self._batch_payload()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        first = self.client.post(url, payload, format="json")
        original_id = first.data["items"][0]["workflow_version_id"]
        payload["items"][0]["successor_of"] = original_id
        successor = self.client.post(url, payload, format="json")
        self.assertEqual(successor.data["status"], "complete", successor.data)
        object_info = self._object_info()
        object_info["SaveImage"]["description"] = "Updated node catalog"
        changed = self._batch_payload(object_info=object_info)
        self.assertNotEqual(changed["items"][0]["contract_draft"], payload["items"][0]["contract_draft"])
        changed["items"][0]["successor_of"] = original_id
        before = WorkflowTemplateVersion.objects.count()
        response = self.client.post(url, changed, format="json")
        self.assertEqual(response.data["status"], "failed", response.data)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), before)
        repeated = self.client.post(url, payload, format="json")
        self.assertEqual(repeated.data["items"][0]["workflow_version_id"], successor.data["items"][0]["workflow_version_id"])

    def test_successor_rejects_different_source_path_without_writes(self):
        payload = self._batch_payload("saved/a.json")
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        original = self.client.post(url, payload, format="json")
        other = self._batch_payload("saved/b.json")
        other["items"][0]["successor_of"] = original.data["items"][0]["workflow_version_id"]
        versions_before = WorkflowTemplateVersion.objects.count()
        sources_before = ComfyWorkflowSource.objects.count()
        rejected = self.client.post(url, other, format="json")
        self.assertEqual(rejected.data["status"], "failed", rejected.data)
        self.assertEqual(rejected.data["items"][0]["code"], "workflow_successor_conflict")
        self.assertIn("来源范围", rejected.data["items"][0]["detail"])
        self.assertEqual(WorkflowTemplateVersion.objects.count(), versions_before)
        self.assertEqual(ComfyWorkflowSource.objects.count(), sources_before)

    def test_same_bytes_at_second_path_preserve_provenance_and_reuse_canonical_version(self):
        driver = self._driver(paths=["saved/a.json", "saved/b.json"])
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, self._batch_payload("saved/a.json"), format="json")
            second = self.client.post(url, self._batch_payload("saved/b.json"), format="json")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(ComfyWorkflowSource.objects.count(), 4)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count + 1,
        )
        self.assertEqual(first.data["items"][0]["workflow_version_id"], second.data["items"][0]["workflow_version_id"])

    def test_expected_hash_mismatch_is_explicit_and_writes_nothing(self):
        driver = self._driver()
        payload = self._batch_payload()
        payload["items"][0]["expected_sha256"] = "f" * 64
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(
            response.data["items"][0]["code"],
            "workflow_contract_draft_mismatch",
        )
        self.assertEqual(ComfyWorkflowSource.objects.count(), 1)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count,
        )

    def test_same_source_rejects_changed_kind_but_ignores_legacy_client_smoke_fields(self):
        driver = self._driver()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, self._batch_payload(), format="json")
            changed_kind = self._batch_payload()
            changed_kind["items"][0] = {
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "ignored",
            }
            kind_response = self.client.post(url, changed_kind, format="json")
            changed_contract = self._batch_payload()
            changed_contract["items"][0]["input_contract"] = {
                "schema_version": 2,
                "smoke_inputs": {"image.width": 4096},
                "smoke_asset_bindings": [{
                    "asset_id": str(self.smoke_asset.id),
                }],
            }
            contract_response = self.client.post(url, changed_contract, format="json")

        self.assertEqual(first.data["status"], "complete")
        self.assertEqual(kind_response.data["status"], "failed")
        self.assertEqual(
            kind_response.data["items"][0]["code"], "source_already_classified",
        )
        self.assertEqual(contract_response.data["status"], "complete")
        self.assertEqual(
            contract_response.data["items"][0]["result"], "reused",
        )
        self.assertEqual(
            contract_response.data["items"][0]["workflow_version_id"],
            first.data["items"][0]["workflow_version_id"],
        )
        version = WorkflowTemplateVersion.objects.get(
            pk=first.data["items"][0]["workflow_version_id"],
        )
        self.assertNotIn("smoke_inputs", version.input_contract)
        self.assertNotIn("smoke_asset_bindings", version.input_contract)
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count + 1,
        )

    def test_case_distinct_userdata_paths_preserve_distinct_provenance(self):
        driver = self._driver(paths=["saved/Example.json", "saved/example.json"])
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            upper = self.client.post(url, self._batch_payload("saved/Example.json"), format="json")
            lower = self.client.post(url, self._batch_payload("saved/example.json"), format="json")

        self.assertEqual(upper.data["status"], "complete")
        self.assertEqual(lower.data["status"], "complete")
        self.assertEqual(
            set(ComfyWorkflowSource.objects.values_list("source_path_key", flat=True)),
            {"saved/Example.json", "saved/example.json"},
        )

    def test_secret_widget_is_rejected_before_any_version_or_source_write(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"][1]["inputs"].append({
            "name": "api_key",
            "type": "STRING",
            "widget": {"name": "api_key"},
            "link": None,
        })
        canvas["nodes"][1]["widgets_values"].append("plaintext-secret")
        object_info = deepcopy(self._object_info())
        object_info["SaveImage"]["input"]["required"]["api_key"] = [
            "STRING", {"default": ""},
        ]
        object_info["SaveImage"]["input_order"]["required"].append("api_key")
        driver = self._driver(canvas=canvas, object_info=object_info)

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                {"items": [{
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "import_kind": "candidate",
                }]},
                format="json",
            )

        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["items"][0]["code"], "secret_field_forbidden")
        self.assertEqual(ComfyWorkflowSource.objects.count(), 0)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count,
        )

    def test_unsupported_text_import_keeps_prompt_neutralized_and_blocks_promotion(self):
        text_template = WorkflowTemplate.objects.create(
            owner=None,
            slug="saved-text-import",
            version=1,
            name="Saved text import",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.TEXT,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )
        canvas = {
            "id": "22222222-2222-4222-8222-222222222222",
            "revision": 0,
            "last_node_id": 99,
            "last_link_id": 1,
            "nodes": [
                {
                    "id": 77,
                    "type": "TextGenerate",
                    "pos": [0, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 0,
                    "mode": 0,
                    "inputs": [{
                        "name": "prompt", "type": "STRING",
                        "widget": {"name": "prompt"}, "link": None,
                    }],
                    "outputs": [{"name": "text", "type": "STRING", "links": [1]}],
                    "properties": {"Node name for S&R": "TextGenerate"},
                    "widgets_values": ["TOP SECRET PLOT"],
                },
                {
                    "id": 99,
                    "type": "PreviewAny",
                    "pos": [300, 0],
                    "size": [240, 120],
                    "flags": {},
                    "order": 1,
                    "mode": 0,
                    "inputs": [{"name": "source", "type": "STRING", "link": 1}],
                    "outputs": [],
                    "properties": {"Node name for S&R": "PreviewAny"},
                    "widgets_values": [],
                },
            ],
            "links": [[1, 77, 0, 99, 0, "STRING"]],
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }
        object_info = {
            "TextGenerate": {
                "input": {"required": {"prompt": ["STRING", {"default": ""}]}},
                "input_order": {"required": ["prompt"]},
                "output": ["STRING"],
                "output_name": ["text"],
            },
            "PreviewAny": {
                "input": {"required": {"source": ["STRING", {}]}},
                "input_order": {"required": ["source"]},
                "output": [],
                "output_name": [],
                "output_node": True,
            },
        }
        driver = self._driver(canvas=canvas, object_info=object_info)
        text_deployment = self._second_same_scope_deployment(
            key="bound-text-prompt",
            modality=ModelProfileVersion.Modality.TEXT,
            capabilities=(),
        )

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            preview = self._post_managed_preview(
                {
                    "source_path": "saved/example.json",
                    "expected_sha256": self.raw_sha,
                    **self._expected_source_scope_fields(),
                    "primary_output_node_ids": [99],
                    "model_profile_version": str(text_deployment.profile_version_id),
                },
            )
            self.assertEqual(preview.status_code, 200, preview.data)
            payload = {"items": [{
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "import_kind": "template_version",
                "primary_output_node_ids": [99],
                "expected_object_info_schema_sha256": preview.data[
                    "object_info_schema_sha256"
                ],
                "expected_converted_graph_sha256": preview.data[
                    "converted_graph_sha256"
                ],
                "expected_parameter_schema_sha256": preview.data[
                    "parameter_schema_sha256"
                ],
                "expected_structural_contract_sha256": preview.data[
                    "structural_contract_sha256"
                ],
                "contract_draft": preview.data["contract_draft_id"],
                "template": str(text_template.id),
                "model_profile_version": str(text_deployment.profile_version_id),
            }]}
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertNotIn("TOP SECRET PLOT", str(preview.data))
        self.assertEqual(
            preview.data["suggested_semantic_bindings"],
            [{
                "semantic_key": "prompt.positive",
                "node_id": "1",
                "input_name": "prompt",
                "value_type": "string",
                "is_required": True,
            }],
        )
        self.assertEqual(response.data["status"], "failed", response.data)
        self.assertEqual(response.data["items"][0]["code"], "workflow_contract_blocked")
        self.assertEqual(WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count)
        self.assertIn("workflow_projector_capability_mismatch", {row["code"] for row in preview.data["blockers"]})
        draft = ComfyWorkflowContractDraft.objects.get(pk=preview.data["contract_draft_id"])
        self.assertNotIn("TOP SECRET PLOT", str(draft.projection))
        self.assertEqual(draft.projection["executable_graph"], {}, "Blocked candidates must not persist an executable graph")

    def test_promotion_uses_frozen_contract_draft_when_object_info_changes(self):
        object_info_a = self._object_info()
        object_info_b = deepcopy(object_info_a)
        object_info_b["SaveImage"]["input"]["required"]["filename_prefix"][1]["default"] = "changed"
        preview_driver = self._driver(object_info=object_info_a)
        batch_driver = self._driver(object_info=object_info_b)
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        )
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=preview_driver,
        ):
            preview = self._post_managed_preview({
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            })
        payload = {"items": [{
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "template_version",
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": preview.data[
                "object_info_schema_sha256"
            ],
            "expected_converted_graph_sha256": preview.data[
                "converted_graph_sha256"
            ],
            "expected_parameter_schema_sha256": preview.data[
                "parameter_schema_sha256"
            ],
            "expected_structural_contract_sha256": preview.data[
                "structural_contract_sha256"
            ],
            "contract_draft": preview.data["contract_draft_id"],
            "template": str(self.template.id),
            "model_profile_version": str(self.profile.id),
        }]}
        batch_driver.reset_mock()
        with patch(
            "studio.services.comfyui_workflow_imports.driver_for",
            return_value=batch_driver,
        ):
            response = self.client.post(batch_url, payload, format="json")

        self.assertEqual(response.data["status"], "complete", response.data)
        self.assertEqual(response.data["items"][0]["result"], "created")
        batch_driver.invoke.assert_not_called()
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)

    def test_target_model_profile_must_use_source_comfy_provider(self):
        foreign_profile = ModelProfileVersion.objects.filter(
            status=ModelProfileVersion.Status.PUBLISHED,
        ).exclude(provider=self.provider).first()
        self.assertIsNotNone(foreign_profile)
        payload = self._batch_payload()
        payload["items"][0]["model_profile_version"] = str(foreign_profile.id)
        driver = self._driver()

        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(
            response.data["items"][0]["code"],
            "workflow_contract_draft_mismatch",
        )
        self.assertEqual(ComfyWorkflowSource.objects.count(), 1)

    def test_unsafe_canvas_output_prefix_stays_candidate_only_even_when_bound(self):
        for index, unsafe in enumerate(("../../escape", "C:\\x", "/absolute")):
            with self.subTest(unsafe=unsafe):
                canvas = deepcopy(self.canvas)
                canvas["nodes"][1]["widgets_values"] = [unsafe, 512, 512, 1]
                source_path = f"saved/unsafe-output-{index}.json"
                driver = self._driver(paths=[source_path], canvas=canvas)
                with patch(
                    "studio.services.comfyui_workflow_imports.driver_for",
                    return_value=driver,
                ):
                    response = self.client.post(
                        f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                        {"items": [{
                            "source_path": source_path,
                            "expected_sha256": self.raw_sha,
                            **self._expected_source_scope_fields(),
                            "import_kind": "candidate",
                        }]},
                        format="json",
                    )
                    preview = self.client.post(
                        f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/preview/",
                        {
                            "source_path": source_path,
                            "expected_sha256": self.raw_sha,
                            **self._expected_source_scope_fields(),
                            "primary_output_node_ids": [123],
                            "model_profile_version": str(self.profile.id),
                        },
                        format="json",
                    )
                self.assertEqual(response.data["status"], "complete", response.data)
                self.assertEqual(response.data["items"][0]["import_kind"], "candidate")
                self.assertEqual(preview.status_code, 200, preview.data)
                self.assertEqual(preview.data["actionability"], "candidate_only")
                self.assertIn(
                    "unsafe_source_literal",
                    {row["code"] for row in preview.data["blockers"]},
                )
                self.assertTrue(preview.data["contract_draft_id"])
                self.assertNotIn(unsafe, str(preview.data))
                self.assertEqual(
                    ComfyWorkflowSource.objects.filter(
                        import_kind=ComfyWorkflowSource.ImportKind.CANDIDATE,
                    ).count(),
                    index + 1,
                )
                self.assertEqual(ComfyWorkflowContractDraft.objects.count(), index + 1)
                self.assertEqual(
                    WorkflowTemplateVersion.objects.count(),
                    self.initial_workflow_version_count,
                )

    def test_model_dependency_candidates_use_exact_kj_and_seed_inventory_contracts(self):
        graph = {
            "1": {"class_type": "DiffusionModelLoaderKJ", "inputs": {
                "model_name": "nested\\qwen-edit.safetensors",
            }},
            "2": {"class_type": "SeedVR2LoadDiTModel", "inputs": {
                "model": "seed-dit.safetensors",
            }},
            "3": {"class_type": "SeedVR2LoadVAEModel", "inputs": {
                "model": "seed-vae.safetensors",
            }},
        }
        object_info = {
            "DiffusionModelLoaderKJ": {"input": {"required": {
                "model_name": [["nested\\qwen-edit.safetensors"], {}],
            }}},
            "SeedVR2LoadDiTModel": {"input": {"required": {
                "model": ["COMBO", {"options": ["seed-dit.safetensors"]}],
            }}},
            "SeedVR2LoadVAEModel": {"input": {"required": {
                "model": ["COMBO", {"options": [{"key": "seed-vae.safetensors"}]}],
            }}},
        }
        matched = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="qwen-edit",
            filename="nested/qwen-edit.safetensors",
            sha256="a" * 64,
            size_bytes=123,
            status=ModelArtifact.Status.AVAILABLE,
        )

        rows = _model_dependency_candidates(
            graph,
            object_info,
            model_profile_version=self.profile,
        )

        self.assertEqual(len(rows), 3)
        by_node = {row["node_id"]: row for row in rows}
        self.assertEqual(by_node["1"]["role"], ModelArtifact.Role.DIFFUSION_MODEL)
        self.assertEqual(by_node["1"]["filename"], "nested/qwen-edit.safetensors")
        self.assertEqual(by_node["1"]["inventory_folder"], "diffusion_models")
        self.assertEqual(by_node["1"]["artifact_matches"][0]["id"], str(matched.id))
        for node_id, node_type in (("2", "SeedVR2LoadDiTModel"), ("3", "SeedVR2LoadVAEModel")):
            self.assertEqual(by_node[node_id]["role"], ModelArtifact.Role.OTHER)
            self.assertEqual(by_node[node_id]["inventory_folder"], "SEEDVR2")
            self.assertEqual(by_node[node_id]["inventory_node_type"], node_type)
            self.assertEqual(by_node[node_id]["inventory_input_name"], "model")

    def test_windows_model_names_round_trip_preview_to_managed_batch_import(self):
        canvas = deepcopy(self.canvas)
        canvas["nodes"].insert(1, {
            "id": 2,
            "type": "UNETLoader",
            "pos": [0, 200],
            "size": [240, 120],
            "flags": {},
            "order": 1,
            "mode": 0,
            "inputs": [
                {"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}},
                {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}},
            ],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [2]}],
            "properties": {"Node name for S&R": "UNETLoader"},
            "widgets_values": ["nested\\qwen.safetensors", "default"],
        })
        canvas["nodes"][2]["inputs"].append({
            "name": "model", "type": "MODEL", "link": 2,
        })
        canvas["links"].append([2, 2, 0, 123, 5, "MODEL"])
        object_info = self._object_info()
        object_info["UNETLoader"] = {
            "input": {"required": {
                "unet_name": [["nested\\qwen.safetensors"], {}],
                "weight_dtype": [["default"], {}],
            }},
            "input_order": {"required": ["unet_name", "weight_dtype"]},
            "output": ["MODEL"],
            "output_name": ["MODEL"],
        }
        object_info["SaveImage"]["input"]["optional"] = {"model": ["MODEL", {}]}
        object_info["SaveImage"]["input_order"]["optional"] = ["model"]
        evidence = HostCapabilitySnapshot(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(evidence, "details", {"schema_version": 1})
        evidence.save()
        artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="windows-normalized-qwen",
            filename="nested/qwen.safetensors",
            sha256="d" * 64,
            size_bytes=321,
            status=ModelArtifact.Status.AVAILABLE,
            verified_snapshot=evidence,
            verified_at=timezone.now(),
            verified_by=self.admin,
        )
        RuntimeModelAvailability.objects.create(
            snapshot=evidence,
            deployment=self.deployment,
            artifact=artifact,
            availability_key=f"deployment:{self.deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=(
                RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
            ),
        )
        driver = self._driver(canvas=canvas, object_info=object_info)
        preview_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/preview/"
        )
        batch_url = (
            f"/api/v1/model-deployments/{self.deployment.id}/"
            "saved-workflows/batch-import/"
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            preview = self._post_managed_preview({
                "source_path": "saved/windows-model.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(self.profile.id),
            })
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["model_dependencies"][0]["filename"], artifact.filename)
        self.assertEqual(
            preview.data["suggested_model_requirements"][0]["artifact_id"],
            str(artifact.id),
        )
        payload = {"items": [{
            "source_path": "saved/windows-model.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "template_version",
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": preview.data["object_info_schema_sha256"],
            "expected_converted_graph_sha256": preview.data["converted_graph_sha256"],
            "expected_parameter_schema_sha256": preview.data[
                "parameter_schema_sha256"
            ],
            "expected_structural_contract_sha256": preview.data[
                "structural_contract_sha256"
            ],
            "contract_draft": preview.data["contract_draft_id"],
            "template": str(self.template.id),
            "model_profile_version": str(self.profile.id),
        }]}
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            imported = self.client.post(batch_url, payload, format="json")
        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["status"], "complete", imported.data)
        version = WorkflowTemplateVersion.objects.get(
            pk=imported.data["items"][0]["workflow_version_id"],
        )
        unet = next(node for node in version.graph.values() if node["class_type"] == "UNETLoader")
        self.assertEqual(unet["inputs"]["unet_name"], "nested\\qwen.safetensors")
        self.assertEqual(version.model_requirements.get().expected_filename, artifact.filename)

    def test_discovery_rejects_fresh_artifact_with_wrong_inventory_selector(self):
        snapshot = HostCapabilitySnapshot(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="wrong-selector",
            filename="nested/qwen.safetensors",
            inventory_folder="SEEDVR2",
            inventory_node_type="SeedVR2LoadDiTModel",
            inventory_input_name="model",
            sha256="e" * 64,
            size_bytes=321,
            status=ModelArtifact.Status.AVAILABLE,
            verified_snapshot=snapshot,
            verified_at=timezone.now(),
            verified_by=self.admin,
        )
        RuntimeModelAvailability.objects.create(
            snapshot=snapshot,
            deployment=self.deployment,
            artifact=artifact,
            availability_key=f"deployment:{self.deployment.id}:artifact:{artifact.id}",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
            observation_source=(
                RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
            ),
        )

        codes = _profile_candidate_evidence_codes({
            "model_dependencies": [{
                "available_in_object_info": True,
                "inventory_folder": "diffusion_models",
                "inventory_node_type": "",
                "inventory_input_name": "",
                "artifact_matches": [{"id": str(artifact.id)}],
            }],
        }, target_deployment=self.deployment)

        self.assertEqual(codes, ["model_artifact_inventory_mismatch"])

    def test_model_dependency_requires_exact_binding_requirement_and_attested_artifact(self):
        snapshot = HostCapabilitySnapshot(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "details", {"schema_version": 1})
        snapshot.save()
        artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="qwen-edit-covered",
            filename="qwen-edit-covered.safetensors",
            sha256="b" * 64,
            size_bytes=456,
            status=ModelArtifact.Status.AVAILABLE,
            verified_snapshot=snapshot,
            verified_at=timezone.now(),
            verified_by=self.admin,
        )
        graph = {"1": {"class_type": "DiffusionModelLoaderKJ", "inputs": {
            "model_name": artifact.filename,
        }}}
        object_info = {"DiffusionModelLoaderKJ": {"input": {"required": {
            "model_name": [[artifact.filename], {}],
        }}}}
        binding = {
            "semantic_key": "model.dit.filename",
            "node_id": "1",
            "input_name": "model_name",
            "value_type": "model_artifact",
            "is_required": True,
        }
        requirement = {
            "role": ModelArtifact.Role.DIFFUSION_MODEL,
            "alias": "dit",
            "artifact_id": str(artifact.id),
            "expected_filename": artifact.filename,
            "expected_sha256": artifact.sha256,
            "is_required": True,
        }
        item = {
            "model_profile_version": self.profile,
            "input_contract": {},
            "semantic_bindings": [binding],
            "model_requirements": [requirement],
        }

        _validate_model_dependency_coverage(graph, object_info, item=item)
        for field, value in (
            ("semantic_bindings", []),
            ("model_requirements", []),
            ("model_requirements", [{**requirement, "expected_sha256": "c" * 64}]),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                Exception,
                "模型|model|artifact",
            ):
                _validate_model_dependency_coverage(
                    graph,
                    object_info,
                    item={**item, field: value},
                )

    def test_source_deployment_change_after_draft_does_not_rewrite_frozen_promotion(self):
        payload = self._batch_payload()
        current = ModelDeployment.objects.get(pk=self.deployment.pk)
        assign_json_contract(current, "runtime_config", {
            "base_url": "http://127.0.0.1:8199",
            "timeout_seconds": 10,
        })
        current.save(update_fields=(
            "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
        ))
        driver = self._driver()
        driver.reset_mock()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(response.data["status"], "complete", response.data)
        self.assertEqual(response.data["items"][0]["result"], "created")
        driver.invoke.assert_not_called()
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count + 1,
        )

    def test_runtime_host_disabled_after_draft_does_not_rewrite_frozen_promotion(self):
        payload = self._batch_payload()
        RuntimeHost.objects.filter(pk=self.host.pk).update(
            status=RuntimeHost.Status.DISABLED,
        )
        driver = self._driver()
        driver.reset_mock()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )

        self.assertEqual(response.data["status"], "complete", response.data)
        self.assertEqual(response.data["items"][0]["result"], "created")
        driver.invoke.assert_not_called()
        self.assertEqual(ComfyWorkflowSource.objects.count(), 2)
        self.assertEqual(
            WorkflowTemplateVersion.objects.count(), self.initial_workflow_version_count + 1,
        )

    def test_existing_ignored_source_reuse_rechecks_locked_deployment_after_read(self):
        driver = self._driver()
        payload = {"items": [{
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "ignored",
        }]}
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, payload, format="json")
        self.assertEqual(first.data["items"][0]["result"], "created", first.data)
        original_invoke = driver.invoke.side_effect

        def invoke_with_drift(deployment, operation, request_payload):
            result = original_invoke(deployment, operation, request_payload)
            if operation == "workflow_library.read":
                current = ModelDeployment.objects.get(pk=self.deployment.pk)
                assign_json_contract(current, "runtime_config", {
                    "base_url": "http://127.0.0.1:8199",
                    "timeout_seconds": 10,
                })
                current.save(update_fields=(
                    "runtime_config", "runtime_config_schema_version",
                    "runtime_config_size_bytes", "runtime_config_sha256", "updated_at",
                ))
            return result

        driver.invoke.side_effect = invoke_with_drift
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            second = self.client.post(url, payload, format="json")
        self.assertEqual(second.data["status"], "failed", second.data)
        self.assertEqual(second.data["items"][0]["code"], "source_deployment_changed")

    def test_existing_template_source_reuse_consumes_frozen_draft_without_source_read(self):
        driver = self._driver()
        payload = self._batch_payload()
        url = f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/"
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            first = self.client.post(url, payload, format="json")
        self.assertEqual(first.data["items"][0]["result"], "created", first.data)
        source_count = ComfyWorkflowSource.objects.count()
        version_count = WorkflowTemplateVersion.objects.count()
        driver.reset_mock()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            second = self.client.post(url, payload, format="json")
        self.assertEqual(second.data["status"], "complete", second.data)
        self.assertEqual(second.data["items"][0]["result"], "reused")
        driver.invoke.assert_not_called()
        self.assertEqual(ComfyWorkflowSource.objects.count(), source_count)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), version_count)

    def test_retired_target_profile_is_rejected_before_frozen_draft_promotion(self):
        target = ModelProfileVersion(
            profile_key="saved-import-race-target",
            version=1,
            provider=self.provider,
            model_identifier="saved-import-race-target",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(target, "specification", {"schema_version": 1})
        target.save()
        driver = self._driver()
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            preview = self._post_managed_preview({
                "source_path": "saved/example.json",
                "expected_sha256": self.raw_sha,
                **self._expected_source_scope_fields(),
                "primary_output_node_ids": [123],
                "model_profile_version": str(target.id),
            })
        payload = {"items": [{
            "source_path": "saved/example.json",
            "expected_sha256": self.raw_sha,
            **self._expected_source_scope_fields(),
            "import_kind": "template_version",
            "primary_output_node_ids": [123],
            "expected_object_info_schema_sha256": preview.data[
                "object_info_schema_sha256"
            ],
            "expected_converted_graph_sha256": preview.data[
                "converted_graph_sha256"
            ],
            "expected_parameter_schema_sha256": preview.data[
                "parameter_schema_sha256"
            ],
            "expected_structural_contract_sha256": preview.data[
                "structural_contract_sha256"
            ],
            "contract_draft": preview.data["contract_draft_id"],
            "template": str(self.template.id),
            "model_profile_version": str(target.id),
        }]}
        ModelProfileVersion.objects.filter(pk=target.pk).update(
            status=ModelProfileVersion.Status.RETIRED,
        )
        with patch("studio.services.comfyui_workflow_imports.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/saved-workflows/batch-import/",
                payload,
                format="json",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("model_profile_version", str(response.data))
        self.assertEqual(ComfyWorkflowSource.objects.count(), 1)
