from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ConfigRevision
from studio.services.configuration import create_config_revision, publish_config_revision


class RegistrationSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.payload = {
            "username": "new-user", "email": "new@example.test",
            "first_name": "新", "last_name": "用户", "password": "StrongPass123!",
        }

    def test_public_registration_is_disabled_by_default(self):
        response = self.client.post("/api/v1/auth/register/", self.payload, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("公开注册已关闭", str(response.data))

    def test_operator_can_enable_registration_with_database_rate_limit(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "access.public_registration_enabled": True,
                "access.throttle_anon_rate": "1000/hour",
                "access.throttle_user_rate": "1000/hour",
                "access.throttle_registration_rate": "1/hour",
            },
            created_by=None,
            clone_published=True,
        )
        publish_config_revision(revision, published_by=None)

        response = self.client.post("/api/v1/auth/register/", self.payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["first_name"], "新")

        second_payload = {**self.payload, "username": "new-user-2", "email": "new2@example.test"}
        throttled = self.client.post(
            "/api/v1/auth/register/", second_payload, format="json",
        )
        self.assertEqual(throttled.status_code, 429, throttled.data)
