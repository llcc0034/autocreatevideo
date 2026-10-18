import importlib
from io import StringIO

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.models.deletion import ProtectedError, PROTECT
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, PlatformLifecycleAuditEvent, SeriesProject, Style
from studio.tests.factories import build_story


class StyleManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="style-owner")
        self.other = get_user_model().objects.create_user(username="other-style-owner")
        self.staff = get_user_model().objects.create_user(username="style-admin", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.personal = Style.objects.create(owner=self.user, slug="own", name="我的风格", category_id="wuxia")
        self.shared = Style.objects.create(slug="shared", name="共享风格", is_system=True)
        self.hidden = Style.objects.create(owner=self.other, slug="hidden", name="私有风格")

    def test_list_permissions_counts_and_metadata(self):
        response = self.client.get("/api/v1/styles/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["id"]: row for row in response.data["results"]}
        self.assertEqual(set(rows), {str(self.personal.pk), str(self.shared.pk)})
        self.assertTrue(rows[str(self.personal.pk)]["can_manage"])
        self.assertFalse(rows[str(self.shared.pk)]["can_manage"])
        self.assertEqual(rows[str(self.shared.pk)]["scope"], "global")
        self.assertEqual(rows[str(self.personal.pk)]["project_count"], 0)
        catalog = self.client.get("/api/v1/styles/catalog/")
        self.assertEqual(catalog.status_code, 200, catalog.data)
        self.assertFalse(catalog.data["can_manage_categories"])
        self.assertEqual(rows[str(self.personal.pk)]["category_label"], "武侠")
        self.assertEqual([item["value"] for item in catalog.data["scopes"]], ["personal"])
        self.client.force_authenticate(self.staff)
        self.assertEqual(len(self.client.get("/api/v1/styles/catalog/").data["scopes"]), 2)

    def test_create_edit_delete_and_audit(self):
        response = self.client.post("/api/v1/styles/", {"name": "独有风格", "category": "anime", "scope": "personal", "description": "选择说明", "visual_prompt": "clean lines"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["owner"], self.user.pk)
        self.assertTrue(response.data["slug"])
        self.assertEqual(response.data["asset_count"], 0)
        pk, slug = response.data["id"], response.data["slug"]
        updated = self.client.patch(f"/api/v1/styles/{pk}/", {"name": "改名", "slug": "should-not-change", "visual_prompt": "new lines"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["slug"], slug)
        self.assertEqual(updated.data["visual_prompt"], "new lines")
        removed = self.client.delete(f"/api/v1/styles/{pk}/")
        self.assertEqual(removed.status_code, 204, removed.data)
        events = PlatformLifecycleAuditEvent.objects.filter(object_type="visual_style", object_id=pk)
        self.assertEqual(set(events.values_list("action", flat=True)), {"create", "update", "delete"})
        self.assertNotIn("new lines", str(list(events.values("before_summary", "after_summary"))))

    def test_scope_and_write_authorization(self):
        for method in (self.client.patch, self.client.delete):
            self.assertEqual(method(f"/api/v1/styles/{self.shared.pk}/", {}, format="json").status_code, 403)
            self.assertEqual(method(f"/api/v1/styles/{self.hidden.pk}/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/styles/", {"name": "unauthorized", "scope": "global"}, format="json").status_code, 400)
        self.client.force_authenticate(self.staff)
        response = self.client.post("/api/v1/styles/", {"name": "新共享风格", "scope": "global"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["owner"])
        changed = self.client.patch(f"/api/v1/styles/{self.shared.pk}/", {"scope": "personal"}, format="json")
        self.assertEqual(changed.status_code, 400)

    def test_filters_and_reference_privacy(self):
        own = Asset.objects.create(owner=self.user, style=self.shared, kind="prop", name="自己的素材")
        Asset.objects.create(owner=self.other, style=self.shared, kind="prop", name="不可见素材")
        rows = self.client.get("/api/v1/styles/", {"scope": "global", "search": "共享"}).data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_count"], 2)
        result = self.client.get(f"/api/v1/styles/{self.shared.pk}/references/", {"kind": "assets", "page_size": 1})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["results"], [{"id": str(own.pk), "name": "自己的素材", "kind": "assets"}])
        self.assertEqual(self.client.get(f"/api/v1/styles/{self.shared.pk}/references/").status_code, 400)

    def test_used_style_cannot_be_deleted_or_unlinked(self):
        asset = Asset.objects.create(owner=self.user, style=self.personal, kind="prop", name="引用")
        response = self.client.delete(f"/api/v1/styles/{self.personal.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("style_in_use", str(response.data))
        asset.refresh_from_db()
        self.assertEqual(asset.style_id, self.personal.pk)
        with self.assertRaises(ProtectedError):
            self.personal.delete()
        for model in (Asset, SeriesProject, AssetGenerationJob):
            self.assertIs(model._meta.get_field("style").remote_field.on_delete, PROTECT)

    def test_disable_preserves_existing_references_but_blocks_new_ones(self):
        asset = Asset.objects.create(owner=self.user, style=self.personal, kind="prop", name="保留", prompt_hint="a paper boat")
        response = self.client.patch(f"/api/v1/styles/{self.personal.pk}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        active = self.client.get("/api/v1/styles/", {"is_active": "true"}).data["results"]
        self.assertNotIn(str(self.personal.pk), {row["id"] for row in active})
        from studio.serializers import AssetSerializer
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().patch("/")
        force_authenticate(request, user=self.user)
        context = {"request": Request(request)}
        data = {"style": str(self.personal.pk), "name": "新关联", "kind": "prop"}
        self.assertFalse(AssetSerializer(data=data, context=context).is_valid())
        serializer = AssetSerializer(asset, data={"style": str(self.personal.pk), "description": "更新说明"}, partial=True, context=context)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_project_references_are_counted_and_paginated(self):
        user, project, *_ = build_story()
        project.style = self.personal
        project.owner = self.user
        project.save(update_fields=["style", "owner"])
        result = self.client.get(f"/api/v1/styles/{self.personal.pk}/references/", {"kind": "projects"})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["results"][0]["name"], project.title)
        self.assertEqual(self.client.get(f"/api/v1/styles/{self.personal.pk}/").data["project_count"], 1)

    def test_description_migration_only_replaces_exact_bootstrap_text(self):
        call_command("seed_data", stdout=StringIO())
        wuxia = Style.objects.get(owner=None, slug="wuxia")
        wuxia.description = "武侠商业短剧预设，可绑定本地人物、场景与道具参考图。"
        wuxia.save()
        Style.objects.filter(owner=None, slug="anime").update(description="人工说明")
        migration = importlib.import_module("studio.migrations.0174_visual_style_management")
        editor = connection.schema_editor()
        migration.replace_bootstrap_descriptions(apps, editor)
        migration.replace_bootstrap_descriptions(apps, editor)
        wuxia.refresh_from_db()
        self.assertIn("水墨", wuxia.description)
        self.assertEqual(Style.objects.get(owner=None, slug="anime").description, "人工说明")
        self.assertEqual(wuxia.visual_prompt, next(row[3] for row in importlib.import_module("studio.management.commands.seed_data").STYLES if row[0]=="wuxia"))
