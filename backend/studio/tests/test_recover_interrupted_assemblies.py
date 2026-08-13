from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from studio.models import AssemblyJob
from studio.tests.factories import build_story


class RecoverInterruptedAssembliesCommandTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, self.episode, _, _ = build_story()

    def _job(self, *, status, dry_run, attempts=0, max_attempts=3):
        return AssemblyJob.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            scope=AssemblyJob.Scope.EPISODE,
            status=status,
            dry_run=dry_run,
            attempts=attempts,
            max_attempts=max_attempts,
        )

    def test_only_interrupted_running_jobs_are_recovered(self):
        running = self._job(
            status=AssemblyJob.Status.RUNNING, dry_run=False, attempts=1,
        )
        queued = self._job(status=AssemblyJob.Status.QUEUED, dry_run=False)
        incomplete_legacy = self._job(status=AssemblyJob.Status.PLANNED, dry_run=False)
        prepared_legacy = self._job(status=AssemblyJob.Status.PLANNED, dry_run=False)
        prepared_legacy.output_path = "prepared.mp4"
        prepared_legacy.ffmpeg_command = ["ffmpeg", "prepared.mp4"]
        prepared_legacy.save(update_fields=("output_path", "ffmpeg_command", "updated_at"))
        legitimate_plan = self._job(status=AssemblyJob.Status.PLANNED, dry_run=True)
        completed = self._job(status=AssemblyJob.Status.SUCCEEDED, dry_run=False)
        exhausted = self._job(
            status=AssemblyJob.Status.RUNNING, dry_run=False, attempts=3, max_attempts=3,
        )
        output = StringIO()

        call_command(
            "recover_interrupted_assemblies",
            reason="Test process restart",
            stdout=output,
        )

        running.refresh_from_db()
        queued.refresh_from_db()
        incomplete_legacy.refresh_from_db()
        prepared_legacy.refresh_from_db()
        legitimate_plan.refresh_from_db()
        completed.refresh_from_db()
        exhausted.refresh_from_db()
        self.assertEqual(running.status, AssemblyJob.Status.QUEUED)
        self.assertIsNone(running.finished_at)
        self.assertIn("Test process restart", running.error_message)
        self.assertEqual(queued.status, AssemblyJob.Status.QUEUED)
        self.assertEqual(incomplete_legacy.status, AssemblyJob.Status.FAILED)
        self.assertIn("no executable command", incomplete_legacy.error_message)
        self.assertEqual(prepared_legacy.status, AssemblyJob.Status.QUEUED)
        self.assertIn("legacy prepared job was queued", prepared_legacy.error_message)
        self.assertEqual(legitimate_plan.status, AssemblyJob.Status.PLANNED)
        self.assertEqual(completed.status, AssemblyJob.Status.SUCCEEDED)
        self.assertEqual(exhausted.status, AssemblyJob.Status.FAILED)
        self.assertIsNotNone(exhausted.finished_at)
        self.assertIn("retry limit reached", exhausted.error_message)
        self.assertIn("Recovered 4 interrupted assembly job(s)", output.getvalue())

    def test_recovery_is_idempotent_and_does_not_rewrite_terminal_jobs(self):
        running = self._job(
            status=AssemblyJob.Status.RUNNING, dry_run=False, attempts=1,
        )
        failed = self._job(status=AssemblyJob.Status.FAILED, dry_run=False)
        failed.error_message = "Original FFmpeg failure"
        failed.save(update_fields=("error_message", "updated_at"))

        first_output = StringIO()
        call_command(
            "recover_interrupted_assemblies",
            reason="First restart",
            stdout=first_output,
        )
        running.refresh_from_db()
        failed.refresh_from_db()
        recovered_queued_at = running.queued_at

        second_output = StringIO()
        call_command(
            "recover_interrupted_assemblies",
            reason="Second restart",
            stdout=second_output,
        )
        running.refresh_from_db()
        failed.refresh_from_db()

        self.assertEqual(running.status, AssemblyJob.Status.QUEUED)
        self.assertEqual(running.queued_at, recovered_queued_at)
        self.assertEqual(running.error_message.count("First restart"), 1)
        self.assertNotIn("Second restart", running.error_message)
        self.assertEqual(failed.error_message, "Original FFmpeg failure")
        self.assertIn("Recovered 1 interrupted assembly job(s)", first_output.getvalue())
        self.assertIn("Recovered 0 interrupted assembly job(s)", second_output.getvalue())

    def test_zero_stale_window_includes_job_updated_at_the_threshold(self):
        exhausted = self._job(
            status=AssemblyJob.Status.RUNNING,
            dry_run=False,
            attempts=3,
            max_attempts=3,
        )
        threshold = exhausted.updated_at

        with patch("studio.services.assembly_queue.timezone.now", return_value=threshold):
            call_command(
                "recover_interrupted_assemblies",
                reason="Same clock tick restart",
                stdout=StringIO(),
            )

        exhausted.refresh_from_db()
        self.assertEqual(exhausted.status, AssemblyJob.Status.FAILED)
        self.assertEqual(exhausted.finished_at, threshold)
        self.assertIn("retry limit reached", exhausted.error_message)
