"""Isolated original queue -> appearance choice, without adoption/real models."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ArtifactSelectionDecision, AssetGenerationJob, AssetReferenceVersion, GeneratedArtifact, GenerationAttempt
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_recipe_material_selection import choose_recipe_material_candidate, read_recipe_material_selection
from studio.tests import test_recipe_material_candidate_flow as flow


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMaterialSelectionTests(TestCase):
    prepare_initial = False
    freeze_input = True
    evaluator = "first_succeeded"
    setUpTestData = classmethod(flow.RecipeMaterialCandidateFlowTests.setUpTestData.__func__)
    write_contract = staticmethod(flow.RecipeMaterialCandidateFlowTests.write_contract)
    seed_rule = flow.RecipeMaterialCandidateFlowTests.seed_rule
    prepare = flow.RecipeMaterialCandidateFlowTests.prepare
    download_image = staticmethod(flow.RecipeMaterialCandidateFlowTests.download_image)
    submit_candidate = flow.RecipeMaterialCandidateFlowTests.submit_candidate
    collect_candidate = flow.RecipeMaterialCandidateFlowTests.collect_candidate

    def image_stage(self, route):
        return {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
            "mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": self.evaluator}}

    def queue_step(self, attempt, operation, response):
        # This fixture models a slow peer for other in-flight requests. Keep
        # the real worker's oldest-due-first claiming rule and submit lifecycle.
        GenerationAttempt.objects.filter(status__in=("queued", "submitted")).exclude(pk=attempt.pk).update(
            next_poll_at=timezone.now() + timedelta(hours=1))
        return flow.RecipeMaterialCandidateFlowTests.queue_step(self, attempt, operation, response)

    def setUp(self):
        if self._testMethodName == "test_unconfigured_auto_stays_waiting_and_manual_still_works":
            self.evaluator = None
        flow.RecipeMaterialCandidateFlowTests.setUp(self)
        self.unit_key = f"appearance:{self.setting.variant_id}"

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.assertFalse(GeneratedArtifact.objects.exclude(asset_id=None).exists())

    def choose(self, **kwargs):
        return choose_recipe_material_candidate(stage_run_id=self.stage.pk, unit_key=self.unit_key, **kwargs)

    def selected(self, **kwargs):
        self.stage.refresh_from_db()
        return read_recipe_material_selection(stage=self.stage, unit_key=self.unit_key, **kwargs)

    def candidate(self, number=1):
        _, attempt = self.submit_candidate(number)
        return self.collect_candidate(attempt)

    def test_automatic_choice_waits_then_uses_candidate_number_and_preserves_original_facts(self):
        self.assertIsNone(self.choose())
        _, first = self.submit_candidate(1)
        _, second = self.submit_candidate(2)
        second_artifact = self.collect_candidate(second)
        first_artifact = self.collect_candidate(first)
        original = deepcopy(first.preparation.contract.resolved_inputs)
        decision = self.choose()
        self.assertEqual(decision.artifact_id, first_artifact.pk)
        self.assertEqual((decision.selection_mode, decision.selected_by_id, decision.revision), ("automatic", None, 1))
        second_artifact.refresh_from_db()
        self.assertEqual(second_artifact.status, "available")
        result = self.selected(decision_id=decision.pk)
        self.assertEqual(result["candidate_number"], 1)
        self.assertEqual(result["media"]["media_facts"]["sha256"], first_artifact.sha256)
        self.assertEqual(result["target"]["setting"], result["preparation"].contract.resolved_inputs["media_generation_target"]["setting"])
        self.assertEqual(self.choose().pk, decision.pk)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        first.preparation.contract.refresh_from_db()
        self.assertEqual(first.preparation.contract.resolved_inputs, original)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_existing_choice_does_not_switch_when_lower_number_finishes_later(self):
        _, first = self.submit_candidate(1)
        second = self.candidate(2)
        chosen = self.choose()
        self.assertEqual(chosen.artifact_id, second.pk)
        self.collect_candidate(first)
        self.assertEqual(self.choose().pk, chosen.pk)
        self.assertEqual(self.selected()["artifact"].pk, second.pk)

    def test_original_primary_stage_records_auto_choice_in_existing_detail_api(self):
        from studio.services.production_recipe_media_stages import advance_recipe_media_stage
        first, _ = self.candidate(1), self.candidate(2)
        self.stage.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual((decision.artifact_id, decision.selection_mode, decision.selected_by_id),
            (first.pk, "automatic", None))
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        stage = next(row for row in response.data["stages"] if str(row["id"]) == str(self.stage.pk))
        actual, = stage["artifact_selection_decisions"]
        self.assertEqual((str(actual["id"]), actual["selection_mode"], actual["selected_by"]),
            (str(decision.pk), "automatic", None))
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_manual_change_keeps_exact_history_and_does_not_claim_human_approval_for_auto(self):
        first, second = self.candidate(1), self.candidate(2)
        original = self.choose()
        changed = self.choose(actor=self.actor, artifact_id=second.pk)
        self.assertEqual((changed.selection_mode, changed.selected_by_id, changed.revision), ("manual", self.actor.pk, 2))
        self.assertEqual(self.choose(actor=self.actor, artifact_id=second.pk).pk, changed.pk)
        self.assertEqual(self.choose().pk, changed.pk)
        self.assertEqual(self.selected(decision_id=original.pk, require_current=False)["artifact"].pk, first.pk)
        with self.assertRaisesMessage(ValidationError, "素材选择已改变"):
            self.selected(decision_id=original.pk)
        original.refresh_from_db()
        self.assertEqual((original.selection_mode, original.selected_by_id), ("automatic", None))
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)

    def test_unconfigured_auto_stays_waiting_and_manual_still_works(self):
        # Configure before the original fixture freezes its plan; no editing of
        # an existing RunPlan or historical decision to make this scenario pass.
        self.assertIsNone(self.choose())
        artifact = self.candidate()
        self.assertIsNone(self.choose())
        decision = self.choose(actor=self.actor, artifact_id=artifact.pk)
        self.assertEqual(decision.selection_mode, "manual")

    def test_permission_wrong_unit_wrong_artifact_and_stopped_run_are_rejected(self):
        artifact = self.candidate()
        stranger = get_user_model().objects.create_user(username="selection-stranger", password="isolated")
        with self.assertRaises(PermissionDenied):
            self.choose(actor=stranger, artifact_id=artifact.pk)
        with self.assertRaises(PermissionDenied):
            self.choose(artifact_id=artifact.pk)
        with self.assertRaisesMessage(ValidationError, "自动选择不能"):
            self.choose(actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "生成候选"):
            self.choose(actor=self.actor, artifact_id=uuid4())
        with self.assertRaisesMessage(ValidationError, "冻结的外观需求"):
            choose_recipe_material_candidate(stage_run_id=self.stage.pk, unit_key=f"appearance:{uuid4()}")
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "只有运行中"):
            self.choose()
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_changed_m3_keeps_history_but_cannot_authorize_new_variant_input(self):
        artifact = self.candidate()
        decision = self.choose()
        revise_variant(actor=self.actor, variant=self.setting.variant, expected_revision=1,
            value={**setting_payload(self.setting), "appearance_anchor": "New unreviewed coat"})
        self.assertEqual(self.selected(decision_id=decision.pk, require_current=False)["artifact"].pk, artifact.pk)
        for operation in (self.choose, self.selected):
            with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
                operation()
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)

    def test_selection_write_failure_rolls_back_artifact_status(self):
        artifact = self.candidate()
        with patch("studio.services.generated_artifacts.ArtifactSelectionDecision.objects.create",
                side_effect=RuntimeError("isolated selection failure")):
            with self.assertRaisesMessage(RuntimeError, "isolated selection failure"):
                self.choose()
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertIsNotNone(self.choose())

    def test_invalidated_call_or_wrong_decision_cannot_be_used_as_primary_input(self):
        artifact = self.candidate()
        decision = self.choose()
        with self.assertRaisesMessage(ValidationError, "确切素材选择不存在"):
            self.selected(decision_id=uuid4())
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(ignore_late_result=True)
        with self.assertRaisesMessage(ValidationError, "明确成功的主调用"):
            self.selected(decision_id=decision.pk)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "selected")

    def test_selection_lookup_uses_original_group_index_and_history_needs_exact_id(self):
        group = f"stage:{self.stage.pk}:unit:{sha256(self.unit_key.encode()).hexdigest()}"
        query = ArtifactSelectionDecision.objects.filter(stage_run=self.stage, variant_group=group).order_by(
            "-revision").values("id", "artifact_id", "revision")
        plan = query.explain()
        self.assertIn("SEARCH", plan)
        self.assertIn("INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)
        self.assertNotIn("SELECT *", str(query.query))
        with self.assertRaisesMessage(ValidationError, "必须给出确切决定"):
            self.selected(require_current=False)

    def test_corrupt_or_orphaned_selection_is_not_repaired_or_reselected(self):
        artifact = self.candidate()
        artifact.status = "selected"
        artifact.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "缺少原选择记录"):
            self.choose()
        artifact.status = "available"
        artifact.save(update_fields=("status",))
        decision = self.choose()
        artifact.status = "available"
        artifact.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "素材选择已改变"):
            self.choose()
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, decision.pk)

    def context(self, artifact):
        return self.client.get(f"/api/v1/production-runs/{self.run.pk}/material-selection-context/",
            {"artifact_id": str(artifact.pk)})

    def post_choice(self, artifact, expected=None, request_key=None, **extra):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/",
            {"artifact_id": str(artifact.pk), "material_selection": {"expected_decision_id": expected}, **extra}, format="json",
            HTTP_IDEMPOTENCY_KEY=request_key or str(uuid4()))

    def test_public_context_and_manual_choice_reuse_ledger_without_adoption_or_dispatch(self):
        artifact = self.candidate()
        response = self.context(artifact)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual((response.data["appearance_role"], response.data["candidate_number"],
            response.data["unit_key"], response.data["expected_decision_id"]), ("primary", 1, self.unit_key, None))
        self.assertEqual(response.data["reference_name"], self.setting.reference_name)
        self.assertTrue(response.data["can_select"])
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            result = self.post_choice(artifact, request_key="original-choice", material_selection={"expected_decision_id": None,
                "expected_dependencies_sha256": response.data["dependencies_sha256"]})
        self.assertEqual(result.status_code, 200, result.data)
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual((result.data["result_kind"], result.data["target_id"]), ("artifact_selection", str(decision.pk)))
        self.assertEqual((decision.selection_mode, decision.selected_by_id), ("manual", self.actor.pk))
        replay = self.post_choice(artifact, request_key="original-choice", material_selection={"expected_decision_id": None,
            "expected_dependencies_sha256": response.data["dependencies_sha256"]})
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(result.data, replay.data)  # Unknown response retries the exact original command.
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        self.assertFalse(self.context(artifact).data["can_select"])
        self.assertEqual(callbacks, [])
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_public_stale_choice_conflict_and_explicit_refresh(self):
        first, second = self.candidate(1), self.candidate(2)
        self.assertIsNone(self.context(second).data["expected_decision_id"])
        original = self.choose()
        result = self.post_choice(second)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertIn("material_selection_changed", str(result.data))
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        fresh = self.context(second)
        self.assertEqual(fresh.data["expected_decision_id"], str(original.pk))
        result = self.post_choice(second, material_selection={"expected_decision_id": str(original.pk),
            "expected_dependencies_sha256": fresh.data["dependencies_sha256"]})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        self.assertEqual(self.selected(decision_id=original.pk, require_current=False)["artifact"].pk, first.pk)

    def test_public_selection_permissions_strict_payload_and_stopped_stage(self):
        artifact = self.candidate()
        url = f"/api/v1/production-runs/{self.run.pk}/select-artifact/"
        for payload in ({"artifact_id": str(artifact.pk)},
                {"artifact_id": str(artifact.pk), "material_selection": {}},
                {"artifact_id": str(artifact.pk), "material_selection": {"expected_decision_id": None, "force": True}}):
            result = self.client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
            self.assertEqual(result.status_code, 400, result.data)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        self.assertFalse(self.context(artifact).data["can_select"])
        self.assertEqual(self.post_choice(artifact).status_code, 400)
        stranger = get_user_model().objects.create_user(username="public-selection-stranger", password="isolated")
        self.client.force_authenticate(stranger)
        self.assertEqual(self.context(artifact).status_code, 404)
        self.assertEqual(self.post_choice(artifact).status_code, 404)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
