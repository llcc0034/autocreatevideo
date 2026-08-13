from io import BytesIO
import hashlib
from pathlib import Path
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import Resolver404, resolve
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset
from studio.tests.factories import build_story


def png_upload(name="reference.png", size=(8, 8), content_type="image/png"):
    stream = BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type=content_type)


class AssetSecurityTests(TestCase):
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
        self.user, self.project, *_ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_non_staff_cannot_claim_server_local_path(self):
        candidate = self.asset_root / "global.png"
        candidate.write_bytes(b"not-relevant")
        response = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "伪造路径", "local_path": str(candidate),
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不能声明服务端 local_path", str(response.data))

    def test_valid_image_upload_is_decoded_and_stored_under_owner_directory(self):
        response = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "用户参考", "file": png_upload(),
        }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        asset = Asset.objects.get(pk=response.data["id"])
        path = Path(asset.file.path).resolve()
        self.assertTrue(path.is_relative_to((self.media_root / "assets" / str(self.user.id)).resolve()))
        self.assertEqual(asset.mime_type, "image/png")
        self.assertEqual(len(asset.checksum), 64)
        self.assertEqual(asset.checksum, hashlib.sha256(Path(asset.file.path).read_bytes()).hexdigest())
        self.assertTrue(response.data["preview_url"].startswith("/api/v1/media/"))
        self.client.force_authenticate(user=None)
        download = self.client.get(response.data["preview_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "image/png")
        download.close()
        self.assertEqual(self.client.get(response.data["preview_url"] + "x").status_code, 404)
        self.assertEqual(self.client.get(asset.file.url).status_code, 404)

    def test_invalid_image_content_and_mime_mismatch_are_rejected(self):
        invalid = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "坏文件",
            "file": SimpleUploadedFile("bad.png", b"not-an-image", content_type="image/png"),
        }, format="multipart")
        self.assertEqual(invalid.status_code, 400, invalid.data)
        mismatch = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "MIME伪造",
            "file": png_upload(content_type="image/jpeg"),
        }, format="multipart")
        self.assertEqual(mismatch.status_code, 400, mismatch.data)

    @override_settings(ASSET_IMAGE_MAX_WIDTH=4)
    def test_image_dimensions_are_limited(self):
        response = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "尺寸超限", "file": png_upload(size=(8, 8)),
        }, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("图片尺寸超限", str(response.data))

    @override_settings(ASSET_TENANT_QUOTA_BYTES=1)
    def test_tenant_upload_quota_is_enforced(self):
        response = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "配额超限", "file": png_upload(),
        }, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("租户素材配额不足", str(response.data))

    def test_metadata_edit_preserves_existing_server_metadata(self):
        created = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "元数据更新", "file": png_upload("protected.png"),
        }, format="multipart")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        changed = self.client.patch(
            f"/api/v1/assets/{asset_id}/", {"metadata": {"note": "safe edit"}}, format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        asset = Asset.objects.get(pk=asset_id)
        self.assertEqual(asset.metadata["note"], "safe edit")

    def test_development_asset_static_route_does_not_capture_spa_asset_page(self):
        with self.assertRaises(Resolver404):
            resolve("/assets/")
