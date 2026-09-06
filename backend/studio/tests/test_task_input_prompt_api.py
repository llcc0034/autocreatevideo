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

from studio.models import (
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import json_contract
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
        self.artifact_prompt = "archived authoring prompt 人物进入雨巷"
        self.artifact_negative = "archived authoring negative"
        self.injected_prompt = "actual injected prompt 人物保持青色长衫"
        self.injected_negative = "actual injected negative costume drift"
        self.prompt = PromptArtifact.objects.create(is_multi_shot=False,
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model="minimax-h3",
            conditioning_mode="ref2va",
            include_audio=True,
            total_duration_ms=5000,
            prompt_text=self.artifact_prompt,
            negative_prompt=self.artifact_negative,
            structured_payload={
                "prompt_template_key": "payload-template-key",
                "private_path": "C:/private/payload/reference.png",
            },
            content_hash="b" * 64,
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
            injected_values={},
            content_hash=json_contract(self.template.definition).sha256,
            compiler_id="semantic_test_compiler",
            compiler_version=1,
            route_snapshot_sha256="r" * 64,
            workflow_contract_sha256="c" * 64,
        )
        graph_contract = json_contract(self.template.definition)
        input_contract = {"schema_version": 2}
        input_contract_json = json_contract(input_contract)
        self.version = WorkflowTemplateVersion.objects.create(
            template=self.template,
            version=1,
            graph=self.template.definition,
            graph_size_bytes=graph_contract.size_bytes,
            graph_sha256=graph_contract.sha256,
            input_contract=input_contract,
            input_contract_size_bytes=input_contract_json.size_bytes,
            input_contract_sha256=input_contract_json.sha256,
            compiler_id="semantic_test_compiler",
            compiler_version=1,
            semantic_bindings_sha256=json_contract([]).sha256,
            model_requirements_sha256=json_contract([]).sha256,
            execution_contract_sha256=self.workflow.workflow_contract_sha256,
        )
        self.workflow.workflow_template_version = self.version
        self.workflow.model_requirements_sha256 = json_contract([]).sha256
        self.workflow.save(update_fields=(
            "workflow_template_version", "model_requirements_sha256", "updated_at",
        ))
        reference_assets = [
            {
                "asset_id": str(self.owner_asset.id),
                "name": "stale owner name",
                "category": "character",
                "media_kind": "image",
                "input_slot": 1,
                "input_label": "<Picture 1>",
                "local_path": "C:/private/task/owner.png",
            },
            {
                "asset_id": str(self.other_asset.id),
                "name": "cross tenant snapshot",
                "category": "character",
                "media_kind": "image",
                "input_slot": 2,
                "input_label": "<Picture 2>",
                "local_path": "E:/private/task/other.png",
            },
            {
                "asset_id": str(self.missing_asset_id),
                "name": "missing snapshot",
                "category": "character",
                "media_kind": "image",
                "input_slot": 3,
                "input_label": "<Picture 3>",
                "local_path": "/srv/private/task/missing.png",
            },
            {
                "asset_id": str(self.inactive_asset.id),
                "name": "inactive snapshot",
                "category": "character",
                "media_kind": "image",
                "input_slot": 4,
                "input_label": "<Picture 4>",
                "local_path": "C:/private/task/inactive.png",
            },
        ]
        capability_snapshot = self.freeze_current_contract(
            reference_assets=reference_assets,
        )
        self.task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=self.workflow,
            kind=GenerationTask.Kind.VIDEO,
            backend="comfyui_local",
            model_profile="fictional-video-model",
            workflow_version=1,
            seed=42,
            effective_frames=120,
            effective_duration_ms=5000,
            max_attempts=3,
            capability_snapshot=capability_snapshot,
            staged_inputs=[{"source_path": "C:/private/staging/owner.png"}],
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def freeze_current_contract(
        self,
        *,
        kind=GenerationTask.Kind.VIDEO,
        reference_assets=None,
        negative_prompt="__default__",
    ) -> dict:
        is_image = kind == GenerationTask.Kind.IMAGE
        references = list(reference_assets or [])
        semantics = {
            "prompt.positive": self.injected_prompt,
            "image.width": 672,
            "image.height": 1216,
            "sampling.seed": 42,
        }
        if negative_prompt == "__default__":
            semantics["prompt.negative"] = self.injected_negative
        elif negative_prompt is not None:
            semantics["prompt.negative"] = negative_prompt
        if not is_image:
            semantics.update({
                "video.fps": 24,
                "video.duration_ms": 5000,
                "video.frames": 120,
                "audio.enabled": True,
            })
        mode = "" if is_image else "OrbitRefs"
        selected_mode = None if is_image else {"key": mode}
        conditioning_contract = {
            "schema_version": 1,
            "modes": [] if is_image else [selected_mode],
        }
        monitoring = {"schema_version": 1, "stages": []}
        metadata = {
            "schema_version": 1,
            "workflow_version_id": str(self.version.id),
            "workflow_version": 1,
            "template": {
                "id": str(self.template.id),
                "slug": self.template.slug,
                "name": self.template.name,
                "output_kind": kind,
            },
            "model_identifier": "fictional-image-model" if is_image else "fictional-video-model",
            "workflow_mode": "semantic",
            "source_workflow": "fictional/current-workflow.json",
            "required_dimension_multiple": 16,
            "conditioning_contract": conditioning_contract,
            "conditioning_contract_sha256": json_contract(conditioning_contract).sha256,
            "conditioning_mode_contract": selected_mode,
            "conditioning_mode_contract_sha256": json_contract(selected_mode).sha256 if selected_mode else "",
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": json_contract(monitoring).sha256,
        }
        metadata_sha256 = json_contract(metadata).sha256
        semantics_sha256 = json_contract(semantics).sha256
        bindings_sha256 = json_contract(references).sha256
        self.workflow.injected_values = {
            "schema_version": 2,
            "resolved_semantics": semantics,
            "asset_bindings": references,
            "model_requirements": [],
            "output_contract": {
                "schema_version": 1,
                "primary": {"node_id": "1", "kind": kind},
            },
            "execution_metadata": metadata,
            "execution_metadata_sha256": metadata_sha256,
        }
        self.workflow.resolved_semantics_sha256 = semantics_sha256
        self.workflow.asset_bindings_sha256 = bindings_sha256
        self.workflow.save(update_fields=(
            "injected_values", "resolved_semantics_sha256",
            "asset_bindings_sha256", "updated_at",
        ))
        self.prompt.prompt_type = kind
        self.prompt.model = metadata["model_identifier"]
        self.prompt.conditioning_mode = mode
        self.prompt.structured_payload = {
            "effective_conditioning_mode": "TEXT_ONLY" if is_image else mode,
            "prompt_template_key": "fictional:image:v1" if is_image else "fictional:video:v1",
        }
        self.prompt.save(update_fields=(
            "prompt_type", "model", "conditioning_mode", "structured_payload", "updated_at",
        ))
        self.template.output_kind = kind
        self.template.save(update_fields=("output_kind", "updated_at"))
        snapshot = {
            "generated_workflow_id": str(self.workflow.id),
            "workflow_hash": self.workflow.content_hash,
            "prompt_artifact_id": str(self.prompt.id),
            "prompt_content_hash": self.prompt.content_hash,
            "template_id": str(self.template.id),
            "template_slug": self.template.slug,
            "model": metadata["model_identifier"],
            "conditioning_mode": mode,
            "effective_conditioning_mode": "TEXT_ONLY" if is_image else mode,
            "prompt_template_key": self.prompt.structured_payload["prompt_template_key"],
            "workflow_mode": metadata["workflow_mode"],
            "source_workflow": metadata["source_workflow"],
            "workflow_parameters": semantics,
            "resolution_profile": "semantic_contract",
            "resolution_multiple": 16,
            "width": 672,
            "height": 1216,
            "shot_ids": [str(self.shot.id)],
            "shot_count": 1,
            "whole_episode": False,
            "workflow_template_version_id": str(self.version.id),
            "workflow_contract_sha256": self.workflow.workflow_contract_sha256,
            "route_snapshot_sha256": self.workflow.route_snapshot_sha256,
            "asset_bindings": references,
            "asset_bindings_sha256": bindings_sha256,
            "continuity_assets": [],
            "continuity_assets_sha256": json_contract([]).sha256,
            "resolved_semantics_sha256": semantics_sha256,
            "model_requirements": [],
            "model_requirements_sha256": self.workflow.model_requirements_sha256,
            "output_contract": self.workflow.injected_values["output_contract"],
            "execution_metadata": metadata,
            "execution_metadata_sha256": metadata_sha256,
        }
        if not is_image:
            snapshot.update({"fps": 24, "requested_duration_ms": 5000, "include_audio": True})
        return snapshot

    def test_owner_reads_exact_semantic_prompt_without_workflow_or_path_leakage(self):
        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["task_id"], str(self.task.id))
        self.assertEqual(response.data["prompt_artifact_id"], str(self.prompt.id))
        self.assertEqual(response.data["prompt_type"], PromptArtifact.PromptType.VIDEO)
        self.assertEqual(response.data["model"], "fictional-video-model")
        self.assertEqual(response.data["conditioning_mode"], "OrbitRefs")
        self.assertEqual(response.data["effective_conditioning_mode"], "OrbitRefs")
        self.assertEqual(response.data["prompt_template_key"], "fictional:video:v1")
        self.assertEqual(response.data["content_hash"], self.prompt.content_hash)
        self.assertEqual(response.data["prompt"], self.injected_prompt)
        self.assertEqual(response.data["negative_prompt"], self.injected_negative)
        self.assertEqual(response.data["prompt_character_count"], len(self.injected_prompt))
        self.assertEqual(
            response.data["negative_prompt_character_count"], len(self.injected_negative),
        )
        self.assertEqual(
            response.data["prompt_source"],
            "generated_workflow.injected_values.resolved_semantics.prompt.positive",
        )
        self.assertEqual(
            response.data["negative_prompt_source"],
            "generated_workflow.injected_values.resolved_semantics.prompt.negative",
        )
        self.assertTrue(response.data["negative_prompt_submitted"])
        self.assertNotIn("injected_values", response.data)
        self.assertNotIn("workflow_json", response.data)
        self.assertNotIn("staged_inputs", response.data)
        encoded = json.dumps(response.data, ensure_ascii=False)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)

    def test_live_prompt_artifact_changes_do_not_rewrite_frozen_task_projection(self):
        frozen_hash = self.task.capability_snapshot["prompt_content_hash"]
        self.prompt.model = "mutated-live-model"
        self.prompt.prompt_type = PromptArtifact.PromptType.IMAGE
        self.prompt.content_hash = "9" * 64
        self.prompt.save(update_fields=(
            "model", "prompt_type", "content_hash", "updated_at",
        ))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt_type"], GenerationTask.Kind.VIDEO)
        self.assertEqual(response.data["model"], "fictional-video-model")
        self.assertEqual(response.data["content_hash"], frozen_hash)

    def test_image_prompt_reports_text_only_instead_of_video_conditioning_mode(self):
        snapshot = self.freeze_current_contract(kind=GenerationTask.Kind.IMAGE)
        self.task.kind = GenerationTask.Kind.IMAGE
        self.task.model_profile = "fictional-image-model"
        self.task.seed = 42
        self.task.effective_frames = 0
        self.task.effective_duration_ms = 0
        self.task.capability_snapshot = snapshot
        self.task.save(update_fields=(
            "kind", "model_profile", "seed", "effective_frames",
            "effective_duration_ms", "capability_snapshot", "updated_at",
        ))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt_type"], PromptArtifact.PromptType.IMAGE)
        self.assertEqual(response.data["conditioning_mode"], "")
        self.assertEqual(response.data["effective_conditioning_mode"], "TEXT_ONLY")
        self.assertEqual(response.data["prompt_template_key"], "fictional:image:v1")

    def test_missing_resolved_prompt_fails_closed_without_artifact_fallback(self):
        snapshot = self.freeze_current_contract()
        injected = dict(self.workflow.injected_values)
        semantics = dict(injected["resolved_semantics"])
        semantics.pop("prompt.positive")
        injected["resolved_semantics"] = semantics
        semantics_sha256 = json_contract(semantics).sha256
        self.workflow.injected_values = injected
        self.workflow.resolved_semantics_sha256 = semantics_sha256
        self.workflow.save(update_fields=(
            "injected_values", "resolved_semantics_sha256", "updated_at",
        ))
        snapshot["workflow_parameters"] = semantics
        snapshot["resolved_semantics_sha256"] = semantics_sha256
        self.task.capability_snapshot = snapshot
        self.task.save(update_fields=("capability_snapshot", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("缺少实际提交的正向提示词", str(response.data))

    def test_semantic_workflow_reports_archived_negative_as_not_submitted(self):
        snapshot = self.freeze_current_contract(negative_prompt=None)
        self.task.capability_snapshot = snapshot
        self.task.save(update_fields=("capability_snapshot", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{self.task.id}/input-prompt/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["prompt"], self.injected_prompt)
        self.assertEqual(
            response.data["prompt_source"],
            "generated_workflow.injected_values.resolved_semantics.prompt.positive",
        )
        self.assertEqual(response.data["negative_prompt"], "")
        self.assertEqual(response.data["negative_prompt_source"], "not_submitted")
        self.assertFalse(response.data["negative_prompt_submitted"])

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
                effective_frames=self.task.effective_frames,
                effective_duration_ms=self.task.effective_duration_ms,
                max_attempts=3,
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
