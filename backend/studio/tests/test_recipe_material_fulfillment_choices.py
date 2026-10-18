"""Explicit per-run reuse choices with real isolated review/media, no dispatch."""
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from studio.models import AssetReferenceVersion, ProductionMaterialFulfillment, StageRun
from studio.services.asset_references import removal_impact
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_material_fulfillment import (
    read_recipe_material_fulfillment, select_recipe_material_asset,
)
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.services.production_runs import _canonical_bible_image_candidates, _formal_snapshot
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_fulfillment as fixtures
from studio.tests.test_asset_management_api import image_upload


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialFulfillmentChoiceTests(StructureFixture):
    # Share setup, not test inheritance/counts. The generation resolver and
    # external transport remain forbidden by this original fixture's teardown.
    setUp = fixtures.RecipeMaterialAssociationTests.setUp
    tearDown = fixtures.RecipeMaterialAssociationTests.tearDown
    facts = fixtures.RecipeMaterialAssociationTests.facts
    attach = fixtures.RecipeMaterialAssociationTests.attach
    create_job = fixtures.RecipeMaterialAssociationTests.create_job
    primary_version = fixtures.RecipeMaterialAssociationTests.primary_version
    write = fixtures.RecipeMaterialAssociationTests.write
    input = staticmethod(fixtures.RecipeMaterialAssociationTests.input)
    configure_variant_sources = fixtures.RecipeMaterialAssociationTests.configure_variant_sources
    configure_declaration = fixtures.RecipeMaterialAssociationTests.configure_declaration
    target = fixtures.RecipeMaterialAssociationTests.target
    prepare = fixtures.RecipeMaterialAssociationTests.prepare

    def choose(self, *, setting=None, series=None, **changes):
        setting = setting or self.settings[0]
        return select_recipe_material_asset(**{"stage_run_id": self.stage.pk,
            "expected_run_plan_sha256": self.plan.sha256, "unit_key": f"appearance:{setting.variant_id}",
            "requirement_key": f"setting:{setting.pk}", "series_id": (series or self.series[0]).pk,
            "asset_id": self.assets[0].pk, "source": "existing", "actor": self.user, **changes})

    def read(self, *, setting=None, **changes):
        return read_recipe_material_fulfillment(stage=self.stage,
            unit_key=f"appearance:{(setting or self.settings[0]).variant_id}", **changes)

    def test_library_association_is_not_a_run_choice_and_reads_never_backfill(self):
        self.assertIsNone(self.read())
        version, _ = self.attach()
        self.assertIsNone(self.read())
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(self.series[0].versions.get().pk, version.pk)
        self.assertEqual(self.facts(), self.before)

    def test_primary_and_two_siblings_choose_exact_versions_without_synthetic_facts(self):
        files = {str(path): path.read_bytes() for path in self.root.rglob("*.png")}
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            choice, created = self.choose(setting=setting, series=series)
            self.assertTrue(created)
            self.assertEqual(choice.selected_by, self.user)
            self.assertEqual(self.choose(setting=setting, series=series), (choice, False))
            result = self.read(setting=setting)
            self.assertEqual(result["fulfillment"].pk, choice.pk)
            self.assertEqual(result["version"].pk, choice.reference_version_id)
            self.assertEqual(result["version"].setting_revision_id, setting.pk)
            self.assertIsNone(result["version"].generated_from_id)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 3)
        self.assertEqual(self.facts(), self.before)
        self.assertEqual(files, {str(path): path.read_bytes() for path in self.root.rglob("*.png")})

    def test_upload_choice_reuses_formal_asset_without_copying(self):
        response = self.client.post("/api/v1/assets/", {"name": "选用上传图", "kind": "reference", "file": image_upload()},
            format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        choice, _ = self.choose(asset_id=response.data["id"], source="upload")
        self.assertEqual(str(choice.reference_version.asset_id), response.data["id"])
        self.assertEqual(choice.reference_version.source, "upload")
        self.assertEqual(self.read()["fulfillment"].pk, choice.pk)
        self.assertEqual(self.choose(asset_id=response.data["id"], source="upload"), (choice, False))
        self.assertEqual(self.facts(), self.before)

    def test_all_reused_units_complete_dispatch_without_routes_or_generation_facts(self):
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            self.choose(setting=setting, series=series)
        # Reusing an image does not require choosing a model route or allowing
        # external calls. The stored recipe and M3 requirement stay unchanged.
        self.stage.selected_variant_route_index = None
        self.stage.save(update_fields=["selected_variant_route_index"])
        self.run.allow_external_calls = False
        self.run.save(update_fields=["allow_external_calls"])
        before = self.facts()
        with patch("studio.services.production_recipe_media_stages.require_recipe_stage_route",
                side_effect=AssertionError("No model route for a reused result")), patch(
                "studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("No generation preparation")):
            for _ in range(2):
                self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "material-fulfillments:3")
        self.assertEqual(self.facts(), before)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 3)
        query = ProductionMaterialFulfillment.objects.filter(stage_run=self.stage).values_list("unit_key", flat=True)
        self.assertIn("INDEX", query.explain().upper())
        self.assertNotIn("SCAN", query.explain().upper())

    def test_all_uploaded_units_complete_dispatch_with_original_upload_facts(self):
        response = self.client.post("/api/v1/assets/", {"name": "isolated completed upload",
            "kind": "reference", "file": image_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            self.choose(setting=setting, series=series, asset_id=response.data["id"], source="upload")
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No copied media")):
            self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "material-fulfillments:3")
        self.assertEqual(self.facts(), self.before)
        self.assertEqual(set(ProductionMaterialFulfillment.objects.values_list("reference_version__source", flat=True)),
            {"upload"})

    def test_invalid_later_reused_unit_blocks_before_any_earlier_generation_preparation(self):
        self.choose(setting=self.settings[1], series=self.series[1])
        Path(self.assets[0].file.path).write_bytes(b"invalid isolated selected image")
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("Validate every chosen image before preparing unfulfilled units")):
            with self.assertRaises(ValidationError):
                advance_recipe_media_stage(self.run, self.stage)
        self.assertEqual(self.facts(), self.before)

    def test_formal_manifest_freezes_exact_reuse_references_without_candidates(self):
        choices = [self.choose(setting=setting, series=series)[0]
            for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]]
        result = advance_recipe_media_stage(self.run, self.stage)
        snapshot = _formal_snapshot(self.run, self.stage, result)
        self.assertEqual(snapshot["output_reference"], "material-fulfillments:3")
        canonical = snapshot["canonical"]
        self.assertEqual(canonical, _canonical_bible_image_candidates(self.run, self.stage))
        self.assertEqual(canonical["material_fulfillments"], [{"fulfillment_id": str(row.pk),
            "unit_key": row.unit_key, "requirement_key": row.requirement_key,
            "reference_version_id": str(row.reference_version_id), "target_sha256": row.target_sha256,
            "source": "existing"} for row in sorted(choices, key=lambda row: row.unit_key)])
        self.assertTrue(all(not row["candidates"] for row in canonical["entities"]))
        self.assertEqual(self.facts(), self.before)
        # A later read cannot certify the saved manifest against changed bytes.
        Path(self.assets[0].file.path).write_bytes(b"corrupt isolated chosen image")
        with self.assertRaises(ValidationError):
            _canonical_bible_image_candidates(self.run, self.stage)

    def test_library_only_relation_does_not_appear_as_a_run_fulfillment(self):
        self.attach()
        canonical = _canonical_bible_image_candidates(self.run, self.stage)
        self.assertNotIn("material_fulfillments", canonical)
        self.assertEqual(self.facts(), self.before)

    def test_out_of_scope_choice_is_not_ignored_or_dispatched(self):
        choice, _ = self.choose()
        corrupted = ProductionMaterialFulfillment(**{field: getattr(choice, field) for field in choice._SNAPSHOT_FIELDS})
        corrupted.unit_key = f"appearance:{uuid4()}"
        # Deliberately bypass the immutable writer to test stored corruption.
        ProductionMaterialFulfillment.objects.bulk_create([corrupted])
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("Out-of-scope selection must fail before dispatch")):
            with self.assertRaises(ValidationError):
                advance_recipe_media_stage(self.run, self.stage)
        self.assertEqual(self.facts(), self.before)

    def test_conflicting_replay_does_not_associate_another_version_or_probe_media(self):
        choice, _ = self.choose()
        before = list(AssetReferenceVersion.objects.order_by("pk").values())
        with patch("studio.services.media_generation_targets.resolve_execution_asset_media",
                side_effect=AssertionError("Conflicting choice must not read files")):
            for changes in ({"asset_id": self.assets[1].pk}, {"source": "upload"}, {"series": self.series[1]},
                    {"expected_run_plan_sha256": "0" * 64}, {"requirement_key": f"setting:{self.settings[1].pk}"}):
                with self.subTest(changes=changes), self.assertRaisesMessage(ValidationError, "已有不同"):
                    self.choose(**changes)
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), before)
        self.assertEqual(ProductionMaterialFulfillment.objects.get().pk, choice.pk)

    def test_selection_failure_rolls_back_new_association_and_receipt_together(self):
        original = ProductionMaterialFulfillment.save
        def fail_after_write(row, *args, **kwargs):
            original(row, *args, **kwargs)
            raise RuntimeError("isolated choice commit failure")
        with patch.object(ProductionMaterialFulfillment, "save", fail_after_write):
            with self.assertRaisesMessage(RuntimeError, "choice commit failure"):
                self.choose()
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertFalse(self.series[0].versions.exists())
        row, _ = self.choose()
        self.assertEqual(row.reference_version.version, 1)
        self.assertEqual(self.facts(), self.before)

    def test_replay_rechecks_owner_stage_and_review_but_historical_read_does_not_reapprove(self):
        choice, _ = self.choose()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.choose()
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        StageRun.objects.filter(pk=self.stage.pk).update(status="succeeded")
        with self.assertRaises(ValidationError):
            self.choose()
        StageRun.objects.filter(pk=self.stage.pk).update(status="running")
        revise_variant(actor=self.user, variant=self.settings[0].variant, expected_revision=1,
            value={**setting_payload(self.settings[0]), "appearance_anchor": "A new unreviewed requirement"})
        with self.assertRaises(ValidationError):
            self.choose()
        with self.assertRaises(ValidationError):
            self.read()
        self.assertEqual(self.read(require_current=False)["fulfillment"].pk, choice.pk)
        self.assertEqual(self.facts(), self.before)

    def test_chosen_file_change_is_not_a_satisfied_requirement(self):
        self.choose()
        Path(self.assets[0].file.path).write_bytes(b"changed isolated media")
        with self.assertRaises(ValidationError):
            self.read()
        self.assertEqual(self.facts(), self.before)

    def test_relation_identity_change_cannot_silently_change_the_chosen_asset(self):
        choice, _ = self.choose()
        AssetReferenceVersion.objects.filter(pk=choice.reference_version_id).update(asset=self.assets[1])
        with self.assertRaisesMessage(ValidationError, "选择时不一致"):
            self.read()

    def test_choice_is_immutable_and_unique_with_indexed_lookup(self):
        row, _ = self.choose()
        with self.assertRaises(ValidationError):
            ProductionMaterialFulfillment.objects.filter(pk=row.pk).update(target_sha256="0" * 64)
        row.target_sha256 = "0" * 64
        with self.assertRaises(ValidationError):
            row.save()
        row.refresh_from_db()
        duplicate = ProductionMaterialFulfillment(**{field: getattr(row, field) for field in row._SNAPSHOT_FIELDS})
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductionMaterialFulfillment.objects.bulk_create([duplicate])
        plan = ProductionMaterialFulfillment.objects.filter(stage_run=self.stage, unit_key=row.unit_key).explain()
        self.assertIn("INDEX", plan.upper())
        self.assertNotIn("SCAN", plan.upper())

    def test_chosen_version_and_series_removal_are_protected_and_preview_is_truthful(self):
        row, _ = self.choose()
        version = row.reference_version
        for kind, target in (("versions", version), ("series", version.series)):
            self.assertEqual(removal_impact(target)["production_use_count"], 1)
            response = self.client.get(f"/api/v1/asset-reference-{kind}/{target.pk}/removal-impact/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["production_use_count"], 1)
            self.assertFalse(response.data["can_remove"])
            response = self.client.delete(f"/api/v1/asset-reference-{kind}/{target.pk}/")
            self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.read()["version"].pk, version.pk)
        self.assertEqual(self.facts(), self.before)

    def test_selected_unit_cannot_create_generation_preparation(self):
        self.choose()
        with self.assertRaisesMessage(ValidationError, "已明确选用成图满足；不能再创建生成准备或重发原失败调用"):
            self.prepare()
        self.assertEqual(self.facts(), self.before)

    def test_shared_preparation_writer_also_rejects_bypassing_recipe_adapter(self):
        from studio.services.execution_preparations import create_execution_instance_preparation
        row, _ = self.choose()
        with self.assertRaisesMessage(ValidationError, "已明确选用成图满足；不能再创建生成准备或重发原失败调用"):
            create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
                version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
                unit_key=row.unit_key, request_key="route:1:candidate:1", intent=json_contract({}), actor=self.user,
                resolve_facts=lambda *_: self.fail("Reused results must not resolve generation inputs"))
        self.assertEqual(self.facts(), self.before)

    def test_prepared_variant_blocks_same_unit_selection_and_new_primary_source(self):
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts", resolve_execution_request_facts):
            self.prepare()
        before = self.facts()
        with self.assertRaisesMessage(ValidationError, "已有生成准备"):
            self.choose()
        with self.assertRaisesMessage(ValidationError, "已有变体准备"):
            self.choose(setting=self.primary_setting, series=self.primary.series)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertFalse(self.series[0].versions.exists())
        self.assertEqual(self.facts(), before)

    def test_choice_failure_does_not_remove_preexisting_library_relation(self):
        version, _ = self.attach()
        with patch.object(ProductionMaterialFulfillment, "save", side_effect=RuntimeError("choice unavailable")):
            with self.assertRaises(RuntimeError):
                self.choose()
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(self.series[0].versions.get().pk, version.pk)
        self.assertEqual(self.facts(), self.before)
