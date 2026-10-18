"""Parent facts -> real child Job -> controlled peer -> pending field revisions."""
import json
from dataclasses import replace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob, ExecutionFieldValue, ExecutionPreparation
from studio.services.asset_generation import create_asset_generation_job, claim_next_text_asset_job, process_text_asset_job, request_asset_generation_cancel
from studio.services.execution_asset_auxiliaries import create_asset_field_child, ingest_asset_field_result, reconcile_asset_preparation_fields
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparation_fields import confirm_execution_field
from studio.tests import test_execution_profile_preparation as fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAssetAuxiliaryTests(TestCase):
    setUpTestData = fixtures.ExecutionProfilePreparationTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfilePreparationTests.input)
    bind = fixtures.ExecutionProfilePreparationTests.bind

    def setUp(self):
        fixtures.ExecutionProfilePreparationTests.setUp(self)
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))
        leaf = clone_execution_draft(source_id=self.leaf.pk, expected_revision=self.leaf.revision,
            expected_definition_sha256=self.leaf.contract.sha256, actor=self.actor)
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor,
            changes={"parameters": []})
        leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.root, _ = create_asset_generation_job(owner=self.actor, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video", field_values=[], name="Field-based video",
            client_idempotency_key="auxiliary-root", allow_external_calls=True, budget_limit=None, **fixtures.request_for(self))
        self.prep = self.root.execution_preparations.get()
        self.batch = self.prep.contract.resolved_inputs["field_requests"][0]

    def child(self, **changes):
        self.prep.refresh_from_db()
        return create_asset_field_child(**{"preparation_id": self.prep.pk, "expected_preparation_revision": self.prep.revision,
            "field_batch_key": self.batch["sha256"], "attempt_number": 1, "actor": self.actor, **changes})

    def advance(self):
        self.prep.refresh_from_db()
        # Protocol-only fixture; current resource/evidence/budget collection is
        # exercised without this substitute in test_execution_asset_admission.
        with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=isolated_observations):
            return advance_execution_preparation(preparation_id=self.prep.pk,
                expected_preparation_revision=self.prep.revision, actor=self.actor)

    def test_authorized_missing_batch_is_due_and_coordinator_waits_for_actual_human_choice(self):
        self.assertEqual(self.prep.status, "awaiting_input")
        self.assertIsNotNone(self.prep.next_check_at)
        self.assertEqual(self.advance().reason_code, "execution_auxiliary_pending")
        child = self.prep.auxiliary_asset_jobs.get()
        self.assertEqual(child.status, "queued")
        self.assertIsNone(child.submission_started_at)
        self.advance()
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)
        self.execute(child)
        self.assertEqual(self.advance().status, "awaiting_review")
        self.assertFalse(self.prep.field_selections.exists())
        self.root.refresh_from_db()
        self.assertEqual(self.root.status, "preparing")
        value = self.prep.field_values.get()
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)
        final = self.advance()
        self.assertEqual(final.status, "dispatched", final.reason)
        self.root.refresh_from_db()
        self.assertEqual(self.root.status, "queued")
        self.assertEqual(self.prep.field_selections.get().field_value_id, value.pk)
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)

    def test_coordinator_never_retries_failed_or_uncertain_auxiliary(self):
        self.advance()
        child = self.prep.auxiliary_asset_jobs.get()
        for state in ("failed", "uncertain"):
            AssetGenerationJob.objects.filter(pk=child.pk).update(status=state)
            blocked = self.advance()
            self.assertEqual(blocked.reason_code, "execution_auxiliary_requires_action")
            self.assertIsNone(blocked.next_check_at)
            self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)

    def test_explicit_uncertain_retry_retains_original_cost_input_and_generates_automatic_fields(self):
        from decimal import Decimal
        from studio.services.execution_auxiliary_actions import read_auxiliary_batches, retry_preparation_field_batch
        from studio.services.execution_asset_budget import asset_budget_totals_query
        self.advance()
        original = self.prep.auxiliary_asset_jobs.get()
        AssetGenerationJob.objects.filter(pk=original.pk).update(status="uncertain", error_code="uncertain_remote_response",
            estimated_cost=Decimal("0.01"), actual_cost=None, cost_currency="USD")
        self.advance()
        before = AssetGenerationJob.objects.filter(pk=original.pk).values().get()
        self.prep.refresh_from_db()
        self.assertTrue(read_auxiliary_batches(self.prep)[0]["can_retry"])
        args = dict(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            field_batch_key=self.batch["sha256"], attempt_number=2, actor=self.actor)
        for ack in (None, self.root.pk):
            with self.assertRaises(ValidationError):
                retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=ack)
        with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=isolated_observations):
            retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=original.pk)
        new = self.prep.auxiliary_asset_jobs.get(attempt_number=2)
        receipt = new.resolutions.get(outcome="uncertain_retry_authorized")
        self.assertEqual(receipt.evidence["previous_task_id"], str(original.pk))
        self.assertIsNone(receipt.evidence["previous_actual_cost"])
        self.assertEqual(receipt.decided_by, self.actor)
        self.assertEqual(new.execution_instance_version_id, original.execution_instance_version_id)
        self.assertEqual(new.field_batch_key, original.field_batch_key)
        self.assertEqual(AssetGenerationJob.objects.filter(pk=original.pk).values().get(), before)
        self.assertFalse(self.prep.field_values.exists())
        total = list(asset_budget_totals_query(self.root, self.prep, new.pk))
        self.assertEqual(next(row for row in total if row["cost_currency"] == "USD")["amount"], Decimal("0.01"))
        with self.assertRaises(ValidationError):
            retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=original.pk)
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 2)
        self.execute(new)
        self.assertEqual(self.advance().status, "awaiting_review")
        value = self.prep.field_values.get()
        self.assertEqual((value.origin_asset_job_id, value.origin_kind, value.status), (new.pk, "model", "pending"))
        self.assertEqual(AssetGenerationJob.objects.filter(pk=original.pk).values().get(), before)
        # Simulate a genuinely late response, never rewrite real old records.
        finished = AssetGenerationJob.objects.get(pk=new.pk)
        AssetGenerationJob.objects.filter(pk=original.pk).update(status="succeeded", result_text=finished.result_text,
            result_text_sha256=finished.result_text_sha256, result_text_size_bytes=finished.result_text_size_bytes)
        self.assertEqual(ingest_asset_field_result(job_id=original.pk, actor=self.actor), [])
        self.assertEqual(self.prep.field_values.count(), 1)

    def test_uncertain_retry_api_checks_exact_permission_state_revision_and_acknowledgement(self):
        import uuid
        from rest_framework.test import APIClient
        from django.contrib.auth import get_user_model
        self.advance()
        child = self.prep.auxiliary_asset_jobs.get()
        client = APIClient(); client.force_authenticate(self.actor)
        for state, claim in (("running", None), ("succeeded", None), ("uncertain", uuid.uuid4()), ("uncertain", None)):
            AssetGenerationJob.objects.filter(pk=child.pk).update(status=state, claim_token=claim)
            self.prep.refresh_from_db()
            payload = {"expected_preparation_revision": self.prep.revision, "field_batch_key": self.batch["sha256"],
                "attempt_number": 2, "acknowledged_uncertain_task_id": str(child.pk)}
            if state == "uncertain" and claim is None:
                payload["expected_preparation_revision"] += 1
            response = client.post(f"/api/v1/execution-preparations/{self.prep.pk}/retry-field-batch/", payload, format="json")
            self.assertIn(response.status_code, (400, 409), response.data)
            self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)
        client.force_authenticate(get_user_model().objects.create_user(username="other-uncertain"))
        response = client.post(f"/api/v1/execution-preparations/{self.prep.pk}/retry-field-batch/", payload, format="json")
        self.assertIn(response.status_code, (403, 404))

    def test_text_failure_keeps_safe_transport_cause_in_original_job(self):
        from studio.services.provider_registry import ProviderDriverError
        child, _ = self.child()
        freeze(child, self.actor)
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="queued")
        claimed = claim_next_text_asset_job("field-diagnostic")
        error = ProviderDriverError("PRIVATE URL TOKEN", code="provider_request_failed", submission_uncertain=True)
        error.metadata = {"transport_phase": "response_body", "exception_kind": "ReadTimeout", "http_status": 200}
        with patch("studio.services.asset_generation.driver_for") as peer:
            peer.return_value.invoke.side_effect = error
            finished = process_text_asset_job(claimed)
        self.assertEqual(finished.status, "uncertain")
        self.assertIsNone(finished.actual_cost)
        self.assertIn("response_body", finished.error_message)
        self.assertIn("ReadTimeout", finished.error_message)
        self.assertNotIn("PRIVATE", finished.error_message)

    def test_parent_advancement_preserves_unchanged_child_smoke_wait_revision(self):
        def missing_smoke(preparation, definition, request):
            return replace(isolated_observations(preparation, definition, request), smoke_facts=())

        def advance_parent(**kwargs):
            self.prep.refresh_from_db()
            # Only transport/resource observations are isolated; the real
            # missing-evidence gate, persisted revisions and coordinator run.
            with patch("studio.services.execution_advancement.collect_asset_execution_admission", side_effect=missing_smoke):
                return advance_execution_preparation(preparation_id=self.prep.pk,
                    expected_preparation_revision=self.prep.revision, actor=self.actor, **kwargs)

        advance_parent()
        child = self.prep.auxiliary_asset_jobs.get()
        blocked = child.execution_preparations.get()
        self.assertEqual(blocked.reason_code, "execution_smoke_missing")
        original = (blocked.revision, blocked.contract.evaluation_sha256, blocked.evaluated_at)
        for _ in range(3):
            advance_parent()
            current = child.execution_preparations.get()
            self.assertEqual((current.revision, current.contract.evaluation_sha256, current.evaluated_at), original)
            self.assertEqual(current.reason_code, "execution_smoke_missing")
            self.assertIsNone(current.request_frozen_at)
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)
        child.refresh_from_db()
        self.assertIsNone(child.submission_started_at)
        advance_parent(human_initiated=True)
        current = child.execution_preparations.get()
        self.assertGreater(current.revision, blocked.revision)
        self.assertEqual(current.reason_code, "execution_smoke_missing")

    def test_batch_reports_child_preparation_block_without_relabelling_task(self):
        from studio.services.execution_auxiliary_actions import read_auxiliary_batches
        child, _ = self.child()
        prep = child.execution_preparations.get()
        ExecutionPreparation.objects.filter(pk=prep.pk).update(status="blocked",
            reason_code="execution_smoke_missing", reason="辅助实例尚无有效烟测，请先验收该实例。")
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="preparing", error_message="")
        batch = read_auxiliary_batches(self.prep)[0]
        self.assertEqual(batch["status"], "preparing")
        self.assertIn("[execution_smoke_missing]", batch["reason"])
        self.assertIn("辅助实例尚无有效烟测", batch["reason"])
        self.assertFalse(batch["can_retry"])
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="failed", error_message="供应商请求失败")
        self.assertEqual(read_auxiliary_batches(self.prep)[0]["reason"], "供应商请求失败")
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="succeeded", error_message="")
        self.assertEqual(read_auxiliary_batches(self.prep)[0]["reason"], "")

    def execute(self, child, *, text="In soft daylight, a courier opens a blue parcel."):
        prep = child.execution_preparations.get()
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["field_request_sha256"], self.batch["sha256"])
        freeze(child, self.actor)  # isolated admission fixture, not live Smoke
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="queued")  # original dispatch still separately tested
        claimed = claim_next_text_asset_job("field-worker")
        self.assertEqual(claimed.pk, child.pk)
        payload = {"items": [{"request_id": self.batch["value"]["items"][0]["request_id"], "text": text}]}
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(payload), "request_id": "field-fixture-request",
            "usage": {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30}}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            finished = process_text_asset_job(claimed)
        self.assertEqual(finished.status, "succeeded", finished.error_message)
        peer.invoke.assert_called_once()
        transmitted = peer.invoke.call_args.args[2]
        self.assertNotIn("system_message_layout", transmitted)
        from studio.services.execution_preparations import read_frozen_execution_request
        from studio.task_center_details import inspect_input
        prep.refresh_from_db()
        _, _, saved = read_frozen_execution_request(prep)
        self.assertEqual(saved.value["system_message_layout"], preview.transport.value["system_message_layout"])
        self.assertEqual(inspect_input(prep)["system_message_trace"]["state"], "matched")
        self.assertEqual(json.loads(transmitted["messages"][1]["content"]), self.batch["value"])
        from studio.services.execution_resource_policy import read_resource_policy
        self.assertEqual(transmitted["max_tokens"], read_resource_policy().value["values"]["output_length"])
        return finished

    def test_child_creation_and_preview_are_idempotent_exact_and_do_not_dispatch(self):
        child, created = self.child()
        replay, created_again = self.child(expected_preparation_revision=1)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(child.pk, replay.pk)
        self.assertEqual((child.parent_preparation_id, child.job_type, child.media_type, child.operation),
            (self.prep.pk, "field", "text", "prompt.fields.generate"))
        self.assertEqual(child.status, "preparing")
        self.assertIsNone(child.estimated_cost)
        self.assertFalse(child.execution_preparations.get().field_values.exists())
        self.assertFalse(self.prep.field_values.exists())
        self.assertIsNone(claim_next_text_asset_job("not-dispatched"))
        prep = child.execution_preparations.get()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor).transport
        self.assertEqual(transport.value["field_request_sha256"], self.batch["sha256"])

    def test_success_is_saved_pending_once_with_real_origin_and_separate_cost(self):
        child = self.execute(self.child()[0])
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.actor)
        child_prep = child.execution_preparations.get()
        detail = client.get(f"/api/v1/task-center/preparation/{child_prep.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        receipt = detail.data["output"]["result"]["provider_response"]
        self.assertEqual(receipt["task_id"], str(child.pk))
        self.assertEqual(receipt["usage"], {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30})
        self.assertFalse(self.root.resolutions.exists())
        from studio.asset_generation_serializers import AssetGenerationJobSerializer
        self.assertIsNone(AssetGenerationJobSerializer(child).data["latest_resolution"])
        self.assertIsNone(child.actual_cost)
        rows = ingest_asset_field_result(job_id=child.pk, actor=self.actor)
        self.assertEqual(len(rows), 1)
        value = rows[0]
        self.assertEqual((value.origin_asset_job_id, value.status, value.origin_kind), (child.pk, "pending", "model"))
        self.assertIsNone(value.reviewed_at)
        self.assertIsNone(value.approval_kind)
        self.assertFalse(self.prep.field_selections.exists())
        self.prep.refresh_from_db()
        evaluation = evaluate_preparation_fields(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(evaluation.status, "awaiting_review")
        self.assertEqual(ingest_asset_field_result(job_id=child.pk, actor=self.actor)[0].pk, value.pk)
        self.assertEqual(self.prep.field_values.count(), 1)
        self.root.refresh_from_db()
        self.assertIsNone(self.root.actual_cost)
        self.assertIsNone(self.root.output_asset_id)

    def test_replay_after_confirmation_does_not_revoke_or_replace_choice(self):
        child = self.execute(self.child()[0])
        value = ingest_asset_field_result(job_id=child.pk, actor=self.actor)[0]
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)
        self.assertEqual(ingest_asset_field_result(job_id=child.pk, actor=self.actor)[0].status, "confirmed")
        self.assertEqual(self.prep.field_selections.get().field_value_id, value.pk)
        self.assertEqual(self.prep.field_values.count(), 1)

    def test_cancelled_parent_ignores_successful_late_field_result(self):
        child = self.execute(self.child()[0])
        request_asset_generation_cancel(self.root)
        self.assertEqual(ingest_asset_field_result(job_id=child.pk, actor=self.actor), [])
        child.refresh_from_db()
        self.prep.refresh_from_db()
        self.assertTrue(child.ignore_late_result)
        self.assertEqual((child.status, self.prep.status), ("succeeded", "cancelled"))
        self.assertFalse(self.prep.field_values.exists())

    def test_invalid_batch_failed_result_or_uncertain_retry_cannot_create_fields(self):
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        with self.assertRaises(ValidationError):
            self.child(field_batch_key="a" * 64)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)
        child, _ = self.child()
        for state in ("preparing", "failed", "uncertain"):
            AssetGenerationJob.objects.filter(pk=child.pk).update(status=state)
            with self.subTest(state=state), self.assertRaises(ValidationError):
                ingest_asset_field_result(job_id=child.pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            self.child(attempt_number=2)
        self.assertFalse(ExecutionFieldValue.objects.exists())
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="failed")
        retry, created = self.child(attempt_number=2)
        self.assertTrue(created)
        self.assertNotEqual(retry.pk, child.pk)
        self.assertEqual(retry.field_batch_key, child.field_batch_key)

    def test_unapproved_external_call_rolls_back(self):
        AssetGenerationJob.objects.filter(pk=self.root.pk).update(allow_external_calls=False)
        with self.assertRaises(ValidationError) as caught:
            self.child()
        self.assertEqual(caught.exception.code, "execution_external_authorization_required")
        self.assertFalse(self.prep.auxiliary_asset_jobs.exists())

    def test_result_digest_mismatch_never_partially_saves_fields(self):
        child = self.execute(self.child()[0])
        AssetGenerationJob.objects.filter(pk=child.pk).update(result_text_sha256="f" * 64)
        with self.assertRaises(ValidationError):
            ingest_asset_field_result(job_id=child.pk, actor=self.actor)
        self.assertFalse(self.prep.field_values.exists())

    def reconcile(self):
        self.prep.refresh_from_db()
        return reconcile_asset_preparation_fields(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)

    def test_reconcile_allocates_once_then_recovers_finished_result_without_calling_again(self):
        first = self.reconcile()
        self.assertEqual(first.reason_code, "execution_auxiliary_pending")
        child = self.prep.auxiliary_asset_jobs.get()
        second = self.reconcile()
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)
        self.execute(child)
        # Simulates a process restart before parent completion notification.
        result = self.reconcile()
        self.assertEqual(result.status, "awaiting_review")
        self.assertEqual(self.prep.field_values.get().origin_asset_job_id, child.pk)
        self.assertFalse(self.prep.field_selections.exists())
        self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)

    def test_reconcile_never_retries_failed_uncertain_or_cancelled_children(self):
        child, _ = self.child()
        for state in ("failed", "uncertain", "cancelled"):
            AssetGenerationJob.objects.filter(pk=child.pk).update(status=state)
            current = self.reconcile()
            self.assertEqual((current.status, current.reason_code), ("blocked", "execution_auxiliary_requires_action"))
            self.assertIsNone(current.next_check_at)
            self.assertEqual(self.prep.auxiliary_asset_jobs.count(), 1)

    def test_cancel_waits_for_claimed_child_then_reconciliation_closes_parent(self):
        child, _ = self.child()
        freeze(child, self.actor)
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="queued")
        claimed = claim_next_text_asset_job("cancel-field-worker")
        parent = request_asset_generation_cancel(self.root)
        self.assertEqual(parent.status, "cancel_requested")
        self.assertEqual(self.reconcile().status, "cancelled")
        with self.captureOnCommitCallbacks(execute=True):
            ended = process_text_asset_job(claimed)
        self.assertEqual(ended.status, "cancelled")
        self.prep.refresh_from_db()
        self.assertIsNotNone(self.prep.next_check_at)
        final = self.reconcile()
        self.root.refresh_from_db()
        self.assertEqual((self.root.status, final.status), ("cancelled", "cancelled"))
        self.assertIsNone(final.next_check_at)
        self.assertFalse(self.prep.field_values.exists())
