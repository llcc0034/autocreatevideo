"""G03 selection and original handler; formal catalog, isolated storage/provider."""
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact, GenerationAttempt
from studio.platform_serializers import ArtifactSelectionDecisionSerializer
from studio.services.generated_artifacts import select_artifact
from studio.services.production_project_text_stages import advance_recipe_project_text_stage
from studio.services.production_runs import StageBlocked, select_run_artifact
from studio.services.production_structured_text_selection import choose_structured_text_candidate
from studio.tests import test_recipe_repeatable_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StructuredTextSelectionTests(TestCase):
    setUpTestData = fixtures.RepeatableRecipePreparationTests.__dict__["setUpTestData"]
    setUp = fixtures.RepeatableRecipePreparationTests.setUp
    node = fixtures.RepeatableRecipePreparationTests.node
    create = fixtures.RepeatableRecipePreparationTests.create
    publish = fixtures.RepeatableRecipePreparationTests.publish
    output = fixtures.RepeatableRecipePreparationTests.output
    process = fixtures.RepeatableRecipePreparationTests.process
    prepare = fixtures.RepeatableRecipePreparationTests.prepare
    queue_fixture = fixtures.RepeatableRecipePreparationTests.queue_fixture
    candidate_count = 3
    evaluator = "first_succeeded"

    def candidate(self, number, *, step=None, status="succeeded"):
        step = step or self.steps[0]
        prep = self.prepare(step, number)
        self.queue_fixture(prep)
        if status == "succeeded":
            result = self.process(str(number))
            self.assertEqual(result.status, "succeeded", result.error_message)
            artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        else:
            # Only a stored terminal/pending fixture, not a fabricated provider outcome.
            GenerationAttempt.objects.filter(preparation=prep).update(status=status, actual_cost=0)
            artifact = None
        return prep, artifact

    def advance(self, step=None):
        # Preserve the actual preparation/choice/stage handler; do not enter
        # external submission admission while testing already-created attempts.
        def existing(**kwargs):
            return ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])
        with patch("studio.services.production_project_text_stages.advance_execution_preparation", side_effect=existing):
            return advance_recipe_project_text_stage(self.run, step or self.steps[0])

    def test_automatic_uses_candidate_number_despite_completion_order_and_partial_failure(self):
        _, third = self.candidate(3)
        _, second = self.candidate(2)
        self.candidate(1, status="failed")
        self.assertLess(third.variant_index, second.variant_index)
        output = self.advance()
        self.assertEqual(output, f"stage-candidates:{self.steps[0].pk}")
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual(decision.artifact_id, second.pk)
        self.assertEqual(decision.selection_mode, "automatic")
        self.assertIsNone(decision.selected_by_id)
        data = ArtifactSelectionDecisionSerializer(decision).data
        self.assertIsNone(data["selected_by"])
        self.assertEqual(data["selection_mode"], "automatic")
        from studio.services.execution_selected_outputs import _selection_reference
        reference = _selection_reference(self.run, self.steps[0], second, decision)
        self.assertIsNone(reference["selected_by"])
        self.assertEqual(reference["selection_mode"], "automatic")
        self.assertIn("INDEX", ArtifactSelectionDecision.objects.filter(stage_run=self.steps[0],
            variant_group=second.variant_group).order_by("-revision").explain())
        self.steps[0].refresh_from_db()
        self.assertEqual(self.steps[0].output_snapshot["selection"]["artifact_id"], str(second.pk))
        self.assertEqual(self.steps[0].output_snapshot["requested_candidate_count"], 3)
        self.assertEqual(GenerationAttempt.objects.filter(status="failed").count(), 1)
        self.assertEqual(self.advance(), output)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 3)

    def test_late_lower_number_does_not_replace_selected_candidate_or_resubmit(self):
        _, second = self.candidate(2)
        original = choose_structured_text_candidate(self.steps[0])
        self.assertEqual(original.artifact_id, second.pk)
        self.candidate(1)
        with patch("studio.services.production_project_text_stages.prepare_recipe_project_stage",
                side_effect=AssertionError("Selected output must not allocate new preparations")):
            self.advance()
        self.assertEqual(choose_structured_text_candidate(self.steps[0]).pk, original.pk)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)

    def test_manual_is_explicit_idempotent_and_cannot_cross_steps(self):
        _, first = self.candidate(1)
        _, second = self.candidate(2)
        selected = select_run_artifact(self.run, second, user=self.actor)
        self.assertEqual(selected.pk, second.pk)
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual(decision.selected_by_id, self.actor.pk)
        self.assertEqual(decision.selection_mode, "manual")
        select_artifact(second, user=self.actor)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        self.assertEqual(choose_structured_text_candidate(self.steps[0]).pk, decision.pk)
        with self.assertRaises(ValidationError):
            choose_structured_text_candidate(self.steps[1], actor=self.actor, artifact_id=first.pk)
        self.steps[0].status = "succeeded"
        self.steps[0].save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "已结束"):
            select_artifact(first, user=self.actor)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)

    def test_manual_can_change_unfinished_output_but_auto_does_not_override(self):
        _, first = self.candidate(1)
        _, second = self.candidate(2)
        select_artifact(first, user=self.actor)
        self.steps[1].status = "pending"
        self.steps[1].save(update_fields=("status",))
        select_artifact(second, user=self.actor)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.status, "available")
        decision = choose_structured_text_candidate(self.steps[0])
        self.assertEqual(decision.artifact_id, second.pk)
        self.steps[0].status = "succeeded"
        self.steps[0].save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "已结束"):
            select_artifact(first, user=self.actor)

    def test_no_success_blocks_and_pending_does_not_create_decision(self):
        self.assertIsNone(self.advance())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        for number in range(1, 4):
            self.candidate(number, status="failed")
        with self.assertRaisesMessage(StageBlocked, "未成功"):
            self.advance()
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_invalid_or_cancelled_candidate_cannot_be_selected(self):
        _, artifact = self.candidate(1)
        self.actor.is_active = False
        with self.assertRaises(PermissionDenied):
            select_artifact(artifact, user=self.actor)
        with self.assertRaisesMessage(ValidationError, "不能填写人工"):
            choose_structured_text_candidate(self.steps[0], actor=self.actor)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "已结束"):
            choose_structured_text_candidate(self.steps[0])
        self.run.status = "running"
        self.run.save(update_fields=("status",))
        artifact.sha256 = "f" * 64
        artifact.save(update_fields=("sha256",))
        with self.assertRaisesMessage(ValidationError, "摘要不一致"):
            choose_structured_text_candidate(self.steps[0])
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_actor_mode_database_constraint_rejects_fake_human_facts(self):
        _, artifact = self.candidate(1)
        for mode, actor in (("automatic", self.actor), ("manual", None), ("unknown", None)):
            with self.subTest(mode=mode), self.assertRaises(IntegrityError), transaction.atomic():
                ArtifactSelectionDecision.objects.create(stage_run=self.steps[0], artifact=artifact,
                    variant_group=artifact.variant_group, revision=1, selection_mode=mode, selected_by=actor)

    def test_same_named_steps_have_independent_selections_and_reads_do_not_select(self):
        from studio.services.execution_production_candidates import read_production_text_candidate
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        _, first = self.candidate(1)
        _, second = self.candidate(1, step=self.steps[1])
        with CaptureQueriesContext(connection) as queries:
            read_production_text_candidate(first)
            read_production_text_candidate(second)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        for step, artifact in zip(self.steps, (first, second)):
            self.assertEqual(choose_structured_text_candidate(step).artifact_id, artifact.pk)
        self.assertEqual(GeneratedArtifact.objects.filter(status="selected").count(), 2)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)

    def test_rejected_candidate_is_not_a_reason_to_block_other_success(self):
        _, first = self.candidate(1)
        _, second = self.candidate(2)
        first.status = "rejected"
        first.save(update_fields=("status",))
        self.assertEqual(choose_structured_text_candidate(self.steps[0]).artifact_id, second.pk)

    def test_automatic_all_rejected_is_not_reported_as_manual_selection(self):
        _, first = self.candidate(1)
        first.status = "rejected"
        first.save(update_fields=("status",))
        self.candidate(2, status="failed")
        pending, _ = self.candidate(3, status="queued")
        self.assertIsNone(self.advance())
        self.assertEqual(self.steps[0].reason_code, "waiting_external_task")
        GenerationAttempt.objects.filter(preparation=pending).update(status="failed")
        with self.assertRaises(StageBlocked) as error:
            self.advance()
        self.assertEqual(error.exception.code, "execution_candidates_unavailable")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_explicit_candidate_does_not_require_other_candidate_integrity(self):
        _, first = self.candidate(1)
        _, second = self.candidate(2)
        first.sha256 = "f" * 64
        first.save(update_fields=("sha256",))
        self.assertEqual(select_artifact(second, user=self.actor).pk, second.pk)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ManualStructuredTextSelectionTests(TestCase):
    setUpTestData = fixtures.RepeatableRecipePreparationTests.__dict__["setUpTestData"]
    setUp = fixtures.RepeatableRecipePreparationTests.setUp
    node = fixtures.RepeatableRecipePreparationTests.node
    create = fixtures.RepeatableRecipePreparationTests.create
    publish = fixtures.RepeatableRecipePreparationTests.publish
    output = fixtures.RepeatableRecipePreparationTests.output
    process = fixtures.RepeatableRecipePreparationTests.process
    prepare = fixtures.RepeatableRecipePreparationTests.prepare
    queue_fixture = fixtures.RepeatableRecipePreparationTests.queue_fixture
    candidate = StructuredTextSelectionTests.candidate
    advance = StructuredTextSelectionTests.advance
    candidate_count = 2
    evaluator = None

    def test_partial_success_waits_for_manual_choice_then_original_stage_can_finish(self):
        self.candidate(1, status="failed")
        _, artifact = self.candidate(2)
        self.assertIsNone(self.advance())
        self.assertEqual(self.steps[0].reason_code, "execution_candidate_selection_required")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/",
            {"artifact_id": str(artifact.pk)}, format="json", HTTP_IDEMPOTENCY_KEY="select-structured")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.advance(), f"stage-candidates:{self.steps[0].pk}")
        self.assertEqual(ArtifactSelectionDecision.objects.get().selection_mode, "manual")
        self.assertEqual(GenerationAttempt.objects.count(), 2)
