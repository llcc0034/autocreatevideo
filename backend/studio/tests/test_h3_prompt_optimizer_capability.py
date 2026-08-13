from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase


User = get_user_model()


class H3PromptOptimizerCapabilityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="optimizer-viewer", password="strong-pass-123")
        self.url = reverse("h3-prompt-optimizer-capability")

    def test_capability_requires_authentication(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(H3_PROMPT_OPTIMIZER={
        "enabled": True,
        "base_url": "https://optimizer.example.invalid/v1",
        "api_key": "never-return-this-secret",
        "model": "MiniMax-M2.7",
        "allow_non_loopback": True,
        "timeout_seconds": 60,
        "failure_mode": "block",
    }, H3_PROMPT_LANGUAGE_GATE="block")
    def test_remote_snapshot_exposes_only_safe_configuration_state(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {
            "enabled", "configured", "transport", "model", "failure_mode", "language_gate",
        })
        self.assertEqual(response.data, {
            "enabled": True,
            "configured": True,
            "transport": "remote",
            "model": "MiniMax-M2.7",
            "failure_mode": "block",
            "language_gate": "block",
        })
        rendered = response.content.decode("utf-8")
        self.assertNotIn("never-return-this-secret", rendered)
        self.assertNotIn("optimizer.example.invalid", rendered)
        self.assertNotIn("base_url", rendered)
        self.assertNotIn("api_key", rendered)

    @override_settings(H3_PROMPT_OPTIMIZER={
        "enabled": False,
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "model": "local-director-model",
        "allow_non_loopback": False,
        "timeout_seconds": 60,
        "failure_mode": "warn",
    }, H3_PROMPT_LANGUAGE_GATE="warn")
    def test_local_configuration_can_be_ready_while_disabled(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["enabled"])
        self.assertTrue(response.data["configured"])
        self.assertEqual(response.data["transport"], "local")
        self.assertEqual(response.data["language_gate"], "warn")

    @override_settings(H3_PROMPT_OPTIMIZER={
        "enabled": True,
        "base_url": "http://remote.example.invalid/v1",
        "api_key": "present-but-unsafe-transport",
        "model": "remote-model",
        "allow_non_loopback": True,
        "timeout_seconds": 60,
        "failure_mode": "unexpected-value",
    })
    def test_remote_http_is_not_configured_and_failure_mode_is_normalized(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["enabled"])
        self.assertFalse(response.data["configured"])
        self.assertEqual(response.data["transport"], "remote")
        self.assertEqual(response.data["failure_mode"], "warn")
