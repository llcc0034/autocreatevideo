from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
import uuid
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import GeneratedArtifact, GenerationAttempt
from studio.services.generated_artifacts import (
    _artifact_limit,
    build_comparison_parameters,
    verified_local_media_facts,
)
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError, _peer_address


MAX_REMOTE_ARTIFACTS = 8


def _global_allowed_hosts() -> set[str]:
    configured = getattr(settings, "AI_PROVIDER_ALLOWED_HOSTS", ())
    if isinstance(configured, str):
        configured = configured.split(",")
    return {str(item).strip().casefold() for item in configured if str(item).strip()}


def _validated_artifact_url(url: str, provider_config: dict) -> tuple[str, set[str]]:
    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or "").casefold()
    provider_hosts = {
        str(item).strip().casefold()
        for item in provider_config.get("artifact_allowed_hosts", [])
        if str(item).strip()
    }
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or hostname not in provider_hosts
        or hostname not in _global_allowed_hosts()
    ):
        raise ProviderDriverError("远端产物 URL 未通过主机策略", code="artifact_url_forbidden")
    loopback_name = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not loopback_name:
        raise ProviderDriverError("公网远端产物必须使用 HTTPS", code="artifact_url_forbidden")
    try:
        addresses = {
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError) as exc:
        raise ProviderDriverError("远端产物主机无法安全解析", code="artifact_dns_unavailable") from exc
    if not addresses:
        raise ProviderDriverError("远端产物主机没有可验证地址", code="artifact_dns_unavailable")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if loopback_name:
            if not ip.is_loopback:
                raise ProviderDriverError("远端产物 loopback 解析不一致", code="artifact_peer_forbidden")
        elif any((
            ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast,
            ip.is_reserved, ip.is_unspecified,
        )):
            raise ProviderDriverError("远端产物解析到禁止地址", code="artifact_peer_forbidden")
    return str(url), addresses


def _download_one(
    *,
    url: str,
    provider_config: dict,
    target: Path,
    expected_kind: str,
    expected_sha256: str,
    max_bytes: int,
    heartbeat=None,
) -> dict:
    safe_url, addresses = _validated_artifact_url(url, provider_config)
    timeout = float(provider_config.get("timeout_seconds", 60))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".remote-",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    digest = hashlib.sha256()
    size = 0
    response = None
    try:
        if heartbeat is not None:
            heartbeat()
        response = requests.get(
            safe_url,
            stream=True,
            allow_redirects=False,
            timeout=timeout,
        )
        peer = _peer_address(response)
        if not peer or peer not in addresses:
            raise ProviderDriverError("远端产物连接地址无法验证", code="artifact_peer_mismatch")
        if 300 <= response.status_code < 400:
            raise ProviderDriverError("远端产物禁止重定向", code="artifact_redirect_forbidden")
        if response.status_code >= 400:
            raise ProviderDriverError("远端产物下载失败", code="artifact_download_failed")
        raw_length = str(response.headers.get("Content-Length") or "").strip()
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise ProviderDriverError("远端产物超过大小上限", code="artifact_too_large")
            except ValueError as exc:
                raise ProviderDriverError("远端产物 Content-Length 无效", code="artifact_response_invalid") from exc
        with handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ProviderDriverError("远端产物超过大小上限", code="artifact_too_large")
                digest.update(chunk)
                handle.write(chunk)
                if heartbeat is not None:
                    heartbeat()
            handle.flush()
            os.fsync(handle.fileno())
        if size <= 0:
            raise ProviderDriverError("远端产物为空", code="artifact_response_invalid")
        downloaded_sha = digest.hexdigest()
        normalized_expected = str(expected_sha256 or "").casefold()
        if normalized_expected:
            if re.fullmatch(r"[0-9a-f]{64}", normalized_expected) is None:
                raise ProviderDriverError("远端产物 checksum 格式无效", code="artifact_response_invalid")
            if downloaded_sha != normalized_expected:
                raise ProviderDriverError("远端产物 checksum 不匹配", code="artifact_checksum_mismatch")
        facts = verified_local_media_facts(
            temporary,
            expected_kind=expected_kind,
            max_bytes=max_bytes,
        )
        if facts["sha256"] != downloaded_sha:
            raise ProviderDriverError("远端产物校验结果漂移", code="artifact_checksum_mismatch")
        os.replace(temporary, target)
        persisted = verified_local_media_facts(
            target,
            expected_kind=expected_kind,
            max_bytes=max_bytes,
        )
        if persisted != facts:
            target.unlink(missing_ok=True)
            raise ProviderDriverError("远端产物原子归档后二次校验失败", code="artifact_archive_failed")
        return persisted
    except ProviderDriverError:
        if not handle.closed:
            handle.close()
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, requests.RequestException, ValueError) as exc:
        if not handle.closed:
            handle.close()
        temporary.unlink(missing_ok=True)
        raise ProviderDriverError("远端产物下载或归档失败", code="artifact_download_failed") from exc
    finally:
        if response is not None:
            response.close()
        if not handle.closed:
            handle.close()


