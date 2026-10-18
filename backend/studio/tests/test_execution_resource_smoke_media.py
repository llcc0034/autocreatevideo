"""Real resource preparation and original media workers with isolated peers.

Resource admission, preparation advancement and original worker submission
checks are real. Only external peers and media probes are isolated; results
are development evidence, not operational acceptance.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionCapacityObservation, ExecutionPreparation, WorkflowSmokeAttempt, ModelDeploymentSmokeAttempt
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_instances import ExecutionResourceSelection
from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
from studio.services.execution_smoke import create_resource_smoke
from studio.services.execution_smoke_evidence import read_prepared_smoke_fact
from studio.services.deployment_smoke_queue import process_deployment_smoke
from studio.services.workflow_smoke_queue import process_workflow_smoke
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke
from studio.tests import test_execution_resource_selection as fixtures
from studio.tests import test_execution_definitions as async_fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


def dispatch_resource_worker_fixture(prep, attempt, actor):
    ready = advance_execution_preparation(preparation_id=prep.pk,
        expected_preparation_revision=prep.revision, actor=actor)
    if ready.status != "dispatched":
        raise AssertionError(ready.reason)
    attempt.refresh_from_db()
    return ready


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ResourceAsyncWorkerTests(TestCase):
    setUpTestData = async_fixtures.ExecutionDefinitionTests.__dict__["setUpTestData"]
    write_contract = staticmethod(async_fixtures.ExecutionDefinitionTests.write_contract)

    def setUp(self):
        # Unlike definition-only fixtures, actual admission must read the
        # isolated keyring. Block network I/O, not the credential validator.
        for target in ("studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No real external execution")))
        from studio.tests.secret_fixtures import enable_test_keyring, create_test_secret
        enable_test_keyring(self)
        self.deployment.credential = create_test_secret(key="resource-async-credential", actor=self.actor)
        self.deployment.save(update_fields=("credential", "updated_at"))
        # Real admission needs the declared system currency threshold. Keep
        # missing-policy confirmation intact and configure only this test DB.
        policy = deepcopy(read_resource_policy().value["values"])
        policy["budget_by_currency"] = {"USD": "1.00"}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: policy},
            created_by=self.actor, change_summary="Isolated async resource worker policy")
        publish_config_revision(revision, published_by=self.actor)
        self.version.refresh_from_db()
        selection = ExecutionResourceSelection.from_version(self.version)
        self.definition = build_resource_smoke_definition(selection)
        fixture = {"inputs": {}, "parameters": {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3},
            "manual_channels": {"positive_prompt": "A red cube turns."}, "subject_facts": [], "source_language": "", "target_language": ""}
        self.attempt, prep, _ = create_resource_smoke(selection=selection, expected_definition_sha256=self.definition.sha256,
            fixture=fixture, field_values=[], client_idempotency_key="resource-async-worker",
            allow_external_calls=True, budget_limit=None, actor=self.actor)
        self.prep = dispatch_resource_worker_fixture(prep, self.attempt, self.actor)
        self.peer = Mock()
        self.enterContext(patch("studio.services.execution_smoke_worker.driver_for", return_value=self.peer))

    def cycle(self):
        from studio.services.deployment_smoke_queue import claim_next_deployment_smoke
        ModelDeploymentSmokeAttempt.objects.filter(pk=self.attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_deployment_smoke("isolated-resource-async-worker")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.pk, self.attempt.pk)
        return process_deployment_smoke(claim)

    def submit(self):
        self.peer.invoke.return_value = {"request_id": "isolated-resource-remote", "status": "queued"}
        done = self.cycle()
        self.assertEqual(done.status, "submitted", done.error_code)
        self.assertEqual(self.peer.invoke.call_args.args[2]["idempotency_key"], str(self.prep.pk))
        return done

    def test_resource_async_poll_collect_preserves_scope_and_exact_settlement(self):
        self.submit()
        self.peer.invoke.side_effect = [{"terminal_success": True, "status": "succeeded"},
            {"artifacts": [{"kind": "video", "url": "https://cdn.example.com/fixture.mp4"}], "actual_cost": "0.08", "currency": "USD"}]
        with patch("studio.services.remote_artifacts.verify_remote_artifacts_for_smoke",
                return_value=[{"kind": "video", "sha256": "a" * 64}]) as verify:
            done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        self.assertEqual(verify.call_args.kwargs["expected_kind"], "video")
        self.assertEqual((done.actual_cost, done.cost_currency), (Decimal("0.08"), "USD"))
        self.assertEqual(read_prepared_smoke_fact(done).identity.scope, "resource_smoke")
        self.assertFalse(ExecutionCapacityObservation.objects.exists())
        from studio.services.execution_deployment_resource_evidence import current_resource_smoke
        from studio.services.runtime_platform import _candidate_state
        self.assertEqual(current_resource_smoke(self.deployment, done.operation).pk, done.pk)
        # Recommendation verifies a real credential binding; credential access
        # is isolated here just like the external Provider transport above.
        with patch("studio.services.runtime_platform.resolve_credential", return_value="isolated-test-credential"):
            state, reason = _candidate_state(snapshot=None, deployment=self.deployment, workflow=None,
                availability=[], max_vram_bytes=0)
        self.assertEqual((state, reason), ("compatible", "remote_provider_compatible"))
        self.write_contract(self.provider, "driver_config", {**self.provider.driver_config, "base_url": "https://api.example.com/changed"})
        self.deployment.refresh_from_db()
        with patch("studio.services.runtime_platform.resolve_credential", return_value="isolated-test-credential"):
            state, reason = _candidate_state(snapshot=None, deployment=self.deployment, workflow=None,
                availability=[], max_vram_bytes=0)
        self.assertEqual((state, reason), ("unavailable", "resource_smoke_evidence_invalid"))

    def test_changed_async_target_does_not_poll_old_request_at_new_endpoint(self):
        self.submit()
        self.write_contract(self.provider, "driver_config", {**self.provider.driver_config, "base_url": "https://api.example.com/changed"})
        self.peer.invoke.reset_mock()
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("uncertain", "smoke_transport_or_result_invalid"))
        self.peer.invoke.assert_not_called()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ResourceComfyWorkerTests(TestCase):
    _create_version = fixtures.WorkflowResourceDefinitionTests._create_version
    workflow_fixtures = fixtures.WorkflowResourceDefinitionTests.workflow_fixtures
    resource_fixture = fixtures.WorkflowResourceDefinitionTests.resource_fixture
    validate_resource_workflow = fixtures.WorkflowResourceDefinitionTests.validate_resource_workflow

    def setUp(self):
        fixtures.WorkflowResourceDefinitionTests.setUp(self)
        from studio.tests.execution_runtime_fixtures import record_workflow_runtime
        self.host_snapshot = record_workflow_runtime(self.deployment)
        self.workflow = self.validate_resource_workflow()
        selection = ExecutionResourceSelection(self.deployment, "video.reference_to_video", "Ref2VA", "manual", None, self.workflow)
        self.definition = build_resource_smoke_definition(selection)
        self.attempt, prep, _ = create_resource_smoke(selection=selection, expected_definition_sha256=self.definition.sha256,
            fixture=self.resource_fixture(self.definition), field_values=[], client_idempotency_key="resource-media-worker",
            allow_external_calls=False, budget_limit=None, actor=self.user)
        self.prep = dispatch_resource_worker_fixture(prep, self.attempt, self.user)
        self.peer, self.upload = Mock(), RecordingUploadClient()
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.driver_for", return_value=self.peer))
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.ExecutionWorkflowUploadClient", return_value=self.upload))

    def cycle(self):
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_workflow_smoke("isolated-resource-media-worker")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.pk, self.attempt.pk)
        return process_workflow_smoke(claim)

    def submit(self):
        self.peer.invoke.return_value = {"request_id": str(self.prep.pk)}
        done = self.cycle()
        self.assertEqual(done.status, "running", done.error_code)
        self.assertEqual(len(self.upload.uploads), 2)
        self.assertEqual(len(done.staged_inputs), 2)
        return done

    def test_media_verifier_envelope_reaches_resource_evidence_writer(self):
        # Do not stub the shared verifier with a hand-built evidence envelope:
        # doing so used to conceal missing kind/node fields on real media.
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from studio.services.execution_preparations import read_frozen_execution_request
        self.submit()
        self.prep.refresh_from_db()
        _, _, transport = read_frozen_execution_request(self.prep)
        primary = transport.value["payload"]["compiled_workflow"]["output_contract"]["primary"]
        with TemporaryDirectory() as directory, self.settings(COMFYUI_OUTPUT_DIR=directory):
            folder = Path(directory) / "autoprompt" / "execution"
            folder.mkdir(parents=True)
            source = folder / f"{self.prep.pk}_00001_.mp4"
            source.write_bytes(b"isolated-media-probe-fixture")
            self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": [{
                "node_id": primary["node_id"], "kind": "images", "filename": source.name,
                "subfolder": "autoprompt\\execution", "type": "output"}]}]
            with patch("studio.services.execution_workflow_outputs.verified_local_media_facts", return_value={
                    "mime_type": "video/mp4", "size_bytes": source.stat().st_size, "sha256": "a" * 64,
                    "observed_media": {"width": 320, "height": 512, "frames": 124, "fps": 24, "has_audio": True}}):
                done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        facts = done.result["evidence"]["artifact_facts"][0]
        self.assertEqual((facts["kind"], facts["node_id"]), ("video", str(primary["node_id"])))
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "passed")
        self.assertEqual(read_prepared_smoke_fact(done).status, "succeeded")

    def test_resource_uses_same_dynamic_media_order_probe_and_scope_without_instance_capacity(self):
        frozen = deepcopy(self.prep.contract.prepared_request)
        self.submit()
        submitted = self.peer.invoke.call_args.args[2]
        self.assertEqual(submitted["submission_token"], str(self.prep.pk))
        for index in range(2):
            self.assertEqual(submitted["workflow"][f"cr_images_{index}"]["inputs"]["image"], self.upload.uploads[index])
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": []}]
        with patch("studio.services.execution_workflow_smoke_worker.verify_comfy_primary_output",
                return_value=(None, {"kind": "video", "sha256": "a" * 64})) as verify:
            done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        self.assertEqual(verify.call_args.kwargs["expected_prefix"], f"autoprompt/execution/{self.prep.pk}")
        self.assertEqual(verify.call_args.kwargs["resolved_semantics"]["video.frames"], 124)
        self.assertIsNone(read_prepared_smoke_fact(done).identity.version_id)
        self.assertEqual(done.result["scope"], "resource_smoke")
        self.assertFalse(ExecutionCapacityObservation.objects.exists())
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.prepared_request, frozen)
        # Base acceptance references this original result, never instance capacity.
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "passed")
        from studio.services.workflow_versions import inspect_workflow_acceptance, verify_published_workflow_capability
        evidence = self.workflow.smoke_test_evidence
        self.assertEqual(evidence["scope"], "resource_smoke")
        self.assertEqual(evidence["result_sha256"], done.result_sha256)
        self.assertEqual(evidence["request_sha256"], self.prep.request_sha256)
        self.assertEqual(evidence["prepared_target_sha256"], done.target_sha256)
        self.assertNotEqual(evidence["prepared_target_sha256"], evidence["execution_target_sha256"])
        self.assertNotIn("artifact_facts", evidence)
        self.assertTrue(inspect_workflow_acceptance(self.workflow)["smoke"]["fresh"])
        from rest_framework.test import APIClient
        from studio.models import PlatformLifecycleAuditEvent
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(f"/api/v1/workflow-template-versions/{self.workflow.pk}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.workflow.refresh_from_db()
        published = self.workflow
        audit = PlatformLifecycleAuditEvent.objects.get(
            object_type="workflow_template_version", object_id=published.pk, action="publish",
        )
        self.assertEqual(audit.before_summary["status"], "validated")
        self.assertEqual(audit.after_summary["status"], "published")
        self.assertEqual(audit.before_summary["validated_deployment_id"], str(self.deployment.pk))
        self.assertEqual(audit.after_summary["validated_deployment_id"], str(self.deployment.pk))
        # This shared fixture intentionally starts with a disabled template.
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            verify_published_workflow_capability(published, self.deployment, operation="video.reference_to_video")
        published.template.is_active = True
        published.template.save(update_fields=["is_active"])
        proof, target_sha256 = verify_published_workflow_capability(published, self.deployment, operation="video.reference_to_video")
        self.assertEqual(proof.pk, done.pk)
        self.assertEqual(proof.status, "succeeded")
        self.assertEqual(target_sha256, evidence["execution_target_sha256"])
        # Expiry is computed from the original result, without rewriting it.
        with patch("studio.services.workflow_versions.timezone.now", return_value=done.evidence_expires_at):
            state = inspect_workflow_acceptance(published)["smoke"]
            self.assertEqual((state["fresh"], state["state"], state["reason_code"]),
                (False, "expired", "workflow_smoke_expired"))
            from django.core.exceptions import ValidationError
            with self.assertRaises(ValidationError):
                verify_published_workflow_capability(published, self.deployment, operation="video.reference_to_video")
        done.refresh_from_db()
        self.assertEqual(done.status, "succeeded")
        # Even a rehashed UI projection cannot replace the root outcome.
        published.smoke_test_evidence = {**evidence, "result_sha256": "0" * 64}
        from studio.services.workflow_versions import _evidence_digest
        published.smoke_test_evidence_sha256 = _evidence_digest(published.smoke_test_evidence)
        state = inspect_workflow_acceptance(published)["smoke"]
        self.assertEqual(state["reason_code"], "workflow_smoke_evidence_invalid")
        self.assertFalse(state["fresh"])

    def test_resource_failure_projects_original_root_and_never_publishes(self):
        self.submit()
        self.peer.invoke.return_value = {"status": "failed"}
        done = self.cycle()
        self.assertEqual(done.status, "failed")
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "failed")
        self.assertEqual(self.workflow.smoke_test_evidence["smoke_attempt_id"], str(done.pk))
        self.assertEqual(self.workflow.smoke_test_evidence["result_sha256"], done.result_sha256)
        from studio.services.workflow_versions import inspect_workflow_acceptance, publish_workflow_version
        from django.core.exceptions import ValidationError
        self.assertFalse(inspect_workflow_acceptance(self.workflow)["smoke"]["fresh"])
        with self.assertRaises(ValidationError):
            publish_workflow_version(self.workflow, published_by=self.user)

    def test_definite_submit_rejection_is_failed_but_not_a_remote_success(self):
        from studio.services.provider_registry import ProviderDriverError
        self.peer.invoke.side_effect = ProviderDriverError("isolated rejected request", code="provider_request_rejected", submission_uncertain=False)
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("failed", "provider_request_rejected"))
        self.assertIsNone(done.submitted_at)
        self.assertIsNone(done.evidence_expires_at)
        self.peer.invoke.assert_called_once()
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "failed")

    def test_resource_release_failure_before_submit_is_failed_without_code_whitelist(self):
        from studio.services.provider_registry import ProviderDriverError
        self.peer.invoke.side_effect = ProviderDriverError("isolated resource release rejected", code="comfyui_resource_release_unconfirmed",
            submission_uncertain=False)
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("failed", "comfyui_resource_release_unconfirmed"))
        self.assertIsNone(done.submitted_at)
        self.assertIsNone(done.evidence_expires_at)

    def test_submit_timeout_is_uncertain_even_when_no_remote_id_was_received(self):
        from studio.services.provider_registry import ProviderDriverError
        self.peer.invoke.side_effect = ProviderDriverError("isolated submit timeout", code="comfyui_operation_failed",
            submission_uncertain=True)
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("uncertain", "comfyui_operation_failed"))
        self.assertIsNone(done.submitted_at)
        self.assertIsNone(done.evidence_expires_at)
        self.assertIsNone(claim_next_workflow_smoke("never-resubmit-uncertain"))

    def test_remote_failure_preserves_safe_node_diagnostic_from_frozen_graph(self):
        self.submit()
        compiled = self.prep.contract.prepared_request["transport"]["payload"]["compiled_workflow"]
        node_id = next(iter(compiled["graph"]))
        self.peer.invoke.return_value = {"status": "failed", "failure": {"schema_version": 1, "kind": "execution_error",
            "node_id": node_id, "node_type": "UntrustedRemoteNode", "exception_type": "ValueError",
            "exception_message": "isolated invalid input token=secret-not-for-output E:\\private\\data.bin"}}
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("failed", "smoke_remote_terminated"))
        self.assertIn(compiled["graph"][node_id]["class_type"], done.error_message)
        self.assertIn("isolated invalid input", done.error_message)
        self.assertNotIn("secret-not-for-output", done.error_message)
        self.assertNotIn("E:\\private", done.error_message)
        self.assertNotIn("UntrustedRemoteNode", done.error_message)
        self.assertIsNone(done.evidence_expires_at)

    def test_poll_rejection_does_not_prove_remote_failure_and_never_resubmits(self):
        from studio.services.provider_registry import ProviderDriverError
        self.submit()
        self.peer.invoke.side_effect = ProviderDriverError("isolated poll 404", code="provider_request_rejected", submission_uncertain=False)
        done = self.cycle()
        self.assertEqual(done.status, "uncertain")
        self.assertEqual(self.peer.invoke.call_args.args[1], "poll")
        self.assertIsNone(done.evidence_expires_at)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "failed")
        self.assertIsNone(claim_next_workflow_smoke("no-resubmit-after-poll-error"))

    def test_changed_target_cannot_receive_old_remote_poll_or_repeated_submission(self):
        self.submit()
        type(self.workflow).objects.filter(pk=self.workflow.pk).update(validation_evidence_sha256="0" * 64)
        self.peer.invoke.reset_mock()
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("uncertain", "smoke_transport_or_result_invalid"))
        self.peer.invoke.assert_not_called()
        self.assertEqual(len(self.upload.uploads), 2)

        # The original root still records its uncertainty but cannot overwrite
        # the newer validation generation's current display.
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "not_run")
        self.assertEqual(self.workflow.smoke_test_evidence, {})

    def test_pre_submission_validation_change_fails_without_upload_or_overwriting_new_validation(self):
        type(self.workflow).objects.filter(pk=self.workflow.pk).update(validation_evidence_sha256="0" * 64,
            smoke_test_state="not_run", smoke_test_evidence={}, smoke_test_evidence_sha256="")
        done = self.cycle()
        self.assertEqual((done.status, done.error_code), ("failed", "smoke_transport_or_result_invalid"))
        self.peer.invoke.assert_not_called()
        self.assertEqual(self.upload.uploads, [])
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "not_run")
        self.assertEqual(self.workflow.smoke_test_evidence, {})

    def test_late_success_records_original_result_without_overwriting_new_validation(self):
        self.submit()
        def completed_after_revalidate(*args):
            if args[1] == "poll":
                return {"status": "succeeded"}
            type(self.workflow).objects.filter(pk=self.workflow.pk).update(validation_evidence_sha256="0" * 64,
                smoke_test_state="not_run", smoke_test_evidence={}, smoke_test_evidence_sha256="")
            return {"outputs": []}
        self.peer.invoke.side_effect = completed_after_revalidate
        with patch("studio.services.execution_workflow_smoke_worker.verify_comfy_primary_output",
                return_value=(None, {"kind": "video", "sha256": "a" * 64})):
            done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        self.assertEqual(read_prepared_smoke_fact(done).status, "succeeded")
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "not_run")
        self.assertEqual(self.workflow.smoke_test_evidence, {})

    def test_uncertain_resource_http_resolution_preserves_original_fact_and_requires_explicit_new_intent(self):
        from rest_framework.test import APIClient
        from django.core.exceptions import ValidationError
        from studio.services.provider_registry import ProviderDriverError
        self.submit()
        self.peer.invoke.side_effect = ProviderDriverError("isolated poll lost", code="comfyui_operation_failed")
        done = self.cycle()
        self.assertEqual(done.status, "uncertain")
        client = APIClient()
        client.force_authenticate(self.user)
        endpoint = f"/api/v1/workflow-template-versions/{self.workflow.pk}/smoke-test/"
        body = {"selection": {"operation": "video.reference_to_video", "mode": "Ref2VA", "prompt_mode": "manual", "prompt_profile_version_id": None},
            "expected_definition_sha256": self.definition.sha256, "fixture": self.resource_fixture(self.definition),
            "field_values": [], "allow_external_calls": False}
        preparations = ExecutionPreparation.objects.count()
        for legacy_budget in (None, "1.00"):
            with self.subTest(legacy_budget=legacy_budget):
                rejected = client.post(endpoint, {**body, "budget_limit": legacy_budget},
                    format="json", HTTP_IDEMPOTENCY_KEY="retired-budget")
                self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(ExecutionPreparation.objects.count(), preparations)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 1)
        blocked = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="after-resolution")
        self.assertEqual(blocked.status_code, 409, blocked.data)
        url = f"/api/v1/workflow-smoke-attempts/{done.pk}/resolve-uncertain/"
        payload = {"outcome": "confirmed_remote_failed", "provider_request_id": str(self.prep.pk), "note": "已核对受控对端的原任务失败终态。"}
        wrong = client.post(url, {**payload, "provider_request_id": str(done.submission_token)}, format="json")
        self.assertEqual(wrong.status_code, 400, wrong.data)
        resolution = client.post(url, payload, format="json")
        self.assertEqual(resolution.status_code, 201, resolution.data)
        self.assertNotIn("provider_request_id", resolution.data)
        repeated = client.post(url, payload, format="json")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["id"], resolution.data["id"])
        conflict = client.post(url, {**payload, "outcome": "confirmed_remote_succeeded"}, format="json")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        published = client.post(f"/api/v1/workflow-template-versions/{self.workflow.pk}/publish/", {}, format="json")
        self.assertEqual(published.status_code, 400, published.data)
        saved = done.resolutions.get()
        saved.note = "must not replace verification"
        with self.assertRaises(ValidationError):
            saved.save(update_fields=["note", "updated_at"])
        with self.assertRaises(ValidationError):
            done.resolutions.update(note="must not replace verification")
        created = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="after-resolution")
        self.assertEqual(created.status_code, 202, created.data)
        self.assertNotEqual(str(created.data["attempt_id"]), str(done.pk))
        done.refresh_from_db()
        self.assertEqual(done.status, "uncertain")
        self.assertIsNone(done.evidence_expires_at)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 2)

    def test_resource_comfy_cancel_uses_original_preparation_and_uncertainty_checks_actual_submission_token(self):
        from django.core.exceptions import ValidationError
        from studio.services.workflow_smoke_queue import request_workflow_smoke_cancel
        from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
        self.submit()
        cancelled = request_workflow_smoke_cancel(self.attempt, actor=self.user)
        self.assertEqual(cancelled.status, "cancel_requested")
        self.assertEqual(cancelled.provider_request_id, str(self.prep.pk))
        WorkflowSmokeAttempt.objects.filter(pk=self.attempt.pk).update(status="uncertain")
        self.attempt.refresh_from_db()
        args = {"outcome": "confirmed_remote_failed", "note": "已核对受控远端任务为失败终态。", "user": self.user}
        with self.assertRaises(ValidationError):
            resolve_workflow_smoke_attempt(self.attempt, **args, provider_request_id=str(self.attempt.submission_token))
        resolution, created = resolve_workflow_smoke_attempt(self.attempt, **args, provider_request_id=str(self.prep.pk))
        self.assertTrue(created)
        self.assertIsNone(resolution.evidence["execution_instance_version_id"])
        self.assertEqual(resolution.evidence["scope"], "resource")
        self.assertEqual(resolution.evidence["preparation_id"], str(self.prep.pk))
        self.assertEqual(resolution.evidence["prepared_request_sha256"], self.prep.request_sha256)
        repeated, created = resolve_workflow_smoke_attempt(self.attempt, **args, provider_request_id=str(self.prep.pk))
        self.assertFalse(created)
        self.assertEqual(repeated.pk, resolution.pk)
        self.attempt.refresh_from_db(); self.workflow.refresh_from_db()
        self.assertEqual(self.attempt.status, "uncertain")
        self.assertIsNone(self.attempt.evidence_expires_at)
        self.assertEqual(self.workflow.smoke_test_state, "not_run")
        self.peer.invoke.assert_called_once()
