from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from studio.models import ConfigRevision, ModelProfileVersion, ProviderProfile
from studio.services.async_media_contract import async_operation_contract
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.json_contracts import assign_json_contract
from studio.services.provider_registry import ProviderDriverError, RestrictedAsyncRestDriver
from studio.services.remote_artifacts import _download_one, _validated_artifact_url
from studio.tests.factories import conditioning_contract


def _media_constraints():
    return {
        "schema_version": 1,
        "resolution": {
            "minimum_width": 64,
            "maximum_width": 4096,
            "minimum_height": 64,
            "maximum_height": 4096,
        },
        "fps": {"allowed": [24]},
        "duration_ms": {"minimum": 100, "maximum": 30_000},
    }


def _async_spec():
    def operation(kind):
        properties = {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "model": {"type": "string", "minLength": 1, "maxLength": 255},
            "width": {"type": "integer", "minimum": 64, "maximum": 4096},
            "height": {"type": "integer", "minimum": 64, "maximum": 4096},
        }
        if kind == "video":
            properties.update({
                "duration_seconds": {"type": "number", "minimum": 0.1, "maximum": 30},
                "fps": {"type": "integer", "minimum": 1, "maximum": 120},
            })
        return {
            "output_kind": kind,
            "request_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "model", "width", "height"],
                "properties": properties,
            },
            "smoke_inputs": {
                "prompt": "smoke",
                "model": "cloud-v1",
                "width": 64,
                "height": 64,
                **({"duration_seconds": 0.5, "fps": 24} if kind == "video" else {}),
            },
        }
    return {
        "schema_version": 1,
        "model_family": "async_cloud",
        "capabilities": ["image.text_to_image", "video.text_to_video"],
        "conditioning_contract": conditioning_contract("T2VA"),
        "media_constraints": _media_constraints(),
        "async_media": {
            "schema_version": 1,
            "operations": {
                "image.text_to_image": operation("image"),
                "video.text_to_video": operation("video"),
            },
        },
    }


