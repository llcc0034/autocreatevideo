from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from studio.services import creative_drafts, prompt_translation
from studio.services.local_qwen_lock import local_qwen_runtime_lock


class GenerationWorkerCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_path = str(Path(self.temp_dir.name) / "shared-local-ai.lock")
        self.settings_override = override_settings(LOCAL_QWEN_LOCK_PATH=self.lock_path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_can_use_a_separate_threshold(self, recover, _claim):
        call_command(
            "run_generation_worker",
            once=True,
            recover_minutes=30,
            startup_recover_minutes=0,
        )

        self.assertEqual(recover.call_args.args[0], 0)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_defaults_to_periodic_threshold(self, recover, _claim):
        call_command("run_generation_worker", once=True, recover_minutes=30)

        self.assertEqual(recover.call_args.args[0], 30)

    @patch("studio.management.commands.run_generation_worker.claim_next_task")
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_qwen_owner_prevents_worker_from_claiming(self, _recover, claim):
        with local_qwen_runtime_lock():
            call_command("run_generation_worker", once=True, recover_minutes=30)

        claim.assert_not_called()

    @patch("studio.management.commands.run_generation_worker.process_task")
    @patch("studio.management.commands.run_generation_worker.claim_next_task")
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_worker_holds_resource_lease_during_processing(
        self, _recover, claim, process,
    ):
        task = SimpleNamespace(id="task-1", attempts=1, max_attempts=3, status="succeeded")
        claim.return_value = task

        def assert_qwen_is_blocked(current, **_kwargs):
            translation_config = {
                "runtime_python": Path("python"),
                "runtime_script": Path("runtime.py"),
                "timeout_seconds": 30,
            }
            creative_config = dict(translation_config)
            with patch("studio.services.prompt_translation.subprocess.run") as translate_run:
                with self.assertRaises(prompt_translation.PromptTranslationResourceBusy):
                    prompt_translation._run_runtime({"text": "测试"}, translation_config)
                translate_run.assert_not_called()
            with patch("studio.services.creative_drafts.subprocess.run") as creative_run:
                with self.assertRaises(creative_drafts.CreativeResourceBusy):
                    creative_drafts._run_runtime(
                        {"operation": "creative_storyboard"}, creative_config,
                    )
                creative_run.assert_not_called()
            return current

        process.side_effect = assert_qwen_is_blocked
        call_command("run_generation_worker", once=True, recover_minutes=30)

        process.assert_called_once()
