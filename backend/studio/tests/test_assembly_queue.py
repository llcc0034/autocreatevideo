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

from studio.models import AssemblyJob, Episode, WorkerLease
from studio.services.assembly_queue import (
    ASSEMBLY_WORKER_HEARTBEAT_KEY,
    claim_next_assembly,
    process_assembly_job,
    recover_stale_assemblies,
)
from studio.services.provenance import persist_assembly_provenance
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
