from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import math
import os
import platform
import shutil
import subprocess
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from studio.models import (
    ConfigRevision,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
    RecommendationCandidate,
    RecommendationDecision,
    RecommendationSnapshot,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    RuntimeModelAvailability,
    SeriesProject,
    WorkflowTemplateVersion,
)
from studio.services.configuration import (
    ResolvedConfiguration,
    create_config_revision,
    resolve_configuration,
)
from studio.services.async_media_contract import validate_async_model_profile
from studio.services.comfyui_artifacts import (
    _plain_existing_path,
    artifact_inventory_spec,
    controlled_local_model_path,
    controlled_local_models_root,
    object_info_inventory_names,
    safe_model_relative_filename,
)
from studio.services.json_contracts import (
    assign_json_contract,
    canonical_json_bytes,
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.runtime_host_probe_settings import PROBE_SETTING_KEYS, host_probe_settings
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    driver_for,
    resolve_credential,
    validate_deployment_execution_contract,
)
from studio.services.security_boundaries import boundary_int


class RecommendationStale(ValidationError):
    """The frozen recommendation no longer describes executable live state."""


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _memory_total_bytes() -> int:
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
        return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def _gpu_probe(*, timeout_seconds: float) -> tuple[list[dict[str, Any]], str]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
    ):
        raise ValidationError({
            "runtime.gpu_probe_timeout_seconds": "必须是有限正数",
        })
    binary = shutil.which("nvidia-smi")
    if not binary:
        return [], "nvidia-smi_not_found"
    command = [
        binary,
        "--query-gpu=index,name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "nvidia-smi_unavailable"
    if result.returncode != 0:
        return [], "nvidia-smi_failed"
    rows = []
    for raw in csv.reader(io.StringIO(result.stdout)):
        if len(raw) < 5:
            continue
        try:
            rows.append({
                "index": int(raw[0].strip()),
                "name": raw[1].strip()[:200],
                "vram_total_bytes": int(float(raw[2].strip()) * 1024 * 1024),
                "driver_version": raw[3].strip()[:80],
                "compute_capability": raw[4].strip()[:40],
            })
        except (TypeError, ValueError):
            continue
    return rows, "" if rows else "nvidia-smi_no_devices"


def _hash_string_list(values: list[str]) -> str:
    return hashlib.sha256(canonical_json_bytes(sorted(set(values)))).hexdigest()


def _host_fingerprint() -> str:
    facts = {
        "hostname": platform.node(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }
    return hashlib.sha256(canonical_json_bytes(facts)).hexdigest()


def _deployment_is_loopback(deployment: ModelDeployment) -> bool:
    config = {
        **(deployment.profile_version.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }
    hostname = (urlparse(str(config.get("base_url") or "")).hostname or "").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        # Never resolve DNS here: an arbitrary hostname can be rebound after
        # review and is not authoritative proof of a local filesystem scope.
        return False


def _host_probe_is_local(host: RuntimeHost) -> bool:
    deployments = list(ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(
        runtime_host=host,
        status=ModelDeployment.Status.ACTIVE,
        profile_version__provider__driver_type=ProviderProfile.DriverType.COMFYUI,
    ))
    if not deployments:
        return host.key == "local"
    return all(_deployment_is_loopback(item) for item in deployments)


def draft_model_profile_fingerprint(profile: ModelProfileVersion) -> str:
    """Freeze the non-secret draft contract used by model byte attestation."""

    return json_contract({
        "profile_id": str(profile.id),
        "provider_id": str(profile.provider_id),
        "status": profile.status,
        "model_identifier": profile.model_identifier,
        "modality": profile.modality,
        "specification_schema_version": profile.specification_schema_version,
        "specification_size_bytes": profile.specification_size_bytes,
        "specification_sha256": profile.specification_sha256,
    }, max_bytes=_stage_snapshot_max_bytes()).sha256


@dataclass(frozen=True)
class _ObservedFileFacts:
    size_bytes: int
    sha256: str
    path: Path
    fingerprint: tuple[int, int, int, int, int, int]
    allowed_root: Path
    final_path: Path
    stream: Any | None = None

    def verify_unchanged(self) -> None:
        if self.stream is None or self.stream.closed:
            raise OSError("attestation model file handle is no longer open")
        opened = os.fstat(self.stream.fileno())
        if not _plain_existing_path(self.path):
            raise OSError("model path contains a link or reparse point")
        current = self.path.lstat()
        fingerprint = (
            current.st_mode,
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        opened_fingerprint = (
            opened.st_mode,
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or fingerprint != self.fingerprint
            or opened_fingerprint != self.fingerprint
            or _final_path_for_stream(self.stream) != self.final_path
            or not _path_is_within(self.final_path, self.allowed_root)
        ):
            raise OSError("model file changed after hashing")

    def close(self) -> None:
        if self.stream is not None and not self.stream.closed:
            self.stream.close()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    if os.name == "nt":
        return os.path.normcase(common) == os.path.normcase(str(root))
    return common == str(root)


def _normalize_windows_final_path(value: str) -> Path:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _final_path_for_stream(stream) -> Path:
    if os.name != "nt":
        proc_path = Path(f"/proc/self/fd/{stream.fileno()}")
        try:
            return Path(os.readlink(proc_path)).resolve(strict=True)
        except OSError:
            return Path(stream.name).resolve(strict=True)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    size = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if size == 0 or size >= len(buffer):
        raise OSError(ctypes.get_last_error(), "cannot resolve opened model handle")
    return _normalize_windows_final_path(buffer.value)


def _open_model_file(path: Path, *, allowed_root: Path):
    path = Path(path)
    allowed_root = Path(allowed_root)
    if (
        not path.is_absolute()
        or not allowed_root.is_absolute()
        or not _path_is_within(path, allowed_root)
        or not _plain_existing_path(path)
    ):
        raise OSError("model path is outside the controlled root or contains a link")
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            stream = os.fdopen(descriptor, "rb")
            final_path = _final_path_for_stream(stream)
            if not _path_is_within(final_path, allowed_root):
                raise OSError("opened model file escaped the controlled root")
            return stream, final_path
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_sequential_scan = 0x08000000
    file_attribute_reparse_point = 0x00000400
    invalid_handle_value = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        generic_read,
        file_share_read,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_sequential_scan,
        None,
    )
    if handle == invalid_handle_value:
        raise OSError(ctypes.get_last_error(), "cannot open controlled model file")

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        )

    try:
        info = FileAttributeTagInfo()
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            9,  # FileAttributeTagInfo
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise OSError(ctypes.get_last_error(), "cannot inspect model file handle")
        if info.file_attributes & file_attribute_reparse_point:
            raise OSError("model file is a reparse point")
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        handle = None  # descriptor now owns the native handle
        stream = os.fdopen(descriptor, "rb")
        final_path = _final_path_for_stream(stream)
        if not _path_is_within(final_path, allowed_root):
            stream.close()
            raise OSError("opened model file escaped the controlled root")
        return stream, final_path
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _observed_file_facts(
    path: Path,
    *,
    lease_renew: Callable[[], None] | None = None,
    allowed_root: Path | None = None,
    hold_open: bool = False,
) -> _ObservedFileFacts:
    digest = hashlib.sha256()
    size = 0
    path = Path(path)
    root = Path(allowed_root or path.parent).resolve(strict=True)
    stream, final_path = _open_model_file(path, allowed_root=root)
    try:
        opened_before = os.fstat(stream.fileno())
        path_before = path.lstat()
        before_fingerprint = (
            opened_before.st_mode,
            opened_before.st_dev,
            opened_before.st_ino,
            opened_before.st_size,
            opened_before.st_mtime_ns,
            opened_before.st_ctime_ns,
        )
        if before_fingerprint != (
            path_before.st_mode,
            path_before.st_dev,
            path_before.st_ino,
            path_before.st_size,
            path_before.st_mtime_ns,
            path_before.st_ctime_ns,
        ) or not stat.S_ISREG(opened_before.st_mode):
            raise OSError("model file changed before hashing")
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if lease_renew is not None:
                lease_renew()
        opened_after = os.fstat(stream.fileno())
        path_after = path.lstat()
        after_fingerprint = (
            opened_after.st_mode,
            opened_after.st_dev,
            opened_after.st_ino,
            opened_after.st_size,
            opened_after.st_mtime_ns,
            opened_after.st_ctime_ns,
        )
        if (
            before_fingerprint != after_fingerprint
            or before_fingerprint != (
                path_after.st_mode,
                path_after.st_dev,
                path_after.st_ino,
                path_after.st_size,
                path_after.st_mtime_ns,
                path_after.st_ctime_ns,
            )
            or size != opened_after.st_size
        ):
            raise OSError("model file changed while hashing")
        facts = _ObservedFileFacts(
            size_bytes=size,
            sha256=digest.hexdigest(),
            path=path,
            fingerprint=before_fingerprint,
            allowed_root=root,
            final_path=final_path,
            stream=stream if hold_open else None,
        )
        if not hold_open:
            stream.close()
        return facts
    except Exception:
        stream.close()
        raise


def _close_availability_file_facts(availability) -> None:
    seen: set[int] = set()
    for item in availability or []:
        file_facts = item.get("file_facts") if isinstance(item, dict) else None
        if isinstance(file_facts, _ObservedFileFacts) and id(file_facts) not in seen:
            seen.add(id(file_facts))
            file_facts.close()


@contextmanager
def _close_file_facts_on_error(availability):
    try:
        yield
    except BaseException:
        _close_availability_file_facts(availability)
        raise


def verified_artifact_availability(
    *,
    snapshot: HostCapabilitySnapshot,
    deployment: ModelDeployment,
    artifact: ModelArtifact,
) -> RuntimeModelAvailability:
    """Return exact, fresh per-host byte evidence or fail closed."""

    verify_host_snapshot(snapshot)
    if deployment.runtime_host_id is None or snapshot.host_id != deployment.runtime_host_id:
        raise ValidationError(
            "artifact evidence 的主机与 deployment 不一致",
            code="artifact_host_mismatch",
        )
    latest = HostCapabilitySnapshot.objects.filter(host_id=snapshot.host_id).order_by(
        "-captured_at", "-revision",
    ).first()
    if latest is None or latest.id != snapshot.id:
        raise ValidationError(
            "artifact evidence 不是该主机最新快照",
            code="host_snapshot_not_latest",
        )
    if snapshot.expires_at is None or snapshot.expires_at <= timezone.now():
        raise ValidationError("artifact evidence 已过期", code="host_snapshot_stale")
    if snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED:
        raise ValidationError(
            "artifact evidence 所属主机探测失败",
            code="host_probe_failed",
        )
    if artifact.profile_version_id != deployment.profile_version_id:
        raise ValidationError(
            "artifact 与 deployment model profile 不一致",
            code="artifact_profile_mismatch",
        )
    availability = RuntimeModelAvailability.objects.filter(
        snapshot=snapshot,
        deployment=deployment,
        artifact=artifact,
    ).first()
    trusted_sources = {
        RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        RuntimeModelAvailability.ObservationSource.REMOTE_ATTESTATION,
    }
    if (
        availability is None
        or availability.status != RuntimeModelAvailability.Status.AVAILABLE
        or availability.observation_source not in trusted_sources
        or availability.observed_size_bytes is None
        or not availability.observed_sha256
        or artifact.size_bytes is None
        or not artifact.sha256
        or availability.observed_size_bytes != artifact.size_bytes
        or availability.observed_sha256 != artifact.sha256
        or (
            availability.observation_source
            == RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
            and not _deployment_is_loopback(deployment)
        )
    ):
        raise ValidationError(
            "artifact 缺少与声明一致的主机观测 size/hash",
            code="artifact_observation_unverified",
        )
    return availability


def validate_draft_profile_artifacts_for_publish(
    profile: ModelProfileVersion,
    *,
    artifacts: list[ModelArtifact] | None = None,
) -> None:
    """Require fresh loopback byte evidence before freezing Comfy artifacts."""

    if profile.provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        return
    if artifacts is None:
        artifacts = list(profile.artifacts.select_related(
            "verified_snapshot__probe_attempt",
            "verified_snapshot__host",
        ).all())
    else:
        artifacts = list(artifacts)
    if not artifacts:
        return
    now = timezone.now()
    for artifact in artifacts:
        snapshot = artifact.verified_snapshot
        if (
            artifact.status != ModelArtifact.Status.AVAILABLE
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or not artifact.sha256
            or snapshot is None
            or snapshot.expires_at is None
            or snapshot.expires_at <= now
            or snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED
        ):
            raise ValidationError(
                f"artifact {artifact.logical_name} 缺少新鲜的本机 size/hash 证明",
            )

    snapshot_ids = {artifact.verified_snapshot_id for artifact in artifacts}
    if len(snapshot_ids) != 1:
        raise ValidationError("draft profile 的全部 artifact 必须由同一次证明快照覆盖")
    snapshot = artifacts[0].verified_snapshot
    verify_host_snapshot(snapshot)
    attempt = snapshot.probe_attempt
    if (
        attempt is None
        or attempt.probe_kind
        != RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
        or attempt.status != RuntimeHostProbeAttempt.Status.SUCCEEDED
        or attempt.host_id != snapshot.host_id
    ):
        raise ValidationError("draft profile 的证明不是成功的草稿模型证明任务")
    verify_json_contract(
        attempt.request_snapshot,
        schema_version=attempt.request_snapshot_schema_version,
        size_bytes=attempt.request_snapshot_size_bytes,
        sha256=attempt.request_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    request = attempt.request_snapshot
    if request.get("request_contract_sha256") != json_contract(
        {
            key: value
            for key, value in request.items()
            if key != "request_contract_sha256"
        },
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256:
        raise ValidationError("draft profile 的证明 request contract 已损坏")
    artifact_rows = request.get("artifacts")
    if not isinstance(artifact_rows, list):
        raise ValidationError("draft profile 的证明缺少 artifact closure")
    current_ids = {str(artifact.id) for artifact in artifacts}
    frozen_ids = {
        str(row.get("artifact_id") or "")
        for row in artifact_rows
        if isinstance(row, dict)
    }
    if (
        len(frozen_ids) != len(artifact_rows)
        or frozen_ids != current_ids
        or str(request.get("target_model_profile_version_id") or "")
        != str(profile.id)
        or request.get("target_profile_fingerprint_sha256")
        != draft_model_profile_fingerprint(profile)
    ):
        raise ValidationError("draft profile 当前 artifact 与证明冻结 closure 不一致")

    for artifact in artifacts:
        matching_rows = [
            row for row in artifact_rows
            if isinstance(row, dict)
            and str(row.get("artifact_id") or "") == str(artifact.id)
        ]
        if (
            len(matching_rows) != 1
            or matching_rows[0].get("role") != artifact.role
            or matching_rows[0].get("filename") != artifact.filename
            or matching_rows[0].get("inventory_folder") != artifact.inventory_folder
            or matching_rows[0].get("inventory_node_type")
            != artifact.inventory_node_type
            or matching_rows[0].get("inventory_input_name")
            != artifact.inventory_input_name
        ):
            raise ValidationError(
                f"artifact {artifact.logical_name} 不属于该证明任务冻结的精确 closure",
            )
        availability = RuntimeModelAvailability.objects.select_related(
            "deployment__profile_version__provider",
        ).filter(
            snapshot=snapshot,
            artifact=artifact,
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
            observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256,
        ).first()
        if (
            availability is None
            or str(request.get("source_deployment_id") or "")
            != str(availability.deployment_id)
            or availability.deployment.status != ModelDeployment.Status.ACTIVE
            or availability.deployment.runtime_host_id != snapshot.host_id
            or availability.deployment.profile_version.provider_id != profile.provider_id
            or not _deployment_is_loopback(availability.deployment)
            or not _host_probe_is_local(snapshot.host)
        ):
            raise ValidationError(
                f"artifact {artifact.logical_name} 的证明不是同 provider loopback 主机事实",
            )
        # A mutable deployment FK alone is not provenance.  The source
        # endpoint/provider/runtime-host contract must still equal the frozen
        # request when the draft profile is published.
        from studio.services.comfyui_workflow_imports import (
            source_deployment_fingerprint,
        )
        if (
            source_deployment_fingerprint(availability.deployment)
            != request.get("source_deployment_fingerprint_sha256")
        ):
            raise ValidationError(
                f"artifact {artifact.logical_name} 的 source deployment 已变化",
            )


def required_workflow_artifacts(
    workflow: WorkflowTemplateVersion,
) -> list[ModelArtifact]:
    """Resolve the exact required artifact closure declared by one workflow."""

    rows = list(workflow.model_requirements.select_related("artifact").filter(
        is_required=True,
    ))
    if not rows:
        raise ValidationError("workflow 未声明 required model artifact")
    artifacts: list[ModelArtifact] = []
    seen: set[Any] = set()
    for row in rows:
        artifact = row.artifact
        if artifact is None:
            raise ValidationError("workflow required model artifact 未绑定")
        if artifact.profile_version_id != workflow.model_profile_version_id:
            raise ValidationError("workflow required artifact 不属于绑定 model profile")
        if artifact.filename != row.expected_filename:
            raise ValidationError("workflow required artifact 文件名已漂移")
        if (
            not row.expected_sha256
            or not artifact.sha256
            or row.expected_sha256 != artifact.sha256
        ):
            raise ValidationError("workflow required artifact hash 缺失或漂移")
        if artifact.id not in seen:
            seen.add(artifact.id)
            artifacts.append(artifact)
    return artifacts


def _comfy_probe(
    host: RuntimeHost,
    *,
    lease_renew: Callable[[], None] | None = None,
    attestation_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    deployments = list(
        ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).filter(
            runtime_host=host,
            status=ModelDeployment.Status.ACTIVE,
            profile_version__provider__driver_type="comfyui",
        ).order_by("-is_default", "-priority", "key")
    )
    if not deployments:
        return {}, [], ["comfyui_deployment_missing"]
    errors: list[str] = []
    availability: list[dict[str, Any]] = []
    deployment_details: list[dict[str, Any]] = []
    attestation_deployment_id = (
        attestation_context["source_deployment"].id
        if isinstance(attestation_context, dict) else None
    )
    attestation_artifacts = {
        artifact.id: artifact
        for artifact in (
            attestation_context.get("artifacts", [])
            if isinstance(attestation_context, dict) else []
        )
    }
    for deployment in deployments:
        if lease_renew is not None:
            lease_renew()
        driver = driver_for(deployment.profile_version.provider)
        local_filesystem = _deployment_is_loopback(deployment)
        try:
            health = driver.invoke(deployment, "health", {})
            if lease_renew is not None:
                lease_renew()
            object_info = driver.invoke(deployment, "object_info", {})
        except Exception:
            errors.append(f"comfyui_probe_failed:{deployment.key}")
            deployment_details.append({
                "deployment_id": str(deployment.id),
                "deployment_key": deployment.key,
                "status": "unavailable",
                "reason_code": "comfyui_probe_failed",
            })
            artifacts = list(ModelArtifact.objects.filter(
                profile_version=deployment.profile_version,
            ))
            if deployment.id == attestation_deployment_id:
                artifacts = list({
                    artifact.id: artifact
                    for artifact in (*artifacts, *attestation_artifacts.values())
                }.values())
            for artifact in artifacts:
                availability.append({
                    "deployment": deployment,
                    "artifact": artifact,
                    "key": f"deployment:{deployment.id}:artifact:{artifact.id}",
                    "status": RuntimeModelAvailability.Status.UNKNOWN,
                    "reason": "ComfyUI 部署探测失败",
                    "observed_size_bytes": None,
                    "observed_sha256": "",
                    "observation_source": RuntimeModelAvailability.ObservationSource.NAME_ONLY,
                    "is_attestation": (
                        deployment.id == attestation_deployment_id
                        and artifact.id in attestation_artifacts
                    ),
                    "file_facts": None,
                })
            continue
        system = health.get("system") if isinstance(health, dict) else {}
        node_types = sorted(object_info) if isinstance(object_info, dict) else []
        artifacts = list(ModelArtifact.objects.filter(profile_version=deployment.profile_version))
        if deployment.id == attestation_deployment_id:
            artifacts = list({
                artifact.id: artifact
                for artifact in (*artifacts, *attestation_artifacts.values())
            }.values())
        inventory_specs = {}
        for artifact in artifacts:
            try:
                inventory_specs[artifact.id] = artifact_inventory_spec(artifact)
            except ValueError:
                inventory_specs[artifact.id] = None
        folder_values: dict[str, set[str]] = {}
        folder_summaries: dict[str, dict[str, Any]] = {}
        deployment_errors: list[str] = []
        model_api_folders = sorted({
            spec.folder
            for spec in inventory_specs.values()
            if spec is not None and spec.source == "models_api"
        })
        for folder in model_api_folders:
            if lease_renew is not None:
                lease_renew()
            try:
                response = driver.invoke(deployment, "models", {"folder": folder})
                items = response.get("items") if isinstance(response, dict) else []
                values = {
                    normalized
                    for item in items
                    if isinstance(item, str)
                    for normalized in [safe_model_relative_filename(item)]
                    if normalized is not None
                }
                folder_values[folder] = values
                folder_summaries[folder] = {
                    "count": len(values),
                    "sha256": _hash_string_list(list(values)),
                }
            except Exception:
                code = f"comfyui_models_{folder}_unavailable:{deployment.key}"
                errors.append(code)
                deployment_errors.append(code.split(":", 1)[0])
                folder_values[folder] = set()
        for artifact in artifacts:
            spec = inventory_specs[artifact.id]
            availability_key = f"deployment:{deployment.id}:artifact:{artifact.id}"
            if artifact.status in {ModelArtifact.Status.INVALID, ModelArtifact.Status.MISSING}:
                availability.append({
                    "deployment": deployment,
                    "artifact": artifact,
                    "key": availability_key,
                    "status": RuntimeModelAvailability.Status.INCOMPATIBLE,
                    "reason": "artifact 注册状态不可用",
                    "observed_size_bytes": None,
                    "observed_sha256": "",
                    "observation_source": RuntimeModelAvailability.ObservationSource.NAME_ONLY,
                    "is_attestation": (
                        deployment.id == attestation_deployment_id
                        and artifact.id in attestation_artifacts
                    ),
                    "file_facts": None,
                })
                continue
            if spec is None:
                availability.append({
                    "deployment": deployment,
                    "artifact": artifact,
                    "key": availability_key,
                    "status": RuntimeModelAvailability.Status.UNKNOWN,
                    "reason": "artifact 缺少合法的受控库存目录或 object_info selector",
                    "observed_size_bytes": None,
                    "observed_sha256": "",
                    "observation_source": RuntimeModelAvailability.ObservationSource.NAME_ONLY,
                    "is_attestation": (
                        deployment.id == attestation_deployment_id
                        and artifact.id in attestation_artifacts
                    ),
                    "file_facts": None,
                })
                continue
            folder = spec.folder
            artifact_filename = safe_model_relative_filename(artifact.filename)
            observed_size = None
            observed_sha = ""
            file_facts = None
            observation_source = RuntimeModelAvailability.ObservationSource.NAME_ONLY
            if spec.source == "object_info":
                try:
                    reported_names = object_info_inventory_names(object_info, spec)
                except ValueError:
                    reported_names = set()
                    catalog_unavailable = True
                    selector_key = f"object_info:{spec.node_type}.{spec.input_name}"
                    deployment_errors.append("comfyui_custom_inventory_unavailable")
                    errors.append(f"comfyui_custom_inventory_unavailable:{deployment.key}")
                else:
                    catalog_unavailable = False
                    selector_key = f"object_info:{spec.node_type}.{spec.input_name}"
                    folder_summaries[selector_key] = {
                        "count": len(reported_names),
                        "sha256": _hash_string_list(list(reported_names)),
                    }
            else:
                reported_names = folder_values.get(folder, set())
                catalog_unavailable = (
                    folder not in folder_values
                    or any(
                        error.startswith(f"comfyui_models_{folder}_unavailable")
                        for error in deployment_errors
                    )
                )
            if artifact_filename is None:
                state = RuntimeModelAvailability.Status.INCOMPATIBLE
                reason = "artifact filename 不是受控 models 相对路径"
            elif catalog_unavailable:
                state = RuntimeModelAvailability.Status.UNKNOWN
                reason = "ComfyUI 未返回该 artifact 的受控库存"
            elif artifact_filename in reported_names and not local_filesystem:
                state = RuntimeModelAvailability.Status.UNKNOWN
                reason = "远程 ComfyUI 仅报告文件名，缺少受认证 size/hash 证明"
            elif artifact_filename in reported_names:
                candidate = controlled_local_model_path(folder, artifact_filename)
                if candidate is None:
                    state = RuntimeModelAvailability.Status.INCOMPATIBLE
                    reason = "本地模型路径不在受控 Comfy 根或文件不可读"
                else:
                    is_attestation = (
                        deployment.id == attestation_deployment_id
                        and artifact.id in attestation_artifacts
                    )
                    try:
                        file_facts = _observed_file_facts(
                            candidate,
                            lease_renew=lease_renew,
                            allowed_root=controlled_local_models_root(),
                            hold_open=is_attestation,
                        )
                    except OSError:
                        state = RuntimeModelAvailability.Status.UNKNOWN
                        reason = "本地模型文件无法读取"
                    else:
                        observed_size = file_facts.size_bytes
                        observed_sha = file_facts.sha256
                        observation_source = (
                            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
                        )
                        if (
                            is_attestation
                            and artifact.sha256
                            and artifact.sha256 != observed_sha
                        ) or (
                            is_attestation
                            and artifact.size_bytes is not None
                            and artifact.size_bytes != observed_size
                        ):
                            state = RuntimeModelAvailability.Status.INCOMPATIBLE
                            reason = "草稿 artifact 的既有 size/hash 与主机观测不一致"
                        elif is_attestation:
                            state = RuntimeModelAvailability.Status.AVAILABLE
                            reason = "草稿 artifact 已从受控本地文件系统生成 size/hash 证明"
                        elif not artifact.sha256 or artifact.size_bytes is None:
                            state = RuntimeModelAvailability.Status.INCOMPATIBLE
                            reason = "artifact 声明缺少生产必需的 size/hash"
                        elif (
                            observed_sha != artifact.sha256
                            or observed_size != artifact.size_bytes
                        ):
                            state = RuntimeModelAvailability.Status.INCOMPATIBLE
                            reason = "主机观测 size/hash 与 artifact 声明不一致"
                        else:
                            state = RuntimeModelAvailability.Status.AVAILABLE
                            reason = "主机已从受控本地文件系统验证 size/hash"
            else:
                state = RuntimeModelAvailability.Status.MISSING
                reason = "运行节点未报告该文件"
            availability.append({
                "deployment": deployment,
                "artifact": artifact,
                "key": availability_key,
                "status": state,
                "reason": reason,
                "observed_size_bytes": observed_size,
                "observed_sha256": observed_sha,
                "observation_source": observation_source,
                "is_attestation": (
                    deployment.id == attestation_deployment_id
                    and artifact.id in attestation_artifacts
                ),
                # Internal only.  This object is never serialized into details or
                # RuntimeModelAvailability; it carries the opened-file identity
                # across the network probe to the fenced DB commit.
                "file_facts": file_facts,
            })
        deployment_details.append({
            "deployment_id": str(deployment.id),
            "deployment_key": deployment.key,
            "status": "partial" if deployment_errors else "available",
            "version": str((system or {}).get("comfyui_version") or ""),
            "node_count": len(node_types),
            "node_types_sha256": _hash_string_list(node_types),
            "model_folders": folder_summaries,
            "reason_codes": sorted(set(deployment_errors)),
        })
    available_count = sum(item["status"] == "available" for item in deployment_details)
    aggregate_status = (
        "available" if available_count == len(deployment_details)
        else "partial" if available_count or any(item["status"] == "partial" for item in deployment_details)
        else "unavailable"
    )
    first_version = next(
        (str(item.get("version") or "") for item in deployment_details if item.get("version")),
        "",
    )
    return {
        "status": aggregate_status,
        "version": first_version,
        "deployment_count": len(deployment_details),
        "deployments": deployment_details,
    }, availability, errors


def probe_runtime_host(
    host: RuntimeHost,
    *,
    lease_renew: Callable[[], None] | None = None,
    probe_attempt: RuntimeHostProbeAttempt | None = None,
    commit_guard: Callable[[], None] | None = None,
    attestation_context: dict[str, Any] | None = None,
) -> HostCapabilitySnapshot:
    host = RuntimeHost.objects.get(pk=host.pk)
    if probe_attempt is not None and probe_attempt.host_id != host.pk:
        raise ValidationError("runtime host probe attempt 与目标主机不一致")
    if host.status == RuntimeHost.Status.DISABLED:
        raise ValidationError("已禁用主机不能执行探测；请先通过受控状态入口启用")
    if lease_renew is not None:
        lease_renew()
    configuration = resolve_configuration(required_keys=(
        *PROBE_SETTING_KEYS,
        "runtime.gpu_probe_timeout_seconds",
    ))
    probe_mode, probe_validity = host_probe_settings(configuration.values)
    gpu_probe_timeout_seconds = float(
        configuration.values["runtime.gpu_probe_timeout_seconds"]
    )
    errors: list[str] = []
    local_probe = _host_probe_is_local(host)
    if local_probe:
        gpus, gpu_error = _gpu_probe(
            timeout_seconds=gpu_probe_timeout_seconds,
        )
        if gpu_error:
            errors.append(gpu_error)
    else:
        gpus = []
        errors.append("remote_host_attestation_unavailable")
    if lease_renew is not None:
        lease_renew()
    storage_root = Path(settings.MEDIA_ROOT)
    disk_total = 0
    disk_free = 0
    if local_probe:
        try:
            disk = shutil.disk_usage(storage_root)
            disk_total, disk_free = int(disk.total), int(disk.free)
        except OSError:
            errors.append("media_storage_unavailable")
    comfy, availability, comfy_errors = _comfy_probe(
        host,
        lease_renew=lease_renew,
        attestation_context=attestation_context,
    )
    errors.extend(comfy_errors)
    attestation_items: list[dict[str, Any]] = []
    attestation_complete = False
    if isinstance(attestation_context, dict):
        expected_ids = {
            artifact.id for artifact in attestation_context.get("artifacts", [])
        }
        attestation_items = [
            item for item in availability if item.get("is_attestation")
        ]
        observed_ids = [item["artifact"].id for item in attestation_items]
        if (
            not expected_ids
            or len(observed_ids) != len(expected_ids)
            or set(observed_ids) != expected_ids
        ):
            _close_availability_file_facts(availability)
            raise ValidationError(
                "draft artifact attestation 未形成精确且唯一的 artifact closure",
            )
        attestation_complete = all(
            item["status"] == RuntimeModelAvailability.Status.AVAILABLE
            and item["observation_source"]
            == RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
            and item["observed_size_bytes"] is not None
            and bool(item["observed_sha256"])
            and isinstance(item.get("file_facts"), _ObservedFileFacts)
            for item in attestation_items
        )
        if not attestation_complete:
            errors.append("draft_artifact_attestation_incomplete")
    details = {
        "schema_version": 1,
        "probe_scope": "local_process" if local_probe else "remote_unattested",
        "os": ({
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        } if local_probe else {}),
        "gpus": gpus,
        "comfyui": comfy,
        "probe_errors": sorted(set(errors)),
        "runtime_configuration": {
            "resolution_sha256": configuration.sha256,
            "gpu_probe_timeout_seconds": gpu_probe_timeout_seconds,
            "host_probe_mode": probe_mode,
            "host_probe_validity_seconds": int(probe_validity.total_seconds()),
        },
    }
    try:
        details_contract = json_contract(
            details,
            max_bytes=_stage_snapshot_max_bytes(),
        )
    except BaseException:
        _close_availability_file_facts(availability)
        raise
    core_ok = bool(local_probe and os.cpu_count() and _memory_total_bytes() and disk_total)
    status = (
        HostCapabilitySnapshot.ProbeStatus.SUCCEEDED
        if core_ok and not errors
        else HostCapabilitySnapshot.ProbeStatus.PARTIAL
        if core_ok
        else HostCapabilitySnapshot.ProbeStatus.FAILED
    )
    captured_at = timezone.now()
    if lease_renew is not None:
        try:
            lease_renew()
        except BaseException:
            _close_availability_file_facts(availability)
            raise
    with _close_file_facts_on_error(availability), transaction.atomic():
        host = RuntimeHost.objects.select_for_update().get(pk=host.pk)
        if host.status == RuntimeHost.Status.DISABLED:
            raise ValidationError("主机在探测期间已被禁用；探测结果未写入")
        if commit_guard is not None:
            # The durable attempt claim is fenced inside the same transaction
            # that persists the immutable snapshot. A cancelled or recovered
            # worker therefore cannot commit a late result.
            try:
                commit_guard()
            except OSError as exc:
                raise ValidationError(
                    "attestation 模型文件在提交围栏期间无法保持不变",
                ) from exc
        # Re-check the exact path identity after the durable claim and source /
        # profile / artifact declarations have been locked.  A replacement in
        # the gap between closing the hashed fd and this transaction therefore
        # yields no snapshot and no artifact mutation.
        for item in attestation_items:
            file_facts = item.get("file_facts")
            if file_facts is None:
                continue
            try:
                file_facts.verify_unchanged()
            except OSError as exc:
                raise ValidationError(
                    "attestation 模型文件在 hash 后、提交前发生变化",
                ) from exc
        next_revision = (
            HostCapabilitySnapshot.objects.filter(host=host)
            .aggregate(value=Max("revision"))["value"]
            or 0
        ) + 1
        snapshot = HostCapabilitySnapshot.objects.create(
            host=host,
            probe_attempt=probe_attempt,
            revision=next_revision,
            probe_status=status,
            cpu_architecture=platform.machine()[:80] if local_probe else "",
            cpu_name=(platform.processor() or platform.machine())[:200] if local_probe else "",
            logical_cpu_count=int(os.cpu_count() or 0) if local_probe else 0,
            memory_total_bytes=_memory_total_bytes() if local_probe else 0,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            gpu_count=len(gpus),
            ffmpeg_available=bool(
                local_probe and shutil.which(str(getattr(settings, "FFMPEG_BINARY", "ffmpeg")))
            ),
            ffprobe_available=bool(
                local_probe and shutil.which(str(getattr(settings, "FFPROBE_BINARY", "ffprobe")))
            ),
            comfyui_version=str(comfy.get("version") or "")[:120],
            details=details,
            details_schema_version=details_contract.schema_version,
            details_size_bytes=details_contract.size_bytes,
            details_sha256=details_contract.sha256,
            captured_at=captured_at,
            expires_at=captured_at + probe_validity,
        )
        RuntimeModelAvailability.objects.bulk_create([
            RuntimeModelAvailability(
                snapshot=snapshot,
                deployment=item["deployment"],
                artifact=item["artifact"],
                availability_key=item["key"],
                status=item["status"],
                reason=item["reason"],
                observed_size_bytes=item["observed_size_bytes"],
                observed_sha256=item["observed_sha256"],
                observation_source=item["observation_source"],
            )
            for item in availability
        ])
        if attestation_complete:
            items_by_artifact = {
                item["artifact"].id: item for item in attestation_items
            }
            locked_artifacts = list(
                ModelArtifact.objects.select_for_update()
                .select_related("profile_version")
                .filter(pk__in=sorted(items_by_artifact, key=str))
                .order_by("pk")
            )
            if len(locked_artifacts) != len(items_by_artifact):
                raise ValidationError("attestation artifact closure 在提交前发生变化")
            target_profile_id = attestation_context["target_profile"].id
            for artifact in locked_artifacts:
                item = items_by_artifact[artifact.id]
                if (
                    artifact.profile_version_id != target_profile_id
                    or artifact.profile_version.status
                    != ModelProfileVersion.Status.DRAFT
                ):
                    raise ValidationError("attestation 目标 model profile 已不再是 draft")
                if artifact.sha256 and artifact.sha256 != item["observed_sha256"]:
                    raise ValidationError("attestation artifact hash 在提交前发生变化")
                if (
                    artifact.size_bytes is not None
                    and artifact.size_bytes != item["observed_size_bytes"]
                ):
                    raise ValidationError("attestation artifact size 在提交前发生变化")
            # This second loop is intentionally separate: all declarations are
            # checked before the first row is mutated, while transaction.atomic
            # still guarantees rollback if a later save fails.
            for artifact in locked_artifacts:
                item = items_by_artifact[artifact.id]
                artifact.sha256 = item["observed_sha256"]
                artifact.size_bytes = item["observed_size_bytes"]
                artifact.status = ModelArtifact.Status.AVAILABLE
                artifact.verified_snapshot = snapshot
                artifact.verified_at = captured_at
                artifact.verified_by = attestation_context.get("verified_by")
                artifact.save(update_fields=(
                    "sha256", "size_bytes", "status", "verified_snapshot",
                    "verified_at", "verified_by", "updated_at",
                ))
        if local_probe:
            host.name = platform.node() or host.name
            host.fingerprint_sha256 = _host_fingerprint()
        host.status = RuntimeHost.Status.ACTIVE if core_ok else RuntimeHost.Status.UNAVAILABLE
        host.last_seen_at = captured_at
        host_fields = ["status", "last_seen_at", "updated_at"]
        if local_probe:
            host_fields.extend(("name", "fingerprint_sha256"))
        host.save(update_fields=host_fields)
    _close_availability_file_facts(availability)
    return snapshot


def probe_local_host() -> HostCapabilitySnapshot:
    host, _ = RuntimeHost.objects.get_or_create(
        key="local",
        defaults={"name": platform.node() or "Local runtime host"},
    )
    return probe_runtime_host(host)


def verify_host_snapshot(snapshot: HostCapabilitySnapshot) -> None:
    verify_json_contract(
        snapshot.details,
        schema_version=snapshot.details_schema_version,
        size_bytes=snapshot.details_size_bytes,
        sha256=snapshot.details_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )


def _text_resource_candidate_state(deployment):
    """A resource recommendation, not permission for any instance to execute."""
    from studio.services.execution_deployment_resource_evidence import current_resource_smoke
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
    from studio.services.execution_text_contracts import TEXT_EXECUTION_OPERATIONS
    try:
        driver_for(deployment.provider).validate_execution_credentials(deployment)
    except (ProviderDriverError, ValidationError):
        return RecommendationCandidate.Status.UNAVAILABLE, "text_credential_unavailable"
    try:
        specification = verified_model_profile_specification(deployment.profile_version)
        capabilities = specification.get("capabilities")
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise ValidationError("模型缺少明确能力声明")
        operations = sorted(set(capabilities) & {*NATIVE_TEXT_OPERATIONS, *TEXT_EXECUTION_OPERATIONS})
    except ValidationError:
        return RecommendationCandidate.Status.UNAVAILABLE, "model_profile_contract_invalid"
    passed, damaged = False, False
    for operation in operations:
        try:
            passed = current_resource_smoke(deployment, operation) is not None or passed
        except ValidationError:
            damaged = True
    # Evaluate all operations before concluding; the order of declarations
    # must not decide whether a different, valid capability can be recommended.
    if passed:
        return RecommendationCandidate.Status.COMPATIBLE, "text_resource_smoke_passed"
    if damaged:
        return RecommendationCandidate.Status.UNAVAILABLE, "resource_smoke_evidence_invalid"
    return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "text_resource_smoke_required"


def capability_readiness(snapshot: HostCapabilitySnapshot) -> dict[str, Any]:
    from studio.services.execution_deployment_resource_evidence import current_resource_smoke
    verify_host_snapshot(snapshot)
    deployments = list(ModelDeployment.objects.filter(
        Q(runtime_host=snapshot.host) | Q(runtime_host__isnull=True),
        status=ModelDeployment.Status.ACTIVE,
        profile_version__status="published",
        profile_version__provider__is_active=True,
    ).select_related("profile_version__provider").prefetch_related("profile_version__artifacts"))
    stale = bool(snapshot.expires_at and snapshot.expires_at <= timezone.now())

    def deployment_state(deployment: ModelDeployment, modality: str) -> tuple[str, str]:
        provider = deployment.profile_version.provider
        if modality == "text" and provider.driver_type in {
                ProviderProfile.DriverType.OPENAI_COMPATIBLE, ProviderProfile.DriverType.DEEPSEEK_API,
                ProviderProfile.DriverType.LOCAL_QWEN}:
            if deployment.runtime_host_id is not None and (stale or snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED):
                return "blocked", "主机能力快照过期或失败"
            status, _reason = _text_resource_candidate_state(deployment)
            if status == RecommendationCandidate.Status.COMPATIBLE:
                return "ready", "至少一项文本能力具有有效基础烟测；业务执行仍需核对精确实例组合"
            if status == RecommendationCandidate.Status.SMOKE_TEST_REQUIRED:
                return "smoke_required", "文本资源尚缺对应能力的有效基础烟测，连接健康不代表能力验收通过"
            return "blocked", "文本资源的配置、凭据或基础证据不可核验，请前往能力验收查看"
        if deployment.runtime_host_id is None:
            if provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
                return "smoke_required", "云端 deployment 尚无受控 async smoke 证据"
            reference = deployment.credential or provider.credential
            try:
                if not reference:
                    raise ProviderDriverError("credential missing", code="credential_missing")
                resolve_credential(reference)
                operations = validate_async_model_profile(deployment.profile_version)
            except (ProviderDriverError, ValidationError):
                return "blocked", "云端 deployment 凭据或能力契约不可用"
            relevant = [item for item in operations if item.startswith(f"{modality}.")]
            if not relevant:
                return "blocked", "云端 deployment 未声明该能力"
            try:
                if any(current_resource_smoke(deployment, operation) for operation in relevant):
                    return "ready", "云端资源的对应能力具有有效基础烟测；业务执行仍需核对精确实例组合"
            except ValidationError:
                return "blocked", "基础烟测证据或目标配置不可核验，请前往能力验收查看"
            return "smoke_required", "云端 async deployment 需要真实烟测"

        if stale or snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED:
            return "blocked", "主机能力快照过期或失败"
        if modality not in {"image", "video"}:
            return "ready", "主机能力快照可用"
        workflows = list(WorkflowTemplateVersion.objects.filter(
            model_profile_version=deployment.profile_version,
            validated_deployment=deployment,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            is_current=True,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
            template__output_kind=modality,
        ))
        if not workflows:
            return "smoke_required", "本地媒体 workflow 尚未完成发布烟测"
        for workflow in workflows:
            try:
                artifacts = required_workflow_artifacts(workflow)
                if any(
                    artifact.status != ModelArtifact.Status.AVAILABLE
                    for artifact in artifacts
                ):
                    continue
                for artifact in artifacts:
                    verified_artifact_availability(
                        snapshot=snapshot,
                        deployment=deployment,
                        artifact=artifact,
                    )
            except ValidationError:
                continue
            return "ready", "主机、required model artifact 与 workflow 证据均可用"
        return "blocked", "workflow required artifact 缺少一致的新鲜 size/hash 主机证据"

    result = {}
    for modality in ("text", "image", "video"):
        candidates = [
            item for item in deployments
            if item.profile_version.modality in {modality, "multimodal"}
        ]
        states = [deployment_state(candidate, modality) for candidate in candidates]
        viable_count = sum(item_state == "ready" for item_state, _reason in states)
        if not candidates:
            state, reason = "unconfigured", "没有启用的模型部署"
        elif viable_count:
            state, reason = "ready", "至少一个 deployment 有完整能力证据"
        elif any(item_state == "smoke_required" for item_state, _reason in states):
            state, reason = "smoke_required", next(
                item_reason for item_state, item_reason in states
                if item_state == "smoke_required"
            )
        else:
            state, reason = "blocked", states[0][1]
        result[modality] = {
            "state": state,
            "reason": reason,
            "deployment_count": len(candidates),
            "viable_deployment_count": viable_count,
            "cloud_deployment_count": sum(
                candidate.runtime_host_id is None and item_state == "ready"
                for candidate, (item_state, _reason) in zip(candidates, states)
            ),
        }
    return {
        "schema_version": 1,
        "snapshot_id": str(snapshot.id),
        "captured_at": snapshot.captured_at.isoformat(),
        "capabilities": result,
    }


def host_resolution_and_concurrency(
    snapshot: HostCapabilitySnapshot,
    tiers: list[dict],
) -> tuple[int, int]:
    """Resolve the frozen host-compatible resolution/concurrency tier."""

    details = snapshot.details
    gpus = details.get("gpus") if isinstance(details, dict) else None
    if (not isinstance(gpus, list) or any(not isinstance(row, dict)
            or type(row.get("vram_total_bytes")) is not int or row["vram_total_bytes"] < 0 for row in gpus)):
        raise ValidationError("主机缺少完整显存观测，不能默认按零显存推断容量")
    if not isinstance(tiers, list) or not tiers:
        raise ValidationError("recommendation.vram_tiers 缺少完整规则")
    previous = -1
    for index, tier in enumerate(tiers):
        if (not isinstance(tier, dict) or not {"max_vram_bytes", "short_edge", "concurrency"}.issubset(tier)
                or any(type(tier[key]) is not int or tier[key] < 1 for key in ("short_edge", "concurrency"))):
            raise ValidationError("显存规则必须明确正整数短边和并发，不能使用内置默认值")
        upper = tier["max_vram_bytes"]
        if upper is None:
            if index != len(tiers) - 1:
                raise ValidationError("显存规则的无上限档位只能放在最后")
        elif type(upper) is not int or upper <= previous:
            raise ValidationError("显存档位上界必须是递增的非负整数")
        else:
            previous = upper
    # An explicit empty inventory is a recorded no-GPU host, not an absent
    # observation. Keep CPU/API recommendations viable under configured tiers.
    max_vram = max((row["vram_total_bytes"] for row in gpus), default=0)
    for tier in tiers:
        if tier["max_vram_bytes"] is None or max_vram <= tier["max_vram_bytes"]:
            return tier["short_edge"], tier["concurrency"]
    raise ValidationError("recommendation.vram_tiers 没有可用规则")


def _declared_model_capabilities(deployment: ModelDeployment) -> set[str]:
    specification = verified_model_profile_specification(
        deployment.profile_version,
    )
    declared: set[str] = set()
    for field in ("capabilities", "operations", "protocols"):
        values = specification.get(field)
        if isinstance(values, list):
            declared.update(str(item).strip() for item in values if str(item).strip())
    return declared


def _workflow_capability(
    version: WorkflowTemplateVersion | None,
    modality: str,
    *,
    declared: set[str] | None = None,
) -> str:
    contract = version.input_contract if version and isinstance(version.input_contract, dict) else {}
    capability = str(contract.get("capability") or "").strip()
    if capability:
        return capability
    if modality == "text":
        return "script.generate"
    if modality == "image":
        return "image.text_to_image"
    if modality == "video":
        values = declared or set()
        # A workflow-less async video deployment is T2V when the model says so;
        # never invent an image input by default.
        if values.intersection({"video.text_to_video", "text_to_video"}):
            return "video.text_to_video"
        if values.intersection({"video.image_to_video", "image_to_video"}):
            return "video.image_to_video"
        return "video.text_to_video"
    return "model.multimodal"


def _minimum_vram_bytes(specification: dict[str, Any]) -> int:
    for container_key in ("runtime_requirements", "resource_requirements", "requirements"):
        container = specification.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in ("minimum_vram_bytes", "min_vram_bytes", "vram_bytes"):
            try:
                value = int(container.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return 0


def _candidate_state(
    *,
    snapshot: HostCapabilitySnapshot,
    deployment: ModelDeployment,
    workflow: WorkflowTemplateVersion | None,
    availability: list[RuntimeModelAvailability],
    max_vram_bytes: int,
) -> tuple[str, str]:
    from studio.services.execution_deployment_resource_evidence import current_resource_smoke
    try:
        model_specification = verified_model_profile_specification(
            deployment.profile_version,
        )
    except ValidationError:
        return RecommendationCandidate.Status.UNAVAILABLE, "model_profile_contract_invalid"
    try:
        validate_deployment_execution_contract(deployment)
    except (ProviderDriverError, ValidationError):
        return (
            RecommendationCandidate.Status.UNAVAILABLE,
            "deployment_execution_contract_invalid",
        )
    is_local = deployment.runtime_host_id is not None
    modality = deployment.profile_version.modality
    if is_local and snapshot.expires_at and snapshot.expires_at <= timezone.now():
        return RecommendationCandidate.Status.UNAVAILABLE, "snapshot_stale"
    if not is_local and deployment.profile_version.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        reference = deployment.credential or deployment.profile_version.provider.credential
        try:
            if not reference:
                raise ProviderDriverError("credential missing", code="credential_missing")
            resolve_credential(reference)
            operations = validate_async_model_profile(deployment.profile_version)
        except (ProviderDriverError, ValidationError):
            return RecommendationCandidate.Status.UNAVAILABLE, "async_provider_unavailable"
        operation = _workflow_capability(
            workflow,
            modality,
            declared=set(operations),
        )
        if operation not in operations:
            return RecommendationCandidate.Status.UNAVAILABLE, "async_operation_missing"
        try:
            if current_resource_smoke(deployment, operation) is None:
                return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "async_smoke_test_required"
        except ValidationError:
            return RecommendationCandidate.Status.UNAVAILABLE, "resource_smoke_evidence_invalid"
        return RecommendationCandidate.Status.COMPATIBLE, "remote_provider_compatible"
    if modality == ModelProfileVersion.Modality.TEXT:
        return _text_resource_candidate_state(deployment)
    if is_local and modality in {"image", "video", "multimodal"}:
        if workflow is None:
            return RecommendationCandidate.Status.UNAVAILABLE, "workflow_missing"
        try:
            artifacts = required_workflow_artifacts(workflow)
        except ValidationError:
            return RecommendationCandidate.Status.UNAVAILABLE, "artifact_binding_missing"
        if any(artifact.status != ModelArtifact.Status.AVAILABLE for artifact in artifacts):
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "artifact_verification_required"
        try:
            for artifact in artifacts:
                verified_artifact_availability(
                    snapshot=snapshot,
                    deployment=deployment,
                    artifact=artifact,
                )
        except ValidationError:
            artifact_ids = {artifact.id for artifact in artifacts}
            states = {
                item.status
                for item in availability
                if (
                    item.deployment_id == deployment.id
                    and item.artifact_id in artifact_ids
                )
            }
            if RuntimeModelAvailability.Status.INCOMPATIBLE in states:
                return RecommendationCandidate.Status.UNAVAILABLE, "artifact_incompatible"
            if RuntimeModelAvailability.Status.MISSING in states:
                return RecommendationCandidate.Status.UNAVAILABLE, "artifact_missing"
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "artifact_inventory_unverified"
        minimum_vram = _minimum_vram_bytes(model_specification)
        if minimum_vram and max_vram_bytes < minimum_vram:
            return RecommendationCandidate.Status.UNAVAILABLE, "insufficient_vram"
    if modality in {"image", "video", "multimodal"}:
        if workflow is None:
            return RecommendationCandidate.Status.UNAVAILABLE, "workflow_missing"
        if (
            workflow.status != WorkflowTemplateVersion.Status.PUBLISHED
            or not workflow.is_current
        ):
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "workflow_not_published"
        if (
            workflow.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
            or workflow.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED
        ):
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "workflow_smoke_test_required"
    return (
        RecommendationCandidate.Status.COMPATIBLE,
        "remote_provider_compatible" if not is_local else "compatible",
    )


def _recommendable_deployments(snapshot: HostCapabilitySnapshot):
    return ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(
        Q(runtime_host=snapshot.host) | Q(runtime_host__isnull=True),
        status=ModelDeployment.Status.ACTIVE,
        profile_version__status=ModelProfileVersion.Status.PUBLISHED,
        profile_version__provider__is_active=True,
    ).order_by("-is_default", "-priority", "key")


def _catalog_sha256(deployments: list[ModelDeployment]) -> str:
    profile_ids = {item.profile_version_id for item in deployments}
    deployment_ids = {item.id for item in deployments}
    payload = {
        "schema_version": 1,
        "deployments": [{
            "id": str(item.id),
            "status": item.status,
            "priority": item.priority,
            "is_default": item.is_default,
            "runtime_host_id": str(item.runtime_host_id or ""),
            "runtime_config_sha256": item.runtime_config_sha256,
            "credential_binding_sha256": credential_binding_sha256(item),
            "profile_version_id": str(item.profile_version_id),
            "profile_status": item.profile_version.status,
            "specification_sha256": item.profile_version.specification_sha256,
            "provider_id": str(item.profile_version.provider_id),
            "provider_active": item.profile_version.provider.is_active,
            "provider_config_sha256": item.profile_version.provider.driver_config_sha256,
        } for item in deployments],
        "artifacts": [{
            "id": str(item.id),
            "profile_version_id": str(item.profile_version_id),
            "role": item.role,
            "filename": item.filename,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "status": item.status,
        } for item in ModelArtifact.objects.filter(
            profile_version_id__in=profile_ids,
        ).order_by("profile_version_id", "role", "logical_name")],
        "workflows": [{
            "id": str(item.id),
            "profile_version_id": str(item.model_profile_version_id),
            "status": item.status,
            "is_current": item.is_current,
            "validation_state": item.validation_state,
            "smoke_test_state": item.smoke_test_state,
            "execution_contract_sha256": item.execution_contract_sha256,
        } for item in WorkflowTemplateVersion.objects.filter(
            model_profile_version_id__in=profile_ids,
        ).order_by("model_profile_version_id", "template_id", "version")],
        "deployment_evidence": [{
            "id": str(item.id),
            "deployment_id": str(item.deployment_id),
            "revision": item.revision,
            "probe_kind": item.probe_kind,
            "status": item.status,
            "target_sha256": item.target_sha256,
            "operations_sha256": item.operations_sha256,
            "evidence_sha256": item.evidence_sha256,
            "expires_at": item.expires_at.isoformat(),
        } for item in ModelDeploymentCapabilitySnapshot.objects.filter(
            deployment_id__in=deployment_ids,
        ).order_by("deployment_id", "revision")],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@transaction.atomic
def recommend_for_host(
    snapshot: HostCapabilitySnapshot,
    *,
    project: SeriesProject | None,
    created_by,
) -> RecommendationSnapshot:
    verify_host_snapshot(snapshot)
    configuration: ResolvedConfiguration = resolve_configuration(
        project=project,
        required_keys=("recommendation.vram_tiers",),
    )
    tiers = configuration.values["recommendation.vram_tiers"]
    short_edge, concurrency = host_resolution_and_concurrency(snapshot, tiers)
    availability = list(snapshot.model_availability.select_related("deployment").all())
    deployments = list(_recommendable_deployments(snapshot))
    catalog_sha256 = _catalog_sha256(deployments)
    details = snapshot.details if isinstance(snapshot.details, dict) else {}
    gpus = details.get("gpus") if isinstance(details.get("gpus"), list) else []
    max_vram_bytes = max(
        (int(item.get("vram_total_bytes") or 0) for item in gpus if isinstance(item, dict)),
        default=0,
    )
    candidate_rows: list[dict[str, Any]] = []
    for deployment in deployments:
        verified_model_profile_specification(deployment.profile_version)
        workflows: list[WorkflowTemplateVersion | None]
        if deployment.profile_version.modality == "text":
            workflows = [None]
        else:
            workflows = list(WorkflowTemplateVersion.objects.filter(
                model_profile_version=deployment.profile_version,
            ).select_related("template").order_by("-is_current", "-version")) or [None]
        for workflow in workflows:
            candidate_status, reason_code = _candidate_state(
                snapshot=snapshot,
                deployment=deployment,
                workflow=workflow,
                availability=availability,
                max_vram_bytes=max_vram_bytes,
            )
            capability_key = _workflow_capability(
                workflow,
                deployment.profile_version.modality,
                declared=_declared_model_capabilities(deployment),
            )
            candidate_rows.append({
                "capability_key": capability_key,
                "deployment": deployment,
                "workflow_version": workflow,
                "status": candidate_status,
                "reason_code": reason_code,
            })
    ranked: dict[str, int] = {}
    recommended: set[str] = set()
    for row in candidate_rows:
        capability_key = row["capability_key"]
        ranked[capability_key] = ranked.get(capability_key, 0) + 1
        row["rank"] = ranked[capability_key]
        if (
            row["status"] == RecommendationCandidate.Status.COMPATIBLE
            and capability_key not in recommended
        ):
            row["status"] = RecommendationCandidate.Status.RECOMMENDED
            row["reason_code"] = "recommended_best_match"
            recommended.add(capability_key)
    aspect_ratio = project.aspect_ratio if project is not None else "9:16"
    try:
        ratio_width, ratio_height = [int(item) for item in aspect_ratio.split(":", 1)]
    except (TypeError, ValueError):
        ratio_width, ratio_height = 9, 16
    if ratio_width <= ratio_height:
        width = short_edge
        height = round(short_edge * ratio_height / ratio_width / 8) * 8
    else:
        height = short_edge
        width = round(short_edge * ratio_width / ratio_height / 8) * 8
    status = (
        RecommendationSnapshot.Status.READY
        if recommended else (
            RecommendationSnapshot.Status.INSUFFICIENT_DATA
            if any(
                item["status"] == RecommendationCandidate.Status.SMOKE_TEST_REQUIRED
                for item in candidate_rows
            )
            else RecommendationSnapshot.Status.NO_COMPATIBLE_MODEL
        )
    )
    public_candidates = [{
        "capability_key": item["capability_key"],
        "deployment_id": str(item["deployment"].id),
        "deployment_key": item["deployment"].key,
        "profile_version_id": str(item["deployment"].profile_version_id),
        "model_identifier": item["deployment"].profile_version.model_identifier,
        "modality": item["deployment"].profile_version.modality,
        "provider_driver": item["deployment"].profile_version.provider.driver_type,
        "workflow_version_id": (
            str(item["workflow_version"].id) if item["workflow_version"] else None
        ),
        "status": item["status"],
        "rank": item["rank"],
        "reason_code": item["reason_code"],
    } for item in candidate_rows]
    recommendation = {
        "schema_version": 1,
        "algorithm": "host-capability-v2",
        "host_snapshot_id": str(snapshot.id),
        "candidates": public_candidates,
        "recommended_resolution": {"width": width, "height": height},
        "recommended_concurrency": concurrency,
        "override_policy": "allowed_with_validation",
        "config_sha256": configuration.sha256,
        "catalog_sha256": catalog_sha256,
    }
    recommendation_contract = json_contract(
        recommendation,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    input_payload = {
        "host_snapshot_sha256": snapshot.details_sha256,
        "project_id": str(project.id) if project else None,
        "aspect_ratio": aspect_ratio,
        "config_sha256": configuration.sha256,
        "catalog_sha256": catalog_sha256,
    }
    input_sha256 = hashlib.sha256(canonical_json_bytes(input_payload)).hexdigest()
    existing = RecommendationSnapshot.objects.filter(
        host_snapshot=snapshot,
        project=project,
        input_sha256=input_sha256,
    ).first()
    if existing is not None:
        return existing
    config_revision = None
    if configuration.revision_ids:
        from studio.models import ConfigRevision

        config_revision = ConfigRevision.objects.filter(
            pk=configuration.revision_ids[-1],
        ).first()
    recommendation_snapshot = RecommendationSnapshot.objects.create(
        host_snapshot=snapshot,
        project=project,
        config_revision=config_revision,
        input_sha256=input_sha256,
        status=status,
        reason=(
            "已按主机事实、模型文件与 workflow 证据生成分级建议"
            if candidate_rows else "当前没有已启用且已发布的模型部署"
        ),
        recommendation=recommendation,
        recommendation_schema_version=recommendation_contract.schema_version,
        recommendation_size_bytes=recommendation_contract.size_bytes,
        recommendation_sha256=recommendation_contract.sha256,
        created_by=created_by,
    )
    for item in candidate_rows:
        suggested_values = {
            "configuration": {
                "production.default_resolution_width": width,
                "production.default_resolution_height": height,
                "production.default_concurrency": concurrency,
            },
            "runtime": {
                "resolution": {"width": width, "height": height},
                "concurrency": concurrency,
            },
        }
        contract = json_contract(
            suggested_values,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        RecommendationCandidate.objects.create(
            recommendation_snapshot=recommendation_snapshot,
            capability_key=item["capability_key"],
            deployment=item["deployment"],
            workflow_version=item["workflow_version"],
            status=item["status"],
            rank=item["rank"],
            reason_code=item["reason_code"],
            suggested_values=suggested_values,
            suggested_values_schema_version=contract.schema_version,
            suggested_values_size_bytes=contract.size_bytes,
            suggested_values_sha256=contract.sha256,
        )
    return recommendation_snapshot


def _assert_recommendation_current(
    recommendation: RecommendationSnapshot,
    *,
    candidate: RecommendationCandidate | None,
    decision: str,
) -> None:
    verify_json_contract(
        recommendation.recommendation,
        schema_version=recommendation.recommendation_schema_version,
        size_bytes=recommendation.recommendation_size_bytes,
        sha256=recommendation.recommendation_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    snapshot = HostCapabilitySnapshot.objects.select_related("host").get(
        pk=recommendation.host_snapshot_id,
    )
    verify_host_snapshot(snapshot)
    if (
        snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED
        or not snapshot.expires_at
        or snapshot.expires_at <= timezone.now()
    ):
        raise RecommendationStale("主机能力快照已失败或过期；请重新 probe/recommend")
    deployments = list(_recommendable_deployments(snapshot))
    expected_catalog = str(recommendation.recommendation.get("catalog_sha256") or "")
    if not expected_catalog or _catalog_sha256(deployments) != expected_catalog:
        raise RecommendationStale("模型、provider、deployment、workflow 或烟测目录已变化；请重新 recommend")
    configuration = resolve_configuration(
        project=recommendation.project,
        required_keys=("recommendation.vram_tiers",),
    )
    if configuration.sha256 != str(recommendation.recommendation.get("config_sha256") or ""):
        raise RecommendationStale("推荐所依据的配置已变化；请重新 recommend")
    if candidate is None or decision == RecommendationDecision.Decision.REJECT:
        return
    deployment_by_id = {item.id: item for item in deployments}
    deployment = deployment_by_id.get(candidate.deployment_id)
    if deployment is None:
        raise RecommendationStale("候选 deployment 已停用或不再属于该运行主机")
    workflow = None
    if candidate.workflow_version_id:
        workflow = WorkflowTemplateVersion.objects.filter(
            pk=candidate.workflow_version_id,
        ).first()
        if workflow is None:
            raise RecommendationStale("候选 workflow 已不存在")
    details = snapshot.details if isinstance(snapshot.details, dict) else {}
    gpus = details.get("gpus") if isinstance(details.get("gpus"), list) else []
    max_vram_bytes = max(
        (int(item.get("vram_total_bytes") or 0) for item in gpus if isinstance(item, dict)),
        default=0,
    )
    current_state, reason_code = _candidate_state(
        snapshot=snapshot,
        deployment=deployment,
        workflow=workflow,
        availability=list(snapshot.model_availability.all()),
        max_vram_bytes=max_vram_bytes,
    )
    if (
        candidate.status not in {
            RecommendationCandidate.Status.RECOMMENDED,
            RecommendationCandidate.Status.COMPATIBLE,
        }
        or current_state != RecommendationCandidate.Status.COMPATIBLE
    ):
        raise RecommendationStale(f"候选当前不再可执行: {reason_code}")


@transaction.atomic
def decide_recommendation(
    recommendation: RecommendationSnapshot,
    *,
    candidate: RecommendationCandidate | None,
    decision: str,
    scope_type: str,
    project: SeriesProject | None,
    override_values: dict[str, Any],
    reason: str,
    decided_by,
) -> RecommendationDecision:
    recommendation = RecommendationSnapshot.objects.select_for_update().get(
        pk=recommendation.pk,
    )
    if candidate is not None:
        candidate = RecommendationCandidate.objects.select_related(
            "deployment", "workflow_version",
        ).get(pk=candidate.pk, recommendation_snapshot=recommendation)
        verify_json_contract(
            candidate.suggested_values,
            schema_version=candidate.suggested_values_schema_version,
            size_bytes=candidate.suggested_values_size_bytes,
            sha256=candidate.suggested_values_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
    if decision not in RecommendationDecision.Decision.values:
        raise ValidationError("decision 无效")
    _assert_recommendation_current(
        recommendation,
        candidate=candidate,
        decision=decision,
    )
    if scope_type not in {ConfigRevision.ScopeType.SYSTEM, ConfigRevision.ScopeType.PROJECT}:
        raise ValidationError("recommendation decision 只支持 system/project scope")
    if scope_type == ConfigRevision.ScopeType.PROJECT:
        if project is None or recommendation.project_id not in {None, project.id}:
            raise ValidationError("project scope 与推荐快照不一致")
    elif project is not None:
        raise ValidationError("system scope 不接受 project")
    if decision != RecommendationDecision.Decision.REJECT and candidate is None:
        raise ValidationError("接受或覆盖必须选择 candidate")
    if (
        decision == RecommendationDecision.Decision.ACCEPT
        and candidate.status == RecommendationCandidate.Status.UNAVAILABLE
    ):
        raise ValidationError("不可接受 unavailable candidate")
    if not isinstance(override_values, dict):
        raise ValidationError("override_values 必须是对象")
    sensitive = find_sensitive_json_keys(override_values)
    if sensitive:
        raise ValidationError(
            f"override_values 不能包含敏感字段: {', '.join(sensitive)}"
        )
    if set(override_values) - {"configuration"}:
        raise ValidationError("推荐只支持 configuration 覆盖；执行实例和配方须通过正式配置入口创建")
    if not isinstance(override_values.get("configuration", {}), dict):
        raise ValidationError("configuration override 必须是对象")
    if decision != RecommendationDecision.Decision.OVERRIDE and override_values:
        raise ValidationError("仅 override 决策可提交覆盖配置")
    contract = json_contract(
        override_values,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    config_revision = None
    if decision != RecommendationDecision.Decision.REJECT:
        suggested = (
            candidate.suggested_values
            if isinstance(candidate.suggested_values, dict) else {}
        )
        suggested_config = suggested.get("configuration", {})
        override_config = override_values.get("configuration", {})
        config_values = (
            {**suggested_config, **override_config}
            if decision == RecommendationDecision.Decision.OVERRIDE
            else suggested_config
        )
        if not isinstance(config_values, dict):
            raise ValidationError("configuration override 必须是对象")
        if config_values:
            config_revision = create_config_revision(
                scope_type=scope_type,
                project=project,
                run=None,
                values=config_values,
                created_by=decided_by,
                change_summary=f"推荐决策 {recommendation.id}: {decision}"[:500],
            )
    return RecommendationDecision.objects.create(
        recommendation_snapshot=recommendation,
        candidate=candidate,
        decision=decision,
        scope_type=scope_type,
        project=project,
        config_revision=config_revision,
        override_values=override_values,
        override_values_schema_version=contract.schema_version,
        override_values_size_bytes=contract.size_bytes,
        override_values_sha256=contract.sha256,
        reason=reason[:500],
        decided_by=decided_by,
    )
