from io import BytesIO
from pathlib import Path
import hashlib
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset
from studio.services.security_boundaries import boundary_int
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
        self.assertEqual(response.data["source_info"], "用户本地上传")
        asset = Asset.objects.get(pk=response.data["id"])
        self.assertEqual(asset.source_info, "用户本地上传")
        return asset

    def test_source_info_is_searchable_but_not_client_editable(self):
        asset = self.upload(name="来源检索素材")

        rejected_create = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": "伪造来源素材",
                "source_info": "客户端伪造来源",
                "file": image_upload("forged-source.png"),
            },
            format="multipart",
        )
        self.assertEqual(rejected_create.status_code, 400, rejected_create.data)
        self.assertIn("客户端不能提交或改写", str(rejected_create.data))

        searched = self.client.get("/api/v1/assets/", {"search": "用户本地上传"})
        self.assertEqual(searched.status_code, 200, searched.data)
        self.assertIn(str(asset.id), {str(row["id"]) for row in searched.data["results"]})

        changed = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"source_info": "被客户端改写"},
            format="json",
        )
        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertIn("客户端不能提交或改写", str(changed.data))
        asset.refresh_from_db()
        self.assertEqual(asset.source_info, "用户本地上传")

    def test_asset_create_records_exact_non_upload_source(self):
        staff = get_user_model().objects.create_superuser(
            username="asset-source-staff",
            password="StrongPass123!",
        )
        self.client.force_authenticate(staff)
        managed = self.asset_root / "managed.png"
        managed.write_bytes(image_upload("managed.png").read())

        local = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": "受管路径素材",
                "local_path": str(managed),
            },
            format="json",
        )
        self.assertEqual(local.status_code, 201, local.data)
        self.assertEqual(local.data["source_info"], "服务端受管路径登记")

        external = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.REFERENCE,
                "name": "外部引用素材",
                "reference_url": "https://example.invalid/reference.png",
            },
            format="json",
        )
        self.assertEqual(external.status_code, 201, external.data)
        self.assertEqual(external.data["source_info"], "外部引用地址登记")

        changed_source = self.client.patch(
            f"/api/v1/assets/{external.data['id']}/",
            {
                "reference_url": "",
                "local_path": str(managed),
            },
            format="json",
        )
        self.assertEqual(changed_source.status_code, 200, changed_source.data)
        self.assertEqual(
            changed_source.data["source_info"],
            "服务端受管路径登记",
        )

        missing = self.client.post(
            "/api/v1/assets/",
            {"kind": Asset.Kind.REFERENCE, "name": "无来源素材"},
            format="json",
        )
        self.assertEqual(missing.status_code, 400, missing.data)
        self.assertIn("必须保留上传文件", str(missing.data))

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

        explicitly_source_less = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"reference_url": ""},
            format="json",
        )
        self.assertEqual(
            explicitly_source_less.status_code,
            400,
            explicitly_source_less.data,
        )
        self.assertIn("必须保留上传文件", str(explicitly_source_less.data))

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

    def test_script_asset_upload_download_and_project_preview_import(self):
        upload = SimpleUploadedFile(
            "episode.md",
            "# 第一集\n\n林默：门没有从外面打开。".encode("utf-8"),
            content_type="text/markdown",
        )
        created = self.client.post(
            "/api/v1/assets/",
            {
                "kind": Asset.Kind.SCRIPT,
                "name": "雨夜剧本",
                "description": "可复用剧本素材",
                "file": upload,
            },
            format="multipart",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["kind"], Asset.Kind.SCRIPT)
        self.assertEqual(created.data["mime_type"], "text/markdown")
        self.assertTrue(created.data["download_url"].endswith("?download=1"))

        downloaded = self.client.get(created.data["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded["Content-Disposition"])
        downloaded.close()

        imported = self.client.post(
            "/api/v1/script-sources/",
            {"project_id": str(self.project.id), "asset_id": created.data["id"]},
            format="json",
        )
        self.assertEqual(imported.status_code, 201, imported.data)
        self.assertEqual(imported.data["source"]["source_type"], "library_asset")
        self.assertEqual(str(imported.data["source"]["source_asset"]), created.data["id"])
        self.assertEqual(imported.data["revision"]["status"], "preview")
        self.assertIn("林默", imported.data["revision"]["content"])

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

    def test_asset_api_computes_truthful_smoke_eligibility_from_managed_bytes(self):
        source = self.asset_root / "smoke.png"
        source.write_bytes(b"managed-smoke-image")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        eligible = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE,
            name="可用于烟测",
            local_path=str(source),
            mime_type="image/png",
            checksum=digest,
            is_active=True,
            metadata={"fixture_scope": "workflow_smoke"},
        )
        uppercase_scope = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE,
            name="大写烟测范围",
            local_path=str(source),
            mime_type="image/png",
            checksum=digest,
            is_active=True,
            metadata={"fixture_scope": "WORKFLOW_SMOKE"},
        )
        stale = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE,
            name="摘要已失效",
            local_path=str(source),
            mime_type="image/png",
            checksum="0" * 64,
            is_active=True,
        )
        inactive = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE,
            name="已停用",
            local_path=str(source),
            mime_type="image/png",
            checksum=digest,
            is_active=False,
        )

        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        with patch("studio.services.smoke_assets.sha256_file", wraps=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()) as digest:
            ordinary = self.client.get("/api/v1/assets/?ordering=name")
            self.assertEqual(ordinary.status_code, 200, ordinary.data)
            self.assertEqual(digest.call_count, 0)
            ordinary_rows = ordinary.data.get("results", ordinary.data)
            self.assertNotIn(
                str(eligible.id),
                {str(row["id"]) for row in ordinary_rows},
            )
            self.assertNotIn(
                str(uppercase_scope.id),
                {str(row["id"]) for row in ordinary_rows},
            )
            self.assertNotIn("smoke_eligible", ordinary_rows[0])
            rows = []
            next_url = "/api/v1/assets/workflow-smoke-candidates/"
            while next_url:
                response = self.client.get(next_url)
                self.assertEqual(response.status_code, 200, response.data)
                rows.extend(response.data.get("results", response.data))
                next_url = response.data.get("next")

        by_id = {str(row["id"]): row for row in rows}
        self.assertIn(str(eligible.id), by_id)
        self.assertIs(by_id[str(eligible.id)]["smoke_eligible"], True)
        self.assertEqual(by_id[str(eligible.id)]["smoke_ineligible_reason"], "")
        self.assertIs(by_id[str(stale.id)]["smoke_eligible"], False)
        self.assertEqual(
            by_id[str(stale.id)]["smoke_ineligible_reason"],
            "checksum_mismatch",
        )
        self.assertIs(by_id[str(inactive.id)]["smoke_eligible"], False)
        self.assertEqual(
            by_id[str(inactive.id)]["smoke_ineligible_reason"],
            "inactive",
        )
        self.assertNotIn(str(source), by_id[str(stale.id)]["smoke_ineligible_reason"])

    def test_workflow_smoke_candidates_are_staff_only_and_page_bounded(self):
        denied = self.client.get("/api/v1/assets/workflow-smoke-candidates/")
        self.assertEqual(denied.status_code, 403, denied.data)

        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        maximum_page_size = boundary_int("security.api.max_page_size")
        candidate_count = maximum_page_size + 5
        Asset.objects.bulk_create([
            Asset(
                owner=None,
                kind=Asset.Kind.REFERENCE,
                name=f"候选 {index:03d}",
                is_active=False,
            )
            for index in range(candidate_count)
        ])
        response = self.client.get(
            (
                "/api/v1/assets/workflow-smoke-candidates/"
                f"?page_size={candidate_count}"
            ),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], candidate_count)
        self.assertEqual(len(response.data["results"]), maximum_page_size)
