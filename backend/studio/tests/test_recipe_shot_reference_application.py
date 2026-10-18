"""Real M4 receipts/files -> frozen M5 choices in isolated original Run stages.

The downstream media validator is a recording boundary in this selection-only
fixture; exact-target validation is separately covered by the policy/mapping
suites. No fixture substitutes the source reader, matching plan or writer.
"""
from copy import deepcopy
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import ApprovalDecision, Episode, ExecutionPreparation, GenerationAttempt, Shot, ShotReferenceUse, StageRun
from studio.services.production_runs import (
    StageBlocked, _assert_frozen_formal_objects, _execute_unattended_policy_review,
    _formal_snapshot, _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot,
)
from studio.services.production_storyboard_policy import approve_recipe_storyboard_references
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
from studio.tests import test_recipe_shot_reference_policy as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeShotReferenceApplicationTests(fixtures.StructureFixture):
    Fixture = fixtures.FrozenShotReferencePolicyTests.Fixture
    review_declaration = {**fixtures.REVIEW, "approval": "policy",
        "decision_policy": UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"]}
    extra_before_media_declarations = [{"key": "storyboard", "mode": "deterministic", "approval": "auto"}]
    tearDown = fixtures.FrozenShotReferencePolicyTests.tearDown
    facts = fixtures.FrozenShotReferencePolicyTests.facts
    attach = fixtures.FrozenShotReferencePolicyTests.attach
    choose = fixtures.FrozenShotReferencePolicyTests.choose
    create_job = fixtures.FrozenShotReferencePolicyTests.create_job
    primary_version = fixtures.FrozenShotReferencePolicyTests.primary_version
    write = fixtures.FrozenShotReferencePolicyTests.write
    input = staticmethod(fixtures.FrozenShotReferencePolicyTests.input)
    configure_variant_sources = fixtures.FrozenShotReferencePolicyTests.configure_variant_sources
    target = fixtures.FrozenShotReferencePolicyTests.target
    prepare = fixtures.FrozenShotReferencePolicyTests.prepare
    complete = fixtures.FrozenShotReferencePolicyTests.complete
    use = fixtures.FrozenShotReferencePolicyTests.use

    def configure_declaration(self, declaration):
        route = deepcopy(declaration["routing"])
        self.extra_after_review_declarations = [{"key": "media_generation", "mode": "existing_pipeline",
            "approval": "required", "routing": route}]
        fixtures.FrozenShotReferencePolicyTests.configure_declaration(self, declaration)

    def create_prior_media_stages(self):
        episode = Episode.objects.create(project=self.project, number=91, title="Isolated declared uses")
        self.shots = [Shot.objects.create(episode=episode, number=number, duration_ms=1000,
            media_type="image", include_audio=False) for number in (1, 2)]
        self.uses = [self.use(setting=setting, shot=shot) for setting, shot in zip(self.settings, self.shots)]
        if self._testMethodName in {"test_missing_later_use_waits_without_applying_earlier_available_shot",
                "test_required_review_wait_reason_does_not_approve",
                "test_waiting_policy_does_not_create_review_or_call",
                "test_scheduler_waits_for_selection_and_ordinary_wake_does_not_resume"}:
            ShotReferenceUse.objects.filter(pk=self.uses[1].pk).update(setting_revision=None)
        self.source = StageRun.objects.create(production_run=self.run, stage_key="storyboard", ordinal=2,
            mode="deterministic", status="succeeded", idempotency_key=str(uuid4()))
        self.freeze_source()

    def setUp(self):
        self.Fixture.setUp(self)
        self.fulfillments = self.complete()
        self.review.stage_key, self.review.ordinal = "storyboard_review", 4
        self.review.decision_policy = self.review_declaration["decision_policy"]
        self.review.decision_policy_sha256 = "f" * 64
        self.review.save(update_fields=("stage_key", "ordinal", "decision_policy", "decision_policy_sha256"))
        self.run.status, self.run.current_stage_key = "running", self.review.stage_key
        self.run.save(update_fields=("status", "current_stage_key"))
        self.media_stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=5,
            mode="existing_pipeline", status="pending", idempotency_key=str(uuid4()))
        _freeze_stage_config(self.run, self.review)
        _freeze_stage_input(self.run, self.review)
        self.validator = Mock()
        self.enterContext(patch("studio.services.production_recipe_media_preparations.recipe_reference_selection_validator",
            return_value=self.validator))

    def freeze_source(self):
        _save_stage_snapshot(self.source, "output_snapshot", _formal_snapshot(self.run, self.source, "isolated-declared-shots"))

    def apply(self):
        return approve_recipe_storyboard_references(self.run)

    def test_two_shots_take_different_exact_costumes_without_primary_or_legacy_writes(self):
        primary = self.character.anchor_asset_id
        before = list(Shot.objects.filter(pk__in=[shot.pk for shot in self.shots]).order_by("pk").values())
        source_sha = self.source.output_snapshot_sha256
        result = self.apply()
        self.assertEqual(result["shot_count"], 2)
        for use, fulfillment in zip(self.uses, self.fulfillments[1:]):
            use.refresh_from_db()
            self.assertEqual(use.reference_version_id, fulfillment.reference_version_id)
            self.assertEqual(use.source_mode, "existing")
        self.assertNotEqual(self.uses[0].reference_version_id, self.uses[1].reference_version_id)
        self.character.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, primary)
        self.assertEqual(self.source.output_snapshot_sha256, source_sha)
        self.assertEqual(before, list(Shot.objects.filter(pk__in=[shot.pk for shot in self.shots]).order_by("pk").values()))
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())
        calls = self.validator.call_args_list
        self.assertEqual([call.kwargs["records"][0]["purpose"] for call in calls], ["本镜校服参考"] * 2)
        self.assertEqual([call.kwargs["records"][0]["source_key"] for call in calls], ["images"] * 2)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.apply(), result)
        self.assertFalse([q for q in queries if q["sql"].startswith(("INSERT", "UPDATE", "DELETE"))])
        _assert_frozen_formal_objects(self.run)

    def test_missing_later_use_waits_without_applying_earlier_available_shot(self):
        self.assertIsNone(self.apply())
        self.review.refresh_from_db()
        self.assertEqual(self.review.reason_code, "waiting_shot_reference_selection")
        self.assertIn("镜头2/clothes", self.review.reason)
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.validator.assert_not_called()

    def test_required_review_wait_reason_does_not_approve(self):
        from studio.services.production_runs import _execute_stage
        self.review.decision_policy = ""
        self.review.approval_required = True
        with self.assertRaises(StageBlocked) as error:
            _execute_stage(self.run, self.review)
        self.assertEqual(error.exception.code, "waiting_shot_reference_selection")
        self.assertTrue(error.exception.awaiting_approval)
        self.assertIn("镜头2/clothes", error.exception.reason)
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_prepared_later_shot_rolls_back_earlier_selection(self):
        ExecutionPreparation.objects.create(stage_run=self.media_stage, execution_instance_version=self.version,
            purpose="production", unit_key=f"shot:{self.shots[1].pk}", request_key="isolated-pending",
            created_by=self.user, status="awaiting_input", intent_sha256="a" * 64)
        with self.assertRaisesMessage(ValidationError, "已有执行准备"):
            self.apply()
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.validator.assert_not_called()

    def test_cancelled_unused_preparation_is_editable_but_frozen_cancel_is_not(self):
        prep = ExecutionPreparation.objects.create(stage_run=self.media_stage, execution_instance_version=self.version,
            purpose="production", unit_key=f"shot:{self.shots[1].pk}", request_key="isolated-cancelled",
            created_by=self.user, status="cancelled", cancelled_at=timezone.now(), intent_sha256="a" * 64)
        with transaction.atomic():
            self.assertIsNotNone(self.apply())
            transaction.set_rollback(True)
        prep.request_frozen_at, prep.request_sha256 = timezone.now(), "b" * 64
        prep.save(update_fields=("request_frozen_at", "request_sha256"))
        with self.assertRaisesMessage(ValidationError, "已有执行准备"):
            self.apply()
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())

    def test_later_validator_failure_rolls_back_both_shots_without_approval(self):
        self.validator.side_effect = [None, ValidationError("isolated target rejects later shot")]
        with self.assertRaisesMessage(ValidationError, "target rejects"):
            self.apply()
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_creative_drift_and_changed_upstream_do_not_choose_or_refreeze(self):
        self.shots[0].narrative = "changed raw author field"
        self.shots[0].save(update_fields=("narrative",))
        with self.assertRaisesMessage(ValidationError, "已在审核证据生成后变化"):
            self.apply()
        self.shots[0].narrative = ""
        self.shots[0].save(update_fields=("narrative",))
        _save_stage_snapshot(self.stage, "output_snapshot", {**self.stage.output_snapshot, "changed": True})
        with self.assertRaisesMessage(ValidationError, "上游已改变"):
            self.apply()
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())

    def test_original_policy_writes_nonhuman_approval_and_receipt_failure_rolls_back(self):
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"]
        # This fixture isolates policy authority as the existing M4 review suite
        # does; it does not claim admission/full unattended execution.
        with patch("studio.services.production_runs._verified_unattended_decision_policy", return_value=(policy, "f" * 64)):
            with patch.object(ApprovalDecision, "save", side_effect=RuntimeError("isolated receipt failure")):
                with self.assertRaisesRegex(RuntimeError, "receipt failure"):
                    _execute_unattended_policy_review(self.run, self.review)
            self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
            self.assertTrue(_execute_unattended_policy_review(self.run, self.review).startswith("approval:"))
        approval = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual((approval.actor, approval.decided_by_id), ("recipe_policy", None))
        self.review.status = "succeeded"; self.review.save(update_fields=("status",))
        _assert_frozen_formal_objects(self.run)
        ShotReferenceUse.objects.filter(pk=self.uses[0].pk).update(asset_sha256="0" * 64)
        with self.assertRaises(StageBlocked) as error:
            _assert_frozen_formal_objects(self.run)
        self.assertEqual(error.exception.code, "storyboard_snapshot_drift")

    def test_waiting_policy_does_not_create_review_or_call(self):
        with patch("studio.services.production_runs._verified_unattended_decision_policy", return_value=(
                UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"], "f" * 64)):
            self.assertIsNone(_execute_unattended_policy_review(self.run, self.review))
        self.review.refresh_from_db()
        self.assertEqual(self.review.reason_code, "waiting_shot_reference_selection")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_scheduler_waits_for_selection_and_ordinary_wake_does_not_resume(self):
        from studio.services.json_contracts import assign_json_contract, json_contract
        from studio.services.production_runs import advance_production_run
        # A declared-stage fixture, not full recipe admission or a live receipt.
        # Keep the real root/plan/stage verification active for this scheduler test.
        intent = json_contract({"schema_version": 4, "plan_source": "recipe", "project_id": str(self.project.pk),
            "recipe_version_id": str(self.run.recipe_version_id), "script_revision_id": None, "episode_ids": [],
            "creation_inputs": {"idea": "", "style_hint": "", "episode_count": None,
                "shots_per_episode": None, "episode_duration_seconds": None}, "unattended_confirmed": False,
            "project_source_confirmations": [], "allow_external_calls": False, "config_overrides": {}})
        self.run.request_sha256 = intent.sha256
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 4, "request_intent": intent.value,
            "run_plan_sha256": self.plan.sha256}, schema_version=4)
        self.run.save(update_fields=("request_sha256", "input_snapshot", "input_snapshot_schema_version",
            "input_snapshot_size_bytes", "input_snapshot_sha256"))
        with patch("studio.services.production_runs._assert_frozen_bible_run_closure"), patch(
                "studio.services.production_runs._verified_unattended_decision_policy", return_value=(
                    self.review_declaration["decision_policy"], "f" * 64)):
            waiting = advance_production_run(self.run)
        self.assertEqual((waiting.status, waiting.blocked_code), ("awaiting_input", "waiting_shot_reference_selection"))
        with patch("studio.services.production_runs._execute_stage", side_effect=AssertionError("No retry on ordinary wake")):
            self.assertEqual(advance_production_run(waiting, automatic=True).status, "awaiting_input")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "running")
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())
