"""Real isolated generation/registration -> another Run's zero-call reuse.

Original worker and byte/setting readers are used. Only the existing fixture's
admission/Comfy peers and unrelated complete-delivery closure are isolated.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import (Asset, AssetReferenceVersion, ArtifactSelectionDecision, ExecutionPreparation,
    GeneratedArtifact, GenerationAttempt, ProductionMaterialFulfillment, ProductionRecipeVersion, ProductionRun, StageRun)
from studio.services.asset_references import read_generated_material
from studio.services.bible_content_review import recipe_bible_review_detail
from studio.services.json_contracts import assign_json_contract
from studio.services.media_generation_targets import read_media_fulfillment_version
from studio.services.production_recipe_material_fulfillment import read_recipe_material_fulfillment, material_fulfillment_reference
from studio.services.production_recipe_material_sources import material_source_version_candidates
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, decide_stage
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_mixed_material_stage as fixtures


class GeneratedReuseContractTests(SimpleTestCase):
    def test_receipt_keeps_generated_origin_but_public_input_cannot_invent_it(self):
        from drf_spectacular.generators import SchemaGenerator
        models = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        def choices(name):
            field = models[name]["properties"]["source"]
            ref = field.get("$ref") or field["allOf"][0]["$ref"]
            return set(models[ref.split("/")[-1]]["enum"])
        self.assertEqual(choices("RecipeMaterialFulfillmentReceipt"), {"upload", "existing", "generated"})
        self.assertEqual(choices("RecipeMaterialFulfillmentInputRequest"), {"upload", "existing"})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class GeneratedMaterialReuseTests(StructureFixture):
    setUp = fixtures.RecipeMixedMaterialStageTests.setUp
    tearDown = fixtures.RecipeMixedMaterialStageTests.tearDown
    create_job = fixtures.RecipeMixedMaterialStageTests.create_job
    primary_version = fixtures.RecipeMixedMaterialStageTests.primary_version
    write = fixtures.RecipeMixedMaterialStageTests.write
    input = staticmethod(fixtures.RecipeMixedMaterialStageTests.input)
    configure_variant_sources = fixtures.RecipeMixedMaterialStageTests.configure_variant_sources
    configure_declaration = fixtures.RecipeMixedMaterialStageTests.configure_declaration
    variant_anchors = fixtures.RecipeMixedMaterialStageTests.variant_anchors
    select = fixtures.RecipeMixedMaterialStageTests.select
    allow_calls = fixtures.RecipeMixedMaterialStageTests.allow_calls
    advance = fixtures.RecipeMixedMaterialStageTests.advance
    fill_fields = fixtures.RecipeMixedMaterialStageTests.fill_fields
    collect = fixtures.RecipeMixedMaterialStageTests.collect
    start_primary = fixtures.RecipeMixedMaterialStageTests.start_primary
    primary_ready = fixtures.RecipeMixedMaterialStageTests.primary_ready
    start_variants = fixtures.RecipeMixedMaterialStageTests.start_variants

    def library(self):
        primary, _ = self.start_variants()
        artifacts = [self.collect(), self.collect()]
        self.advance()
        self.assertEqual(self.advance(), "generated-artifacts:3")
        for artifact in artifacts:
            artifact.refresh_from_db()
        return primary, [read_generated_material(artifact=artifact) for artifact in artifacts]

    def consumer(self, *, recipe_key="generated-reuse-isolated", selection_policy="unique_version"):
        # A NEW isolated recipe and Run. Never rewrite the producer's frozen
        # plan/request or relabel its preparations as another Run's results.
        recipe = ProductionRecipeVersion(recipe_key=recipe_key, version=1,
            name="Isolated exact generated reuse", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "manual", "approval": "required"},
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required",
                "material_sources": {"primary": "existing", "variant": "existing"},
                "material_selection": {"primary": selection_policy, "variant": selection_policy}},
            {"key": "bible_image_review", "mode": "review", "approval": "required"}]}, schema_version=3)
        recipe.save()
        run = ProductionRun.objects.create(owner=self.user, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="awaiting_approval", current_stage_key="bible_review",
            idempotency_key=str(uuid4()), request_sha256="b" * 64, allow_external_calls=False)
        assign_json_contract(run, "config_snapshot", deepcopy(self.run.config_snapshot),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(run, "input_snapshot", {"schema_version": 1, "interaction_mode": "assisted"})
        run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        review = StageRun.objects.create(production_run=run, stage_key="bible_review", mode="manual",
            status="awaiting_approval", ordinal=1, approval_required=True, idempotency_key=str(uuid4()))
        stage = StageRun.objects.create(production_run=run, stage_key="bible_image_generation", mode="existing_pipeline",
            status="pending", ordinal=2, idempotency_key=str(uuid4()))
        _freeze_stage_config(run, review)
        _freeze_stage_input(run, review)
        _freeze_stage_config(run, stage)
        detail = recipe_bible_review_detail(run)
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure"):
            decide_stage(run, stage_key="bible_review", decision="approve", user=self.user,
                comment="Isolated reuse requirement", bible_review={
                    "expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                    "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
                    "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                    "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]})
        run.status, run.current_stage_key = "running", stage.stage_key
        run.save(update_fields=("status", "current_stage_key"))
        stage.refresh_from_db()
        stage.status = "running"
        stage.save(update_fields=("status",))
        _freeze_stage_input(run, stage)
        stage.production_run = run
        return run, stage

    def reuse(self, run, stage):
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("No new preparation when reusing generated versions")), patch(
                "studio.services.production_recipe_material_sources.material_source_version_candidates",
                side_effect=AssertionError("No rediscovery after entry")), patch(
                "django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No copied result")):
            return advance_recipe_media_stage(run, stage)

    def test_reused_generated_variants_keep_actual_old_primary_and_original_provenance(self):
        primary, variants = self.library()
        primary_target = read_media_fulfillment_version(primary, expected_media_type="image")
        self.assertEqual(primary_target.source_mode, "generated")
        self.assertIsNone(primary_target.value["generated_from"])
        self.assertEqual(primary_target.value["generation_origin"]["reference_version_id"], str(primary.pk))
        run, stage = self.consumer()
        models = (Asset, AssetReferenceVersion, GeneratedArtifact, GenerationAttempt, ExecutionPreparation, ArtifactSelectionDecision)
        originals = {model: list(model.objects.order_by("pk").values()) for model in models}
        self.assertIsNone(self.reuse(run, stage))  # Two exact primary versions: no latest guess.
        self.assertIn("多个确切版本", stage.reason)
        for version in variants:
            row = ProductionMaterialFulfillment.objects.get(stage_run=stage, reference_version=version)
            self.assertEqual((row.selection_mode, row.selected_by_id), ("automatic", None))
            choice = read_recipe_material_fulfillment(stage=stage, unit_key=row.unit_key)
            self.assertEqual(choice["version"].generated_from_id, primary.pk)
            self.assertEqual(material_fulfillment_reference(choice)["source"], "generated")
            query = material_source_version_candidates(stage, choice["target"], "existing")
            self.assertEqual(list(query), [version.pk])
            self.assertIn("INDEX", query.explain().upper())
            self.assertIn("LIMIT 2", str(query.query))
            self.assertEqual(list(material_source_version_candidates(stage, choice["target"], "upload")), [])
        self.client.force_authenticate(self.user)
        base = f"/api/v1/production-runs/{run.pk}/"
        response = self.client.get(base + "material-fulfillment-context/", {
            "stage_run_id": str(stage.pk), "unit_key": f"appearance:{self.settings[0].variant_id}"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["selection"]["source"], "generated")
        self.assertIsNone(response.data["selection"]["selected_by_id"])
        # Explicitly choose the fixture's other original primary, not the one
        # these already-generated variants used. No generation basis rewrites.
        from studio.services.production_recipe_material_fulfillment import select_recipe_material_asset
        select_recipe_material_asset(stage_run_id=stage.pk,
            expected_run_plan_sha256=stage.input_snapshot["material_requirements"]["run_plan_sha256"],
            unit_key=f"appearance:{self.primary_setting.variant_id}", requirement_key=f"setting:{self.primary_setting.pk}",
            series_id=self.primary.series_id, asset_id=self.primary.asset_id, source="existing", actor=self.user)
        for _ in range(2):
            self.assertEqual(self.reuse(run, stage), "material-fulfillments:3")
        self.assertEqual({model: list(model.objects.order_by("pk").values()) for model in models}, originals)
        from studio.services.production_material_references import read_material_result_reference
        choice = read_recipe_material_fulfillment(stage=stage, unit_key=f"appearance:{self.settings[0].variant_id}")
        with CaptureQueriesContext(connection) as queries:
            result = read_material_result_reference(project_id=self.project.pk, field="material_fulfillments",
                reference=material_fulfillment_reference(choice))
        self.assertEqual(result["version"].generated_from_id, primary.pk)
        self.assertFalse(any(q["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))

        manual_run, manual_stage = self.consumer(recipe_key="manual-generated-reuse", selection_policy="manual")
        with patch("studio.services.production_recipe_material_fulfillment.attach_asset",
                side_effect=AssertionError("Manual reuse must preserve the original version")):
            for version in (primary, *variants):
                payload = {"stage_run_id": str(manual_stage.pk),
                    "unit_key": f"appearance:{version.setting_revision.variant_id}",
                    "requirement_key": f"setting:{version.setting_revision_id}",
                    "expected_run_plan_sha256": manual_stage.input_snapshot["material_requirements"]["run_plan_sha256"],
                    "reference_version_id": str(version.pk)}
                url = f"/api/v1/production-runs/{manual_run.pk}/fulfill-material/"
                selected = self.client.post(url, payload, format="json")
                self.assertEqual(selected.status_code, 201, selected.data)
                self.assertEqual(selected.data["source"], "generated")
                self.assertEqual(selected.data["selection_mode"], "manual")
                self.assertEqual(selected.data["selected_by_id"], self.user.pk)
                replay = self.client.post(url, payload, format="json")
                self.assertEqual(replay.status_code, 200, replay.data)
                self.assertEqual(replay.data, selected.data)
        self.assertEqual(self.reuse(manual_run, manual_stage), "material-fulfillments:3")
        self.assertEqual({model: list(model.objects.order_by("pk").values()) for model in models}, originals)

    def test_corrupt_generated_origin_waits_without_relabel_and_later_corruption_is_rejected(self):
        primary, variants = self.library()
        run, stage = self.consumer()
        version = variants[0]
        # Deliberate isolated relationship corruption, no production repair.
        AssetReferenceVersion.objects.filter(pk=version.pk).update(generated_from_id=self.primary.pk)
        self.assertIsNone(self.reuse(run, stage))
        self.assertIn("冻结版本或文件不可用", stage.reason)
        self.assertFalse(ProductionMaterialFulfillment.objects.filter(reference_version=version).exists())
        self.assertTrue(ProductionMaterialFulfillment.objects.filter(reference_version=variants[1]).exists())
        AssetReferenceVersion.objects.filter(pk=version.pk).update(generated_from_id=primary.pk)
        self.assertIsNone(self.reuse(run, stage))
        row = ProductionMaterialFulfillment.objects.get(reference_version=version)
        AssetReferenceVersion.objects.filter(pk=version.pk).update(generated_from_id=self.primary.pk)
        with self.assertRaises((ValidationError, APIValidationError)):
            read_recipe_material_fulfillment(stage=stage, unit_key=row.unit_key)
        AssetReferenceVersion.objects.filter(pk=version.pk).update(generated_from_id=primary.pk)
        version.refresh_from_db()
        path = Path(version.asset.file.path)
        original = path.read_bytes()
        path.write_bytes(b"isolated damaged generated image")
        with self.assertRaises(ValidationError):
            read_media_fulfillment_version(version, expected_media_type="image")
        path.write_bytes(original)
        self.assertEqual(read_media_fulfillment_version(version).value["generated_from"], str(primary.pk))
        self.assertEqual(AssetReferenceVersion.objects.filter(asset_id=version.asset_id).count(), 1)
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=stage).exists())
