"""Original library -> frozen recipe -> zero-call fulfillment, isolated only."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceVersion, ProductionMaterialFulfillment
from studio.services.asset_references import attach_asset
from studio.services.production_recipe_contracts import recipe_stage_declarations
from studio.services.production_recipe_material_sources import material_source_version_candidates
from studio.services.production_recipe_media_stages import recipe_media_input_contract
from studio.services.production_runs import _freeze_stage_input
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_sources as fixtures


class AutomaticMaterialPolicyTests(SimpleTestCase):
    def test_policy_contract_rejects_generated_unknown_or_unscoped_selection(self):
        stage = {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto",
            "material_sources": {"primary": "existing", "variant": "upload"},
            "material_selection": {"primary": "unique_version", "variant": "manual"}}
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": [stage]}
        self.assertEqual(recipe_stage_declarations(definition), [stage])
        for value in (None, {}, [], {"primary": "latest"}, {"primary": []}, {"wrong": "manual"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                recipe_stage_declarations({**definition, "stages": [{**stage, "material_selection": value}]})
        for sources in ({"primary": "generate", "variant": "upload"}, None):
            bad = deepcopy(stage)
            if sources is None:
                bad.pop("material_sources")
            else:
                bad["material_sources"] = sources
            with self.assertRaises(ValidationError):
                recipe_stage_declarations({**definition, "stages": [bad]})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class AutomaticMaterialFulfillmentTests(StructureFixture):
    tearDown = fixtures.MaterialSourceStageTests.tearDown
    facts = fixtures.MaterialSourceStageTests.facts
    create_job = fixtures.MaterialSourceStageTests.create_job
    primary_version = fixtures.MaterialSourceStageTests.primary_version
    write = fixtures.MaterialSourceStageTests.write
    input = staticmethod(fixtures.MaterialSourceStageTests.input)
    configure_variant_sources = fixtures.MaterialSourceStageTests.configure_variant_sources
    target = fixtures.MaterialSourceStageTests.target
    prepare = fixtures.MaterialSourceStageTests.prepare
    payload = fixtures.MaterialSourceStageTests.payload
    inspect = fixtures.MaterialSourceStageTests.inspect
    select = fixtures.MaterialSourceStageTests.select
    advance = fixtures.MaterialSourceStageTests.advance

    def configure_declaration(self, declaration):
        declaration["material_sources"] = {"primary": "existing", "variant": "existing"}
        declaration["material_selection"] = {"primary": "unique_version", "variant": "unique_version"}
        declaration.pop("routing")
        declaration.pop("variant_routing")

    def setUp(self):
        fixtures.MaterialSourceStageTests.setUp(self)
        # Arrange the isolated stage immediately before entry, with no execution
        # or fulfillment. Each test freezes once after arranging its library.
        self.stage.input_snapshot = {}
        self.stage.input_snapshot_sha256 = ""
        self.stage.save(update_fields=("input_snapshot", "input_snapshot_sha256"))

    def associate(self, setting=None, series=None, asset=None):
        return attach_asset(actor=self.user, series_id=(series or self.series[0]).pk,
            asset_id=(asset or self.assets[0]).pk, source="existing",
            setting_revision_id=(setting or self.settings[0]).pk, expected_media_type="image")[0]

    def begin(self):
        _freeze_stage_input(self.run, self.stage)
        self.stage.refresh_from_db()
        self.before = self.facts()
        return deepcopy(self.stage.input_snapshot)

    def test_three_unique_versions_complete_zero_call_and_replay_preserves_automatic_identity(self):
        versions = [self.primary, *[self.associate(setting, series) for setting, series in zip(self.settings, self.series)]]
        saved = self.begin()
        originals = list(AssetReferenceVersion.objects.order_by("pk").values())
        files = {str(path): path.read_bytes() for path in self.root.rglob("*.png")}
        with patch("studio.services.production_recipe_material_sources.material_source_version_candidates",
                side_effect=AssertionError("No library rediscovery after freezing")):
            self.assertEqual(recipe_media_input_contract(self.stage).value, saved)
            for _ in range(2):
                self.assertEqual(self.advance(), "material-fulfillments:3")
        rows = list(ProductionMaterialFulfillment.objects.all())
        self.assertEqual({row.reference_version_id for row in rows}, {row.pk for row in versions})
        self.assertTrue(all(row.selection_mode == "automatic" and row.selected_by_id is None for row in rows))
        response = self.select(self.payload(self.primary_setting, self.primary.series), asset_id=str(self.primary.asset_id))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["selection_mode"], "automatic")
        self.assertIsNone(response.data["selected_by_id"])
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), originals)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*.png")}, files)
        self.assertEqual(self.facts(), self.before)

    def test_missing_snapshot_stays_missing_after_new_version_and_manual_takeover_resumes(self):
        saved = self.begin()
        self.associate()
        self.assertEqual(recipe_media_input_contract(self.stage).value, saved)
        self.assertIsNone(self.advance())
        self.assertIn("无确切版本", self.stage.reason)
        self.assertEqual(ProductionMaterialFulfillment.objects.get().reference_version_id, self.primary.pk)
        for setting, series in zip(self.settings, self.series):
            response = self.select(self.payload(setting, series))
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["selection_mode"], "manual")
        self.assertEqual(self.advance(), "material-fulfillments:3")
        self.assertTrue(all(row.selected_by_id == self.user.pk for row in
            ProductionMaterialFulfillment.objects.filter(reference_version__series__role="variant")))
        self.assertIsNone(ProductionMaterialFulfillment.objects.get(reference_version__series__role="primary").selected_by_id)
        self.assertEqual(self.facts(), self.before)

    def test_multiple_versions_freeze_ambiguity_without_picking_first_or_latest(self):
        self.associate()
        self.associate(asset=self.assets[1])
        saved = self.begin()
        key = self.payload()["unit_key"]
        self.assertEqual(saved["material_source_versions"][key], {"status": "ambiguous", "version_id": None})
        self.assertIsNone(self.advance())
        self.assertIn("多个确切版本", self.stage.reason)
        self.assertEqual(ProductionMaterialFulfillment.objects.get().reference_version_id, self.primary.pk)
        self.assertEqual(self.select().status_code, 201)
        self.assertIsNone(self.advance())
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 2)

    def test_broken_frozen_file_waits_for_manual_and_does_not_choose_new_version(self):
        version = self.associate()
        self.begin()
        Path(version.asset.file.path).write_bytes(b"isolated corrupt PNG")
        self.associate(asset=self.assets[1])
        self.assertIsNone(self.advance())
        self.assertIn("冻结版本或文件不可用", self.stage.reason)
        self.assertEqual(ProductionMaterialFulfillment.objects.get().reference_version_id, self.primary.pk)
        response = self.select(asset_id=str(self.assets[1].pk))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.advance())
        self.assertEqual(ProductionMaterialFulfillment.objects.get(reference_version__series__role="variant").selected_by_id, self.user.pk)

    def test_candidate_lookup_is_indexed_read_only_bounded_and_scope_exact(self):
        self.associate()
        target = next(row for row in recipe_media_input_contract(self.stage).value["material_requirements"]["targets"]
            if row["setting"]["setting_revision_id"] == str(self.settings[0].pk))
        query = material_source_version_candidates(self.stage, target, "existing")
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(len(list(query)), 1)
        self.assertEqual(len(captured), 1)
        self.assertIn("LIMIT 2", captured[0]["sql"])
        self.assertIn("INDEX", query.explain().upper())
        self.assertEqual(list(material_source_version_candidates(self.stage, target, "upload")), [])
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_missing_frozen_identities_are_not_backfilled_from_current_library(self):
        self.associate()
        self.begin()
        from studio.services.production_runs import _save_stage_snapshot
        broken = deepcopy(self.stage.input_snapshot)
        broken.pop("material_source_versions")
        _save_stage_snapshot(self.stage, "input_snapshot", broken)
        with self.assertRaisesMessage(ValidationError, "不能重新搜索"):
            self.advance()
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_existing_policy_reuses_uploaded_origins_without_relabel(self):
        AutomaticUploadedMaterialTests.test_formal_uploaded_versions_complete_without_new_files_versions_or_human_approval(self)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class AutomaticUploadedMaterialTests(StructureFixture):
    setUp = AutomaticMaterialFulfillmentTests.setUp
    tearDown = fixtures.MaterialSourceStageTests.tearDown
    facts = fixtures.MaterialSourceStageTests.facts
    create_job = fixtures.MaterialSourceStageTests.create_job
    primary_version = fixtures.MaterialSourceStageTests.primary_version
    write = fixtures.MaterialSourceStageTests.write
    input = staticmethod(fixtures.MaterialSourceStageTests.input)
    configure_variant_sources = fixtures.MaterialSourceStageTests.configure_variant_sources
    target = fixtures.MaterialSourceStageTests.target
    prepare = fixtures.MaterialSourceStageTests.prepare
    advance = fixtures.MaterialSourceStageTests.advance
    begin = AutomaticMaterialFulfillmentTests.begin

    def configure_declaration(self, declaration):
        AutomaticMaterialFulfillmentTests.configure_declaration(self, declaration)
        declaration["material_sources"]["variant"] = "upload"

    def test_formal_uploaded_versions_complete_without_new_files_versions_or_human_approval(self):
        from studio.tests.test_asset_management_api import image_upload
        response = self.client.post("/api/v1/assets/", {"name": "isolated automatic uploaded result",
            "kind": "reference", "file": image_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        for setting, series in zip(self.settings, self.series):
            attach_asset(actor=self.user, series_id=series.pk, asset_id=response.data["id"], source="upload",
                setting_revision_id=setting.pk, expected_media_type="image")
        self.begin()
        versions = list(AssetReferenceVersion.objects.order_by("pk").values())
        files = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for _ in range(2):
            self.assertEqual(self.advance(), "material-fulfillments:3")
        rows = list(ProductionMaterialFulfillment.objects.filter(reference_version__series__role="variant"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(row.reference_version.asset_id) == response.data["id"] and row.reference_version.source == "upload"
            and row.selection_mode == "automatic" and row.selected_by_id is None for row in rows))
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), versions)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, files)
        self.assertEqual(self.facts(), self.before)
