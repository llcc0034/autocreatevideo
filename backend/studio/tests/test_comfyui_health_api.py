from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.services.comfyui import ComfyUIError


class ComfyUIHealthAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="health-user",
            password="StrongPass123!",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("studio.views.ComfyUIClient")
    def test_healthy_service_preserves_existing_fields(self, client_factory):
        client_factory.return_value.health.return_value = {"devices": [{"name": "GPU"}]}

        response = self.client.get("/api/v1/comfyui/health/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["reachable"])
        self.assertFalse(response.data["degraded"])
        self.assertEqual(response.data["system_stats"]["devices"][0]["name"], "GPU")

    @patch("studio.views.ComfyUIClient")
    def test_system_stats_failure_with_working_queue_is_degraded_but_reachable(self, client_factory):
        client = client_factory.return_value
        client.health.side_effect = ComfyUIError("/system_stats returned 500: CUDA out of memory")
        client.queue.return_value = {
            "queue_running": [[1, "prompt-1"]],
            "queue_pending": [[2, "prompt-2"], [3, "prompt-3"]],
        }

        response = self.client.get("/api/v1/comfyui/health/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["reachable"])
        self.assertTrue(response.data["degraded"])
        self.assertIsNone(response.data["system_stats"])
        self.assertIn("CUDA out of memory", response.data["error"])
        self.assertEqual(response.data["queue_stats"], {"running": 1, "pending": 2})

    @patch("studio.views.ComfyUIClient")
    def test_system_stats_and_queue_failure_is_unreachable(self, client_factory):
        client = client_factory.return_value
        client.health.side_effect = ComfyUIError("system stats connection failed")
        client.queue.side_effect = ComfyUIError("queue connection failed")

        response = self.client.get("/api/v1/comfyui/health/")

        self.assertEqual(response.status_code, 503, response.data)
        self.assertFalse(response.data["reachable"])
        self.assertFalse(response.data["degraded"])
        self.assertIsNone(response.data["system_stats"])
        self.assertIn("system stats connection failed", response.data["error"])
        self.assertIn("queue connection failed", response.data["queue_error"])
