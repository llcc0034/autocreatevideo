from io import BytesIO
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset
from studio.tests.factories import build_story


def image_upload(name="asset.png"):
    stream = BytesIO()
    Image.new("RGB", (8, 8), color=(30, 60, 90)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


class AssetManagementAPITests(TestCase):
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
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot,
            _,
        ) = build_story("asset-management")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def upload(self, *, kind="character", name="待纠正素材"):
        response = self.client.post(
            "/api/v1/assets/",
            {
                "kind": kind,
                "name": name,
                "description": "素材基础描述",
                "prompt_hint": "固定材质、颜色和结构",
                "file": image_upload(f"{name}.png"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"])

    def test_owner_can_correct_unreferenced_classified_image_kind(self):
        asset = self.upload(kind=Asset.Kind.CHARACTER)

        response = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"kind": Asset.Kind.SCENE},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["kind"], Asset.Kind.SCENE)
        asset.refresh_from_db()
        self.assertEqual(asset.kind, Asset.Kind.SCENE)

    def test_classified_reference_can_be_corrected_to_native_kind(self):
        asset = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.REFERENCE,
            name="历史分类参考",
            description="场景描述",
            prompt_hint="固定场景结构",
            metadata={"reference_category": "character", "note": "keep"},
        )

        response = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"kind": Asset.Kind.SCENE},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        asset.refresh_from_db()
        self.assertEqual(asset.kind, Asset.Kind.SCENE)
        self.assertNotIn("reference_category", asset.metadata)
        self.assertEqual(asset.metadata["note"], "keep")

    def test_bible_or_shot_reference_blocks_reclassification_and_deletion(self):
        bible_asset = self.character.anchor_asset
        bible_asset.description = "人物基础描述"
        bible_asset.prompt_hint = "固定人物外观"
        bible_asset.save(update_fields=("description", "prompt_hint", "updated_at"))
        changed = self.client.patch(
            f"/api/v1/assets/{bible_asset.id}/",
            {"kind": Asset.Kind.SCENE},
            format="json",
        )
        deleted = self.client.delete(f"/api/v1/assets/{bible_asset.id}/")
        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertIn("Bible", str(changed.data))
        self.assertIn("解除引用", str(deleted.data))
        bible_asset.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(bible_asset.kind, Asset.Kind.CHARACTER)
        self.assertEqual(self.character.anchor_asset_id, bible_asset.id)

        shot_asset = self.upload(kind=Asset.Kind.PROP, name="分镜道具")
        self.shot.reference_assets.add(shot_asset)
        shot_deleted = self.client.delete(f"/api/v1/assets/{shot_asset.id}/")
        self.assertEqual(shot_deleted.status_code, 400, shot_deleted.data)
        self.assertIn("分镜参考", str(shot_deleted.data))
        self.assertTrue(Asset.objects.filter(pk=shot_asset.id).exists())
        self.assertTrue(self.shot.reference_assets.filter(pk=shot_asset.id).exists())

    def test_unreferenced_upload_delete_removes_database_row_and_file_after_commit(self):
        asset = self.upload(kind=Asset.Kind.SCENE, name="可删除场景")
        path = Path(asset.file.path)
        self.assertTrue(path.exists())

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/v1/assets/{asset.id}/")

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(Asset.objects.filter(pk=asset.id).exists())
        self.assertFalse(path.exists())

    def test_foreign_and_global_assets_remain_read_only(self):
        foreign = self.upload(kind=Asset.Kind.CHARACTER, name="他人素材")
        other = get_user_model().objects.create_user(
            username="asset-management-other",
            password="StrongPass123!",
        )
        self.client.force_authenticate(other)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/assets/{foreign.id}/",
                {"kind": Asset.Kind.SCENE},
                format="json",
            ).status_code,
            404,
        )
        self.assertEqual(self.client.delete(f"/api/v1/assets/{foreign.id}/").status_code, 404)

        global_asset = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.CHARACTER,
            name="全局素材",
            description="全局人物",
            prompt_hint="固定人物",
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/assets/{global_asset.id}/",
                {"kind": Asset.Kind.SCENE},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(self.client.delete(f"/api/v1/assets/{global_asset.id}/").status_code, 403)
