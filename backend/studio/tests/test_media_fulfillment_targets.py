"""M4 zero-call targets use real temporary Assets and the original relation API."""
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase

from studio.models import (Asset, AssetGenerationJob, AssetReferenceSeries, AssetReferenceVersion,
    BibleVariantRevision, BibleVariantReview, ExecutionPreparation, GenerationAttempt, ProductionRun)
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.media_generation_targets import resolve_media_fulfillment_target
from studio.tests import test_asset_reference_api as fixtures
from studio.tests.factories import build_story


class MediaFulfillmentTargetTests(TestCase):
    make_asset = fixtures.AssetReferenceApiTests.make_asset
    series = fixtures.AssetReferenceApiTests.series
    attach = fixtures.AssetReferenceApiTests.attach

    def setUp(self):
        fixtures.AssetReferenceApiTests.setUp(self)
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Fulfillment must not contact a model")))
        self.resolve = self.enterContext(patch("studio.services.media_generation_targets.resolve_execution_request_facts",
            side_effect=AssertionError("Fulfillment must not resolve generation inputs or seeds")))

    def target(self, *, series=None, asset=None, **changes):
        return resolve_media_fulfillment_target(
            series=AssetReferenceSeries.objects.get(pk=(series or self.primary)["id"]),
            asset_id=(asset or self.asset).pk, **{"source_mode": "existing", **changes})

    def assert_no_generation(self):
        for model in (AssetGenerationJob, ExecutionPreparation, GenerationAttempt, ProductionRun, BibleVariantReview):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.external.assert_not_called()
        self.resolve.assert_not_called()

    def setting_series(self):
        owner, project, character, *_ = build_story("fulfillment-setting")
        self.client.force_authenticate(owner)
        value = {"name": "主图", "reference_name": "主图", "identity_anchor": "identity",
            "appearance_anchor": "original appearance", "basis_asset_id": None, "basis_asset_sha256": ""}
        variant = create_variant(actor=owner, project_id=project.pk, entity_kind="character", entity_id=character.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="", value=value)
        setting = variant.revisions.get(revision=1)
        series = self.series("主图目标", bible_variant_id=str(variant.pk))
        asset = self.make_asset(owner=owner, name="existing result", kind="reference")
        return owner, variant, setting, series, asset

    def test_primary_fulfillment_contract_is_exact_and_has_no_generation_policy(self):
        target = self.target()
        value = target.value
        self.assertEqual(value["source_mode"], "existing")
        self.assertEqual(value["appearance_role"], "primary")
        self.assertEqual(value["media_type"], "image")
        self.assertEqual(value["asset"], {"asset_id": str(self.asset.pk), "sha256": self.asset.checksum})
        for key in ("execution_instance_version_id", "definition_sha256", "candidates", "inputs_sha256",
                "setting", "generated_from"):
            self.assertIsNone(value[key], key)
        self.assertEqual(target.asset.pk, self.asset.pk)
        self.assertNotIn(str(self.media_root), str(value))
        self.assertFalse(AssetReferenceVersion.objects.exists())  # Resolution is not fulfillment.
        self.assert_no_generation()

    def test_primary_and_sibling_variants_reuse_original_writer_and_do_not_require_primary_file(self):
        # A primary series with no versions is enough for organizational links.
        # Direct fulfillment must not wait for or fake a generated primary input.
        variants = [self.series(name, role="variant", primary_series_id=self.primary["id"])
            for name in ("校服", "外套")]
        original = Path(self.asset.file.path).read_bytes()
        for series, source in zip(variants, ("existing", "upload")):
            target = self.target(series=series, source_mode=source)
            self.assertEqual(target.value["appearance_role"], "variant")
            response = self.attach(series, source=source)
            self.assertEqual(response.status_code, 201, response.data)
            for key in ("generated_from_id", "generation_job", "generated_artifact"):
                self.assertIsNone(response.data[key])
            self.assertEqual(self.attach(series, source=source).data["id"], response.data["id"])
        self.assertFalse(AssetReferenceVersion.objects.filter(series_id=self.primary["id"]).exists())
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (1, 2))
        self.assertEqual(Path(self.asset.file.path).read_bytes(), original)
        self.assertEqual(len(list(self.media_root.rglob("*.png"))), 1)
        self.assert_no_generation()

    def test_explicit_old_setting_is_not_replaced_by_current_revision(self):
        owner, variant, setting, series, asset = self.setting_series()
        revise_variant(actor=owner, variant=variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "new appearance"})
        target = self.target(series=series, asset=asset, setting_revision_id=setting.pk)
        self.assertEqual(target.value["setting"], {"appearance_id": str(variant.pk),
            "setting_revision_id": str(setting.pk), "revision": 1, "content_sha256": setting.content_sha256})
        response = self.attach(series, asset, setting_revision_id=str(setting.pk))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["setting_revision_id"], str(setting.pk))
        self.assertNotIn("appearance_anchor", str(target.value) + str(response.data))
        self.assertEqual(variant.revisions.count(), 2)
        self.assert_no_generation()

    def test_omitted_setting_stays_absent_even_for_bound_series(self):
        _, _, _, series, asset = self.setting_series()
        self.assertIsNone(self.target(series=series, asset=asset).value["setting"])
        response = self.attach(series, asset)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["setting_revision_id"])
        self.assert_no_generation()

    def test_variant_setting_keeps_exact_primary_setting_dependency_without_generated_basis(self):
        owner, primary, primary_setting, primary_series, asset = self.setting_series()
        variant = create_variant(actor=owner, project_id=primary.project_id, entity_kind="character",
            entity_id=primary.character_id, appearance_role="variant", primary_setting_revision_id=primary_setting.pk,
            primary_setting_sha256=primary_setting.content_sha256, value={"name": "校服", "reference_name": "校服",
                "identity_anchor": "", "appearance_anchor": "uniform", "basis_asset_id": None, "basis_asset_sha256": ""})
        setting = variant.revisions.get(revision=1)
        series = self.series("校服目标", role="variant", primary_series_id=primary_series["id"], bible_variant_id=str(variant.pk))
        target = self.target(series=series, asset=asset, setting_revision_id=setting.pk)
        self.assertEqual(target.value["setting"]["setting_revision_id"], str(setting.pk))
        self.assertEqual(target.setting.primary_setting_revision_id, primary_setting.pk)
        self.assertIsNone(target.value["generated_from"])
        response = self.attach(series, asset, setting_revision_id=str(setting.pk))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["generated_from_id"])
        self.assert_no_generation()

    def test_corrupt_or_retired_setting_contract_is_explicit_and_does_not_write_relation(self):
        _, _, setting, series, asset = self.setting_series()
        # Ordinary writes are immutable. SQL only injects corruption into this
        # disposable test DB, exercising the reader rather than weakening it.
        with self.assertRaises(ValidationError):
            BibleVariantRevision.objects.filter(pk=setting.pk).update(appearance_anchor="tampered")
        for anchor, schema in (("tampered", 2), (setting.appearance_anchor, 1)):
            with self.subTest(anchor=anchor, schema=schema):
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s, "
                        "payload_schema_version=%s WHERE id=%s", [anchor, schema, setting.pk.hex])
                response = self.attach(series, asset, setting_revision_id=str(setting.pk))
                self.assertEqual(response.status_code, 409, response.data)
                self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertTrue(Path(asset.file.path).is_file())
        self.assert_no_generation()

    def test_actual_mime_mismatch_rejects_even_when_kind_and_checksum_match(self):
        self.asset.mime_type = "image/jpeg"  # Actual temporary file is PNG.
        self.asset.save(update_fields=["mime_type"])
        response = self.attach()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("MIME", str(response.data))
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assert_no_generation()

    def test_upload_provenance_cannot_be_asserted_for_an_existing_non_upload(self):
        self.asset.source_info = "existing generated file"
        self.asset.save(update_fields=["source_info"])
        response = self.attach(source="upload")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("source", response.data["error"]["details"])
        self.assertEqual(self.attach(source="existing").status_code, 201)
        self.assert_no_generation()

    def test_generation_source_is_rejected_before_reading_media_or_drawing_seed(self):
        with patch("studio.services.media_generation_targets.resolve_execution_asset_media",
                side_effect=AssertionError("Reject wrong mode first")):
            for mode in ("generate", "generated", None, True):
                with self.subTest(mode=mode), self.assertRaises(ValidationError):
                    self.target(source_mode=mode)
        self.assert_no_generation()

    def test_relation_write_failure_keeps_original_file_and_does_not_consume_version(self):
        with patch("studio.services.asset_references._save", side_effect=RuntimeError("isolated write failure")):
            with self.assertRaisesMessage(RuntimeError, "isolated write failure"):
                attach_asset(actor=self.owner, series_id=self.primary["id"], asset_id=self.asset.pk, source="existing")
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertTrue(Path(self.asset.file.path).is_file())
        response = self.attach()
        self.assertEqual((response.status_code, response.data["version"]), (201, 1))
        self.assert_no_generation()

    def test_replay_rechecks_changed_file_without_replacing_original_relation(self):
        first = self.attach()
        self.assertEqual(first.status_code, 201, first.data)
        Path(self.asset.file.path).write_bytes(b"changed bytes")
        response = self.attach()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(list(AssetReferenceVersion.objects.values_list("pk", flat=True)),
            [AssetReferenceVersion.objects.get(pk=first.data["id"]).pk])
        self.assert_no_generation()
