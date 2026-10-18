"""Formal HTTP selection on isolated original M3/Asset facts; no dispatch."""
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceVersion, ProductionMaterialFulfillment, ProductionRun, StageRun
from studio.services.bible_variants import revise_variant, setting_payload
from studio.tests import test_recipe_material_fulfillment as fixtures
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialFulfillmentApiTests(StructureFixture):
    setUp = fixtures.RecipeMaterialAssociationTests.setUp
    tearDown = fixtures.RecipeMaterialAssociationTests.tearDown
    facts = fixtures.RecipeMaterialAssociationTests.facts
    create_job = fixtures.RecipeMaterialAssociationTests.create_job
    primary_version = fixtures.RecipeMaterialAssociationTests.primary_version
    write = fixtures.RecipeMaterialAssociationTests.write
    input = staticmethod(fixtures.RecipeMaterialAssociationTests.input)
    configure_variant_sources = fixtures.RecipeMaterialAssociationTests.configure_variant_sources
    configure_declaration = fixtures.RecipeMaterialAssociationTests.configure_declaration
    target = fixtures.RecipeMaterialAssociationTests.target
    prepare = fixtures.RecipeMaterialAssociationTests.prepare

    def payload(self, setting=None, series=None, **changes):
        setting = setting or self.settings[0]
        return {"stage_run_id": str(self.stage.pk), "unit_key": f"appearance:{setting.variant_id}",
            "requirement_key": f"setting:{setting.pk}", "expected_run_plan_sha256": self.plan.sha256,
            "series_id": str((series or self.series[0]).pk), "asset_id": str(self.assets[0].pk),
            "source": "existing", **changes}

    def inspect(self, payload=None, **changes):
        payload = payload or self.payload()
        return self.client.get(f"/api/v1/production-runs/{self.run.pk}/material-fulfillment-context/",
            {"stage_run_id": payload["stage_run_id"], "unit_key": payload["unit_key"], **changes})

    def select(self, payload=None, **changes):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/fulfill-material/",
            {**(payload or self.payload()), **changes}, format="json")

    def version_payload(self, version, setting=None):
        return {key: value for key, value in self.payload(setting,
            reference_version_id=str(version.pk)).items() if key not in {"series_id", "asset_id", "source"}}

    def test_exact_version_choice_keeps_original_relation_and_replays_without_attach(self):
        from studio.services.asset_references import attach_asset
        version, _ = attach_asset(actor=self.user, series_id=self.series[0].pk, asset_id=self.assets[0].pk,
            source="existing", setting_revision_id=self.settings[0].pk)
        original = list(AssetReferenceVersion.objects.order_by("pk").values())
        with patch("studio.services.production_recipe_material_fulfillment.attach_asset",
                side_effect=AssertionError("Exact version selection must never associate again")):
            result = self.select(self.version_payload(version))
            self.assertEqual(result.status_code, 201, result.data)
            self.assertEqual(result.data["reference_version_id"], str(version.pk))
            self.assertEqual(result.data["selection_mode"], "manual")
            self.assertEqual(result.data["selected_by_id"], self.user.pk)
            replay = self.select(self.version_payload(version))
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, result.data)
        self.assertEqual(self.select(self.version_payload(self.primary)).status_code, 400)
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), original)
        self.assertEqual(self.facts(), self.before)

    def test_exact_version_rejects_mixed_missing_wrong_scope_and_stale_contract(self):
        primary = self.version_payload(self.primary, self.primary_setting)
        for data in (self.version_payload(self.primary), {**primary, "reference_version_id": str(uuid4())},
                {**primary, "asset_id": str(self.assets[0].pk)}, {**primary, "source": "generated"},
                {**primary, "series_id": str(self.primary.series_id)},
                {**primary, "expected_run_plan_sha256": "0" * 64},
                {key: value for key, value in primary.items() if key != "reference_version_id"}):
            with self.subTest(data=data):
                self.assertEqual(self.select(data).status_code, 400)
        StageRun.objects.filter(pk=self.stage.pk).update(status="succeeded")
        self.assertEqual(self.select(primary).status_code, 400)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_exact_version_checks_original_file_and_rolls_back_only_new_choice(self):
        primary = self.version_payload(self.primary, self.primary_setting)
        before = list(AssetReferenceVersion.objects.order_by("pk").values())
        original = ProductionMaterialFulfillment.save
        def fail(row, *args, **kwargs):
            original(row, *args, **kwargs)
            raise RuntimeError("isolated version choice rollback")
        with patch.object(ProductionMaterialFulfillment, "save", fail), self.assertRaisesMessage(RuntimeError, "rollback"):
            self.select(primary)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        path = Path(self.primary.asset.file.path)
        path.write_bytes(b"isolated damaged version")
        self.assertEqual(self.select(primary).status_code, 400)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), before)
        self.assertEqual(self.facts(), self.before)

    def test_context_reads_exact_requirement_and_fixed_primary_without_writes_or_library_guess(self):
        versions = AssetReferenceVersion.objects.count()
        with CaptureQueriesContext(connection) as queries:
            result = self.inspect()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertEqual(result.data["appearance_id"], str(self.settings[0].variant_id))
        self.assertEqual(result.data["setting_revision_id"], str(self.settings[0].pk))
        self.assertEqual(result.data["primary_appearance_id"], str(self.primary_setting.variant_id))
        self.assertEqual(result.data["run_plan_sha256"], self.plan.sha256)
        self.assertTrue(result.data["can_select"])
        self.assertEqual(result.data["reason"], "")
        self.assertIsNone(result.data["selection"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(AssetReferenceVersion.objects.count(), versions)
        self.assertEqual(self.facts(), self.before)
        for model in (ProductionMaterialFulfillment,):
            sql_plan = model.objects.filter(stage_run=self.stage, unit_key=self.payload()["unit_key"]).explain().upper()
            self.assertIn("INDEX", sql_plan)
            self.assertNotIn("SCAN", sql_plan)

    def test_primary_and_two_variants_commit_then_replay_same_receipts_without_advancing(self):
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            data = self.payload(setting, series)
            result = self.select(data)
            self.assertEqual(result.status_code, 201, result.data)
            self.assertEqual(result.data["selected_by_id"], self.user.pk)
            self.assertEqual(result.data["selection_mode"], "manual")
            self.assertIs(type(result.data["selected_by_id"]), int)
            replay = self.select(data)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, result.data)
            context = self.inspect(data)
            self.assertEqual(context.status_code, 200, context.data)
            self.assertFalse(context.data["can_select"])
            self.assertEqual(context.data["selection"], result.data)
            self.assertIsNone(AssetReferenceVersion.objects.get(pk=result.data["reference_version_id"]).generated_from_id)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 3)
        self.assertEqual(self.facts(), self.before)

    def test_upload_uses_formal_multipart_asset_and_rejects_forged_upload_origin(self):
        self.assertEqual(self.select(source="upload").status_code, 400)
        uploaded = self.client.post("/api/v1/assets/", {"name": "isolated API fulfillment",
            "kind": "reference", "file": image_upload()}, format="multipart")
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        result = self.select(asset_id=uploaded.data["id"], source="upload")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["asset_id"], uploaded.data["id"])
        self.assertEqual(result.data["source"], "upload")
        self.assertEqual(self.facts(), self.before)

    def test_foreign_run_or_stage_not_disclosed_even_for_staff_cross_run_url(self):
        other = get_user_model().objects.create_user(username="fulfillment-other")
        self.client.force_authenticate(other)
        self.assertEqual(self.inspect().status_code, 404)
        self.assertEqual(self.select().status_code, 404)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_authenticate(self.user)
        other_run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual", status="running")
        other_stage = StageRun.objects.create(production_run=other_run, stage_key="script_generate",
            mode="manual", status="running", ordinal=1)
        with patch("studio.production_material_fulfillment_views.select_recipe_material_asset") as writer:
            # The actual URL is inaccessible regardless of a valid submitted Stage.
            result = self.client.post(f"/api/v1/production-runs/{uuid4()}/fulfill-material/", self.payload(), format="json")
            self.assertEqual(result.status_code, 404)
            result = self.select(stage_run_id=str(uuid4()))
            self.assertEqual(result.status_code, 404)
            self.assertEqual(self.select(stage_run_id=str(other_stage.pk)).status_code, 404)
            self.assertEqual(self.inspect(stage_run_id=str(other_stage.pk)).status_code, 404)
            writer.assert_not_called()

    def test_strict_inputs_cannot_supply_actor_setting_or_alternate_generation_source(self):
        for change in ({"selected_by_id": self.user.pk}, {"selection_mode": "automatic"},
                {"setting_revision_id": str(self.primary_setting.pk)},
                {"source": "generated"}, {"source": "automatic"}, {"asset_id": "not-a-uuid"},
                {"expected_run_plan_sha256": "invalid"}, {"requirement_key": "x" * 192}, {"file": "bypass"}):
            with self.subTest(change=change):
                self.assertEqual(self.select(**change).status_code, 400)
        self.assertEqual(self.inspect(extra="guess").status_code, 400)
        self.assertEqual(self.inspect(unit_key="").status_code, 400)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(self.facts(), self.before)

    def test_wrong_requirement_plan_and_series_reject_without_association(self):
        versions = AssetReferenceVersion.objects.count()
        for change in ({"expected_run_plan_sha256": "0" * 64}, {"unit_key": f"appearance:{uuid4()}"},
                {"requirement_key": f"setting:{uuid4()}"}, {"series_id": str(self.series[1].pk)}):
            with self.subTest(change=change):
                self.assertEqual(self.select(**change).status_code, 400)
        self.assertEqual(AssetReferenceVersion.objects.count(), versions)
        self.assertEqual(self.facts(), self.before)

    def test_conflicting_replay_preserves_first_receipt_and_no_post_commit_read(self):
        with patch("studio.services.production_recipe_material_fulfillment.read_recipe_material_fulfillment",
                side_effect=AssertionError("New commit response must not fail in a post-commit reread")):
            result = self.select()
        self.assertEqual(result.status_code, 201, result.data)
        versions = AssetReferenceVersion.objects.count()
        self.assertEqual(self.select(asset_id=str(self.assets[1].pk)).status_code, 400)
        self.assertEqual(str(ProductionMaterialFulfillment.objects.get().pk), result.data["id"])
        self.assertEqual(AssetReferenceVersion.objects.count(), versions)
        self.assertEqual(self.facts(), self.before)

    def test_changed_m3_after_context_rejects_choice_without_silent_refresh(self):
        self.assertEqual(self.inspect().status_code, 200)
        revise_variant(actor=self.user, variant=self.settings[0].variant, expected_revision=1,
            value={**setting_payload(self.settings[0]), "appearance_anchor": "new isolated unreviewed setting"})
        self.assertEqual(self.select().status_code, 400)
        self.assertEqual(self.inspect().status_code, 400)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_generation_created_after_context_blocks_same_unit_and_primary_without_cancel_or_retry(self):
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        self.assertTrue(self.inspect().data["can_select"])
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts", resolve_execution_request_facts):
            self.prepare()
        before = self.facts()
        for data in (self.payload(), self.payload(self.primary_setting, self.primary.series)):
            context = self.inspect(data)
            self.assertEqual(context.status_code, 200, context.data)
            self.assertFalse(context.data["can_select"])
            self.assertIn("准备", context.data["reason"])
            self.assertEqual(self.select(data).status_code, 400)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(self.facts(), before)

    def test_terminal_stage_is_readable_but_cannot_select_or_replay(self):
        StageRun.objects.filter(pk=self.stage.pk).update(status="succeeded")
        result = self.inspect()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertFalse(result.data["can_select"])
        before = self.facts()
        self.assertEqual(self.select().status_code, 400)
        self.assertEqual(self.facts(), before)

    def test_corrupt_selected_file_is_explicit_error_not_usable_context_or_regeneration(self):
        result = self.select()
        self.assertEqual(result.status_code, 201, result.data)
        Path(self.assets[0].file.path).write_bytes(b"isolated corrupt selected image")
        self.assertEqual(self.inspect().status_code, 400)
        self.assertEqual(self.select().status_code, 400)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 1)
        self.assertEqual(self.facts(), self.before)

    def test_disabled_owner_cannot_be_overridden_by_staff_or_called_anonymously(self):
        staff = get_user_model().objects.create_user(username="fulfillment-staff", is_staff=True)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.client.force_authenticate(staff)
        self.assertEqual(self.inspect().status_code, 403)
        self.assertEqual(self.select().status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.inspect().status_code, (401, 403))
        self.assertIn(self.select().status_code, (401, 403))
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_failure_after_choice_write_rolls_back_association_and_receipt(self):
        original = ProductionMaterialFulfillment.save
        def fail(row, *args, **kwargs):
            original(row, *args, **kwargs)
            raise RuntimeError("isolated commit failure")
        count = AssetReferenceVersion.objects.count()
        with patch.object(ProductionMaterialFulfillment, "save", fail), self.assertRaisesMessage(RuntimeError, "commit failure"):
            self.select()
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(AssetReferenceVersion.objects.count(), count)
        self.assertEqual(self.facts(), self.before)
