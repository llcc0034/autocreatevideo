"""Original business worker with real preparation/PNG uploads, isolated peer.

Smoke admission uses an explicit boundary fixture, not live GPU acceptance.
Business collection tests encode a real local sample and use the real probe;
that file is a test signal, not model-generated footage.
"""
from datetime import timedelta
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob
from studio.services.asset_generation import create_asset_generation_job, claim_next_comfy_asset_job, process_comfy_asset_job, request_asset_generation_cancel, recover_stale_asset_generation_jobs, reconcile_uncertain_comfy_asset_jobs
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_smoke_comfy as fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionComfyAssetWorkerTests(TestCase):
    _create_version = fixtures.ExecutionComfySmokeTests._create_version
    cycle = fixtures.ExecutionComfySmokeTests.cycle
    submit = fixtures.ExecutionComfySmokeTests.submit

    def setUp(self):
        fixtures.ExecutionComfySmokeTests.setUp(self)
        self.submit()
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": []}]
        with patch("studio.services.execution_workflow_smoke_worker.verify_comfy_primary_output",
                return_value=(None, {"kind": "video", "sha256": "a" * 64})):
            self.assertEqual(self.cycle().status, "succeeded")
        self.enterContext(patch("studio.services.execution_asset_admission.read_execution_resource_state",
            return_value=json_contract({"schema_version": 1, "blockers": [], "test_scope": "isolated resource checks",
                "capacity_runtime": {"kind": "unknown", "host_id": None, "fingerprint": None}})))
        self.job, _ = create_asset_generation_job(owner=self.user, execution_instance_version_id=self.execution.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video", name="隔离视频任务",
            **self.intent, field_values=[], client_idempotency_key="comfy-asset-original-worker")
        self.business_prep = self.job.execution_preparations.get()
        ready = advance_execution_preparation(preparation_id=self.business_prep.pk,
            expected_preparation_revision=self.business_prep.revision, actor=self.user)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.business_peer, self.business_upload = Mock(), RecordingUploadClient()
        self.enterContext(patch("studio.services.asset_generation.driver_for", return_value=self.business_peer))
        self.enterContext(patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=self.business_upload))
        self.business_peer.invoke.return_value = {"request_id": str(self.business_prep.pk)}
        submitted = self.business_cycle()
        self.assertEqual(submitted.status, "submitted", submitted.error_message)
        self.assertEqual(len(self.business_upload.uploads), 2)
        self.assertIsNone(submitted.actual_cost)
        self.assertIsNotNone(submitted.estimated_cost)

    def business_cycle(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_comfy_asset_job("isolated-business-worker")
        self.assertIsNotNone(claim)
        return process_comfy_asset_job(claim)

    def test_bad_primary_output_does_not_turn_estimate_into_a_bill(self):
        self.business_peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(self.business_cycle().status, "running")
        self.business_peer.invoke.return_value = {"outputs": []}
        result = self.business_cycle()
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertIsNone(result.actual_cost)
        self.assertIsNotNone(result.estimated_cost)
        self.assertIsNone(result.output_asset_id)

    def local_video_receipt(self, *, wrong_width=False):
        self.business_prep.refresh_from_db()
        compiled = self.business_prep.contract.prepared_request["transport"]["payload"]["compiled_workflow"]
        semantics = compiled["resolved_semantics"]
        namespace = PurePosixPath(semantics["output.prefix"])
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(self.settings(COMFYUI_OUTPUT_DIR=temporary.name))
        root = Path(temporary.name).resolve()
        output = root / Path(*namespace.parent.parts) / f"{namespace.name}_00001.mp4"
        self.assertTrue(output.resolve().is_relative_to(root))
        output.parent.mkdir(parents=True)
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg, "The repository FFmpeg toolchain is required for media regression")
        width = semantics["image.width"] + (16 if wrong_width else 0)
        size = f"{width}x{semantics['image.height']}"
        encoded = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            f"color=c=blue:s={size}:r={semantics['video.fps']}", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-frames:v", str(semantics["video.frames"]), "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(output)], capture_output=True, timeout=30)
        self.assertEqual(encoded.returncode, 0, encoded.stderr.decode(errors="replace"))
        return output, {"outputs": [{"node_id": compiled["output_contract"]["primary"]["node_id"],
            "filename": output.name, "subfolder": str(namespace.parent), "type": "output"}]}

    def test_exact_prepared_graph_collects_and_materializes_real_probed_video(self):
        self.business_peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(self.business_cycle().status, "running")
        source, receipt = self.local_video_receipt()
        self.business_peer.invoke.return_value = receipt
        result = self.business_cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        asset = result.output_asset
        self.assertEqual(asset.kind, "reference_video")
        self.assertEqual(asset.metadata["generation_job_id"], str(self.job.pk))
        self.assertEqual(asset.metadata["preparation_id"], str(self.business_prep.pk))
        self.assertEqual(Path(asset.file.path).read_bytes(), source.read_bytes())
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit", "poll", "collect"])
        self.assertEqual(len(self.business_upload.uploads), 2)
        self.assertIsNone(result.actual_cost)
        self.assertIsNone(claim_next_comfy_asset_job("no-second-output"))

    def test_success_receipt_with_wrong_real_dimensions_does_not_create_an_asset(self):
        self.business_peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(self.business_cycle().status, "running")
        _, receipt = self.local_video_receipt(wrong_width=True)
        self.business_peer.invoke.return_value = receipt
        result = self.business_cycle()
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertIn("image.width", result.error_message)
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)

    def test_lost_submit_confirmation_recovers_frozen_token_then_collects_real_video(self):
        # The peer accepted the original POST. Simulate the worker dying before
        # recording the response, while keeping the original frozen request.
        stale = timezone.now() - timedelta(minutes=20)
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="submitting", provider_request_id="",
            provider_request_id_sha256="", submitted_at=None, submission_started_at=stale,
            claim_token=uuid4(), worker_id="lost-submit-worker", claimed_at=stale, heartbeat_at=stale)
        recovered = recover_stale_asset_generation_jobs(10, text=False, execution_kind="comfy_workflow",
            text_submission_grace_seconds=30)
        self.assertEqual(recovered, {"released": 0, "uncertain": 1})
        self.business_peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(reconcile_uncertain_comfy_asset_jobs(limit=1), {"resumed": 1, "terminal": 0, "unresolved": 0})
        self.job.refresh_from_db()
        self.assertEqual((self.job.status, self.job.remote_status), ("running", "succeeded"))
        self.assertEqual(self.job.provider_request_id, str(self.business_prep.pk))
        self.assertEqual(self.business_peer.invoke.call_args.args[2], {"request_id": str(self.business_prep.pk)})
        source, receipt = self.local_video_receipt()
        self.business_peer.invoke.return_value = receipt
        result = self.business_cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(Path(result.output_asset.file.path).read_bytes(), source.read_bytes())
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit", "poll", "collect"])
        self.assertEqual(len(self.business_upload.uploads), 2)

    def test_late_remote_success_after_cancel_preserves_unknown_bill(self):
        self.job.refresh_from_db()
        request_asset_generation_cancel(self.job)
        self.business_peer.invoke.return_value = {"status": "cancelling"}
        self.assertEqual(self.business_cycle().status, "cancel_requested")
        self.business_peer.invoke.return_value = {"status": "succeeded"}
        result = self.business_cycle()
        self.assertEqual(result.status, "cancelled", result.error_message)
        self.assertIsNone(result.actual_cost)
        self.assertIsNotNone(result.estimated_cost)
        self.assertIsNone(result.output_asset_id)
        self.assertEqual(self.business_peer.invoke.call_args.args[1], "poll")

    def test_crash_at_cancel_boundary_resumes_poll_not_a_second_cancel(self):
        self.job.refresh_from_db()
        request_asset_generation_cancel(self.job)
        self.business_peer.invoke.side_effect = SystemExit("isolated worker loss at cancel")
        with self.assertRaises(SystemExit):
            self.business_cycle()
        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.cancel_submitted_at)
        self.assertEqual(self.job.status, "cancel_requested")
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=2))
        recover_stale_asset_generation_jobs(1, execution_kind="comfy_workflow", text_submission_grace_seconds=30)
        self.business_peer.invoke.side_effect = None
        self.business_peer.invoke.return_value = {"status": "running"}
        result = self.business_cycle()
        self.assertEqual(result.status, "cancel_requested")
        self.assertEqual([call.args[1] for call in self.business_peer.invoke.call_args_list], ["submit", "cancel", "poll"])
