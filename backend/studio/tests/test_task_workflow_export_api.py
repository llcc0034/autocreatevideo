"""Read-only export of real preparation/receipt facts; no live ComfyUI calls."""
import json
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask
from studio.services.comfyui import ComfyUIError
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_production_workflow_artifacts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class GenerationTaskWorkflowExportAPITests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/tasks/{self.task.pk}/comfyui-workflow/"

    @staticmethod
    def object_info():
        def node(inputs, outputs):
            return {"input": {"required": inputs}, "input_order": {"required": list(inputs)},
                "output": outputs, "output_name": outputs}
        return {
            "CheckpointLoaderSimple": node({"ckpt_name": [["fixture.safetensors"], {}]}, ["MODEL", "CLIP", "VAE"]),
            "CLIPTextEncode": node({"text": ["STRING", {}], "clip": ["CLIP", {}]}, ["CONDITIONING"]),
            "VAEEncode": node({"pixels": ["IMAGE", {}], "vae": ["VAE", {}]}, ["LATENT"]),
            "KSampler": node({"model": ["MODEL", {}], "positive": ["CONDITIONING", {}], "negative": ["CONDITIONING", {}],
                "latent_image": ["LATENT", {}], "seed": ["INT", {}], "steps": ["INT", {}], "cfg": ["FLOAT", {}],
                "sampler_name": [["euler"], {}], "scheduler": [["normal"], {}], "denoise": ["FLOAT", {}]}, ["LATENT"]),
            "VAEDecode": node({"samples": ["LATENT", {}], "vae": ["VAE", {}]}, ["IMAGE"]),
            "SaveImage": node({"images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]}, []),
            "LoadImage": node({"image": [["unrelated-server-file.png"], {"image_upload": True}]}, ["IMAGE", "MASK"]),
            "ImageScale": node({"image": ["IMAGE", {}], "upscale_method": [["nearest-exact", "lanczos"], {}],
                "width": ["INT", {}], "height": ["INT", {}], "crop": [["disabled", "center"], {}]}, ["IMAGE"]),
        }

    def test_owner_reads_frozen_request_and_real_receipts_without_writes_or_remote_io(self):
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count(), GenerationTask.objects.count())
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No remote contact")), \
                patch("studio.services.workflow_compiler.compile_frozen_workflow_values", side_effect=AssertionError("No recompile")), \
                CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data
        self.assertEqual(row["preparation_id"], str(self.preparation.pk))
        self.assertEqual(row["attempt_id"], str(self.attempt.pk))
        self.assertEqual(row["workflow_version_id"], str(self.workflow.pk))
        self.assertEqual(row["request_sha256"], self.preparation.request_sha256)
        self.assertEqual(row["graph_state"], "staged")
        self.assertTrue(row["import_ready"])
        self.assertEqual(row["workflow"]["9"]["inputs"]["image"], self.task.staged_inputs[0]["comfy_path"])
        self.assertEqual(row["workflow"]["2"]["inputs"]["text"], "PRIVATE PROMPT <Picture 1>.")
        self.assertEqual(row["content_hash"], json_contract(row["workflow"]).sha256)
        self.assertNotIn("generated_workflow_id", row)
        self.assertNotIn("injected_values", row)
        self.assertFalse(row["safe_to_open"])
        self.assertEqual(row["comfyui_url"], "")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(any(item["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for item in queries))
        self.assertFalse(any("studio_generatedworkflow" in item["sql"].lower() or "studio_promptartifact" in item["sql"].lower() for item in queries))
        self.assertEqual(before, (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count(), GenerationTask.objects.count()))

    def test_missing_receipts_show_prepared_graph_and_never_upload(self):
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[])
        with patch("studio.services.execution_workflow_staging.stage_execution_workflow_media", side_effect=AssertionError("GET must not upload")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["graph_state"], "prepared")
        self.assertFalse(response.data["import_ready"])
        self.assertEqual(response.data["missing_receipt_count"], 1)
        self.assertEqual(response.data["staged_placeholder_count"], 1)
        self.assertIn("不能当作实际已提交图", response.data["open_reason"])

    def test_corrupt_receipts_or_request_are_not_recompiled_or_repaired(self):
        original = deepcopy(self.task.staged_inputs)
        for changes in ({"owner_id": "someone-else"}, {"root_id": str(self.run.pk)},
                {"comfy_path": "../other/file.png"}, {"submitted_sha256": "0" * 64}):
            with self.subTest(changes=changes):
                rows = deepcopy(original)
                rows[0].update(changes)
                GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=rows)
                self.assertEqual(self.client.get(self.url).status_code, 400)
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=original)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_attachment_contains_same_verified_graph(self):
        expected = self.client.get(self.url).data["workflow"]
        response = self.client.get(self.url, {"download": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), expected)
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="autoprompt-task-{self.task.pk}-comfyui-api.json"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_ui_uses_only_actual_target_catalog_and_does_not_replace_frozen_values(self):
        peer = Mock(base_url="http://127.0.0.1:8199")
        peer.object_info.return_value = self.object_info()
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer) as target:
            response = self.client.get(self.url, {"workflow_format": "ui"})
        self.assertEqual(response.status_code, 200, response.data)
        target.assert_called_once()
        self.assertEqual(target.call_args.args[0].pk, self.task.pk)
        peer.object_info.assert_called_once_with()
        peer.submit_prompt.assert_not_called()
        self.assertEqual(response.data["comfyui_url"], "http://127.0.0.1:8199/")
        self.assertEqual(response.data["workflow"]["version"], 0.4)
        self.assertIn(self.task.staged_inputs[0]["comfy_path"], json.dumps(response.data["workflow"]))
        self.assertNotIn("unrelated-server-file.png", json.dumps(response.data["workflow"]))
        self.assertEqual(response.data["content_hash"], json_contract(response.data["workflow"]).sha256)
        self.assertTrue(response.data["import_ready"])

    def test_unavailable_target_does_not_switch_server_or_block_frozen_api_export(self):
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=ComfyUIError("private endpoint mismatch")):
            failed = self.client.get(self.url, {"workflow_format": "ui"})
            frozen = self.client.get(self.url)
        self.assertEqual(failed.status_code, 503, failed.data)
        self.assertEqual(failed.data["code"], "workflow_target_unavailable")
        self.assertNotIn("private endpoint", str(failed.data))
        self.assertEqual(frozen.status_code, 200, frozen.data)

    def test_owner_scope_and_strict_query_contract(self):
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("Invalid requests do not contact target")):
            for query in ("workflow_format=canvas", "workflow_format=api&workflow_format=ui", "download=yes", "obsolete=1"):
                self.assertEqual(self.client.get(self.url + "?" + query).status_code, 400)
        other = get_user_model().objects.create_user(username="workflow-export-other")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"download": "true"}).status_code, 404)
        other.is_staff = True
        other.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(self.url).status_code, 200)
