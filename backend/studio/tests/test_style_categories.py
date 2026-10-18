from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import PROTECT, ProtectedError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import PlatformLifecycleAuditEvent, Style, StyleCategory


class StyleCategoryTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="category-admin", is_staff=True)
        self.user = get_user_model().objects.create_user(username="category-reader")
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def create_category(self, name="校园青春"):
        response = self.client.post("/api/v1/style-categories/", {"name": name, "description": "学校生活题材", "sort_order": 3}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_crud_has_stable_key_and_lifecycle_audit(self):
        row = self.create_category()
        url = f'/api/v1/style-categories/{row["id"]}/'
        changed = self.client.patch(url, {"name": "青春校园", "key": "ignored", "sort_order": 9}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(changed.data["key"], row["key"])
        self.assertEqual(changed.data["style_count"], 0)
        self.assertEqual(self.client.delete(url).status_code, 204)
        events = PlatformLifecycleAuditEvent.objects.filter(object_type="style_category", object_id=row["id"])
        self.assertEqual(set(events.values_list("action", flat=True)), {"create", "update", "delete"})
        self.assertNotIn("青春", str(list(events.values("before_summary", "after_summary"))))

    def test_only_staff_can_write_and_users_can_read(self):
        row = self.create_category()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/style-categories/").status_code, 200)
        self.assertFalse(self.client.get(f'/api/v1/style-categories/{row["id"]}/').data["can_manage"])
        self.assertEqual(self.client.post("/api/v1/style-categories/", {"name": "unauthorized"}).status_code, 403)
        for method in (self.client.patch, self.client.delete):
            self.assertEqual(method(f'/api/v1/style-categories/{row["id"]}/', {}, format="json").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/style-categories/").status_code, 401)

    def test_dynamic_choice_rename_and_reference_protection(self):
        row = self.create_category()
        response = self.client.post("/api/v1/styles/", {"name": "新风格", "category": row["key"]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        style = Style.objects.get(pk=response.data["id"])
        url = f'/api/v1/style-categories/{row["id"]}/'
        self.assertEqual(self.client.get(url).data["style_count"], 1)
        self.assertEqual(self.client.get("/api/v1/styles/", {"category": row["key"]}).data["count"], 1)
        self.assertEqual(self.client.patch(url, {"name": "校园日常"}, format="json").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/styles/{style.pk}/").data["category_label"], "校园日常")
        self.assertEqual(self.client.delete(url).status_code, 400)
        with self.assertRaises(ProtectedError):
            style.category.delete()
        self.assertIs(Style._meta.get_field("category").remote_field.on_delete, PROTECT)
        self.client.patch(f"/api/v1/styles/{style.pk}/", {"category": None}, format="json")
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertTrue(Style.objects.filter(pk=style.pk, category__isnull=True).exists())

    def test_inactive_category_preserves_old_but_rejects_new_association(self):
        row = self.create_category()
        style = Style.objects.create(owner=self.user, slug="existing", name="已有关联", category_id=row["key"])
        self.client.patch(f'/api/v1/style-categories/{row["id"]}/', {"is_active": False}, format="json")
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/v1/styles/", {"name": "新关联", "category": row["key"]}, format="json").status_code, 400)
        response = self.client.patch(f"/api/v1/styles/{style.pk}/", {"category": row["key"], "name": "保留分类"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["category_active"])
        self.assertEqual(self.client.post("/api/v1/styles/", {"name": "未分类"}, format="json").data["category"], None)
        active = self.client.get("/api/v1/style-categories/", {"is_active": "true", "search": "校园"})
        self.assertEqual(active.data["count"], 0)

    def test_validation_and_paginated_order(self):
        self.create_category()
        for payload in ({"name": "校园青春"}, {"name": " "}, {"name": "invalid", "sort_order": -1}, {"name": "invalid", "description": "x" * 501}):
            self.assertEqual(self.client.post("/api/v1/style-categories/", payload, format="json").status_code, 400)
        response = self.client.get("/api/v1/style-categories/", {"page_size": 1, "ordering": "sort_order"})
        self.assertTrue(response.data["next"])
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(self.client.post("/api/v1/styles/", {"name": "bad", "category": "not-registered"}, format="json").status_code, 400)

    def test_seed_never_restores_removed_or_disabled_categories(self):
        StyleCategory.objects.filter(key="wuxia").delete()
        with self.assertRaises(CommandError):
            call_command("seed_data", stdout=StringIO())
        self.assertFalse(StyleCategory.objects.filter(key="wuxia").exists())
        StyleCategory.objects.create(key="wuxia", name="改名武侠", is_active=False)
        with self.assertRaises(CommandError):
            call_command("seed_data", stdout=StringIO())
        self.assertFalse(StyleCategory.objects.get(key="wuxia").is_active)
