from django.test import TestCase, override_settings
from rest_framework.test import APIClient


class RegistrationSecurityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {
            "username": "new-user", "email": "new@example.test",
            "first_name": "新", "last_name": "用户", "password": "StrongPass123!",
        }

    def test_public_registration_is_disabled_by_default(self):
        response = self.client.post("/api/v1/auth/register/", self.payload, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("公开注册已关闭", str(response.data))

    @override_settings(ALLOW_PUBLIC_REGISTRATION=True)
    def test_operator_can_explicitly_enable_public_registration(self):
        response = self.client.post("/api/v1/auth/register/", self.payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["first_name"], "新")
