"""Original review writer consumes real isolated reused/uploaded image choices.

Only frozen policy authorization is substituted, as in the existing policy
writer tests. No model transport, live database or operational approval runs.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import override_settings

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, Asset, AssetReferenceVersion,
    ExecutionPreparation, GeneratedArtifact, GenerationAttempt, StageRun)
from studio.services.production_recipe_material_review import material_review_context
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.services.production_runs import (
    StageBlocked, _StoredAssetFileGuard, _approve_bible_image_bindings,
    _assert_frozen_formal_objects, _execute_unattended_policy_review, _formal_snapshot, _save_stage_snapshot,
)
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_fulfillment_choices as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialReviewTests(StructureFixture):
    Fixture = fixtures.RecipeMaterialFulfillmentChoiceTests
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    facts = Fixture.facts
    attach = Fixture.attach
    choose = Fixture.choose
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    target = Fixture.target
    prepare = Fixture.prepare

    def configure_declaration(self, declaration):
        if self._testMethodName.startswith("test_frozen_scope_"):
            from studio.models import CharacterBible
            self.outside = CharacterBible.objects.create(project=self.project,
                name="No appearance requirement", description="Retained project entity",
                stable_speaker_id="unrequested-entity", slug="unrequested-entity")
        source = "upload" if "generic_upload" in self._testMethodName else "existing"
        declaration["material_sources"] = {"primary": source, "variant": source}
        declaration.pop("routing")
        declaration.pop("variant_routing")

    def complete(self, *, upload=False, same_asset=False):
        pairs = [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]
        choices = []
        for index, (setting, series) in enumerate(pairs):
            if upload:
                response = self.client.post("/api/v1/assets/", {
                    "name": f"isolated-upload-{index}", "kind": "reference", "file": image_upload()}, format="multipart")
                self.assertEqual(response.status_code, 201, response.data)
                asset_id, source = response.data["id"], "upload"
            else:
                asset_id, source = self.assets[0 if same_asset else index % len(self.assets)].pk, "existing"
            choices.append(self.choose(setting=setting, series=series, asset_id=asset_id, source=source)[0])
        result = advance_recipe_media_stage(self.run, self.stage)
        self.assertEqual(result, "material-fulfillments:3")
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, result))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        self.review = StageRun.objects.create(production_run=self.run, stage_key="bible_image_review",
            ordinal=self.stage.ordinal + 1, mode="review", status="running", idempotency_key=str(uuid4()))
        return choices

    def execute_policy_review(self):
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["bible_image_review"]
        with transaction.atomic(), patch("studio.services.production_runs._verified_unattended_decision_policy",
                return_value=(policy, "f" * 64)):
            return _execute_unattended_policy_review(self.run, self.review)

    def selection(self):
        context = material_review_context(self.run, self.stage.output_snapshot["canonical"])
        row = context[("character", str(self.character.pk))]
        return {"entity_type": "character", "entity_id": str(self.character.pk), "selected_artifact_ids": [],
            "selected_library_assets": [{"asset_id": asset_id, "expected_sha256": fact["checksum"]}
                for asset_id, fact in sorted(row["assets"].items())],
            "anchor_artifact_id": None, "anchor_library_asset_id": row["primary_asset_id"]}

    def approve(self, selection=None):
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            _approve_bible_image_bindings(self.run, self.review, self.user, bindings=[selection or self.selection()],
                expected_source_snapshot_sha256=self.stage.output_snapshot_sha256, asset_guard=guard)

    def assert_no_generation(self):
        for model in (ExecutionPreparation, GenerationAttempt, GeneratedArtifact, ArtifactSelectionDecision):
            self.assertFalse(model.objects.exists(), model.__name__)

    def check_frozen_scope_review(self, *, automatic):
        from studio.models import CharacterBible
        from studio.services.production_runs import _assert_stage_output_integrity
        original = CharacterBible.objects.filter(pk=self.outside.pk).values().get()
        self.complete()
        canonical = self.stage.output_snapshot["canonical"]
        self.assertEqual([(row["entity_type"], row["entity_id"]) for row in canonical["entities"]],
            [("character", str(self.character.pk))])
        _assert_stage_output_integrity(self.run, self.stage)
        if automatic:
            self.execute_policy_review()
            self.review.refresh_from_db()
            # Policy execution already saved its formal snapshot.
            binding = self.review.output_snapshot["canonical"]["bindings"][0]
        else:
            self.approve()
            self.review.refresh_from_db()
            _assert_stage_output_integrity(self.run, self.review)
            binding = self.review.output_snapshot["bindings"][0]
            _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review, "isolated-review"))
        self.assertEqual(binding["entity_id"], str(self.character.pk))
        self.review.status = "succeeded"
        self.review.save(update_fields=["status"])
        _assert_frozen_formal_objects(self.run)
        self.assertEqual(CharacterBible.objects.filter(pk=self.outside.pk).values().get(), original)
        self.assert_no_generation()

    def test_frozen_scope_policy_review_does_not_require_unrequested_project_entities(self):
        self.check_frozen_scope_review(automatic=True)

    def test_frozen_scope_manual_review_does_not_write_unrequested_project_entities(self):
        self.check_frozen_scope_review(automatic=False)

    def test_unprepared_variants_prevent_completion_but_not_candidate_preview(self):
        from studio.services.production_runs import _assert_stage_output_integrity, _canonical_bible_image_candidates
        from studio.services.production_recipe_material_review import require_complete_recipe_material_results
        self.choose(setting=self.primary_setting, series=self.primary.series)
        self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
        self.assertEqual(self.stage.reason_code, "waiting_material_source")
        preview = _canonical_bible_image_candidates(self.run, self.stage)
        self.assertEqual(len(preview["material_fulfillments"]), 1)
        self.assertFalse(ExecutionPreparation.objects.exists())
        before = self.facts()
        for check in (
            lambda: _assert_stage_output_integrity(self.run, self.stage),
            lambda: _formal_snapshot(self.run, self.stage, "material-fulfillments:1"),
            lambda: require_complete_recipe_material_results(self.stage, preview),
        ):
            with self.assertRaisesMessage(ValidationError, "尚未全部选择并登记"):
                check()
        self.assertEqual(self.facts(), before)
        self.complete()
        _assert_stage_output_integrity(self.run, self.stage)
        self.assert_no_generation()

    def test_zero_call_completion_then_policy_review_keeps_exact_versions_and_actor(self):
        choices = self.complete()
        assets_before = list(Asset.objects.order_by("pk").values())
        versions_before = list(AssetReferenceVersion.objects.order_by("pk").values())
        files = {str(path): path.read_bytes() for path in self.root.rglob("*.png")}
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No copied Asset")):
            result = self.execute_policy_review()
        decision = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual(result, f"approval:{decision.pk}")
        self.assertEqual((decision.actor, decision.decided_by_id), ("recipe_policy", None))
        self.review.refresh_from_db()
        payload = self.review.output_snapshot["canonical"]
        self.assertEqual(payload["material_fulfillments"], self.stage.output_snapshot["canonical"]["material_fulfillments"])
        binding = payload["bindings"][0]
        self.assertEqual(binding["anchor_asset_id"], str(choices[0].reference_version.asset_id))
        self.assertEqual(binding["selected_artifact_ids"], [])
        refs = [reference for fact in [binding["anchor_asset"], *binding["reference_assets"]]
            for reference in fact["material_fulfillments"]]
        self.assertEqual({row["fulfillment_id"] for row in refs}, {str(row.pk) for row in choices})
        self.assertEqual(self.review.output_snapshot["policy_evidence"]["selected_artifact_ids"], [])
        self.assertEqual(assets_before, list(Asset.objects.order_by("pk").values()))
        self.assertEqual(versions_before, list(AssetReferenceVersion.objects.order_by("pk").values()))
        self.assertEqual(files, {str(path): path.read_bytes() for path in self.root.rglob("*.png")})
        self.assert_no_generation()
        self.review.status = "succeeded"
        self.review.save(update_fields=["status"])
        _assert_frozen_formal_objects(self.run)

    def test_formal_generic_upload_needs_no_category_rewrite_or_fabricated_generation(self):
        # Source configuration differs, not the Asset's provenance/category.
        choices = self.complete(upload=True)
        canonical = self.stage.output_snapshot["canonical"]
        displayed = canonical["entities"][0]["fulfilled_materials"]
        self.assertEqual(len(displayed), 3)
        self.assertEqual([item["reference"] for item in displayed], canonical["material_fulfillments"])
        by_version = {str(choice.reference_version_id): choice.reference_version for choice in choices}
        for item in displayed:
            version = by_version[item["reference"]["reference_version_id"]]
            self.assertEqual((item["asset_id"], item["checksum"], item["mime_type"], item["asset_name"]),
                (str(version.asset_id), version.asset.checksum, version.asset.mime_type, version.asset.name))
            self.assertEqual(item["appearance_role"], version.series.role)
        self.execute_policy_review()
        self.character.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, choices[0].reference_version.asset_id)
        for choice in choices:
            choice.reference_version.refresh_from_db()
            self.assertEqual(choice.reference_version.source, "upload")
            self.assertIsNone(choice.reference_version.generated_from_id)
            self.assertEqual(choice.reference_version.asset.kind, "reference")
            self.assertNotIn("reference_category", choice.reference_version.asset.metadata)
        self.assert_no_generation()

    def test_manual_review_cannot_replace_primary_or_drop_fulfilled_variants(self):
        self.complete()
        selection = self.selection()
        changed = deepcopy(selection)
        changed["anchor_library_asset_id"] = next(row["asset_id"] for row in changed["selected_library_assets"]
            if row["asset_id"] != changed["anchor_library_asset_id"])
        dropped = deepcopy(selection)
        dropped["selected_library_assets"] = [row for row in dropped["selected_library_assets"]
            if row["asset_id"] == dropped["anchor_library_asset_id"]]
        for invalid in (changed, dropped):
            with self.subTest(selection=invalid), self.assertRaises(ValidationError):
                self.approve(invalid)
        self.approve(selection)
        self.assertEqual(self.review.output_snapshot["material_fulfillments"],
            self.stage.output_snapshot["canonical"]["material_fulfillments"])
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assert_no_generation()

    def test_changed_file_stale_manifest_and_unreviewed_setting_do_not_bind(self):
        choices = self.complete()
        before = deepcopy(self.stage.output_snapshot)
        corrupted = deepcopy(before)
        corrupted["canonical"]["material_fulfillments"].pop()
        from studio.services.production_runs import _sha256
        corrupted["canonical_sha256"] = _sha256(corrupted["canonical"])
        _save_stage_snapshot(self.stage, "output_snapshot", corrupted)
        with self.assertRaises(StageBlocked):
            self.execute_policy_review()
        _save_stage_snapshot(self.stage, "output_snapshot", before)
        path = Path(choices[0].reference_version.asset.file.path)
        original = path.read_bytes()
        path.write_bytes(b"isolated corrupt image")
        with self.assertRaises((ValidationError, StageBlocked)):
            self.execute_policy_review()
        path.write_bytes(original)
        from studio.services.bible_variants import revise_variant, setting_payload
        revise_variant(actor=self.user, variant=self.primary_setting.variant, expected_revision=1,
            value={**setting_payload(self.primary_setting), "appearance_anchor": "Changed setting"})
        with self.assertRaises((ValidationError, StageBlocked)):
            self.execute_policy_review()
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assert_no_generation()

    def test_receipt_failure_rolls_back_binding_but_preserves_original_choices(self):
        self.complete()
        original = ApprovalDecision.save
        def fail(row, *args, **kwargs):
            original(row, *args, **kwargs)
            if row.stage_run_id == self.review.pk:
                raise RuntimeError("isolated review receipt failed")
        with patch.object(ApprovalDecision, "save", fail), self.assertRaisesMessage(RuntimeError, "receipt failed"):
            self.execute_policy_review()
        self.character.refresh_from_db()
        self.review.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(self.character.reference_assets.exists())
        self.assertEqual(self.review.output_snapshot, {})
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertEqual(len(material_review_context(self.run, self.stage.output_snapshot["canonical"])), 1)
        self.execute_policy_review()
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 1)
        self.assert_no_generation()

    def test_same_asset_for_multiple_exact_requirements_keeps_each_receipt(self):
        choices = self.complete(same_asset=True)
        self.execute_policy_review()
        self.review.refresh_from_db()
        binding = self.review.output_snapshot["canonical"]["bindings"][0]
        self.assertEqual(len(binding["selected_asset_ids"]), 1)
        self.assertEqual(binding["reference_asset_ids"], [])
        self.assertEqual(len(binding["anchor_asset"]["material_fulfillments"]), len(choices))
        self.assert_no_generation()

    def test_completed_review_rechecks_original_file_and_does_not_rewrite_evidence(self):
        choices = self.complete()
        self.execute_policy_review()
        self.review.refresh_from_db()
        self.review.status = "succeeded"
        self.review.save(update_fields=["status"])
        original = deepcopy(self.review.output_snapshot)
        original_sha = self.review.output_snapshot_sha256
        path = Path(choices[-1].reference_version.asset.file.path)
        path.write_bytes(b"isolated post-review file drift")
        with self.assertRaises((ValidationError, StageBlocked)):
            _assert_frozen_formal_objects(self.run)
        self.review.refresh_from_db()
        self.assertEqual((self.review.output_snapshot, self.review.output_snapshot_sha256), (original, original_sha))
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 1)
        self.assert_no_generation()
