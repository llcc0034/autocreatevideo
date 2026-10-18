from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    AssemblyJob,
    ExecutionPreparation,
    GenerationAttempt,
    GenerationTask,
    ProductionRun,
    ReleaseBundle,
    SeriesProject,
)
from studio.tests import test_execution_production_workflow_requests as execution_fixtures
from studio.tests.factories import build_story
from studio.services.configuration import project_creation_policy
from studio.services.security_boundaries import boundary_int


class SeriesProjectManagementAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_create_from_page_resolves_server_owned_runtime_defaults(self):
        owner = get_user_model().objects.create_user(username="creation-policy-owner")
        self.client.force_authenticate(owner)
        policy = project_creation_policy()
        payload = {
            "title": "独立创建验收",
            "language": policy["default_language"],
            "aspect_ratio": policy["default_aspect_ratio"],
            "width": policy["width"],
            "height": policy["height"],
        }
        response = self.client.post("/api/v1/projects/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        project = SeriesProject.objects.get(pk=response.data["id"])
        self.assertEqual(project.owner, owner)
        for field in ("fps", "default_shot_duration_ms"):
            self.assertEqual(getattr(project, field), policy[field])
        seed = project.continuity_seed
        self.assertGreater(seed, 0)
        if policy["continuity_seed_policy"]["mode"] == "explicit":
            self.assertEqual(seed, policy["continuity_seed_policy"]["value"])
        changed = self.client.patch(
            f"/api/v1/projects/{project.id}/", {"logline": "只改说明"}, format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        project.refresh_from_db()
        self.assertEqual(project.fps, policy["fps"])
        self.assertEqual(project.continuity_seed, seed)

    def test_owned_source_catalog_does_not_confuse_staff_visibility_with_execution_scope(self):
        staff, own, *_ = build_story("source-catalog-staff")
        other, foreign, *_ = build_story("source-catalog-other")
        staff.is_staff = True
        staff.save(update_fields=("is_staff",))
        self.client.force_authenticate(staff)
        for query, expected in (({}, {str(own.id), str(foreign.id)}),
                                ({"owned_only": "false"}, {str(own.id), str(foreign.id)}),
                                ({"owned_only": "true"}, {str(own.id)})):
            response = self.client.get("/api/v1/projects/", query)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual({row["id"] for row in response.data["results"]}, expected)
        self.assertEqual(self.client.get("/api/v1/projects/", {"owned_only": "other"}).status_code, 400)
        self.client.force_authenticate(other)
        for value in ("true", "false"):
            response = self.client.get("/api/v1/projects/", {"owned_only": value})
            self.assertEqual({row["id"] for row in response.data["results"]}, {str(foreign.id)})

    def test_create_rejects_explicit_invalid_runtime_values(self):
        owner = get_user_model().objects.create_user(username="creation-invalid-owner")
        self.client.force_authenticate(owner)
        policy = project_creation_policy()
        payload = {
            "title": "无效创建",
            "language": policy["default_language"],
            "aspect_ratio": policy["default_aspect_ratio"],
            "width": policy["width"],
            "height": policy["height"],
        }
        for field, value in (("fps", 0), ("fps", boundary_int("security.media.max_fps") + 1), ("continuity_seed", "invalid")):
            with self.subTest(field=field, value=value):
                response = self.client.post("/api/v1/projects/", {**payload, field: value}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, str(response.data))
        self.assertFalse(SeriesProject.objects.filter(owner=owner).exists())

    def test_create_resolves_missing_dimensions_from_selected_aspect(self):
        owner = get_user_model().objects.create_user(username="creation-aspect-owner")
        self.client.force_authenticate(owner)
        policy = project_creation_policy()
        for option in policy["aspect_ratio_options"]:
            response = self.client.post("/api/v1/projects/", {
                "title": "画幅默认值验收", "aspect_ratio": option["value"],
            }, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["width"], option["width"])
            self.assertEqual(response.data["height"], option["height"])

    def test_owner_can_favorite_project_but_other_tenant_cannot_patch_it(self):
        owner = get_user_model().objects.create_user(
            username="favorite-owner", password="StrongPass123!",
        )
        other = get_user_model().objects.create_user(
            username="favorite-other", password="StrongPass123!",
        )
        project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=owner, title="待收藏连续剧", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)

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
        project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=owner, title="不可跨租户删除", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        self.client.force_authenticate(other)

        response = self.client.delete(f"/api/v1/projects/{project.id}/")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(SeriesProject.objects.filter(pk=project.pk).exists())

    def test_active_assembly_jobs_block_project_delete(self):
        user, project, *_ = build_story("project-delete-assembly")
        job = AssemblyJob.objects.create(
            owner=user,
            project=project,
            scope=AssemblyJob.Scope.SERIES,
            max_attempts=3,
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



@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class SeriesProjectExecutionDeleteTests(TestCase):
    _create_version = execution_fixtures.ProductionWorkflowRequestTests._create_version
    resolve = execution_fixtures.ProductionWorkflowRequestTests.resolve

    def setUp(self):
        execution_fixtures.ProductionWorkflowRequestTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.task = self.attempt.generation_task

    def assert_execution_retained(self):
        for model, row in ((SeriesProject, self.project), (ProductionRun, self.run),
                (ExecutionPreparation, self.preparation), (GenerationAttempt, self.attempt),
                (GenerationTask, self.task)):
            self.assertTrue(model.objects.filter(pk=row.pk).exists(), model.__name__)

    def test_active_generation_tasks_block_project_delete(self):
        for state in ("queued", "running", "cancel_requested"):
            GenerationTask.objects.filter(pk=self.task.pk).update(status=state)
            response = self.client.delete(f"/api/v1/projects/{self.project.pk}/")
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["error"]["code"], "project_has_active_work")
            self.assertEqual(response.data["error"]["details"]["active_generation_tasks"], 1)
            self.assert_execution_retained()

    def test_terminal_execution_still_has_a_formal_run_not_an_orphan_workflow(self):
        # The real queue is allocated from a Run/Preparation/Attempt. The
        # ordinary project delete endpoint does not purge that closure; P5's
        # explicitly reviewed incompatible-data cleanup is a different action.
        for state, submission, remote in (
            ("succeeded", "submitted", "unknown"), ("failed", "submitted", "succeeded"),
            ("failed", "terminal_failed", "failed"), ("cancelled", "terminal_cancelled", "cancelled"),
        ):
            GenerationTask.objects.filter(pk=self.task.pk).update(status=state,
                submission_state=submission, submission_token="recorded-submit", remote_state=remote)
            GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status=state)
            ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
            response = self.client.delete(f"/api/v1/projects/{self.project.pk}/")
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["error"]["code"], "protected_resource")
            self.assert_execution_retained()

    def test_protected_run_release_preserves_all_current_execution_members(self):
        GenerationTask.objects.filter(pk=self.task.pk).update(status="succeeded",
            submission_state="terminal_succeeded", remote_state="succeeded")
        bundle = ReleaseBundle.objects.create(owner=self.user, project=self.project, production_run=self.run,
            created_by=self.user, version=1, title="Run delivery",
            selection={"shots": [], "episodes": [], "series_artifact_id": None})
        response = self.client.delete(f"/api/v1/projects/{self.project.pk}/")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "protected_resource")
        self.assert_execution_retained()
        self.assertTrue(ReleaseBundle.objects.filter(pk=bundle.pk).exists())

    def test_remote_token_blocks_delete_even_after_local_failure_or_cancel(self):
        for state in ("failed", "cancelled"):
            GenerationTask.objects.filter(pk=self.task.pk).update(status=state,
                submission_token="recorded-submit", submission_state="submitted", remote_state="missing")
            response = self.client.delete(f"/api/v1/projects/{self.project.pk}/")
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["error"]["code"], "project_has_active_work")
            self.assertEqual(response.data["error"]["details"]["unresolved_remote_tasks"], 1)
            self.assert_execution_retained()
