from django.test import TestCase
from rest_framework.test import APIClient

from studio.tests.factories import build_story


class RetiredEpisodeBatchGenerationSurfaceTests(TestCase):
    def setUp(self):
        user, self.project, _, _, _, _, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_build_production_and_batch_mutations_are_not_public_routes(self):
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.project.id}/build-production/",
                {},
                format="json",
            ).status_code,
            404,
        )
        for action in ("resume-batch", "cancel-batch", "delete-batch"):
            with self.subTest(action=action):
                response = self.client.post(
                    f"/api/v1/tasks/00000000-0000-0000-0000-000000000000/{action}/",
                    {},
                    format="json",
                )
                self.assertEqual(response.status_code, 404)
