from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import (
    Asset, CharacterBible, PropBible, SeriesProject, Style,
)
from studio.services.asset_bible_drafts import _snapshot_hash


User = get_user_model()
SHA_A = "a" * 64
SHA_B = "b" * 64


class AssetBibleDraftAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="asset-drafter", password="password123")
        self.other = User.objects.create_user(username="asset-other", password="password123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.project = SeriesProject.objects.create(owner=self.user, title="快速设定")
        self.style = Style.objects.create(
            owner=None, slug="draft-wuxia", name="写实武侠",
            description="冷色写实武侠风格",
            visual_prompt="cinematic wuxia realism",
            is_system=True,
        )
        self.asset = Asset.objects.create(
            owner=None, style=self.style, kind=Asset.Kind.CHARACTER,
            name="凌霜", description="年轻女剑客，冷静克制，月灰色窄袖劲装。",
            prompt_hint="高束黑发，月灰色层叠劲装，右眉尾浅疤，固定玄色剑鞘。",
            local_path="E:/library/character.jpg", mime_type="image/jpeg",
            checksum=SHA_A, is_active=True,
            metadata={"opaque_secret": "must-not-reach-model"},
        )
        self.endpoint = f"/api/v1/projects/{self.project.id}/bible-drafts/from-assets/"

    def request_data(self, **overrides):
        data = {
            "entity_type": "character",
            "output_language": "zh",
            "asset_id": str(self.asset.id),
            "asset_checksum": SHA_A,
        }
        data.update(overrides)
        return data

    @staticmethod
    def runtime_result(*, language="zh", name="凌霜"):
        if language == "en":
            description = "A disciplined young swordswoman in a restrained wuxia setting."
            visual = "High-bound black hair, layered moon-gray narrow-sleeved clothing, and a shallow scar at the right eyebrow tail."
        else:
            description = "冷静克制的年轻女剑客，采用写实武侠设定。"
            visual = "高束黑发，月灰色层叠窄袖劲装，右眉尾浅疤，固定玄色剑鞘。"
        return {
            "ok": True,
            "result": {"name": name, "description": description, "visual_identity": visual},
            "model": "Qwen/Qwen3-4B-GGUF", "elapsed_ms": 12,
        }

    def generate(self, data=None, *, result=None, side_effect=None):
        captured = {}

        def invoke(payload, config):
            captured.update(payload)
            if side_effect:
                side_effect()
            return result or self.runtime_result(
                language=(data or {}).get("output_language", "zh"),
            )

        with patch(
            "studio.services.asset_bible_drafts.require_local_qwen_runtime",
        ), patch(
            "studio.services.asset_bible_drafts.run_local_qwen_runtime",
            side_effect=invoke,
        ):
            response = self.client.post(self.endpoint, data or self.request_data(), format="json")
        return response, captured

    def test_returns_review_only_metadata_grounded_draft_without_bible_write(self):
        response, captured = self.generate(self.request_data())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["evidence_basis"], "asset_metadata")
        self.assertEqual(response.data["reference_mode"], "single_reference")
        self.assertFalse(response.data["pixel_vision_used"])
        self.assertEqual(response.data["draft"]["name"], self.asset.name)
        self.assertEqual(response.data["source_assets"][0]["checksum"], SHA_A)
        self.assertEqual(len(response.data["source_assets"][0]["metadata_sha256"]), 64)
        self.assertFalse(response.data["runtime"]["persisted"])
        self.assertFalse(response.data["runtime"]["pixel_vision_supported"])
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertEqual(PropBible.objects.count(), 0)
        self.assertEqual(captured["operation"], "asset_bible_draft")
        self.assertEqual(len(captured["assets"]), 1)
        model_asset = captured["assets"][0]
        self.assertEqual(model_asset["name"], self.asset.name)
        self.assertNotIn("local_path", model_asset)
        self.assertNotIn("metadata", model_asset)
        self.assertNotIn("opaque_secret", str(captured))

    def test_english_choice_translates_description_and_visual_but_keeps_saved_name(self):
        data = self.request_data(output_language="en")
        response, _ = self.generate(data, result=self.runtime_result(language="en"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["draft"]["name"], "凌霜")
        self.assertNotRegex(response.data["draft"]["description"], r"[\u4e00-\u9fff]")
        self.assertNotRegex(response.data["draft"]["visual_identity"], r"[\u4e00-\u9fff]")

    def test_cross_tenant_asset_is_indistinguishable_from_missing(self):
        private = Asset.objects.create(
            owner=self.other, kind=Asset.Kind.CHARACTER, name="私有角色",
            description="完整描述", prompt_hint="完整视觉锚点",
            mime_type="image/png", checksum=SHA_B,
        )
        response, _ = self.generate(self.request_data(
            asset_id=str(private.id), asset_checksum=SHA_B,
        ))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不存在", str(response.data))

    def test_stale_checksum_and_concurrent_metadata_edit_return_conflict(self):
        stale, _ = self.generate(self.request_data(asset_checksum=SHA_B))
        self.assertEqual(stale.status_code, 409, stale.data)

        def edit_metadata():
            self.asset.description = "生成期间被修改的描述"
            self.asset.save(update_fields=("description", "updated_at"))

        changed, _ = self.generate(self.request_data(), side_effect=edit_metadata)
        self.assertEqual(changed.status_code, 409, changed.data)

    def test_category_and_generic_reference_category_are_strict(self):
        self.asset.kind = Asset.Kind.SCENE
        self.asset.save(update_fields=("kind", "updated_at"))
        wrong, _ = self.generate(self.request_data())
        self.assertEqual(wrong.status_code, 400, wrong.data)

        generic = Asset.objects.create(
            owner=None, kind=Asset.Kind.REFERENCE, name="通用人物参考",
            description="灰衣剑客", prompt_hint="高束发与灰衣",
            mime_type="image/png", checksum=SHA_B, metadata={},
        )
        missing_category, _ = self.generate(self.request_data(
            asset_id=str(generic.id), asset_checksum=SHA_B,
        ))
        self.assertEqual(missing_category.status_code, 400, missing_category.data)
        generic.metadata = {"reference_category": "character"}
        generic.save(update_fields=("metadata", "updated_at"))
        accepted, captured = self.generate(
            self.request_data(asset_id=str(generic.id), asset_checksum=SHA_B),
            result=self.runtime_result(name=generic.name),
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(captured["assets"][0]["kind"], "reference")

    def test_metadata_is_required_because_runtime_does_not_read_pixels(self):
        self.asset.prompt_hint = ""
        self.asset.save(update_fields=("prompt_hint", "updated_at"))
        response, captured = self.generate(self.request_data())
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(captured, {})
        self.assertIn("不读取图片像素", str(response.data))

    def test_video_task_guard_blocks_before_local_model_load(self):
        busy_queryset = Mock()
        busy_queryset.exists.return_value = True
        with patch(
            "studio.services.asset_bible_drafts.GenerationTask.objects.filter",
            return_value=busy_queryset,
        ), patch(
            "studio.services.asset_bible_drafts.require_local_qwen_runtime",
        ) as require_runtime:
            response = self.client.post(self.endpoint, self.request_data(), format="json")
        self.assertEqual(response.status_code, 409, response.data)
        require_runtime.assert_not_called()

    def test_pixel_claim_or_changed_name_is_rejected(self):
        pixel = self.runtime_result()
        pixel["result"]["visual_identity"] = "从图中可见高束黑发。"
        rejected, _ = self.generate(self.request_data(), result=pixel)
        self.assertEqual(rejected.status_code, 503, rejected.data)
        renamed, _ = self.generate(
            self.request_data(), result=self.runtime_result(name="模型擅自改名"),
        )
        self.assertEqual(renamed.status_code, 503, renamed.data)
        wrong_language, _ = self.generate(
            self.request_data(output_language="zh"),
            result=self.runtime_result(language="en"),
        )
        self.assertEqual(wrong_language.status_code, 503, wrong_language.data)

    def test_existing_normalized_name_warns_and_bible_create_rejects_duplicate(self):
        CharacterBible.objects.create(
            project=self.project, slug="existing", name="ＬＩＮＧＳＨＵＡＮ",
            stable_speaker_id="S1",
        )
        self.asset.name = "lingshuan"
        self.asset.save(update_fields=("name", "updated_at"))
        response, _ = self.generate(
            self.request_data(), result=self.runtime_result(name="lingshuan"),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(any("名称" in warning for warning in response.data["warnings"]))
        duplicate = self.client.post("/api/v1/props/", {
            "project": str(self.project.id), "slug": "duplicate-name",
            "name": "LINGSHUAN", "is_locked": False,
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertIn("name", duplicate.data["error"]["details"])

    def test_unreferenced_asset_kind_can_be_corrected_after_creation(self):
        owned = Asset.objects.create(
            owner=self.user, kind=Asset.Kind.CHARACTER, name="用户人物",
            description="人物描述", prompt_hint="人物视觉锚点",
            mime_type="image/png", checksum=SHA_B,
        )
        response = self.client.patch(
            f"/api/v1/assets/{owned.id}/", {"kind": "scene"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        owned.refresh_from_db()
        self.assertEqual(owned.kind, Asset.Kind.SCENE)

    def test_owner_classified_image_create_requires_description_and_prompt(self):
        missing_description = self.client.post("/api/v1/assets/", {
            "kind": "character", "name": "未描述人物", "prompt_hint": "灰衣",
        }, format="json")
        self.assertEqual(missing_description.status_code, 400, missing_description.data)
        self.assertIn("description", missing_description.data["error"]["details"])
        missing_prompt = self.client.post("/api/v1/assets/", {
            "kind": "scene", "name": "未锚定场景", "description": "雨夜客栈",
        }, format="json")
        self.assertEqual(missing_prompt.status_code, 400, missing_prompt.data)
        self.assertIn("prompt_hint", missing_prompt.data["error"]["details"])
        unclassified_reference = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "普通首尾帧参考",
        }, format="json")
        self.assertEqual(unclassified_reference.status_code, 201, unclassified_reference.data)

        invalid_category = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "非法分类参考",
            "metadata": {"reference_category": "costume"},
        }, format="json")
        self.assertEqual(invalid_category.status_code, 400, invalid_category.data)
        self.assertIn("metadata", invalid_category.data["error"]["details"])

        classified_missing_description = self.client.post("/api/v1/assets/", {
            "kind": "reference", "name": "分类人物参考", "prompt_hint": "高束发，灰衣",
            "metadata": {"reference_category": "character"},
        }, format="json")
        self.assertEqual(
            classified_missing_description.status_code, 400,
            classified_missing_description.data,
        )
        self.assertIn("description", classified_missing_description.data["error"]["details"])

    def bible_sync_payload(self, *, asset=None, snapshot_hash=None, updated_at=None, name="新角色"):
        asset = asset or self.asset
        snapshot_hash = snapshot_hash or _snapshot_hash([asset])
        updated_at = updated_at or asset.updated_at.isoformat()
        return {
            "characters": [{
                "slug": "new-character", "name": name, "stable_speaker_id": "S1",
                "description": "可编辑基础描述", "visual_identity": "可编辑视觉锚点",
                "reference_assets": [str(asset.id)], "is_locked": False,
                "metadata": {"asset_draft_provenance": {
                    "evidence_basis": "asset_metadata", "pixel_vision_used": False,
                    "reference_mode": "single_reference", "entity_type": "character",
                    "asset_id": str(asset.id), "asset_checksum": asset.checksum,
                    "asset_updated_at": updated_at,
                    "asset_metadata_sha256": snapshot_hash,
                    "source_metadata_sha256": snapshot_hash,
                }},
            }],
            "scenes": [], "props": [],
        }

    def test_sync_bibles_accepts_current_provenance_and_rejects_source_drift(self):
        asset_response = self.client.get(f"/api/v1/assets/{self.asset.id}/")
        self.assertEqual(asset_response.status_code, 200, asset_response.data)
        self.assertEqual(asset_response.data["metadata_sha256"], _snapshot_hash([self.asset]))
        accepted = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            self.bible_sync_payload(
                snapshot_hash=asset_response.data["metadata_sha256"],
                updated_at=asset_response.data["updated_at"],
            ), format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(CharacterBible.objects.count(), 1)

        existing = CharacterBible.objects.get()
        historical_provenance = dict(existing.metadata["asset_draft_provenance"])
        old_hash = _snapshot_hash([self.asset])
        old_updated_at = self.asset.updated_at.isoformat()
        self.asset.description = "另一标签页修改了素材描述"
        self.asset.save(update_fields=("description", "updated_at"))

        ordinary_resync = self.bible_sync_payload(
            snapshot_hash=old_hash, updated_at=old_updated_at,
        )
        ordinary_resync["characters"][0]["id"] = str(existing.id)
        second_reference = Asset.objects.create(
            owner=self.user, kind=Asset.Kind.CHARACTER, name="补充人物参考",
            description="补充人物元数据", prompt_hint="补充稳定外观",
            mime_type="image/png", checksum=SHA_B,
        )
        ordinary_resync["characters"][0]["reference_assets"].append(
            str(second_reference.id)
        )
        resaved = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            ordinary_resync, format="json",
        )
        self.assertEqual(resaved.status_code, 200, resaved.data)
        existing.refresh_from_db()
        self.assertEqual(
            existing.metadata["asset_draft_provenance"], historical_provenance,
        )

        tampered = self.bible_sync_payload(snapshot_hash="f" * 64)
        tampered["characters"][0]["id"] = str(existing.id)
        rejected_tamper = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/", tampered, format="json",
        )
        self.assertEqual(rejected_tamper.status_code, 400, rejected_tamper.data)
        existing.refresh_from_db()
        self.assertEqual(
            existing.metadata["asset_draft_provenance"], historical_provenance,
        )

        CharacterBible.objects.all().delete()
        stale = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            self.bible_sync_payload(
                snapshot_hash=old_hash, updated_at=old_updated_at,
            ), format="json",
        )
        self.assertEqual(stale.status_code, 400, stale.data)
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertIn("重新生成", str(stale.data))

        invalid_time = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            self.bible_sync_payload(updated_at="2026-99-99T99:99:99+99:99"), format="json",
        )
        self.assertEqual(invalid_time.status_code, 400, invalid_time.data)

    def test_sync_bibles_rejects_wrong_kind_inactive_and_cross_tenant_assets(self):
        endpoint = f"/api/v1/projects/{self.project.id}/sync-bibles/"

        self.asset.kind = Asset.Kind.SCENE
        self.asset.save(update_fields=("kind", "updated_at"))
        wrong_kind = self.client.post(
            endpoint, self.bible_sync_payload(), format="json",
        )
        self.assertEqual(wrong_kind.status_code, 400, wrong_kind.data)
        self.asset.kind = Asset.Kind.CHARACTER
        self.asset.is_active = False
        self.asset.save(update_fields=("kind", "is_active", "updated_at"))
        inactive = self.client.post(endpoint, self.bible_sync_payload(), format="json")
        self.assertEqual(inactive.status_code, 400, inactive.data)
        self.asset.is_active = True
        self.asset.save(update_fields=("is_active", "updated_at"))

        private = Asset.objects.create(
            owner=self.other, kind=Asset.Kind.CHARACTER, name="他人素材",
            description="人物描述", prompt_hint="人物视觉锚点",
            mime_type="image/png", checksum=SHA_B,
        )
        cross_tenant = self.client.post(
            endpoint, self.bible_sync_payload(asset=private), format="json",
        )
        self.assertEqual(cross_tenant.status_code, 400, cross_tenant.data)

    def test_sync_bibles_enforces_cross_type_nfkc_name_uniqueness(self):
        payload = {
            "characters": [{
                "slug": "same-character", "name": "Ａｎｃｈｏｒ", "stable_speaker_id": "S1",
            }],
            "scenes": [{"slug": "same-scene", "name": "anchor"}],
            "props": [],
        }
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/", payload, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(CharacterBible.objects.count(), 0)

    def test_global_assets_are_read_only_even_for_staff(self):
        staff = User.objects.create_superuser(
            username="asset-admin", password="password123", email="admin@example.com",
        )
        staff_client = APIClient()
        staff_client.force_authenticate(staff)
        changed = staff_client.patch(
            f"/api/v1/assets/{self.asset.id}/", {"name": "篡改内置素材"}, format="json",
        )
        self.assertEqual(changed.status_code, 400, changed.data)
        deleted = staff_client.delete(f"/api/v1/assets/{self.asset.id}/")
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertTrue(Asset.objects.filter(pk=self.asset.id).exists())

    def test_direct_bible_api_cannot_bypass_asset_category_rules(self):
        scene_asset = Asset.objects.create(
            owner=self.user, kind=Asset.Kind.SCENE, name="场景图",
            description="雨夜客栈", prompt_hint="木构客栈与冷雨",
            mime_type="image/png", checksum=SHA_B,
        )
        response = self.client.post("/api/v1/characters/", {
            "project": str(self.project.id), "slug": "wrong-reference",
            "name": "错误人物", "stable_speaker_id": "S1", "is_locked": False,
            "reference_assets": [str(scene_asset.id)],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertIn("类别", str(response.data))
