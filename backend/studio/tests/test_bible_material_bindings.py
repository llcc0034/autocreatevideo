"""Exact retained image receipts in isolated Runs, never live adoption/calls."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models.deletion import RestrictedError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, Asset, AssetReferenceVersion, CharacterBible, ProductionRun, StageRun
from studio.services.bible_material_bindings import record_binding_review, reviewed_binding
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_runs import (
    ProductionRunConflict, StageBlocked, _StoredAssetFileGuard, _apply_bible_image_bindings,
    _bible_binding_asset_fact, _partition_current_preserved_bible_assets, _save_stage_snapshot, _sha256,
)
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_review as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class BibleMaterialBindingTests(StructureFixture):
    Fixture = fixtures.RecipeMaterialReviewTests
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
    configure_declaration = Fixture.configure_declaration
    target = Fixture.target
    prepare = Fixture.prepare
    complete = Fixture.complete
    execute_policy_review = Fixture.execute_policy_review
    assert_no_generation = Fixture.assert_no_generation

    def adopted(self):
        self.choices = self.complete(upload="generic_upload" in self._testMethodName)
        self.execute_policy_review()
        self.character.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(self.character.image_binding_review_id, self.review.pk)
        return self.review.output_snapshot["canonical"]["bindings"][0]

    def read_fact(self, asset):
        return _bible_binding_asset_fact(asset, project=self.project, entity_type="character", entity=self.character)

    def test_generic_upload_retained_receipt_is_exact_indexed_and_read_only(self):
        binding = self.adopted()
        # No latest review/AssetReferenceVersion search, even with an unrelated later StageRun.
        StageRun.objects.create(production_run=self.run, stage_key="delivery", ordinal=99,
            mode="manual", status="pending", idempotency_key=str(uuid4()))
        before = (list(Asset.objects.values()), list(AssetReferenceVersion.objects.values()), self.facts())
        for fact in [binding["anchor_asset"], *binding["reference_assets"]]:
            self.assertEqual(self.read_fact(Asset.objects.get(pk=fact["asset_id"])), fact)
        self.character.refresh_from_db()
        with CaptureQueriesContext(connection) as captured:
            reviewed_binding(self.character, "character")
        sql = next(row["sql"] for row in captured if 'FROM "studio_stagerun"' in row["sql"])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql)
            explanation = str(cursor.fetchall())
        self.assertIn("INDEX", explanation)
        self.assertNotIn("SCAN", explanation)
        self.assertEqual(before, (list(Asset.objects.values()), list(AssetReferenceVersion.objects.values()), self.facts()))
        self.assert_no_generation()

    def test_generic_upload_second_round_writer_keeps_original_versions_without_review_chain(self):
        original = self.adopted()
        original_snapshot = deepcopy(self.review.output_snapshot)
        before = list(AssetReferenceVersion.objects.values())
        catalog = production_stage_catalog()
        next_run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="awaiting_input", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        freeze_run_execution_plan(run=next_run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256,
            actor=self.user)
        next_review = StageRun.objects.create(production_run=next_run, stage_key="bible_image_review",
            ordinal=3, mode="manual", status="pending", idempotency_key=str(uuid4()))
        selection = {"entity_type": "character", "entity_id": str(self.character.pk), "selected_artifact_ids": [],
            "selected_library_assets": [{"asset_id": original["anchor_asset_id"], "expected_sha256": original["anchor_asset"]["checksum"]}],
            "anchor_artifact_id": None, "anchor_library_asset_id": original["anchor_asset_id"]}
        canonical = {"entities": [{"entity_type": "character", "entity_id": str(self.character.pk), "candidates": []}]}
        with transaction.atomic(), _StoredAssetFileGuard() as guard:
            rows = _apply_bible_image_bindings(next_run, next_review, self.user, bindings=[selection],
                canonical=canonical, asset_guard=guard)
            # Original shared writer receipt; no invented ApprovalDecision or producer success.
            _save_stage_snapshot(next_review, "output_snapshot", {"kind": "manual_bible_image_review", "bindings": rows})
            record_binding_review(next_review, rows)
        self.character.refresh_from_db()
        self.assertEqual(self.character.image_binding_review_id, next_review.pk)
        for key in ("anchor_asset", "reference_assets"):
            self.assertEqual(rows[0][key], original[key])
        # Flattened original choice references do not need the previous review to read.
        StageRun.objects.filter(pk=self.review.pk).update(output_snapshot_sha256="0" * 64)
        for fact in [rows[0]["anchor_asset"], *rows[0]["reference_assets"]]:
            self.assertEqual(self.read_fact(Asset.objects.get(pk=fact["asset_id"])), fact)
        self.review.refresh_from_db()
        self.assertEqual(self.review.output_snapshot, original_snapshot)
        self.assertEqual(list(AssetReferenceVersion.objects.values()), before)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=next_review).exists())
        self.assert_no_generation()

    def test_corrupt_receipt_or_exact_reference_fails_without_library_fallback(self):
        self.adopted()
        before = deepcopy(self.review.output_snapshot)
        cases = []
        for change in ("entity", "reference", "format"):
            value = deepcopy(before)
            binding = value["canonical"]["bindings"][0]
            if change == "entity":
                binding["entity_id"] = str(uuid4())
            elif change == "reference":
                binding["anchor_asset"]["material_fulfillments"][0]["reference_version_id"] = str(uuid4())
            else:
                binding["reference_assets"] = None
            value["canonical_sha256"] = _sha256(value["canonical"])
            cases.append(value)
        for value in cases:
            _save_stage_snapshot(self.review, "output_snapshot", value)
            self.character.refresh_from_db()
            with self.assertRaises(ValidationError):
                self.read_fact(self.character.anchor_asset)
        _save_stage_snapshot(self.review, "output_snapshot", before)
        self.character.refresh_from_db()
        self.assertEqual(self.read_fact(self.character.anchor_asset), before["canonical"]["bindings"][0]["anchor_asset"])

    def test_stale_setting_partitions_links_but_broken_files_are_not_staleness(self):
        self.adopted()
        assets = {str(row.reference_version.asset_id): row.reference_version.asset for row in self.choices}
        asset = self.character.anchor_asset
        path = Path(asset.file.path)
        content = path.read_bytes()
        path.write_bytes(b"isolated corrupted image")
        with self.assertRaises((ValidationError, StageBlocked)):
            _partition_current_preserved_bible_assets(self.run, entity=self.character, entity_type="character", assets=assets)
        path.write_bytes(content)
        revise_variant(actor=self.user, variant=self.primary_setting.variant, expected_revision=1,
            value={**setting_payload(self.primary_setting), "appearance_anchor": "A revised primary"})
        with self.assertRaises(ProductionRunConflict):
            self.read_fact(asset)
        current, removed = _partition_current_preserved_bible_assets(self.run, entity=self.character, entity_type="character", assets=assets)
        self.assertEqual(current, {})
        self.assertEqual(set(removed), set(assets))
        self.assertEqual(path.read_bytes(), content)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.character.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, asset.pk)  # partition itself is read-only

    def test_formal_partial_unbind_retains_receipt_full_unbind_clears_and_input_cannot_forge(self):
        self.adopted()
        url = f"/api/v1/characters/{self.character.pk}/"
        self.assertNotIn("image_binding_review", self.client.get(url).data)
        response = self.client.patch(url, {"image_binding_review": str(self.review.pk)}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(url, {"is_locked": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(url, {"reference_assets": []}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.character.refresh_from_db()
        self.assertEqual(self.character.image_binding_review_id, self.review.pk)
        with transaction.atomic(), self.assertRaises(RestrictedError):
            self.review.delete()
        response = self.client.patch(url, {"anchor_asset": None}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.character.refresh_from_db()
        self.assertIsNone(self.character.image_binding_review_id)
        self.assertEqual(AssetReferenceVersion.objects.filter(pk__in=[choice.reference_version_id for choice in self.choices]).count(), 3)

    def test_receipt_failure_rolls_back_new_pointer(self):
        self.complete()
        with patch.object(ApprovalDecision, "save", side_effect=RuntimeError("isolated receipt failure")), self.assertRaises(RuntimeError):
            self.execute_policy_review()
        self.character.refresh_from_db()
        self.assertIsNone(self.character.image_binding_review_id)
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(self.character.reference_assets.exists())
        self.assert_no_generation()

    def test_generic_upload_later_manual_endpoint_preserves_versions_and_can_reopen_stale_links(self):
        original = self.adopted()
        catalog = production_stage_catalog()
        run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="awaiting_input", idempotency_key=str(uuid4()), request_sha256="b" * 64)
        for field in ("config_snapshot", "config_snapshot_schema_version", "config_snapshot_size_bytes", "config_snapshot_sha256"):
            setattr(run, field, deepcopy(getattr(self.run, field)))
        run.save()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        stages = {}
        for row in catalog.value["stages"]:
            if row["key"] in {"bible_image_generation", "bible_image_review"}:
                stages[row["key"]] = StageRun.objects.create(production_run=run, stage_key=row["key"],
                    ordinal=row["ordinal"], mode="manual", status="pending", idempotency_key=str(uuid4()))
        read_url = f"/api/v1/production-runs/{run.pk}/manual-bible-image-review/"
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.data
        binding = {"entity_type": "character", "entity_id": str(self.character.pk), "selected_artifact_ids": [],
            "selected_library_assets": [{"asset_id": original["anchor_asset_id"], "expected_sha256": original["anchor_asset"]["checksum"]}],
            "anchor_artifact_id": None, "anchor_library_asset_id": original["anchor_asset_id"]}
        request = {"action": "approve", "stage_key": "bible_image_review", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": 0, "idempotency_key": str(uuid4()), "payload": {
                "expected_candidates_sha256": detail["candidates_sha256"], "expected_targets_sha256": detail["targets_sha256"],
                "expected_content_basis_sha256": detail["content_basis_sha256"], "bindings": [binding], "comment": "Isolated manual review"}}
        response = self.client.post(f"/api/v1/production-runs/{run.pk}/manual-actions/", request, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.character.refresh_from_db()
        self.assertEqual(self.character.image_binding_review_id, stages["bible_image_review"].pk)
        self.assertEqual(self.read_fact(self.character.anchor_asset), original["anchor_asset"])
        self.assertEqual(self.client.get(read_url).data["approval"]["status"], "current")
        stages["bible_image_generation"].refresh_from_db()
        self.assertEqual(stages["bible_image_generation"].status, "pending")
        revise_variant(actor=self.user, variant=self.primary_setting.variant, expected_revision=1,
            value={**setting_payload(self.primary_setting), "appearance_anchor": "Changed after adoption"})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["approval"]["status"], "changed")
        self.assert_no_generation()
