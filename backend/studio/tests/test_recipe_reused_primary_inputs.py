"""Explicit reused primary -> original variant inputs, queue and registration.

Isolated DB/media and controlled Comfy transport only. The dispatcher case
covers mixed reuse/generation, not source authoring/UI or full Run acceptance.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import SimpleTestCase, override_settings

from studio.models import (ArtifactSelectionDecision, AssetReferenceVersion,
    ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ProductionMaterialFulfillment)
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_material_fulfillment import attach_recipe_material_asset, select_recipe_material_asset
from studio.services.production_recipe_material_inputs import read_recipe_material_binding, recipe_material_primary_reference
from studio.services.production_recipe_material_registration import read_registered_recipe_primary_dependency, register_recipe_material_selection
from studio.services.production_recipe_material_selection import choose_recipe_material_candidate
from studio.services.production_recipe_routing import require_current_recipe_preparation
from studio.services.production_runs import _StoredAssetFileGuard
from studio.tests import test_recipe_material_inputs as inputs
from studio.tests import test_recipe_mixed_material_stage as queue
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeReusedPrimaryInputTests(StructureFixture):
    # Reuse setup/transport helpers, not their TestCases or test counts.
    create_job = queue.RecipeMixedMaterialStageTests.create_job
    primary_version = queue.RecipeMixedMaterialStageTests.primary_version
    write = queue.RecipeMixedMaterialStageTests.write
    input = staticmethod(queue.RecipeMixedMaterialStageTests.input)
    configure_variant_sources = queue.RecipeMixedMaterialStageTests.configure_variant_sources
    variant_anchors = queue.RecipeMixedMaterialStageTests.variant_anchors
    select = queue.RecipeMixedMaterialStageTests.select
    allow_calls = queue.RecipeMixedMaterialStageTests.allow_calls
    fill_fields = queue.RecipeMixedMaterialStageTests.fill_fields
    collect = queue.RecipeMixedMaterialStageTests.collect
    advance = queue.RecipeMixedMaterialStageTests.advance
    tearDown = queue.RecipeMixedMaterialStageTests.tearDown
    prepare = inputs.RecipeMaterialInputTests.prepare

    def configure_declaration(self, declaration):
        queue.RecipeMixedMaterialStageTests.configure_declaration(self, declaration)
        if self._testMethodName == "test_dispatcher_skips_reused_primary_and_completes_both_generated_siblings":
            declaration["routing"]["mode"] = "manual_choice"
        if self._testMethodName == "test_configured_existing_primary_waits_then_dispatches_only_generated_variants":
            declaration["material_sources"] = {"primary": "existing", "variant": "generate"}
            declaration.pop("routing")

    def setUp(self):
        queue.RecipeMixedMaterialStageTests.setUp(self)
        self.client.force_authenticate(self.user)
        for asset in self.assets:
            asset.mime_type = "image/png"
            asset.save(update_fields=("mime_type",))
        # A separate existing library Asset: assets[0] already occupies another
        # image slot, whose published contract correctly forbids duplicate use.
        response = self.client.post("/api/v1/assets/", {"name": "Isolated library primary", "kind": "reference",
            "file": image_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.reused_asset_id = response.data["id"]

    def choice_arguments(self, **changes):
        return {"stage_run_id": self.stage.pk, "expected_run_plan_sha256": self.plan.sha256,
            "unit_key": f"appearance:{self.primary_setting.variant_id}",
            "requirement_key": f"setting:{self.primary_setting.pk}", "series_id": self.primary.series_id,
            "asset_id": self.reused_asset_id, "source": "existing", "actor": self.user, **changes}

    def choose(self, **changes):
        choice, created = select_recipe_material_asset(**self.choice_arguments(**changes))
        self.assertTrue(created)
        self.assertNotEqual(choice.reference_version_id, self.primary.pk)
        return choice

    def assert_binding(self, preparation, choice, index):
        detail = read_execution_preparation_detail(preparation)
        target = read_media_generation_target(preparation).value
        self.assertEqual(read_recipe_material_binding(preparation)["primary"], {
            "fulfillment_id": str(choice.pk), "version_id": str(choice.reference_version_id)})
        self.assertEqual(target["appearance"]["primary"]["version_id"], str(choice.reference_version_id))
        self.assertEqual(detail.resolved_inputs["inputs"]["logical_values"]["reference_2"][0]["asset_id"],
            str(choice.reference_version.asset_id))
        self.assertEqual(detail.resolved_inputs["inputs"]["logical_values"][inputs.SOURCE]["changes"][1]["detail.text"],
            self.variant_anchors[index])

    def assert_no_primary_generation(self):
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage,
            unit_key=f"appearance:{self.primary_setting.variant_id}").exists())

    def test_configured_existing_primary_waits_then_dispatches_only_generated_variants(self):
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_source")
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.test_dispatcher_skips_reused_primary_and_completes_both_generated_siblings()

    def test_library_association_without_run_choice_never_supplies_variant(self):
        attach_recipe_material_asset(**self.choice_arguments())
        self.assertIsNone(read_registered_recipe_primary_dependency(stage=self.stage,
            unit_key=f"appearance:{self.settings[0].variant_id}"))
        with self.assertRaisesMessage(ValidationError, "尚未选择或登记"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_formal_upload_choice_supplies_both_siblings_without_fake_generation(self):
        response = self.client.post("/api/v1/assets/", {"name": "Isolated uploaded primary", "kind": "reference",
            "file": image_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        choice = self.choose(asset_id=response.data["id"], source="upload")
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No media copy")):
            for index in range(2):
                preparation = self.prepare(index)
                self.assert_binding(preparation, choice, index)
                require_current_recipe_preparation(preparation, stage=self.stage)
        self.assertEqual(choice.reference_version.source, "upload")
        self.assertIsNone(choice.reference_version.generated_artifact_id)
        self.assertIsNone(choice.reference_version.generated_from_id)
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assert_no_primary_generation()

    def test_preparation_replay_preserves_original_receipt_without_current_reselection(self):
        choice = self.choose()
        preparation = self.prepare()
        original = deepcopy(read_execution_preparation_detail(preparation).resolved_inputs)
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency",
                side_effect=AssertionError("Do not reselect frozen history")):
            self.assertEqual(self.prepare().pk, preparation.pk)
            self.assert_binding(preparation, choice, 0)
        self.assertEqual(read_execution_preparation_detail(preparation).resolved_inputs, original)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_changed_primary_file_blocks_current_use_but_not_frozen_history(self):
        choice = self.choose()
        preparation = self.prepare()
        Path(choice.reference_version.asset.file.path).write_bytes(b"isolated changed media")
        with self.assertRaises(ValidationError):
            self.prepare(1)
        with self.assertRaises(ValidationError):
            require_current_recipe_preparation(preparation, stage=self.stage)
        self.assertEqual(read_recipe_material_binding(preparation)["primary"]["fulfillment_id"], str(choice.pk))
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_changed_choice_identity_cannot_dispatch_old_preparation(self):
        choice = self.choose()
        preparation = self.prepare()
        # Corrupt-reader fixture only; the real immutable choice has no update API.
        dependency = read_registered_recipe_primary_dependency(stage=self.stage, unit_key=preparation.unit_key)
        dependency["fulfillment"] = SimpleNamespace(pk=uuid4())
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency",
                return_value=dependency), self.assertRaisesMessage(ValidationError, "主图选择已改变"):
            require_current_recipe_preparation(preparation, stage=self.stage)
        self.assertEqual(ProductionMaterialFulfillment.objects.get().pk, choice.pk)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_reused_primary_reaches_original_queue_and_exact_generated_from_for_two_siblings(self):
        choice = self.choose()
        files_before = Path(choice.reference_version.asset.file.path).read_bytes()
        preparations = [self.prepare(index) for index in range(2)]
        for index, preparation in enumerate(preparations):
            self.assert_binding(preparation, choice, index)
            self.fill_fields(preparation)
            preparation.refresh_from_db()
            preparation = advance_execution_preparation(preparation_id=preparation.pk,
                expected_preparation_revision=preparation.revision, actor=self.user)
            self.assertEqual(preparation.status, "dispatched", (preparation.reason_code, preparation.reason))
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        artifacts = [self.collect(), self.collect()]
        for artifact in artifacts:
            preparation = artifact.generation_attempt.preparation
            decision = choose_recipe_material_candidate(stage_run_id=self.stage.pk, unit_key=preparation.unit_key)
            self.assertIsNotNone(decision)
            with _StoredAssetFileGuard() as guard, transaction.atomic():
                version = register_recipe_material_selection(stage_run_id=self.stage.pk, unit_key=preparation.unit_key,
                    decision_id=decision.pk, actor=self.user, asset_guard=guard)
            self.assertEqual(version.generated_from_id, choice.reference_version_id)
            self.assertEqual(version.source, "generated")
            self.assertEqual(version.generated_artifact_id, artifact.pk)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions + 3)
        self.assertEqual(GeneratedArtifact.objects.count(), 2)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        self.assert_no_primary_generation()
        choice.reference_version.refresh_from_db()
        self.assertEqual(choice.reference_version.source, "existing")
        self.assertIsNone(choice.reference_version.generated_artifact_id)
        self.assertIsNone(choice.reference_version.generated_from_id)
        self.assertEqual(Path(choice.reference_version.asset.file.path).read_bytes(), files_before)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "running")  # No whole-stage completion claimed.

    def test_dispatcher_skips_reused_primary_and_completes_both_generated_siblings(self):
        choice = self.choose()
        self.assertIsNone(self.stage.selected_route_index)
        self.assertIsNone(self.advance(supply_fields=True))
        preparations = list(ExecutionPreparation.objects.filter(stage_run=self.stage).order_by("unit_key"))
        self.assertEqual(len(preparations), 2)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assert_no_primary_generation()
        for preparation in preparations:
            index = next(index for index, setting in enumerate(self.settings)
                if preparation.unit_key == f"appearance:{setting.variant_id}")
            self.assert_binding(preparation, choice, index)
        artifacts = [self.collect(), self.collect()]
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        for artifact in artifacts:
            self.assertEqual(AssetReferenceVersion.objects.get(generated_artifact=artifact).generated_from_id,
                choice.reference_version_id)
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No repeat copy")):
            for _ in range(2):
                self.assertEqual(self.advance(), "generated-artifacts:2;material-fulfillments:1")
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(GeneratedArtifact.objects.count(), 2)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 1)
        self.assert_no_primary_generation()


class RecipePrimaryReceiptTests(SimpleTestCase):
    def test_source_union_rejects_synthetic_generation_on_reuse(self):
        dependency = {"version": SimpleNamespace(pk=uuid4()), "fulfillment": SimpleNamespace(pk=uuid4())}
        self.assertEqual(set(recipe_material_primary_reference(dependency)), {"version_id", "fulfillment_id"})
        for key in ("artifact", "decision", "preparation"):
            with self.subTest(key=key), self.assertRaisesMessage(ValidationError, "不能同时声明"):
                recipe_material_primary_reference({**dependency, key: SimpleNamespace(pk=uuid4())})

    def test_frozen_receipt_rejects_mixed_missing_and_noncanonical_identities(self):
        version_id, fulfillment_id, setting_id = (str(uuid4()) for _ in range(3))
        difference = {"setting_revision_id": setting_id, "value_sha256": "a" * 64}
        appearance = {"role": "variant", "primary": {"version_id": version_id},
            "setting": {"setting_revision_id": setting_id}, "difference": {"value_sha256": "a" * 64}}
        primary = {"version_id": version_id, "fulfillment_id": fulfillment_id}
        for invalid in ({**primary, "decision_id": str(uuid4())}, {"version_id": version_id},
                {**primary, "fulfillment_id": fulfillment_id.upper()}, {**primary, "version_id": str(uuid4())}):
            detail = SimpleNamespace(resolved_inputs={"recipe_material_binding": {"primary": invalid, "difference": difference}})
            with self.subTest(primary=invalid), patch(
                    "studio.services.execution_preparations.read_execution_preparation_detail", return_value=detail), patch(
                    "studio.services.media_generation_targets.read_media_generation_target", return_value=SimpleNamespace(value={"appearance": appearance})), self.assertRaises(ValidationError):
                read_recipe_material_binding(SimpleNamespace())
