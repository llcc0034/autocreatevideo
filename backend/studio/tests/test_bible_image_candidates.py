from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import GeneratedWorkflow, GenerationTask, PromptArtifact
from studio.tests.factories import build_story


class BibleImageCandidateReadOnlySurfaceTests(TestCase):
    def setUp(self):
        user, self.project, _, _, _, _, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(user)
        self.url = f"/api/v1/projects/{self.project.id}/bible-image-candidates/"

    def test_candidate_projection_is_read_only(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("candidates", response.data)
        before = (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
        )
        rejected = self.client.post(self.url, {}, format="json")
        self.assertEqual(rejected.status_code, 405, rejected.data)
        self.assertEqual(before, (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
        ))

    def test_low_level_workflow_and_task_creation_are_closed(self):
        self.assertEqual(
            self.client.post("/api/v1/workflows/generate/", {}, format="json").status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/v1/tasks/", {}, format="json").status_code,
            405,
        )
