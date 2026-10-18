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
from django.db.models import Max

from studio.models import GeneratedArtifact, GenerationAttempt
from studio.services.generated_artifacts import (
    _artifact_limit,
    build_comparison_parameters,
    verified_local_media_facts,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.provider_registry import ProviderDriverError, _peer_address
from studio.services.security_boundaries import boundary_int
from studio.services.bible_reference_contract import BIBLE_IMAGE_GENERATION_STAGE_KEY
from studio.services.execution_production_media import read_prepared_production_media
from studio.services.execution_preparation_owners import locked_preparation_owner_rows


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


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
    try:
        timeout = float(provider_config["timeout_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderDriverError(
            "远端产物下载缺少已冻结 timeout_seconds",
            code="provider_config_invalid",
        ) from exc
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


def download_remote_artifact(**kwargs) -> dict:
    """Public restricted downloader for non-ProductionRun asset jobs."""

    return _download_one(**kwargs)




def verify_remote_artifacts_for_smoke(
    *,
    deployment_id,
    artifacts: list[dict],
    provider_config: dict,
    expected_kind: str,
    max_bytes: int,
) -> list[dict]:
    """Download and validate smoke outputs without making them production artifacts."""
    max_artifacts = boundary_int("security.remote.max_artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= max_artifacts:
        raise ProviderDriverError(
            f"烟测产物数量必须为 1 到当前安全边界 {max_artifacts}",
            code="artifact_manifest_invalid",
        )
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
    request = read_prepared_production_media(attempt)
    if request["driver_type"] != "async_rest":
        raise ValidationError("受限URL归档只接收异步媒体回执，ComfyUI须核对其原工作流输出")
    if request["output_kind"] != expected_kind:
        raise ValidationError("归档类型与本次冻结请求不一致")
    max_artifacts = boundary_int("security.remote.max_artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= max_artifacts:
        raise ProviderDriverError(
            f"远端产物数量必须为 1 到当前安全边界 {max_artifacts}",
            code="artifact_manifest_invalid",
        )
    is_bible_candidate = (
        attempt.stage_run.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
    )
    if is_bible_candidate and (expected_kind != "image" or len(artifacts) != 1):
        raise ProviderDriverError(
            "Bible 参考图 attempt 必须且只能归档一个图片 primary output",
            code="artifact_response_invalid",
        )
    run = attempt.stage_run.production_run
    max_bytes = _artifact_limit(run)
    root = Path(settings.MEDIA_ROOT).resolve()
    tenant_owner_id = run.project.owner_id
    base = (
        root / "generated-artifacts" / str(tenant_owner_id) / str(run.project_id)
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
            metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
                size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
                max_bytes=_stage_snapshot_max_bytes()).value
            if (artifact.production_run_id != run.pk or artifact.stage_run_id != attempt.stage_run_id
                    or any(metadata.get(key) != value for key, value in {
                        "preparation_id": request["preparation_id"], "definition_sha256": request["definition_sha256"],
                        "resolved_inputs_sha256": request["resolved_inputs_sha256"], "request_sha256": attempt.request_sha256,
                        "execution_instance_version_id": str(attempt.execution_instance_version_id),
                        "generation_attempt_id": str(attempt.pk), "unit_key": attempt.unit_key,
                        "production_unit": request["production_unit"]}.items())):
                raise ValidationError("已有媒体归档与本次执行准备的来源不一致")
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
        variant_group = f"stage:{attempt.stage_run_id}:unit:{hashlib.sha256(attempt.unit_key.encode()).hexdigest()}"
        created: list[GeneratedArtifact] = []
        with locked_preparation_owner_rows([("stage_run_id", attempt.stage_run_id)]):
            locked = GenerationAttempt.objects.select_for_update().select_related(
                "stage_run__production_run",
            ).get(pk=attempt.pk)
            if locked.claim_token != attempt.claim_token or locked.status not in {"submitted", "cancel_requested"}:
                raise ProviderDriverError("媒体归档租约已失效", code="async_claim_lost")
            if locked.ignore_late_result or locked.status == "cancel_requested" or locked.stage_run.production_run.status in {"cancelled", "failed", "succeeded"}:
                raise ProviderDriverError("运行已取消，晚到产物不会进入正式资产", code="late_result_ignored")
            last_variant = GeneratedArtifact.objects.filter(stage_run=locked.stage_run, variant_group=variant_group).aggregate(
                value=Max("variant_index"))["value"] or 0
            for ordinal, item, facts, target in downloaded:
                metadata = {
                    "schema_version": 1,
                    "source": "restricted_async_rest",
                    "tenant_owner_id": str(tenant_owner_id),
                    "generation_attempt_id": str(locked.id),
                    "production_run_id": str(run.id),
                    "stage_run_id": str(locked.stage_run_id),
                    "provider_request_id": locked.provider_request_id,
                    "remote_artifact_id": str(item.get("artifact_id") or "")[:191],
                    "ordinal": ordinal,
                    "request_sha256": locked.request_sha256,
                    "preparation_id": request["preparation_id"],
                    "execution_instance_version_id": str(locked.execution_instance_version_id),
                    "definition_sha256": request["definition_sha256"],
                    "resolved_inputs_sha256": request["resolved_inputs_sha256"],
                    "unit_key": locked.unit_key,
                    "production_unit": request["production_unit"],
                }
                contract = json_contract(
                    metadata,
                    max_bytes=_stage_snapshot_max_bytes(),
                )
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
                        "role": (
                            "bible_reference_candidate"
                            if is_bible_candidate
                            else "primary_generation_output" if ordinal == 1 else "generation_output"
                        ),
                        "variant_group": variant_group,
                        "variant_index": last_variant + ordinal,
                        # Cost reconciliation and cancellation are still open at
                        # archive time. Generation never selects a candidate;
                        # adoption belongs to the explicit review action.
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
