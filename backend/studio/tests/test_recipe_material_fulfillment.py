"""Exact M3 requirement -> shared library relation, not a completed Run.

The original review and upload APIs use an isolated DB and temporary PNGs.
No model transport, generated candidate or synthetic approval is allowed.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, Asset, AssetGenerationJob,
    AssetReferenceVersion, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ProductionRun, StageRun)
from studio.services.asset_references import attach_asset, create_series
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_recipe_material_fulfillment import attach_recipe_material_asset
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_variant_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialAssociationTests(StructureFixture):
    create_job = fixtures.RecipeVariantPreparationTests.create_job
    primary_version = fixtures.RecipeVariantPreparationTests.primary_version
    write = fixtures.RecipeVariantPreparationTests.write
    input = staticmethod(fixtures.RecipeVariantPreparationTests.input)
    configure_variant_sources = fixtures.RecipeVariantPreparationTests.configure_variant_sources
    configure_declaration = fixtures.RecipeVariantPreparationTests.configure_declaration
    target = fixtures.RecipeVariantPreparationTests.target
    prepare = fixtures.RecipeVariantPreparationTests.prepare

    def setUp(self):
        fixtures.RecipeVariantPreparationTests.setUp(self)
        self.series = [create_series(actor=self.user, values={"name": setting.name, "role": "variant",
            "primary_series_id": str(self.primary.series_id), "bible_variant_id": str(setting.variant_id)})
            for setting in self.settings]
        for asset in self.assets:
            asset.mime_type = "image/png"
            asset.save(update_fields=["mime_type"])
        self.resolve = self.enterContext(patch("studio.services.media_generation_targets.resolve_execution_request_facts",
            side_effect=AssertionError("No generation inputs or seed for an existing result")))
        self.before = self.facts()

    def facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in
            (ApprovalDecision, ArtifactSelectionDecision, GeneratedArtifact, GenerationAttempt,
                AssetGenerationJob, ExecutionPreparation, ProductionRun, StageRun)}

    def attach(self, *, setting=None, series=None, **changes):
        setting = setting or self.settings[0]
        return attach_recipe_material_asset(**{"stage_run_id": self.stage.pk,
            "expected_run_plan_sha256": self.plan.sha256, "unit_key": f"appearance:{setting.variant_id}",
            "requirement_key": f"setting:{setting.pk}", "series_id": (series or self.series[0]).pk,
            "asset_id": self.assets[0].pk, "source": "existing", "actor": self.user, **changes})

    def tearDown(self):
        self.external.assert_not_called()
        self.resolve.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_primary_and_siblings_reuse_same_writer_without_new_execution_or_review(self):
        before_assets = list(Asset.objects.order_by("pk").values())
        files = {str(path): path.read_bytes() for path in self.root.rglob("*.png")}
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            with self.subTest(role=series.role, setting=setting.pk):
                version, created = self.attach(setting=setting, series=series)
                self.assertTrue(created)
                replay, created = attach_asset(actor=self.user, series_id=series.pk, asset_id=self.assets[0].pk,
                    source="existing", setting_revision_id=setting.pk)
                self.assertFalse(created)
                self.assertEqual(replay.pk, version.pk)
                self.assertEqual(version.setting_revision_id, setting.pk)
                self.assertIsNone(version.generated_from_id)
                self.assertIsNone(version.generated_artifact_id)
                self.assertIsNone(version.generation_job_id)
                self.assertEqual(self.attach(setting=setting, series=series), (version, False))
        self.assertEqual(self.facts(), self.before)
        self.assertEqual(list(Asset.objects.order_by("pk").values()), before_assets)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*.png")}, files)

    def test_formal_upload_is_exact_asset_and_cannot_be_faked_by_source_label(self):
        response = self.client.post("/api/v1/assets/", {"name": "本地成图", "kind": "reference", "file": image_upload()},
            format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        asset = Asset.objects.get(pk=response.data["id"])
        version, _ = self.attach(asset_id=asset.pk, source="upload")
        self.assertEqual((version.asset_id, version.source, version.generated_from_id), (asset.pk, "upload", None))
        self.assertEqual(self.attach(asset_id=asset.pk, source="upload"), (version, False))
        with self.assertRaises(APIValidationError):
            self.attach(source="upload")
        self.assertEqual(self.facts(), self.before)

    def test_wrong_target_plan_or_series_is_rejected_before_media_read(self):
        cases = [{"unit_key": f"appearance:{uuid4()}"}, {"requirement_key": f"setting:{uuid4()}"},
            {"expected_run_plan_sha256": "0" * 64}, {"expected_run_plan_sha256": None},
            {"series": self.series[1]}, {"series": self.primary.series}]
        before = AssetReferenceVersion.objects.count()
        with patch("studio.services.media_generation_targets.resolve_execution_asset_media",
                side_effect=AssertionError("Wrong target must not probe media")):
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ValidationError):
                    self.attach(**changes)
        self.assertEqual(AssetReferenceVersion.objects.count(), before)
        self.assertEqual(self.facts(), self.before)

    def test_explicit_parent_series_needs_exact_appearance_not_a_generated_primary_file(self):
        parent = create_series(actor=self.user, values={"name": "尚无图片的主系列", "role": "primary",
            "bible_variant_id": str(self.primary_setting.variant_id)})
        self.series[0].primary_series = parent
        self.series[0].save(update_fields=["primary_series"])
        self.assertFalse(parent.versions.exists())
        version, _ = self.attach()
        self.assertIsNone(version.generated_from_id)
        self.assertFalse(parent.versions.exists())
        self.assertEqual(self.facts(), self.before)

    def test_unbound_parent_and_staff_cross_owner_series_cannot_be_inferred(self):
        unrelated = create_series(actor=self.user, values={"name": "不是本需求主图", "role": "primary"})
        self.series[0].primary_series = unrelated
        self.series[0].save(update_fields=["primary_series"])
        with self.assertRaisesMessage(ValidationError, "主图外观系列"):
            self.attach()
        staff = get_user_model().objects.create_user(username="cross-owner-staff", is_staff=True)
        foreign = create_series(actor=staff, values={"name": "他人素材系列", "role": "primary"})
        with self.assertRaisesMessage(ValidationError, "所有者及确切外观"):
            self.attach(setting=self.primary_setting, series=foreign, actor=staff)
        self.assertFalse(self.series[0].versions.exists())
        self.assertEqual(self.facts(), self.before)

    def test_unreviewed_setting_change_cannot_reuse_old_requirement_even_on_replay(self):
        version, _ = self.attach()
        revise_variant(actor=self.user, variant=self.settings[0].variant, expected_revision=1,
            value={**setting_payload(self.settings[0]), "appearance_anchor": "New unreviewed difference"})
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            self.attach()
        version.refresh_from_db()
        self.assertEqual(version.setting_revision_id, self.settings[0].pk)
        self.assertEqual(self.series[0].versions.count(), 1)
        self.assertEqual(self.facts(), self.before)

    def test_wrong_media_type_and_actual_mime_or_changed_bytes_do_not_write_versions(self):
        asset = self.assets[0]
        for mime in ("video/mp4", "audio/wav", "image/jpeg"):
            asset.mime_type = mime
            asset.save(update_fields=["mime_type"])
            with self.subTest(mime=mime), self.assertRaises(APIValidationError):
                self.attach()
        asset.mime_type = "image/png"
        asset.save(update_fields=["mime_type"])
        path = Path(asset.file.path)
        path.write_bytes(b"corrupt isolated file")
        with self.assertRaises(APIValidationError):
            self.attach()
        self.assertFalse(self.series[0].versions.exists())
        self.assertEqual(self.facts(), self.before)

    def test_relation_failure_rolls_back_and_replay_does_not_consume_version(self):
        from studio.services.asset_references import _save
        def fail_after_write(row):
            _save(row)
            raise RuntimeError("isolated relation write failure")
        with patch("studio.services.asset_references._save", side_effect=fail_after_write):
            with self.assertRaisesMessage(RuntimeError, "isolated relation write failure"):
                self.attach()
        self.assertFalse(self.series[0].versions.exists())
        version, created = self.attach()
        self.assertTrue(created)
        self.assertEqual(version.version, 1)
        self.assertEqual(self.facts(), self.before)

    def test_owner_and_inactive_or_finished_stage_cannot_write(self):
        other = get_user_model().objects.create_user(username="not-the-run-owner")
        with self.assertRaises(PermissionDenied):
            self.attach(actor=other)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        staff = get_user_model().objects.create_user(username="fulfillment-staff", is_staff=True)
        with self.assertRaisesMessage(PermissionDenied, "所有者已停用"):
            self.attach(actor=staff)
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        for status in ("pending", "cancelled", "succeeded"):
            StageRun.objects.filter(pk=self.stage.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.attach()
        self.assertFalse(self.series[0].versions.exists())

    def test_prepared_unit_cannot_be_replaced_by_zero_call_association(self):
        # Preparing this fixture is intentional; the adapter itself must never
        # resolve generation. Restore the real resolver only around setup.
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts", resolve_execution_request_facts):
            preparation = self.prepare()
        before = deepcopy(preparation.contract.resolved_inputs)
        with self.assertRaisesMessage(ValidationError, "已有生成准备"):
            self.attach()
        self.assertFalse(self.series[0].versions.exists())
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, before)
