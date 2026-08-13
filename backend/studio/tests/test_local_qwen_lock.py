from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from studio.services import creative_drafts, prompt_translation
from studio.services.local_qwen_lock import (
    LocalQwenResourceBusy,
    local_qwen_runtime_lock,
)


class LocalQwenRuntimeLockTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_path = str(Path(self.temp_dir.name) / "shared-qwen.lock")
        self.settings_override = override_settings(LOCAL_QWEN_LOCK_PATH=self.lock_path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_second_file_handle_fails_nonblocking_then_lock_is_reusable(self):
        with local_qwen_runtime_lock():
            with self.assertRaises(LocalQwenResourceBusy):
                with local_qwen_runtime_lock():
                    self.fail("second owner must not enter")
        with local_qwen_runtime_lock() as path:
            self.assertEqual(str(path), self.lock_path)

    def test_translation_maps_shared_lock_to_busy_without_starting_subprocess(self):
        config = {
            "runtime_python": Path("python"), "runtime_script": Path("runtime.py"),
            "timeout_seconds": 30,
        }
        with local_qwen_runtime_lock(), patch(
            "studio.services.prompt_translation.subprocess.run",
        ) as run:
            with self.assertRaises(prompt_translation.PromptTranslationResourceBusy):
                prompt_translation._run_runtime({"text": "测试"}, config)
            run.assert_not_called()

    def test_creative_maps_shared_lock_to_busy_without_starting_subprocess(self):
        config = {
            "runtime_python": Path("python"), "runtime_script": Path("runtime.py"),
            "timeout_seconds": 30,
        }
        with local_qwen_runtime_lock(), patch(
            "studio.services.creative_drafts.subprocess.run",
        ) as run:
            with self.assertRaises(creative_drafts.CreativeResourceBusy):
                creative_drafts._run_runtime({"operation": "creative_storyboard"}, config)
            run.assert_not_called()
