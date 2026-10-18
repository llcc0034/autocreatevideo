"""M4 replacement with original auxiliary workers; isolated responses only."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from studio.models import ArtifactSelectionDecision, ExecutionPreparation, GenerationAttempt
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_auxiliary_requests import read_production_field_context
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.execution_production_auxiliaries import create_production_field_child
from studio.services.production_recipe_material_repreparation import _require_settled_auxiliaries
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_repreparation as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialAuxiliaryRepreparationTests(StructureFixture):
    create_job = fixtures.RecipeMaterialRepreparationTests.create_job
    primary_version = fixtures.RecipeMaterialRepreparationTests.primary_version
    write = fixtures.RecipeMaterialRepreparationTests.write
    input = staticmethod(fixtures.RecipeMaterialRepreparationTests.input)
    configure_variant_sources = fixtures.RecipeMaterialRepreparationTests.configure_variant_sources
    variant_anchors = fixtures.RecipeMaterialRepreparationTests.variant_anchors
    select = fixtures.RecipeMaterialRepreparationTests.select
    setUp = fixtures.RecipeMaterialRepreparationTests.setUp
    tearDown = fixtures.RecipeMaterialRepreparationTests.tearDown
    allow_calls = fixtures.RecipeMaterialRepreparationTests.allow_calls
    advance = fixtures.RecipeMaterialRepreparationTests.advance
    fill_fields = fixtures.RecipeMaterialRepreparationTests.fill_fields
    collect = fixtures.RecipeMaterialRepreparationTests.collect
    configure_declaration = fixtures.RecipeMaterialRepreparationTests.configure_declaration
    untouched_variants = fixtures.RecipeMaterialRepreparationTests.untouched_variants
    public_context = fixtures.RecipeMaterialRepreparationTests.public_context
    public_change = fixtures.RecipeMaterialRepreparationTests.public_change

    def child(self, parent):
        parent.refresh_from_db()
        batch = read_execution_preparation_detail(parent).resolved_inputs["field_requests"][0]
        child, created = create_production_field_child(preparation_id=parent.pk,
            expected_preparation_revision=parent.revision, field_batch_key=batch["sha256"],
            attempt_number=1, actor=self.user)
        self.assertTrue(created)
        return child

    def dispatch(self, child):
        child.refresh_from_db()
        result = advance_execution_preparation(preparation_id=child.pk,
            expected_preparation_revision=child.revision, actor=self.user)
        self.assertEqual(result.status, "dispatched", result.reason)
        return GenerationAttempt.objects.get(preparation=child)

    def complete_auxiliary(self, child, *, malformed=False, claimed=None):
        claimed = claimed or claim_next_text_attempt("isolated-material-fields")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.preparation_id, child.pk)
        child.refresh_from_db()
        batch = read_production_field_context(child)[1]
        peer = Mock()
        peer.invoke.return_value = {"text": "not-json" if malformed else json.dumps({"items": [
            {"request_id": item["request_id"], "text": "A courier opens the parcel beside the blue box."}
            for item in batch.value["items"]]}), "request_id": "isolated-material-auxiliary"}
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer), patch(
                "studio.services.production_run_auto_advance.schedule_recipe_run_advance"):
            result = process_text_attempt(claimed)
        peer.invoke.assert_called_once()
        self.assertEqual(result.status, "awaiting_review" if malformed else "succeeded", result.error_message)
        return result

    def test_settled_success_and_rejected_response_preserved_on_original_preparations(self):
        from studio.services.asset_references import read_generated_material
        self.untouched_variants()
        children = [self.child(parent) for parent in self.parents]
        for child in children:
            self.dispatch(child)
        first = self.complete_auxiliary(children[0])
        second = self.complete_auxiliary(children[1], malformed=True)
        self.assertGreater(self.parents[0].field_values.count(), 0)
        evidence = list(GenerationAttempt.objects.values())
        child_rows = list(ExecutionPreparation.objects.filter(pk__in=[row.pk for row in children]).values())
        fields = [list(parent.field_values.values()) for parent in self.parents]
        for parent in self.parents:
            parent.refresh_from_db()
        details = [deepcopy(read_execution_preparation_detail(parent).resolved_inputs) for parent in self.parents]
        context = self.public_context()
        self.assertTrue(context["can_select"], context)
        self.assertEqual({row["action"] for row in context["dependent_preparations"]}, {"reprepare"})
        response = self.public_change(context)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.public_change(context).status_code, 200)
        self.assertEqual(list(GenerationAttempt.objects.values()), evidence)
        self.assertEqual(list(ExecutionPreparation.objects.filter(pk__in=[row.pk for row in children]).values()), child_rows)
        for parent, values, resolved in zip(self.parents, fields, details):
            parent.refresh_from_db()
            self.assertEqual(parent.status, "cancelled")
            self.assertEqual(list(parent.field_values.values()), values)
            self.assertEqual(read_execution_preparation_detail(parent).resolved_inputs, resolved)
            replacement = ExecutionPreparation.objects.get(supersedes=parent)
            self.assertFalse(replacement.field_values.exists())  # No old output transplanted.
            self.assertFalse(replacement.field_selections.exists())
            with self.assertRaises(ValidationError):
                create_production_field_child(preparation_id=parent.pk,
                    expected_preparation_revision=parent.revision,
                    field_batch_key=resolved["field_requests"][0]["sha256"], attempt_number=2, actor=self.user)
        self.assertIsNone(claim_next_text_attempt("no-replay"))
        self.advance(supply_fields=True)
        results = [self.collect(), self.collect()]
        self.advance()
        self.alternative.refresh_from_db()
        primary = read_generated_material(artifact=self.alternative)
        for artifact in results:
            artifact.refresh_from_db()
            self.assertEqual(read_generated_material(artifact=artifact).generated_from_id, primary.pk)
        self.assertEqual(GenerationAttempt.objects.count(), len(evidence) + 2)
        for attempt, status in ((first, "succeeded"), (second, "awaiting_review")):
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, status)

    def test_unsubmitted_auxiliary_stop_rollback_and_digest_cover_child_only_changes(self):
        self.untouched_variants()
        children = [self.child(parent) for parent in self.parents]
        context = self.public_context()
        revisions = list(ExecutionPreparation.objects.filter(pk__in=[p.pk for p in self.parents]).values_list("revision", flat=True))
        queued = self.dispatch(children[0])
        self.assertEqual(list(ExecutionPreparation.objects.filter(pk__in=[p.pk for p in self.parents]).values_list("revision", flat=True)), revisions)
        self.assertEqual(self.public_change(context).status_code, 409)  # Only the child changed.
        context = self.public_context()
        self.assertTrue(context["can_select"], context)
        states = list(ExecutionPreparation.objects.values())
        attempts = list(GenerationAttempt.objects.values())
        self.run.refresh_from_db()
        committed = self.run.committed_cost_total
        files = {path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        invoked = []
        def prepare(**kwargs):
            invoked.append(kwargs["supersedes"].pk)
            if len(invoked) == 2:
                raise RuntimeError("isolated replacement rollback after auxiliary cancellation")
            return prepare_recipe_media_unit(**kwargs)
        with patch("studio.services.production_recipe_media_preparations.prepare_recipe_media_unit", side_effect=prepare):
            with self.assertRaisesMessage(RuntimeError, "isolated replacement rollback"):
                self.public_change(context)
        self.assertEqual(len(invoked), 2)
        self.assertEqual(list(ExecutionPreparation.objects.values()), states)
        self.assertEqual(list(GenerationAttempt.objects.values()), attempts)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, committed)
        self.assertEqual({path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}, files)
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, self.original.pk)
        response = self.public_change(context)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.public_change(context).status_code, 200)
        queued.refresh_from_db()
        self.assertEqual((queued.status, queued.error_code), ("cancelled", "execution_cancelled_before_submit"))
        self.assertIsNone(queued.submission_started_at)
        self.assertIsNone(queued.actual_cost)
        self.assertEqual(GenerationAttempt.objects.count(), len(attempts))
        self.assertIsNone(claim_next_text_attempt("old-queue-must-stay-stopped"))
        for child in children:
            child.refresh_from_db()
            self.assertEqual(child.status, "cancelled")
            result = advance_execution_preparation(preparation_id=child.pk,
                expected_preparation_revision=child.revision, actor=self.user)
            self.assertEqual(result.status, "cancelled")
        self.assertEqual(ExecutionPreparation.objects.filter(supersedes__isnull=False).count(), 2)

    def test_claim_blocks_without_mutation_then_original_response_allows_fresh_confirmation(self):
        self.untouched_variants()
        child = self.child(self.parents[0])
        self.dispatch(child)
        before_claim = self.public_context()
        claimed = claim_next_text_attempt("isolated-inflight")
        self.assertIsNotNone(claimed)
        current = self.public_context()
        self.assertFalse(current["can_select"])
        before = list(GenerationAttempt.objects.values())
        self.assertEqual(self.public_change(before_claim).status_code, 409)
        self.assertEqual(self.public_change(current).status_code, 400)
        self.assertEqual(list(GenerationAttempt.objects.values()), before)
        self.assertFalse(ExecutionPreparation.objects.filter(supersedes__isnull=False).exists())
        self.complete_auxiliary(child, claimed=claimed, malformed=True)
        fresh = self.public_context()
        self.assertTrue(fresh["can_select"], fresh)
        self.assertNotEqual(fresh["dependencies_sha256"], before_claim["dependencies_sha256"])
        self.assertEqual(self.public_change(before_claim).status_code, 409)
        self.assertEqual(self.public_change(fresh).status_code, 200)
        self.assertIsNone(claim_next_text_attempt("no-unknown-retry"))


class AuxiliaryReplacementStateTests(SimpleTestCase):
    def rows(self, **changes):
        child = SimpleNamespace(pk=uuid4(), status="dispatched", claim_token=None, claim_until=None)
        attempt = SimpleNamespace(preparation_id=child.pk, status="failed", claim_token=None,
            error_code="provider_failure", submission_started_at=None, provider_request_id="",
            ignore_late_result=False, cancel_requested_at=None, actual_cost=None, generation_task_id=None)
        for key, value in changes.items():
            setattr(attempt, key, value)
        return child, attempt

    def test_all_original_attempts_must_be_safe_not_just_latest(self):
        old, unknown = self.rows(error_code="uncertain_remote_response")
        latest, completed = self.rows(status="succeeded")
        with self.assertRaisesMessage(ValidationError, "结果未知"):
            _require_settled_auxiliaries([old, latest], [unknown, completed], stopped=False)

    def test_claimed_submitted_and_uncertain_states_remain_blocked(self):
        for state in ({"status": "running", "claim_token": uuid4()}, {"status": "submitting"},
                {"status": "submitted"}, {"status": "cancel_requested"},
                {"status": "queued", "submission_started_at": "evidence"},
                {"status": "queued", "provider_request_id": "remote"},
                {"status": "queued", "actual_cost": 0}, {"status": "succeeded", "claim_token": uuid4()}):
            with self.subTest(state=state):
                child, attempt = self.rows(**state)
                with self.assertRaises(ValidationError):
                    _require_settled_auxiliaries([child], [attempt], stopped=False)

    def test_terminal_history_preserved_but_stopped_parent_cannot_have_queued_child(self):
        for status in ("succeeded", "failed", "cancelled", "awaiting_review"):
            with self.subTest(status=status):
                child, attempt = self.rows(status=status)
                _require_settled_auxiliaries([child], [attempt], stopped=True)
        child, attempt = self.rows(status="queued")
        _require_settled_auxiliaries([child], [attempt], stopped=False)
        with self.assertRaises(ValidationError):
            _require_settled_auxiliaries([child], [attempt], stopped=True)
