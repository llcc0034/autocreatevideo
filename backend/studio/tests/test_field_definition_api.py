from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from studio.models import FieldDefinition, FieldDefinitionVersion, PromptProfileVersion
from studio.services.field_definitions import field_definition_snapshot


ROOT = "/api/v1/field-definition-versions/"


class FieldIdentityApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="identity-editor", is_staff=True))
        self.root = "/api/v1/field-definitions/"

    def test_new_route_keeps_identity_and_old_route_is_removed(self):
        identity = FieldDefinition.objects.create(key="shared_existing_identity")
        version = FieldDefinitionVersion.objects.create(
            field_definition=identity, version=1, label="保留定义", value_type="text",
        )
        response = self.client.get(f"{self.root}{identity.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(str(response.data["id"]), str(identity.pk))
        self.assertEqual(self.client.get("/api/v1/prompt-field-definitions/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/prompt-field-definitions/{identity.pk}/").status_code, 404)
        self.assertEqual(self.client.get(self.root + "options/").status_code, 200)
        response = self.client.get(self.root, {"search": "shared_existing", "is_active": True, "page_size": 20})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(str(response.data["results"][0]["id"]), str(identity.pk))
        version.refresh_from_db()
        self.assertEqual(version.field_definition_id, identity.pk)
        self.assertEqual(version.label, "保留定义")

    def test_identity_never_accepts_definition_content_or_changes_existing_versions(self):
        row = self.client.post(self.root, {"key": "identity_only"}, format="json")
        self.assertEqual(row.status_code, 201, row.data)
        self.assertEqual(set(row.data), {"id", "key", "is_active", "display_order", "created_at", "updated_at"})
        version = self.client.post(ROOT, {"field_definition": row.data['id'], "label": "冻结标签", "category": "custom", "value_type": "text"}, format="json")
        self.assertEqual(version.status_code, 201, version.data)
        self.assertEqual(self.client.post(f"{ROOT}{version.data['id']}/publish/", {}, format="json").status_code, 200)
        url = f"{self.root}{row.data['id']}/"
        for name in ("label", "category", "description", "impact", "value_type", "value_options", "default_value", "is_required"):
            with self.subTest(name=name):
                response = self.client.patch(url, {name: "unsupported"}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.patch(url, {"key": "renamed_identity"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"is_active": False}, format="json").status_code, 200)
        frozen = self.client.get(f"{ROOT}{version.data['id']}/")
        self.assertEqual((frozen.data['label'], frozen.data['status']), ("冻结标签", "published"))


class FieldDefinitionVersionApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(username="field-version-staff", is_staff=True)
        cls.reader = get_user_model().objects.create_user(username="field-version-reader")
        cls.identity = FieldDefinition.objects.create(
            key="versioned_motion",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def create(self, **values):
        response = self.client.post(ROOT, {
            "field_definition": str(self.identity.pk), "label": "镜头动作",
            "category": "custom", "value_type": "long_text", **values,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def action(self, version, name, body=None):
        return self.client.post(f"{ROOT}{version['id']}/{name}/", body or {}, format="json")

    def test_draft_publish_clone_and_retire_keep_exact_version_meaning(self):
        first = self.create(description="原始含义")
        self.assertEqual((first["version"], first["status"], first["field_key"]), (1, "draft", self.identity.key))
        response = self.client.patch(f"{ROOT}{first['id']}/", {"label": "运动描述"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.action(first, "publish").status_code, 200)
        self.assertEqual(self.action(first, "publish").status_code, 200)
        frozen = deepcopy(field_definition_snapshot(FieldDefinitionVersion.objects.get(pk=first["id"])))
        clone = self.client.post(ROOT, {
            "field_definition": str(self.identity.pk), "source_version": first["id"],
            "label": "运动次数", "value_type": "integer", "unit": "次",
        }, format="json")
        self.assertEqual(clone.status_code, 201, clone.data)
        second = clone.data
        self.assertEqual((second["version"], second["description"], second["status"]), (2, "原始含义", "draft"))
        self.assertEqual(self.action(second, "publish").status_code, 200)
        self.assertEqual(self.action(first, "retire").status_code, 200)
        old = FieldDefinitionVersion.objects.get(pk=first["id"])
        self.assertEqual(field_definition_snapshot(old), frozen)
        response = self.client.get(f"{ROOT}{first['id']}/")
        self.assertEqual(response.data["label"], "运动描述")
        self.assertEqual(response.data["value_type"], "long_text")
        self.assertEqual(response.data["status"], "retired")

    def test_invalid_publish_and_immutable_edits_are_clear_without_partial_write(self):
        draft = self.create(label="", value_type="")
        response = self.action(draft, "publish")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("label", response.data["error"]["details"])
        self.assertEqual(FieldDefinitionVersion.objects.get(pk=draft["id"]).status, "draft")
        self.assertEqual(self.action(draft, "retire").status_code, 400)
        first = self.create()
        self.assertEqual(self.action(first, "publish", {"label": "不应混入状态操作"}).status_code, 400)
        self.assertEqual(self.action(first, "publish").status_code, 200)
        self.assertEqual(self.client.patch(f"{ROOT}{first['id']}/", {"label": "覆盖"}, format="json").status_code, 400)
        self.assertEqual(self.action(first, "retire").status_code, 200)
        self.assertEqual(self.action(first, "publish").status_code, 400)
        self.assertEqual(self.client.delete(f"{ROOT}{first['id']}/").status_code, 405)

    def test_version_allocation_and_all_types_come_from_the_version_model(self):
        options = self.client.get(ROOT + "options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual([item["value"] for item in options.data["value_types"]], FieldDefinitionVersion.ValueType.values)
        for index, kind in enumerate(FieldDefinitionVersion.ValueType.values, start=1):
            with self.subTest(kind=kind):
                row = self.create(value_type=kind, value_options=["中文", "English"] if kind == "enum" else [],
                    version=500, status="published")
                self.assertEqual((row["version"], row["status"]), (index, "draft"))
                self.assertEqual(self.action(row, "publish").status_code, 200)

    def test_usage_values_cross_identity_and_invalid_content_are_rejected(self):
        first = self.create()
        other = FieldDefinition.objects.create(key="another_identity")
        attempts = (
            {"default_value": False}, {"is_required": True}, {"actual_value": "test"},
            {"value_type": "number"}, {"value_type": "enum", "value_options": ["same", "same"]},
            {"value_type": "boolean", "value_options": ["not enum"]},
            {"source_version": first["id"], "field_definition": str(other.pk)},
        )
        count = FieldDefinitionVersion.objects.count()
        for extra in attempts:
            with self.subTest(extra=extra):
                response = self.client.post(ROOT, {"field_definition": str(self.identity.pk), **extra}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(FieldDefinitionVersion.objects.count(), count)
        response = self.client.patch(f"{ROOT}{first['id']}/", {"field_definition": str(other.pk)}, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_list_is_filtered_paginated_and_does_not_fetch_definition_bodies(self):
        for number in range(4):
            self.create(label=f"镜头动作{number}", description="BODY_SENTINEL", impact="BODY_SENTINEL")
        with patch("studio.pagination.boundary_int", return_value=200), \
                patch("studio.pagination._api_default_page_size", return_value=2), \
                CaptureQueriesContext(connection) as queries:
            response = self.client.get(ROOT, {"field_definition": str(self.identity.pk), "status": "draft", "search": "镜头"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual([row["version"] for row in response.data["results"]], [4, 3])
        self.assertIsNotNone(response.data["next"])
        self.assertNotIn("BODY_SENTINEL", str(response.data))
        for row in response.data["results"]:
            self.assertFalse({"description", "impact", "value_options"} & set(row))
        field_queries = [row["sql"] for row in queries if 'FROM "studio_fielddefinition' in row["sql"]]
        self.assertEqual(len(field_queries), 2, field_queries)
        self.assertNotIn('"description"', " ".join(field_queries))
        self.assertNotIn('"impact"', " ".join(field_queries))
        plan = FieldDefinitionVersion.objects.filter(field_definition=self.identity).order_by("-version").explain()
        self.assertIn("INDEX", plan.upper())

    def test_authoring_does_not_update_identity_profile_or_create_business_work(self):
        profile = PromptProfileVersion.objects.create(
            profile_key="unchanged_profile", version=1, name="未切换消费者", model_family="test", operation="video.generate",
            status="published", contract_sha256="1" * 64,
        )
        before = PromptProfileVersion.objects.values().get(pk=profile.pk)
        with CaptureQueriesContext(connection) as queries:
            row = self.create(label="定义版本新名称")
            self.assertEqual(self.action(row, "publish").status_code, 200)
        writes = [query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertTrue(writes)
        self.assertTrue(all('"studio_fielddefinitionversion"' in sql for sql in writes), writes)
        self.identity.refresh_from_db()
        self.assertEqual(self.identity.key, "versioned_motion")
        self.assertEqual(PromptProfileVersion.objects.values().get(pk=profile.pk), before)

    def test_disabled_identity_and_permissions_do_not_silently_enable_versions(self):
        row = self.create()
        self.identity.is_active = False
        self.identity.save()
        self.assertEqual(self.action(row, "publish").status_code, 400)
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(ROOT + "options/").status_code, 403)
        self.assertEqual(self.client.get(f"{ROOT}{row['id']}/").status_code, 403)
        self.assertEqual(self.action(row, "publish").status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(ROOT).status_code, (401, 403))
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(ROOT + "00000000-0000-0000-0000-000000000000/").status_code, 404)


class FieldDefinitionVersionSchemaTests(SimpleTestCase):
    def test_openapi_has_one_version_resource_and_distinct_list_detail_contracts(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths, components = schema["paths"], schema["components"]["schemas"]
        self.assertEqual(set(paths[ROOT]), {"get", "post"})
        self.assertEqual(set(paths[ROOT + "{id}/"]), {"get", "put", "patch"})
        for name in ("publish", "retire"):
            self.assertEqual(set(paths[ROOT + "{id}/" + name + "/"]), {"post"})
            self.assertNotIn("requestBody", paths[ROOT + "{id}/" + name + "/"]["post"])
        self.assertEqual(set(paths[ROOT + "options/"]), {"get"})
        self.assertFalse({"description", "impact", "value_options"} & set(components["FieldDefinitionVersionSummary"]["properties"]))
        self.assertIn("source_version", components["FieldDefinitionVersionRequest"]["properties"])
        self.assertTrue({"description", "impact", "unit", "value_options"} <= set(components["FieldDefinitionVersion"]["properties"]))
