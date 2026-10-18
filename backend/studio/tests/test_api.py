import importlib
import json
from copy import deepcopy
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
    SeriesProject,
    Shot,
    ShotCharacterRelation,
    Style,
)
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
    resolve_configuration,
)
from studio.services.security_boundaries import boundary_int
from studio.tests.factories import build_story


class APIFlowTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene,
            self.episode, self.shot1, self.shot2,
        ) = build_story()
        for shot in (self.shot1, self.shot2):
            ShotCharacterRelation.objects.create(shot=shot, character=self.character, role="visible", position=0)
            ShotCharacterRelation.objects.create(shot=shot, character=self.character, role="speaker", position=0)
            shot.dialogue[0]["voiceover"] = False
            shot.save(update_fields=["dialogue"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_ownership_isolation(self):
        other = get_user_model().objects.create_user(username="bob", password="StrongPass123!")
        self.client.force_authenticate(other)
        response = self.client.get(f"/api/v1/projects/{self.project.id}/")
        self.assertEqual(response.status_code, 404)

    def test_project_default_shot_duration_obeys_configured_policy(self):
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
            "conditioning_mode": "T2VA",
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
                "production.default_include_audio": False,
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
            "conditioning_mode": "T2VA",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["duration_ms"], 6000)
        self.assertFalse(created.data["include_audio"])

        rejected = self.client.patch(
            f"/api/v1/shots/{created.data['id']}/",
            {"duration_ms": 9000},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("4000–8000ms", str(rejected.data))

    def test_switching_video_shot_to_image_clears_video_flags_not_reference_facts(self):
        import hashlib
        import tempfile
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=root))
        image = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
        content = image.getvalue()
        reference = Asset.objects.create(owner=self.user, style=self.project.style,
            kind="character", name="Explicit character source", mime_type="image/png",
            file=SimpleUploadedFile("character.png", content, content_type="image/png"),
            checksum=hashlib.sha256(content).hexdigest())
        self.character.reference_assets.add(reference)
        self.shot1.conditioning_mode = "T2VA"
        from studio.models import ShotReferenceUse
        use = ShotReferenceUse.objects.create(shot=self.shot1, use_key="portrait", position=0,
            media_kind="image", purpose="原始外观描述", source_mode="existing", asset=reference,
            asset_sha256=reference.checksum)
        references = ShotReferenceUse.objects.values().get(pk=use.pk)
        self.shot1.save(update_fields=(
            "conditioning_mode", "updated_at",
        ))

        changed = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"media_type": Shot.MediaType.IMAGE},
            format="json",
        )

        self.assertEqual(changed.status_code, 200, changed.data)
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.media_type, Shot.MediaType.IMAGE)
        self.assertFalse(self.shot1.include_audio)
        self.assertEqual(self.shot1.conditioning_mode, "")
        self.assertEqual(ShotReferenceUse.objects.values().get(pk=use.pk), references)

        # Content persistence cannot change per-use selection.
        cleared = self.client.patch(f"/api/v1/shots/{self.shot1.id}/", {"reference_bindings": {}}, format="json")
        self.assertEqual(cleared.status_code, 400, cleared.data)
        self.shot1.refresh_from_db()
        self.assertEqual(ShotReferenceUse.objects.values().get(pk=use.pk), references)

        rejected = self.client.patch(
            f"/api/v1/shots/{self.shot2.id}/",
            {
                "media_type": Shot.MediaType.IMAGE,
                "include_audio": False,
                "conditioning_mode": "T2VA",
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("conditioning_mode", str(rejected.data))

    def test_retired_reference_storage_does_not_block_or_change_content_save(self):
        self.assertNotIn("reference_bindings", {field.name for field in Shot._meta.get_fields()})
        response = self.client.patch(f"/api/v1/shots/{self.shot1.pk}/", {"media_type": "image"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("reference_bindings", response.data)
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.media_type, "image")

    def test_video_shot_api_has_no_stale_100ms_floor(self):
        revision = create_config_revision(
            scope_type="project",
            project=self.project,
            values={
                "production.default_shot_duration_ms": 75,
                "production.video_shot_min_duration_ms": 50,
                "production.video_shot_max_duration_ms": 100,
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

        created = self.client.post("/api/v1/shots/", {
            "episode": str(self.episode.id),
            "number": 3,
            "narrative": "低于旧静态门槛但符合当前项目合同",
            "media_type": Shot.MediaType.VIDEO,
            "conditioning_mode": "T2VA",
            "duration_ms": 50,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        tightened = create_config_revision(
            scope_type="project",
            project=self.project,
            values={
                "production.video_shot_min_duration_ms": 60,
            },
            created_by=self.user,
        )
        publish_config_revision(tightened, published_by=self.user)
        rejected = self.client.patch(
            f"/api/v1/shots/{created.data['id']}/",
            {"duration_ms": 50},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("60–100ms", str(rejected.data))

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
        high_project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user, title="超长默认值", width=768, height=1344, fps=24,
            default_shot_duration_ms=20000, continuity_seed=1,
        )
        self.shot1.media_type = Shot.MediaType.VIDEO
        self.shot1.duration_ms = 100
        self.shot1.save(update_fields=("media_type", "duration_ms", "updated_at"))
        self.shot2.media_type = Shot.MediaType.IMAGE
        self.shot2.duration_ms = 100
        self.shot2.save(update_fields=("media_type", "duration_ms", "updated_at"))
        long_video = Shot.objects.create(include_audio=True,
            episode=self.episode, number=3, duration_ms=20000,
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

    def test_seeded_demo_uses_published_creation_policy_and_resyncs_existing_demo(self):
        call_command("seed_data", "--with-demo-project", stdout=StringIO(), verbosity=0)
        current_policy = resolve_configuration(
            required_keys=("production.project_creation_policy",),
        ).values["production.project_creation_policy"]
        changed_policy = deepcopy(current_policy)
        changed_policy.update({
            "default_language": "Chinese-Simplified",
            "default_aspect_ratio": "16:9",
            "fps": 30,
            "default_shot_media_type": Shot.MediaType.IMAGE,
        })
        revision = create_config_revision(
            scope_type="system",
            values={
                "production.project_creation_policy": changed_policy,
                "production.default_resolution_width": 1344,
                "production.default_resolution_height": 768,
                "production.default_shot_duration_ms": 6000,
                "production.default_include_audio": False,
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

        call_command("seed_data", "--with-demo-project", stdout=StringIO(), verbosity=0)

        demo = get_user_model().objects.get(username="demo")
        project = demo.series_projects.get(title="无名剑影")
        self.assertEqual(project.language, "Chinese-Simplified")
        self.assertEqual(project.aspect_ratio, "16:9")
        self.assertEqual((project.width, project.height), (1344, 768))
        self.assertEqual(project.fps, 30)
        self.assertEqual(project.default_shot_duration_ms, 6000)
        shot = project.episodes.get(number=1).shots.get(number=1)
        self.assertEqual(shot.duration_ms, 6000)
        self.assertEqual(shot.media_type, Shot.MediaType.IMAGE)
        self.assertFalse(shot.include_audio)
        self.assertEqual(shot.conditioning_mode, "")
        self.assertEqual(list(shot.character_relations.order_by("role").values_list("role", "position")),
            [("speaker", 0), ("visible", 0)])

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

    def test_page_size_query_is_bounded_and_preserved_across_pages(self):
        maximum_page_size = boundary_int("security.api.max_page_size")
        page_size = min(maximum_page_size, 50)
        final_page_size = min(page_size, 40)
        asset_count = page_size + final_page_size
        style = Style.objects.create(owner=self.user, slug="pagination-style", name="分页测试")
        Asset.objects.bulk_create([
            Asset(owner=self.user, style=style, kind=Asset.Kind.REFERENCE, name=f"asset-{index:03d}")
            for index in range(asset_count)
        ])
        first_page = self.client.get(
            "/api/v1/assets/",
            {"style": str(style.id), "page_size": page_size},
        )
        self.assertEqual(first_page.status_code, 200, first_page.data)
        self.assertEqual(first_page.data["count"], asset_count)
        self.assertEqual(len(first_page.data["results"]), page_size)
        self.assertIn(f"page_size={page_size}", first_page.data["next"])
        second_page = self.client.get(first_page.data["next"])
        self.assertEqual(len(second_page.data["results"]), final_page_size)
        self.assertIsNone(second_page.data["next"])

    def test_default_page_size_is_resolved_from_configuration(self):
        configured_default = int(resolve_configuration(
            required_keys=("api.default_page_size",),
        ).values["api.default_page_size"])
        maximum_page_size = boundary_int("security.api.max_page_size")
        expected_page_size = (
            configured_default + 1
            if configured_default < maximum_page_size
            else max(1, configured_default - 1)
        )
        revision = create_config_revision(
            scope_type="system",
            values={"api.default_page_size": expected_page_size},
            created_by=self.user,
            change_summary="test configurable pagination default",
        )
        publish_config_revision(revision, published_by=self.user)
        style = Style.objects.create(
            owner=self.user,
            slug="configured-pagination-style",
            name="配置分页测试",
        )
        Asset.objects.bulk_create([
            Asset(
                owner=self.user,
                style=style,
                kind=Asset.Kind.REFERENCE,
                name=f"configured-asset-{index:03d}",
            )
            for index in range(expected_page_size + 1)
        ])

        response = self.client.get("/api/v1/assets/", {"style": str(style.id)})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), expected_page_size)
        self.assertIn("page=2", response.data["next"])

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
