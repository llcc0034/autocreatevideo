import importlib
import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.apps import apps as django_apps
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    CharacterBible,
    DeferredGenerationRequest,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PropBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplate,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.tests.factories import build_story


@override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
class APIFlowTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene,
            self.episode, self.shot1, self.shot2,
        ) = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_ownership_isolation(self):
        other = get_user_model().objects.create_user(username="bob", password="StrongPass123!")
        self.client.force_authenticate(other)
        response = self.client.get(f"/api/v1/projects/{self.project.id}/")
        self.assertEqual(response.status_code, 404)

    def test_h3_project_default_shot_duration_is_guarded(self):
        self.assertEqual(self.project.default_shot_duration_ms, 5000)
        response = self.client.patch(f"/api/v1/projects/{self.project.id}/", {
            "default_shot_duration_ms": 4999,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("5000–15000ms", str(response.data))

    def test_video_shot_create_and_update_enforce_safe_duration(self):
        payload = {
            "episode": str(self.episode.id),
            "number": 3,
            "narrative": "持续推进的视频镜头",
            "media_type": Shot.MediaType.VIDEO,
        }
        for unsafe_duration in (4999, 15001):
            with self.subTest(duration_ms=unsafe_duration):
                response = self.client.post(
                    "/api/v1/shots/",
                    {**payload, "duration_ms": unsafe_duration},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("duration_ms", str(response.data))

        created = self.client.post("/api/v1/shots/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["duration_ms"], 5000)

        rejected_update = self.client.patch(
            f"/api/v1/shots/{created.data['id']}/",
            {"duration_ms": 15001},
            format="json",
        )
        self.assertEqual(rejected_update.status_code, 400, rejected_update.data)

        image = self.client.post("/api/v1/shots/", {
            "episode": str(self.episode.id), "number": 4,
            "narrative": "允许短时长的静态定帧", "media_type": Shot.MediaType.IMAGE,
            "duration_ms": 100,
        }, format="json")
        self.assertEqual(image.status_code, 201, image.data)
        self.assertEqual(image.data["duration_ms"], 100)

    def test_project_duration_policy_controls_api_validation_and_defaults(self):
        revision = create_config_revision(
            scope_type="project",
            project=self.project,
            values={
                "production.default_shot_duration_ms": 6000,
                "production.video_shot_min_duration_ms": 4000,
                "production.video_shot_max_duration_ms": 8000,
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

        created = self.client.post("/api/v1/shots/", {
            "episode": str(self.episode.id),
            "number": 3,
            "narrative": "数据库策略默认时长",
            "media_type": Shot.MediaType.VIDEO,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["duration_ms"], 6000)

        rejected = self.client.patch(
            f"/api/v1/shots/{created.data['id']}/",
            {"duration_ms": 9000},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("4000–8000ms", str(rejected.data))

    def test_sync_shots_legacy_write_route_is_absent(self):
        response = self.client.post(
            f"/api/v1/episodes/{self.episode.id}/sync-shots/",
            {"shots": []},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_duration_data_migration_clamps_projects_and_only_video_shots(self):
        self.project.default_shot_duration_ms = 100
        self.project.save(update_fields=("default_shot_duration_ms", "updated_at"))
        high_project = SeriesProject.objects.create(
            owner=self.user, title="超长默认值", default_shot_duration_ms=20000,
        )
        self.shot1.media_type = Shot.MediaType.VIDEO
        self.shot1.duration_ms = 100
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.duration_ms = 100
        self.shot2.save(update_fields=("media_type", "duration_ms", "updated_at"))
        long_video = Shot.objects.create(
            episode=self.episode, number=3, narrative="超长视频", duration_ms=20000,
            media_type=Shot.MediaType.VIDEO,
        )

        migration = importlib.import_module("studio.migrations.0007_clamp_video_shot_durations")
        migration.clamp_video_shot_durations(django_apps, None)

        self.project.refresh_from_db()
        high_project.refresh_from_db()
        self.shot1.refresh_from_db()
        self.shot2.refresh_from_db()
        long_video.refresh_from_db()
        self.assertEqual(self.project.default_shot_duration_ms, 5000)
        self.assertEqual(high_project.default_shot_duration_ms, 15000)
        self.assertEqual(self.shot1.duration_ms, 5000)
        self.assertEqual(long_video.duration_ms, 15000)
        self.assertEqual(self.shot2.duration_ms, 100)

    def test_seeded_demo_video_uses_five_second_duration(self):
        call_command("seed_data", "--with-demo-project", stdout=StringIO(), verbosity=0)
        demo = get_user_model().objects.get(username="demo")
        project = demo.series_projects.get(title="无名剑影")
        self.assertEqual(project.default_shot_duration_ms, 5000)
        self.assertEqual(project.episodes.get(number=1).shots.get(number=1).duration_ms, 5000)

    def test_jwt_token_endpoint(self):
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/v1/auth/token/", {"username": "alice", "password": "StrongPass123!"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_breakdown_script_legacy_write_route_is_absent(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            {"episode_count_hint": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_page_size_query_returns_full_90_asset_library_slice(self):
        style = Style.objects.create(owner=self.user, slug="pagination-style", name="分页测试")
        Asset.objects.bulk_create([
            Asset(owner=self.user, style=style, kind=Asset.Kind.REFERENCE, name=f"asset-{index:03d}")
            for index in range(90)
        ])
        response = self.client.get("/api/v1/assets/", {"style": str(style.id), "page_size": 200})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 90)
        self.assertEqual(len(response.data["results"]), 90)
        first_page = self.client.get("/api/v1/assets/", {"style": str(style.id), "page_size": 50})
        self.assertEqual(len(first_page.data["results"]), 50)
        self.assertIn("page_size=50", first_page.data["next"])
        second_page = self.client.get(first_page.data["next"])
        self.assertEqual(len(second_page.data["results"]), 40)
        self.assertIsNone(second_page.data["next"])

    def test_sync_shots_and_sync_bibles_legacy_routes_are_absent(self):
        sync_shots = self.client.post(
            f"/api/v1/episodes/{self.episode.id}/sync-shots/",
            {"shots": []},
            format="json",
        )
        sync_bibles = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            {"characters": [], "scenes": [], "props": []},
            format="json",
        )
        self.assertEqual(sync_shots.status_code, 404)
        self.assertEqual(sync_bibles.status_code, 404)

    def test_locked_bible_must_be_explicitly_unlocked_before_identity_change(self):
        character = self.project.characters.get(stable_speaker_id="S1")
        denied = self.client.patch(f"/api/v1/characters/{character.id}/", {
            "wardrobe": "突然换成红色礼服",
        }, format="json")
        self.assertEqual(denied.status_code, 400)
        allowed = self.client.patch(f"/api/v1/characters/{character.id}/", {
            "is_locked": False, "wardrobe": "剧情需要的红色礼服",
        }, format="json")
        self.assertEqual(allowed.status_code, 200, allowed.data)
