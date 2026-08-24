from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import AccessControlAuditEvent, UserSecurityProfile
from studio.services.access_control import (
    ROLE_MEMBER,
    ROLE_PLATFORM_ADMIN,
    create_managed_user,
)


User = get_user_model()


class AccessControlApiTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="root-admin",
            email="root@example.test",
            password="RootAdminPass123!",
        )
        UserSecurityProfile.objects.create(user=self.superuser)
        self.staff = User.objects.create_user(
            username="platform-admin",
            password="PlatformAdminPass123!",
            is_staff=True,
        )
        UserSecurityProfile.objects.create(user=self.staff)
        self.member = User.objects.create_user(
            username="member",
            password="MemberPass123!",
        )
        UserSecurityProfile.objects.create(user=self.member)

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_only_superuser_can_read_or_mutate_access_control(self):
        anonymous = APIClient().get("/api/v1/access/users/")
        member = self._client(self.member).get("/api/v1/access/users/")
        staff = self._client(self.staff).get("/api/v1/access/users/")
        allowed = self._client(self.superuser).get("/api/v1/access/users/")

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(member.status_code, 403)
        self.assertEqual(staff.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(allowed.data["count"], 3)

    def test_create_returns_temporary_password_once_without_persisting_plaintext(self):
        response = self._client(self.superuser).post("/api/v1/access/users/", {
            "username": "new-operator",
            "email": "operator@example.test",
            "role": ROLE_PLATFORM_ADMIN,
            "reason": "operator_onboarding",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        temporary_password = response.data["temporary_password"]
        created = User.objects.get(username="new-operator")
        self.assertTrue(created.check_password(temporary_password))
        self.assertTrue(created.security_profile.must_change_password)
        self.assertFalse(hasattr(created, "temporary_password"))
        event = AccessControlAuditEvent.objects.get(target_user=created)
        self.assertNotIn(temporary_password, str(event.__dict__))

        detail = self._client(self.superuser).get(
            f"/api/v1/access/users/{created.id}/",
        )
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("temporary_password", detail.data)

    def test_first_login_is_blocked_until_password_change_and_old_tokens_are_revoked(self):
        target, temporary_password = create_managed_user(
            actor=self.superuser,
            username="first-login",
            email="first@example.test",
            role=ROLE_MEMBER,
        )
        login = APIClient().post("/api/v1/auth/token/", {
            "username": target.username,
            "password": temporary_password,
        }, format="json")
        self.assertEqual(login.status_code, 200, login.data)
        access = login.data["access"]
        refresh = login.data["refresh"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        blocked = client.get("/api/v1/projects/")
        self.assertEqual(blocked.status_code, 403, blocked.data)
        self.assertEqual(blocked.data["error"]["code"], "password_change_required")
        self.assertEqual(client.get("/api/v1/auth/me/").status_code, 200)

        changed = client.post("/api/v1/auth/password/change/", {
            "current_password": temporary_password,
            "new_password": "PermanentPass456!",
            "confirm_password": "PermanentPass456!",
        }, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertFalse(changed.data["must_change_password"])

        self.assertEqual(client.get("/api/v1/auth/me/").status_code, 401)
        refreshed = APIClient().post("/api/v1/auth/token/refresh/", {
            "refresh": refresh,
        }, format="json")
        self.assertEqual(refreshed.status_code, 401, refreshed.data)
        relogin = APIClient().post("/api/v1/auth/token/", {
            "username": target.username,
            "password": "PermanentPass456!",
        }, format="json")
        self.assertEqual(relogin.status_code, 200, relogin.data)

    def test_last_active_superuser_and_self_cannot_be_disabled_or_downgraded(self):
        client = self._client(self.superuser)
        deactivate = client.post(
            f"/api/v1/access/users/{self.superuser.id}/status/",
            {"is_active": False, "reason": "test"},
            format="json",
        )
        downgrade = client.post(
            f"/api/v1/access/users/{self.superuser.id}/role/",
            {"role": ROLE_PLATFORM_ADMIN, "reason": "test"},
            format="json",
        )

        self.assertEqual(deactivate.status_code, 400, deactivate.data)
        self.assertEqual(downgrade.status_code, 400, downgrade.data)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)
        self.assertTrue(self.superuser.is_superuser)


class InitialSuperadminCommandTests(TestCase):
    def test_bootstrap_prints_one_temporary_password_and_is_idempotent(self):
        output = StringIO()
        call_command(
            "bootstrap_superadmin", username="initial-root", if_missing=True,
            stdout=output,
        )
        lines = output.getvalue().splitlines()
        password_line = next(line for line in lines if line.startswith("temporary_password="))
        password = password_line.split("=", 1)[1]
        user = User.objects.get(username="initial-root")
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.security_profile.must_change_password)
        self.assertTrue(user.check_password(password))
        event = AccessControlAuditEvent.objects.get(target_user=user)
        self.assertNotIn(password, str(event.__dict__))

        second = StringIO()
        call_command("bootstrap_superadmin", if_missing=True, stdout=second)
        self.assertEqual(second.getvalue().strip(), "superadmin=existing")
        self.assertNotIn("temporary_password", second.getvalue())
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
