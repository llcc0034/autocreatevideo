"""M9-01 relation schema only; no provider, current media, or business adoption."""
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from studio.models import (Asset, AssetGenerationJob, AssetReferenceSeries, AssetReferenceVersion,
    BibleVariant, BibleVariantRevision, GeneratedArtifact)
from studio.tests.factories import build_story
from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests import test_execution_workflow_request_media as workflow_fixtures


class AssetReferenceModelTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="reference-owner")
        self.asset = Asset.objects.create(owner=self.owner, name="独立图片", kind="reference", mime_type="image/png")
        self.primary = self.series("主图")
        self.variant = self.series("校服 / Uniform", role="variant", primary_series=self.primary)

    def series(self, name, **kwargs):
        row = AssetReferenceSeries(owner=self.owner, name=name, role=kwargs.pop("role", "primary"), **kwargs)
        row.full_clean()
        row.save()
        return row

    def version(self, series=None, **kwargs):
        row = AssetReferenceVersion(series=series or self.primary, asset=kwargs.pop("asset", self.asset),
            version=kwargs.pop("version", 1), source=kwargs.pop("source", "existing"), **kwargs)
        row.full_clean()
        row.save()
        return row

    def artifact(self, asset=None):
        return GeneratedArtifact.objects.create(manifest_key=str(uuid4()), asset=asset or self.asset,
            kind="image", role="isolated-test", status="available", mime_type="image/png")

    def test_independent_primary_two_variants_and_candidate_versions_share_assets(self):
        before = Asset.objects.count()
        other = self.series("外套", role="variant", primary_series=self.primary)
        rows = [self.version(), self.version(self.variant), self.version(self.variant, version=2), self.version(other)]
        self.assertEqual(Asset.objects.count(), before)
        self.assertEqual({row.asset_id for row in rows}, {self.asset.pk})
        self.assertEqual(self.primary.variants.count(), 2)
        self.assertEqual(list(self.variant.versions.values_list("version", flat=True)), [2, 1])
        self.assertIsNone(self.primary.bible_variant_id)
        self.assertIsNone(self.variant.bible_variant_id)
        self.assertTrue(all(row.generated_from_id is None for row in rows))
        self.assertFalse(any(field.get_internal_type() in ("FileField", "JSONField", "TextField")
            for model in (AssetReferenceSeries, AssetReferenceVersion) for field in model._meta.fields))

    def test_database_rejects_role_parent_self_parent_and_duplicate_or_zero_version(self):
        for values in ({"role": "variant"}, {"role": "primary", "primary_series": self.primary},
                {"role": "unconfigured"}):
            with self.subTest(values=values), transaction.atomic(), self.assertRaises(IntegrityError):
                AssetReferenceSeries.objects.create(owner=self.owner, name="invalid", **values)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            AssetReferenceSeries.objects.filter(pk=self.variant.pk).update(primary_series_id=self.variant.pk)
        self.version()
        for number in (0, 1):
            with self.subTest(version=number), transaction.atomic(), self.assertRaises(IntegrityError):
                AssetReferenceVersion.objects.create(series=self.primary, asset=self.asset, version=number, source="existing")

    def test_multilevel_parent_other_owner_and_foreign_asset_are_invalid(self):
        with self.assertRaises(ValidationError):
            self.series("多级", role="variant", primary_series=self.variant)
        other = get_user_model().objects.create_user(username="other-reference-owner")
        row = AssetReferenceSeries(owner=other, name="other", role="variant", primary_series=self.primary)
        with self.assertRaises(ValidationError):
            row.full_clean()
        foreign = Asset.objects.create(owner=other, name="other", kind="reference")
        with self.assertRaises(ValidationError):
            self.version(asset=foreign)

    def test_generated_variant_records_exact_primary_version_not_current_candidate(self):
        original, newer = self.version(), self.version(version=2)
        variant = self.version(self.variant, source="generated", generated_artifact=self.artifact(), generated_from=original)
        self.assertEqual(variant.generated_from_id, original.pk)
        self.assertNotEqual(variant.generated_from_id, newer.pk)
        for values in ({}, {"generated_from": self.version(self.series("另一个人物"))},
                {"generated_from": variant}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.version(self.variant, version=2, source="generated", generated_artifact=self.artifact(), **values)
        with self.assertRaises(ValidationError):
            self.version(version=3, source="generated", generated_artifact=self.artifact(), generated_from=original)
        wrong_asset = Asset.objects.create(owner=self.owner, kind="reference", name="another")
        with self.assertRaises(ValidationError):
            self.version(version=3, source="generated", generated_artifact=self.artifact(wrong_asset))

    def test_upload_reuse_do_not_imply_generation_and_relations_do_not_delete_assets(self):
        primary = self.version()
        for source in ("upload", "existing"):
            with self.subTest(source=source), transaction.atomic(), self.assertRaises(IntegrityError):
                AssetReferenceVersion.objects.create(series=self.variant, version=1,
                    asset=self.asset, source=source, generated_from=primary)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            AssetReferenceVersion.objects.create(series=self.variant, version=1, asset=self.asset, source="generated")
        uploaded = self.version(self.variant, source="upload")
        self.assertIsNone(uploaded.generated_from_id)
        with self.assertRaises(ProtectedError):
            self.asset.delete()
        self.variant.delete()  # Removing a series removes relations, not the file owner.
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        self.assertTrue(AssetReferenceVersion.objects.filter(pk=primary.pk).exists())

    def test_bible_link_is_optional_exact_and_does_not_copy_setting_text(self):
        owner, project, character, *_ = build_story("reference-bible")
        setting = BibleVariant.objects.create(project=project, character=character, name="主图", reference_name="主图",
            reference_key="primary", appearance_role="primary", created_by=owner)
        revision = BibleVariantRevision.objects.create(variant=setting, revision=1, name="主图", reference_name="主图",
            appearance_anchor="只存在设定中", content_sha256="a" * 64, created_by=owner)
        row = AssetReferenceSeries(owner=owner, name="可读名称", role="primary", bible_variant=setting)
        row.full_clean()
        row.save()
        asset = Asset.objects.create(owner=owner, name="关联图", kind="reference")
        version = self.version(row, asset=asset, setting_revision=revision)
        self.assertEqual(version.setting_revision_id, revision.pk)
        with self.assertRaises(ValidationError):
            self.version(version=1, setting_revision=revision)
        row.role, row.primary_series = "variant", self.primary
        with self.assertRaises(ValidationError):
            row.full_clean()
        revision.refresh_from_db()
        self.assertEqual(revision.appearance_anchor, "只存在设定中")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssetReferenceJobOriginTests(TestCase):
    _create_version = workflow_fixtures.ExecutionWorkflowMediaRequestTests._create_version
    setUp = workflow_fixtures.ExecutionWorkflowMediaRequestTests.setUp

    def test_independent_job_is_an_original_origin_not_a_fabricated_production_artifact(self):
        job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version_id=self.definition.value["version_id"], workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="image", status="succeeded",
            operation="image.generate", idempotency_key=str(uuid4()), request_snapshot_sha256="a" * 64,
            output_asset=self.asset)
        series = AssetReferenceSeries.objects.create(owner=self.user, name="独立模型主图", role="primary")
        row = AssetReferenceVersion(series=series, version=1, asset=self.asset, source="generated", generation_job=job)
        count = GeneratedArtifact.objects.count()
        row.full_clean()
        row.save()
        self.assertEqual(GeneratedArtifact.objects.count(), count)
        self.assertIsNone(row.generated_artifact_id)
        self.assertIsNone(row.generated_from_id)
        artifact = GeneratedArtifact.objects.create(manifest_key=str(uuid4()), asset=self.asset,
            kind="image", role="isolated-test", status="available")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            AssetReferenceVersion.objects.filter(pk=row.pk).update(generated_artifact=artifact)
        row.asset = Asset.objects.create(owner=self.user, name="not the job output", kind="reference")
        with self.assertRaises(ValidationError):
            row.full_clean()


class AssetReferenceMigrationTests(_MigrationTestCase):
    migrate_from = "0210_artifact_selection_mode"
    migrate_to = "0211_asset_reference_relations"

    def set_up_before_migration(self, apps):
        self.asset_id = apps.get_model("studio", "Asset").objects.create(name="Existing asset", kind="reference",
            checksum="unchanged", file="assets/isolated-existing.png", metadata={"fixture": True}).pk

    def test_additive_schema_does_not_infer_series_rewrite_assets_or_copy_files(self):
        asset = self.apps.get_model("studio", "Asset").objects.get(pk=self.asset_id)
        self.assertEqual((asset.checksum, str(asset.file), asset.metadata),
            ("unchanged", "assets/isolated-existing.png", {"fixture": True}))
        self.assertEqual(self.apps.get_model("studio", "AssetReferenceSeries").objects.count(), 0)
        self.assertEqual(self.apps.get_model("studio", "AssetReferenceVersion").objects.count(), 0)
