from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings
from django.test.runner import DiscoverRunner

from autoprompt_backend.test_runner import IsolatedMediaDiscoverRunner


class IsolatedTestMediaTests(SimpleTestCase):
    def test_standard_entry_uses_isolated_runner(self):
        self.assertEqual(settings.TEST_RUNNER, "autoprompt_backend.test_runner.IsolatedMediaDiscoverRunner")

    def test_real_file_and_nested_fixture_roots_restore_without_touching_original_root(self):
        original_root, original_storages = settings.MEDIA_ROOT, settings.STORAGES
        runner = IsolatedMediaDiscoverRunner()
        with patch.object(DiscoverRunner, "setup_test_environment"), patch.object(DiscoverRunner, "teardown_test_environment"):
            runner.setup_test_environment()
            root = Path(settings.MEDIA_ROOT)
            try:
                self.assertNotEqual(root, Path(original_root))
                self.assertEqual(settings.STORAGES["staticfiles"], original_storages["staticfiles"])
                key = default_storage.save("execution-requests/test/request.json", ContentFile(b"{}"))
                self.assertTrue(Path(default_storage.path(key)).is_file())
                with override_settings(MEDIA_ROOT=root / "fixture"):
                    fixture_key = default_storage.save("asset.png", ContentFile(b"test"))
                    self.assertEqual(Path(default_storage.path(fixture_key)).parent, root / "fixture")
                self.assertEqual(Path(default_storage.path(key)), root / key)
            finally:
                runner.teardown_test_environment()
            self.assertFalse(root.exists())
            self.assertEqual(settings.MEDIA_ROOT, original_root)
            self.assertEqual(settings.STORAGES, original_storages)

    def test_teardown_failure_still_restores_settings_and_removes_only_test_directory(self):
        original_root = settings.MEDIA_ROOT
        runner = IsolatedMediaDiscoverRunner()
        with patch.object(DiscoverRunner, "setup_test_environment"):
            runner.setup_test_environment()
        root = Path(settings.MEDIA_ROOT)
        with patch.object(DiscoverRunner, "teardown_test_environment", side_effect=RuntimeError("fixture teardown")):
            with self.assertRaisesRegex(RuntimeError, "fixture teardown"):
                runner.teardown_test_environment()
        self.assertFalse(root.exists())
        self.assertEqual(settings.MEDIA_ROOT, original_root)
