"""M5 exact-use resolution with disposable media, no model or business adoption."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import wave

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import Asset, AssetReferenceSeries, AssetReferenceVersion, ShotReferenceUse
from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.shot_reference_sources import _use_rows, resolve_shot_reference_sources
from studio.tests.factories import build_story
from studio.tests.test_asset_management_api import image_upload


class ShotReferenceSourceTests(TestCase):
    def setUp(self):
        self.owner, self.project, self.character, _, _, self.shot, self.second = build_story("shot-sources")
        self.other, self.other_project, _, _, _, self.foreign_shot, _ = build_story("other-shot-sources")
        root = self.enterContext(TemporaryDirectory())
        self.root = Path(root)
        self.enterContext(override_settings(MEDIA_ROOT=root, ASSET_LIBRARY_ROOT=self.root / "library"))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No external call")))
        self.asset = self.media()

    def media(self, data=None, *, kind="image", owner=None):
        data = image_upload().read() if data is None else data
        suffix, mime = {"image": ("png", "image/png"), "video": ("mp4", "video/mp4"),
            "audio": ("wav", "audio/wav")}[kind]
        asset = Asset(owner=owner or self.owner, kind="reference" if kind == "image" else f"reference_{kind}",
            name="Isolated material", mime_type=mime, checksum=sha256(data).hexdigest(),
            source_info=ASSET_UPLOAD_SOURCE_INFO)
        asset.file.save(f"fixture.{suffix}", ContentFile(data))
        return asset

    def use(self, *, shot=None, asset=None, version=None, setting=None, key="appearance", position=0, **changes):
        asset = None if version else (asset or self.asset)
        return ShotReferenceUse.objects.create(shot=shot or self.shot, use_key=key, position=position,
            setting_revision=setting, reference_version=version, asset=asset, **{
                "media_kind": "image", "purpose": "configured appearance", "source_mode": "existing",
                "asset_sha256": (version.asset if version else asset).checksum, **changes})

    def resolve(self, **changes):
        return resolve_shot_reference_sources(**{"actor": self.owner, "project_id": self.project.pk,
            "shot_ids": [self.shot.pk, self.second.pk], **changes})

    def setting(self, name, primary=None):
        variant = create_variant(actor=self.owner, project_id=self.project.pk, entity_kind="character",
            entity_id=self.character.pk, appearance_role="variant" if primary else "primary",
            primary_setting_revision_id=primary.pk if primary else None,
            primary_setting_sha256=primary.content_sha256 if primary else "",
            value={"name": name, "reference_name": name, "identity_anchor": "" if primary else "person identity",
                "appearance_anchor": name, "basis_asset_id": None, "basis_asset_sha256": ""})
        return variant.revisions.get(revision=1)

    def version(self, setting=None, primary=None, asset=None):
        series = AssetReferenceSeries.objects.create(owner=self.owner, name="isolated series",
            role="variant" if primary else "primary", primary_series=primary,
            bible_variant=setting.variant if setting else None)
        return attach_asset(actor=self.owner, series_id=series.pk, asset_id=(asset or self.asset).pk,
            source="existing", setting_revision_id=setting.pk if setting else None)[0]

    def test_empty_uses_do_not_read_legacy_pools_or_choose_primary(self):
        self.assertTrue(self.character.anchor_asset_id)
        self.assertEqual(self.resolve().value["shots"], [
            {"shot_id": str(shot.pk), "uses": []} for shot in (self.shot, self.second)])

    def test_creative_description_is_optional_without_changing_media_identity(self):
        use = self.use(purpose="")
        value = self.resolve().value["shots"][0]["uses"][0]
        self.assertEqual(value["purpose"], "")
        self.assertEqual(value["asset"]["asset_id"], str(self.asset.pk))
        use.refresh_from_db()
        self.assertEqual(use.purpose, "")

    def test_two_shots_same_person_keep_different_exact_clothes_and_old_versions(self):
        primary = self.setting("主图")
        base = self.version(primary)
        uniform, coat = self.setting("校服", primary), self.setting("雨衣", primary)
        uniform_version = self.version(uniform, base.series)
        coat_bytes = BytesIO()
        Image.new("RGB", (8, 8), color="red").save(coat_bytes, format="PNG")
        coat_version = self.version(coat, base.series, self.media(coat_bytes.getvalue()))
        self.use(setting=uniform, version=uniform_version)
        self.use(shot=self.second, setting=coat, version=coat_version)
        original = self.resolve().value
        newer = attach_asset(actor=self.owner, series_id=uniform_version.series_id,
            asset_id=self.media().pk, source="existing", setting_revision_id=uniform.pk)[0]
        revise_variant(actor=self.owner, variant=uniform.variant, expected_revision=1,
            value={**setting_payload(uniform), "appearance_anchor": "new uniform setting"})
        self.assertEqual(self.resolve().value, original)
        rows = [shot["uses"][0] for shot in original["shots"]]
        self.assertEqual([row["setting"]["entity_id"] for row in rows], [str(self.character.pk)] * 2)
        self.assertEqual([row["setting"]["reference_name"] for row in rows], ["校服", "雨衣"])
        self.assertEqual([row["source"]["reference_version_id"] for row in rows],
            [str(uniform_version.pk), str(coat_version.pk)])
        self.assertNotEqual(rows[0]["source"]["reference_version_id"], str(newer.pk))
        self.assertNotEqual(rows[0]["asset"]["asset_id"], rows[1]["asset"]["asset_id"])
        self.assertEqual([row["asset"]["sha256"] for row in rows],
            [uniform_version.asset.checksum, coat_version.asset.checksum])
        self.assertNotEqual(rows[0]["asset"]["sha256"], rows[1]["asset"]["sha256"])

    def test_independent_video_audio_use_real_files_without_bible_or_series(self):
        buf = BytesIO()
        with wave.open(buf, "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 1600)
        audio = self.media(buf.getvalue(), kind="audio")
        video_path = self.root / "isolated.mp4"
        subprocess.run([str(getattr(settings, "FFMPEG_BINARY", "ffmpeg")), "-v", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=32x32:r=10", "-t", "0.2", "-pix_fmt", "yuv420p", str(video_path)],
            check=True, capture_output=True, timeout=20)
        video = self.media(video_path.read_bytes(), kind="video")
        self.use(asset=video, key="movement", media_kind="video", purpose="camera motion")
        self.use(asset=audio, key="voice", position=1, media_kind="audio", purpose="voice reference", source_mode="upload")
        uses = self.resolve().value["shots"][0]["uses"]
        self.assertEqual([row["media_kind"] for row in uses], ["video", "audio"])
        self.assertTrue(all(row["setting"] is None and row["source"]["kind"] == "asset" for row in uses))
        self.assertFalse(AssetReferenceSeries.objects.exists())
        self.assertNotIn(str(self.root), str(uses))

    def test_repeated_asset_retains_every_use_order_and_does_not_write_or_share_return_values(self):
        for index, key in enumerate(("composition", "outfit", "lighting")):
            self.use(key=key, position=index, purpose=f"purpose-{key}")
        from studio.services.shot_reference_sources import resolve_execution_asset_media
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.shot_reference_sources.resolve_execution_asset_media", wraps=resolve_execution_asset_media) as read:
            result = self.resolve(shot_ids=[self.second.pk, self.shot.pk])
        read.assert_called_once()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual([row["use_key"] for row in result.value["shots"][1]["uses"]],
            ["composition", "outfit", "lighting"])
        changed = result.value
        changed["shots"][1]["uses"][0]["purpose"] = "caller modified"
        self.assertNotEqual(changed, result.value)
        self.assertEqual(self.resolve(shot_ids=[self.second.pk, self.shot.pk]).value, result.value)

    def test_pending_use_is_a_path_error_not_empty_success_or_auto_selection(self):
        pending = ShotReferenceUse.objects.create(shot=self.shot, use_key="unselected", position=0,
            media_kind="image", purpose="required appearance")
        with self.assertRaises(ValidationError) as error: self.resolve()
        self.assertIn(f"reference_uses.{self.shot.pk}.unselected", error.exception.message_dict)
        self.assertIn("尚未选择", str(error.exception))
        pending.refresh_from_db()
        self.assertIsNone(pending.asset_id)

    def test_permissions_invalid_ids_duplicates_and_cross_project_never_partially_resolve(self):
        self.use()
        for changes in ({"actor": AnonymousUser()}, {"actor": self.other}, {"project_id": uuid4()},
                {"project_id": "bad"}, {"shot_ids": "bad"}, {"shot_ids": ["bad"]},
                {"shot_ids": [self.shot.pk, self.shot.pk]}, {"shot_ids": [self.shot.pk, uuid4()]},
                {"shot_ids": [self.shot.pk, self.foreign_shot.pk]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.resolve(**changes)

    def test_staff_does_not_make_another_tenants_media_valid_for_project(self):
        row = self.use(asset=self.media(owner=self.other))
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        with self.assertRaises(ValidationError): self.resolve()
        foreign_series = AssetReferenceSeries.objects.create(owner=self.other, role="primary", name="foreign")
        foreign = AssetReferenceVersion.objects.create(series=foreign_series, version=1, source="existing", asset=self.asset)
        row.asset, row.reference_version = None, foreign
        row.save(update_fields=["asset", "reference_version"])
        with self.assertRaisesMessage(ValidationError, "其他用户"): self.resolve()

    def test_wrong_setting_revision_and_series_binding_are_not_interchangeable(self):
        primary = self.setting("primary")
        base = self.version(primary)
        variant = self.setting("coat", primary)
        row = self.use(version=base, setting=variant)
        with self.assertRaisesMessage(ValidationError, "另一实体或变体"): self.resolve()
        next_revision = revise_variant(actor=self.owner, variant=primary.variant, expected_revision=1,
            value={**setting_payload(primary), "appearance_anchor": "edited"}).revisions.get(revision=2)
        row.setting_revision = next_revision
        row.save(update_fields=["setting_revision"])
        with self.assertRaisesMessage(ValidationError, "确切设定修订"): self.resolve()

    def test_inactive_changed_hash_and_recorded_mime_fail_without_replacing_choice(self):
        row = self.use()
        for fields in ({"is_active": False}, {"checksum": "f" * 64}, {"mime_type": "image/jpeg"}):
            original = {key: getattr(self.asset, key) for key in fields}
            Asset.objects.filter(pk=self.asset.pk).update(**fields)
            with self.subTest(fields=fields), self.assertRaises(ValidationError): self.resolve()
            Asset.objects.filter(pk=self.asset.pk).update(**original)
        row.asset_sha256 = "d" * 64
        row.save(update_fields=["asset_sha256"])
        with self.assertRaisesMessage(ValidationError, "偏离本次选择"): self.resolve()

    def test_deleted_temporary_file_fails_even_if_another_valid_version_exists(self):
        version = self.version()
        self.use(version=version)
        attach_asset(actor=self.owner, series_id=version.series_id, asset_id=self.media().pk, source="existing")
        Path(self.asset.file.path).unlink()  # Disposable fixture only.
        with self.assertRaisesMessage(ValidationError, "文件不存在"): self.resolve()
        self.assertEqual(self.shot.reference_uses.get().reference_version_id, version.pk)

    def test_source_mode_is_not_invented_acquisition_or_generation_proof(self):
        row = self.use(source_mode="generated")
        with self.assertRaisesMessage(ValidationError, "原登记素材版本"): self.resolve()
        row.asset, row.reference_version = None, self.version()
        row.save(update_fields=["asset", "reference_version"])
        with self.assertRaisesMessage(ValidationError, "本次生成来源"): self.resolve()
        row.source_mode = "upload"
        row.save(update_fields=["source_mode"])
        self.asset.source_info = "not uploaded"
        self.asset.save(update_fields=["source_info"])
        with self.assertRaisesMessage(ValidationError, "正式上传来源"): self.resolve()
        row.source_mode = "existing"
        row.save(update_fields=["source_mode"])
        self.assertEqual(self.resolve().value["shots"][0]["uses"][0]["source_mode"], "existing")

    def test_uses_query_is_narrow_indexed_and_database_reads_do_not_grow_per_repeated_use(self):
        self.use()
        with CaptureQueriesContext(connection) as one: self.resolve()
        for index in range(1, 5): self.use(key=f"use-{index}", position=index)
        with CaptureQueriesContext(connection) as many: self.resolve()
        self.assertEqual(len(one), len(many))
        query = _use_rows([self.shot.pk])
        sql = str(query.query)
        self.assertNotIn("SELECT *", sql)
        for name in ("metadata", "appearance_anchor", "narrative"): self.assertNotIn(name, sql)
        if connection.vendor == "sqlite": self.assertIn("USING INDEX", query.explain())

    def test_repeated_version_is_verified_once_but_every_use_keeps_its_identity(self):
        version = self.version()
        self.use(version=version)
        self.use(shot=self.second, version=version, key="another-purpose")
        from studio.services.shot_reference_sources import read_media_fulfillment_version
        with patch("studio.services.shot_reference_sources.read_media_fulfillment_version",
                wraps=read_media_fulfillment_version) as read:
            result = self.resolve().value
        read.assert_called_once()
        self.assertNotEqual(result["shots"][0]["uses"][0]["use_id"], result["shots"][1]["uses"][0]["use_id"])
        self.assertEqual([row["uses"][0]["source"]["reference_version_id"] for row in result["shots"]],
            [str(version.pk)] * 2)

    def test_corrupt_setting_and_cross_project_setting_have_explicit_errors(self):
        setting = self.setting("primary")
        self.use(setting=setting)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s WHERE id=%s",
                ["corrupt isolated body", setting.pk.hex])
        with self.assertRaises(ValidationError): self.resolve()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s WHERE id=%s",
                [setting.appearance_anchor, setting.pk.hex])
        self.use(shot=self.foreign_shot, asset=self.media(owner=self.other), setting=setting)
        with self.assertRaisesMessage(ValidationError, "不属于本项目"):
            self.resolve(actor=self.other, project_id=self.other_project.pk, shot_ids=[self.foreign_shot.pk])

    def test_actual_byte_change_and_wrong_media_kind_fail_at_the_common_reader(self):
        row = self.use()
        original = Path(self.asset.file.path).read_bytes()
        Path(self.asset.file.path).write_bytes(b"isolated changed bytes")
        with self.assertRaisesMessage(ValidationError, "哈希"): self.resolve()
        Path(self.asset.file.path).write_bytes(original)
        row.media_kind = "video"
        row.save(update_fields=["media_kind"])
        with self.assertRaises(ValidationError): self.resolve()

    def test_common_reader_api_error_is_located_at_the_exact_use(self):
        self.use(version=self.version())
        with patch("studio.services.shot_reference_sources.read_media_fulfillment_version",
                side_effect=APIValidationError("原生成依据已损坏")), self.assertRaises(ValidationError) as error:
            self.resolve()
        self.assertIn(f"reference_uses.{self.shot.pk}.appearance", error.exception.message_dict)
        self.assertIn("原生成依据已损坏", str(error.exception))
