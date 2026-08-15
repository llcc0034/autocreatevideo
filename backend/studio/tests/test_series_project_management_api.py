from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import (
    AssemblyJob,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    ReleaseBundle,
    SeriesProject,
    WorkflowTemplate,
)
from studio.tests.factories import build_story


class SeriesProjectManagementAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _create_generation_task(
        self,
        *,
        user,
        project,
        suffix,
        task_status,
        submission_state=GenerationTask.SubmissionState.UNPREPARED,
        submission_token="",
        remote_state=GenerationTask.RemoteState.UNKNOWN,
    ):
        artifact = PromptArtifact.objects.create(
            project=project,
            created_by=user,
            prompt_text=f"project delete {suffix}",
            content_hash=suffix.encode("utf-8").hex().ljust(64, "0")[:64],
        )
        template = WorkflowTemplate.objects.create(
            owner=user,
            name=f"Project delete {suffix}",
            slug=f"project-delete-{suffix}",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            prompt_artifact=artifact,
            template=template,
            created_by=user,
            content_hash=suffix.encode("utf-8").hex().rjust(64, "0")[-64:],
        )
        task = GenerationTask.objects.create(
            owner=user,
            project=project,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=task_status,
            submission_state=submission_state,
            submission_token=submission_token,
            comfy_prompt_id=submission_token,
            remote_state=remote_state,
        )
        return artifact, workflow, task

    def test_owner_can_favorite_project_but_other_tenant_cannot_patch_it(self):
        owner = get_user_model().objects.create_user(
            username="favorite-owner", password="StrongPass123!",
        )
        other = get_user_model().objects.create_user(
            username="favorite-other", password="StrongPass123!",
        )
        project = SeriesProject.objects.create(owner=owner, title="待收藏连续剧")

        self.client.force_authenticate(owner)
        response = self.client.patch(
            f"/api/v1/projects/{project.id}/", {"is_favorite": True}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_favorite"])
        project.refresh_from_db()
        self.assertTrue(project.is_favorite)

        self.client.force_authenticate(other)
        response = self.client.patch(
            f"/api/v1/projects/{project.id}/", {"is_favorite": False}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)
        project.refresh_from_db()
        self.assertTrue(project.is_favorite)

    def test_create_project_returns_zero_aggregate_fields(self):
        user = get_user_model().objects.create_user(
            username="create-project-summary", password="StrongPass123!",
        )
        self.client.force_authenticate(user)

        response = self.client.post(
            "/api/v1/projects/",
            {"title": "新建项目摘要", "logline": "创建响应包含稳定聚合值。"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["episode_count"], 0)
        self.assertEqual(response.data["completed_episode_count"], 0)
        self.assertEqual(response.data["total_duration_ms"], 0)
        self.assertEqual(response.data["character_count"], 0)
        self.assertEqual(response.data["scene_count"], 0)
        self.assertEqual(response.data["prop_count"], 0)

    def test_plain_project_delete_cascades_project_owned_story_data(self):
        user, project, character, scene, episode, shot1, shot2 = build_story(
            "project-delete-cascade",
        )
        self.client.force_authenticate(user)

        response = self.client.delete(f"/api/v1/projects/{project.id}/")

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(SeriesProject.objects.filter(pk=project.pk).exists())
        for item in (character, scene, episode, shot1, shot2):
            self.assertFalse(type(item).objects.filter(pk=item.pk).exists())

    def test_project_list_exposes_episode_progress_and_total_duration(self):
        user, project, *_story, episode, _shot1, _shot2 = build_story(
            "project-list-summary",
        )
        episode.status = episode.Status.COMPLETED
        episode.save(update_fields=("status", "updated_at"))
        self.client.force_authenticate(user)

        response = self.client.get("/api/v1/projects/")

        self.assertEqual(response.status_code, 200, response.data)
        payload = next(
            item for item in response.data["results"] if item["id"] == str(project.id)
        )
        self.assertEqual(payload["episode_count"], 1)
        self.assertEqual(payload["completed_episode_count"], 1)
        self.assertEqual(payload["total_duration_ms"], 5600)

    def test_other_tenant_cannot_delete_project(self):
        owner = get_user_model().objects.create_user(
            username="delete-owner", password="StrongPass123!",
        )
        other = get_user_model().objects.create_user(
            username="delete-other", password="StrongPass123!",
        )
        project = SeriesProject.objects.create(owner=owner, title="不可跨租户删除")
        self.client.force_authenticate(other)

        response = self.client.delete(f"/api/v1/projects/{project.id}/")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())

    def test_active_generation_tasks_block_project_delete(self):
        user, project, *_ = build_story("project-delete-generation")
        artifact = PromptArtifact.objects.create(
            project=project,
            created_by=user,
            prompt_text="delete guard",
            content_hash="a" * 64,
        )
        template = WorkflowTemplate.objects.create(
            owner=user,
            name="Delete guard template",
            slug="delete-guard-template",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            prompt_artifact=artifact,
            template=template,
            created_by=user,
            content_hash="b" * 64,
        )
        task = GenerationTask.objects.create(
            owner=user,
            project=project,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
        )
        self.client.force_authenticate(user)

        for task_status in (
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        ):
            with self.subTest(status=task_status):
                task.status = task_status
                task.save(update_fields=("status", "updated_at"))
                response = self.client.delete(f"/api/v1/projects/{project.id}/")
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["error"]["code"], "project_has_active_work")
                active_count = response.data["error"]["details"]["active_generation_tasks"]
                self.assertIsInstance(active_count, int)
                self.assertEqual(active_count, 1)
                self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())

    def test_terminal_generation_history_is_deleted_with_project(self):
        cases = (
            (
                "locally-succeeded",
                GenerationTask.Status.SUCCEEDED,
                GenerationTask.SubmissionState.SUBMITTED,
                "project-delete-locally-succeeded-token",
                GenerationTask.RemoteState.UNKNOWN,
            ),
            (
                "remotely-succeeded",
                GenerationTask.Status.FAILED,
                GenerationTask.SubmissionState.SUBMITTED,
                "project-delete-remotely-succeeded-token",
                GenerationTask.RemoteState.SUCCEEDED,
            ),
            (
                "failed",
                GenerationTask.Status.FAILED,
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                "project-delete-failed-token",
                GenerationTask.RemoteState.FAILED,
            ),
            (
                "cancelled",
                GenerationTask.Status.CANCELLED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
                "project-delete-cancelled-token",
                GenerationTask.RemoteState.CANCELLED,
            ),
        )
        for suffix, task_status, submission_state, token, remote_state in cases:
            with self.subTest(status=task_status):
                user, project, *_ = build_story(f"project-delete-terminal-{suffix}")
                artifact, workflow, task = self._create_generation_task(
                    user=user,
                    project=project,
                    suffix=suffix,
                    task_status=task_status,
                    submission_state=submission_state,
                    submission_token=token,
                    remote_state=remote_state,
                )
                self.client.force_authenticate(user)

                response = self.client.delete(f"/api/v1/projects/{project.id}/")

                self.assertEqual(response.status_code, 204, response.data)
                self.assertFalse(SeriesProject.objects.filter(pk=project.pk).exists())
                self.assertFalse(GenerationTask.objects.filter(pk=task.pk).exists())
                self.assertFalse(GeneratedWorkflow.objects.filter(pk=workflow.pk).exists())
                self.assertFalse(PromptArtifact.objects.filter(pk=artifact.pk).exists())

    def test_protected_release_rolls_back_terminal_task_cleanup(self):
        user, project, *_ = build_story("project-delete-protected-release")
        artifact, workflow, task = self._create_generation_task(
            user=user,
            project=project,
            suffix="protected-release",
            task_status=GenerationTask.Status.SUCCEEDED,
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
            submission_token="project-delete-protected-token",
            remote_state=GenerationTask.RemoteState.SUCCEEDED,
        )
        ReleaseBundle.objects.create(
            owner=user,
            project=project,
            created_by=user,
            version=1,
            title="受保护发行证据",
            selection={"shots": [], "episodes": [], "series_assembly_id": None},
        )
        self.client.force_authenticate(user)

        response = self.client.delete(f"/api/v1/projects/{project.id}/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "protected_resource")
        self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())
        self.assertTrue(GenerationTask.objects.filter(pk=task.pk).exists())
        self.assertTrue(GeneratedWorkflow.objects.filter(pk=workflow.pk).exists())
        self.assertTrue(PromptArtifact.objects.filter(pk=artifact.pk).exists())

    def test_nonterminal_remote_token_blocks_project_delete_after_local_failure_or_cancel(self):
        user, project, *_ = build_story("project-delete-unresolved-remote")
        artifact = PromptArtifact.objects.create(
            project=project,
            created_by=user,
            prompt_text="unresolved remote delete guard",
            content_hash="c" * 64,
        )
        template = WorkflowTemplate.objects.create(
            owner=user,
            name="Unresolved remote delete guard",
            slug="unresolved-remote-delete-guard",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            prompt_artifact=artifact,
            template=template,
            created_by=user,
            content_hash="d" * 64,
        )
        task = GenerationTask.objects.create(
            owner=user,
            project=project,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            submission_token="2f270fa3-9715-448e-a499-57ad82ae152a",
            comfy_prompt_id="2f270fa3-9715-448e-a499-57ad82ae152a",
            submission_state=GenerationTask.SubmissionState.SUBMITTED,
        )
        self.client.force_authenticate(user)

        for task_status in (
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        ):
            with self.subTest(status=task_status):
                task.status = task_status
                task.save(update_fields=("status", "updated_at"))
                response = self.client.delete(f"/api/v1/projects/{project.id}/")
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["error"]["code"], "project_has_active_work")
                unresolved_count = response.data["error"]["details"]["unresolved_remote_tasks"]
                self.assertIsInstance(unresolved_count, int)
                self.assertEqual(unresolved_count, 1)
                self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())

    def test_active_assembly_jobs_block_project_delete(self):
        user, project, *_ = build_story("project-delete-assembly")
        job = AssemblyJob.objects.create(
            owner=user,
            project=project,
            scope=AssemblyJob.Scope.SERIES,
        )
        self.client.force_authenticate(user)

        for job_status in (AssemblyJob.Status.QUEUED, AssemblyJob.Status.RUNNING):
            with self.subTest(status=job_status):
                job.status = job_status
                job.save(update_fields=("status", "updated_at"))
                response = self.client.delete(f"/api/v1/projects/{project.id}/")
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["error"]["code"], "project_has_active_work")
                active_count = response.data["error"]["details"]["active_assembly_jobs"]
                self.assertIsInstance(active_count, int)
                self.assertEqual(active_count, 1)
                self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())
