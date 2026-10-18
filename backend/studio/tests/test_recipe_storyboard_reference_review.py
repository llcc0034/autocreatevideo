"""Human review of exact saved uses; real isolated M4/selection/API receipts.

Declared stages and recorded media validation isolate review from full recipe
admission and model request execution. No real provider or live data is used.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, ShotReferenceUse
from studio.platform_serializers import ApprovalDecisionInputSerializer
from studio.services.production_runs import _save_stage_snapshot
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_shot_reference_recovery as fixtures
from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeStoryboardReferenceReviewTests(StructureFixture):
    Recovery = fixtures.RecipeShotReferenceRecoveryTests
    Fixture = Recovery.Fixture
    # Share fixture helpers, not their discovered test methods.
    for _name in ("CommandFixture", "Application", "extra_before_media_declarations", "tearDown", "facts", "attach",
            "choose", "create_job", "primary_version", "write", "input", "configure_variant_sources",
            "configure_declaration", "freeze_source", "target", "prepare", "complete", "use", "media",
            "detail", "body", "post", "asset_choice", "select_both", "create_prior_media_stages"):
        locals()[_name] = getattr(fixtures.RecipeShotReferenceRecoveryTests, _name)
    input = staticmethod(Recovery.input)

    def setUp(self):
        self.Recovery.setUp(self)
        freeze_recipe_request_fixture(self.run, authorized=False)
        self.run.status, self.run.current_stage_key = "awaiting_approval", "storyboard_review"
        self.run.save(update_fields=("status", "current_stage_key"))
        self.review.status = "awaiting_approval"
        self.review.save(update_fields=("status",))
        self.review_url = f"/api/v1/production-runs/{self.run.pk}/storyboard-reference-selection/"
        self.approve_url = f"/api/v1/production-runs/{self.run.pk}/approve/"
        self.enterContext(patch("studio.services.production_runs._assert_frozen_bible_run_closure"))
        # Approval and its output remain real; do not dispatch unfinished media.
        self.enterContext(patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run))

    def read(self):
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.data

    def approve(self, observed):
        return self.client.post(self.approve_url, {"stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": observed["source_snapshot_sha256"],
            "expected_selection_sha256": observed["selection_sha256"]}, format="json")

    def test_read_and_approval_freeze_exact_uses_without_legacy_or_primary_writes(self):
        self.select_both()
        primary = self.character.anchor_asset_id
        uses = list(ShotReferenceUse.objects.order_by("pk").values())
        with CaptureQueriesContext(connection) as queries:
            observed = self.read()
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(observed["shot_count"], 2)
        self.assertEqual(observed["display"]["name_basis"], "current_bible_by_id")
        self.assertEqual({row["shot_id"] for row in observed["display"]["shots"]}, {str(shot.pk) for shot in self.shots})
        from studio.services.production_storyboard_policy import read_recipe_storyboard_reference_selection
        original = read_recipe_storyboard_reference_selection(self.run)
        self.assertEqual(observed["selection_sha256"], original["selection_sha256"])
        self.assertNotIn("display", original)
        response = self.approve(observed)
        self.assertEqual(response.status_code, 200, response.data)
        approval = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual((approval.actor, approval.decided_by_id), ("human", self.user.pk))
        self.review.refresh_from_db()
        canonical = self.review.output_snapshot["canonical"]
        shots = [shot for episode in canonical["episodes"] for shot in episode["shots"]]
        for shot, selected in zip(shots, self.fulfillments[1:]):
            self.assertEqual(shot["reference_uses"][0]["reference_version_id"], str(selected.reference_version_id))
            self.assertNotIn("reference_bindings", shot)
        self.assertEqual(uses, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.character.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, primary)
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_existing_replacement_keeps_source_but_rejects_stale_human_selection_hash(self):
        assets = [self.media(), self.media(), self.media()]
        for index in range(2):
            result = self.post(self.body(index=index, key=f"existing-{index}",
                choice=self.asset_choice(assets[index], index=index, mode="existing")))
            self.assertEqual(result.status_code, 201, result.data)
        observed = self.read()
        from studio.services.shot_reference_choices import apply_shot_reference_choices
        apply_shot_reference_choices(actor=self.user, project_id=self.project.pk, shot_id=self.shots[0].pk,
            expected_uses_sha256=self.detail()["uses_sha256"], choices=[self.asset_choice(assets[2], mode="existing")])
        latest = self.read()
        self.assertEqual(observed["source_snapshot_sha256"], latest["source_snapshot_sha256"])
        self.assertNotEqual(observed["selection_sha256"], latest["selection_sha256"])
        response = self.approve(observed)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "storyboard_reference_selection_changed")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertEqual(self.approve(latest).status_code, 200)

    def test_missing_selection_file_or_target_rejection_never_approves(self):
        self.assertEqual(self.client.get(self.review_url).status_code, 400)
        self.select_both()
        observed = self.read()
        self.validator.side_effect = ValidationError("isolated target rejects")
        self.assertEqual(self.approve(observed).status_code, 400)
        self.validator.side_effect = None
        Path(self.fulfillments[1].reference_version.asset.file.path).unlink()  # Disposable fixture only.
        self.assertEqual(self.approve(observed).status_code, 400)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_source_change_rejects_review_and_approval_without_refreezing(self):
        self.select_both()
        observed, frozen = self.read(), deepcopy(self.review.input_snapshot)
        _save_stage_snapshot(self.stage, "output_snapshot", {**self.stage.output_snapshot, "later": True})
        self.assertEqual(self.client.get(self.review_url).status_code, 400)
        self.assertEqual(self.approve(observed).status_code, 400)
        self.review.refresh_from_db()
        self.assertEqual(frozen, self.review.input_snapshot)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_approval_receipt_failure_rolls_back_without_changing_selected_uses(self):
        self.select_both()
        observed, before = self.read(), list(ShotReferenceUse.objects.order_by("pk").values())
        with patch.object(ApprovalDecision, "save", side_effect=RuntimeError("isolated approval failure")):
            with self.assertRaisesRegex(RuntimeError, "approval failure"):
                self.approve(observed)
        self.review.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((self.review.status, self.run.status), ("awaiting_approval", "awaiting_approval"))
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_read_rejects_extra_query_wrong_actor_and_non_current_review(self):
        self.assertEqual(self.client.get(self.review_url, {"shot_id": str(self.shots[0].pk)}).status_code, 400)
        other = get_user_model().objects.create_user(username="other-storyboard-review")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.review_url).status_code, 404)
        self.client.force_authenticate(self.user)
        self.run.status = "running"; self.run.save(update_fields=("status",))
        self.assertEqual(self.client.get(self.review_url).status_code, 400)


class StoryboardReferenceApprovalShapeTests(TestCase):
    def test_only_current_storyboard_approval_accepts_required_selection_hash(self):
        good = {"stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64, "expected_selection_sha256": "b" * 64}
        serializer = ApprovalDecisionInputSerializer(data=good)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        for changes in ({"expected_selection_sha256": ""}, {"expected_selection_sha256": "bad"},
                {"shot_reference_bindings": []}, {"decision": "reject"}, {"stage_key": "media_review"}):
            invalid = ApprovalDecisionInputSerializer(data={**good, **changes})
            self.assertFalse(invalid.is_valid(), changes)
        missing = dict(good); missing.pop("expected_selection_sha256")
        self.assertFalse(ApprovalDecisionInputSerializer(data=missing).is_valid())
