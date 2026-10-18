from __future__ import annotations

import ipaddress
import hashlib
import math
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from studio.models import ModelDeployment, ProviderProfile, SecretBinding, SecretVersion
from studio.services.async_media_schema import STANDARD_MEDIA_FIELDS
from studio.services.json_contracts import (
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.comfyui_workflow_library import saved_workflow_directory
from studio.services.secret_store import SecretStoreError, resolve_secret
from studio.services.security_boundaries import boundary_int, boundary_number, boundary_values
from studio.services.prompt_normalization_contract import (
    PROMPT_NORMALIZE_OPERATION,
    PromptNormalizationContractError,
    parse_prompt_normalization_chat_response,
    prompt_normalization_input_warnings,
    prompt_normalization_policy_from_specification,
    prompt_normalization_chat_payload,
    safe_prompt_field_validation_context,
    safe_prompt_field_validation_reason_code,
)


SAFE_CHAT_FINISH_REASONS = frozenset({
    "content_filter",
    "function_call",
    "length",
    "stop",
    "tool_calls",
})
SAFE_CHAT_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
SAFE_TRANSPORT_PHASES = frozenset({"connect", "request", "response_body", "response_headers"})
SAFE_TRANSPORT_EXCEPTIONS = frozenset({"ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "ChunkedEncodingError", "ContentDecodingError", "Timeout", "RequestException"})
SAFE_CHAT_RESPONSE_ERROR_CODES = frozenset({
    "response_output_budget_exhausted", "response_content_filtered", "response_missing_text",
    "response_tool_call_without_text", "response_reasoning_without_text",
    "request_text_capacity_rejected",
})
_DRIVERS: dict[str, type["ProviderDriver"]] = {}
COMFYUI_WORKFLOW_SOURCE_SCOPE_SCHEMA_VERSION = 2

_ASYNC_REST_TERMINAL_STATUS_MAX_ITEMS_KEY = (
    "security.provider.async_rest_terminal_status_max_items"
)
_ASYNC_REST_TERMINAL_STATUS_MAX_CHARACTERS_KEY = (
    "security.provider.async_rest_terminal_status_max_characters"
)
_ASYNC_REST_ARTIFACT_HOST_MAX_ITEMS_KEY = (
    "security.provider.async_rest_artifact_host_max_items"
)


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def safe_chat_usage(value: Any) -> dict[str, int]:
    """Allowlisted provider token counts, not a cost estimate or billing fact."""
    maximum_count = boundary_int("security.prompt.max_usage_count")
    if not isinstance(value, dict):
        return {}
    return {
        field: raw
        for field in SAFE_CHAT_USAGE_FIELDS
        if type((raw := value.get(field))) is int
        and 0 <= raw <= maximum_count
    }


def safe_chat_failure_details(error: ProviderDriverError) -> dict[str, Any]:
    """Bounded transport diagnostics, never response prose or model policy.

    All prepared text consumers use this projection before persisting an
    exception. Unknown keys/codes are not evidence and cannot be copied into
    audit records, even when an adapter attaches an arbitrary metadata object.
    """
    metadata = error.metadata
    if not isinstance(metadata, dict):
        return {}
    result: dict[str, Any] = {}
    code, reason = metadata.get("contract_error_code"), metadata.get("finish_reason")
    if isinstance(code, str) and code in SAFE_CHAT_RESPONSE_ERROR_CODES:
        result["contract_error_code"] = code
    if isinstance(reason, str) and reason in SAFE_CHAT_FINISH_REASONS:
        result["finish_reason"] = reason
    usage = safe_chat_usage(metadata.get("usage"))
    if usage:
        result["usage"] = usage
    digest = metadata.get("response_id_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
        result["response_id_sha256"] = digest
    for key, allowed in (("transport_phase", SAFE_TRANSPORT_PHASES), ("exception_kind", SAFE_TRANSPORT_EXCEPTIONS)):
        value = metadata.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    status = metadata.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    return result


def safe_chat_failure_message(error: ProviderDriverError) -> str:
    """Persist bounded causes instead of dropping them or copying network URLs."""
    labels = {
        "provider_request_failed": "连接或传输失败", "provider_rate_limited": "服务限流",
        "provider_request_temporary_failure": "服务暂时不可用", "provider_request_rejected": "服务拒绝请求",
        "provider_peer_unverifiable": "无法核验服务对端", "provider_peer_mismatch": "服务对端不匹配",
        "provider_redirect_forbidden": "服务返回禁止的重定向", "provider_response_invalid": "响应格式无效",
        "provider_response_too_large": "响应超出大小限制", "provider_text_capacity_rejected": "服务拒绝文本容量参数",
        "provider_dns_unavailable": "服务域名解析失败",
        "provider_private_address_forbidden": "服务地址不符合网络访问规则",
        "provider_loopback_mismatch": "本机服务地址与绑定不一致",
        "provider_path_origin_mismatch": "请求路径与服务来源不一致",
        "provider_plaintext_secret_forbidden": "不允许通过明文连接发送凭据",
        "provider_config_invalid": "服务配置无效", "provider_payload_invalid": "服务请求结构无效",
        "provider_driver_not_registered": "服务驱动未注册",
        "provider_operation_not_supported": "服务不支持本次操作",
        "provider_pricing_invalid": "服务价格配置无效", "credential_missing": "服务缺少受管凭据",
        "credential_inactive": "受管凭据已停用", "credential_material_missing": "受管凭据缺少秘密值",
        "credential_version_binding_mismatch": "凭据版本与绑定不一致",
        "credential_algorithm_unsupported": "凭据加密算法不受支持", "credential_key_missing": "凭据解密密钥缺失",
        "credential_ciphertext_invalid": "凭据密文结构无效", "credential_ciphertext_mismatch": "凭据密文摘要不匹配",
        "credential_decryption_failed": "凭据解密校验失败", "secret_keyring_invalid": "凭据密钥环无效",
        "secret_keyring_unconfigured": "凭据密钥环未配置", "secret_active_key_missing": "当前加密密钥缺失",
        "secret_aad_version_unsupported": "凭据加密附加数据版本不受支持",
    }
    detail = safe_chat_failure_details(error)
    parts = [labels.get(error.code, "文本服务调用失败")]
    if error.code in labels:
        parts.append(error.code)
    for key in ("transport_phase", "exception_kind"):
        if key in detail:
            parts.append(detail[key])
    if "http_status" in detail:
        parts.append(f'HTTP {detail["http_status"]}')
    suffix = "远端结果仍未知，不能自动重发；核对原调用或明确授权新增尝试。" if error.submission_uncertain else "本次未形成可用结果，请按任务恢复规则处理。"
    return " · ".join(parts) + "；" + suffix


def _transport_failure(exc, *, submission, phase):
    # Requests documents ConnectTimeout as safe to retry: no request reached
    # the server. A generic ConnectionError/read failure does not prove this.
    before_send = phase == "request" and isinstance(exc, requests.ConnectTimeout)
    error = ProviderDriverError("provider 请求失败", code="provider_request_failed",
        retryable=before_send or not submission, submission_uncertain=submission and not before_send)
    kind = type(exc).__name__
    error.metadata = {"transport_phase": "connect" if before_send else phase,
        "exception_kind": kind if kind in SAFE_TRANSPORT_EXCEPTIONS else "RequestException"}
    return error


def restricted_async_rest_runtime_budget(config: dict[str, Any]) -> dict[str, int | float]:
    """Validate the explicit async transport/poll budget frozen in config."""

    required = ("timeout_seconds", "poll_interval_seconds", "max_poll_seconds")
    missing = [field for field in required if field not in config]
    if missing:
        raise ValidationError({
            "driver_config": "async_rest 运行预算缺少必填字段: " + ", ".join(missing),
        })
    timeout = config["timeout_seconds"]
    poll_interval = config["poll_interval_seconds"]
    max_poll = config["max_poll_seconds"]
    limits = boundary_values(
        "security.provider.async_rest_timeout_max_seconds",
        "security.provider.async_rest_poll_interval_max_seconds",
        "security.provider.async_rest_max_poll_seconds",
    )
    timeout_max = float(limits["security.provider.async_rest_timeout_max_seconds"])
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= timeout_max
    ):
        raise ValidationError({"timeout_seconds": f"必须是大于 0 且不超过安全边界 {timeout_max:g} 的有限数"})
    poll_max = float(limits["security.provider.async_rest_poll_interval_max_seconds"])
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not math.isfinite(float(poll_interval))
        or not 0 < float(poll_interval) <= poll_max
    ):
        raise ValidationError({
            "poll_interval_seconds": f"必须是大于 0 且不超过安全边界 {poll_max:g} 的有限数",
        })
    max_poll_max = int(limits["security.provider.async_rest_max_poll_seconds"])
    if (
        isinstance(max_poll, bool)
        or not isinstance(max_poll, int)
        or not 1 <= max_poll <= max_poll_max
    ):
        raise ValidationError({
            "max_poll_seconds": f"必须是正整数且不超过安全边界 {max_poll_max}",
        })
    return {
        "timeout_seconds": float(timeout),
        "poll_interval_seconds": float(poll_interval),
        "max_poll_seconds": max_poll,
    }


def restricted_async_rest_contract_limits() -> dict[str, int]:
    keys = (
        _ASYNC_REST_TERMINAL_STATUS_MAX_ITEMS_KEY,
        _ASYNC_REST_TERMINAL_STATUS_MAX_CHARACTERS_KEY,
        _ASYNC_REST_ARTIFACT_HOST_MAX_ITEMS_KEY,
    )
    values = boundary_values(*keys)
    if any(type(values.get(key)) is not int or values[key] < 1 for key in keys):
        raise ValidationError({
            "security_boundary": "受限异步 REST Provider 合同安全边界无效",
        })
    return {key: int(values[key]) for key in keys}


def restricted_comfyui_runtime_budget(config: dict[str, Any]) -> dict[str, int | float]:
    """Validate the explicit ComfyUI transport and asset-poll budget."""

    required = ("timeout_seconds", "poll_interval_seconds", "max_poll_seconds")
    missing = [field for field in required if field not in config]
    if missing:
        raise ValidationError({
            "runtime_config": "ComfyUI 运行预算缺少必填字段: " + ", ".join(missing),
        })
    timeout = config["timeout_seconds"]
    poll_interval = config["poll_interval_seconds"]
    max_poll = config["max_poll_seconds"]
    limits = boundary_values(
        "security.runtime.max_timeout_seconds",
        "security.runtime.max_interval_seconds",
    )
    timeout_max = float(limits["security.runtime.max_timeout_seconds"])
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= timeout_max
    ):
        raise ValidationError({
            "timeout_seconds": f"必须是大于 0 且不超过安全边界 {timeout_max:g} 的有限数",
        })
    poll_max = float(limits["security.runtime.max_interval_seconds"])
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not math.isfinite(float(poll_interval))
        or not 0 < float(poll_interval) <= poll_max
    ):
        raise ValidationError({
            "poll_interval_seconds": f"必须是大于 0 且不超过安全边界 {poll_max:g} 的有限数",
        })
    if (
        isinstance(max_poll, bool)
        or not isinstance(max_poll, int)
        or not 1 <= max_poll <= int(timeout_max)
    ):
        raise ValidationError({
            "max_poll_seconds": f"必须是正整数且不超过安全边界 {int(timeout_max)}",
        })
    return {
        "timeout_seconds": float(timeout),
        "poll_interval_seconds": float(poll_interval),
        "max_poll_seconds": max_poll,
    }


