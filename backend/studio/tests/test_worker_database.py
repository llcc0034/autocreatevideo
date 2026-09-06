from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3

from django.db import OperationalError
from django.test import SimpleTestCase

from studio.services import worker_database as database
from studio.services.worker_heartbeat import WorkerHeartbeat


class WorkerDatabaseTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(vendor="sqlite", in_atomic_block=False)
        patcher = patch.object(database, "connection", self.connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claim_retries_only_database_lock_conflicts(self):
        claim = Mock(side_effect=[OperationalError("database is locked"), "claimed"])
        with patch.object(database.time, "sleep") as sleep, patch.object(
            database, "close_old_connections",
        ) as close:
            result = database.retry_worker_database_step(claim, "worker", wait_seconds=0.2)
        self.assertEqual(result, "claimed")
        self.assertEqual(claim.call_count, 2)
        sleep.assert_called_once_with(0.2)
        close.assert_called_once()

    def test_unknown_error_or_outer_transaction_is_not_retried(self):
        for vendor, atomic, message in (
            ("sqlite", False, "disk I/O error"),
            ("sqlite", True, "database is locked"),
            ("postgresql", False, "database is locked"),
        ):
            with self.subTest(vendor=vendor, atomic=atomic):
                self.connection.vendor = vendor
                self.connection.in_atomic_block = atomic
                claim = Mock(side_effect=OperationalError(message))
                with patch.object(database.time, "sleep") as sleep:
                    with self.assertRaises(OperationalError):
                        database.retry_worker_database_step(claim, wait_seconds=0.2)
                self.assertEqual(claim.call_count, 1)
                sleep.assert_not_called()

    def test_actual_sqlite_busy_recovers_after_writer_releases_lock(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "lock-test.sqlite3")
            writer = sqlite3.connect(path, timeout=0)
            reader = sqlite3.connect(path, timeout=0)
            try:
                writer.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY)")
                writer.execute("BEGIN IMMEDIATE")

                def claim():
                    try:
                        reader.execute("BEGIN IMMEDIATE")
                        reader.execute("INSERT INTO claims VALUES (1)")
                        reader.commit()
                    except sqlite3.OperationalError as exc:
                        reader.rollback()
                        raise OperationalError(str(exc)) from exc
                    return 1

                with patch.object(database.time, "sleep", side_effect=lambda _: writer.rollback()) as sleep:
                    self.assertEqual(database.retry_worker_database_step(claim, wait_seconds=0.1), 1)
                sleep.assert_called_once_with(0.1)
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            finally:
                reader.close()
                writer.close()

    def test_startup_heartbeat_lock_does_not_kill_worker(self):
        heartbeat = WorkerHeartbeat("test-worker", interval=0.1)
        with patch.object(heartbeat, "_touch", side_effect=OperationalError("database is locked")), patch(
            "studio.services.worker_heartbeat.threading.Thread",
        ) as thread:
            with heartbeat:
                thread.return_value.start.assert_called_once()

    def test_generation_claim_releases_runtime_gate_before_database_wait(self):
        from studio.management.commands import run_generation_worker as command
        locked = []

        @contextmanager
        def gate():
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

        def wait(_):
            self.assertFalse(locked)

        smoke = object()
        with patch.object(command, "local_qwen_runtime_lock", gate), patch.object(
            command, "claim_next_workflow_smoke",
            side_effect=[OperationalError("database is locked"), smoke],
        ), patch.object(command, "claim_next_comfy_asset_job") as assets, patch.object(
            database.time, "sleep", side_effect=wait,
        ):
            result = database.retry_worker_database_step(
                command._claim_generation_work, "test-worker", wait_seconds=0.1,
            )
        self.assertEqual(result, (smoke, None, None))
        assets.assert_not_called()

    def test_async_worker_claim_recovery_does_not_replay_provider_execution(self):
        from studio.management.commands import run_async_media_worker as command
        smoke = SimpleNamespace(id="test-smoke", status="failed")
        with ExitStack() as stack:
            stack.enter_context(patch.object(command, "resolve_configuration", return_value=SimpleNamespace(
                values={"runtime.asset_text_submission_grace_seconds": 30},
            )))
            stack.enter_context(patch.object(command, "WorkerHeartbeat"))
            for name in ("recover_stale_async_media_attempts", "recover_stale_deployment_smokes",
                         "recover_stale_asset_generation_jobs"):
                stack.enter_context(patch.object(command, name, return_value=0))
            stack.enter_context(patch.object(command, "claim_next_async_media_attempt", side_effect=[
                OperationalError("database is locked"), None,
            ]))
            stack.enter_context(patch.object(command, "claim_next_media_asset_job", return_value=None))
            stack.enter_context(patch.object(command, "claim_next_deployment_smoke", return_value=smoke))
            process = stack.enter_context(patch.object(command, "process_deployment_smoke", return_value=smoke))
            stack.enter_context(patch.object(database.time, "sleep"))
            command.Command().handle(once=True, poll_seconds=0.1, recover_minutes=1)
            process.assert_called_once_with(smoke)
            # Even a database error after submission must escape, not run the
            # provider operation a second time under the claim retry wrapper.
            process.reset_mock()
            process.side_effect = OperationalError("database is locked")
            stack.enter_context(patch.object(command, "claim_next_async_media_attempt", return_value=None))
            with self.assertRaises(OperationalError):
                command.Command().handle(once=True, poll_seconds=0.1, recover_minutes=1)
            process.assert_called_once_with(smoke)
