from io import BytesIO
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset, GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.tests.factories import build_story


def png_upload(name: str) -> SimpleUploadedFile:
    stream = BytesIO()
    Image.new("RGB", (8, 8), color=(38, 62, 88)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


class GenerationTaskInputPromptAPITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_root = Path(self.temp.name) / "media"
        self.asset_root = Path(self.temp.name) / "library"
        self.media_root.mkdir()
        self.asset_root.mkdir()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_root,
            ASSET_LIBRARY_ROOT=self.asset_root,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        (
            self.user,
            self.project,
            self.character,
            _,
            self.episode,
            self.shot,
            _,
        ) = build_story("task-input-prompt-owner")
        self.other = get_user_model().objects.create_user(
            username="task-input-prompt-other", password="StrongPass123!",
        )
        self.staff = get_user_model().objects.create_user(
            username="task-input-prompt-staff", password="StrongPass123!", is_staff=True,
        )
        self.owner_asset = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.CHARACTER,
            name="可预览人物参考",
            file=png_upload("owner-reference.png"),
            mime_type="image/png",
        )
        self.other_asset = Asset.objects.create(
            owner=self.other,
            kind=Asset.Kind.CHARACTER,
            name="其他租户人物参考",
            file=png_upload("other-reference.png"),
            mime_type="image/png",
        )
        self.inactive_asset = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.CHARACTER,
            name="已停用人物参考",
            file=png_upload("inactive-reference.png"),
            mime_type="image/png",
            is_active=False,
        )
        self.missing_asset_id = uuid4()
        self.artifact_prompt = "artifact fallback prompt 人物进入雨巷"
        self.artifact_negative = "artifact fallback negative"
        self.injected_prompt = "actual injected prompt 人物保持青色长衫"
        self.injected_negative = "actual injected negative costume drift"
        self.prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model="minimax-h3",
            h3_mode="ref2va",
            include_audio=True,
            total_duration_ms=5000,
            prompt_text=self.artifact_prompt,
            negative_prompt=self.artifact_negative,
            structured_payload={
                "prompt_template_key": "payload-template-key",
                "private_path": "C:/private/payload/reference.png",
            },
            content_hash="p" * 64,
        )
        self.template = WorkflowTemplate.objects.create(
            name="input-prompt-template",
            slug="input-prompt-template",
            output_kind=PromptArtifact.PromptType.VIDEO,
            is_system=True,
            definition={
                "1": {
                    "class_type": "SaveVideo",
                    "inputs": {"private_path": "E:/private/workflow/output.mp4"},
                },
            },
        )
        self.workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=self.prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            injected_values={
                "PROMPT": self.injected_prompt,
                "NEGATIVE_PROMPT": self.injected_negative,
                "PROMPT_TEMPLATE_KEY": "injected-template-key",
                "PRIVATE_PATH": "C:/private/injected/reference.png",
            },
            content_hash="w" * 64,
        )
        self.task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=self.workflow,
            kind=GenerationTask.Kind.VIDEO,
            model_profile="local_h3_ref2va_official",
            capability_snapshot={
                "reference_assets": [
                    {
                        "asset_id": str(self.owner_asset.id),
                        "name": "stale owner name",
                        "category": "character",
                        "input_slot": 1,
                        "input_label": "<Picture 1>",
                        "local_path": "C:/private/task/owner.png",
                    },
                    {
                        "asset_id": str(self.other_asset.id),
                        "name": "cross tenant snapshot",
                        "category": "character",
                        "input_slot": 2,
                        "input_label": "<Picture 2>",
                        "local_path": "E:/private/task/other.png",
                    },
                    {
                        "asset_id": str(self.missing_asset_id),
                        "name": "missing snapshot",
                        "category": "character",
                        "input_slot": 3,
                        "input_label": "<Picture 3>",
                        "local_path": "/srv/private/task/missing.png",
                    },
                    {
                        "asset_id": str(self.inactive_asset.id),
                        "name": "inactive snapshot",
                        "category": "character",
                        "input_slot": 4,
                        "input_label": "<Picture 4>",
                        "local_path": "C:/private/task/inactive.png",
                    },
                ],
            },
            staged_inputs=[{"source_path": "C:/private/staging/owner.png"}],
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_owner_reads_actual_injected_prompt_without_workflow_or_path_leakage(self):
        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["task_id"], str(self.task.id))
        self.assertEqual(response.data["prompt_artifact_id"], str(self.prompt.id))
        self.assertEqual(response.data["prompt_type"], PromptArtifact.PromptType.VIDEO)
        self.assertEqual(response.data["model"], "minimax-h3")
        self.assertEqual(response.data["h3_mode"], "ref2va")
        self.assertEqual(response.data["effective_conditioning_mode"], "ref2va")
        self.assertEqual(response.data["prompt_template_key"], "injected-template-key")
        self.assertEqual(response.data["content_hash"], self.prompt.content_hash)
        self.assertEqual(response.data["prompt"], self.injected_prompt)
        self.assertEqual(response.data["negative_prompt"], self.injected_negative)
        self.assertEqual(response.data["prompt_character_count"], len(self.injected_prompt))
        self.assertEqual(
            response.data["negative_prompt_character_count"], len(self.injected_negative),
        )
        self.assertEqual(
            response.data["prompt_source"], "generated_workflow.injected_values.PROMPT",
        )
        self.assertEqual(
            response.data["negative_prompt_source"],
            "generated_workflow.injected_values.NEGATIVE_PROMPT",
        )
        self.assertNotIn("injected_values", response.data)
        self.assertNotIn("workflow_json", response.data)
        self.assertNotIn("staged_inputs", response.data)
        encoded = json.dumps(response.data, ensure_ascii=False)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)

    def test_image_prompt_reports_text_only_instead_of_h3_mode(self):
        self.prompt.prompt_type = PromptArtifact.PromptType.IMAGE
        self.prompt.model = "stabilityai/stable-diffusion-xl-base-1.0"
        self.prompt.h3_mode = "T2VA"
        self.prompt.structured_payload = {
            **self.prompt.structured_payload,
            "effective_conditioning_mode": "TEXT_ONLY",
            "prompt_template_key": "sdxl_txt2img_bible_v1",
        }
        self.prompt.save(update_fields=(
            "prompt_type", "model", "h3_mode", "structured_payload", "updated_at",
        ))
        self.task.kind = GenerationTask.Kind.IMAGE
        self.task.capability_snapshot = {
            **self.task.capability_snapshot,
            "effective_conditioning_mode": "TEXT_ONLY",
            "prompt_template_key": "sdxl_txt2img_bible_v1",
        }
        self.task.save(update_fields=("kind", "capability_snapshot", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt_type"], PromptArtifact.PromptType.IMAGE)
        self.assertEqual(response.data["h3_mode"], "")
        self.assertEqual(response.data["effective_conditioning_mode"], "TEXT_ONLY")
        self.assertEqual(response.data["prompt_template_key"], "sdxl_txt2img_bible_v1")

    def test_prompt_falls_back_to_artifact_when_injected_text_is_absent(self):
        self.workflow.injected_values = {"PROMPT_TEMPLATE_KEY": "injected-template-key"}
        self.workflow.save(update_fields=("injected_values", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt"], self.artifact_prompt)
        self.assertEqual(response.data["negative_prompt"], self.artifact_negative)
        self.assertEqual(response.data["prompt_source"], "prompt_artifact.prompt_text")
        self.assertEqual(
            response.data["negative_prompt_source"], "prompt_artifact.negative_prompt",
        )
        self.assertEqual(response.data["prompt_character_count"], len(self.artifact_prompt))
        self.assertEqual(
            response.data["negative_prompt_character_count"], len(self.artifact_negative),
        )

    def test_input_prompt_uses_task_owner_scope_and_allows_staff(self):
        self.client.force_authenticate(self.other)
        hidden = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")
        self.assertEqual(hidden.status_code, 404, hidden.data)

        self.client.force_authenticate(self.staff)
        visible = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")
        self.assertEqual(visible.status_code, 200, visible.data)

        self.client.force_authenticate(user=None)
        unauthenticated = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")
        self.assertIn(unauthenticated.status_code, {401, 403})

    def test_task_input_metadata_and_reference_previews_are_safe_and_tenant_scoped(self):
        # Live catalog edits must not rewrite the persisted task description;
        # only preview availability is resolved against current Asset state.
        self.owner_asset.name = "后来改名的素材"
        self.owner_asset.kind = Asset.Kind.REFERENCE_VIDEO
        self.owner_asset.save(update_fields=("name", "kind", "updated_at"))
        response = self.client.get(f"/api/v1/tasks/{self.task.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        parameters = response.data["input_parameters"]
        self.assertTrue(parameters["prompt_available"])
        self.assertEqual(parameters["prompt_character_count"], len(self.injected_prompt))
        self.assertEqual(
            parameters["negative_prompt_character_count"], len(self.injected_negative),
        )
        task_payload = json.dumps(response.data, ensure_ascii=False, default=str)
        self.assertNotIn(self.injected_prompt, task_payload)
        self.assertNotIn(self.injected_negative, task_payload)
        references = {item["asset_id"]: item for item in parameters["reference_assets"]}
        owner_reference = references[str(self.owner_asset.id)]
        self.assertEqual(owner_reference["media_kind"], "image")
        self.assertEqual(owner_reference["display_name"], "stale owner name")
        self.assertTrue(owner_reference["preview_url"].startswith("/api/v1/media/"))
        self.assertEqual(references[str(self.other_asset.id)]["preview_url"], "")
        self.assertEqual(references[str(self.missing_asset_id)]["preview_url"], "")
        self.assertEqual(references[str(self.inactive_asset.id)]["preview_url"], "")
        encoded = json.dumps(parameters, ensure_ascii=False)
        self.assertNotIn(self.injected_prompt, encoded)
        self.assertNotIn(self.injected_negative, encoded)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)
        self.assertNotIn("/srv/private", encoded)

        anonymous = APIClient()
        preview = anonymous.get(owner_reference["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        preview.close()

    def test_task_list_uses_snapshot_summaries_without_asset_queries_or_filesystem_previews(self):
        for index in range(2):
            GenerationTask.objects.create(
                owner=self.user,
                project=self.project,
                episode=self.episode,
                generated_workflow=self.workflow,
                kind=GenerationTask.Kind.VIDEO,
                model_profile=f"list-task-{index}",
                capability_snapshot=self.task.capability_snapshot,
                staged_inputs=[{"source_path": f"C:/private/staging/list-{index}.png"}],
            )

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/tasks/")

        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(
            Asset._meta.db_table.casefold() in query["sql"].casefold()
            for query in captured.captured_queries
        ), [query["sql"] for query in captured.captured_queries])
        for row in rows:
            self.assertNotIn("capability_snapshot", row)
            self.assertEqual(row["staged_inputs"], [])
            self.assertEqual(row["outputs"], [])
            self.assertEqual(row["error_message"], "")
            self.assertEqual(row["remote_error"], "")
            self.assertEqual(row["timeline"], [])
            references = row["input_parameters"]["reference_assets"]
            self.assertTrue(references)
            self.assertTrue(all(item["preview_url"] == "" for item in references))
        encoded = json.dumps(rows, ensure_ascii=False, default=str)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)
        self.assertNotIn("/srv/private", encoded)
