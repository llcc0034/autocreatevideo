"""Entity CRUD owns metadata; setting text belongs to its exact revisions."""
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import BibleVariant, BibleVariantRevision, CharacterBible, PropBible, SceneBible, SeriesProject
from studio.tests.primary_setting_fixtures import create_primary_setting


class BibleEntityAuthoringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="entity-authoring")
        cls.other = get_user_model().objects.create_user(username="other-authoring")
        cls.project = SeriesProject.objects.create(owner=cls.actor, title="Isolated authoring",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    kinds = (("characters", CharacterBible), ("scenes", SceneBible), ("props", PropBible))

    def payload(self, section):
        return {"project": str(self.project.pk), "slug": f"manual-{section}", "name": "同名 / Name",
            "description": "", "is_locked": False,
            **({"stable_speaker_id": "S1"} if section == "characters" else {})}

    def create(self, section):
        response = self.client.post(f"/api/v1/{section}/", self.payload(section), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_name_only_entities_need_no_anchor_or_image_and_do_not_invent_settings(self):
        for section, model in self.kinds:
            row = self.create(section)
            self.assertNotIn("visual_identity", row)
            self.assertEqual(row["description"], "")
            self.assertIsNone(row["anchor_asset"])
            self.assertEqual(row["reference_assets"], [])
            self.assertNotIn("visual_identity", {field.name for field in model._meta.fields})
        self.assertFalse(BibleVariant.objects.exists())
        self.assertFalse(BibleVariantRevision.objects.exists())

    def test_retired_field_is_explicitly_rejected_on_create_patch_and_put_even_if_blank(self):
        for section, model in self.kinds:
            for value in ("duplicate", "", None):
                response = self.client.post(f"/api/v1/{section}/",
                    {**self.payload(section), "visual_identity": value}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("主图/变体设定", str(response.data))
                self.assertFalse(model.objects.exists())
            row = self.create(section)
            for method in (self.client.patch, self.client.put):
                response = method(f"/api/v1/{section}/{row['id']}/",
                    {**self.payload(section), "name": "Must not save", "visual_identity": ""}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(model.objects.get(pk=row["id"]).name, row["name"])
        self.assertFalse(BibleVariant.objects.exists())

    def test_non_object_input_keeps_validation_error_instead_of_server_error(self):
        for value in (123, "text", ["visual_identity"], [1]):
            response = self.client.post("/api/v1/characters/", value, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(CharacterBible.objects.exists())

    def test_metadata_edits_leave_original_primary_revision_untouched(self):
        for section, model in self.kinds:
            row = self.create(section)
            entity = model.objects.get(pk=row["id"])
            primary = create_primary_setting(entity, "唯一共同身份", "唯一外观")
            original = deepcopy(list(primary.revisions.values()))
            response = self.client.patch(f"/api/v1/{section}/{row['id']}/",
                {"name": "改名", "description": "单独编辑说明"}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("visual_identity", response.data)
            entity.refresh_from_db()
            self.assertEqual((entity.name, entity.description), ("改名", "单独编辑说明"))
            self.assertEqual(list(primary.revisions.values()), original)

    def test_public_list_detail_and_search_do_not_read_or_match_retired_text(self):
        for section, model in self.kinds:
            row = self.create(section)
            for suffix in (f"?project={self.project.pk}", f"{row['id']}/", "?search=only-old-secret-anchor"):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(f"/api/v1/{section}/{suffix}")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertNotIn("visual_identity", str(response.data))
                self.assertNotIn("visual_identity", "\n".join(q["sql"] for q in queries))
                if suffix.startswith("?search"):
                    self.assertEqual(response.data["results"], [])
            if connection.vendor == "sqlite":
                plan = model.objects.filter(project=self.project).explain()
                self.assertIn("SEARCH", plan)
                self.assertNotIn("SCAN", plan)

    def test_ownership_and_locked_entity_guards_remain_in_effect(self):
        row = self.create("characters")
        url = f"/api/v1/characters/{row['id']}/"
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.patch(url, {"name": "other"}).status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post("/api/v1/characters/", self.payload("characters")).status_code, (401, 403))
        self.client.force_authenticate(self.actor)
        self.assertEqual(self.client.patch(url, {"is_locked": True}).status_code, 200)
        response = self.client.patch(url, {"name": "locked change"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("is_locked", response.data["error"]["details"])
        self.assertFalse(BibleVariant.objects.exists())
