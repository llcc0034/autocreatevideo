"""Real isolated files and the original Asset writer; no model or live data."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase, override_settings

from studio.models import Asset, ProductionRun, StageRun
from studio.services.production_runs import (
    ProductionRunConflict, StageBlocked, _StoredAssetFileGuard, _bible_binding_asset_fact, _register_bible_candidate_asset,
    _approve_bible_image_bindings, _canonical_bible_image_candidates, _formal_snapshot, _save_stage_snapshot,
)
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_bible_image_candidates as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class BibleAssetPromotionTests(TestCase):
    _Fixture = fixtures.BibleExecutionCandidateTests._Fixture
    mark_submitted = fixtures.BibleExecutionCandidateTests.mark_submitted
    archive = fixtures.BibleExecutionCandidateTests.archive
    resolve = fixtures.BibleExecutionCandidateTests.resolve

    def setUp(self):
        fixtures.BibleExecutionCandidateTests.setUp(self)
        declaration = next(row for row in production_stage_catalog().value["stages"] if row["key"] == "bible_image_review")
        self.review = StageRun.objects.create(production_run=self.run, stage_key=declaration["key"],
            ordinal=declaration["ordinal"], mode="manual", status="pending", idempotency_key=str(uuid4()))
        self.artifact, = self.archive()
        self.stage.refresh_from_db()

    def promote(self, guard):
        return _register_bible_candidate_asset(run=self.run, review_stage=self.review,
            artifact=self.artifact, entity_type="prop", entity=self.entity, binding_role="anchor", asset_guard=guard)

    def fact(self, asset):
        return _bible_binding_asset_fact(asset, project=self.project, entity_type="prop", entity=self.entity)

    def test_original_copy_and_read_use_exact_preparation_not_old_prompt(self):
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
            self.artifact.asset, self.artifact.status = asset, "selected"
            self.artifact.save(update_fields=("asset", "status"))
        self.assertEqual(asset.name, self.entity.name)
        self.assertEqual(asset.description, self.entity.description)
        self.assertEqual(asset.prompt_hint, "")  # No second copy of Bible anchor text.
        self.assertEqual(asset.checksum, self.artifact.sha256)
        self.assertEqual(Path(asset.file.path).read_bytes(), self.output.read_bytes())
        self.assertEqual(asset.metadata["schema_version"], 3)
        fact = self.fact(asset)
        self.assertEqual(fact["preparation_id"], str(self.preparation.pk))
        self.assertEqual(fact["execution_instance_version_id"], str(self.version.pk))
        self.assertEqual(fact["request_sha256"], self.preparation.request_sha256)
        for obsolete in ("prompt_artifact_id", "source_snapshot_sha256", "prompt_profile_binding_key"):
            self.assertNotIn(obsolete, fact)
            self.assertNotIn(obsolete, asset.metadata)
        # Binding its own image is not a creative-content change.
        self.entity.anchor_asset = asset
        self.entity.save(update_fields=("anchor_asset",))
        self.assertEqual(self.fact(asset), fact)
        with _StoredAssetFileGuard() as guard, self.assertRaises(ValidationError):
            self.promote(guard)
        original = deepcopy(asset.metadata)
        for key, value in (("request_sha256", "0" * 64), ("preparation_id", str(uuid4())),
                ("schema_version", 1), ("generation_purpose", None)):
            asset.metadata = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.fact(asset)
        asset.metadata = original
        self.entity.description = "Changed creative content"
        self.entity.save(update_fields=("description",))
        with self.assertRaises(ProductionRunConflict):
            self.fact(asset)

    def test_changed_relation_wrong_target_and_late_result_are_not_copied(self):
        before = Asset.objects.count()
        self.entity.reference_assets.add(self.asset)
        with _StoredAssetFileGuard() as guard, self.assertRaises(ProductionRunConflict):
            self.promote(guard)
        self.entity.reference_assets.clear()
        self.attempt.ignore_late_result = True
        self.attempt.save(update_fields=("ignore_late_result",))
        with _StoredAssetFileGuard() as guard, self.assertRaises(ValidationError):
            self.promote(guard)
        self.attempt.ignore_late_result = False
        self.attempt.save(update_fields=("ignore_late_result",))
        with _StoredAssetFileGuard() as guard, self.assertRaises(ValidationError):
            _register_bible_candidate_asset(run=self.run, review_stage=self.review, artifact=self.artifact,
                entity_type="character", entity=self.entity, binding_role="anchor", asset_guard=guard)
        self.assertEqual(Asset.objects.count(), before)
        self.artifact.refresh_from_db()
        self.assertIsNone(self.artifact.asset_id)

    def test_primary_revision_invalidates_adoption_but_keeps_exact_original_candidate(self):
        from studio.services.bible_variants import revise_variant, setting_payload
        from studio.services.production_bible_targets import read_bible_candidate_target

        _, original = read_bible_candidate_target(self.artifact)
        self.assertEqual(original["schema_version"], 2)
        self.assertNotIn("visual_identity", original)
        setting = self.primary.revisions.get(revision=1)
        self.assertEqual(original["primary_setting"]["setting_revision_id"], str(setting.pk))
        before = (Asset.objects.count(), self.preparation.request_sha256)
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
            self.artifact.asset, self.artifact.status = asset, "selected"
            self.artifact.save(update_fields=("asset", "status"))
        self.assertEqual(asset.prompt_hint, "")
        self.fact(asset)
        revise_variant(actor=self.user, variant=self.primary, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "圆形握柄"})
        self.assertEqual(read_bible_candidate_target(self.artifact)[1], original)
        with self.assertRaises(ProductionRunConflict):
            self.fact(asset)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        candidate = response.data["candidates"][0]["latest_candidate"]
        self.assertFalse(candidate["source_current"])
        self.assertEqual(candidate["integrity_status"], "valid")
        self.preparation.refresh_from_db()
        self.assertEqual((Asset.objects.count(), self.preparation.request_sha256), (before[0] + 1, before[1]))

    def test_changed_primary_prevents_copy_before_any_asset_or_selection_write(self):
        from studio.models import ApprovalDecision, ArtifactSelectionDecision
        from studio.services.bible_variants import revise_variant, setting_payload
        from studio.services.production_bible_targets import read_bible_candidate_target

        original = read_bible_candidate_target(self.artifact)[1]
        before = (Asset.objects.count(), ApprovalDecision.objects.count(), ArtifactSelectionDecision.objects.count())
        revise_variant(actor=self.user, variant=self.primary, expected_revision=1,
            value={**setting_payload(self.primary.revisions.get(revision=1)), "identity_anchor": "新的共同身份"})
        with _StoredAssetFileGuard() as guard, transaction.atomic(), self.assertRaises(ProductionRunConflict):
            self.promote(guard)
        self.assertEqual(before, (Asset.objects.count(), ApprovalDecision.objects.count(), ArtifactSelectionDecision.objects.count()))
        self.assertEqual(read_bible_candidate_target(self.artifact)[1], original)

    def test_failed_final_write_rolls_back_database_and_new_asset_file(self):
        before, copied = Asset.objects.count(), []
        with self.assertRaisesMessage(RuntimeError, "receipt failed"):
            with _StoredAssetFileGuard() as guard, transaction.atomic():
                asset = self.promote(guard)
                copied.append(Path(asset.file.path))
                raise RuntimeError("receipt failed")
        self.assertEqual(Asset.objects.count(), before)
        self.assertTrue(copied)
        self.assertFalse(copied[0].exists())
        self.artifact.refresh_from_db()
        self.assertIsNone(self.artifact.asset_id)
        # Byte-copy verification remains mandatory and is not replaced by
        # merely accepting the metadata of the original candidate.
        with patch("studio.services.production_media_assets.verified_local_media_facts", return_value={
                "sha256": "0" * 64, "size_bytes": self.artifact.size_bytes, "mime_type": "image/png"}):
            with self.assertRaisesMessage(ValidationError, "二次校验失败"):
                with _StoredAssetFileGuard() as guard, transaction.atomic():
                    self.promote(guard)
        self.assertEqual(Asset.objects.count(), before)

    def test_another_round_keeps_origin_and_adoption_identities_separate(self):
        next_run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="awaiting_input", idempotency_key=str(uuid4()), request_sha256="d" * 64)
        review = StageRun.objects.create(production_run=next_run, stage_key="bible_image_review",
            ordinal=self.review.ordinal, mode="manual", status="pending", idempotency_key=str(uuid4()))
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            # This round has its own frozen configuration, never the old
            # model's current options or the source Run's mutable settings.
            for field in ("config_snapshot", "config_snapshot_schema_version", "config_snapshot_size_bytes", "config_snapshot_sha256"):
                setattr(next_run, field, deepcopy(getattr(self.run, field)))
            next_run.save()
            asset = _register_bible_candidate_asset(run=next_run, review_stage=review, artifact=self.artifact,
                entity_type="prop", entity=self.entity, binding_role="reference", asset_guard=guard)
            self.artifact.asset = asset
            self.artifact.save(update_fields=("asset",))
        self.assertEqual(asset.metadata["production_run_id"], str(self.run.pk))
        self.assertNotIn("adoption_run_id", asset.metadata)
        self.assertNotIn("review_stage_id", asset.metadata)
        self.assertEqual(self.fact(asset)["preparation_id"], str(self.preparation.pk))

    def test_original_review_writes_multiple_sources_and_exactly_one_anchor(self):
        from hashlib import sha256
        from django.core.files.base import ContentFile
        from studio.models import ArtifactSelectionDecision

        image = self.output.read_bytes()
        library = Asset(owner=self.user, kind="prop", name="Side view", mime_type="image/png", checksum=sha256(image).hexdigest())
        library.file.save("side-view.png", ContentFile(image))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        canonical = _canonical_bible_image_candidates(self.run, self.stage)
        candidate = canonical["entities"][0]["candidates"][0]
        self.assertEqual(str(candidate["preparation_id"]), str(self.preparation.pk))
        self.assertEqual(candidate["eligibility"], "eligible")
        for obsolete in ("prompt_artifact_id", "prompt_profile_binding_key", "status"):
            self.assertNotIn(obsolete, candidate)
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, "images"))
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            _approve_bible_image_bindings(self.run, self.review, self.user, bindings=[{
                "entity_type": "prop", "entity_id": str(self.entity.pk),
                "selected_artifact_ids": [str(self.artifact.pk)],
                "selected_library_assets": [{"asset_id": str(library.pk), "expected_sha256": library.checksum}],
                "anchor_artifact_id": str(self.artifact.pk), "anchor_library_asset_id": None,
            }], expected_source_snapshot_sha256=self.stage.output_snapshot_sha256, asset_guard=guard)
        self.entity.refresh_from_db()
        self.artifact.refresh_from_db()
        self.assertEqual(self.entity.anchor_asset_id, self.artifact.asset_id)
        self.assertEqual(list(self.entity.reference_assets.values_list("pk", flat=True)), [library.pk])
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.review)
        self.assertEqual(decision.artifact_id, self.artifact.pk)
        self.assertEqual(decision.selected_by_id, self.user.pk)
        self.assertEqual(self.review.output_snapshot["bindings"][0]["reference_asset_ids"], [str(library.pk)])
        self.assertEqual(_canonical_bible_image_candidates(self.run, self.stage), canonical)
        self.assertEqual(self.fact(self.artifact.asset)["request_sha256"], self.preparation.request_sha256)

    def test_review_refuses_a_stale_snapshot_without_creating_a_selection_or_asset(self):
        from studio.models import ArtifactSelectionDecision

        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, "images"))
        before = Asset.objects.count()
        with self.assertRaises(ProductionRunConflict):
            with _StoredAssetFileGuard() as guard, transaction.atomic():
                _approve_bible_image_bindings(self.run, self.review, self.user, bindings=[{
                    "entity_type": "prop", "entity_id": str(self.entity.pk),
                    "selected_artifact_ids": [str(self.artifact.pk)], "selected_library_assets": [],
                    "anchor_artifact_id": str(self.artifact.pk), "anchor_library_asset_id": None,
                }], expected_source_snapshot_sha256="0" * 64, asset_guard=guard)
        self.assertEqual(Asset.objects.count(), before)
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())
        self.entity.refresh_from_db()
        self.assertIsNone(self.entity.anchor_asset_id)

    def test_promoted_asset_cannot_be_replaced_and_unbinding_keeps_exact_candidate_visible(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIClient

        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
            self.artifact.asset, self.artifact.status = asset, "selected"
            self.artifact.save(update_fields=("asset", "status"))
            self.entity.anchor_asset = asset
            self.entity.save(update_fields=("anchor_asset",))
        client = APIClient()
        client.force_authenticate(self.user)
        before = Path(asset.file.path).read_bytes()
        response = client.patch(f"/api/v1/assets/{asset.pk}/", {
            "description": "Do not silently replace an approved reference",
            "file": SimpleUploadedFile("replacement.png", before, content_type="image/png"),
        }, format="multipart")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("bible_reference_asset_immutable", str(response.data))
        asset.refresh_from_db()
        self.assertEqual(Path(asset.file.path).read_bytes(), before)

        self.entity.anchor_asset = None
        self.entity.save(update_fields=("anchor_asset",))
        response = client.get(f"/api/v1/projects/{self.project.pk}/bible-image-candidates/")
        self.assertEqual(response.status_code, 200, response.data)
        entity = next(row for row in response.data["candidates"] if str(row["entity_id"]) == str(self.entity.pk))
        candidate = next(row for row in entity["candidate_history"] if str(row["artifact_id"]) == str(self.artifact.pk))
        self.assertEqual(candidate["binding_role"], "unbound")
        self.assertEqual(str(candidate["materialized_asset"]["id"]), str(asset.pk))
        self.assertEqual(str(candidate["preparation_id"]), str(self.preparation.pk))
        rejected = client.delete(f"/api/v1/assets/{asset.pk}/")
        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(Path(asset.file.path).is_file())

    def test_one_library_asset_cannot_be_assigned_to_two_bible_entities_or_use_a_stale_checksum(self):
        from hashlib import sha256
        from django.core.files.base import ContentFile
        from studio.models import ArtifactSelectionDecision, PropBible

        other = PropBible.objects.create(project=self.project, slug="other-key", name="另一把钥匙", description="独立道具")
        image = self.output.read_bytes()
        library = Asset(owner=self.user, kind="prop", name="Reference", mime_type="image/png", checksum=sha256(image).hexdigest())
        library.file.save("shared-reference.png", ContentFile(image))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, "images"))
        def choice(entity, checksum):
            return {"entity_type": "prop", "entity_id": str(entity.pk), "selected_artifact_ids": [],
                "selected_library_assets": [{"asset_id": str(library.pk), "expected_sha256": checksum}],
                "anchor_artifact_id": None, "anchor_library_asset_id": str(library.pk)}
        before = Asset.objects.count()
        for bindings in ([choice(self.entity, library.checksum), choice(other, library.checksum)],
                [choice(self.entity, "0" * 64)]):
            with self.subTest(bindings=bindings), self.assertRaises((ValidationError, ProductionRunConflict)):
                with _StoredAssetFileGuard() as guard, transaction.atomic():
                    _approve_bible_image_bindings(self.run, self.review, self.user, bindings=bindings,
                        expected_source_snapshot_sha256=self.stage.output_snapshot_sha256, asset_guard=guard)
            self.assertEqual(Asset.objects.count(), before)
            self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())
            for entity in (self.entity, other):
                entity.refresh_from_db()
                self.assertIsNone(entity.anchor_asset_id)

    def test_promoted_bytes_and_source_changes_invalidate_the_frozen_asset_fact(self):
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
            self.artifact.asset = asset
            self.artifact.save(update_fields=("asset",))
        before = Path(asset.file.path).read_bytes()
        Path(asset.file.path).write_bytes(b"isolated corrupted reference")
        with self.assertRaises(StageBlocked) as error:
            self.fact(asset)
        self.assertEqual(error.exception.code, "bible_image_binding_asset_integrity")
        Path(asset.file.path).write_bytes(before)
        self.assertEqual(self.fact(asset)["request_sha256"], self.preparation.request_sha256)
        from studio.services.bible_variants import revise_variant, setting_payload
        revise_variant(actor=self.user, variant=self.primary, expected_revision=1,
            value={**setting_payload(self.primary.revisions.get(revision=1)), "identity_anchor": "Explicitly changed subject"})
        with self.assertRaises(ProductionRunConflict):
            self.fact(asset)

    def policy_review(self):
        """Exercise the original writer, not Recipe admission or model execution.

        Only policy authorization is isolated here; candidate identity, bytes,
        selection, Asset creation, stage checks and the decision are real.
        """
        from studio.services.production_runs import _execute_unattended_policy_review
        from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY

        policy = UNATTENDED_DECISION_POLICY_REGISTRY["bible_image_review"]
        with patch("studio.services.production_runs._verified_unattended_decision_policy",
                return_value=(policy, "f" * 64)):
            return _execute_unattended_policy_review(self.run, self.review)

    def test_later_review_reuses_the_materialized_candidate_without_rewriting_its_origin(self):
        from studio.models import ArtifactSelectionDecision
        from studio.services.production_run_plans import freeze_run_execution_plan

        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
            self.artifact.asset, self.artifact.status = asset, "selected"
            self.artifact.save(update_fields=("asset", "status"))
            self.entity.anchor_asset = asset
            self.entity.save(update_fields=("anchor_asset",))
        original_metadata = deepcopy(asset.metadata)
        next_run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="awaiting_input", idempotency_key=str(uuid4()), request_sha256="d" * 64)
        for field in ("config_snapshot", "config_snapshot_schema_version", "config_snapshot_size_bytes", "config_snapshot_sha256"):
            setattr(next_run, field, deepcopy(getattr(self.run, field)))
        next_run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=next_run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        source = StageRun.objects.create(production_run=next_run, stage_key=self.stage.stage_key,
            ordinal=self.stage.ordinal, mode="manual", status="succeeded", idempotency_key=str(uuid4()))
        review = StageRun.objects.create(production_run=next_run, stage_key=self.review.stage_key,
            ordinal=self.review.ordinal, mode="manual", status="pending", idempotency_key=str(uuid4()))
        _save_stage_snapshot(source, "output_snapshot", _formal_snapshot(next_run, source, "review-existing-candidate"))
        before = Asset.objects.count()
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            _approve_bible_image_bindings(next_run, review, self.user, bindings=[{
                "entity_type": "prop", "entity_id": str(self.entity.pk),
                "selected_artifact_ids": [str(self.artifact.pk)], "selected_library_assets": [],
                "anchor_artifact_id": str(self.artifact.pk), "anchor_library_asset_id": None,
            }], expected_source_snapshot_sha256=source.output_snapshot_sha256, asset_guard=guard)
        self.assertEqual(Asset.objects.count(), before)
        asset.refresh_from_db()
        self.assertEqual(asset.metadata, original_metadata)
        decision = ArtifactSelectionDecision.objects.get(stage_run=review)
        self.assertEqual(decision.artifact_id, self.artifact.pk)
        self.assertEqual(self.fact(asset)["preparation_id"], str(self.preparation.pk))
        self.entity.refresh_from_db()
        self.assertEqual(self.entity.anchor_asset_id, asset.pk)

    def test_policy_writer_selects_only_the_current_candidate_and_records_its_actor(self):
        from studio.models import ApprovalDecision, ArtifactSelectionDecision

        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, "images"))
        result = self.policy_review()
        decision = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual(result, f"approval:{decision.pk}")
        self.assertEqual(decision.actor, ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(decision.decided_by_id)
        self.assertEqual(decision.decision_policy_sha256, "f" * 64)
        self.entity.refresh_from_db()
        self.artifact.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(self.entity.anchor_asset_id, self.artifact.asset_id)
        self.assertEqual(self.artifact.status, "selected")
        self.assertEqual(self.artifact.asset.owner_id, self.project.owner_id)
        self.assertEqual(self.fact(self.artifact.asset)["preparation_id"], str(self.preparation.pk))
        self.assertEqual(self.review.output_snapshot["policy_evidence"]["selected_artifact_ids"], [str(self.artifact.pk)])
        self.assertEqual(decision.output_snapshot_sha256, self.review.output_snapshot_sha256)
        selected = ArtifactSelectionDecision.objects.get(stage_run=self.review)
        self.assertEqual((selected.selection_mode, selected.selected_by_id), ("automatic", None))

    def test_policy_writer_refuses_ambiguous_or_noncurrent_candidates_without_writes(self):
        from studio.models import ApprovalDecision, ArtifactSelectionDecision
        from studio.services.production_runs import _sha256

        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        original = _formal_snapshot(self.run, self.stage, "images")
        before = Asset.objects.count()
        for change in ("multiple", "historical", "other_run", "other_stage"):
            snapshot = deepcopy(original)
            candidates = snapshot["canonical"]["entities"][0]["candidates"]
            if change == "multiple":
                candidates.append(deepcopy(candidates[0]))
            elif change == "historical":
                candidates[0]["historical"] = True
            elif change == "other_run":
                candidates[0]["origin_production_run_id"] = str(uuid4())
            else:
                candidates[0]["origin_stage_run_id"] = str(uuid4())
            snapshot["canonical_sha256"] = _sha256(snapshot["canonical"])
            _save_stage_snapshot(self.stage, "output_snapshot", snapshot)
            with self.subTest(change=change), self.assertRaises(StageBlocked) as error:
                self.policy_review()
            self.assertEqual(error.exception.code, "unattended_bible_image_candidate_count_invalid")
            self.assertEqual(Asset.objects.count(), before)
            self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
            self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())
            self.entity.refresh_from_db()
            self.assertIsNone(self.entity.anchor_asset_id)

    def test_project_owner_reads_candidates_but_other_tenant_cannot_and_delete_is_protected(self):
        from django.contrib.auth import get_user_model
        from django.conf import settings
        from rest_framework.test import APIClient
        from studio.models import ExecutionPreparation, GeneratedArtifact, SeriesProject

        client = APIClient()
        self.user.is_staff = False
        self.user.save(update_fields=("is_staff",))
        client.force_authenticate(self.user)
        for path in (f"/api/v1/production-runs/{self.run.pk}/",
                f"/api/v1/generation-attempts/{self.attempt.pk}/",
                f"/api/v1/generated-artifacts/{self.artifact.pk}/"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.data)
        managed = Path(settings.MEDIA_ROOT) / self.artifact.object_key
        before = managed.read_bytes()
        rejected = client.delete(f"/api/v1/projects/{self.project.pk}/")
        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertEqual(rejected.data["error"]["code"], "protected_resource")
        self.assertTrue(SeriesProject.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(ExecutionPreparation.objects.filter(pk=self.preparation.pk).exists())
        self.assertTrue(GeneratedArtifact.objects.filter(pk=self.artifact.pk).exists())
        self.assertEqual(managed.read_bytes(), before)
        other = get_user_model().objects.create_user(username="foreign-bible-reader")
        client.force_authenticate(other)
        for path in (f"/api/v1/production-runs/{self.run.pk}/",
                f"/api/v1/generation-attempts/{self.attempt.pk}/",
                f"/api/v1/generated-artifacts/{self.artifact.pk}/"):
            self.assertEqual(client.get(path).status_code, 404)
