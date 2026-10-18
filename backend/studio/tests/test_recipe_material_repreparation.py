"""M4 public replacement and original worker; isolated transport, no live model."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from studio.models import Asset, ArtifactSelectionDecision, AssetReferenceVersion, ExecutionPreparation, GenerationAttempt
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.production_recipe_material_selection import change_recipe_primary_and_reprepare
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_recipe_routing import read_recipe_candidate_route, require_current_recipe_preparation
from studio.services.production_runs import ProductionRunConflict
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_mixed_material_stage as mixed


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialRepreparationTests(StructureFixture):
    create_job = mixed.RecipeMixedMaterialStageTests.create_job
    primary_version = mixed.RecipeMixedMaterialStageTests.primary_version
    write = mixed.RecipeMixedMaterialStageTests.write
    input = staticmethod(mixed.RecipeMixedMaterialStageTests.input)
    configure_variant_sources = mixed.RecipeMixedMaterialStageTests.configure_variant_sources
    variant_anchors = mixed.RecipeMixedMaterialStageTests.variant_anchors
    select = mixed.RecipeMixedMaterialStageTests.select
    setUp = mixed.RecipeMixedMaterialStageTests.setUp
    tearDown = mixed.RecipeMixedMaterialStageTests.tearDown
    allow_calls = mixed.RecipeMixedMaterialStageTests.allow_calls
    advance = mixed.RecipeMixedMaterialStageTests.advance
    fill_fields = mixed.RecipeMixedMaterialStageTests.fill_fields
    collect = mixed.RecipeMixedMaterialStageTests.collect

    def configure_declaration(self, declaration):
        mixed.RecipeMixedMaterialStageTests.configure_declaration(self, declaration)
        declaration["routing"]["variant_count"] = 2

    def untouched_variants(self):
        self.advance(supply_fields=True)
        first, second = self.collect(), self.collect(color="blue")
        self.advance()
        selected = ArtifactSelectionDecision.objects.get()
        self.alternative = second if selected.artifact_id == first.pk else first
        self.original = selected
        self.primary_unit = selected.artifact.generation_attempt.preparation.unit_key
        self.parents = [prepare_recipe_media_unit(stage_run_id=self.stage.pk,
            expected_run_plan_sha256=self.plan.sha256, scope=self.scope, candidate_number=1,
            actor=self.user, material_unit_key=f"appearance:{setting.variant_id}") for setting in self.settings]
        self.baseline = {row.pk: deepcopy(read_execution_preparation_detail(row).resolved_inputs) for row in self.parents}
        self.assertFalse(GenerationAttempt.objects.filter(preparation__in=self.parents).exists())

    def change(self, **kwargs):
        return change_recipe_primary_and_reprepare(**{
            "stage_run_id": self.stage.pk, "unit_key": self.primary_unit, "actor": self.user,
            "artifact_id": self.alternative.pk, "expected_decision_id": str(self.original.pk),
            "expected_preparation_ids": [str(row.pk) for row in self.parents], **kwargs})

    def public_context(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/material-selection-context/",
            {"artifact_id": str(self.alternative.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def public_change(self, context):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/", {
            "artifact_id": str(self.alternative.pk), "material_selection": {
                "expected_decision_id": context["expected_decision_id"],
                "expected_dependencies_sha256": context["dependencies_sha256"]}}, format="json", HTTP_IDEMPOTENCY_KEY="change-primary")

    def test_public_mixed_completed_and_untouched_preserves_old_basis_and_continues(self):
        from studio.services.execution_advancement import advance_execution_preparation
        from studio.services.asset_references import read_generated_material
        self.untouched_variants()
        parent = self.parents[0]
        self.fill_fields(parent)
        parent.refresh_from_db()
        advance_execution_preparation(preparation_id=parent.pk,
            expected_preparation_revision=parent.revision, actor=self.user)
        pending = self.public_context()
        self.assertFalse(pending["can_select"])
        self.assertIn("blocked", [row["action"] for row in pending["dependent_preparations"]])
        self.assertEqual(self.public_change(pending).status_code, 400)
        completed = self.collect()
        self.assertEqual(completed.generation_attempt.preparation_id, parent.pk)
        context = self.public_context()
        self.assertTrue(context["can_select"])
        self.assertEqual({row["action"] for row in context["dependent_preparations"]}, {"retain", "reprepare"})
        self.assertNotEqual(context["dependencies_sha256"], pending["dependencies_sha256"])
        self.assertEqual(self.public_change(pending).status_code, 409)
        prior = list(GenerationAttempt.objects.values())
        old_request = ExecutionPreparation.objects.values().get(pk=parent.pk)
        result = self.public_change(context)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(list(GenerationAttempt.objects.values()), prior)
        self.assertEqual(ExecutionPreparation.objects.values().get(pk=parent.pk), old_request)
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 1)
        self.assertEqual(self.public_change(context).status_code, 200)  # Same unknown-response replay.
        self.alternative.refresh_from_db()
        new_primary = read_generated_material(artifact=self.alternative)
        self.advance(supply_fields=True)
        fresh = self.collect()
        self.advance()
        completed.refresh_from_db()
        fresh.refresh_from_db()
        old_primary_id = self.baseline[parent.pk]["recipe_material_binding"]["primary"]["version_id"]
        self.assertEqual(str(read_generated_material(artifact=completed).generated_from_id), old_primary_id)
        self.assertEqual(read_generated_material(artifact=fresh).generated_from_id, new_primary.pk)
        self.assertEqual(GenerationAttempt.objects.count(), len(prior) + 1)
        after = self.public_context()
        self.assertFalse(after["can_select"])  # Already chosen, never force another replacement.
        self.assertEqual(len(after["dependent_preparations"]), 2)  # Explicit leaf, no cancelled parent.
        self.assertEqual({row["action"] for row in after["dependent_preparations"]}, {"retain"})

    def test_public_rechecks_revision_and_rejects_missing_or_forged_impact(self):
        self.untouched_variants()
        context = self.public_context()
        self.assertTrue(context["can_select"])
        self.assertEqual({row["action"] for row in context["dependent_preparations"]}, {"reprepare"})
        self.assertEqual(self.public_change({**context, "dependencies_sha256": "0" * 64}).status_code, 409)
        self.assertEqual(self.public_change({**context, "dependencies_sha256": "invalid"}).status_code, 400)
        missing = self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/", {
            "artifact_id": str(self.alternative.pk),
            "material_selection": {"expected_decision_id": context["expected_decision_id"]}}, format="json", HTTP_IDEMPOTENCY_KEY="missing-dependencies")
        self.assertEqual(missing.status_code, 409)
        self.fill_fields(self.parents[0])  # Same IDs, but a changed preparation revision.
        result = self.public_change(context)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertIn("material_dependencies_changed", str(result.data))
        fresh = self.public_context()
        self.assertNotEqual(fresh["dependencies_sha256"], context["dependencies_sha256"])
        result = self.public_change(fresh)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 2)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_atomic_replacement_replays_and_original_worker_uses_new_primary(self):
        from studio.services.asset_references import read_generated_material
        self.untouched_variants()
        old_primary = self.baseline[self.parents[0].pk]["recipe_material_binding"]["primary"]["version_id"]
        calls = GenerationAttempt.objects.count()
        decision = self.change()
        self.alternative.refresh_from_db()
        new_primary = read_generated_material(artifact=self.alternative)
        self.assertNotEqual(str(new_primary.pk), old_primary)
        self.assertNotEqual(self.alternative.sha256, self.original.artifact.sha256)
        self.assertEqual((decision.selection_mode, decision.selected_by_id), ("manual", self.user.pk))
        self.assertEqual(GenerationAttempt.objects.count(), calls)  # No dispatch in confirmation.
        replacements = list(ExecutionPreparation.objects.filter(supersedes__in=self.parents))
        self.assertEqual(len(replacements), 2)
        for row in replacements:
            original = row.supersedes
            self.assertEqual(original.status, "cancelled")
            self.assertEqual(read_execution_preparation_detail(original).resolved_inputs, self.baseline[original.pk])
            value = read_execution_preparation_detail(row).resolved_inputs
            self.assertEqual(value["recipe_material_binding"]["primary"]["version_id"], str(new_primary.pk))
            self.assertEqual(value["recipe_material_binding"]["difference"], self.baseline[original.pk]["recipe_material_binding"]["difference"])
            choice = read_recipe_candidate_route(stage=self.stage, plan=self.plan, declaration=self.declaration,
                unit_key=row.unit_key, candidate_number=1)
            self.assertEqual(choice.preparation.pk, row.pk)
            with self.assertRaisesMessage(ValidationError, "不能重新提交原路线"):
                require_current_recipe_preparation(original, stage=self.stage)
            query = ExecutionPreparation.objects.filter(supersedes_id=original.pk).values("id")
            self.assertIn("INDEX", query.explain())
            with self.assertRaises(ValidationError):
                ExecutionPreparation.objects.filter(pk=row.pk).update(supersedes_id=None)
        self.assertEqual(self.change().pk, decision.pk)
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 2)
        self.advance(supply_fields=True)
        artifacts = [self.collect(), self.collect()]
        self.advance()
        for artifact in artifacts:
            artifact.refresh_from_db()
            self.assertEqual(read_generated_material(artifact=artifact).generated_from_id, new_primary.pk)
        self.assertEqual(GenerationAttempt.objects.filter(preparation__in=self.parents).count(), 0)
        self.assertEqual(GenerationAttempt.objects.count(), calls + 2)

    def test_second_replacement_failure_rolls_back_choice_assets_and_original_states(self):
        self.untouched_variants()
        states = list(ExecutionPreparation.objects.values("id", "revision", "status", "intent_sha256"))
        assets, versions = Asset.objects.count(), AssetReferenceVersion.objects.count()
        files = {path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        invoked = []
        def prepare(**kwargs):
            invoked.append(kwargs["supersedes"].pk)
            if len(invoked) == 2:
                raise RuntimeError("isolated second replacement failure")
            return prepare_recipe_media_unit(**kwargs)
        with patch("studio.services.production_recipe_media_preparations.prepare_recipe_media_unit", side_effect=prepare):
            with self.assertRaisesMessage(RuntimeError, "isolated second replacement failure"):
                self.change()
        self.assertEqual(len(invoked), 2)
        self.assertEqual(list(ExecutionPreparation.objects.values("id", "revision", "status", "intent_sha256")), states)
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (assets, versions))
        self.assertEqual({path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}, files)
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, self.original.pk)
        for row in self.parents:
            self.assertEqual(read_execution_preparation_detail(row).resolved_inputs, self.baseline[row.pk])

    def test_stale_impact_claim_and_actual_calls_cannot_be_replaced(self):
        from uuid import uuid4
        self.untouched_variants()
        with self.assertRaises(ProductionRunConflict):
            self.change(expected_preparation_ids=[str(self.parents[0].pk)])
        with self.assertRaises(ProductionRunConflict):
            self.change(expected_preparation_ids=[{}])
        with self.assertRaises(ProductionRunConflict):
            self.change(expected_decision_id=str(uuid4()))
        with self.assertRaises(PermissionDenied):
            self.change(actor=None)
        parent = self.parents[0]
        ExecutionPreparation.objects.filter(pk=parent.pk).update(claim_token=uuid4(), claim_until=timezone.now())
        with self.assertRaisesMessage(ValidationError, "工作进程占用"):
            self.change()
        ExecutionPreparation.objects.filter(pk=parent.pk).update(claim_token=None, claim_until=None)
        self.advance(supply_fields=True)  # Original queue now owns the variant calls.
        with self.assertRaisesMessage(ValidationError, "未执行"):
            self.change()
        self.assertFalse(ExecutionPreparation.objects.filter(supersedes__isnull=False).exists())
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, self.original.pk)

    def test_replaced_empty_parents_do_not_block_actual_failed_variant_recovery(self):
        from studio.services.production_recipe_material_fulfillment import _require_replaceable_preparations
        from studio.services.production_recipe_material_units import frozen_recipe_material_units
        self.untouched_variants()
        self.change()
        units, _ = frozen_recipe_material_units(self.stage, self.plan)
        variants = [unit for unit in units if unit["appearance_role"] == "variant"]
        for unit in variants:
            with self.assertRaisesMessage(ValidationError, "全部调用明确失败"):
                _require_replaceable_preparations(self.stage, self.plan, self.declaration, unit)
        self.advance(supply_fields=True)
        self.collect(fail=True)
        self.collect(fail=True)
        for unit in variants:
            _require_replaceable_preparations(self.stage, self.plan, self.declaration, unit)
        self.assertFalse(GenerationAttempt.objects.filter(preparation__in=self.parents).exists())
        for parent in self.parents:
            self.assertEqual(read_execution_preparation_detail(parent).resolved_inputs, self.baseline[parent.pk])


class ReplacementChainShapeTests(SimpleTestCase):
    def leaf(self, links):
        from studio.services.production_recipe_material_repreparation import current_replacement_leaf
        rows = [SimpleNamespace(pk=pk, supersedes_id=parent, stage_run_id=1, unit_key="unit", request_key="request")
            for pk, parent in links]
        detail = SimpleNamespace(intent={"request": {"requested": {}}})
        with patch("studio.services.production_recipe_material_repreparation.read_execution_preparation_detail", return_value=detail), patch(
                "studio.services.production_recipe_material_repreparation.require_replacement_parent"):
            return current_replacement_leaf(rows).pk

    def test_explicit_links_not_row_order_define_current_leaf(self):
        self.assertEqual(self.leaf([(3, 2), (1, None), (2, 1)]), 3)

    def test_missing_parent_and_fork_are_not_silently_ignored(self):
        for links in ([(2, 1)], [(1, None), (2, 1), (3, 1)]):
            with self.subTest(links=links), self.assertRaises(ValidationError):
                self.leaf(links)

    def test_multiple_roots_and_cycle_are_rejected(self):
        for links in ([(1, None), (2, None)], [(1, 2), (2, 1)], [(1, None), (2, 3), (3, 2)]):
            with self.subTest(links=links), self.assertRaises(ValidationError):
                self.leaf(links)
