"""Same M2 source through the real scheduler/review writers, isolated DB only.

The next stage is deliberately blocked: not full recipe publication, model or
delivery acceptance. No provider, media creation or live business mutations.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ApprovalDecision, BibleVariant, BibleVariantReview, GenerationAttempt, ScriptBibleEntityBinding, StageRun
from studio.services.bible_content_review import recipe_bible_review_detail
from studio.services.bible_variants import review_variant, revise_variant, setting_payload
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import advance_production_run, decide_stage, _sha256
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
from studio.services.production_reviewed_content import read_reviewed_bible, read_reviewed_entity_primaries
from studio.services.script_storyboard_plan import read_storyboard_source_plan
from studio.services.security_boundaries import boundary_int
from studio.services.script_bible_materialization import freeze_script_reference_bible, materialize_script_reference_bible
from studio.tests import test_production_run_plans as fixtures
from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source


class BiblePolicyContinuationTests(TestCase):
    root = fixtures.ProductionRunPlanTests.root
    recipe = fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        fixtures.ProductionRunPlanTests.setUp(self)
        self.revision = confirmed_structured_bible_source(self, episodes=[{"episode": 1, "shots": [{
            "shot": 1, "character_keys": ["person_a"], "scene_key": "station", "references": [
                {"use_key": "hero", "entity_key": "person_a", "variant_key": "uniform", "media_kind": "image", "purpose": "appearance"}]}]}])

    def build(self, automatic=True, authorized=True, following_stages=()):
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["bible_review"]
        definition = {"schema_version": 3, "interaction_mode": "unattended" if automatic else "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "policy" if automatic else "required",
                **({"decision_policy": policy} if automatic else {})},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"}] + list(following_stages)}
        recipe = self.recipe(definition=definition)
        run = self.root(plan_source="recipe", recipe_version=recipe, script_revision=self.revision, status="queued")
        assign_json_contract(run, "config_snapshot", resolve_configuration(project=self.project).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        run.save()
        freeze_run_execution_plan(run=run, **{**self.request, "initial_content": {
            "kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256}})
        freeze_recipe_request_fixture(run, authorized=authorized)
        for index, declaration in enumerate(definition["stages"], 1):
            is_policy = declaration.get("approval") == "policy"
            stage_policy = declaration.get("decision_policy", "")
            StageRun.objects.create(production_run=run, ordinal=index, stage_key=declaration["key"], mode=declaration["mode"],
                status="blocked" if index == 4 else "pending", reason_code="isolated_next_stage" if index == 4 else "",
                approval_required=declaration["approval"] == "required", idempotency_key=str(uuid4()),
                decision_policy=stage_policy if is_policy else "",
                decision_policy_sha256=_sha256({"schema_version": 1, "interaction_mode": "unattended",
                    "stage_key": declaration["key"], "decision_policy": stage_policy,
                    "recipe_definition_sha256": recipe.definition_sha256}) if is_policy else "")
        return run

    def reviewed(self, run):
        return read_reviewed_bible(run=run, eligible_stage_ids=list(run.stages.values_list("pk", flat=True)))

    def test_auto_materializes_usable_exact_settings_without_human_decisions_and_replay_is_idle(self):
        run = advance_production_run(self.build(), automatic=True)
        review = run.stages.get(stage_key="bible_review")
        self.assertEqual(review.status, "succeeded")
        self.assertEqual(run.blocked_code, "isolated_next_stage")
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertFalse(BibleVariantReview.objects.exists())
        decision = ApprovalDecision.objects.get(stage_run=review)
        self.assertEqual(decision.actor, "recipe_policy")
        self.assertIsNone(decision.decided_by_id)
        self.assertEqual(decision.decision_policy_sha256, review.decision_policy_sha256)
        self.assertEqual(decision.output_snapshot_sha256, review.output_snapshot_sha256)
        from studio.platform_serializers import ApprovalDecisionFactSerializer
        fact = ApprovalDecisionFactSerializer(decision).data
        self.assertEqual(fact["actor"], "recipe_policy")
        self.assertIsNone(fact["decided_by"])
        self.assertEqual(review.output_snapshot["policy_evidence"]["kind"], "bible_content_policy")
        self.assertNotIn("reviewed_setting_keys", str(review.output_snapshot))
        read = self.reviewed(run)
        primary = read_reviewed_entity_primaries({("character", row["id"]): row
            for row in read["canonical"]["characters"]}, project_id=run.project_id)
        self.assertEqual(next(iter(primary.values()))[0]["identity_anchor"], "黑色短发")
        plan = read_storyboard_source_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
        use = plan.episodes[0]["shots"][0]["relations"]["uses"][0]
        self.assertEqual(use["variant_key"], "uniform")
        self.assertIsNone(use["review_id"])
        self.assertEqual(plan.basis["bible_review"]["decision_id"], str(decision.pk))
        before = list(ApprovalDecision.objects.values())
        advance_production_run(run, automatic=True)
        self.assertEqual(list(ApprovalDecision.objects.values()), before)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_assisted_same_source_waits_for_real_complete_human_confirmation(self):
        run = advance_production_run(self.build(automatic=False), automatic=True)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.current_stage_key, "bible_review")
        self.assertFalse(BibleVariant.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())
        advance_production_run(run, automatic=True)
        self.assertFalse(ApprovalDecision.objects.exists())
        detail = recipe_bible_review_detail(run)
        confirmation = {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
            "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
            "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
            "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]}
        with self.assertRaises(ValidationError):
            decide_stage(run, stage_key="bible_review", decision="approve", comment="isolated actual action", user=self.actor,
                bible_review={**confirmation, "reviewed_setting_keys": []})
        self.assertFalse(BibleVariant.objects.exists())
        run = decide_stage(run, stage_key="bible_review", decision="approve", comment="isolated actual action", user=self.actor,
            bible_review=confirmation)
        self.assertEqual(run.stages.get(stage_key="bible_review").status, "succeeded")
        self.assertEqual(BibleVariantReview.objects.count(), 4)
        decision = ApprovalDecision.objects.get()
        self.assertEqual((decision.actor, decision.decided_by_id, decision.decision_policy), ("human", self.actor.pk, ""))
        plan = read_storyboard_source_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
        self.assertIsNotNone(plan.episodes[0]["shots"][0]["relations"]["uses"][0]["review_id"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_no_authority_or_changed_policy_cannot_materialize_or_approve(self):
        for authorized in (False, True):
            run = self.build(authorized=authorized)
            if authorized:
                StageRun.objects.filter(production_run=run, stage_key="bible_review").update(decision_policy_sha256="0" * 64)
            result = advance_production_run(run, automatic=True)
            self.assertEqual(result.status, "blocked")
            self.assertTrue(result.blocked_code.startswith("unattended_policy_"))
            self.assertFalse(BibleVariant.objects.exists())
            self.assertFalse(ApprovalDecision.objects.exists())

    def test_rejected_setting_blocks_auto_without_rewriting_history_or_partial_materialization(self):
        materialize_script_reference_bible(actor=self.actor, project_id=self.project.pk, script_revision_id=self.revision.pk,
            expected_source=freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk))
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        setting = variant.revisions.get()
        review_variant(actor=self.actor, variant=variant, expected_revision=1, expected_sha256=setting.content_sha256,
            decision="reject", comment="human rejection")
        before = list(BibleVariantReview.objects.values())
        result = advance_production_run(self.build(), automatic=True)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_code, "bible_content_policy_invalid")
        self.assertEqual(list(BibleVariantReview.objects.values()), before)
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_decision_storage_failure_rolls_back_materialization_and_source_remains_saved(self):
        run = self.build()
        with patch("studio.services.production_runs.ApprovalDecision.objects.create", side_effect=ValidationError("isolated write failure")):
            result = advance_production_run(run, automatic=True)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.stages.get(stage_key="bible_materialize").status, "succeeded")
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())
        self.assertFalse(BibleVariant.objects.exists())
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_changed_setting_invalidates_downstream_without_rewriting_prior_policy(self):
        run = advance_production_run(self.build(), automatic=True)
        review = run.stages.get(stage_key="bible_review")
        before = deepcopy(review.output_snapshot)
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get()), "appearance_anchor": "human revised blue coat"})
        with self.assertRaises(ValidationError) as error:
            self.reviewed(run)
        self.assertEqual(error.exception.code, "reference_review_changed")
        review.refresh_from_db()
        self.assertEqual(review.output_snapshot, before)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_policy_read_checks_source_receipt_and_authority_not_just_actor_label(self):
        run = advance_production_run(self.build(), automatic=True)
        review = run.stages.get(stage_key="bible_review")
        review.decision_policy_sha256 = "0" * 64
        review.save(update_fields=("decision_policy_sha256",))
        with self.assertRaises(ValidationError):
            self.reviewed(run)

    def test_auto_keeps_original_human_decision_and_current_manual_revision(self):
        materialize_script_reference_bible(actor=self.actor, project_id=self.project.pk, script_revision_id=self.revision.pk,
            expected_source=freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk))
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get()), "appearance_anchor": "人工选定的蓝色制服"})
        current = variant.revisions.get(revision=2)
        review_variant(actor=self.actor, variant=variant, expected_revision=2, expected_sha256=current.content_sha256,
            decision="approve", comment="实际人工核对")
        before = list(BibleVariantReview.objects.values())
        run = advance_production_run(self.build(), automatic=True)
        self.assertEqual(run.stages.get(stage_key="bible_review").status, "succeeded")
        self.assertEqual(list(BibleVariantReview.objects.values()), before)
        plan = read_storyboard_source_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
        use = plan.episodes[0]["shots"][0]["relations"]["uses"][0]
        self.assertEqual(use["setting_revision_id"], str(current.pk))
        self.assertEqual(use["review_id"], str(before[0]["id"]))
        self.assertEqual(BibleVariant.objects.count(), 4)

    def test_policy_decision_with_wrong_registered_hash_is_not_usable_downstream(self):
        run = advance_production_run(self.build(), automatic=True)
        review = run.stages.get(stage_key="bible_review")
        ApprovalDecision.objects.create(stage_run=review, revision=2, decision="approve", actor="recipe_policy",
            decided_by=None, decision_policy=review.decision_policy, decision_policy_sha256="0" * 64,
            output_snapshot_sha256=review.output_snapshot_sha256)
        with self.assertRaises(ValidationError) as error:
            read_storyboard_source_plan(run=run, stage=run.stages.get(stage_key="storyboard"))
        self.assertEqual(error.exception.code, "reference_review_invalid")

    def test_auto_still_validates_explicit_setting_asset_basis_and_rolls_back_on_error(self):
        run = self.build()
        with patch("studio.services.bible_variants._basis", side_effect=ValidationError("isolated unavailable basis")) as validate:
            result = advance_production_run(run, automatic=True)
        self.assertTrue(validate.called)
        self.assertEqual(result.status, "blocked")
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())
        self.assertFalse(BibleVariant.objects.exists())
