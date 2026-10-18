"""Exact saved-choice review and explicit Continue, isolated original receipts.

M4 fulfillment/files, the selection command and policy review are real. The
fixture's unfinished media stage deliberately blocks before any model dispatch;
this is not full recipe admission, request execution or manual review acceptance.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, ShotReferenceUse
from studio.services.production_runs import StageBlocked, _save_stage_snapshot, advance_production_run
from studio.services.production_storyboard_policy import read_recipe_storyboard_reference_selection
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_shot_reference_commands as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeShotReferenceRecoveryTests(StructureFixture):
    CommandFixture = fixtures.RecipeShotReferenceCommandTests
    Application = CommandFixture.Application
    Fixture = CommandFixture.Fixture
    extra_before_media_declarations = CommandFixture.extra_before_media_declarations
    setUp = CommandFixture.setUp
    tearDown = CommandFixture.tearDown
    facts = CommandFixture.facts
    attach = CommandFixture.attach
    choose = CommandFixture.choose
    create_job = CommandFixture.create_job
    primary_version = CommandFixture.primary_version
    write = CommandFixture.write
    input = staticmethod(CommandFixture.input)
    configure_variant_sources = CommandFixture.configure_variant_sources
    configure_declaration = CommandFixture.configure_declaration
    freeze_source = CommandFixture.freeze_source
    target = CommandFixture.target
    prepare = CommandFixture.prepare
    complete = CommandFixture.complete
    use = CommandFixture.use
    media = CommandFixture.media
    detail = CommandFixture.detail
    body = CommandFixture.body
    post = CommandFixture.post
    asset_choice = CommandFixture.asset_choice
    apply = Application.apply

    def create_prior_media_stages(self):
        self.CommandFixture.create_prior_media_stages(self)
        if self._testMethodName in {
                "test_saved_choice_does_not_resume_waiting_and_implicit_advance_is_quiet",
                "test_explicit_continue_with_missing_use_stays_waiting_without_approval_or_call",
                "test_explicit_continue_rechecks_saved_choices_and_retains_policy_actor",
                "test_changed_upstream_on_explicit_continue_blocks_without_refreezing_or_approval"}:
            # Declare the missing setting before the review freezes its original
            # storyboard input, not by replacing that input after admission.
            ShotReferenceUse.objects.filter(pk=self.uses[1].pk).update(setting_revision=None)
            self.freeze_source()

    def select_both(self):
        for index in range(2):
            result = self.post(self.body(index=index, key=f"explicit-{index}"))
            self.assertEqual(result.status_code, 201, result.data)

    def read(self):
        return read_recipe_storyboard_reference_selection(self.run)

    def policy_context(self):
        # Same declared-stage boundary as application tests: retain real root,
        # frozen input/source and approval verification, not full admission.
        self.enterContext(patch("studio.services.production_runs._assert_frozen_bible_run_closure"))
        self.enterContext(patch("studio.services.production_runs._verified_unattended_decision_policy",
            return_value=(self.review_declaration["decision_policy"], "f" * 64)))
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=False)

    def wait(self):
        self.policy_context()
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.status, self.run.blocked_code),
            ("awaiting_input", "waiting_shot_reference_selection"), self.run.blocked_reason)

    def test_readonly_selection_hash_binds_exact_versions_without_approval_or_writes(self):
        self.select_both()
        before = list(self.run.stages.values())
        with CaptureQueriesContext(connection) as queries:
            value = self.read()
            self.assertEqual(self.read(), value)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(value["shot_count"], 2)
        self.assertEqual(value["source_snapshot_sha256"], self.source.output_snapshot_sha256)
        for row, fulfillment in zip(value["selection"]["shots"], self.fulfillments[1:]):
            self.assertEqual(row["uses"][0]["source"]["reference_version_id"], str(fulfillment.reference_version_id))
        self.assertEqual(before, list(self.run.stages.values()))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_reviewer_rejects_forged_round_origin_and_non_upstream_asset(self):
        self.select_both()
        ShotReferenceUse.objects.filter(pk=self.uses[0].pk).update(source_mode="generated")
        # The shared source reader rejects forged acquisition provenance before
        # the round-membership classifier; it is not recoverable source drift.
        with self.assertRaisesMessage(ValidationError, "已有或上传版本不能当成本次生成来源"):
            self.read()
        asset = self.media()
        ShotReferenceUse.objects.filter(pk=self.uses[0].pk).update(source_mode="existing", reference_version=None,
            asset=asset, asset_sha256=asset.checksum)
        with self.assertRaisesMessage(ValidationError, "本轮冻结上游"):
            self.read()
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_missing_file_rejects_current_selection_without_mutating_source_or_review(self):
        self.select_both()
        before = deepcopy(self.review.input_snapshot)
        Path(self.fulfillments[1].reference_version.asset.file.path).unlink()  # Disposable test media only.
        with self.assertRaises(ValidationError):
            self.read()
        self.review.refresh_from_db()
        self.assertEqual(self.review.input_snapshot, before)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_existing_selection_hash_changes_but_creative_source_and_sibling_do_not(self):
        assets = [self.media(), self.media(), self.media()]
        for index, asset in enumerate(assets[:2]):
            result = self.post(self.body(index=index, key=f"existing-{index}",
                choice=self.asset_choice(asset, index=index, mode="existing")))
            self.assertEqual(result.status_code, 201, result.data)
        before = self.read()
        # Use the shared writer to isolate review of a changed selection. Frozen
        # override permission is separately enforced at the original command.
        from studio.services.shot_reference_choices import apply_shot_reference_choices
        detail = self.detail()
        apply_shot_reference_choices(actor=self.user, project_id=self.project.pk, shot_id=self.shots[0].pk,
            expected_uses_sha256=detail["uses_sha256"], choices=[self.asset_choice(assets[2], mode="existing")])
        after = self.read()
        self.assertNotEqual(before["selection_sha256"], after["selection_sha256"])
        self.assertEqual(before["source_snapshot_sha256"], after["source_snapshot_sha256"])
        self.assertEqual(before["selection"]["shots"][1], after["selection"]["shots"][1])
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_missing_choice_and_target_rejection_never_produce_review_evidence(self):
        with self.assertRaises(ValidationError):
            self.read()
        self.select_both()
        self.validator.side_effect = ValidationError("isolated target contract rejection")
        with self.assertRaisesMessage(ValidationError, "target contract rejection"):
            self.read()
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_saved_choice_does_not_resume_waiting_and_implicit_advance_is_quiet(self):
        self.wait()
        result = self.post(self.body(key="first-selected"))
        self.assertEqual(result.status_code, 201, result.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_input")
        with patch("studio.services.production_runs._execute_stage", side_effect=AssertionError("No implicit recovery")):
            self.assertEqual(advance_production_run(self.run).status, "awaiting_input")
            self.assertEqual(advance_production_run(self.run, automatic=True).status, "awaiting_input")
            self.assertEqual(advance_production_run(self.run, automatic=True, human_initiated=True).status, "awaiting_input")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_explicit_continue_with_missing_use_stays_waiting_without_approval_or_call(self):
        self.wait()
        result = self.post(self.body(key="partial-selection"))
        self.assertEqual(result.status_code, 201, result.data)
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/continue/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db(); self.review.refresh_from_db()
        self.assertEqual((self.run.status, self.review.status, self.review.reason_code),
            ("awaiting_input", "running", "waiting_shot_reference_selection"))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_explicit_continue_rechecks_saved_choices_and_retains_policy_actor(self):
        self.wait()
        self.select_both()
        selected = self.read()
        from studio.services.production_runs import _execute_stage
        def until_media(run, stage, **kwargs):
            if stage.pk == self.media_stage.pk:
                raise StageBlocked("isolated_media_not_executed", "No external execution in this fixture")
            return _execute_stage(run, stage, **kwargs)
        with patch("studio.services.production_runs._execute_stage", side_effect=until_media):
            response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/continue/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db(); self.review.refresh_from_db()
        self.assertEqual((self.run.status, self.run.blocked_code), ("blocked", "isolated_media_not_executed"))
        self.assertEqual(self.review.status, "succeeded")
        approval = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual((approval.actor, approval.decided_by_id), ("recipe_policy", None))
        self.assertEqual(self.review.output_snapshot["policy_evidence"]["selection_sha256"], selected["selection_sha256"])
        self.assertEqual(self.read(), selected)
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_changed_upstream_on_explicit_continue_blocks_without_refreezing_or_approval(self):
        self.wait()
        self.select_both()
        before = deepcopy(self.review.input_snapshot)
        _save_stage_snapshot(self.stage, "output_snapshot", {**self.stage.output_snapshot, "changed": True})
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/continue/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db(); self.review.refresh_from_db()
        self.assertEqual(self.run.status, "blocked")
        self.assertEqual(self.review.input_snapshot, before)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())
