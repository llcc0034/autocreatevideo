"""Keep managed test files out of the application's persistent media root."""
from contextlib import ExitStack
from tempfile import TemporaryDirectory

from django.conf import settings
from django.test import override_settings
from django.test.runner import DiscoverRunner


class IsolatedMediaDiscoverRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media_context = ExitStack()
        try:
            root = self._media_context.enter_context(TemporaryDirectory(prefix="autoprompt-test-media-"))
            self._media_context.enter_context(override_settings(MEDIA_ROOT=root, STORAGES={
                **settings.STORAGES,
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            }))
        except BaseException:
            self.teardown_test_environment()
            raise

    def teardown_test_environment(self, **kwargs):
        try:
            super().teardown_test_environment(**kwargs)
        finally:
            self._media_context.close()
