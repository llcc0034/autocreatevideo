from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from studio.management.commands import run_generation_worker
from studio.services.local_qwen_lock import local_qwen_runtime_lock


class GenerationWorkerCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_path = str(Path(self.temp_dir.name) / "shared-local-ai.lock")
        self.settings_override = override_settings(LOCAL_QWEN_LOCK_PATH=self.lock_path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        comfy_client = patch(
            "studio.management.commands.run_generation_worker.comfy_client_for_task",
            return_value=SimpleNamespace(),
        )
        comfy_client.start()
        self.addCleanup(comfy_client.stop)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch(
        "studio.management.commands.run_generation_worker.recover_stale_deferred_requests",
        return_value=0,
    )
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_can_use_a_separate_threshold(
        self,
        recover,
        _recover_deferred,
        _claim,
    ):
        call_command(
            "run_generation_worker",
            once=True,
            recover_minutes=30,
            startup_recover_minutes=1,
        )

        self.assertEqual(recover.call_args.args[0], 1)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_defaults_to_periodic_threshold(self, recover, _claim):
        call_command("run_generation_worker", once=True, recover_minutes=30)

        self.assertEqual(recover.call_args.args[0], 30)

    def test_asset_uncertain_reconcile_uses_explicit_runtime_batch_size(self):
        runtime = SimpleNamespace(values={
            "runtime.worker_stale_recovery_minutes": 30,
            "runtime.asset_text_submission_grace_seconds": 45,
            "runtime.asset_uncertain_reconcile_batch_size": 7,
        })
        with (
            patch(
                "studio.management.commands.run_generation_worker.resolve_configuration",
                return_value=runtime,
            ),
            patch(
                "studio.management.commands.run_generation_worker.recover_stale_tasks",
                return_value=0,
            ),
            patch(
                "studio.management.commands.run_generation_worker.recover_stale_deferred_requests",
                return_value=0,
            ),
            patch(
                "studio.management.commands.run_generation_worker.recover_stale_workflow_smokes",
                return_value=0,
            ),
            patch(
                "studio.management.commands.run_generation_worker.recover_stale_asset_generation_jobs",
                return_value={"released": 0, "uncertain": 0},
            ) as recover_assets,
            patch(
                "studio.management.commands.run_generation_worker.reconcile_uncertain_comfy_asset_jobs",
                return_value={"resumed": 0, "terminal": 0, "unresolved": 0},
            ) as reconcile,
            patch(
                "studio.management.commands.run_generation_worker.claim_next_workflow_smoke",
                return_value=None,
            ),
            patch(
                "studio.management.commands.run_generation_worker.claim_next_comfy_asset_job",
                return_value=None,
            ),
            patch(
                "studio.management.commands.run_generation_worker.claim_next_task",
                return_value=None,
            ),
            patch(
                "studio.management.commands.run_generation_worker.claim_next_deferred_request",
                return_value=None,
            ),
        ):
            run_generation_worker.Command()._run_worker({
                "once": True,
                "poll": 0.1,
                "recover_minutes": None,
                "startup_recover_minutes": None,
                "recover_interval": 60,
            })

        recover_assets.assert_called_once_with(
            30,
            text=False,
            execution_kind="comfy_workflow",
            text_submission_grace_seconds=45,
        )
        reconcile.assert_called_once_with(limit=7)

    @patch("studio.management.commands.run_generation_worker.claim_next_task")
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_qwen_owner_prevents_worker_from_claiming(self, _recover, claim):
        with local_qwen_runtime_lock():
            call_command("run_generation_worker", once=True, recover_minutes=30)

        claim.assert_not_called()

    @patch("studio.management.commands.run_generation_worker.process_deferred_request")
    @patch("studio.management.commands.run_generation_worker.claim_next_deferred_request")
    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_deferred_requests", return_value=0)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_worker_does_not_claim_retired_shot_detail_queue(
        self,
        _recover_tasks,
        _recover_deferred,
        _claim_task,
        claim_deferred,
        process_deferred,
    ):
        deferred_request = SimpleNamespace(
            id="deferred-1",
            attempts=1,
            max_attempts=3,
            status="waiting_upstream",
        )
        processed = SimpleNamespace(id="deferred-1", status="materialized")
        claim_deferred.return_value = deferred_request
        process_deferred.return_value = processed

        call_command("run_generation_worker", once=True, recover_minutes=30)

        claim_deferred.assert_called_once()
        process_deferred.assert_called_once_with(deferred_request)
        self.assertFalse(hasattr(run_generation_worker, "claim_next_shot_detail_item"))
        self.assertFalse(hasattr(run_generation_worker, "process_shot_detail_item"))
        self.assertFalse(hasattr(run_generation_worker, "recover_stale_shot_detail_items"))