def validate_capability_ttl_seconds(
    config: dict[str, Any],
    *,
    required: bool = False,
) -> None:
    """Validate a deployment capability snapshot TTL when the driver declares one."""

    if "capability_ttl_seconds" not in config:
        if required:
            raise ValidationError({
                "capability_ttl_seconds": "必须显式配置正整数",
            })
        return
    capability_ttl = config["capability_ttl_seconds"]
    ttl_max = boundary_int("security.runtime.max_capability_ttl_seconds")
    if type(capability_ttl) is not int or not 1 <= capability_ttl <= ttl_max:
        raise ValidationError({
            "capability_ttl_seconds": (
                f"必须是正整数且不超过安全边界 {ttl_max}"
            ),
        })


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
        self.metadata: dict[str, Any] = {}


class ProviderCollectResponseError(ProviderDriverError):
    """An actual collect response failed validation; private, not error metadata."""

    def __init__(self, message, *, code, receipt):
        super().__init__(message, code=code)
        self.collect_receipt = receipt


def _async_status_response(response, *, config, request_id, status_limit):
    remote_status = str(response.get(config["status_field"]) or "")
    if len(remote_status) > status_limit:
        raise ProviderDriverError("异步 REST status 超过已发布安全边界", code="provider_response_invalid")
    return {
        "schema_version": 1, "request_id": request_id, "status": remote_status,
        "terminal_success": remote_status in set(config.get("terminal_success") or []),
        "terminal_failure": remote_status in set(config.get("terminal_failure") or []),
        "terminal_cancelled": remote_status in set(config.get("terminal_cancelled") or []),
    }


def _async_collect_response(response, *, config, request_id, status_limit):
    # request_id correlates the GET made by this adapter; source_response is the
    # actual decoded peer body, not reconstructed HTTP bytes or a remote echo.
    receipt = {"schema_version": 1, "request_id": request_id, "source_response": response}
    try:
        result = _async_status_response(response, config=config, request_id=request_id, status_limit=status_limit)
        artifacts = response.get(config["artifacts_field"])
        if not isinstance(artifacts, list) or not artifacts:
            raise ProviderDriverError("异步 REST collect 缺少 artifacts", code="provider_response_invalid")
        normalized_artifacts = []
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise ProviderDriverError("异步 REST artifact 必须是对象", code="provider_response_invalid")
            url = artifact.get(config["artifact_url_field"])
            kind = str(artifact.get(config["artifact_kind_field"]) or "")
            if not isinstance(url, str) or not url or kind not in {"image", "video", "audio"}:
                raise ProviderDriverError(
                    f"异步 REST artifact[{index}] 缺少受支持的 url/kind", code="provider_response_invalid")
            normalized_artifacts.append({
                "url": url, "kind": kind,
                "artifact_id": str(artifact.get(config["artifact_id_field"]) or "")[:191],
                "sha256": str(artifact.get(config["artifact_sha256_field"]) or "").casefold(),
            })
        return {**result, "source_response": response, "artifacts": normalized_artifacts,
            "actual_cost": response.get(config["actual_cost_field"]),
            "currency": str(response.get(config["currency_field"]) or "")}
    except ProviderDriverError as exc:
        raise ProviderCollectResponseError(str(exc), code=exc.code, receipt=receipt) from exc


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


