"""Original queue and real PNG staging; isolated GPU peer, no live Smoke."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ModelExecutionInstanceVersion, WorkflowSmokeAttempt, AssetGenerationJob, ModelDeploymentCapabilitySnapshot
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_smoke_evidence import read_instance_smoke_facts
from studio.services.json_contracts import json_contract
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke, process_workflow_smoke, request_workflow_smoke_cancel
from studio.tests import test_execution_workflow_request_media as fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionComfySmokeTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version

    def setUp(self):
        fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])
        self.execution = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        publish_execution_version(version_id=self.execution.pk, expected_revision=self.execution.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        # Host/model evidence is tested separately; this test isolates actual
        # shared queue/admission/staging mechanics, not operational readiness.
        self.enterContext(patch("studio.services.execution_smoke_admission.read_execution_resource_state",
            return_value=json_contract({"schema_version": 1, "blockers": [], "test_scope": "isolated resource checks",
                "capacity_runtime": {"kind": "unknown", "host_id": None, "fingerprint": None}})))
        self.intent = {"inputs": {"references": {"items": [{"kind": "asset", "item_key": key,
            "asset_id": str(self.asset.pk), "purpose": None} for key in ("second", "first")]}},
            "parameters": {key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            "manual_channels": {"positive_prompt": "A parcel opens."}, "subject_facts": [], "source_language": "", "target_language": ""}
        self.attempt, self.prep, _ = create_instance_smoke(version_id=self.execution.pk,
            expected_definition_sha256=self.definition.sha256, fixture=self.intent, field_values=[],
            client_idempotency_key="comfy-combination", allow_external_calls=False, budget_limit=None, actor=self.user)
        ready = advance_execution_preparation(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.prep.refresh_from_db()
        self.peer, self.upload = Mock(), RecordingUploadClient()
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.driver_for", return_value=self.peer))
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.ExecutionWorkflowUploadClient", return_value=self.upload))

    def cycle(self):
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_workflow_smoke("comfy-instance")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.pk, self.attempt.pk)
        return process_workflow_smoke(claim)

    def test_evidence_http_reads_original_workflow_attempt_without_gpu_or_upload(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f"/api/v1/model-execution-instance-versions/{self.execution.pk}/evidence/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["workflow_version_id"], str(self.workflow.pk))
        self.assertEqual(response.data["summary"]["evidence_state"], "missing")
        row = response.data["results"][0]
        self.assertEqual((row["attempt_id"], row["attempt_kind"], row["preparation_id"]),
            (str(self.attempt.pk), "workflow_smoke", str(self.prep.pk)))
        self.assertEqual(row["request_sha256"], self.prep.request_sha256)
        self.peer.invoke.assert_not_called()
        self.assertEqual(self.upload.uploads, [])

    def submit(self):
        self.peer.invoke.return_value = {"request_id": str(self.prep.pk)}
        before = deepcopy(self.prep.contract.prepared_request)
        result = self.cycle()
        self.assertEqual(result.status, "running", result.error_code)
        self.assertEqual(len(self.upload.uploads), 2)
        payload = self.peer.invoke.call_args.args[2]
        self.assertEqual(payload["submission_token"], str(self.prep.pk))
        self.assertNotEqual(payload["submission_token"], str(self.attempt.submission_token))
        self.assertEqual(payload["workflow"]["cr_images_0"]["inputs"]["image"], self.upload.uploads[0])
        self.assertEqual(len(result.staged_inputs), 2)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.prepared_request, before)
        return result

    def test_dynamic_inputs_and_combination_result_do_not_mutate_base_workflow_or_make_asset_parent(self):
        before = (self.workflow.smoke_test_state, self.workflow.smoke_test_evidence_sha256)
        self.submit()
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": []}]
        with patch("studio.services.execution_workflow_smoke_worker.verify_comfy_primary_output",
                return_value=(None, {"kind": "video", "sha256": "a" * 64})) as verify:
            result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertEqual(verify.call_args.kwargs["expected_prefix"], f"autoprompt/execution/{self.prep.pk}")
        self.assertEqual(verify.call_args.kwargs["resolved_semantics"]["video.frames"], 124)
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition)[0].status, "succeeded")
        self.workflow.refresh_from_db()
        self.assertEqual((self.workflow.smoke_test_state, self.workflow.smoke_test_evidence_sha256), before)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertIsNone(result.actual_cost)

    def test_invalid_media_after_confirmed_completion_is_failed_not_uncertain(self):
        self.submit()
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": []}]
        result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("failed", "comfyui_primary_output_invalid"))
        self.assertIsNone(result.evidence_expires_at)

    def test_cancel_ack_waits_for_confirmation_and_never_reposts(self):
        result = request_workflow_smoke_cancel(self.submit(), actor=self.user)
        self.assertEqual(result.status, "cancel_requested")
        self.peer.invoke.return_value = {"status": "cancel_requested"}
        self.assertEqual(self.cycle().status, "cancel_requested")
        self.assertEqual(self.peer.invoke.call_args.args[1], "cancel")
        self.peer.invoke.return_value = {"status": "missing"}
        self.assertEqual(self.cycle().status, "cancelled")
        self.assertEqual(self.peer.invoke.call_args.args[1], "poll")
        self.assertEqual(len(self.upload.uploads), 2)

    def test_mismatched_receipt_is_uncertain_and_unclaimable(self):
        self.peer.invoke.return_value = {"request_id": str(uuid4())}
        result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("uncertain", "comfyui_submission_token_mismatch"))
        self.assertIsNone(claim_next_workflow_smoke("no-repost"))
        self.peer.invoke.assert_called_once()

    def test_inflight_poll_not_due_does_not_allow_another_queued_workflow(self):
        running = self.submit()
        # Separate original target row to exercise the global GPU mutex, not
        # just the per-target active uniqueness constraint.
        from studio.models import ModelDeployment
        another = ModelDeployment.objects.create(key="other-gpu", name="Other", profile_version=self.profile)
        WorkflowSmokeAttempt.objects.create(workflow_version=self.workflow, deployment=another, created_by=self.user,
            status="queued", graph_sha256=self.workflow.graph_sha256, execution_contract_sha256="a" * 64,
            validation_evidence_sha256="b" * 64, target_sha256="c" * 64)
        WorkflowSmokeAttempt.objects.filter(pk=running.pk).update(next_poll_at=timezone.now() + timedelta(hours=1))
        self.assertIsNone(claim_next_workflow_smoke("must-wait"))

    def test_authorization_rechecked_after_upload_before_submit(self):
        original = self.upload.upload_input_file
        def upload(*args, **kwargs):
            receipt = original(*args, **kwargs)
            if len(self.upload.uploads) == 2:
                type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
            return receipt
        self.upload.upload_input_file = upload
        result = self.cycle()
        self.assertEqual(result.status, "queued")
        self.assertIsNone(result.claim_token)
        self.assertIsNone(result.submission_started_at)
        self.assertEqual(len(result.staged_inputs), 2)
        self.peer.invoke.assert_not_called()
        self.prep.refresh_from_db()
        self.assertTrue(self.prep.request_sha256)
        self.assertIn("execution_owner_unavailable", str(self.prep.contract.evaluation))

    def test_recovered_submitting_never_uploads_or_submits_again(self):
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(status="submitting", submission_started_at=timezone.now())
        result = self.cycle()
        self.assertEqual((result.status, result.error_code), ("uncertain", "submission_uncertain"))
        self.assertEqual(self.upload.uploads, [])
        self.peer.invoke.assert_not_called()

    def test_operator_resolution_uses_frozen_preparation_token_not_attempt_token(self):
        from django.core.exceptions import ValidationError
        from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(status="uncertain", submission_started_at=timezone.now())
        self.attempt.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "submission token"):
            resolve_workflow_smoke_attempt(self.attempt, outcome="confirmed_remote_failed", note="Isolated failed remote verification",
                provider_request_id=str(self.attempt.submission_token), user=self.user)
        result, created = resolve_workflow_smoke_attempt(self.attempt, outcome="confirmed_remote_failed", note="Isolated failed remote verification",
            provider_request_id=str(self.prep.pk), user=self.user)
        self.assertTrue(created)
        self.assertEqual(result.evidence["preparation_id"], str(self.prep.pk))
        self.assertEqual(result.evidence["prepared_request_sha256"], self.prep.request_sha256)
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())

    def test_confirmed_no_submission_releases_held_cost_without_inventing_pass(self):
        from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(status="uncertain", submission_started_at=timezone.now(),
            estimated_cost="0.25", cost_currency="USD")
        self.attempt.refresh_from_db()
        result, created = resolve_workflow_smoke_attempt(self.attempt, outcome="confirmed_not_submitted", note="Isolated operator confirmed no remote request",
            provider_request_id="", user=self.user)
        self.assertTrue(created)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.actual_cost, 0)
        self.assertEqual(self.attempt.status, "uncertain")
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())
