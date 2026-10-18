from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import SeriesProject, Shot
from studio.tests.factories import build_story


class ProjectInputModeTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, self.episode, self.shot, _ = build_story(username="input-mode")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_project_and_capability_work_without_any_video_execution_modes(self):
        with patch("studio.capability_views.configured_video_conditioning_modes", side_effect=AssertionError("configuration must not scan video")):
            capability = self.client.get("/api/v1/capabilities/production/")
            self.assertEqual(capability.status_code, 200, capability.data)
            self.assertNotIn("conditioning_modes", capability.data)
            self.assertNotIn("default_conditioning_mode", capability.data)
            created = self.client.post("/api/v1/projects/", {"title": "未选择模型的新项目"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertNotIn("default_conditioning_mode", created.data["production_settings"])

    def test_obsolete_project_default_cannot_be_written_again(self):
        for mode in ("ExampleMode", "", None):
            response = self.client.patch(f"/api/v1/projects/{self.project.pk}/", {
                "production_settings": {"default_conditioning_mode": mode},
            }, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn("production_settings", response.data["error"]["details"])

    def test_new_video_shot_does_not_inherit_or_require_a_project_mode(self):
        self.project.production_settings = {"default_conditioning_mode": "MustNotInherit"}
        self.project.save(update_fields=("production_settings",))
        response = self.client.post("/api/v1/shots/", {
            "episode": str(self.episode.pk), "number": 3, "title": "尚未选择执行实例",
            "media_type": "video", "duration_ms": 6000,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Shot.objects.get(pk=response.data["id"]).conditioning_mode, "")
        updated = self.client.patch(f"/api/v1/shots/{response.data['id']}/", {
            "conditioning_mode": "",
        }, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["conditioning_mode"], "")

    def test_cleanup_only_removes_project_preference_and_is_idempotent(self):
        retained = {"translation_glossary": {"纸船": "Paper Boat"}, "negative_prompt": "watermark"}
        self.project.production_settings = {**deepcopy(retained), "default_conditioning_mode": "OldMode"}
        self.project.save(update_fields=("production_settings",))
        before_mode = self.shot.conditioning_mode
        module = import_module("studio.migrations.0172_remove_project_default_conditioning_mode")
        editor = SimpleNamespace(connection=connection)
        module.remove_project_default(apps, editor)
        module.remove_project_default(apps, editor)
        self.project.refresh_from_db()
        self.shot.refresh_from_db()
        self.assertEqual(self.project.production_settings, retained)
        self.assertEqual(self.shot.conditioning_mode, before_mode)
        self.assertFalse(SeriesProject.objects.filter(production_settings__has_key="default_conditioning_mode").exists())