def registered_driver_capabilities() -> dict[str, dict[str, Any]]:
    """Return the non-secret capability declaration of every code driver.

    The registry options API uses this read-only catalog so the browser never
    has to duplicate transport operations or infer them from Provider labels.
    Driver capability declarations must therefore stay side-effect free.
    """

    return {
        driver_type: dict(driver_class().capabilities())
        for driver_type, driver_class in sorted(_DRIVERS.items())
    }


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


@dataclass(frozen=True, slots=True)
class _ResolvedSocketAddress:
    ip: str
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedProviderEndpoint:
    endpoint: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[_ResolvedSocketAddress, ...]

    @property
    def peer_addresses(self) -> frozenset[str]:
        return frozenset(item.ip for item in self.addresses)


def _validated_endpoint_parts(value: str) -> tuple[str, Any, str, int]:
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
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValidationError("provider base_url 端口无效") from exc
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValidationError("公网 provider 必须使用 HTTPS")
    return endpoint, parsed, hostname, port


def _resolved_provider_endpoint(value: str) -> _ResolvedProviderEndpoint:
    endpoint, parsed, hostname, port = _validated_endpoint_parts(value)
    raw_addresses: list[tuple[int, int, int, tuple[Any, ...]]] = []
    try:
        numeric_ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            raw_addresses = [
                (family, socktype, protocol, tuple(sockaddr))
                for family, socktype, protocol, _canonname, sockaddr
                in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            ]
        except OSError as exc:
            raise ProviderDriverError(
                "provider hostname 无法解析",
                code="provider_dns_unavailable",
            ) from exc
    else:
        family = socket.AF_INET6 if numeric_ip.version == 6 else socket.AF_INET
        sockaddr = (
            (str(numeric_ip), port, 0, 0)
            if family == socket.AF_INET6
            else (str(numeric_ip), port)
        )
        raw_addresses = [(family, socket.SOCK_STREAM, 0, sockaddr)]
    if not raw_addresses:
        raise ProviderDriverError(
            "provider hostname 没有可验证的 DNS 地址",
            code="provider_dns_unavailable",
        )
    local_hostname = hostname in {"localhost", "127.0.0.1", "::1"}
    addresses: list[_ResolvedSocketAddress] = []
    seen: set[tuple[str, int, int]] = set()
    for family, socktype, protocol, sockaddr in raw_addresses:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            raise ProviderDriverError(
                "provider hostname 返回了不受支持的地址族",
                code="provider_dns_unavailable",
            )
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (TypeError, ValueError) as exc:
            raise ProviderDriverError(
                "provider hostname 返回了无效地址",
                code="provider_dns_unavailable",
            ) from exc
        if local_hostname:
            if not ip.is_loopback:
                raise ProviderDriverError(
                    "显式 loopback provider 解析到了非 loopback 地址",
                    code="provider_loopback_mismatch",
                )
        elif any((
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
        canonical_ip = str(ip)
        key = (canonical_ip, family, protocol)
        if key in seen:
            continue
        seen.add(key)
        canonical_sockaddr = (
            (
                canonical_ip,
                port,
                sockaddr[2] if len(sockaddr) > 2 else 0,
                sockaddr[3] if len(sockaddr) > 3 else 0,
            )
            if family == socket.AF_INET6
            else (canonical_ip, port)
        )
        addresses.append(_ResolvedSocketAddress(
            ip=canonical_ip,
            family=family,
            socktype=socktype or socket.SOCK_STREAM,
            protocol=protocol,
            sockaddr=canonical_sockaddr,
        ))
    if not addresses:
        raise ProviderDriverError(
            "provider hostname 没有可验证的 DNS 地址",
            code="provider_dns_unavailable",
        )
    return _ResolvedProviderEndpoint(
        endpoint=endpoint,
        scheme=parsed.scheme,
        hostname=hostname,
        port=port,
        addresses=tuple(addresses),
    )


def validate_endpoint(value: str, *, resolve_dns: bool = False) -> str:
    endpoint, _parsed, _hostname, _port = _validated_endpoint_parts(value)
    if resolve_dns:
        _resolved_provider_endpoint(endpoint)
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
    max_response_bytes = boundary_int("security.provider.max_response_bytes")
    raw_length = str(response.headers.get("Content-Length") or "").strip()
    if raw_length:
        try:
            if int(raw_length) > max_response_bytes:
                response.close()
                raise ProviderDriverError(
                    f"provider JSON 响应超过安全边界 {max_response_bytes} bytes",
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
            if size > max_response_bytes:
                raise ProviderDriverError(
                    f"provider JSON 响应超过安全边界 {max_response_bytes} bytes",
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


def _connect_to_resolved_address(
    connection: HTTPConnection | HTTPSConnection,
    addresses: tuple[_ResolvedSocketAddress, ...],
) -> socket.socket:
    last_error: OSError | None = None
    for address in addresses:
        sock = None
        try:
            sock = socket.socket(address.family, address.socktype, address.protocol)
            if isinstance(connection.timeout, (int, float)) or connection.timeout is None:
                sock.settimeout(connection.timeout)
            if connection.source_address:
                sock.bind(connection.source_address)
            for option in connection.socket_options or ():
                sock.setsockopt(*option)
            sock.connect(address.sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if isinstance(last_error, TimeoutError):
        raise ConnectTimeoutError(
            connection,
            f"Connection to {connection.host} timed out.",
        ) from last_error
    raise NewConnectionError(
        connection,
        f"Failed to establish a connection to {connection.host}.",
    ) from last_error


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, *args, resolved_addresses, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_addresses = tuple(resolved_addresses)

    def _new_conn(self) -> socket.socket:
        return _connect_to_resolved_address(self, self._resolved_addresses)


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, resolved_addresses, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_addresses = tuple(resolved_addresses)

    def _new_conn(self) -> socket.socket:
        return _connect_to_resolved_address(self, self._resolved_addresses)


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedAddressAdapter(HTTPAdapter):
    """Connect one request only to its already filtered DNS result.

    The pool host remains the configured hostname, so the HTTP Host header and
    HTTPS SNI/certificate verification keep their original security semantics.
    Only the TCP destination is replaced by the immutable numeric address set.
    """

    def __init__(self, resolved: _ResolvedProviderEndpoint, *, before_send=None):
        super().__init__(max_retries=0)
        self._resolved = resolved
        self._before_send = before_send
        self._pool: HTTPConnectionPool | HTTPSConnectionPool | None = None

    def send(self, request, **kwargs):
        if self._before_send is not None:
            from studio.services.chat_http_evidence import prepared_json_body_facts
            # Observe Requests' exact PreparedRequest.body, not a second JSON
            # serializer. The durable observer must succeed before network I/O.
            self._before_send(prepared_json_body_facts(request))
        return super().send(request, **kwargs)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if any(value for value in (proxies or {}).values()):
            raise requests.exceptions.ProxyError(
                "provider pinned transport forbids proxies",
                request=request,
            )
        parsed = urlparse(request.url)
        try:
            request_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise requests.exceptions.InvalidURL(str(exc), request=request) from exc
        if (
            parsed.scheme != self._resolved.scheme
            or (parsed.hostname or "").casefold() != self._resolved.hostname
            or request_port != self._resolved.port
            or parsed.username
            or parsed.password
        ):
            raise requests.exceptions.InvalidURL(
                "provider request origin differs from resolved endpoint",
                request=request,
            )
        pool_class = (
            _PinnedHTTPSConnectionPool
            if self._resolved.scheme == "https"
            else _PinnedHTTPConnectionPool
        )
        self._pool = pool_class(
            self._resolved.hostname,
            self._resolved.port,
            maxsize=1,
            block=True,
            resolved_addresses=self._resolved.addresses,
        )
        return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        super().close()


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
    inspect_text_capacity: bool = False,
) -> dict[str, Any]:
    parsed_url = urlparse(url)
    if parsed_url.query or parsed_url.fragment:
        raise ProviderDriverError(
            "provider request URL 禁止 query/fragment",
            code="provider_path_origin_mismatch",
        )
    resolved = _resolved_provider_endpoint(
        f"{parsed_url.scheme}://{parsed_url.netloc}",
    )
    from studio.services.chat_http_evidence import current_chat_body_observer
    adapter = _PinnedAddressAdapter(resolved, before_send=(
        current_chat_body_observer() if submission and inspect_text_capacity else None))
    session = requests.Session()
    # Provider traffic must never inherit operator/user proxy settings: doing
    # so would replace the verified peer with an unvalidated proxy endpoint.
    session.trust_env = False
    session.mount(f"{resolved.scheme}://", adapter)
    response = None
    try:
        response = session.request(
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
        if peer not in resolved.peer_addresses:
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
            # Recognize only bounded structured diagnostics, never persist the
            # vendor message (which may echo user content or credentials).
            if inspect_text_capacity and response.status_code in {400, 422}:
                try:
                    rejected = _response_json(response).get("error")
                except ProviderDriverError:
                    rejected = None
                if _text_capacity_rejection(rejected):
                    response.close()
                    error = ProviderDriverError("服务商拒绝文本输出参数或上下文容量，请人工调整后建立新请求", code="provider_text_capacity_rejected")
                    error.metadata = {"contract_error_code": "request_text_capacity_rejected"}
                    error.partial_text = ""
                    raise error
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
    except ProviderDriverError as exc:
        if response is not None:
            exc.metadata.update(transport_phase="response_headers", http_status=response.status_code)
        session.close()
        raise
    except requests.RequestException as exc:
        session.close()
        raise _transport_failure(exc, submission=submission, phase="request") from exc
    except Exception:
        session.close()
        raise  # Audit/serialization failures must not leak a session or send.
    try:
        try:
            return _response_json(response)
        except ProviderDriverError as exc:
            if not submission:
                raise
            wrapped = ProviderDriverError(
                str(exc),
                code=exc.code,
                retryable=exc.retryable,
                submission_uncertain=True,
            )
            wrapped.metadata = {**safe_chat_failure_details(exc), "transport_phase": "response_body",
                "http_status": response.status_code}
            raise wrapped from exc
        except requests.RequestException as exc:
            error = _transport_failure(exc, submission=submission, phase="response_body")
            error.metadata["http_status"] = response.status_code
            raise error from exc
    finally:
        session.close()


def _text_capacity_rejection(error):
    if not isinstance(error, dict):
        return False
    code = error.get("code") or error.get("type")
    if not isinstance(code, str):
        return False
    return code in {"context_length_exceeded", "max_tokens_exceeded", "max_output_tokens_exceeded"} or (
        error.get("param") in ("max_tokens", "max_completion_tokens", "max_output_tokens")
        and code in {"invalid_parameter", "invalid_request_error", "invalid_value", "unsupported_value", "invalid_parameter_error"})


def _combined_config(deployment: ModelDeployment) -> dict[str, Any]:
    profile = deployment.profile_version.provider
    validate_provider_profile(profile)
    verify_json_contract(
        deployment.runtime_config,
        schema_version=deployment.runtime_config_schema_version,
        size_bytes=deployment.runtime_config_size_bytes,
        sha256=deployment.runtime_config_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    sensitive = find_sensitive_json_keys(deployment.runtime_config)
    if sensitive:
        raise ProviderDriverError(
            f"runtime_config 含敏感字段: {', '.join(sensitive)}",
            code="provider_plaintext_secret_forbidden",
        )
    combined = {**profile.driver_config, **deployment.runtime_config}
    driver_for(profile).validate_config(combined)
    return combined


@dataclass(frozen=True, slots=True)
class ComfyUIWorkflowSourceScope:
    """Stable identity of one configured ComfyUI userdata workflow library.

    A ModelDeployment is only a transport route to that library.  Model
    profile, priority and timeout settings deliberately do not participate in
    the scope so several model deployments on one ComfyUI instance are scanned
    once.  Provider and host boundaries remain explicit.  Credentials are not
    included until the ComfyUI transport itself actually consumes them.
    """

    schema_version: int
    sha256: str
    endpoint: str
    directory: str


def _canonical_comfyui_source_endpoint(value: str) -> str:
    endpoint = validate_endpoint(value)
    parsed = urlparse(endpoint)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    default_port = 443 if scheme == "https" else 80
    port = parsed.port
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = display_host if port in {None, default_port} else f"{display_host}:{port}"
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{authority}{path}"


def comfyui_workflow_source_scope(
    deployment: ModelDeployment,
) -> ComfyUIWorkflowSourceScope:
    """Return the non-secret, non-network source identity for saved workflows."""

    if deployment.profile_version.provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        raise ValidationError("只有 ComfyUI deployment 具有已保存工作流来源范围")
    config = _combined_config(deployment)
    endpoint = _canonical_comfyui_source_endpoint(str(config.get("base_url") or ""))
    directory = saved_workflow_directory()
    contract = json_contract({
        "schema_version": COMFYUI_WORKFLOW_SOURCE_SCOPE_SCHEMA_VERSION,
        "kind": "comfyui.userdata.saved_workflows",
        "provider_id": str(deployment.profile_version.provider_id),
        "runtime_host_id": str(deployment.runtime_host_id or ""),
        "endpoint": endpoint,
        "directory": directory,
    }, max_bytes=_stage_snapshot_max_bytes())
    return ComfyUIWorkflowSourceScope(
        schema_version=COMFYUI_WORKFLOW_SOURCE_SCOPE_SCHEMA_VERSION,
        sha256=contract.sha256,
        endpoint=endpoint,
        directory=directory,
    )


def validate_deployment_execution_contract(deployment: ModelDeployment) -> None:
    """Validate the complete merged Deployment contract without invoking it."""

    _combined_config(deployment)


def deployment_execution_config_contract(deployment: ModelDeployment):
    """Return validated merged transport configuration for internal hashing.

    TTL and price affect admission/accounting, not what request the driver
    sends or how it reads a response. All other registered configuration keys
    participate, including newly added keys, so an extension cannot silently
    escape execution drift detection. This does not resolve any secret.
    """
    config = _combined_config(deployment)
    return json_contract(
        {key: value for key, value in config.items() if key not in {"capability_ttl_seconds", "pricing"}},
        max_bytes=_stage_snapshot_max_bytes(),
    )


def deployment_capability_ttl_seconds(deployment: ModelDeployment) -> int:
    """One configured TTL for newly completed resource and instance evidence."""
    config = _combined_config(deployment)
    validate_capability_ttl_seconds(config, required=True)
    return config["capability_ttl_seconds"]


def validate_provider_profile(profile: ProviderProfile) -> None:
    verify_json_contract(
        profile.driver_config,
        schema_version=profile.driver_config_schema_version,
        size_bytes=profile.driver_config_size_bytes,
        sha256=profile.driver_config_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    sensitive = find_sensitive_json_keys(profile.driver_config)
    if sensitive:
        raise ValidationError({"driver_config": f"不能保存密钥字段: {', '.join(sensitive)}"})
    try:
        driver_class = _DRIVERS[profile.driver_type]
    except KeyError as exc:
        raise ValidationError({"driver_type": "未注册的 provider driver"}) from exc
    driver = driver_class()
    # A Provider may deliberately delegate its executable endpoint contract to
    # each Deployment.  Validate the Provider allowlist here; the merged,
    # complete contract is validated by _combined_config and deployment writes.
    if profile.driver_config.get("runtime_source") == "deployment":
        ProviderDriver.validate_config(driver, profile.driver_config)
    else:
        driver.validate_config(profile.driver_config)


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

    def validate_execution_credentials(self, deployment: ModelDeployment) -> None:
        """Local credential check using this transport's actual auth policy.

        No remote probe and no secret may enter the returned admission facts.
        Restricted REST permits anonymous transports but validates a configured
        credential. Chat transports tighten this below; ComfyUI consumes none.
        """
        _transport_credential(deployment, required=False)

    @abstractmethod
    def invoke(
        self,
        deployment: ModelDeployment,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _transport_credential(deployment, *, required):
    reference = deployment.credential or deployment.provider.credential
    if reference is not None:
        return resolve_credential(reference)
    if required:
        raise ProviderDriverError("模型部署缺少受管凭据", code="credential_missing")
    return None


def _chat_transport_credential(deployment, base_url, *, credential_optional):
    hostname = (urlparse(base_url).hostname or "").casefold()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    return _transport_credential(deployment, required=not (credential_optional and local))


def _invoke_chat_completion(
    deployment: ModelDeployment,
    payload: dict[str, Any],
    *,
    credential_optional: bool,
) -> dict[str, Any]:
    """One bounded OpenAI-compatible transport using deployment-owned config."""

    config = _combined_config(deployment)
    # Shape/allowlist validation happens here; _request_json performs the one
    # DNS resolution that is pinned through TCP connect and peer validation.
    base_url = validate_endpoint(config.get("base_url"))
    chat_path = _validate_relative_path(config["chat_path"], field="chat_path")
    raw_timeout = payload.get("timeout_seconds", config["timeout_seconds"])
    timeout_max = boundary_number(
        "security.provider.async_rest_timeout_max_seconds",
        minimum=0.001,
    )
    if (
        isinstance(raw_timeout, bool)
        or not isinstance(raw_timeout, (int, float))
        or not math.isfinite(float(raw_timeout))
        or not 0 < float(raw_timeout) <= timeout_max
    ):
        raise ProviderDriverError(
            "timeout_seconds 缺失或超过当前安全边界",
            code="provider_config_invalid",
        )
    timeout = float(raw_timeout)
    from studio.services.chat_body import assemble_chat_body
    try:
        body, _layout = assemble_chat_body(deployment.profile_version.model_identifier, payload)
    except ValidationError as exc:
        raise ProviderDriverError(
            "Chat 请求未通过受控组包合同",
            code="provider_payload_invalid",
        ) from exc
    headers = {"Content-Type": "application/json"}
    credential = _chat_transport_credential(deployment, base_url, credential_optional=credential_optional)
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    response = _request_json(
        "POST", _endpoint_url(base_url, chat_path),
        timeout=timeout, headers=headers, payload=body, submission=True, inspect_text_capacity=True,
    )
    choices = response.get("choices")
    text = ""
    finish_reason = ""
    message: dict[str, Any] | None = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = str(choices[0].get("finish_reason") or "").strip().casefold()
        candidate = choices[0].get("message")
        if isinstance(candidate, dict):
            message = candidate
            text = str(candidate.get("content") or "")
    if finish_reason in {"length", "content_filter"}:
        safe_usage = safe_chat_usage(response.get("usage"))
        response_id = str(response.get("id") or "")
        error = ProviderDriverError(
            "OpenAI-compatible 响应被截断或过滤",
            code="provider_response_invalid",
        )
        error.metadata = {
            "contract_error_code": (
                "response_output_budget_exhausted"
                if finish_reason == "length"
                else "response_content_filtered"
            ),
            "finish_reason": finish_reason,
            **({"usage": safe_usage} if safe_usage else {}),
            **(
                {
                    "response_id_sha256": hashlib.sha256(
                        response_id.encode("utf-8"),
                    ).hexdigest(),
                }
                if response_id else {}
            ),
        }
        if finish_reason == "length":
            # Final content only; never retain reasoning/tool calls in audit
            # metadata. The original production owner persists this separately.
            error.partial_text = text
        raise error
    if not text:
        contract_error_code = "response_missing_text"
        if finish_reason == "length":
            contract_error_code = "response_output_budget_exhausted"
        elif finish_reason == "content_filter":
            contract_error_code = "response_content_filtered"
        elif message is not None and message.get("tool_calls"):
            contract_error_code = "response_tool_call_without_text"
        elif message is not None and message.get("reasoning_content"):
            contract_error_code = "response_reasoning_without_text"
        error = ProviderDriverError(
            "OpenAI-compatible 响应缺少文本",
            code="provider_response_invalid",
        )
        # Persist only a stable internal classifier.  Never attach provider
        # response bodies, authored reasoning, request ids, or secrets here.
        safe_usage = safe_chat_usage(response.get("usage"))
        response_id = str(response.get("id") or "")
        error.metadata = {
            "contract_error_code": contract_error_code,
            **(
                {"finish_reason": finish_reason}
                if finish_reason in SAFE_CHAT_FINISH_REASONS else {}
            ),
            **({"usage": safe_usage} if safe_usage else {}),
            **(
                {
                    "response_id_sha256": hashlib.sha256(
                        response_id.encode("utf-8"),
                    ).hexdigest(),
                }
                if response_id else {}
            ),
        }
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
        policy = prompt_normalization_policy_from_specification(
            verified_model_profile_specification(deployment.profile_version),
            required=True,
        )
        assert policy is not None
        from studio.services.execution_resource_policy import read_resource_policy
        chat_payload = prompt_normalization_chat_payload(payload, policy=policy,
            output_length=read_resource_policy().value["values"]["output_length"])
        input_warnings = prompt_normalization_input_warnings(
            policy,
            chat_payload.get("messages"),
        )
        chat_response = _invoke_chat_completion(
            deployment,
            chat_payload,
            credential_optional=credential_optional,
        )
        result = parse_prompt_normalization_chat_response(payload, chat_response)
        metadata = (
            dict(result.get("metadata"))
            if isinstance(result.get("metadata"), dict)
            else {}
        )
        if input_warnings:
            metadata["warnings"] = [
                *(
                    metadata.get("warnings")
                    if isinstance(metadata.get("warnings"), list)
                    else []
                ),
                *input_warnings,
            ]
        result["metadata"] = metadata
    except PromptNormalizationContractError as exc:
        error = ProviderDriverError(
            str(exc),
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        reason_code = safe_prompt_field_validation_reason_code(
            getattr(exc, "reason_code", ""),
        )
        validation_context = safe_prompt_field_validation_context(
            reason_code,
            getattr(exc, "validation_context", None),
            request_payload=payload,
        )
        error.metadata = {
            "contract_error_code": str(
                getattr(exc, "code", "prompt_normalize_contract_invalid")
            ),
            **({"reason_code": reason_code} if reason_code else {}),
            **(
                {"validation_context": validation_context}
                if validation_context else {}
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
        _validate_relative_path(config["chat_path"], field="chat_path")
        timeout = float(config["timeout_seconds"])
        timeout_max = boundary_number("security.provider.async_rest_timeout_max_seconds", minimum=0.001)
        if not 0 < timeout <= timeout_max:
            return False
    except (ProviderDriverError, ValidationError, KeyError, TypeError, ValueError):
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


def _validate_text_transport_timeout(config):
    timeout = config.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValidationError({"timeout_seconds": "必须显式配置有限正数"})
    timeout_max = boundary_number("security.provider.async_rest_timeout_max_seconds", minimum=0.001)
    if not math.isfinite(timeout) or not 0 < timeout <= timeout_max:
        raise ValidationError({"timeout_seconds": f"必须大于 0 且不超过安全边界 {timeout_max:g} 秒"})


class _ConfiguredTextDriver(ProviderDriver):
    provider_id = ""
    allowed_config_keys = frozenset({"runtime_source", "base_url", "pricing"})

    def validate_execution_credentials(self, deployment):
        _chat_transport_credential(deployment, _combined_config(deployment)["base_url"], credential_optional=False)

    def validate_config(self, config):
        super().validate_config(config)
        validate_capability_ttl_seconds(config, required=True)
        if config.get("runtime_source") != "openai_compatible_endpoint":
            raise ValidationError({
                "runtime_source": (
                    "可执行文本 Deployment 必须显式配置 "
                    "openai_compatible_endpoint"
                ),
            })
        validate_endpoint(config.get("base_url"))
        try:
            chat_path = config["chat_path"]
        except KeyError as exc:
            raise ValidationError({"chat_path": "必须显式配置相对路径"}) from exc
        _validate_relative_path(chat_path, field="chat_path")
        _validate_text_transport_timeout(config)

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
        }

    def invoke(self, deployment, operation, payload):
        _combined_config(deployment)
        raise ProviderDriverError(
            f"当前文本驱动尚未为 {operation} 建立统一调用契约",
            code="provider_operation_not_supported",
        )


@register_driver(ProviderProfile.DriverType.LOCAL_QWEN)
class LocalQwenDriver(_ConfiguredTextDriver):
    provider_id = "local_qwen"

    def validate_execution_credentials(self, deployment):
        _chat_transport_credential(deployment, _combined_config(deployment)["base_url"], credential_optional=True)

    allowed_config_keys = frozenset({
        "runtime_source", "base_url", "chat_path", "timeout_seconds",
        "capability_ttl_seconds", "pricing",
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
                "local_qwen deployment 必须显式配置受控 OpenAI-compatible endpoint",
                code="provider_operation_not_supported",
            )
        return super().invoke(deployment, operation, payload)


@register_driver(ProviderProfile.DriverType.DEEPSEEK_API)
class DeepSeekDriver(_ConfiguredTextDriver):
    provider_id = "deepseek_api"

    allowed_config_keys = frozenset({
        "runtime_source", "base_url", "chat_path", "timeout_seconds",
        "capability_ttl_seconds", "pricing",
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
                "deepseek deployment 必须显式配置受控 OpenAI-compatible endpoint",
                code="provider_operation_not_supported",
            )
        return super().invoke(deployment, operation, payload)


_COMFYUI_DIAGNOSTIC_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_COMFYUI_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b("
    r"api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|authorization|"
    r"token|secret|password)"
    r"\b\s*[:=]\s*[^\s,;]+",
)
_COMFYUI_DIAGNOSTIC_BEARER_RE = re.compile(
    r"(?i)\bbearer\s+[a-z0-9._~+/=-]+",
)
_COMFYUI_DIAGNOSTIC_URL_RE = re.compile(r"(?i)https?://[^\s,;]+")
_COMFYUI_DIAGNOSTIC_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9])[a-z]:\\[^\s,;]+",
)
_COMFYUI_DIAGNOSTIC_POSIX_PATH_RE = re.compile(
    r"(?<![:a-zA-Z0-9])/(?:[^/\s]+/)+[^\s,;]*",
)


def _safe_comfyui_diagnostic_text(value: object, *, maximum: int) -> str:
    """Project one ComfyUI diagnostic field without exposing traceback payloads."""

    if not isinstance(value, str):
        return ""
    text = value.split("Traceback (most recent call last):", 1)[0]
    text = _COMFYUI_DIAGNOSTIC_CONTROL_RE.sub(" ", text)
    text = " ".join(text.split()).strip()
    if not text or re.search(r"(?i)<(?:!doctype|html|body|script)\b", text):
        return ""
    text = _COMFYUI_DIAGNOSTIC_BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _COMFYUI_DIAGNOSTIC_URL_RE.sub("[REDACTED_URL]", text)
    text = _COMFYUI_DIAGNOSTIC_WINDOWS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _COMFYUI_DIAGNOSTIC_POSIX_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _COMFYUI_DIAGNOSTIC_SECRET_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    )
    return text if len(text) <= maximum else f"{text[:maximum - 1]}…"


def _comfyui_failure_explanation(message: str) -> tuple[str, str]:
    normalized = message.casefold()
    if "requires at least one reference media input" in normalized:
        return (
            "Ref2VA 模式至少需要 1 个参考图片、参考视频或参考音频。",
            "在“本次 Smoke 输入”中添加至少 1 个合格参考素材，然后创建新的 Smoke 任务。",
        )
    if "cuda out of memory" in normalized or "outofmemoryerror" in normalized:
        return (
            "模型部署显存不足，ComfyUI 无法继续执行当前工作流。",
            "先释放同主机上的并行 GPU 任务，或改用显存充足的模型部署；重新探测并 Live Validate 后再创建新的 Smoke 任务。",
        )
    if any(marker in normalized for marker in (
        "model not found", "no such file or directory", "file not found",
    )):
        return (
            message or "工作流依赖的模型或文件不存在。",
            "核对该模型部署的模型文件与自定义节点依赖；完成主机探测和 Live Validate 后再创建新的 Smoke 任务。",
        )
    return (
        message or "ComfyUI 未返回可安全展示的具体异常正文。",
        "根据失败节点检查本次输入、模型文件和自定义节点依赖；若修改了节点图，请在 ComfyUI 保存新 revision 并重新导入，否则修正输入后创建新的 Smoke 任务。",
    )


def _comfyui_poll_failure(history: object) -> dict[str, object]:
    """Project only the reviewed fields of the last ComfyUI execution error."""

    status = history.get("status") if isinstance(history, dict) else None
    messages = status.get("messages") if isinstance(status, dict) else None
    failure: dict[str, object] | None = None
    if isinstance(messages, list):
        for event in reversed(messages):
            if (
                isinstance(event, (list, tuple))
                and len(event) >= 2
                and str(event[0]) == "execution_error"
                and isinstance(event[1], dict)
            ):
                failure = event[1]
                break
    if failure is None:
        return {}

    return {
        "schema_version": 1,
        "kind": "execution_error",
        "node_id": _safe_comfyui_diagnostic_text(
            failure.get("node_id"), maximum=120,
        ),
        "exception_type": _safe_comfyui_diagnostic_text(
            failure.get("exception_type"), maximum=120,
        ),
        "exception_message": _safe_comfyui_diagnostic_text(
            failure.get("exception_message"), maximum=700,
        ),
    }


def _comfyui_failure_message(failure: object, *, graph: object) -> str:
    """Format a diagnostic using the frozen submitted graph as node authority."""

    if (
        not isinstance(failure, dict)
        or failure.get("schema_version") != 1
        or failure.get("kind") != "execution_error"
    ):
        return ""
    node_id = _safe_comfyui_diagnostic_text(failure.get("node_id"), maximum=120)
    node = graph.get(node_id) if isinstance(graph, dict) and node_id else None
    verified_node_id = node_id if isinstance(node, dict) else ""
    node_type = _safe_comfyui_diagnostic_text(
        node.get("class_type") if isinstance(node, dict) else "",
        maximum=191,
    )
    exception_type = _safe_comfyui_diagnostic_text(
        failure.get("exception_type"), maximum=120,
    )
    remote_message = _safe_comfyui_diagnostic_text(
        failure.get("exception_message"), maximum=700,
    )
    reason, recovery = _comfyui_failure_explanation(remote_message)
    node_label = (
        f"节点 {verified_node_id}（{node_type}）"
        if verified_node_id and node_type
        else f"节点 {verified_node_id}"
        if verified_node_id
        else "冻结图中无法确认的节点"
    )
    type_label = f" [{exception_type}]" if exception_type else ""
    original = (
        f"远端原文：{remote_message}。"
        if remote_message and remote_message != reason
        else ""
    )
    return (
        f"ComfyUI {node_label}执行失败{type_label}。"
        f"原因：{reason}{original}处理建议：{recovery}"
    )[:1500]


@register_driver(ProviderProfile.DriverType.COMFYUI)
class ComfyUIDriver(ProviderDriver):
    allowed_config_keys = frozenset({
        "runtime_source", "base_url", "timeout_seconds", "pricing",
        "pre_submit_resource_release", "capability_ttl_seconds",
        "poll_interval_seconds", "max_poll_seconds",
    })

    def validate_execution_credentials(self, deployment):
        # Current ComfyUI client does not consume Provider credentials. Do not
        # imply support for an authenticated transport through a registry check.
        return None

    def validate_config(self, config):
        super().validate_config(config)
        validate_capability_ttl_seconds(config, required=True)
        try:
            base_url = config["base_url"]
        except KeyError as exc:
            raise ValidationError({"base_url": "必须显式配置 ComfyUI 服务根地址"}) from exc
        validate_endpoint(base_url)
        restricted_comfyui_runtime_budget(config)
        from studio.services.comfyui import (
            ComfyUIError,
            validate_pre_submit_resource_release_policy,
        )
        try:
            validate_pre_submit_resource_release_policy(
                config.get("pre_submit_resource_release"),
            )
        except ComfyUIError as exc:
            raise ValidationError({"pre_submit_resource_release": str(exc)}) from exc

    def capabilities(self):
        return {
            "schema_version": 1,
            "modalities": ["text", "image", "video", "audio"],
            "operations": [
                "health", "object_info", "models", "workflow_library.list",
                "workflow_library.read", "upload", "submit", "poll", "cancel", "collect",
            ],
        }

    def estimate(self, deployment, operation, payload):
        config = _combined_config(deployment)
        endpoint = urlparse(str(config.get("base_url") or ""))
        local = (endpoint.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        return _pricing_estimate(deployment, allow_zero_without_pricing=local)

    def invoke(self, deployment, operation, payload):
        from studio.services.comfyui import (
            ComfyUIClient,
            ComfyUIError,
            ComfyUIHTTPError,
            ComfyUIPreSubmitResourceReleaseError,
        )

        config = _combined_config(deployment)
        try:
            endpoint = validate_endpoint(config.get("base_url"), resolve_dns=True)
            configured_timeout = float(config["timeout_seconds"])
            transport_timeout = payload.get("_transport_timeout_seconds")
            if transport_timeout is not None:
                if (
                    isinstance(transport_timeout, bool)
                    or not isinstance(transport_timeout, (int, float))
                    or not math.isfinite(float(transport_timeout))
                    or float(transport_timeout) <= 0
                ):
                    raise ProviderDriverError(
                        "transport timeout 合同无效",
                        code="provider_payload_invalid",
                    )
                transport_timeout = float(transport_timeout)
                effective_timeout = min(configured_timeout, transport_timeout)
            else:
                effective_timeout = configured_timeout
            client = ComfyUIClient(
                base_url=endpoint,
                timeout=effective_timeout,
                endpoint_prevalidated=True,
                pre_submit_resource_release_policy=config.get(
                    "pre_submit_resource_release",
                ),
            )
            if operation == "health":
                return client.health()
            if operation == "object_info":
                return client.object_info()
            if operation == "models":
                folder = str(payload.get("folder") or "")
                return {"schema_version": 1, "folder": folder, "items": client.models(folder)}
            if operation == "workflow_library.list":
                return {
                    "schema_version": 1,
                    "items": client.list_saved_workflows(saved_workflow_directory()),
                }
            if operation == "workflow_library.read":
                source_path = str(payload.get("source_path") or "")
                document = client.read_saved_workflow(
                    source_path,
                    saved_workflow_directory(),
                )
                return {
                    "schema_version": 1,
                    "source_path": document.source_path,
                    "source_sha256": document.sha256,
                    "size_bytes": document.size_bytes,
                    "workflow": document.workflow,
                }
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
                release_audit = client.release_resources_before_submit()
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
                    "pre_submit_resource_release": release_audit,
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
                    return {
                        "schema_version": 1,
                        "request_id": request_id,
                        "status": "running" if client.is_queued(request_id) else "missing",
                    }
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
                    result = {
                        "schema_version": 1,
                        "request_id": request_id,
                        "status": "failed" if failed else "succeeded" if completed else "running",
                    }
                    if failed:
                        result["failure"] = _comfyui_poll_failure(history)
                    return result
                if failed or not completed:
                    raise ProviderDriverError(
                        "远端任务尚未成功，不能 collect",
                        code="provider_collect_not_ready",
                    )
                outputs = client.extract_outputs(history)
                # A completed empty receipt is still an actual return. The
                # caller validates its frozen output contract after retention.
                if not isinstance(outputs, list):
                    raise ProviderDriverError(
                        "收集输出结构无效",
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
        except ComfyUIPreSubmitResourceReleaseError as exc:
            raise ProviderDriverError(
                str(exc),
                code=exc.code,
                retryable=False,
                submission_uncertain=False,
            ) from exc
        except ComfyUIHTTPError as exc:
            code = (
                "provider_request_rejected"
                if 400 <= exc.status_code < 500
                else "comfyui_operation_failed"
            )
            raise ProviderDriverError(
                str(exc),
                code=code,
                retryable=exc.status_code >= 500,
            ) from exc
        except (ComfyUIError, ValidationError, ValueError) as exc:
            raise ProviderDriverError(
                f"ComfyUI operation 失败: {str(exc)[:1500]}",
                code="comfyui_operation_failed",
            ) from exc
        raise ProviderDriverError("ComfyUI operation 不受支持", code="provider_operation_not_supported")


@register_driver(ProviderProfile.DriverType.OPENAI_COMPATIBLE)
class OpenAICompatibleDriver(ProviderDriver):
    def validate_execution_credentials(self, deployment):
        _chat_transport_credential(deployment, _combined_config(deployment)["base_url"], credential_optional=False)

    allowed_config_keys = frozenset({
        "base_url", "chat_path", "timeout_seconds", "capability_ttl_seconds", "pricing",
    })

    def validate_config(self, config):
        super().validate_config(config)
        validate_capability_ttl_seconds(config, required=True)
        validate_endpoint(config.get("base_url"))
        try:
            chat_path = config["chat_path"]
        except KeyError as exc:
            raise ValidationError({"chat_path": "必须显式配置相对路径"}) from exc
        _validate_relative_path(chat_path, field="chat_path")
        _validate_text_transport_timeout(config)

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
    STANDARD_REQUEST_FIELDS = STANDARD_MEDIA_FIELDS
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
        required_fields = (
            "health_path", "submit_path", "poll_path", "cancel_path", "collect_path",
            "request_id_field", "status_field", "artifacts_field", "actual_cost_field",
            "currency_field", "artifact_url_field", "artifact_kind_field",
            "artifact_id_field", "artifact_sha256_field", "idempotency_header",
        )
        missing = [field for field in required_fields if field not in config]
        if missing:
            raise ValidationError({
                "driver_config": "async_rest 执行合同缺少必填字段: " + ", ".join(missing),
            })
        _validate_relative_path(config["health_path"], field="health_path")
        _validate_relative_path(config["submit_path"], field="submit_path")
        _validate_relative_path(
            config["poll_path"],
            field="poll_path",
            allow_placeholder=True,
        )
        _validate_relative_path(
            config["cancel_path"],
            field="cancel_path",
            allow_placeholder=True,
        )
        _validate_relative_path(
            config["collect_path"],
            field="collect_path",
            allow_placeholder=True,
        )
        for field in ("request_id_field", "status_field"):
            value = str(config[field])
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None:
                raise ValidationError({field: "只允许单层 JSON 字段名"})
        progress_field = str(config.get("progress_field") or "").strip()
        if progress_field and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", progress_field) is None:
            raise ValidationError({"progress_field": "只允许单层 JSON 字段名"})
        if progress_field:
            if "progress_max" not in config:
                raise ValidationError({"progress_max": "配置 progress_field 时必须显式配置"})
            try:
                progress_max = float(config["progress_max"])
            except (TypeError, ValueError) as exc:
                raise ValidationError({"progress_max": "必须是正数"}) from exc
            if not math.isfinite(progress_max) or progress_max <= 0:
                raise ValidationError({"progress_max": "必须是有限正数"})
        for field in (
            "artifacts_field", "actual_cost_field", "currency_field",
            "artifact_url_field", "artifact_kind_field", "artifact_id_field",
            "artifact_sha256_field",
        ):
            value = str(config[field])
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None:
                raise ValidationError({field: "只允许单层 JSON 字段名"})
        header = str(config["idempotency_header"])
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header) is None:
            raise ValidationError({"idempotency_header": "HTTP header 名无效"})
        restricted_async_rest_runtime_budget(config)
        validate_capability_ttl_seconds(config, required=True)
        contract_limits = restricted_async_rest_contract_limits()
        terminal_status_max_items = contract_limits[
            _ASYNC_REST_TERMINAL_STATUS_MAX_ITEMS_KEY
        ]
        terminal_status_max_characters = contract_limits[
            _ASYNC_REST_TERMINAL_STATUS_MAX_CHARACTERS_KEY
        ]
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
                or len(values) > terminal_status_max_items
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > terminal_status_max_characters
                    for item in values
                )
            ):
                raise ValidationError({
                    field: (
                        f"必须为 1 到 {terminal_status_max_items} 个非空状态字符串，"
                        f"每项不超过 {terminal_status_max_characters} 个字符"
                    ),
                })
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
            or len(artifact_hosts) > contract_limits[
                _ASYNC_REST_ARTIFACT_HOST_MAX_ITEMS_KEY
            ]
        ):
            raise ValidationError({
                "artifact_allowed_hosts": (
                    "必须声明 1 到 "
                    f"{contract_limits[_ASYNC_REST_ARTIFACT_HOST_MAX_ITEMS_KEY]} 个产物主机"
                ),
            })
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
        contract_limits = restricted_async_rest_contract_limits()
        runtime_budget = restricted_async_rest_runtime_budget(config)
        base_url = validate_endpoint(config["base_url"])
        timeout = float(runtime_budget["timeout_seconds"])
        credential = deployment.credential or deployment.provider.credential
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {resolve_credential(credential)}"
        if operation == "health":
            response = _request_json(
                "GET",
                _endpoint_url(base_url, config["health_path"]),
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
            headers[str(config["idempotency_header"])] = idempotency_key
            allowed_payload = self.STANDARD_REQUEST_FIELDS | {"schema_version", "idempotency_key"}
            unknown_payload = sorted(set(payload) - allowed_payload)
            if unknown_payload:
                raise ProviderDriverError(
                    f"异步 submit 含未授权字段: {', '.join(unknown_payload)}",
                    code="provider_payload_invalid",
                )
            field_map = config["request_field_map"]
            from studio.services.async_submit_mapping import async_submit_body
            body = async_submit_body(payload, field_map)
            if not body.get(field_map["model"]) or not body.get(field_map["prompt"]):
                raise ProviderDriverError(
                    "异步 submit 缺少 model/prompt",
                    code="provider_payload_invalid",
                )
            path = config["submit_path"]
            response = _request_json(
                "POST", _endpoint_url(base_url, path),
                timeout=timeout, headers=headers, payload=body, submission=True,
            )
            request_id = response.get(config["request_id_field"])
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
        path = config[path_key].replace(
            "{request_id}", quote(request_id, safe=""),
        )
        response = _request_json(
            "GET" if operation in {"poll", "collect"} else "POST",
            _endpoint_url(base_url, path),
            timeout=timeout,
            headers=headers,
            payload=None,
        )
        remote_status_max_characters = contract_limits[
            _ASYNC_REST_TERMINAL_STATUS_MAX_CHARACTERS_KEY
        ]
        if operation == "collect":
            return _async_collect_response(response, config=config, request_id=request_id,
                status_limit=remote_status_max_characters)
        result = _async_status_response(response, config=config, request_id=request_id,
            status_limit=remote_status_max_characters)
        progress_field = str(config.get("progress_field") or "").strip()
        if operation == "poll" and progress_field and response.get(progress_field) is not None:
            try:
                raw_progress = float(response[progress_field])
                progress_max = float(config["progress_max"])
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
        return result
