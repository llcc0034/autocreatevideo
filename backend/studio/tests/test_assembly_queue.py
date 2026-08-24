from datetime import timedelta
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    Episode,
    ProductionAssemblyLink,
    ProductionRecipeVersion,
    ProductionRun,
    StageRun,
    WorkerLease,
)
from studio.services.assembly_queue import (
    ASSEMBLY_WORKER_HEARTBEAT_KEY,
    claim_next_assembly,
    process_assembly_job,
    recover_stale_assemblies,
)
from studio.services.provenance import persist_assembly_provenance
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_runs import retry_production_run
from studio.tests.factories import build_story


class AssemblyQueueTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_root = Path(self.temp.name) / "media"
        self.override = override_settings(
            MEDIA_ROOT=self.media_root,
            ASSEMBLY_TIMEOUT_SECONDS=30,
            ASSEMBLY_HEARTBEAT_SECONDS=0.01,
            WORKER_HEARTBEAT_INTERVAL_SECONDS=60,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.source_verification_patcher = patch(
            "studio.services.assembly_queue.verify_assembly_sources",
            return_value=None,
        )
        self.source_verification_patcher.start()
        self.addCleanup(self.source_verification_patcher.stop)
        self.user, self.project, _, _, self.episode, _, _ = build_story()

    def _queued_job(self, *, max_attempts=3, filename="assembled.mp4"):
        output = (
            self.media_root / "assemblies" / str(self.user.id)
            / str(self.project.id) / filename
        )
        return AssemblyJob.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            scope=AssemblyJob.Scope.EPISODE,
            status=AssemblyJob.Status.QUEUED,
            dry_run=False,
            output_path=str(output),
            ffmpeg_command=["ffmpeg-test", "-i", "source.mp4", str(output)],
            source_signature="a" * 64,
            max_attempts=max_attempts,
        )

    def _link_assembly_run_with_delivery_gate(self, job):
        recipe_contract = json_contract({"schema_version": 1, "stages": []})
        recipe = ProductionRecipeVersion.objects.create(
            recipe_key=f"assembly-auto-{job.id}",
            version=1,
            name="Assembly auto advance fixture",
            definition=recipe_contract.value,
            definition_schema_version=recipe_contract.schema_version,
            definition_size_bytes=recipe_contract.size_bytes,
            definition_sha256=recipe_contract.sha256,
        )
        config = json_contract({"schema_version": 1})
        run_input = json_contract({
            "schema_version": 1,
            "episode_ids": [str(self.episode.id)],
        })
        run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=recipe,
            idempotency_key=f"assembly-auto-{job.id}",
            request_sha256="e" * 64,
            status=ProductionRun.Status.RUNNING,
            current_stage_key="assembly",
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=run_input.value,
            input_snapshot_schema_version=run_input.schema_version,
            input_snapshot_size_bytes=run_input.size_bytes,
            input_snapshot_sha256=run_input.sha256,
        )

        def stage_row(*, key, ordinal, mode, status, approval_required=False):
            stage = StageRun(
                production_run=run,
                stage_key=key,
                ordinal=ordinal,
                mode=mode,
                status=status,
                approval_required=approval_required,
                idempotency_key=f"{run.id}:{ordinal}:{key}",
            )
            for field, payload in (
                ("routing", {"schema_version": 1}),
                ("routing_decision", {"schema_version": 1}),
                ("config_snapshot", {"schema_version": 1}),
                ("input_snapshot", {"schema_version": 1, "stage_key": key}),
            ):
                assign_json_contract(stage, field, payload)
            stage.save()
            return stage

        assembly_stage = stage_row(
            key="assembly",
            ordinal=1,
            mode="existing_pipeline",
            status=StageRun.Status.RUNNING,
        )
        delivery_gate = stage_row(
            key="delivery",
            ordinal=2,
            mode="manual",
            status=StageRun.Status.PENDING,
            approval_required=True,
        )
        ProductionAssemblyLink.objects.create(
            production_run=run,
            stage_run=assembly_stage,
            episode=self.episode,
            assembly_job=job,
        )
        return run, assembly_stage, delivery_gate

    @staticmethod
    def _successful_ffmpeg(command, **kwargs):
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[-1]).write_bytes(b"complete-video")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def test_claim_persists_worker_identity_token_attempt_and_heartbeat(self):
        queued = self._queued_job()

        claimed = claim_next_assembly(worker_id="worker-A")

        self.assertEqual(claimed.id, queued.id)
        self.assertEqual(claimed.status, AssemblyJob.Status.RUNNING)
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(claimed.worker_id, "worker-A")
        self.assertIsNotNone(claimed.claim_token)
        self.assertIsNotNone(claimed.heartbeat_at)
        self.assertIsNotNone(claimed.started_at)
        self.assertIsNone(claim_next_assembly(worker_id="worker-B"))

    def test_worker_writes_unique_temporary_output_then_completes_scope(self):
        queued = self._queued_job(filename="episode-final.mp4")
        claimed = claim_next_assembly(worker_id="worker-A")

        with patch(
            "studio.services.assembly_queue._run_ffmpeg",
            side_effect=self._successful_ffmpeg,
        ) as run:
            result = process_assembly_job(claimed)

        self.assertEqual(result.id, queued.id)
        self.assertEqual(result.status, AssemblyJob.Status.SUCCEEDED)
        runtime_output = Path(run.call_args.args[0][-1])
        self.assertIn(".part-", runtime_output.name)
        self.assertFalse(runtime_output.exists())
        self.assertEqual(Path(result.output_path).read_bytes(), b"complete-video")
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.status, Episode.Status.COMPLETED)

    def test_terminal_assembly_auto_advances_run_into_delivery_gate(self):
        queued = self._queued_job(filename="auto-advance-episode.mp4")
        run, assembly_stage, delivery_gate = self._link_assembly_run_with_delivery_gate(queued)
        claimed = claim_next_assembly(worker_id="worker-auto-advance")

        with patch(
            "studio.services.assembly_queue._run_ffmpeg",
            side_effect=self._successful_ffmpeg,
        ), self.captureOnCommitCallbacks(execute=True):
            result = process_assembly_job(claimed)

        self.assertEqual(result.status, AssemblyJob.Status.SUCCEEDED)
        run.refresh_from_db()
        assembly_stage.refresh_from_db()
        delivery_gate.refresh_from_db()
        self.assertEqual(assembly_stage.status, StageRun.Status.SUCCEEDED)
        self.assertEqual(delivery_gate.status, StageRun.Status.AWAITING_APPROVAL)
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(run.current_stage_key, "delivery")

    def test_known_assembly_failure_auto_blocks_without_implicit_retry(self):
        queued = self._queued_job(
            max_attempts=1,
            filename="auto-block-assembly.mp4",
        )
        run, assembly_stage, delivery_gate = self._link_assembly_run_with_delivery_gate(queued)
        claimed = claim_next_assembly(worker_id="worker-auto-block")
        failure = subprocess.CompletedProcess(
            ["ffmpeg-test"],
            1,
            stdout="",
            stderr="codec initialization failed",
        )

        with patch(
            "studio.services.assembly_queue._run_ffmpeg",
            return_value=failure,
        ), self.captureOnCommitCallbacks(execute=True):
            result = process_assembly_job(claimed)

        self.assertEqual(result.status, AssemblyJob.Status.FAILED)
        run.refresh_from_db()
        assembly_stage.refresh_from_db()
        delivery_gate.refresh_from_db()
        self.assertEqual(assembly_stage.status, StageRun.Status.BLOCKED)
        self.assertEqual(assembly_stage.reason_code, "assembly_failed")
        self.assertEqual(delivery_gate.status, StageRun.Status.PENDING)
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(run.current_stage_key, "assembly")
        link = ProductionAssemblyLink.objects.get(production_run=run)
        self.assertEqual(link.assembly_job_id, queued.id)

    def test_explicit_run_retry_replaces_failed_assembly_job(self):
        queued = self._queued_job(max_attempts=1, filename="failed-before-retry.mp4")
        run, assembly_stage, _delivery_gate = self._link_assembly_run_with_delivery_gate(queued)
        claimed = claim_next_assembly(worker_id="worker-before-explicit-retry")
        failure = subprocess.CompletedProcess(
            ["ffmpeg-test"],
            1,
            stdout="",
            stderr="codec initialization failed",
        )
        with patch(
            "studio.services.assembly_queue._run_ffmpeg",
            return_value=failure,
        ), self.captureOnCommitCallbacks(execute=True):
            process_assembly_job(claimed)
        replacement = self._queued_job(filename="explicit-retry.mp4")

        with patch(
            "studio.services.production_runs.create_assembly",
            return_value=replacement,
        ):
            retried = retry_production_run(run, user=self.user)

        link = ProductionAssemblyLink.objects.get(production_run=run)
        assembly_stage.refresh_from_db()
        self.assertEqual(link.assembly_job_id, replacement.id)
        self.assertEqual(retried.status, ProductionRun.Status.RUNNING)
        self.assertEqual(assembly_stage.status, StageRun.Status.RUNNING)
        self.assertEqual(assembly_stage.reason_code, "waiting_external_task")
        queued.refresh_from_db()
        self.assertEqual(queued.status, AssemblyJob.Status.FAILED)

    def test_failures_retry_then_persist_terminal_reason(self):
        queued = self._queued_job(max_attempts=2)
        failure = subprocess.CompletedProcess(
            ["ffmpeg-test"], 1, stdout="", stderr="codec initialization failed",
        )

        first_claim = claim_next_assembly(worker_id="worker-A")
        with patch("studio.services.assembly_queue._run_ffmpeg", return_value=failure):
            first = process_assembly_job(first_claim)
        self.assertEqual(first.status, AssemblyJob.Status.QUEUED)
        self.assertEqual(first.attempts, 1)
        self.assertIn("queued for retry", first.error_message)
        self.assertIn("codec initialization failed", first.error_message)

        second_claim = claim_next_assembly(worker_id="worker-B")
        with patch("studio.services.assembly_queue._run_ffmpeg", return_value=failure):
            second = process_assembly_job(second_claim)
        self.assertEqual(second.id, queued.id)
        self.assertEqual(second.status, AssemblyJob.Status.FAILED)
        self.assertEqual(second.attempts, 2)
        self.assertIsNotNone(second.finished_at)
        self.assertIn("Attempt 2/2 failed", second.error_message)
        self.assertIn("codec initialization failed", second.error_message)

    def test_recovery_requeues_stale_claim_and_removes_its_partial_file(self):
        job = self._queued_job()
        token = uuid.uuid4()
        job.status = AssemblyJob.Status.RUNNING
        job.attempts = 1
        job.claim_token = token
        job.worker_id = "dead-worker"
        job.heartbeat_at = timezone.now() - timedelta(hours=1)
        job.save(update_fields=(
            "status", "attempts", "claim_token", "worker_id", "heartbeat_at", "updated_at",
        ))
        output = Path(job.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(f"{output.stem}.part-{token.hex}{output.suffix}")
        partial.write_bytes(b"incomplete")

        recovered = recover_stale_assemblies(30, reason="worker disappeared")

        job.refresh_from_db()
        self.assertEqual(recovered, {"succeeded": 0, "requeued": 1, "failed": 0})
        self.assertEqual(job.status, AssemblyJob.Status.QUEUED)
        self.assertIsNone(job.claim_token)
        self.assertEqual(job.worker_id, "")
        self.assertIn("worker disappeared", job.error_message)
        self.assertFalse(partial.exists())

    def test_recovery_requeues_unsealed_final_output_left_after_atomic_replace(self):
        job = self._queued_job(filename="already-final.mp4")
        job.status = AssemblyJob.Status.RUNNING
        job.attempts = 1
        job.claim_token = uuid.uuid4()
        job.heartbeat_at = timezone.now() - timedelta(hours=1)
        job.save(update_fields=(
            "status", "attempts", "claim_token", "heartbeat_at", "updated_at",
        ))
        output = Path(job.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"atomically-completed-output")

        recovered = recover_stale_assemblies(30)

        job.refresh_from_db()
        # Atomic publication alone is no longer a success boundary: a worker
        # must also persist the signed provenance sidecar.  Re-running FFmpeg
        # is safer than adopting media whose source chain cannot be verified.
        self.assertEqual(recovered, {"succeeded": 0, "requeued": 1, "failed": 0})
        self.assertEqual(job.status, AssemblyJob.Status.QUEUED)
        self.assertIsNone(job.finished_at)
        self.episode.refresh_from_db()
        self.assertNotEqual(self.episode.status, Episode.Status.COMPLETED)

    def test_recovery_commits_final_output_with_verified_provenance(self):
        job = self._queued_job(filename="sealed-final.mp4")
        job.status = AssemblyJob.Status.RUNNING
        job.attempts = 1
        job.claim_token = uuid.uuid4()
        job.heartbeat_at = timezone.now() - timedelta(hours=1)
        job.save(update_fields=(
            "status", "attempts", "claim_token", "heartbeat_at", "updated_at",
        ))
        output = Path(job.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"atomically-completed-output")
        persist_assembly_provenance(job, generated_at=timezone.now())

        recovered = recover_stale_assemblies(30)

        job.refresh_from_db()
        self.assertEqual(recovered, {"succeeded": 1, "requeued": 0, "failed": 0})
        self.assertEqual(job.status, AssemblyJob.Status.SUCCEEDED)
        self.assertIsNotNone(job.finished_at)
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.status, Episode.Status.COMPLETED)

    def test_management_worker_persists_required_process_heartbeat_when_idle(self):
        output = StringIO()

        call_command("run_assembly_worker", once=True, stdout=output)

        heartbeat = WorkerLease.objects.get(pk=ASSEMBLY_WORKER_HEARTBEAT_KEY)
        self.assertIsNotNone(heartbeat.updated_at)

    def test_management_worker_claims_and_executes_one_persisted_job(self):
        job = self._queued_job(filename="command-worker.mp4")
        output = StringIO()

        with patch(
            "studio.services.assembly_queue._run_ffmpeg",
            side_effect=self._successful_ffmpeg,
        ):
            call_command(
                "run_assembly_worker",
                once=True,
                worker_id="command-test-worker",
                stdout=output,
            )

        job.refresh_from_db()
        self.assertEqual(job.status, AssemblyJob.Status.SUCCEEDED)
        self.assertEqual(job.worker_id, "command-test-worker")
        self.assertEqual(job.attempts, 1)
        self.assertTrue(Path(job.output_path).is_file())
        self.assertIn(f"Assembly {job.id}: succeeded", output.getvalue())