def _variant(attempt: GenerationAttempt) -> tuple[str, int]:
    grouped = attempt.unit_key
    variant = 1
    if ":variant:" in grouped:
        grouped, raw = grouped.rsplit(":variant:", 1)
        try:
            variant = max(1, int(raw))
        except ValueError:
            variant = 1
    return grouped, variant


def verify_remote_artifacts_for_smoke(
    *,
    deployment_id,
    artifacts: list[dict],
    provider_config: dict,
    expected_kind: str,
    max_bytes: int,
) -> list[dict]:
    """Download and validate smoke outputs without making them production artifacts."""
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_REMOTE_ARTIFACTS:
        raise ProviderDriverError("烟测产物数量必须为 1 到 8", code="artifact_manifest_invalid")
    root = Path(settings.MEDIA_ROOT).resolve()
    probe_id = uuid.uuid4()
    base = (root / ".deployment-smoke" / str(deployment_id) / str(probe_id)).resolve()
    if not base.is_relative_to(root):
        raise ProviderDriverError("烟测目录越界", code="artifact_archive_failed")
    facts: list[dict] = []
    paths: list[Path] = []
    try:
        for ordinal, item in enumerate(artifacts, start=1):
            if not isinstance(item, dict) or item.get("kind") != expected_kind:
                raise ProviderDriverError("烟测产物类型与能力不一致", code="artifact_kind_mismatch")
            target = (base / f"artifact-{ordinal}.media").resolve()
            paths.append(target)
            facts.append(_download_one(
                url=str(item.get("url") or ""),
                provider_config=provider_config,
                target=target,
                expected_kind=expected_kind,
                expected_sha256=str(item.get("sha256") or ""),
                max_bytes=max_bytes,
            ))
        return facts
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        try:
            base.rmdir()
            base.parent.rmdir()
        except OSError:
            pass


