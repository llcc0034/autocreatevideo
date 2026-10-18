"""Shared transport policy checks are local and never call a provider."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.services.provider_registry import (
    ComfyUIDriver, DeepSeekDriver, LocalQwenDriver, OpenAICompatibleDriver,
    ProviderDriverError, RestrictedAsyncRestDriver,
)


class ExecutionCredentialPolicyTests(SimpleTestCase):
    def deployment(self, credential=None):
        return SimpleNamespace(credential=credential, provider=SimpleNamespace(credential=None))

    def test_chat_requires_real_credentials_except_registered_loopback_local_driver(self):
        for driver in (DeepSeekDriver(), OpenAICompatibleDriver()):
            for endpoint in ("https://api.example.com", "http://127.0.0.1:1234"):
                with self.subTest(driver=type(driver).__name__, endpoint=endpoint), patch(
                        "studio.services.provider_registry._combined_config", return_value={"base_url": endpoint}), self.assertRaises(ProviderDriverError) as error:
                    driver.validate_execution_credentials(self.deployment())
                self.assertEqual(error.exception.code, "credential_missing")
        with patch("studio.services.provider_registry._combined_config", return_value={"base_url": "http://127.0.0.1:1234"}):
            self.assertIsNone(LocalQwenDriver().validate_execution_credentials(self.deployment()))
        with patch("studio.services.provider_registry._combined_config", return_value={"base_url": "https://api.example.com"}), self.assertRaises(ProviderDriverError):
            LocalQwenDriver().validate_execution_credentials(self.deployment())

    def test_configured_credentials_are_checked_but_secret_is_not_returned(self):
        reference = object()
        for driver in (DeepSeekDriver(), OpenAICompatibleDriver(), LocalQwenDriver(), RestrictedAsyncRestDriver()):
            with self.subTest(driver=type(driver).__name__), patch("studio.services.provider_registry._combined_config",
                    return_value={"base_url": "https://api.example.com"}), patch(
                    "studio.services.provider_registry.resolve_credential", return_value="isolated-test-secret") as resolve:
                self.assertIsNone(driver.validate_execution_credentials(self.deployment(reference)))
                resolve.assert_called_once_with(reference)
        with patch("studio.services.provider_registry.resolve_credential", side_effect=ProviderDriverError("unavailable")), self.assertRaises(ProviderDriverError):
            RestrictedAsyncRestDriver().validate_execution_credentials(self.deployment(reference))

    def test_optional_and_unused_transport_credentials_keep_actual_driver_semantics(self):
        with patch("studio.services.provider_registry.resolve_credential") as resolve:
            self.assertIsNone(RestrictedAsyncRestDriver().validate_execution_credentials(self.deployment()))
            self.assertIsNone(ComfyUIDriver().validate_execution_credentials(self.deployment(object())))
        resolve.assert_not_called()
