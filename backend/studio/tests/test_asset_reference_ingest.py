"""Formal upload -> original Asset -> relationship, using temporary media only."""
from pathlib import Path
import hashlib
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetReferenceSeries, AssetReferenceVersion, AssetGenerationJob, ProductionRun, SeriesProject
from studio.tests.test_asset_management_api import image_upload
from studio.services.execution_media_sources import resolve_execution_asset_media
from studio.tests.test_input_source_policies import policy


class AssetReferenceIngestTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = override_settings(MEDIA_ROOT=self.root, ASSET_LIBRARY_ROOT=self.root / "library")
        override.enable()
        self.addCleanup(override.disable)
        self.owner = get_user_model().objects.create_user(username="reference-ingest")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.primary = AssetReferenceSeries.objects.create(owner=self.owner, name="主图", role="primary")

    def upload(self, name="主图"):
        response = self.client.post("/api/v1/assets/", {"name": name, "kind": "reference", "file": image_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        return Asset.objects.get(pk=response.data["id"])

    def test_series_roles_come_from_formal_options(self):
        response = self.client.options("/api/v1/asset-reference-series/")
        self.assertEqual(response.status_code, 200, response.data)
        choices = response.data["actions"]["POST"]["role"]["choices"]
        self.assertEqual({row["value"] for row in choices}, {"primary", "variant"})
        self.assertTrue(all(row["display_name"] for row in choices))

    def attach(self, asset, series=None, source="upload"):
        return self.client.post("/api/v1/asset-reference-versions/", {"series_id": str((series or self.primary).pk),
            "asset_id": str(asset.pk), "source": source}, format="json")

    def test_primary_and_two_variants_upload_and_reuse_without_script_or_model(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external call")):
            asset = self.upload()
            self.assertEqual(self.attach(asset).status_code, 201)
            for name in ("校服", "外套"):
                variant = AssetReferenceSeries.objects.create(owner=self.owner, name=name, role="variant", primary_series=self.primary)
                uploaded = self.upload(name)
                response = self.attach(uploaded, variant)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertIsNone(response.data["generated_from_id"])
                self.assertIsNone(response.data["generation_job"])
                reused = self.attach(asset, variant, "existing")
                self.assertEqual(reused.status_code, 201, reused.data)
                self.assertEqual(self.attach(asset, variant, "existing").data["id"], reused.data["id"])
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (3, 5))
        self.assertEqual((SeriesProject.objects.count(), ProductionRun.objects.count(), AssetGenerationJob.objects.count()), (0, 0, 0))
        self.assertEqual(len(list(self.root.rglob("*.png"))), 3)

    def test_upload_failure_does_not_create_empty_asset_or_version(self):
        response = self.client.post("/api/v1/assets/", {"name": "坏图片", "kind": "reference",
            "file": SimpleUploadedFile("bad.png", b"not an image", content_type="image/png")}, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (0, 0))

    def test_missing_changed_and_empty_bytes_reject_new_relation(self):
        for data in (None, b"changed", b""):
            with self.subTest(data=data):
                asset = self.upload()
                path = Path(asset.file.path)
                if data is None:
                    path.unlink()
                else:
                    path.write_bytes(data)
                response = self.attach(asset)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("asset_id", str(response.data))
        self.assertEqual(AssetReferenceVersion.objects.count(), 0)
        self.assertEqual(Asset.objects.count(), 3)

    def test_failed_association_keeps_upload_and_retry_only_adds_relation(self):
        asset = self.upload()
        original = Path(asset.file.path).read_bytes()
        asset.is_active = False
        asset.save(update_fields=["is_active"])
        self.assertEqual(self.attach(asset).status_code, 400)
        self.assertEqual(AssetReferenceVersion.objects.count(), 0)
        asset.is_active = True
        asset.save(update_fields=["is_active"])
        self.assertEqual(self.attach(asset).status_code, 201)
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(Path(asset.file.path).read_bytes(), original)

    def test_matching_checksum_does_not_make_invalid_image_usable(self):
        asset = self.upload()
        data = b"not an image despite matching metadata"
        Path(asset.file.path).write_bytes(data)
        asset.checksum = hashlib.sha256(data).hexdigest()
        asset.save(update_fields=["checksum"])
        response = self.attach(asset)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("asset_id", str(response.data))
        self.assertEqual(AssetReferenceVersion.objects.count(), 0)

    def test_library_association_has_no_execution_size_gate(self):
        asset = self.upload()
        with patch("studio.services.media_generation_targets.resolve_execution_asset_media", wraps=resolve_execution_asset_media) as reader:
            self.assertEqual(self.attach(asset).status_code, 201)
        self.assertIsNone(reader.call_args.kwargs["max_media_bytes"])
        # The same file is still rejected by an execution with a smaller declared limit.
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            resolve_execution_asset_media(owner=self.owner, asset_id=asset.pk, expected_kind="image", max_media_bytes=1)

    def preview_field(self, selection):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post("/api/v1/data-structure-versions/media-source-preview/",
                {"policy": policy(), "selection": selection}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["preview_only"])
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        return response.data

    @patch("requests.sessions.Session.request", side_effect=AssertionError("No external call"))
    def test_foundation_upload_versions_and_field_selection_keep_exact_assets(self, external):
        # The field-configuration preview is an existing staff-only endpoint.
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        main = self.upload()
        main_version = self.attach(main).data
        variants = []
        for name in ("校服", "tenue de ville"):
            response = self.client.post("/api/v1/asset-reference-series/", {
                "name": name, "role": "variant", "primary_series_id": str(self.primary.pk)}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            variants.append(AssetReferenceSeries.objects.get(pk=response.data["id"]))
        uniform, casual = self.upload("制服旧版"), self.upload("便装")
        old_version = self.attach(uniform, variants[0]).data
        casual_version = self.attach(casual, variants[1]).data
        self.assertEqual(self.attach(self.upload("制服新版"), variants[0]).status_code, 201)
        page = self.client.get("/api/v1/asset-reference-versions/", {
            "series_id": str(variants[0].pk), "page_size": 1, "page": 2})
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data["results"][0]["id"], old_version["id"])
        # A picker resolves the displayed exact version, then stores original Asset IDs.
        selected = []
        for version, purpose in ((old_version, "appearance"), (casual_version, "composition")):
            detail = self.client.get(f"/api/v1/asset-reference-versions/{version['id']}/")
            self.assertEqual(detail.status_code, 200, detail.data)
            selected.append({"item_key": f"use-{len(selected)}", "purpose": purpose,
                "asset_id": detail.data["asset"]["id"]})
        selection = {"source": "asset_library", "items": selected}
        before = self.preview_field(selection)
        self.assertTrue(before["valid"], before)
        self.assertEqual([row["asset_id"] for row in before["resolved"]["items"]], [str(uniform.pk), str(casual.pk)])
        self.assertEqual([row["purpose"] for row in before["resolved"]["items"]], ["appearance", "composition"])
        self.assertEqual([row["position"] for row in before["resolved"]["items"]], [0, 1])
        self.assertEqual([row["media_facts"]["sha256"] for row in before["resolved"]["items"]], [uniform.checksum, casual.checksum])
        renamed = self.client.patch(f"/api/v1/asset-reference-series/{variants[0].pk}/",
            {"name": "制服 / costume"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual(self.attach(self.upload("制服第三版"), variants[0]).status_code, 201)
        self.assertEqual(self.preview_field(selection)["resolved"], before["resolved"])
        self.assertEqual(AssetReferenceVersion.objects.get(pk=old_version["id"]).asset_id, uniform.pk)
        self.assertEqual(AssetReferenceVersion.objects.get(pk=main_version["id"]).asset_id, main.pk)
        self.assertEqual((Asset.objects.count(), AssetReferenceSeries.objects.count(), AssetReferenceVersion.objects.count()), (5, 3, 5))
        self.assertEqual((SeriesProject.objects.count(), ProductionRun.objects.count(), AssetGenerationJob.objects.count()), (0, 0, 0))
        self.assertFalse(AssetReferenceSeries.objects.filter(bible_variant__isnull=False).exists())
        external.assert_not_called()

    @patch("requests.sessions.Session.request", side_effect=AssertionError("No external call"))
    def test_foundation_unlink_and_asset_delete_do_not_replace_or_remove_other_inputs(self, external):
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        main, selected, other = self.upload(), self.upload("被选择版本"), self.upload("保留版本")
        self.assertEqual(self.attach(main).status_code, 201)
        variant = self.client.post("/api/v1/asset-reference-series/", {"name": "変体",
            "role": "variant", "primary_series_id": str(self.primary.pk)}, format="json")
        self.assertEqual(variant.status_code, 201, variant.data)
        series = AssetReferenceSeries.objects.get(pk=variant.data["id"])
        chosen_version = self.attach(selected, series).data
        other_version = self.attach(other, series).data
        selection = {"source": "asset_library", "items": [
            {"item_key": "appearance", "purpose": "appearance", "asset_id": str(selected.pk)}]}
        self.assertTrue(self.preview_field(selection)["valid"])
        rejected = self.client.delete(f"/api/v1/assets/{selected.pk}/")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("主图/变体版本关联", str(rejected.data))
        version_url = f"/api/v1/asset-reference-versions/{chosen_version['id']}/"
        impact = self.client.get(version_url + "removal-impact/")
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual({key: impact.data[key] for key in ("version_count", "asset_count", "can_remove")},
            {"version_count": 1, "asset_count": 1, "can_remove": True})
        self.assertEqual(self.client.delete(version_url).status_code, 204)
        self.assertEqual(self.client.get(version_url).status_code, 404)
        self.assertTrue(Asset.objects.filter(pk=selected.pk).exists())
        selected_path = Path(selected.file.path)
        self.assertTrue(selected_path.is_file())
        # Removing a catalogue relationship does not invalidate the already selected Asset.
        self.assertTrue(self.preview_field(selection)["valid"])
        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.delete(f"/api/v1/assets/{selected.pk}/")
        self.assertEqual(removed.status_code, 204, removed.data)
        self.assertFalse(selected_path.exists())
        failed = self.preview_field(selection)
        self.assertFalse(failed["valid"])
        self.assertIsNone(failed["resolved"])
        self.assertIn("selection.items[0]", failed["errors"])
        self.assertTrue(AssetReferenceVersion.objects.filter(pk=other_version["id"], asset=other).exists())
        self.assertTrue(Path(other.file.path).is_file())
        self.assertTrue(Path(main.file.path).is_file())
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (2, 2))
        self.assertEqual((SeriesProject.objects.count(), ProductionRun.objects.count(), AssetGenerationJob.objects.count()), (0, 0, 0))
        external.assert_not_called()
