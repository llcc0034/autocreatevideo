import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.tests.media_prompt_fixtures import MediaPromptOptions, build_test_video_prompt
from studio.tests.factories import build_story


class GenerationTaskWorkflowExportAPITests(TestCase):
    @staticmethod
    def object_info():
        return {
            "LoadImage": {
                "input": {
                    "required": {
                        "image": [
                            ["unrelated-server-file.png"],
                            {"image_upload": True},
                        ],
                    },
                },
                "input_order": {"required": ["image"]},
                "output": ["IMAGE", "MASK"],
                "output_name": ["IMAGE", "MASK"],
            },
            "SaveImage": {
                "input": {
                    "required": {
                        "images": ["IMAGE", {}],
                        "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    },
                },
                "input_order": {"required": ["images", "filename_prefix"]},
                "output": ["IMAGE"],
                "output_name": ["images"],
                "output_node": True,
            },
        }

    def setUp(self):
        user, project, _, _, episode, shot, _ = build_story()
        prompt_result = build_test_video_prompt(
            project, [shot], MediaPromptOptions(
                model_identifier="minimax-h3-test", language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=True,
            project=project,
            episode=episode,
            shot=shot,
            created_by=user,
            prompt_type="video",
            total_duration_ms=prompt_result.total_duration_ms,
            prompt_text=prompt_result.text,
            structured_payload=prompt_result.payload,
            consistency_snapshot=prompt_result.consistency_snapshot,
            content_hash=prompt_result.content_hash,
        )
        template = WorkflowTemplate.objects.create(
            name="export-system-template",
            slug="export-system-template",
            output_kind="video",
            is_system=True,
            version=3,
            definition={"1": {"class_type": "SaveImage", "inputs": {}}},
        )
        self.graph = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": "__AUTOPROMPT_ASSET__:fixture-asset-id"},
            },
            "2": {
                "class_type": "SaveImage",
                "inputs": {"images": ["1", 0]},
            },
        }
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            episode=episode,
            prompt_artifact=prompt,
            template=template,
            created_by=user,
            workflow_json=self.graph,
            injected_values={"SEED": 42},
            content_hash="b" * 64,
        )
        self.user = user
        self.task = GenerationTask.objects.create(
            owner=user,
            project=project,
            episode=episode,
            generated_workflow=workflow,
            kind="video",
            status=GenerationTask.Status.RUNNING,
            workflow_version=template.version,
            attempts=1,
            max_attempts=3,
            progress=37,
            submission_token="persisted-token",
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        self.url = f"/api/v1/tasks/{self.task.id}/comfyui-workflow/"
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_owner_can_inspect_exact_graph_without_touching_task_or_comfyui(self):
        before = {
            "status": self.task.status,
            "progress": self.task.progress,
            "attempts": self.task.attempts,
            "submission_token": self.task.submission_token,
            "submission_state": self.task.submission_state,
            "updated_at": self.task.updated_at,
        }

        with (
            patch("studio.views.ComfyUIClient") as comfyui_client,
            patch(
                "studio.services.comfyui.default_comfyui_runtime_config",
                return_value={"base_url": "http://127.0.0.1:8188"},
            ),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.data)
        comfyui_client.assert_not_called()
        self.assertEqual(response.data["task_id"], str(self.task.id))
        self.assertEqual(response.data["generated_workflow_id"], str(self.task.generated_workflow_id))
        self.assertEqual(response.data["format"], "comfyui_api")
        self.assertEqual(response.data["workflow"], self.graph)
        self.assertFalse(response.data["import_ready"])
        self.assertIn("尚未全部安全上传", response.data["open_reason"])
        self.assertTrue(response.data["contains_staged_placeholders"])
        self.assertEqual(response.data["staged_placeholder_count"], 1)
        self.assertTrue(response.data["safe_to_open"])
        self.assertEqual(response.data["comfyui_url"], "http://127.0.0.1:8188/")
        self.assertFalse(response.data["direct_open_supported"])
        self.assertIsNone(response.data["direct_open_url"])
        self.assertEqual(response.data["open_mode"], "download_import")
        self.assertIn("?download=true", response.data["download_url"])
        self.assertEqual(response["Cache-Control"], "private, no-store")

        self.task.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(self.task, field), value)

    def test_owner_can_download_exact_graph_as_attachment(self):
        with patch("studio.views.ComfyUIClient") as comfyui_client:
            response = self.client.get(f"{self.url}?download=true")

        self.assertEqual(response.status_code, 200)
        comfyui_client.assert_not_called()
        self.assertEqual(json.loads(response.content.decode("utf-8")), self.graph)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="autoprompt-task-{self.task.id}-comfyui-api.json"',
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_owner_can_inspect_importable_ui_canvas_with_staged_input_path(self):
        self.task.staged_inputs = [{
            "token": "__AUTOPROMPT_ASSET__:fixture-asset-id",
            "comfy_path": "autoprompt/user/task/reference.png",
        }]
        self.task.save(update_fields=("staged_inputs", "updated_at"))

        with patch("studio.views.ComfyUIClient") as comfyui_client:
            comfyui_client.return_value.object_info.return_value = self.object_info()
            response = self.client.get(f"{self.url}?workflow_format=ui")

        self.assertEqual(response.status_code, 200, response.data)
        comfyui_client.return_value.object_info.assert_called_once_with()
        self.assertEqual(response.data["format"], "comfyui_ui")
        self.assertTrue(response.data["import_ready"])
        self.assertFalse(response.data["contains_staged_placeholders"])
        self.assertEqual(
            response.data["filename"],
            f"autoprompt-task-{self.task.id}-comfyui-ui.json",
        )
        self.assertEqual(
            response.data["available_formats"],
            ["comfyui_api", "comfyui_ui"],
        )
        self.assertIn("workflow_format=ui&download=true", response.data["download_url"])
        canvas = response.data["workflow"]
        self.assertEqual(canvas["version"], 0.4)
        self.assertEqual([node["type"] for node in canvas["nodes"]], ["LoadImage", "SaveImage"])
        self.assertEqual(canvas["links"], [[1, 1, 0, 2, 0, "IMAGE"]])
        load_image = canvas["nodes"][0]
        self.assertEqual(
            load_image["widgets_values"],
            ["autoprompt/user/task/reference.png", "image"],
        )
        self.assertEqual([item["name"] for item in load_image["inputs"]], ["image", "upload"])
        self.assertNotIn("unrelated-server-file.png", json.dumps(canvas))
        save_image = canvas["nodes"][1]
        self.assertEqual(save_image["inputs"][0]["link"], 1)
        self.assertEqual(save_image["widgets_values"], ["ComfyUI"])

    def test_owner_can_download_ui_canvas_without_changing_api_download_contract(self):
        self.task.staged_inputs = [{
            "token": "__AUTOPROMPT_ASSET__:fixture-asset-id",
            "comfy_path": "autoprompt/user/task/reference.png",
        }]
        self.task.save(update_fields=("staged_inputs", "updated_at"))

        with patch("studio.views.ComfyUIClient") as comfyui_client:
            comfyui_client.return_value.object_info.return_value = self.object_info()
            response = self.client.get(f"{self.url}?workflow_format=ui&download=true")

        self.assertEqual(response.status_code, 200)
        canvas = json.loads(response.content.decode("utf-8"))
        self.assertEqual(canvas["version"], 0.4)
        self.assertEqual(canvas["links"], [[1, 1, 0, 2, 0, "IMAGE"]])
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="autoprompt-task-{self.task.id}-comfyui-ui.json"',
        )

    def test_unknown_export_format_is_rejected_without_contacting_comfyui(self):
        with patch("studio.views.ComfyUIClient") as comfyui_client:
            response = self.client.get(f"{self.url}?workflow_format=arbitrary")

        self.assertEqual(response.status_code, 400, response.data)
        comfyui_client.assert_not_called()

    def test_ui_export_reports_comfyui_schema_unavailability_but_api_export_still_works(self):
        from studio.services.comfyui import ComfyUIError

        with patch("studio.views.ComfyUIClient") as comfyui_client:
            comfyui_client.return_value.object_info.side_effect = ComfyUIError("offline")
            ui_response = self.client.get(f"{self.url}?workflow_format=ui")

        self.assertEqual(ui_response.status_code, 503, ui_response.data)
        self.assertIn("object_info", ui_response.data["detail"])

        with patch("studio.views.ComfyUIClient") as comfyui_client:
            api_response = self.client.get(self.url)
        self.assertEqual(api_response.status_code, 200, api_response.data)
        comfyui_client.assert_not_called()

    def test_workflow_export_preserves_owner_isolation_but_allows_staff(self):
        other = get_user_model().objects.create_user(
            username="workflow-export-other",
            password="StrongPass123!",
        )
        self.client.force_authenticate(other)
        hidden = self.client.get(self.url)
        self.assertEqual(hidden.status_code, 404)

        staff = get_user_model().objects.create_user(
            username="workflow-export-staff",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_authenticate(staff)
        visible = self.client.get(self.url)
        self.assertEqual(visible.status_code, 200, visible.data)
        self.assertEqual(visible.data["workflow"], self.graph)

    def test_non_http_comfyui_url_is_never_exposed_as_openable(self):
        with patch(
            "studio.services.comfyui.default_comfyui_runtime_config",
            return_value={"base_url": "file:///unsafe/local/path"},
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["comfyui_url"], "")
        self.assertFalse(response.data["safe_to_open"])
        self.assertFalse(response.data["direct_open_supported"])