def archive_remote_artifacts(
    attempt: GenerationAttempt,
    *,
    artifacts: list[dict],
    provider_config: dict,
    expected_kind: str,
    heartbeat=None,
) -> list[GeneratedArtifact]:
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_REMOTE_ARTIFACTS:
        raise ProviderDriverError("远端产物数量必须为 1 到 8", code="artifact_manifest_invalid")
    run = attempt.stage_run.production_run
    max_bytes = _artifact_limit(run)
    root = Path(settings.MEDIA_ROOT).resolve()
    base = (
        root / "generated-artifacts" / str(run.owner_id) / str(run.project_id)
        / "async" / str(attempt.id)
    ).resolve()
    if not base.is_relative_to(root):
        raise ProviderDriverError("远端产物归档目录越界", code="artifact_archive_failed")
    manifest_keys = [f"async:{attempt.id}:{ordinal}" for ordinal in range(1, len(artifacts) + 1)]
    existing = list(GeneratedArtifact.objects.filter(
        manifest_key__in=manifest_keys,
        generation_attempt=attempt,
    ).order_by("manifest_key"))
    if existing and len(existing) != len(manifest_keys):
        raise ProviderDriverError(
            "async 产物归档处于不完整状态，禁止推断或覆盖",
            code="artifact_archive_partial_state",
        )
    if len(existing) == len(manifest_keys):
        verified: list[GeneratedArtifact] = []
        for artifact in existing:
            path = (root / artifact.object_key).resolve()
            if not path.is_relative_to(root):
                raise ProviderDriverError("已有 async 产物路径越界", code="artifact_archive_failed")
            facts = verified_local_media_facts(
                path,
                expected_kind=expected_kind,
                max_bytes=max_bytes,
            )
            if (
                artifact.kind != expected_kind
                or artifact.sha256 != facts["sha256"]
                or artifact.size_bytes != facts["size_bytes"]
                or artifact.mime_type != facts["mime_type"]
            ):
                raise ProviderDriverError("已有 async 产物事实校验失败", code="artifact_archive_failed")
            verified.append(artifact)
        return verified
    downloaded: list[tuple[int, dict, dict, Path]] = []
    unmanaged_paths: list[Path] = []
    try:
        for ordinal, item in enumerate(artifacts, start=1):
            if not isinstance(item, dict) or item.get("kind") != expected_kind:
                raise ProviderDriverError("远端产物类型与请求不一致", code="artifact_kind_mismatch")
            target = (base / f"artifact-{ordinal}.media").resolve()
            if not target.is_relative_to(base):
                raise ProviderDriverError("远端产物路径越界", code="artifact_archive_failed")
            unmanaged_paths.append(target)
            facts = _download_one(
                url=str(item.get("url") or ""),
                provider_config=provider_config,
                target=target,
                expected_kind=expected_kind,
                expected_sha256=str(item.get("sha256") or ""),
                max_bytes=max_bytes,
                heartbeat=heartbeat,
            )
            # No formal artifact row exists on this path yet. It remains an
            # unmanaged staging object even when a previous crashed collect
            # left bytes behind, so cancellation/failure must remove it.
            downloaded.append((ordinal, item, facts, target))
        grouped, variant = _variant(attempt)
        variant_group = f"stage:{attempt.stage_run_id}:unit:{grouped}"
        created: list[GeneratedArtifact] = []
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().select_related(
                "stage_run__production_run",
            ).get(pk=attempt.pk)
            if locked.ignore_late_result:
                raise ProviderDriverError("运行已取消，晚到产物不会进入正式资产", code="late_result_ignored")
            for ordinal, item, facts, target in downloaded:
                metadata = {
                    "schema_version": 1,
                    "source": "restricted_async_rest",
                    "generation_attempt_id": str(locked.id),
                    "provider_request_id": locked.provider_request_id,
                    "remote_artifact_id": str(item.get("artifact_id") or "")[:191],
                    "ordinal": ordinal,
                    "request_sha256": locked.request_sha256,
                }
                contract = json_contract(metadata)
                comparison_parameters = build_comparison_parameters(
                    attempt=locked,
                    stage_run=locked.stage_run,
                )
                relative = target.relative_to(root).as_posix()
                artifact, was_created = GeneratedArtifact.objects.get_or_create(
                    manifest_key=f"async:{locked.id}:{ordinal}",
                    defaults={
                        "production_run": run,
                        "stage_run": locked.stage_run,
                        "generation_attempt": locked,
                        "kind": expected_kind,
                        "role": "primary_generation_output" if ordinal == 1 else "generation_output",
                        "variant_group": variant_group,
                        "variant_index": (variant * 10_000) + ((locked.attempt_number - 1) * 100) + ordinal,
                        # Cost reconciliation and cancellation are still open at
                        # archive time. Selection is promoted atomically with the
                        # successful attempt, never merely because bytes arrived.
                        "status": GeneratedArtifact.Status.AVAILABLE,
                        "storage_backend": "managed_local",
                        "object_key": relative,
                        "mime_type": facts["mime_type"],
                        "size_bytes": facts["size_bytes"],
                        "sha256": facts["sha256"],
                        "manifest_metadata": metadata,
                        "comparison_parameters": comparison_parameters,
                        "manifest_schema_version": contract.schema_version,
                        "manifest_metadata_size_bytes": contract.size_bytes,
                        "manifest_metadata_sha256": contract.sha256,
                    },
                )
                if not was_created and (
                    artifact.object_key != relative
                    or artifact.sha256 != facts["sha256"]
                    or artifact.size_bytes != facts["size_bytes"]
                    or artifact.mime_type != facts["mime_type"]
                ):
                    raise ValidationError("已有 async GeneratedArtifact 与归档字节不一致")
                created.append(artifact)
        return created
    except Exception:
        for path in unmanaged_paths:
            path.unlink(missing_ok=True)
        raise
