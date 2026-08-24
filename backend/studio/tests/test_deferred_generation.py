from django.test import TestCase
from rest_framework.test import APIClient

from studio.tests.factories import build_story


class DeferredGenerationDiagnosticAPITests(TestCase):
    """Historical deferred chains and tasks expose diagnostics, never mutations."""

    def setUp(self):
        self.user, *_rest = build_story("deferred-read-only")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_deferred_collection_is_read_only(self):
        response = self.client.get("/api/v1/deferred-generation-requests/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 0)
        posted = self.client.post(
            "/api/v1/deferred-generation-requests/",
            {"idempotency_key": "public-write-must-not-exist"},
            format="json",
        )
        self.assertEqual(posted.status_code, 405, posted.data)

    def test_deferred_and_task_mutation_actions_are_unroutable(self):
        deferred_id = "11111111-1111-4111-8111-111111111111"
        task_id = "22222222-2222-4222-8222-222222222222"
        for path in (
            f"/api/v1/deferred-generation-requests/{deferred_id}/cancel/",
            f"/api/v1/tasks/{task_id}/cancel/",
            f"/api/v1/tasks/{task_id}/retry/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, {}, format="json").status_code, 404)