def _provider_config():
    return {
        "base_url": "https://api.example.com",
        "health_path": "/health",
        "submit_path": "/jobs",
        "poll_path": "/jobs/{request_id}",
        "cancel_path": "/jobs/{request_id}/cancel",
        "collect_path": "/jobs/{request_id}",
        "request_id_field": "id",
        "status_field": "status",
        "artifacts_field": "artifacts",
        "artifact_url_field": "url",
        "artifact_kind_field": "kind",
        "artifact_id_field": "id",
        "artifact_sha256_field": "sha256",
        "actual_cost_field": "actual_cost",
        "currency_field": "currency",
        "idempotency_header": "Idempotency-Key",
        "terminal_success": ["succeeded"],
        "terminal_failure": ["failed"],
        "terminal_cancelled": ["cancelled"],
        "artifact_allowed_hosts": ["cdn.example.com"],
        "request_field_map": {"model": "model", "prompt": "prompt", "width": "width", "height": "height"},
        "timeout_seconds": 30,
        "poll_interval_seconds": 1,
        "max_poll_seconds": 60,
        "capability_ttl_seconds": 3600,
        "pricing": {"currency": "USD", "fixed_per_request": "0.010000"},
    }


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=(
    "api.example.com", "cdn.example.com", "cdn2.example.com",
))
class RestrictedAsyncRestDriverTests(TestCase):
    def test_terminal_and_artifact_host_security_boundaries_are_configurable(self):
        superuser = get_user_model().objects.create_superuser(
            username="provider-boundary-admin",
            email="provider-boundary@example.com",
            password="not-used-in-test",
        )
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "security.provider.async_rest_terminal_status_max_items": 1,
                "security.provider.async_rest_terminal_status_max_characters": 10,
                "security.provider.async_rest_artifact_host_max_items": 1,
            },
            created_by=superuser,
        )
        publish_config_revision(revision, published_by=superuser)
        driver = RestrictedAsyncRestDriver()

        config = _provider_config()
        driver.validate_config(config)
        config["terminal_success"] = ["succeeded", "complete"]
        with self.assertRaisesMessage(ValidationError, "1 个非空状态字符串"):
            driver.validate_config(config)

        config = _provider_config()
        config["terminal_success"] = ["x" * 11]
        with self.assertRaisesMessage(ValidationError, "10 个字符"):
            driver.validate_config(config)

        config = _provider_config()
        config["artifact_allowed_hosts"] = [
            "cdn.example.com", "cdn2.example.com",
        ]
        with self.assertRaisesMessage(ValidationError, "1 个产物主机"):
            driver.validate_config(config)

        config = _provider_config()
        provider = SimpleNamespace(driver_config=config, credential=None)
        deployment = SimpleNamespace(
            provider=provider,
            profile_version=SimpleNamespace(provider=provider),
            runtime_config={},
            credential=None,
        )
        with patch(
            "studio.services.provider_registry._combined_config",
            return_value=config,
        ), patch(
            "studio.services.provider_registry.validate_endpoint",
            return_value=config["base_url"],
        ), patch(
            "studio.services.provider_registry._request_json",
            return_value={"status": "x" * 11},
        ):
            with self.assertRaises(ProviderDriverError) as captured:
                driver.invoke(deployment, "poll", {"request_id": "request-1"})
        self.assertEqual(captured.exception.code, "provider_response_invalid")

    def test_runtime_budget_must_be_explicit_and_bounded(self):
        for field in ("timeout_seconds", "poll_interval_seconds", "max_poll_seconds"):
            with self.subTest(field=field):
                config = _provider_config()
                config.pop(field)
                with self.assertRaisesMessage(ValidationError, field):
                    RestrictedAsyncRestDriver().validate_config(config)

        config = _provider_config()
        config["max_poll_seconds"] = 10.5
        with self.assertRaisesMessage(ValidationError, "max_poll_seconds"):
            RestrictedAsyncRestDriver().validate_config(config)

    def test_paths_response_fields_and_progress_scale_have_no_hidden_defaults(self):
        for field in (
            "health_path", "submit_path", "poll_path", "cancel_path", "collect_path",
            "request_id_field", "status_field", "artifacts_field", "actual_cost_field",
            "currency_field", "artifact_url_field", "artifact_kind_field",
            "artifact_id_field", "artifact_sha256_field", "idempotency_header",
        ):
            with self.subTest(field=field):
                config = _provider_config()
                config.pop(field)
                with self.assertRaisesMessage(ValidationError, field):
                    RestrictedAsyncRestDriver().validate_config(config)
        config = _provider_config()
        config["progress_field"] = "progress"
        with self.assertRaisesMessage(ValidationError, "progress_max"):
            RestrictedAsyncRestDriver().validate_config(config)

    def test_submit_timeout_is_one_post_and_idempotency_is_header_only(self):
        config = _provider_config()
        provider = SimpleNamespace(driver_config=config, credential=None)
        deployment = SimpleNamespace(
            provider=provider,
            profile_version=SimpleNamespace(provider=provider),
            runtime_config={}, credential=None,
        )
        driver = RestrictedAsyncRestDriver()
        error = ProviderDriverError(
            "timeout", code="provider_timeout", retryable=True, submission_uncertain=True,
        )
        with patch("studio.services.provider_registry._combined_config", return_value=config), patch(
            "studio.services.provider_registry.validate_endpoint", return_value=config["base_url"],
        ), patch(
            "studio.services.provider_registry._request_json", side_effect=error,
        ) as request:
            with self.assertRaises(ProviderDriverError):
                driver.invoke(deployment, "submit", {
                    "schema_version": 1,
                    "idempotency_key": "persisted-key",
                    "model": "cloud-v1",
                    "prompt": "a lighthouse",
                    "width": 64,
                    "height": 64,
                })
        self.assertEqual(request.call_count, 1)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "persisted-key")
        self.assertNotIn("idempotency_key", kwargs["payload"])

    def test_submit_success_without_request_id_is_uncertain(self):
        config = _provider_config()
        provider = SimpleNamespace(driver_config=config, credential=None)
        deployment = SimpleNamespace(
            provider=provider,
            profile_version=SimpleNamespace(provider=provider),
            runtime_config={},
            credential=None,
        )
        with patch(
            "studio.services.provider_registry._combined_config",
            return_value=config,
        ), patch(
            "studio.services.provider_registry.validate_endpoint",
            return_value=config["base_url"],
        ), patch(
            "studio.services.provider_registry._request_json",
            return_value={"status": "queued"},
        ):
            with self.assertRaises(ProviderDriverError) as captured:
                RestrictedAsyncRestDriver().invoke(deployment, "submit", {
                    "schema_version": 1,
                    "idempotency_key": "persisted-key",
                    "model": "cloud-v1",
                    "prompt": "a lighthouse",
                    "width": 64,
                    "height": 64,
                })
        self.assertEqual(captured.exception.code, "provider_response_invalid")
        self.assertTrue(captured.exception.submission_uncertain)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("cdn.example.com",))
