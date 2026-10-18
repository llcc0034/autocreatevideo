"""Isolated original queue/registration -> mixed-result review, no real model.

The existing fixture replaces admission observations and Comfy transport only;
review authorization uses the same explicit substitute as policy-writer tests.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ArtifactSelectionDecision, Asset, AssetReferenceVersion, GenerationAttempt, StageRun
from studio.services.production_recipe_material_fulfillment import select_recipe_material_asset
from studio.services.production_recipe_material_review import material_review_context, read_recipe_material_generations
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot, _assert_frozen_formal_objects
from studio.tests import test_recipe_mixed_material_stage as queue
from studio.tests import test_recipe_material_review as review
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMixedMaterialReviewTests(StructureFixture):
    Fixture = queue.RecipeMixedMaterialStageTests
    setUp = Fixture.setUp
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    variant_anchors = Fixture.variant_anchors
    select = Fixture.select
    allow_calls = Fixture.allow_calls
    fill_fields = Fixture.fill_fields
    advance = Fixture.advance
    collect = Fixture.collect
    execute_policy_review = review.RecipeMaterialReviewTests.execute_policy_review
    selection = review.RecipeMaterialReviewTests.selection
    approve = review.RecipeMaterialReviewTests.approve

    def configure_declaration(self, declaration):
        self.Fixture.configure_declaration(self, declaration)
        self.reuse_primary = "reused_primary" in self._testMethodName
        if self.reuse_primary:
            declaration["material_sources"] = {"primary": "existing", "variant": "generate"}
            declaration.pop("routing")
        else:
            declaration["routing"]["variant_count"] = 2

    def tearDown(self):
        self.external.assert_not_called()

    def complete(self):
        if self.reuse_primary:
            # assets[0] occupies another input slot; its published contract
            # forbids duplicate media, so use a distinct formally uploaded Asset.
            self.client.force_authenticate(self.user)
            response = self.client.post("/api/v1/assets/", {
                "name": "Isolated mixed review primary", "kind": "reference", "file": image_upload(),
            }, format="multipart")
            self.assertEqual(response.status_code, 201, response.data)
            self.fulfillment, _ = select_recipe_material_asset(stage_run_id=self.stage.pk,
                expected_run_plan_sha256=self.plan.sha256, unit_key=f"appearance:{self.primary_setting.variant_id}",
                requirement_key=f"setting:{self.primary_setting.pk}", series_id=self.primary.series_id,
                asset_id=response.data["id"], source="existing", actor=self.user)
        self.assertIsNone(self.advance(supply_fields=True))
        if not self.reuse_primary:
            self.collect()
            self.collect()
            self.assertIsNone(self.advance())
            self.assertIsNone(self.advance(supply_fields=True))
            self.advance()
        self.collect()
        self.collect()
        self.assertIsNone(self.advance())
        result = self.advance()
        self.assertIsNotNone(result)
        self.stage.refresh_from_db()
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, result))
        self.review = StageRun.objects.create(production_run=self.run, stage_key="bible_image_review",
            ordinal=3, mode="review", status="running", idempotency_key=str(uuid4()))

    def facts(self):
        return {"assets": list(Asset.objects.order_by("pk").values()),
            "versions": list(AssetReferenceVersion.objects.order_by("pk").values()),
            "decisions": list(ArtifactSelectionDecision.objects.order_by("pk").values()),
            "attempts": list(GenerationAttempt.objects.order_by("pk").values())}

    def test_reused_primary_and_generated_siblings_review_without_second_selection_or_copy(self):
        self.complete()
        canonical = self.stage.output_snapshot["canonical"]
        self.assertEqual(len(canonical["material_generations"]), 2)
        self.assertEqual(len(canonical["material_fulfillments"]), 1)
        # The original mixed upstream output also supplies exact shot defaults,
        # without interpreting the shot's authored purpose as a model purpose.
        from studio.models import Episode, Shot, ShotReferenceUse
        from studio.services.production_shot_reference_defaults import plan_upstream_shot_reference_choices
        episode = Episode.objects.create(project=self.project, number=91, title="Isolated upstream shot uses")
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=1000,
            media_type="video", include_audio=False)
        references = canonical["material_fulfillments"] + canonical["material_generations"]
        for position, reference in enumerate(references):
            version = AssetReferenceVersion.objects.get(pk=reference["reference_version_id"])
            ShotReferenceUse.objects.create(shot=shot, use_key=f"appearance-{position}", position=position,
                media_kind="image", purpose="原用途说明", setting_revision=version.setting_revision)
        before_defaults = self.facts()
        with CaptureQueriesContext(connection) as default_queries:
            defaults = plan_upstream_shot_reference_choices(stage_run_id=self.review.pk, actor=self.user,
                shot_id=shot.pk, source_stage_id=self.stage.pk,
                expected_source_output_sha256=self.stage.output_snapshot_sha256)
        self.assertTrue(defaults.ready)
        self.assertEqual([row["reference_version_id"] for row in defaults.choices],
            [row["reference_version_id"] for row in references])
        self.assertEqual([row["source_mode"] for row in defaults.choices], ["existing", "generated", "generated"])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in default_queries))
        self.assertEqual(before_defaults, self.facts())
        self.assertFalse(shot.reference_uses.exclude(source_mode="").exists())
        displayed = canonical["entities"][0]["fulfilled_materials"]
        self.assertEqual(len(displayed), 3)
        self.assertEqual([item["reference"] for item in displayed], canonical["material_fulfillments"] + canonical["material_generations"])
        self.assertEqual([item["appearance_role"] for item in displayed], ["primary", "variant", "variant"])
        self.assertEqual([item["reference"]["source"] for item in displayed], ["existing", "generated", "generated"])
        self.assertEqual({item["asset_id"] for item in displayed},
            {str(AssetReferenceVersion.objects.get(pk=item["reference"]["reference_version_id"]).asset_id) for item in displayed})
        before = self.facts()
        files = {str(path): path.read_bytes() for path in self.root.rglob("*.png")}
        from studio.services.production_material_references import read_upstream_material_result
        for field in ("material_fulfillments", "material_generations"):
            for reference in canonical[field]:
                choice = read_upstream_material_result(stage=self.review, source={
                    "stage_run_id": str(self.stage.pk), "output_snapshot_sha256": self.stage.output_snapshot_sha256,
                    "field": field, "reference": reference})
                self.assertEqual(str(choice["version"].pk), reference["reference_version_id"])
                self.assertEqual(choice["version"].source, reference["source"])
                if field == "material_generations":
                    self.assertEqual(choice["version"].generated_from_id, self.fulfillment.reference_version_id)
                    self.assertEqual((choice["decision"].selection_mode, choice["decision"].selected_by_id), ("automatic", None))
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No media copy")):
            self.execute_policy_review()
        self.assertEqual(before, self.facts())
        self.assertEqual(files, {str(path): path.read_bytes() for path in self.root.rglob("*.png")})
        self.review.refresh_from_db()
        result = self.review.output_snapshot["canonical"]
        self.assertEqual(result["material_generations"], canonical["material_generations"])
        binding = result["bindings"][0]
        self.assertEqual(binding["anchor_asset_id"], str(self.fulfillment.reference_version.asset_id))
        self.assertEqual(len(binding["reference_assets"]), 2)
        for fact in binding["reference_assets"]:
            self.assertEqual(fact["material_generations"][0]["generated_from_id"], str(self.fulfillment.reference_version_id))
        self.assertEqual(sum(row["source_type"] == "generated_artifact" for row in binding["selection_sources"]), 2)
        self.review.status = "succeeded"
        self.review.save(update_fields=["status"])
        _assert_frozen_formal_objects(self.run)
        # A later round has no current material_context; retained generated
        # siblings must still resolve their original choice and exact version.
        from studio.services.production_runs import _bible_binding_asset_fact
        self.character.refresh_from_db()
        for fact in [binding["anchor_asset"], *binding["reference_assets"]]:
            self.assertEqual(_bible_binding_asset_fact(Asset.objects.get(pk=fact["asset_id"]),
                project=self.project, entity_type="character", entity=self.character), fact)
        self.assertEqual(before, self.facts())

    def test_multiple_primary_candidates_use_original_choice_and_preserve_automatic_identity(self):
        self.complete()
        canonical = self.stage.output_snapshot["canonical"]
        self.assertEqual(len(canonical["entities"][0]["candidates"]), 2)
        self.assertEqual(len(canonical["material_generations"]), 3)
        context = material_review_context(self.run, canonical)[("character", str(self.character.pk))]
        before = self.facts()
        self.execute_policy_review()
        self.assertEqual(before, self.facts())
        self.character.refresh_from_db()
        self.assertEqual(str(self.character.anchor_asset_id), context["primary_asset_id"])
        self.assertEqual(self.character.reference_assets.count(), 2)
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exclude(selection_mode="automatic", selected_by=None).exists())
        self.assertEqual(ApprovalDecision.objects.get(stage_run=self.review).actor, "recipe_policy")
        # Explain the actual new narrow, stage-scoped distinct unit query.
        with CaptureQueriesContext(connection) as captured, patch(
                "studio.services.production_recipe_material_review.read_recipe_material_fulfillment",
                side_effect=AssertionError("Absent fulfillment must not repeat full requirement/media reads")):
            read_recipe_material_generations(self.stage)
        sql = next(row["sql"] for row in captured if 'SELECT DISTINCT "studio_executionpreparation"."unit_key"' in row["sql"])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql)
            explanation = str(cursor.fetchall())
        self.assertIn("USING", explanation)
        self.assertNotIn("SCAN studio_executionpreparation", explanation)
        fulfillment_sql = next(row["sql"] for row in captured
            if 'SELECT "studio_productionmaterialfulfillment"."unit_key"' in row["sql"])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + fulfillment_sql)
            self.assertIn("INDEX", str(cursor.fetchall()))

    def test_explicit_selected_primary_candidate_reuses_registration_and_rejects_alternate(self):
        self.complete()
        canonical = self.stage.output_snapshot["canonical"]
        context = material_review_context(self.run, canonical)[("character", str(self.character.pk))]
        primary_asset = context["primary_asset_id"]
        primary = context["assets"][primary_asset]["material_generations"][0]["artifact_id"]
        selection = self.selection()
        selection["selected_library_assets"] = [row for row in selection["selected_library_assets"] if row["asset_id"] != primary_asset]
        selection["selected_artifact_ids"], selection["anchor_artifact_id"] = [primary], primary
        selection["anchor_library_asset_id"] = None
        alternate = next(row["artifact_id"] for row in canonical["entities"][0]["candidates"] if row["artifact_id"] != primary)
        invalid = {**selection, "selected_artifact_ids": [alternate], "anchor_artifact_id": alternate}
        before = self.facts()
        with self.assertRaises(ValidationError):
            self.approve(invalid)
        self.approve(selection)
        self.assertEqual(before, self.facts())
        self.assertEqual(self.review.output_snapshot["bindings"][0]["anchor_asset_id"], primary_asset)

    def test_reused_primary_review_rejects_changed_version_file_and_rolls_back_receipt(self):
        self.complete()
        original = deepcopy(self.stage.output_snapshot["canonical"])
        corrupt = deepcopy(original)
        corrupt["material_generations"][0]["reference_version_id"] = str(uuid4())
        with self.assertRaises(ValidationError):
            material_review_context(self.run, corrupt)
        version = AssetReferenceVersion.objects.get(pk=original["material_generations"][0]["reference_version_id"])
        path = Path(version.asset.file.path)
        content = path.read_bytes()
        path.write_bytes(b"isolated corrupt generated variant")
        with self.assertRaises(ValidationError):
            material_review_context(self.run, original)
        path.write_bytes(content)
        before = self.facts()
        with patch.object(ApprovalDecision, "save", side_effect=RuntimeError("isolated receipt failure")), self.assertRaises(RuntimeError):
            self.execute_policy_review()
        self.assertEqual(before, self.facts())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(self.character.reference_assets.exists())
        self.execute_policy_review()
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 1)
