from __future__ import annotations

import ipaddress
import hashlib
import math
import re
import socket
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.core.exceptions import ValidationError

from studio.models import ModelDeployment, ProviderProfile, SecretBinding, SecretVersion
from studio.services.json_contracts import find_sensitive_json_keys, verify_json_contract
from studio.services.secret_store import SecretStoreError, resolve_secret
from studio.services.prompt_normalization_contract import (
    PROMPT_NORMALIZE_OPERATION,
    PromptNormalizationContractError,
    parse_prompt_normalization_chat_response,
    prompt_normalization_chat_payload,
)


MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
_DRIVERS: dict[str, type["ProviderDriver"]] = {}


class ProviderDriverError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_driver_error",
        retryable: bool = False,
        submission_uncertain: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.submission_uncertain = submission_uncertain


def _pricing_estimate(
    deployment: ModelDeployment,
    *,
    allow_zero_without_pricing: bool = False,
) -> dict[str, Any]:
    config = _combined_config(deployment)
    pricing = config.get("pricing")
    if pricing is None and allow_zero_without_pricing:
        return {"schema_version": 1, "amount": "0", "currency": "", "basis": "local"}
    if not isinstance(pricing, dict):
        return {"schema_version": 1, "amount": None, "currency": "", "basis": "unknown"}
    unknown = sorted(set(pricing) - {"currency", "fixed_per_request"})
    if unknown:
        raise ProviderDriverError(
            f"pricing 含不支持字段: {', '.join(unknown)}",
            code="provider_pricing_invalid",
        )
    currency = str(pricing.get("currency") or "").strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", currency) is None:
        raise ProviderDriverError("pricing.currency 无效", code="provider_pricing_invalid")
    try:
        amount = Decimal(str(pricing.get("fixed_per_request")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderDriverError(
            "pricing.fixed_per_request 无效",
            code="provider_pricing_invalid",
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise ProviderDriverError(
            "pricing.fixed_per_request 必须是非负有限数",
            code="provider_pricing_invalid",
        )
    return {
        "schema_version": 1,
        "amount": str(amount),
        "currency": currency,
        "basis": "fixed_per_request",
    }


def register_driver(driver_type: str):
    def decorator(cls: type[ProviderDriver]):
        if driver_type in _DRIVERS:
            raise RuntimeError(f"duplicate provider driver: {driver_type}")
        _DRIVERS[driver_type] = cls
        cls.driver_type = driver_type
        return cls
    return decorator


def registered_driver_types() -> tuple[str, ...]:
    return tuple(sorted(_DRIVERS))


def driver_for(profile: ProviderProfile) -> "ProviderDriver":
    try:
        driver_class = _DRIVERS[profile.driver_type]
    except KeyError as exc:
        raise ProviderDriverError(
            f"provider driver 未注册: {profile.driver_type}",
            code="provider_driver_not_registered",
        ) from exc
    validate_provider_profile(profile)
    return driver_class()


def resolve_credential(binding: SecretBinding | None) -> str:
    try:
        return resolve_secret(binding)
    except SecretStoreError as exc:
        raise ProviderDriverError(str(exc), code=exc.code) from exc


def credential_reference_digest(reference: SecretBinding | SecretVersion | None) -> str:
    """Return a stable digest for an internal binding/version identity."""

    if reference is None:
        return ""
    if isinstance(reference, SecretVersion):
        identity = f"version:{reference.id}:{reference.version}"
    else:
        active_id = reference.active_version_id or ""
        identity = f"binding:{reference.id}:{active_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def credential_binding_sha256(deployment: ModelDeployment) -> str:
    """Return a non-reversible identity for a deployment credential binding."""

    return credential_reference_digest(
        deployment.credential or deployment.provider.credential,
    )


def credential_material_sha256(deployment: ModelDeployment) -> str:
    """Keyed fingerprint that also changes when secret material rotates.

    The digest is never returned to ordinary clients. Missing credentials use
    an explicit marker so a previously passed smoke snapshot cannot remain
    current after the deployment secret disappears.
    """

    binding = deployment.credential or deployment.provider.credential
    if binding is None or binding.active_version is None:
        identity = "<credential-unavailable>"
    else:
        version = binding.active_version
        identity = f"{binding.id}:{version.id}:{version.ciphertext_sha256}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _allowed_provider_hosts() -> set[str]:
    configured = getattr(settings, "AI_PROVIDER_ALLOWED_HOSTS", ())
    if isinstance(configured, str):
        configured = configured.split(",")
    return {
        str(item).strip().casefold()
        for item in configured
        if str(item).strip()
    }


def validate_endpoint(value: str, *, resolve_dns: bool = False) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValidationError("provider base_url 必须是无凭据、无 query/fragment 的 http(s) URL")
    allowed_hosts = _allowed_provider_hosts()
    hostname = parsed.hostname.casefold()
    if hostname not in allowed_hosts:
        raise ValidationError("provider hostname 不在部署允许列表中")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(hostname)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(
                    item[4][0]
                    for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
                )
            except OSError as exc:
                raise ProviderDriverError(
                    "provider hostname 无法解析",
                    code="provider_dns_unavailable",
                ) from exc
    if resolve_dns:
        if not addresses:
            raise ProviderDriverError(
                "provider hostname 没有可验证的 DNS 地址",
                code="provider_dns_unavailable",
            )
        local_hostname = hostname in {"localhost", "127.0.0.1", "::1"}
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if local_hostname:
                if not ip.is_loopback:
                    raise ProviderDriverError(
                        "显式 loopback provider 解析到了非 loopback 地址",
                        code="provider_loopback_mismatch",
                    )
                continue
            if any((
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )):
                raise ProviderDriverError(
                    "公网 provider 解析到了禁止的内网或保留地址",
                    code="provider_private_address_forbidden",
                )
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValidationError("公网 provider 必须使用 HTTPS")
    return endpoint


def _validate_relative_path(value: str, *, field: str, allow_placeholder: bool = False) -> str:
    path = str(value or "").strip()
    first_segment = path.lstrip("/").split("/", 1)[0]
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or ":" in first_segment
        or ".." in path
        or "?" in path
        or "#" in path
    ):
        raise ValidationError({field: "必须是无 query/fragment/目录穿越的绝对 URL path"})
    if "{" in path or "}" in path:
        if not allow_placeholder or path.count("{request_id}") != 1:
            raise ValidationError({field: "只允许一个 {request_id} 占位符"})
    return path


def _endpoint_url(base_url: str, path: str) -> str:
    """Join one validated path without inheriting ``urljoin`` URL semantics."""

    base = str(base_url or "").rstrip("/")
    target = f"{base}{path}"
    base_parsed = urlparse(base)
    target_parsed = urlparse(target)
    if (
        target_parsed.scheme != base_parsed.scheme
        or target_parsed.hostname != base_parsed.hostname
        or target_parsed.port != base_parsed.port
        or target_parsed.username
        or target_parsed.password
    ):
        raise ProviderDriverError(
            "provider operation path 改变了目标 origin",
            code="provider_path_origin_mismatch",
        )
    return target


def _response_json(response: requests.Response) -> dict[str, Any]:
    raw_length = str(response.headers.get("Content-Length") or "").strip()
    if raw_length:
        try:
            if int(raw_length) > MAX_PROVIDER_RESPONSE_BYTES:
                response.close()
                raise ProviderDriverError(
                    "provider JSON 响应超过 1MiB 上限",
                    code="provider_response_too_large",
                )
        except ValueError as exc:
            response.close()
            raise ProviderDriverError(
                "provider Content-Length 无效",
                code="provider_response_invalid",
            ) from exc
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_PROVIDER_RESPONSE_BYTES:
                raise ProviderDriverError(
                    "provider JSON 响应超过 1MiB 上限",
                    code="provider_response_too_large",
                )
            chunks.append(chunk)
    finally:
        response.close()
    content = b"".join(chunks)
    try:
        payload = requests.models.complexjson.loads(content.decode(response.encoding or "utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderDriverError("provider 未返回 JSON", code="provider_response_invalid") from exc
    if not isinstance(payload, dict):
        raise ProviderDriverError("provider JSON 响应必须是对象", code="provider_response_invalid")
    return payload


def _resolved_endpoint_addresses(url: str) -> set[str]:
    parsed = urlparse(url)
    try:
        return {
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ProviderDriverError("provider hostname 无法解析", code="provider_dns_unavailable") from exc


def _peer_address(response: requests.Response) -> str:
    candidates = (
        getattr(getattr(getattr(response, "raw", None), "_connection", None), "sock", None),
        getattr(getattr(getattr(getattr(response, "raw", None), "_fp", None), "fp", None), "raw", None),
    )
    for candidate in candidates:
        sock = candidate
        if sock is not None and not hasattr(sock, "getpeername"):
            sock = getattr(sock, "_sock", None)
        if sock is not None and hasattr(sock, "getpeername"):
            try:
                return str(ipaddress.ip_address(sock.getpeername()[0]))
            except (OSError, ValueError, TypeError):
                continue
    return ""


def _request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    headers=None,
    payload=None,
    submission: bool = False,
) -> dict[str, Any]:
    validate_endpoint(f"{urlparse(url).scheme}://{urlparse(url).netloc}", resolve_dns=True)
    resolved_addresses = _resolved_endpoint_addresses(url)
    try:
        response = requests.request(
            method,
            url,
            json=payload,
            headers=headers or {},
            timeout=timeout,
            allow_redirects=False,
            # Keep the urllib3 connection attached until its peer address has
            # been verified.  With Requests' default stream=False the body may
            # be consumed and the socket returned to the pool before this
            # security check runs.
            stream=True,
        )
        peer = _peer_address(response)
        if not peer:
            response.close()
            raise ProviderDriverError(
                "无法验证 provider 对端地址",
                code="provider_peer_unverifiable",
                submission_uncertain=submission,
            )
        if peer not in resolved_addresses:
            response.close()
            raise ProviderDriverError(
                "provider 连接地址与受控 DNS 解析不一致",
                code="provider_peer_mismatch",
                submission_uncertain=submission,
            )
        if 300 <= response.status_code < 400:
            response.close()
            raise ProviderDriverError(
                "provider 响应禁止重定向",
                code="provider_redirect_forbidden",
                submission_uncertain=submission,
            )
        if response.status_code >= 400:
            response.close()
            if response.status_code == 429:
                raise ProviderDriverError(
                    "provider 请求被限流",
                    code="provider_rate_limited",
                    retryable=True,
                    submission_uncertain=submission,
                )
            if response.status_code >= 500 or response.status_code in {408, 409, 425}:
                raise ProviderDriverError(
                    "provider 暂时无法完成请求",
                    code="provider_request_temporary_failure",
                    retryable=True,
                    submission_uncertain=submission,
                )
            raise ProviderDriverError(
                "provider 拒绝请求",
                code="provider_request_rejected",
            )
    except ProviderDriverError:
        raise
    except requests.RequestException as exc:
        raise ProviderDriverError(
            "provider 请求失败",
            code="provider_request_failed",
            retryable=not submission,
            submission_uncertain=submission,
        ) from exc
    try:
        return _response_json(response)
    except ProviderDriverError as exc:
        if not submission:
            raise
        raise ProviderDriverError(
            str(exc),
            code=exc.code,
            retryable=exc.retryable,
            submission_uncertain=True,
        ) from exc


def _combined_config(deployment: ModelDeployment) -> dict[str, Any]:
    profile = deployment.profile_version.provider
    validate_provider_profile(profile)
    verify_json_contract(
        deployment.runtime_config,
        schema_version=deployment.runtime_config_schema_version,
        size_bytes=deployment.runtime_config_size_bytes,
        sha256=deployment.runtime_config_sha256,
    )
    sensitive = find_sensitive_json_keys(deployment.runtime_config)
    if sensitive:
        raise ProviderDriverError(
            f"runtime_config 含敏感字段: {', '.join(sensitive)}",
            code="provider_plaintext_secret_forbidden",
        )
    return {**profile.driver_config, **deployment.runtime_config}


def validate_provider_profile(profile: ProviderProfile) -> None:
    verify_json_contract(
        profile.driver_config,
        schema_version=profile.driver_config_schema_version,
        size_bytes=profile.driver_config_size_bytes,
        sha256=profile.driver_config_sha256,
    )
    sensitive = find_sensitive_json_keys(profile.driver_config)
    if sensitive:
        raise ValidationError({"driver_config": f"不能保存密钥字段: {', '.join(sensitive)}"})
    try:
        driver_class = _DRIVERS[profile.driver_type]
    except KeyError as exc:
        raise ValidationError({"driver_type": "未注册的 provider driver"}) from exc
    driver_class().validate_config(profile.driver_config)


class ProviderDriver(ABC):
    driver_type = ""
    allowed_config_keys: frozenset[str] = frozenset()

    def validate_config(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValidationError("provider config 必须是 JSON 对象")
        unknown = sorted(set(config) - self.allowed_config_keys)
        if unknown:
            raise ValidationError({"driver_config": f"不支持字段: {', '.join(unknown)}"})

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def estimate(
        self,
        deployment: ModelDeployment,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return _pricing_estimate(deployment)

    @abstractmethod
    def invoke(
        self,
        deployment: ModelDeployment,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _invoke_chat_completion(
    deployment: ModelDeployment,
    payload: dict[str, Any],
    *,
    credential_optional: bool,
) -> dict[str, Any]:
    """One bounded OpenAI-compatible transport using deployment-owned config."""

    config = _combined_config(deployment)
    base_url = validate_endpoint(config.get("base_url"), resolve_dns=True)
    chat_path = _validate_relative_path(
        config.get("chat_path", "/v1/chat/completions"), field="chat_path",
    )
    timeout = float(config.get("timeout_seconds", 60))
    if not 0.5 <= timeout <= 600:
        raise ProviderDriverError("timeout_seconds 无效", code="provider_config_invalid")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProviderDriverError("messages 必须是非空数组", code="provider_payload_invalid")
    headers = {"Content-Type": "application/json"}
    credential = deployment.credential or deployment.provider.credential
    hostname = (urlparse(base_url).hostname or "").casefold()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if credential:
        headers["Authorization"] = f"Bearer {resolve_credential(credential)}"
    elif not (credential_optional and local):
        raise ProviderDriverError("模型部署缺少受管凭据", code="credential_missing")
    body = {
        "model": deployment.profile_version.model_identifier,
        "messages": messages,
    }
    for key in ("temperature", "max_tokens", "response_format", "seed"):
        if key in payload:
            body[key] = payload[key]
    response = _request_json(
        "POST", _endpoint_url(base_url, chat_path),
        timeout=timeout, headers=headers, payload=body, submission=True,
    )
    choices = response.get("choices")
    text = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            text = str(message.get("content") or "")
    if not text:
        error = ProviderDriverError(
            "OpenAI-compatible 响应缺少文本",
            code="provider_response_invalid",
        )
        error.metadata = {"contract_error_code": "response_missing_text"}
        raise error
    return {
        "schema_version": 1,
        "request_id": str(response.get("id") or "")[:191],
        "text": text,
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
    }


def _invoke_prompt_normalization(
    deployment: ModelDeployment,
    payload: dict[str, Any],
    *,
    credential_optional: bool,
) -> dict[str, Any]:
    try:
        chat_payload = prompt_normalization_chat_payload(payload)
        chat_response = _invoke_chat_completion(
            deployment,
            chat_payload,
            credential_optional=credential_optional,
        )
        result = parse_prompt_normalization_chat_response(payload, chat_response)
    except PromptNormalizationContractError as exc:
        error = ProviderDriverError(
            str(exc),
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        error.metadata = {
            "contract_error_code": str(
                getattr(exc, "code", "prompt_normalize_contract_invalid")
            ),
        }
        raise error from exc
    result["provider"] = deployment.provider.key
    result["model"] = deployment.profile_version.model_identifier
    result["model_deployment_id"] = str(deployment.id)
    return result


def deployment_supports_script_generation(deployment: ModelDeployment) -> bool:
    driver_type = deployment.profile_version.provider.driver_type
    config = _combined_config(deployment)
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return False
    try:
        validate_endpoint(base_url)
        _validate_relative_path(
            config.get("chat_path", "/v1/chat/completions"),
            field="chat_path",
        )
        timeout = float(config.get("timeout_seconds", 60))
        if not 0.5 <= timeout <= 600:
            return False
    except (ProviderDriverError, ValidationError, TypeError, ValueError):
        return False
    if driver_type == ProviderProfile.DriverType.OPENAI_COMPATIBLE:
        return True
    if driver_type in {
        ProviderProfile.DriverType.LOCAL_QWEN,
        ProviderProfile.DriverType.DEEPSEEK_API,
    }:
        return bool(
            config.get("runtime_source") == "openai_compatible_endpoint"
            and base_url
        )
    return False


class _LegacyTextDriver(ProviderDriver):
    provider_id = ""
    allowed_config_keys = frozenset({"runtime_source", "base_url", "pricing"})

    def validate_config(self, config):
        super().validate_config(config)
        if config.get("runtime_source") != "openai_compatible_endpoint":
            return
        validate_endpoint(config.get("base_url"))
        _validate_relative_path(
            config.get("chat_path", "/v1/chat/completions"),
            field="chat_path",
        )
        timeout = float(config.get("timeout_seconds", 60))
        if not 0.5 <= timeout <= 600:
            raise ValidationError({"timeout_seconds": "必须在 0.5 到 600 秒之间"})

    def estimate(self, deployment, operation, payload):
        config = _combined_config(deployment)
        hostname = (
            urlparse(str(config.get("base_url") or "")).hostname or ""
        ).casefold()
        return _pricing_estimate(
            deployment,
            allow_zero_without_pricing=(
                self.provider_id == "local_qwen"
                and hostname in {"localhost", "127.0.0.1", "::1"}
            ),
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "modality": "text",
            "operations": [],
            "legacy_settings_bridge": True,
        }

    def invoke(self, deployment, operation, payload):
        _combined_config(deployment)
        raise ProviderDriverError(
            f"legacy text driver 尚未为 {operation} 建立统一调用契约",
            code="provider_operation_not_supported",
        )


@register_driver(ProviderProfile.DriverType.LOCAL_QWEN)
class LocalQwenDriver(_LegacyTextDriver):
    provider_id = "local_qwen"

    allowed_config_keys = frozenset({
        "runtime_source", "base_url", "chat_path", "timeout_seconds", "pricing",
    })

    def capabilities(self):
        return {
            "schema_version": 1,
            "modality": "text",
            "operations": [PROMPT_NORMALIZE_OPERATION, "chat_completion"],
            "deployment_runtime_required": "openai_compatible_endpoint",
        }

    def invoke(self, deployment, operation, payload):
        config = _combined_config(deployment)
        if config.get("runtime_source") == "openai_compatible_endpoint":
            if operation == "chat_completion":
                return _invoke_chat_completion(deployment, payload, credential_optional=True)
            if operation == PROMPT_NORMALIZE_OPERATION:
                return _invoke_prompt_normalization(
                    deployment, payload, credential_optional=True,
                )
        if operation == PROMPT_NORMALIZE_OPERATION:
            raise ProviderDriverError(
                "local_qwen deployment 缺少受控 OpenAI-compatible endpoint；禁止回退 legacy settings",
                code="provider_operation_not_supported",
            )
        return super().invoke(deployment, operation, payload)


@register_driver(ProviderProfile.DriverType.DEEPSEEK_API)
class DeepSeekDriver(_LegacyTextDriver):
    provider_id = "deepseek_api"

    allowed_config_keys = frozenset({
        "runtime_source", "base_url", "chat_path", "timeout_seconds", "pricing",
    })

    def capabilities(self):
        return {
            "schema_version": 1,
            "modality": "text",
            "operations": [PROMPT_NORMALIZE_OPERATION, "chat_completion"],
            "deployment_runtime_required": "openai_compatible_endpoint",
        }

    def invoke(self, deployment, operation, payload):
        config = _combined_config(deployment)
        if config.get("runtime_source") == "openai_compatible_endpoint":
            if operation == "chat_completion":
                return _invoke_chat_completion(deployment, payload, credential_optional=False)
            if operation == PROMPT_NORMALIZE_OPERATION:
                return _invoke_prompt_normalization(
                    deployment, payload, credential_optional=False,
                )
        if operation == PROMPT_NORMALIZE_OPERATION:
            raise ProviderDriverError(
                "deepseek deployment 缺少受控 OpenAI-compatible endpoint；禁止回退 legacy settings",
                code="provider_operation_not_supported",
            )
        return super().invoke(deployment, operation, payload)


@register_driver(ProviderProfile.DriverType.COMFYUI)
class ComfyUIDriver(ProviderDriver):
    allowed_config_keys = frozenset({"runtime_source", "base_url", "timeout_seconds", "pricing"})

    def validate_config(self, config):
        super().validate_config(config)
        if config.get("base_url"):
            validate_endpoint(config["base_url"])
        if "timeout_seconds" in config and not 0.5 <= float(config["timeout_seconds"]) <= 3600:
            raise ValidationError({"timeout_seconds": "必须在 0.5 到 3600 秒之间"})

    def capabilities(self):
        return {
            "schema_version": 1,
            "modalities": ["image", "video"],
            "operations": ["health", "object_info", "models", "upload", "submit", "poll", "cancel", "collect"],
        }

    def estimate(self, deployment, operation, payload):
        config = _combined_config(deployment)
        endpoint = urlparse(str(config.get("base_url") or ""))
        local = (endpoint.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        return _pricing_estimate(deployment, allow_zero_without_pricing=local)

    def invoke(self, deployment, operation, payload):
        from studio.services.comfyui import ComfyUIClient, ComfyUIError

        config = _combined_config(deployment)
        try:
            endpoint = validate_endpoint(config.get("base_url"), resolve_dns=True)
            client = ComfyUIClient(
                base_url=endpoint,
                timeout=float(config.get("timeout_seconds") or 0) or None,
                endpoint_prevalidated=True,
            )
            if operation == "health":
                return client.health()
            if operation == "object_info":
                return client.object_info()
            if operation == "models":
                folder = str(payload.get("folder") or "")
                return {"schema_version": 1, "folder": folder, "items": client.models(folder)}
            if operation == "upload":
                source_path = payload.get("source_path")
                if not isinstance(source_path, (str, Path)):
                    raise ProviderDriverError(
                        "upload 必须提供服务端已授权 source_path",
                        code="provider_payload_invalid",
                    )
                result = client.upload_input_file(
                    source_path,
                    subfolder=str(payload.get("subfolder") or "autoprompt/smoke"),
                    filename=str(payload.get("filename") or "") or None,
                )
                return {
                    "schema_version": 1,
                    "name": str(result.get("name") or ""),
                    "subfolder": str(result.get("subfolder") or ""),
                    "type": str(result.get("type") or "input"),
                }
            if operation == "submit":
                workflow = payload.get("workflow")
                if not isinstance(workflow, dict):
                    raise ProviderDriverError(
                        "submit 必须提供 workflow 对象",
                        code="provider_payload_invalid",
                    )
                result = client.queue_prompt(
                    workflow,
                    client_id=str(payload.get("client_id") or "") or None,
                    prompt_id=str(payload.get("submission_token") or "") or None,
                )
                return {
                    "schema_version": 1,
                    "request_id": result.prompt_id,
                    "number": result.number,
                    "node_errors": result.node_errors,
                }
            if operation in {"poll", "cancel", "collect"}:
                request_id = str(payload.get("request_id") or "").strip()
                if not request_id or len(request_id) > 191:
                    raise ProviderDriverError(
                        "request_id 无效",
                        code="provider_payload_invalid",
                    )
                if operation == "cancel":
                    client.cancel(request_id)
                    return {"schema_version": 1, "request_id": request_id, "status": "cancel_requested"}
                history = client.history(request_id)
                if history is None:
                    return {"schema_version": 1, "request_id": request_id, "status": "running"}
                remote_status = history.get("status") if isinstance(history, dict) else {}
                failed = bool(
                    isinstance(remote_status, dict)
                    and (
                        remote_status.get("status_str") == "error"
                        or remote_status.get("completed") is False
                    )
                )
                completed = bool(
                    isinstance(remote_status, dict)
                    and remote_status.get("completed") is True
                    and not failed
                )
                if operation == "poll":
                    return {
                        "schema_version": 1,
                        "request_id": request_id,
                        "status": "failed" if failed else "succeeded" if completed else "running",
                    }
                if failed or not completed:
                    raise ProviderDriverError(
                        "远端任务尚未成功，不能 collect",
                        code="provider_collect_not_ready",
                    )
                outputs = client.extract_outputs(history)
                if not isinstance(outputs, list) or not outputs:
                    raise ProviderDriverError(
                        "烟测没有返回任何输出",
                        code="provider_smoke_output_missing",
                    )
                return {
                    "schema_version": 1,
                    "request_id": request_id,
                    "status": "succeeded",
                    "outputs": outputs,
                }
        except ProviderDriverError:
            raise
        except (ComfyUIError, ValidationError, ValueError) as exc:
            raise ProviderDriverError(
                "ComfyUI operation 失败",
                code="comfyui_operation_failed",
            ) from exc
        raise ProviderDriverError("ComfyUI operation 不受支持", code="provider_operation_not_supported")


@register_driver(ProviderProfile.DriverType.OPENAI_COMPATIBLE)
class OpenAICompatibleDriver(ProviderDriver):
    allowed_config_keys = frozenset({"base_url", "chat_path", "timeout_seconds", "pricing"})

    def validate_config(self, config):
        super().validate_config(config)
        validate_endpoint(config.get("base_url"))
        _validate_relative_path(config.get("chat_path", "/v1/chat/completions"), field="chat_path")
        timeout = float(config.get("timeout_seconds", 60))
        if not 0.5 <= timeout <= 600:
            raise ValidationError({"timeout_seconds": "必须在 0.5 到 600 秒之间"})

    def capabilities(self):
        return {
            "schema_version": 1,
            "modality": "text",
            "operations": [PROMPT_NORMALIZE_OPERATION, "chat_completion"],
        }

    def invoke(self, deployment, operation, payload):
        if operation == "chat_completion":
            return _invoke_chat_completion(deployment, payload, credential_optional=False)
        if operation == PROMPT_NORMALIZE_OPERATION:
            return _invoke_prompt_normalization(
                deployment, payload, credential_optional=False,
            )
        raise ProviderDriverError(
            "仅支持受控文本能力",
            code="provider_operation_not_supported",
        )


@register_driver(ProviderProfile.DriverType.ASYNC_REST)
class RestrictedAsyncRestDriver(ProviderDriver):
    STANDARD_REQUEST_FIELDS = frozenset({
        "model", "prompt", "negative_prompt", "width", "height",
        "duration_seconds", "fps", "seed", "audio",
    })
    allowed_config_keys = frozenset({
        "base_url", "health_path", "submit_path", "poll_path", "cancel_path", "collect_path",
        "timeout_seconds", "request_id_field", "status_field", "terminal_success",
        "terminal_failure", "terminal_cancelled", "artifacts_field", "actual_cost_field", "currency_field",
        "artifact_url_field", "artifact_kind_field", "artifact_id_field",
        "artifact_sha256_field", "artifact_allowed_hosts", "request_field_map",
        "idempotency_header", "poll_interval_seconds", "max_poll_seconds",
        "progress_field", "progress_max",
        "capability_ttl_seconds", "pricing",
    })

    def validate_config(self, config):
        super().validate_config(config)
        validate_endpoint(config.get("base_url"))
        _validate_relative_path(config.get("health_path", "/health"), field="health_path")
        _validate_relative_path(config.get("submit_path", "/jobs"), field="submit_path")
        _validate_relative_path(
            config.get("poll_path", "/jobs/{request_id}"),
            field="poll_path",
            allow_placeholder=True,
        )
        _validate_relative_path(
            config.get("cancel_path", "/jobs/{request_id}"),
            field="cancel_path",
            allow_placeholder=True,
        )
        _validate_relative_path(
            config.get("collect_path", "/jobs/{request_id}"),
            field="collect_path",
            allow_placeholder=True,
        )
        for field in ("request_id_field", "status_field"):
            value = str(config.get(field) or ("id" if field == "request_id_field" else "status"))
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None:
                raise ValidationError({field: "只允许单层 JSON 字段名"})
        progress_field = str(config.get("progress_field") or "").strip()
        if progress_field and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", progress_field) is None:
            raise ValidationError({"progress_field": "只允许单层 JSON 字段名"})
        if progress_field:
            try:
                progress_max = float(config.get("progress_max", 100))
            except (TypeError, ValueError) as exc:
                raise ValidationError({"progress_max": "必须是正数"}) from exc
            if not math.isfinite(progress_max) or progress_max <= 0:
                raise ValidationError({"progress_max": "必须是有限正数"})
        for field, fallback in (
            ("artifacts_field", "artifacts"),
            ("actual_cost_field", "actual_cost"),
            ("currency_field", "currency"),
            ("artifact_url_field", "url"),
            ("artifact_kind_field", "kind"),
            ("artifact_id_field", "id"),
            ("artifact_sha256_field", "sha256"),
        ):
            value = str(config.get(field) or fallback)
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None:
                raise ValidationError({field: "只允许单层 JSON 字段名"})
        header = str(config.get("idempotency_header") or "Idempotency-Key")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header) is None:
            raise ValidationError({"idempotency_header": "HTTP header 名无效"})
        timeout = float(config.get("timeout_seconds", 60))
        if not 0.5 <= timeout <= 600:
            raise ValidationError({"timeout_seconds": "必须在 0.5 到 600 秒之间"})
        poll_interval = float(config.get("poll_interval_seconds", 2))
        if not 0.25 <= poll_interval <= 300:
            raise ValidationError({"poll_interval_seconds": "必须在 0.25 到 300 秒之间"})
        max_poll = int(config.get("max_poll_seconds", 3600))
        if not 10 <= max_poll <= 604800:
            raise ValidationError({"max_poll_seconds": "必须在 10 到 604800 秒之间"})
        capability_ttl = int(config.get("capability_ttl_seconds", 3600))
        if not 60 <= capability_ttl <= 604800:
            raise ValidationError({"capability_ttl_seconds": "必须在 60 到 604800 秒之间"})
        terminal_success = config.get("terminal_success")
        terminal_failure = config.get("terminal_failure")
        for field, values in (
            ("terminal_success", terminal_success),
            ("terminal_failure", terminal_failure),
            ("terminal_cancelled", config.get("terminal_cancelled")),
        ):
            if (
                not isinstance(values, list)
                or not values
                or len(values) > 32
                or any(not isinstance(item, str) or not item or len(item) > 120 for item in values)
            ):
                raise ValidationError({field: "必须为 1 到 32 个非空状态字符串"})
        terminal_cancelled = config["terminal_cancelled"]
        if (
            set(terminal_success) & set(terminal_failure)
            or set(terminal_success) & set(terminal_cancelled)
            or set(terminal_failure) & set(terminal_cancelled)
        ):
            raise ValidationError({"terminal_failure": "成功、失败与取消终态不能重叠"})
        mapping = config.get("request_field_map")
        if not isinstance(mapping, dict):
            raise ValidationError({"request_field_map": "必须是标准语义到单层字段名的对象"})
        unknown = sorted(set(mapping) - self.STANDARD_REQUEST_FIELDS)
        if unknown:
            raise ValidationError({"request_field_map": f"不支持语义: {', '.join(unknown)}"})
        if not {"model", "prompt"} <= set(mapping):
            raise ValidationError({"request_field_map": "必须映射 model 和 prompt"})
        targets = []
        for semantic, target in mapping.items():
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(target or "")) is None:
                raise ValidationError({"request_field_map": f"{semantic} 的目标字段名无效"})
            targets.append(str(target))
        if len(targets) != len(set(targets)):
            raise ValidationError({"request_field_map": "目标字段名不能重复"})
        artifact_hosts = config.get("artifact_allowed_hosts")
        if (
            not isinstance(artifact_hosts, list)
            or not artifact_hosts
            or len(artifact_hosts) > 32
        ):
            raise ValidationError({"artifact_allowed_hosts": "必须声明 1 到 32 个产物主机"})
        global_hosts = _allowed_provider_hosts()
        for host in artifact_hosts:
            normalized = str(host or "").strip().casefold()
            if (
                not normalized
                or normalized != host
                or normalized not in global_hosts
                or "/" in normalized
                or ":" in normalized
            ):
                raise ValidationError({
                    "artifact_allowed_hosts": "产物主机必须是全局 Provider 白名单的规范化子集",
                })

    def capabilities(self):
        return {
            "schema_version": 1,
            "operations": ["health", "submit", "poll", "cancel", "collect"],
            "transport": "restricted_async_rest",
        }

    def invoke(self, deployment, operation, payload):
        config = _combined_config(deployment)
        self.validate_config(config)
        base_url = validate_endpoint(config["base_url"], resolve_dns=True)
        timeout = float(config.get("timeout_seconds", 60))
        credential = deployment.credential or deployment.provider.credential
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {resolve_credential(credential)}"
        if operation == "health":
            response = _request_json(
                "GET",
                _endpoint_url(base_url, config.get("health_path", "/health")),
                timeout=timeout,
                headers=headers,
            )
            return {"schema_version": 1, "status": "available", "response_sha256": hashlib.sha256(
                repr(sorted(response)).encode("utf-8")
            ).hexdigest()}
        if operation == "submit":
            idempotency_key = str(payload.get("idempotency_key") or "").strip()
            if not idempotency_key or len(idempotency_key) > 191:
                raise ProviderDriverError(
                    "异步 submit 必须提供持久化 idempotency_key",
                    code="provider_payload_invalid",
                )
            headers[str(config.get("idempotency_header") or "Idempotency-Key")] = idempotency_key
            allowed_payload = self.STANDARD_REQUEST_FIELDS | {"schema_version", "idempotency_key"}
            unknown_payload = sorted(set(payload) - allowed_payload)
            if unknown_payload:
                raise ProviderDriverError(
                    f"异步 submit 含未授权字段: {', '.join(unknown_payload)}",
                    code="provider_payload_invalid",
                )
            field_map = config["request_field_map"]
            body = {
                target: payload[semantic]
                for semantic, target in field_map.items()
                if semantic in payload and payload[semantic] is not None
            }
            if not body.get(field_map["model"]) or not body.get(field_map["prompt"]):
                raise ProviderDriverError(
                    "异步 submit 缺少 model/prompt",
                    code="provider_payload_invalid",
                )
            path = config.get("submit_path", "/jobs")
            response = _request_json(
                "POST", _endpoint_url(base_url, path),
                timeout=timeout, headers=headers, payload=body, submission=True,
            )
            request_id = response.get(config.get("request_id_field", "id"))
            if not isinstance(request_id, (str, int)) or not str(request_id):
                raise ProviderDriverError(
                    "异步 REST 响应缺少 request id",
                    code="provider_response_invalid",
                    submission_uncertain=True,
                )
            return {"schema_version": 1, "request_id": str(request_id), "status": "submitted"}
        if operation not in {"poll", "cancel", "collect"}:
            raise ProviderDriverError("异步 REST operation 不受支持", code="provider_operation_not_supported")
        request_id = str(payload.get("request_id") or "")
        if not request_id or len(request_id) > 191:
            raise ProviderDriverError("request_id 无效", code="provider_payload_invalid")
        path_key = (
            "poll_path" if operation == "poll"
            else "cancel_path" if operation == "cancel"
            else "collect_path"
        )
        path = config.get(path_key, "/jobs/{request_id}").replace(
            "{request_id}", quote(request_id, safe=""),
        )
        response = _request_json(
            "GET" if operation in {"poll", "collect"} else "POST",
            _endpoint_url(base_url, path),
            timeout=timeout,
            headers=headers,
            payload=None,
        )
        remote_status = str(response.get(config.get("status_field", "status")) or "")
        result = {
            "schema_version": 1,
            "request_id": request_id,
            "status": remote_status,
            "terminal_success": remote_status in set(config.get("terminal_success") or []),
            "terminal_failure": remote_status in set(config.get("terminal_failure") or []),
            "terminal_cancelled": remote_status in set(config.get("terminal_cancelled") or []),
        }
        progress_field = str(config.get("progress_field") or "").strip()
        if operation == "poll" and progress_field and response.get(progress_field) is not None:
            try:
                raw_progress = float(response[progress_field])
                progress_max = float(config.get("progress_max", 100))
            except (TypeError, ValueError) as exc:
                raise ProviderDriverError(
                    "异步 REST progress 字段无效",
                    code="provider_response_invalid",
                ) from exc
            if not math.isfinite(raw_progress) or raw_progress < 0 or raw_progress > progress_max:
                raise ProviderDriverError(
                    "异步 REST progress 超出声明范围",
                    code="provider_response_invalid",
                )
            result["progress_fraction"] = raw_progress / progress_max
        if operation == "collect":
            artifacts = response.get(config.get("artifacts_field", "artifacts"))
            if not isinstance(artifacts, list) or not artifacts:
                raise ProviderDriverError(
                    "异步 REST collect 缺少 artifacts",
                    code="provider_response_invalid",
                )
            normalized_artifacts = []
            for index, artifact in enumerate(artifacts):
                if not isinstance(artifact, dict):
                    raise ProviderDriverError(
                        "异步 REST artifact 必须是对象",
                        code="provider_response_invalid",
                    )
                url = artifact.get(config.get("artifact_url_field", "url"))
                kind = str(artifact.get(config.get("artifact_kind_field", "kind")) or "")
                if not isinstance(url, str) or not url or kind not in {"image", "video"}:
                    raise ProviderDriverError(
                        f"异步 REST artifact[{index}] 缺少受支持的 url/kind",
                        code="provider_response_invalid",
                    )
                normalized_artifacts.append({
                    "url": url,
                    "kind": kind,
                    "artifact_id": str(artifact.get(config.get("artifact_id_field", "id")) or "")[:191],
                    "sha256": str(artifact.get(config.get("artifact_sha256_field", "sha256")) or "").casefold(),
                })
            result["artifacts"] = normalized_artifacts
            result["actual_cost"] = response.get(config.get("actual_cost_field", "actual_cost"))
            result["currency"] = str(response.get(config.get("currency_field", "currency")) or "")
        return result