class RemoteArtifactSecurityTests(SimpleTestCase):
    def _png(self):
        output = BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
        return output.getvalue()

    def _response(self, body: bytes, *, status=200, headers=None):
        return SimpleNamespace(
            status_code=status,
            headers=headers or {"Content-Length": str(len(body))},
            iter_content=lambda chunk_size: [body],
            close=lambda: None,
        )

    def test_private_dns_and_redirect_are_fail_closed(self):
        config = {
            "artifact_allowed_hosts": ["cdn.example.com"],
            "timeout_seconds": 1,
        }
        with patch("studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 443)),
        ]):
            with self.assertRaises(ProviderDriverError) as private:
                _validated_artifact_url("https://cdn.example.com/result.png", config)
        self.assertEqual(private.exception.code, "artifact_peer_forbidden")

        body = self._png()
        response = self._response(body, status=302, headers={"Location": "https://elsewhere.test/x"})
        with tempfile.TemporaryDirectory() as directory, patch(
            "studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
                (None, None, None, None, ("93.184.216.34", 443)),
            ],
        ), patch("studio.services.remote_artifacts.requests.get", return_value=response), patch(
            "studio.services.remote_artifacts._peer_address", return_value="93.184.216.34",
        ):
            with self.assertRaises(ProviderDriverError) as redirect:
                _download_one(
                    url="https://cdn.example.com/result.png", provider_config=config,
                    target=Path(directory) / "result.media", expected_kind="image",
                    expected_sha256="", max_bytes=1024 * 1024,
                )
        self.assertEqual(redirect.exception.code, "artifact_redirect_forbidden")

    def test_size_mime_hash_and_valid_image(self):
        config = {"artifact_allowed_hosts": ["cdn.example.com"], "timeout_seconds": 1}
        body = self._png()
        import hashlib
        digest = hashlib.sha256(body).hexdigest()
        dns = patch("studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
        ])
        peer = patch("studio.services.remote_artifacts._peer_address", return_value="93.184.216.34")
        with tempfile.TemporaryDirectory() as directory, dns, peer:
            target = Path(directory) / "result.media"
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body),
            ):
                facts = _download_one(
                    url="https://cdn.example.com/result.png", provider_config=config,
                    target=target, expected_kind="image", expected_sha256=digest,
                    max_bytes=1024 * 1024,
                )
            self.assertEqual(facts["sha256"], digest)
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body, headers={"Content-Length": str(len(body))}),
            ):
                with self.assertRaises(ProviderDriverError) as too_large:
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="", max_bytes=1,
                    )
            self.assertEqual(too_large.exception.code, "artifact_too_large")
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body),
            ):
                with self.assertRaises(ProviderDriverError) as mismatch:
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="0" * 64,
                        max_bytes=1024 * 1024,
                    )
            self.assertEqual(mismatch.exception.code, "artifact_checksum_mismatch")
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(b"not an image"),
            ):
                with self.assertRaises(ProviderDriverError):
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="",
                        max_bytes=1024 * 1024,
                    )


class AsyncOperationContractTests(TestCase):
    """Pure model-contract limits; execution behavior is tested on Preparation."""
    def test_prompt_capability_limit_is_explicit_beneath_platform_envelope(self):
        for value, expected in ((None, "maxLength"), (60_001, "60000")):
            with self.subTest(max_length=value):
                specification = deepcopy(_async_spec())
                rule = specification["async_media"]["operations"][
                    "image.text_to_image"
                ]["request_schema"]["properties"]["prompt"]
                if value is None:
                    rule.pop("maxLength")
                else:
                    rule["maxLength"] = value
                profile = ModelProfileVersion(
                    profile_key="async.limit",
                    version=1,
                    provider=ProviderProfile(key="isolated-contract", name="Isolated contract", driver_type="async_rest"),
                    model_identifier="cloud-v1",
                    modality=ModelProfileVersion.Modality.MULTIMODAL,
                    status=ModelProfileVersion.Status.PUBLISHED,
                )
                assign_json_contract(profile, "specification", specification)
                with self.assertRaisesMessage(ValidationError, expected):
                    async_operation_contract(profile, "image.text_to_image")
