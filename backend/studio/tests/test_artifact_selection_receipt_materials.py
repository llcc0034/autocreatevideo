"""Receipt rollback through original material/structured-text selection writers.

Reuse isolated published contracts, temporary PNGs and controlled transports.
Public-route tests exercise DRF integration, not live model or assembly acceptance.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from studio.models import (Asset, AssetReferenceVersion, ApprovalDecision, ArtifactSelectionDecision,
    ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ProductionManualCommand)
from studio.services.production_artifact_selection_receipts import select_run_artifact_command
from studio.services.production_recipe_material_selection import recipe_material_selection_context
from studio.services.production_structured_text_selection import choose_structured_text_candidate
from studio.tests import test_data_structures as structures
from studio.tests import test_recipe_material_repreparation as materials
from studio.tests import test_structured_text_selection as text_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class MaterialSelectionReceiptTests(structures.StructureFixture):
    create_job = materials.RecipeMaterialRepreparationTests.create_job
    primary_version = materials.RecipeMaterialRepreparationTests.primary_version
    write = materials.RecipeMaterialRepreparationTests.write
    input = staticmethod(materials.RecipeMaterialRepreparationTests.input)
    configure_variant_sources = materials.RecipeMaterialRepreparationTests.configure_variant_sources
    variant_anchors = materials.RecipeMaterialRepreparationTests.variant_anchors
    select = materials.RecipeMaterialRepreparationTests.select
    setUp = materials.RecipeMaterialRepreparationTests.setUp
    tearDown = materials.RecipeMaterialRepreparationTests.tearDown
    allow_calls = materials.RecipeMaterialRepreparationTests.allow_calls
    advance = materials.RecipeMaterialRepreparationTests.advance
    fill_fields = materials.RecipeMaterialRepreparationTests.fill_fields
    collect = materials.RecipeMaterialRepreparationTests.collect
    configure_declaration = materials.RecipeMaterialRepreparationTests.configure_declaration
    untouched_variants = materials.RecipeMaterialRepreparationTests.untouched_variants

    def change(self, *, request_key="primary-change"):
        context = recipe_material_selection_context(artifact=self.alternative, actor=self.user)
        return select_run_artifact_command(self.run, self.alternative, actor=self.user, request_key=request_key,
            material_selection={"expected_decision_id": self.original.pk,
                "expected_dependencies_sha256": context["dependencies_sha256"]})

    # Run the original second-replacement failure through the new receipt
    # wrapper; asserts rollback of real decisions, versions, files and parents.
    def test_repreparation_failure_rolls_back_writer_and_receipt(self):
        materials.RecipeMaterialRepreparationTests.test_second_replacement_failure_rolls_back_choice_assets_and_original_states(self)
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_receipt_failure_after_repreparation_removes_only_new_files_and_can_retry(self):
        self.untouched_variants()
        context = recipe_material_selection_context(artifact=self.alternative, actor=self.user)
        body = {"expected_decision_id": self.original.pk,
            "expected_dependencies_sha256": context["dependencies_sha256"]}
        states = list(ExecutionPreparation.objects.values())
        calls = list(GenerationAttempt.objects.values())
        approvals = list(ApprovalDecision.objects.values())
        assets, versions = Asset.objects.count(), AssetReferenceVersion.objects.count()
        files = {path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        def command(key="primary-change"):
            return select_run_artifact_command(self.run, self.alternative, actor=self.user,
                request_key=key, material_selection=deepcopy(body))
        original_save = ProductionManualCommand.save
        def fail_after_writer(receipt, *args, **kwargs):
            self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 2)
            self.assertGreater(AssetReferenceVersion.objects.count(), versions)
            self.assertGreater(Asset.objects.count(), assets)
            original_save(receipt, *args, **kwargs)
            raise RuntimeError("isolated receipt write failure")
        with patch.object(ProductionManualCommand, "save", fail_after_writer):
            with self.assertRaisesMessage(RuntimeError, "isolated receipt write failure"):
                command()
        self.assertEqual(list(ExecutionPreparation.objects.values()), states)
        self.assertEqual(list(GenerationAttempt.objects.values()), calls)
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (assets, versions))
        self.assertEqual({path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}, files)
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, self.original.pk)
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.alternative.refresh_from_db()
        self.assertIsNone(self.alternative.asset_id)
        result = command()
        post = list(ExecutionPreparation.objects.values())
        with patch("studio.services.production_runs.select_run_artifact", side_effect=AssertionError("no repeat writer")):
            self.assertEqual(command(), result)
        self.assertEqual(list(ExecutionPreparation.objects.values()), post)
        # UUID from validated_data and its HTTP string denote the same input.
        body["expected_decision_id"] = str(body["expected_decision_id"])
        self.assertEqual(command(), result)
        new = command("new-material-intent")
        self.assertNotEqual(new["id"], result["id"])
        self.assertEqual(new["target_id"], result["target_id"])
        self.assertEqual(ProductionManualCommand.objects.count(), 2)
        self.assertEqual(list(GenerationAttempt.objects.values()), calls)
        self.original.refresh_from_db()
        self.assertEqual((self.original.selection_mode, self.original.selected_by_id), ("automatic", None))
        self.assertEqual(list(ApprovalDecision.objects.values()), approvals)

    def public_change_request(self, artifact):
        self.client.force_authenticate(self.user)
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/material-selection-context/",
            {"artifact_id": str(artifact.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["can_select"], response.data)
        return {"artifact_id": str(artifact.pk), "material_selection": {
            "expected_decision_id": response.data["expected_decision_id"],
            "expected_dependencies_sha256": response.data["dependencies_sha256"]}}

    def post_change(self, body, key):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/",
            deepcopy(body), format="json", HTTP_IDEMPOTENCY_KEY=key)

    def replacement_facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            ExecutionPreparation, GenerationAttempt, ArtifactSelectionDecision, ProductionManualCommand,
            Asset, AssetReferenceVersion, ApprovalDecision, GeneratedArtifact)}

    def test_two_public_primary_changes_late_receipt_preserves_current_leaf_and_actual_basis(self):
        """A -> B -> A, lost B response: replay cannot create a third generation."""
        from studio.services.asset_references import read_generated_material
        from studio.services.execution_preparations import read_execution_preparation_detail

        self.untouched_variants()
        original_artifact = self.original.artifact
        first_body = self.public_change_request(self.alternative)
        first = self.post_change(first_body, "primary-a-to-b")
        self.assertEqual(first.status_code, 200, first.data)
        first_children = list(ExecutionPreparation.objects.filter(supersedes__in=self.parents))
        self.assertEqual(len(first_children), 2)
        first_inputs = {row.pk: deepcopy(read_execution_preparation_detail(row).resolved_inputs)
            for row in first_children}
        for inputs in first_inputs.values():
            self.assertEqual(inputs["recipe_material_binding"]["primary"]["decision_id"], first.data["target_id"])

        second_body = self.public_change_request(original_artifact)
        second = self.post_change(second_body, "primary-b-to-a")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertNotEqual(second.data["target_id"], first.data["target_id"])
        leaves = list(ExecutionPreparation.objects.filter(supersedes__in=first_children))
        self.assertEqual(len(leaves), 2)
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 4)
        before = self.replacement_facts()
        files = {path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        with patch("studio.services.production_runs.select_run_artifact",
                side_effect=AssertionError("An old receipt must not invoke the selection writer")):
            replay = self.post_change(first_body, "primary-a-to-b")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(self.replacement_facts(), before)
        self.assertEqual({path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*")
            if path.is_file()}, files)
        current = self.public_change_request(self.alternative)["material_selection"]["expected_decision_id"]
        self.assertEqual(current, second.data["target_id"])
        self.assertNotEqual(current, str(self.original.pk))
        original_artifact.refresh_from_db()
        self.alternative.refresh_from_db()
        self.assertEqual((original_artifact.status, self.alternative.status), ("selected", "available"))
        for leaf in leaves:
            self.assertEqual(read_execution_preparation_detail(leaf).resolved_inputs[
                "recipe_material_binding"]["primary"]["decision_id"], second.data["target_id"])

        # Only the second generation of replacement leaves may reach the
        # original queue. The two primary calls already completed above.
        self.advance(supply_fields=True)
        artifacts = [self.collect(), self.collect()]
        self.advance()
        original_artifact.refresh_from_db()
        primary = read_generated_material(artifact=original_artifact)
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        self.assertFalse(GenerationAttempt.objects.filter(preparation__in=[*self.parents, *first_children]).exists())
        self.assertEqual(set(GenerationAttempt.objects.filter(preparation__in=leaves)
            .values_list("preparation_id", flat=True)), {row.pk for row in leaves})
        for artifact in artifacts:
            artifact.refresh_from_db()
            self.assertEqual(read_generated_material(artifact=artifact).generated_from_id, primary.pk)
        for parent in self.parents:
            self.assertEqual(read_execution_preparation_detail(parent).resolved_inputs, self.baseline[parent.pk])
        for child in first_children:
            child.refresh_from_db()
            self.assertEqual(child.status, "cancelled")
            self.assertEqual(read_execution_preparation_detail(child).resolved_inputs, first_inputs[child.pk])
        self.original.refresh_from_db()
        self.assertEqual((self.original.selection_mode, self.original.selected_by_id), ("automatic", None))
        self.assertEqual(ProductionManualCommand.objects.count(), 2)
        self.assertEqual(list(ApprovalDecision.objects.order_by("pk").values()), before["ApprovalDecision"])

    def test_second_public_change_receipt_failure_preserves_first_committed_chain(self):
        self.untouched_variants()
        first_body = self.public_change_request(self.alternative)
        first = self.post_change(first_body, "committed-primary-change")
        self.assertEqual(first.status_code, 200, first.data)
        second_body = self.public_change_request(self.original.artifact)
        before = self.replacement_facts()
        files = {path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        original_save = ProductionManualCommand.save

        def fail_after_save(receipt, *args, **kwargs):
            original_save(receipt, *args, **kwargs)
            self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 4)
            raise RuntimeError("isolated second-generation receipt failure")

        with patch.object(ProductionManualCommand, "save", fail_after_save):
            with self.assertRaisesMessage(RuntimeError, "second-generation receipt failure"):
                self.post_change(second_body, "recover-second-change")
        self.assertEqual(self.replacement_facts(), before)
        self.assertEqual({path: path.read_bytes() for path in Path(settings.MEDIA_ROOT).rglob("*")
            if path.is_file()}, files)
        self.assertEqual(self.post_change(first_body, "committed-primary-change").data, first.data)
        restored = self.post_change(second_body, "recover-second-change")
        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 4)
        self.assertEqual(ProductionManualCommand.objects.count(), 2)
        self.assertEqual(list(GenerationAttempt.objects.order_by("pk").values()), before["GenerationAttempt"])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class StructuredSelectionReceiptTests(TestCase):
    setUpTestData = text_fixtures.StructuredTextSelectionTests.__dict__["setUpTestData"]
    setUp = text_fixtures.StructuredTextSelectionTests.setUp
    node = text_fixtures.StructuredTextSelectionTests.node
    create = text_fixtures.StructuredTextSelectionTests.create
    publish = text_fixtures.StructuredTextSelectionTests.publish
    output = text_fixtures.StructuredTextSelectionTests.output
    process = text_fixtures.StructuredTextSelectionTests.process
    prepare = text_fixtures.StructuredTextSelectionTests.prepare
    queue_fixture = text_fixtures.StructuredTextSelectionTests.queue_fixture
    candidate = text_fixtures.StructuredTextSelectionTests.candidate
    candidate_count = 3
    evaluator = "first_succeeded"

    def test_receipts_preserve_text_same_candidate_and_automatic_provenance(self):
        _, first = self.candidate(1)
        _, second = self.candidate(2)
        automatic = choose_structured_text_candidate(self.steps[0])
        def command(artifact, key):
            return select_run_artifact_command(self.run, artifact, actor=self.actor, request_key=key)
        original = command(first, "first")
        self.assertEqual(original["target_id"], automatic.pk)
        new = command(second, "second")
        self.assertEqual(command(first, "first"), original)
        repeated = command(second, "third")
        self.assertNotEqual(repeated["id"], new["id"])
        self.assertEqual(repeated["target_id"], new["target_id"])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.status, second.status), ("available", "selected"))
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        self.assertEqual(ProductionManualCommand.objects.count(), 3)
        automatic.refresh_from_db()
        self.assertEqual((automatic.selection_mode, automatic.selected_by_id), ("automatic", None))
        self.assertFalse(ApprovalDecision.objects.exists())
