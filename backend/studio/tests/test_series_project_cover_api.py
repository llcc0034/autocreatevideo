from io import BytesIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset, Episode, SeriesProject, Shot


def image_upload(
    name="cover.png",
    *,
    image_format="PNG",
    content_type="image/png",
    animated=False,
):
    stream = BytesIO()
    first = Image.new("RGB", (18, 27), color=(34, 76, 118))
    if animated:
        second = Image.new("RGB", (18, 27), color=(181, 102, 42))
        first.save(
            stream,
            format=image_format,
            save_all=True,
            append_images=[second],
            duration=80,
            loop=0,
        )
    else:
        first.save(stream, format=image_format)
    return SimpleUploadedFile(name, stream.getvalue(), content_type=content_type)


def jpeg_upload_with_exif(name="cover-exif.jpg"):
    stream = BytesIO()
    image = Image.new("RGB", (18, 27), color=(34, 76, 118))
    exif = image.getexif()
    exif[270] = "private location note"
    exif[274] = 6
    image.save(stream, format="JPEG", exif=exif)
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/jpeg")


class SeriesProjectCoverAPITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_root = Path(self.temp.name) / "media"
        self.asset_root = Path(self.temp.name) / "library"
        self.media_root.mkdir()
        self.asset_root.mkdir()
        self.override = override_settings(
            MEDIA_ROOT=self.media_root,
            ASSET_LIBRARY_ROOT=self.asset_root,
            ASSET_UPLOAD_MAX_BYTES=1024 * 1024,
            ASSET_TENANT_QUOTA_BYTES=10 * 1024 * 1024,
            ASSET_IMAGE_MAX_WIDTH=1024,
            ASSET_IMAGE_MAX_HEIGHT=1024,
            ASSET_IMAGE_MAX_PIXELS=1024 * 1024,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.user = get_user_model().objects.create_user(
            username="cover-owner", password="StrongPass123!",
        )
        self.other = get_user_model().objects.create_user(
            username="cover-other", password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="封面测试连续剧")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def upload_cover(self, project=None, *, name="cover.png"):
        project = project or self.project
        response = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": f"{project.title} · 封面",
                "metadata": json.dumps({
                    "usage": "project_cover",
                    "project_id": str(project.id),
                }),
                "file": image_upload(name),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"]), response

    def bind_cover(self, project, asset):
        return self.client.patch(
            f"/api/v1/projects/{project.id}/",
            {"cover_asset": str(asset.id)},
            format="json",
        )

    def test_upload_bind_list_and_signed_preview_round_trip(self):
        asset, _ = self.upload_cover()
        response = self.bind_cover(self.project, asset)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(str(response.data["cover_asset"]), str(asset.id))
        self.assertTrue(response.data["cover_url"].startswith("/api/v1/media/"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.cover_asset_id, asset.id)

        listed = self.client.get("/api/v1/projects/")
        self.assertEqual(listed.status_code, 200, listed.data)
        row = next(item for item in listed.data["results"] if item["id"] == str(self.project.id))
        self.assertEqual(str(row["cover_asset"]), str(asset.id))
        self.assertTrue(row["cover_url"].startswith("/api/v1/media/"))

        self.client.force_authenticate(user=None)
        preview = self.client.get(response.data["cover_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        preview.close()

    def test_cover_binding_is_tenant_and_project_scoped(self):
        asset, _ = self.upload_cover()
        sibling = SeriesProject.objects.create(owner=self.user, title="同租户另一项目")
        mismatch = self.bind_cover(sibling, asset)
        self.assertEqual(mismatch.status_code, 400, mismatch.data)

        self.client.force_authenticate(self.other)
        other_project = SeriesProject.objects.create(owner=self.other, title="另一租户项目")
        other_asset, _ = self.upload_cover(other_project, name="other.png")
        self.client.force_authenticate(self.user)
        cross_tenant = self.bind_cover(self.project, other_asset)
        self.assertEqual(cross_tenant.status_code, 400, cross_tenant.data)

    def test_non_image_and_animated_cover_are_rejected(self):
        invalid = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.REFERENCE,
            name="伪造图片",
            file=SimpleUploadedFile("bad.png", b"not-an-image", content_type="image/png"),
            mime_type="image/png",
        )
        response = self.bind_cover(self.project, invalid)
        self.assertEqual(response.status_code, 400, response.data)

        try:
            animated_upload = image_upload(
                "animated.webp",
                image_format="WEBP",
                content_type="image/webp",
                animated=True,
            )
        except OSError:
            self.skipTest("Pillow build does not support animated WebP")
        animated = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": "动画封面",
                "metadata": json.dumps({
                    "usage": "project_cover",
                    "project_id": str(self.project.id),
                }),
                "file": animated_upload,
            },
            format="multipart",
        )
        self.assertEqual(animated.status_code, 400, animated.data)
        self.assertIn("不支持动画图片", str(animated.data))

    def test_cover_is_orientation_normalized_and_exif_is_stripped(self):
        response = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": "含 EXIF 的封面",
                "metadata": json.dumps({
                    "usage": "project_cover",
                    "project_id": str(self.project.id),
                }),
                "file": jpeg_upload_with_exif(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        asset = Asset.objects.get(pk=response.data["id"])
        with Image.open(asset.file.path) as stored:
            self.assertEqual(stored.size, (27, 18))
            self.assertEqual(len(stored.getexif()), 0)
        self.assertEqual(asset.metadata["manifest_schema_version"], 1)
        self.assertEqual(asset.metadata["source"], "user_upload")
        self.assertEqual(asset.metadata["size_bytes"], asset.file.size)
        self.assertEqual(asset.metadata["cover_processing"], "static_metadata_stripped_v1")

    def test_replacing_and_removing_dedicated_cover_cleans_database_and_file(self):
        first, _ = self.upload_cover(name="first.png")
        self.assertEqual(self.bind_cover(self.project, first).status_code, 200)
        first_path = Path(first.file.path)
        second, _ = self.upload_cover(name="second.png")

        with self.captureOnCommitCallbacks(execute=True):
            replaced = self.bind_cover(self.project, second)
        self.assertEqual(replaced.status_code, 200, replaced.data)
        self.assertFalse(Asset.objects.filter(pk=first.pk).exists())
        self.assertFalse(first_path.exists())

        second_path = Path(second.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.patch(
                f"/api/v1/projects/{self.project.id}/",
                {"cover_asset": None},
                format="json",
            )
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertIsNone(removed.data["cover_asset"])
        self.assertFalse(Asset.objects.filter(pk=second.pk).exists())
        self.assertFalse(second_path.exists())

    def test_unmarked_reference_is_unlinked_but_not_deleted(self):
        response = self.client.post(
            "/api/v1/assets/",
            {"kind": Asset.Kind.REFERENCE, "name": "普通参考图", "file": image_upload("plain.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        asset = Asset.objects.get(pk=response.data["id"])
        path = Path(asset.file.path)
        self.assertEqual(self.bind_cover(self.project, asset).status_code, 200)

        removed = self.client.patch(
            f"/api/v1/projects/{self.project.id}/", {"cover_asset": None}, format="json",
        )
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(path.exists())

    def test_delete_project_or_dedicated_asset_cleans_physical_cover(self):
        asset, _ = self.upload_cover()
        self.assertEqual(self.bind_cover(self.project, asset).status_code, 200)
        path = Path(asset.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.delete(f"/api/v1/projects/{self.project.id}/")
        self.assertEqual(deleted.status_code, 204, deleted.data)
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(path.exists())

        project = SeriesProject.objects.create(owner=self.user, title="直接删除封面素材")
        asset, _ = self.upload_cover(project, name="direct.png")
        self.assertEqual(self.bind_cover(project, asset).status_code, 200)
        path = Path(asset.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.delete(f"/api/v1/assets/{asset.id}/")
        self.assertEqual(deleted.status_code, 204, deleted.data)
        project.refresh_from_db()
        self.assertIsNone(project.cover_asset_id)
        self.assertFalse(path.exists())

    def test_cover_update_rolls_back_if_old_cover_cleanup_fails(self):
        first, _ = self.upload_cover(name="atomic-first.png")
        self.assertEqual(self.bind_cover(self.project, first).status_code, 200)
        second, _ = self.upload_cover(name="atomic-second.png")
        failing_client = APIClient(raise_request_exception=False)
        failing_client.force_authenticate(self.user)

        with patch("studio.views._delete_unused_project_cover", side_effect=RuntimeError("cleanup failed")):
            response = failing_client.patch(
                f"/api/v1/projects/{self.project.id}/",
                {"cover_asset": str(second.id)},
                format="json",
            )
        self.assertEqual(response.status_code, 500)
        self.project.refresh_from_db()
        self.assertEqual(self.project.cover_asset_id, first.id)
        self.assertTrue(Asset.objects.filter(pk=first.id).exists())
        self.assertTrue(Asset.objects.filter(pk=second.id).exists())

    def test_dedicated_cover_cannot_be_reclassified_or_used_as_shot_input(self):
        asset, _ = self.upload_cover()
        metadata_change = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"metadata": {"usage": "reference"}},
            format="json",
        )
        self.assertEqual(metadata_change.status_code, 400, metadata_change.data)

        plain_response = self.client.post(
            "/api/v1/assets/",
            {"kind": Asset.Kind.REFERENCE, "name": "普通参考图", "file": image_upload("plain-ref.png")},
            format="multipart",
        )
        self.assertEqual(plain_response.status_code, 201, plain_response.data)
        promoted = self.client.patch(
            f"/api/v1/assets/{plain_response.data['id']}/",
            {"metadata": {"usage": "project_cover", "project_id": str(self.project.id)}},
            format="json",
        )
        self.assertEqual(promoted.status_code, 400, promoted.data)

        episode = Episode.objects.create(project=self.project, number=1, title="第一集")
        shot = Shot.objects.create(
            episode=episode,
            number=1,
            title="开场",
            media_type=Shot.MediaType.IMAGE,
            duration_ms=5000,
        )
        response = self.client.patch(
            f"/api/v1/shots/{shot.id}/",
            {"reference_assets": [str(asset.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("展示专用素材", str(response.data))

    def test_project_list_cover_query_count_does_not_grow_per_card(self):
        asset, _ = self.upload_cover()
        self.assertEqual(self.bind_cover(self.project, asset).status_code, 200)
        self.client.get("/api/v1/projects/")
        with CaptureQueriesContext(connection) as first_queries:
            self.client.get("/api/v1/projects/")

        for index in range(4):
            project = SeriesProject.objects.create(owner=self.user, title=f"列表封面 {index}")
            asset, _ = self.upload_cover(project, name=f"list-{index}.png")
            self.assertEqual(self.bind_cover(project, asset).status_code, 200)
        with CaptureQueriesContext(connection) as many_queries:
            response = self.client.get("/api/v1/projects/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(len(many_queries), len(first_queries) + 1)
