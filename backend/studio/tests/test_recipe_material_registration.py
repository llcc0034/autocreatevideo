"""Original queue/selection -> Asset/version, with no live model or adoption."""
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import Asset, AssetReferenceSeries, AssetReferenceVersion, AssetGenerationJob, ArtifactSelectionDecision, GenerationAttempt
from studio.services.asset_references import create_series, read_generated_material
from studio.services.production_media_assets import read_registered_production_image
from studio.services.production_recipe_material_registration import (
    register_recipe_material_selection, schedule_recipe_material_registrations, read_registered_recipe_primary_dependency)
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.services.production_runs import _StoredAssetFileGuard, _bible_binding_asset_fact
from studio.tests import test_recipe_material_selection as selection_tests


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMaterialRegistrationTests(TestCase):
    prepare_initial = False
    freeze_input = True
    evaluator = "first_succeeded"
    setUpTestData = classmethod(selection_tests.RecipeMaterialSelectionTests.setUpTestData.__func__)
    write_contract = staticmethod(selection_tests.RecipeMaterialSelectionTests.write_contract)
    seed_rule = selection_tests.RecipeMaterialSelectionTests.seed_rule
    prepare = selection_tests.RecipeMaterialSelectionTests.prepare
    image_stage = selection_tests.RecipeMaterialSelectionTests.image_stage
    queue_step = selection_tests.RecipeMaterialSelectionTests.queue_step
    download_image = staticmethod(selection_tests.RecipeMaterialSelectionTests.download_image)
    submit_candidate = selection_tests.RecipeMaterialSelectionTests.submit_candidate
    collect_candidate = selection_tests.RecipeMaterialSelectionTests.collect_candidate
    candidate = selection_tests.RecipeMaterialSelectionTests.candidate
    choose = selection_tests.RecipeMaterialSelectionTests.choose
    def setUp(self):
        if self._testMethodName == "test_primary_only_manual_choice_must_register_before_stage_completion":
            self.evaluator = None
        selection_tests.RecipeMaterialSelectionTests.setUp(self)

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(self.character.reference_assets.exists())

    def register(self, decision, **kwargs):
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            return register_recipe_material_selection(stage_run_id=self.stage.pk, unit_key=self.unit_key,
                decision_id=decision.pk if decision else None, actor=kwargs.pop("actor", self.actor),
                asset_guard=guard, **kwargs)

    def test_selected_candidate_registers_exact_generated_version_without_adoption(self):
        artifact = self.candidate()
        decision = self.choose()
        original = deepcopy(artifact.generation_attempt.preparation.contract.resolved_inputs)
        version = self.register(decision)
        artifact.refresh_from_db()
        self.assertEqual((version.asset_id, version.source, version.generated_artifact_id),
            (artifact.asset_id, "generated", artifact.pk))
        self.assertEqual((version.setting_revision_id, version.generated_from_id, version.generation_job_id),
            (self.setting.pk, None, None))
        self.assertEqual((version.series.role, version.series.bible_variant_id), ("primary", self.setting.variant_id))
        asset = version.asset
        self.assertEqual(asset.checksum, artifact.sha256)
        self.assertEqual(Path(asset.file.path).read_bytes(), (self.media_root / artifact.object_key).read_bytes())
        self.assertEqual(read_registered_production_image(artifact).pk, asset.pk)
        for key in ("adoption_run_id", "review_stage_id", "binding_role"):
            self.assertNotIn(key, asset.metadata)
        self.assertNotIn("审核", asset.source_info)
        self.assertEqual(asset.metadata["generation_attempt_id"], str(artifact.generation_attempt_id))
        artifact.generation_attempt.preparation.contract.refresh_from_db()
        self.assertEqual(artifact.generation_attempt.preparation.contract.resolved_inputs, original)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        # The later Bible consumer can verify this same Asset without needing
        # fake adoption metadata or a second materialization.
        fact = _bible_binding_asset_fact(asset, project=self.project, entity_type="character", entity=self.character)
        self.assertEqual(fact["generated_artifact_id"], str(artifact.pk))
        response = self.client.get("/api/v1/asset-reference-versions/", {"series_id": str(version.series_id)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(str(version.pk), str(response.data))

    def test_replay_is_idempotent_and_manual_reselection_keeps_old_version(self):
        first, second = self.candidate(1), self.candidate(2)
        original = self.choose()
        version = self.register(original)
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No second copy")):
            self.assertEqual(self.register(original).pk, version.pk)
        changed = self.choose(actor=self.actor, artifact_id=second.pk)
        next_version = self.register(changed)
        self.assertEqual((next_version.series_id, next_version.version), (version.series_id, 2))
        self.assertNotEqual(next_version.asset_id, version.asset_id)
        version.refresh_from_db()
        self.assertEqual(version.generated_artifact_id, first.pk)
        with self.assertRaisesMessage(ValidationError, "素材选择已改变"):
            self.register(original)
        self.assertEqual((AssetReferenceSeries.objects.count(), AssetReferenceVersion.objects.count()), (1, 2))

    def test_superseded_registration_callback_does_not_block_new_manual_selection(self):
        self.candidate(1)
        second = self.candidate(2)
        old = self.choose()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_recipe_material_registrations(stage_run_id=self.stage.pk, selections=[(self.unit_key, old.pk)])
        new = self.choose(actor=self.actor, artifact_id=second.pk)
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance") as advance:
            callbacks[0]()
        advance.assert_called_once_with(self.run.pk)
        self.stage.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual((self.stage.status, self.run.status), ("running", "running"))
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        self.assertEqual(self.register(new).generated_artifact_id, second.pk)

    def test_missing_selection_wrong_owner_and_stopped_run_do_not_write(self):
        artifact = self.candidate()
        with self.assertRaisesMessage(ValidationError, "确切选择决定"):
            self.register(None)
        decision = self.choose()
        stranger = get_user_model().objects.create_user(username="registration-stranger", password="isolated")
        with self.assertRaises(PermissionDenied):
            self.register(decision, actor=stranger)
        original_id = decision.pk
        decision.pk = uuid4()
        with self.assertRaisesMessage(ValidationError, "确切素材选择不存在"):
            self.register(decision)
        decision.pk = original_id
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "运行中素材阶段"):
            self.register(decision)
        artifact.refresh_from_db()
        self.assertIsNone(artifact.asset_id)
        self.assertFalse(AssetReferenceVersion.objects.exists())

    def test_failed_version_write_rolls_back_asset_pointer_and_new_file(self):
        artifact = self.candidate()
        decision = self.choose()
        before = Asset.objects.count()
        with patch("studio.services.asset_references._save", side_effect=RuntimeError("version registration failed")):
            with self.assertRaisesMessage(RuntimeError, "version registration failed"):
                self.register(decision)
        artifact.refresh_from_db()
        self.assertIsNone(artifact.asset_id)
        self.assertEqual(Asset.objects.count(), before)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(list((self.media_root / "assets").rglob("*.png")))
        self.assertTrue((self.media_root / artifact.object_key).is_file())
        self.assertIsNotNone(self.register(decision))

    def test_existing_asset_or_version_corruption_fails_without_repair(self):
        artifact = self.candidate()
        decision = self.choose()
        version = self.register(decision)
        original = deepcopy(version.asset.metadata)
        Asset.objects.filter(pk=version.asset_id).update(metadata={**original, "request_sha256": "0" * 64})
        with self.assertRaisesMessage(ValidationError, "原生成来源不一致"):
            self.register(decision)
        version.asset.refresh_from_db()
        self.assertEqual(version.asset.metadata["request_sha256"], "0" * 64)
        # Explicit isolated fixture restoration, not production repair.
        Asset.objects.filter(pk=version.asset_id).update(metadata=original)
        AssetReferenceVersion.objects.filter(pk=version.pk).update(setting_revision=None)
        with self.assertRaisesMessage(APIValidationError, "原版本关系不一致"):
            self.register(decision)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)

    def test_existing_series_reused_and_ambiguous_series_never_guessed(self):
        artifact = self.candidate()
        decision = self.choose()
        values = {"role": "primary", "name": "Explicit primary", "bible_variant_id": str(self.setting.variant_id)}
        first = create_series(actor=self.actor, values=values)
        extra = create_series(actor=self.actor, values={**values, "name": "Another primary"})
        with self.assertRaisesMessage(APIValidationError, "多个素材系列"):
            self.register(decision)
        artifact.refresh_from_db()
        self.assertIsNone(artifact.asset_id)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        extra.delete()  # Only the isolated fixture relation, no business data.
        self.assertEqual(self.register(decision).series_id, first.pk)

    def test_origin_lookup_uses_existing_foreign_key_index(self):
        rows = AssetReferenceVersion.objects.filter(generated_artifact_id=uuid4()).order_by().values("id")
        plan = rows.explain()
        self.assertIn("SEARCH", plan)
        self.assertIn("INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_changed_asset_mime_and_disabled_asset_cannot_be_reused(self):
        self.candidate()
        decision = self.choose()
        version = self.register(decision)
        Asset.objects.filter(pk=version.asset_id).update(mime_type="image/jpeg")
        with self.assertRaisesMessage(ValidationError, "归档字节不一致"):
            self.register(decision)
        Asset.objects.filter(pk=version.asset_id).update(mime_type="image/png", is_active=False)
        with self.assertRaises(ValidationError):
            self.register(decision)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)

    def test_primary_only_manual_choice_must_register_before_stage_completion(self):
        first, second = self.candidate(1), self.candidate(2)
        self.stage.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            for _ in range(2):
                self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
                self.assertEqual(self.stage.reason_code, "waiting_material_selection")
        self.assertFalse(callbacks)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        decision = self.choose(actor=self.actor, artifact_id=second.pk)
        self.assertEqual((decision.selection_mode, decision.selected_by_id), ("manual", self.actor.pk))
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance"):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
                self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        version = AssetReferenceVersion.objects.get()
        self.assertEqual((version.generated_artifact_id, version.source), (second.pk, "generated"))
        first.refresh_from_db()
        self.assertIsNone(first.asset_id)
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No second copy")):
            self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "generated-artifacts:2")
        self.assertEqual((GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count()), (2, 1))

    def check_explicit_append_reselection(self, *, keep_original):
        from studio.models import StageRun
        from studio.services.execution_advancement import advance_execution_preparation
        from studio.services.production_recipe_material_selection import recipe_material_selection_context
        from studio.services.production_runs import (
            _formal_snapshot, _save_stage_snapshot, regenerate_bible_image_candidates, select_run_artifact,
        )
        first, _ = self.candidate(1), self.candidate(2)
        original = self.choose()
        original_version = self.register(original)
        self.stage.refresh_from_db()
        output = advance_recipe_media_stage(self.run, self.stage)
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, output))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        self.run.status, self.run.current_stage_key = "awaiting_approval", "bible_image_review"
        self.run.save(update_fields=["status", "current_stage_key"])
        # Original append writer/source verification; only its automatic wakeup
        # is held so this fixture can drive the real isolated queue explicitly.
        request = {"request_id": uuid4(), "expected_source_snapshot_sha256": self.stage.output_snapshot_sha256,
            "entities": self.entities, "user": self.actor}
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            regenerate_bible_image_candidates(self.run, **request)
        self.run.refresh_from_db()
        self.stage.refresh_from_db()
        preparation = self.stage.execution_preparations.get(request_key="route:0:candidate:3")
        preparation = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.actor)
        attempt = GenerationAttempt.objects.get(preparation=preparation)
        attempt = self.queue_step(attempt, "submit", {"request_id": f"isolated-{preparation.pk}", "status": "submitted"})
        added = self.collect_candidate(attempt)
        self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
        self.assertEqual(self.stage.reason_code, "waiting_material_selection")
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, "追加候选后须重新选择"):
            _formal_snapshot(self.run, self.stage, "generated-artifacts:3")
        selected = first if keep_original else added
        context = recipe_material_selection_context(artifact=selected, actor=self.actor)
        self.assertTrue(context["can_select"], context)
        observed = {"expected_decision_id": context["expected_decision_id"],
            "expected_dependencies_sha256": context["dependencies_sha256"]}
        select_run_artifact(self.run, selected, user=self.actor, material_selection=observed)
        decision = ArtifactSelectionDecision.objects.order_by("-revision").first()
        self.assertEqual((decision.artifact_id, decision.selection_mode, decision.selected_by_id, decision.revision),
            (selected.pk, "manual", self.actor.pk, 2))
        version = self.register(decision)
        self.assertEqual(version.pk == original_version.pk, keep_original)
        self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "generated-artifacts:3")
        canonical = _formal_snapshot(self.run, self.stage, "generated-artifacts:3")["canonical"]
        self.assertEqual(canonical["material_generations"][0]["decision_id"], str(decision.pk))
        # Duplicate public requests and worker wakeups retain both actual choices
        # and the old version; neither adds a fourth call or a third decision.
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No repeated copy")):
            select_run_artifact(self.run, selected, user=self.actor, material_selection=observed)
            regenerate_bible_image_candidates(self.run, **request)
            self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "generated-artifacts:3")
        original.refresh_from_db()
        original_version.refresh_from_db()
        self.assertEqual((original.artifact_id, original.selection_mode, original.selected_by_id), (first.pk, "automatic", None))
        self.assertEqual(original_version.generated_artifact_id, first.pk)
        self.assertEqual((GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count()), (3, 2))
        self.assertEqual(AssetReferenceVersion.objects.count(), 1 if keep_original else 2)
        self.assertEqual(StageRun.objects.get(pk=self.review.pk).status, "pending")

    def test_explicit_append_can_select_new_candidate_without_replaying_calls(self):
        self.check_explicit_append_reselection(keep_original=False)

    def test_explicit_append_can_keep_original_without_copying_its_version(self):
        self.check_explicit_append_reselection(keep_original=True)

    def test_original_stage_registers_after_commit_and_replay_does_not_copy_or_call_again(self):
        first, _ = self.candidate(1), self.candidate(2)
        self.stage.refresh_from_db()
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance") as resume:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
                self.assertEqual(self.stage.reason_code, "waiting_material_registration")
                self.assertFalse(AssetReferenceVersion.objects.exists())
            resume.assert_called_once_with(self.run.pk)
        self.assertEqual(len(callbacks), 1)
        version = AssetReferenceVersion.objects.get()
        self.assertEqual(version.generated_artifact_id, first.pk)
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No second copy")):
            with self.captureOnCommitCallbacks(execute=True) as repeated:
                self.assertEqual(advance_recipe_media_stage(self.run, self.stage), "generated-artifacts:2")
        self.assertFalse(repeated)
        self.assertEqual((GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count()), (2, 1))
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        actual = next(row for row in response.data["stages"] if str(row["id"]) == str(self.stage.pk))
        self.assertEqual(actual["artifact_selection_decisions"][0]["selection_mode"], "automatic")

    def test_selection_transaction_rollback_does_not_register_or_leave_callback(self):
        self.candidate(1)
        self.candidate(2)
        self.stage.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaisesMessage(RuntimeError, "selection commit failed"), transaction.atomic():
                self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
                raise RuntimeError("selection commit failed")
        self.assertFalse(callbacks)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(list((self.media_root / "assets").rglob("*.png")))

    def test_late_registration_commit_failure_cleans_files_and_blocks_original_stage(self):
        artifact = self.candidate()
        decision = self.choose()
        from studio.services.registry_reference_lock import registry_reference_write_guard

        @contextmanager
        def failed_commit():
            with registry_reference_write_guard():
                yield
                raise RuntimeError("late registration commit failed")

        with patch("studio.services.registry_reference_lock.registry_reference_write_guard", failed_commit), patch(
                "studio.services.production_run_auto_advance.schedule_production_run_advance") as resume:
            with self.assertLogs("studio.services.production_recipe_material_registration", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    schedule_recipe_material_registrations(stage_run_id=self.stage.pk,
                        selections=[(self.unit_key, decision.pk)])
            resume.assert_not_called()
        artifact.refresh_from_db()
        self.stage.refresh_from_db()
        self.run.refresh_from_db()
        self.assertIsNone(artifact.asset_id)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(list((self.media_root / "assets").rglob("*.png")))
        self.assertTrue((self.media_root / artifact.object_key).is_file())
        self.assertEqual((self.stage.status, self.stage.reason_code), ("blocked", "material_registration_failed"))
        self.assertEqual((self.run.status, self.run.blocked_code), ("blocked", "material_registration_failed"))
        # Explicit isolated restart, representing the existing user retry; no model replay.
        self.stage.status = self.run.status = "running"
        self.stage.save(update_fields=("status",))
        self.run.save(update_fields=("status",))
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance"):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_recipe_material_registrations(stage_run_id=self.stage.pk,
                    selections=[(self.unit_key, decision.pk)])
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_stop_before_registration_callback_prevents_any_write_or_resume(self):
        self.candidate()
        decision = self.choose()
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance") as resume:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_recipe_material_registrations(stage_run_id=self.stage.pk,
                    selections=[(self.unit_key, decision.pk)])
                self.run.status = "cancelled"
                self.run.save(update_fields=("status",))
            resume.assert_not_called()
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(list((self.media_root / "assets").rglob("*.png")))

    def test_dependency_reader_returns_original_version_or_waits_without_writing(self):
        artifact = self.candidate()
        decision = self.choose()
        from studio.services.production_recipe_material_selection import read_recipe_material_selection
        self.stage.refresh_from_db()
        selection = read_recipe_material_selection(stage=self.stage, unit_key=self.unit_key, decision_id=decision.pk)
        # Explicit generated-source projection fixture; exact frozen-unit and
        # reuse-source resolution are exercised by the input integration tests.
        # This boundary checks the real original Asset/version graph only.
        with patch("studio.services.production_recipe_material_registration.read_recipe_primary_dependency",
                return_value=selection), patch(
                "studio.services.production_recipe_material_selection.recipe_primary_dependency_target",
                return_value=(selection["unit"], selection["target"]["setting"])), patch(
                "studio.services.production_recipe_material_fulfillment.read_recipe_material_fulfillment", return_value=None):
            self.assertIsNone(read_registered_recipe_primary_dependency(stage=self.stage, unit_key="variant-fixture"))
            version = self.register(decision)
            selection["artifact"].refresh_from_db()
            result = read_registered_recipe_primary_dependency(stage=self.stage, unit_key="variant-fixture")
            self.assertEqual(result["version"].pk, version.pk)
            AssetReferenceVersion.objects.filter(pk=version.pk).update(setting_revision=None)
            with self.assertRaisesMessage(APIValidationError, "原版本关系不一致"):
                read_registered_recipe_primary_dependency(stage=self.stage, unit_key="variant-fixture")
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)

    def test_unlinked_asset_with_existing_version_is_an_error_not_pending(self):
        artifact = self.candidate()
        version = self.register(self.choose())
        artifact.refresh_from_db()
        artifact.asset = None
        with self.assertRaisesMessage(APIValidationError, "缺少原Asset关联"):
            read_generated_material(artifact=artifact)
        self.assertEqual(AssetReferenceVersion.objects.get().pk, version.pk)

    def test_corrupt_registered_version_is_an_explicit_stage_validation_error(self):
        self.candidate(1)
        self.candidate(2)
        version = self.register(self.choose())
        AssetReferenceVersion.objects.filter(pk=version.pk).update(setting_revision=None)
        self.stage.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaisesMessage(ValidationError, "原版本关系不一致"):
                advance_recipe_media_stage(self.run, self.stage)
        self.assertFalse(callbacks)
        version.refresh_from_db()
        self.assertIsNone(version.setting_revision_id)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_original_run_executor_resumes_after_registration_without_extra_call(self):
        from studio.services.json_contracts import assign_json_contract, json_contract
        # Build the isolated root receipt before any preparation/call. The base
        # stage fixture does not exercise root creation; never bypass its check.
        intent = {"schema_version": 4, "plan_source": "recipe", "project_id": str(self.project.pk),
            "recipe_version_id": str(self.run.recipe_version_id), "creation_inputs": {
                "idea": "", "style_hint": "", "episode_count": None,
                "shots_per_episode": None, "episode_duration_seconds": None}}
        self.run.request_sha256 = json_contract(intent).sha256
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 4, "request_intent": intent,
            "run_plan_sha256": self.plan.sha256}, schema_version=4)
        self.run.save()
        self.candidate(1)
        self.candidate(2)
        from studio.services.production_runs import advance_production_run
        # This pre-existing stage-only fixture has no full delivery recipe. Only
        # that unrelated closure is isolated; executor/commit callbacks are real.
        with patch("studio.services.production_runs._assert_frozen_bible_run_closure"):
            with self.captureOnCommitCallbacks(execute=True):
                advance_production_run(self.run, automatic=True)
        self.stage.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        self.assertEqual(self.run.status, "awaiting_approval", self.run.blocked_reason)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_unknown_decision_callback_still_blocks_without_registering_anything(self):
        self.candidate(1)
        original = self.choose()
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance") as resume:
            with self.assertLogs("studio.services.production_recipe_material_registration", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    schedule_recipe_material_registrations(stage_run_id=self.stage.pk,
                        selections=[(self.unit_key, uuid4())])
            resume.assert_not_called()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.reason_code, "material_registration_failed")
        self.assertIn("确切素材选择不存在", self.stage.reason)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertEqual(ArtifactSelectionDecision.objects.order_by("-revision").first().pk, original.pk)
