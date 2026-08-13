from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import AssemblyJob, GeneratedWorkflow, GenerationTask, PromptArtifact, WorkflowTemplate
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.tests.factories import build_story


class GenerationTaskDeleteAPITests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            _,
            _,
            self.episode,
            self.shot,
            self.shot2,
        ) = build_story("task-delete-owner")
        result = build_h3_video_prompt(self.project, [self.shot], H3PromptOptions())
        self.prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text=result.text,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            total_duration_ms=result.total_duration_ms,
            content_hash=result.content_hash,
        )
        self.template = WorkflowTemplate.objects.create(
            name="task-delete-template",
            slug="task-delete-template",
            output_kind=PromptArtifact.PromptType.VIDEO,
            is_system=True,
            definition={"1": {"class_type": "SaveVideo", "inputs": {}}},
        )
        self.workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=self.prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            injected_values={"PROMPT": result.text, "REQUESTED_DURATION_MS": 3500},
            content_hash="d" * 64,
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_file = Path(self.temp.name) / "retained-output.mp4"
        self.media_file.write_bytes(b"completed-video")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_task(self, task_status: str) -> GenerationTask:
        return GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=self.workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=task_status,
            progress=100 if task_status == GenerationTask.Status.SUCCEEDED else 0,
            outputs=[{"filename": str(self.media_file)}],
        )

    def create_succeeded_assembly(self, task: GenerationTask) -> AssemblyJob:
        assembly = AssemblyJob.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            scope=AssemblyJob.Scope.EPISODE,
            status=AssemblyJob.Status.SUCCEEDED,
            dry_run=False,
            output_path=str(Path(self.temp.name) / "assembled.mp4"),
        )
        assembly.source_tasks.add(task)
        return assembly

    def test_owner_can_delete_terminal_history_without_deleting_dependencies_or_media(self):
        for task_status in (
            GenerationTask.Status.SUCCEEDED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        ):
            with self.subTest(status=task_status):
                task = self.create_task(task_status)
                replacement = (
                    self.create_task(GenerationTask.Status.SUCCEEDED)
                    if task_status == GenerationTask.Status.SUCCEEDED else None
                )

                response = self.client.delete(f"/api/v1/tasks/{task.id}/")

                self.assertEqual(response.status_code, 204, response.data)
                self.assertFalse(GenerationTask.objects.filter(pk=task.id).exists())
                self.assertTrue(GeneratedWorkflow.objects.filter(pk=self.workflow.id).exists())
                self.assertTrue(PromptArtifact.objects.filter(pk=self.prompt.id).exists())
                self.assertTrue(WorkflowTemplate.objects.filter(pk=self.template.id).exists())
                self.assertTrue(self.media_file.is_file())
                self.assertEqual(self.media_file.read_bytes(), b"completed-video")
                if replacement:
                    replacement.delete()

    def test_only_successful_video_source_for_shot_cannot_be_deleted(self):
        task = self.create_task(GenerationTask.Status.SUCCEEDED)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("唯一可组装的成功视频产物", str(response.data))
        self.assertIn("镜头 01", str(response.data))
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())

    def test_successful_task_referenced_by_succeeded_assembly_cannot_be_deleted(self):
        task = self.create_task(GenerationTask.Status.SUCCEEDED)
        self.create_task(GenerationTask.Status.SUCCEEDED)
        assembly = self.create_succeeded_assembly(task)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("成功组装成片引用", str(response.data))
        self.assertIn("来源审计链", str(response.data))
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(assembly.source_tasks.filter(pk=task.id).exists())

    def test_failed_task_referenced_by_succeeded_assembly_cannot_be_deleted(self):
        task = self.create_task(GenerationTask.Status.FAILED)
        assembly = self.create_succeeded_assembly(task)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("成功组装成片引用", str(response.data))
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(assembly.source_tasks.filter(pk=task.id).exists())

    def test_unreferenced_failed_task_can_be_deleted(self):
        task = self.create_task(GenerationTask.Status.FAILED)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(GenerationTask.objects.filter(pk=task.id).exists())

    def test_multishot_source_is_protected_for_every_covered_shot(self):
        self.prompt.shot = None
        self.prompt.is_multi_shot = True
        self.prompt.request_options = {
            "shot_ids": [str(self.shot.id), str(self.shot2.id)],
        }
        self.prompt.save(update_fields=(
            "shot", "is_multi_shot", "request_options", "updated_at",
        ))
        task = self.create_task(GenerationTask.Status.SUCCEEDED)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("镜头 01", str(response.data))
        self.assertIn("镜头 02", str(response.data))
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())

    def test_successful_task_with_non_video_output_does_not_block_history_cleanup(self):
        task = self.create_task(GenerationTask.Status.SUCCEEDED)
        task.outputs = [{"kind": "images", "filename": "preview.png"}]
        task.save(update_fields=("outputs", "updated_at"))

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(GenerationTask.objects.filter(pk=task.id).exists())

    def test_active_and_cancellation_pending_tasks_cannot_be_deleted(self):
        for task_status in (
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        ):
            with self.subTest(status=task_status):
                task = self.create_task(task_status)

                response = self.client.delete(f"/api/v1/tasks/{task.id}/")

                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("不可删除", str(response.data))
                self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())

    def test_other_tenant_cannot_see_or_delete_task(self):
        task = self.create_task(GenerationTask.Status.FAILED)
        assembly = self.create_succeeded_assembly(task)
        other = get_user_model().objects.create_user(
            username="task-delete-other", password="StrongPass123!",
        )
        self.client.force_authenticate(other)

        response = self.client.delete(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(assembly.source_tasks.filter(pk=task.id).exists())
