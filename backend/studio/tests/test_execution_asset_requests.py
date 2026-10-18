"""Original asset root reads a real frozen request; no provider submission.

Admission observations below are isolated fixtures, not live Smoke evidence.
The Comfy staging test uses a fake upload peer and actual local PNG Assets.
"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob, ModelExecutionInstanceVersion, WorkflowSmokeAttempt
from studio.services.asset_generation import (
    _asset_metadata, _snapshot, _verify_live_target, _prepared_comfy_asset_payload, create_asset_generation_job,
    request_asset_generation_cancel, claim_next_media_asset_job, claim_next_comfy_asset_job,
    resolve_uncertain_asset_generation_job,
    recover_stale_asset_generation_jobs,
)
from studio.services.execution_admission import ExecutionEvidenceIdentity, ExecutionSmokeFact
from studio.services.execution_finalization import ExecutionAdmissionFacts, finalize_execution
from studio.services.execution_instances import publish_execution_version
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_asset_jobs as fixtures
from studio.tests import test_execution_workflow_request_media as media_fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


def isolated_observations(preparation, definition, request):
    from studio.services.execution_capacity_signatures import describe_execution_capacity
    runtime = {"kind": "unknown", "host_id": None, "fingerprint": None}
    from studio.services.execution_request_storage import execution_request_max_bytes
    capacity = describe_execution_capacity(definition,
        json_contract(request.value["transport"], max_bytes=execution_request_max_bytes()), runtime=runtime)
    now = timezone.now()
    facts = (ExecutionSmokeFact(ExecutionEvidenceIdentity(str(preparation.execution_instance_version_id),
        definition.sha256, definition.value["target_fingerprint"], definition.value["configuration"]["operation"],
        definition.value["configuration"]["mode"], "instance_smoke"), "isolated-worker-reader-fixture", "succeeded", now,
        now + timedelta(hours=1)),)
    return ExecutionAdmissionFacts(facts, "unknown", [], json_contract({"schema_version": 1,
        "preparation_id": str(preparation.pk), "request_sha256": request.sha256,
        "resources": {"capacity_runtime": runtime}, "capacity": capacity.value,
        "budget": {"estimate": {"amount": "0.1", "currency": "USD", "basis": "isolated_accounting_fixture"}},
        "test_scope": "isolated admission observations; no actual Smoke or dispatch"}))


def freeze(job, actor):
    prep = job.execution_preparations.get()
    return finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
        actor=actor, collect_admission_facts=isolated_observations)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAssetRequestTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionAssetJobTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionAssetJobTests.write_contract)
    job_row = fixtures.ExecutionAssetJobTests.job_row
    resolve = fixtures.ExecutionAssetJobTests.resolve
    create = fixtures.ExecutionAssetJobTests.create

    def setUp(self):
        fixtures.ExecutionAssetJobTests.setUp(self)
        self.job, _ = self.create()

    def test_unprepared_root_cannot_supply_legacy_payload_or_call_a_provider(self):
        with self.assertRaisesMessage(ValidationError, "尚未完成"):
            _snapshot(self.job)
        with self.assertRaises(ValidationError):
            _snapshot(self.job_row("legacy-shape-rejected"))

    def test_worker_reads_one_frozen_preparation_and_never_reassembles_or_redraws(self):
        prep = freeze(self.job, self.actor)
        expected = deepcopy(prep.contract.prepared_request["transport"])
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport", side_effect=AssertionError("No recompile")), \
                patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No redraw")):
            first = _snapshot(self.job)
            second = _snapshot(AssetGenerationJob.objects.get(pk=self.job.pk))
        self.assertEqual(first.transport.value, expected)
        self.assertEqual(first.payload, expected["payload"])
        self.assertEqual(second.transport, first.transport)
        self.assertEqual(first.preparation.pk, prep.pk)
        self.assertEqual(first.runtime_budget, self.job.request_snapshot["runtime_budget"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")  # Reading cannot dispatch.

    def test_endpoint_drift_blocks_submission_and_reconciliation_without_rewriting_request(self):
        freeze(self.job, self.actor)
        request = _snapshot(self.job)
        self.assertEqual(_verify_live_target(self.job, request).pk, self.deployment.pk)
        config = deepcopy(self.provider.driver_config)
        config["poll_path"] = "/different/jobs/{request_id}"
        self.write_contract(self.provider, "driver_config", config)
        self.assertEqual(_snapshot(self.job).transport, request.transport)
        for submit in (True, False):
            with self.subTest(submit=submit), self.assertRaises(ValidationError):
                _verify_live_target(self.job, request, require_executable_route=submit)

    def test_inactive_instance_blocks_new_submission_not_original_peer_reconciliation(self):
        freeze(self.job, self.actor)
        request = _snapshot(self.job)
        self.instance.is_active = False
        self.instance.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            _verify_live_target(self.job, request)
        self.assertEqual(_verify_live_target(self.job, request, require_executable_route=False).pk, self.deployment.pk)

    def test_cancelling_unsubmitted_preparation_is_immediate_idempotent_and_unclaimable(self):
        preparation = self.job.execution_preparations.get()
        before = deepcopy(preparation.contract.resolved_inputs)
        result = request_asset_generation_cancel(self.job)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNotNone(result.finished_at)
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "cancelled")
        self.assertIsNone(preparation.next_check_at)
        self.assertIsNone(preparation.request_frozen_at)
        self.assertEqual(preparation.contract.resolved_inputs, before)
        self.assertEqual(request_asset_generation_cancel(result).finished_at, result.finished_at)
        self.assertIsNone(claim_next_media_asset_job("isolated-claim"))

    def test_cancel_does_not_modify_frozen_request_and_unfrozen_cancel_wait_is_not_a_main_job(self):
        preparation = freeze(self.job, self.actor)
        expected = deepcopy(preparation.contract.prepared_request)
        request_asset_generation_cancel(self.job)
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "cancelled")
        self.assertEqual(preparation.contract.prepared_request, expected)
        other, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="cancel-wait", **self.body)
        AssetGenerationJob.objects.filter(pk=other.pk).update(status="cancel_requested")
        self.assertIsNone(claim_next_media_asset_job("isolated-claim"))

    def test_uncertain_remote_state_cannot_be_rewritten_as_cancelled(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain")
        with self.assertRaisesMessage(ValidationError, "消歧"):
            request_asset_generation_cancel(self.job)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "uncertain")

    def test_generated_manifest_points_to_exact_preparation_and_request(self):
        preparation = freeze(self.job, self.actor)
        value = _asset_metadata(self.job, {"size_bytes": 123, "sha256": "a" * 64,
            "observed_media": {"duration_ms": 3000, "fps": 24, "frame_rate": "24/1"}})
        self.assertEqual(value["preparation_id"], str(preparation.pk))
        self.assertEqual(value["prepared_request_sha256"], preparation.request_sha256)
        self.assertEqual(value["execution_instance_version_id"], str(self.version.pk))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionAssetStagingTests(TestCase):
    _create_version = media_fixtures.ExecutionWorkflowMediaRequestTests._create_version

    def setUp(self):
        media_fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.job, _ = create_asset_generation_job(owner=self.user, client_idempotency_key="root-staging",
            execution_instance_version_id=version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="video", name="Prepared root video",
            parameters={key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            inputs={"references": {"items": [{"kind": "asset", "item_key": key, "asset_id": str(self.asset.pk), "purpose": None}
                for key in ("second", "first")]}}, manual_channels={"positive_prompt": "A parcel opens."},
            subject_facts=[], source_language="", target_language="", field_values=[])
        self.prep = freeze(self.job, self.user)
        # Isolated staging lease; original queue/admission tests cover claiming.
        self.job.status, self.job.claim_token = "queued", uuid.uuid4()
        self.job.save(update_fields=["status", "claim_token"])
        self.request = _snapshot(self.job)
        self.peer = RecordingUploadClient()

    def stage(self):
        with patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=self.peer):
            return _prepared_comfy_asset_payload(self.job, self.request, self.deployment, object(), self.request.payload)

    def test_waiting_smoke_does_not_deadlock_original_asset_polling(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(claim_token=None)
        smoke = WorkflowSmokeAttempt.objects.create(workflow_version=self.workflow, deployment=self.deployment,
            created_by=self.user, scope="resource", status="queued", graph_sha256="a" * 64,
            execution_contract_sha256="b" * 64, validation_evidence_sha256="c" * 64, target_sha256="d" * 64)
        self.assertIsNone(claim_next_comfy_asset_job("isolated-asset-worker"))
        token = str(self.prep.pk)
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="submitted", provider_request_id=token,
            submission_started_at=timezone.now())
        claimed = claim_next_comfy_asset_job("isolated-asset-worker")
        self.assertEqual(claimed.pk, self.job.pk)
        self.assertEqual(claimed.provider_request_id, token)
        self.assertEqual(claimed.status, "submitted")
        self.assertEqual(self.peer.uploads, [])
        smoke.refresh_from_db()
        self.assertEqual(smoke.status, "queued")
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(claim_token=None)
        WorkflowSmokeAttempt.objects.filter(pk=smoke.pk).update(status="running")
        self.assertIsNone(claim_next_comfy_asset_job("isolated-asset-worker"))

    def test_uncertain_resolution_uses_preparation_token_not_original_job_id(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain", claim_token=None)
        self.job.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "冻结的 submission token"):
            resolve_uncertain_asset_generation_job(self.job, user=self.user, outcome="confirmed_remote_failed",
                note="Isolated administrator confirmation of a failed remote request.", provider_request_id=str(self.job.pk),
                client_idempotency_key="bad-job-token")
        resolution, created = resolve_uncertain_asset_generation_job(self.job, user=self.user, outcome="confirmed_remote_failed",
            note="Isolated administrator confirmation of a failed remote request.", provider_request_id=str(self.prep.pk),
            client_idempotency_key="actual-preparation-token")
        self.assertTrue(created)
        self.job.refresh_from_db()
        self.assertEqual((self.job.status, self.job.provider_request_id), ("failed", str(self.prep.pk)))
        self.assertEqual(resolution.evidence["preparation_id"], str(self.prep.pk))
        self.assertEqual(resolution.evidence["prepared_request_sha256"], self.prep.request_sha256)

    def test_confirmed_not_submitted_records_real_zero_and_releases_reservation(self):
        from decimal import Decimal
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain", claim_token=None,
            estimated_cost=Decimal("0.3"), cost_currency="USD", submission_started_at=timezone.now())
        self.job.refresh_from_db()
        resolution, _ = resolve_uncertain_asset_generation_job(self.job, user=self.user, outcome="confirmed_not_submitted",
            note="Isolated administrator verified no remote submission occurred.", client_idempotency_key="not-submitted")
        self.job.refresh_from_db()
        self.assertEqual(self.job.actual_cost, Decimal("0"))
        self.assertEqual(resolution.evidence["actual_cost_source"], "operator_confirmed_not_submitted")

    def test_confirmed_success_resumes_original_comfy_request_without_async_contract(self):
        before = deepcopy(self.prep.contract.prepared_request)
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain", claim_token=None)
        self.job.refresh_from_db()
        resolution, created = resolve_uncertain_asset_generation_job(self.job, user=self.user,
            outcome="confirmed_remote_succeeded", note="隔离核验Comfy历史确认该冻结准备执行成功。",
            client_idempotency_key="comfy-confirmed-success")
        self.assertTrue(created)
        self.job.refresh_from_db()
        self.assertEqual((self.job.status, self.job.remote_status), ("running", "succeeded"))
        self.assertEqual(self.job.provider_request_id, str(self.prep.pk))
        self.assertNotEqual(self.job.provider_request_id, str(self.job.pk))
        self.assertIsNotNone(self.job.next_poll_at)
        self.assertEqual(resolution.evidence["preparation_id"], str(self.prep.pk))
        self.assertEqual(self.prep.contract.prepared_request, before)
        self.assertEqual(self.peer.uploads, [])

    def test_reference_video_queue_releases_stale_unsubmitted_claim_without_changing_operation(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(claim_token=None)
        claimed = claim_next_comfy_asset_job("reference-video-fixture")
        self.assertEqual(claimed.pk, self.job.pk)
        self.assertEqual(claimed.operation, "video.reference_to_video")
        before = deepcopy(self.prep.contract.prepared_request)
        stale = timezone.now() - timedelta(minutes=20)
        AssetGenerationJob.objects.filter(pk=claimed.pk).update(heartbeat_at=stale, claimed_at=stale)
        recovered = recover_stale_asset_generation_jobs(10, text=False, execution_kind="comfy_workflow",
            text_submission_grace_seconds=30)
        self.assertEqual(recovered, {"released": 1, "uncertain": 0})
        claimed.refresh_from_db()
        self.assertEqual((claimed.status, claimed.operation), ("queued", "video.reference_to_video"))
        self.assertIsNone(claimed.claim_token)
        self.assertEqual(self.prep.contract.prepared_request, before)

    def test_original_root_stages_exact_graph_and_persists_receipts_without_submitting(self):
        original = deepcopy(self.request.transport.value)
        staged = self.stage()
        self.assertEqual(staged["submission_token"], str(self.prep.pk))
        self.assertNotEqual(staged["submission_token"], str(self.job.pk))
        self.assertEqual(staged["workflow"]["cr_images_0"]["inputs"]["image"], self.peer.uploads[0])
        self.assertEqual(staged["workflow"]["cr_images_1"]["inputs"]["image"], self.peer.uploads[1])
        self.job.refresh_from_db()
        self.assertEqual(len(self.job.staged_inputs), 2)
        self.assertEqual(self.stage(), staged)
        self.assertEqual(len(self.peer.uploads), 2)
        self.assertEqual(self.request.transport.value, original)
        self.assertEqual(self.request.output_prefix, f"autoprompt/execution/{self.prep.pk}")
        self.assertEqual(self.request.output_kind, "video")

    def test_cancel_or_lease_change_prevents_upload_and_receipt_write(self):
        for changes in ({"status": "cancel_requested"}, {"claim_token": uuid.uuid4()}):
            AssetGenerationJob.objects.filter(pk=self.job.pk).update(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.stage()
            AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="queued", claim_token=self.job.claim_token)
        self.assertEqual(self.peer.uploads, [])
