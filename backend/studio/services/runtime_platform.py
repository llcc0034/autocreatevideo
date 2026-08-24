from __future__ import annotations

import csv
from copy import deepcopy
import hashlib
import io
import os
import platform
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path, PurePosixPath
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
    ProductionRecipeVersion,
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
from studio.services.async_media_contract import fresh_smoke_snapshot, validate_async_model_profile
from studio.services.json_contracts import (
    assign_json_contract,
    canonical_json_bytes,
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    driver_for,
    resolve_credential,
)


_MODEL_FOLDER_BY_ROLE = {
    ModelArtifact.Role.CHECKPOINT: "checkpoints",
    ModelArtifact.Role.DIFFUSION_MODEL: "diffusion_models",
    ModelArtifact.Role.TEXT_ENCODER: "text_encoders",
    ModelArtifact.Role.VAE: "vae",
    ModelArtifact.Role.LORA: "loras",
}


class RecommendationStale(ValidationError):
    """The frozen recommendation no longer describes executable live state."""


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


def _gpu_probe() -> tuple[list[dict[str, Any]], str]:
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
            timeout=5,
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
    return hostname in {"localhost", "127.0.0.1", "::1"}


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


def _controlled_local_model_path(folder: str, filename: str) -> Path | None:
    output_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    if not output_raw:
        return None
    relative = PurePosixPath(str(filename or "").replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return None
    models_root = (Path(output_raw).resolve().parent / "models").resolve()
    folder_root = (models_root / folder).resolve()
    candidate = (folder_root / Path(*relative.parts)).resolve()
    if not candidate.is_relative_to(folder_root) or not candidate.is_file():
        return None
    return candidate


def _observed_file_facts(
    path: Path,
    *,
    lease_renew: Callable[[], None] | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if lease_renew is not None:
                lease_renew()
    return size, digest.hexdigest()


def verified_artifact_availability(
    *,
    snapshot: HostCapabilitySnapshot,
    deployment: ModelDeployment,
    artifact: ModelArtifact,
) -> RuntimeModelAvailability:
    """Return exact, fresh per-host byte evidence or fail closed."""

    verify_host_snapshot(snapshot)
    if deployment.runtime_host_id is None or snapshot.host_id != deployment.runtime_host_id:
        raise ValidationError("artifact evidence 的主机与 deployment 不一致")
    latest = HostCapabilitySnapshot.objects.filter(host_id=snapshot.host_id).order_by(
        "-captured_at", "-revision",
    ).first()
    if latest is None or latest.id != snapshot.id:
        raise ValidationError("artifact evidence 不是该主机最新快照")
    if snapshot.expires_at is None or snapshot.expires_at <= timezone.now():
        raise ValidationError("artifact evidence 已过期")
    if snapshot.probe_status == HostCapabilitySnapshot.ProbeStatus.FAILED:
        raise ValidationError("artifact evidence 所属主机探测失败")
    if artifact.profile_version_id != deployment.profile_version_id:
        raise ValidationError("artifact 与 deployment model profile 不一致")
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
        raise ValidationError("artifact 缺少与声明一致的主机观测 size/hash")
    return availability


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
            for artifact in ModelArtifact.objects.filter(profile_version=deployment.profile_version):
                availability.append({
                    "deployment": deployment,
                    "artifact": artifact,
                    "key": f"deployment:{deployment.id}:artifact:{artifact.id}",
                    "status": RuntimeModelAvailability.Status.UNKNOWN,
                    "reason": "ComfyUI 部署探测失败",
                    "observed_size_bytes": None,
                    "observed_sha256": "",
                    "observation_source": RuntimeModelAvailability.ObservationSource.NAME_ONLY,
                })
            continue
        system = health.get("system") if isinstance(health, dict) else {}
        node_types = sorted(object_info) if isinstance(object_info, dict) else []
        folder_values: dict[str, set[str]] = {}
        folder_summaries: dict[str, dict[str, Any]] = {}
        deployment_errors: list[str] = []
        for folder in sorted(set(_MODEL_FOLDER_BY_ROLE.values())):
            if lease_renew is not None:
                lease_renew()
            try:
                response = driver.invoke(deployment, "models", {"folder": folder})
                items = response.get("items") if isinstance(response, dict) else []
                values = {
                    str(item)
                    for item in items
                    if isinstance(item, str) and item
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
        artifacts = ModelArtifact.objects.filter(profile_version=deployment.profile_version)
        for artifact in artifacts:
            folder = _MODEL_FOLDER_BY_ROLE.get(artifact.role)
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
                })
                continue
            if folder is None:
                availability.append({
                    "deployment": deployment,
                    "artifact": artifact,
                    "key": availability_key,
                    "status": RuntimeModelAvailability.Status.UNKNOWN,
                    "reason": "该 artifact role 无 ComfyUI 文件夹探测器",
                    "observed_size_bytes": None,
                    "observed_sha256": "",
                    "observation_source": RuntimeModelAvailability.ObservationSource.NAME_ONLY,
                })
                continue
            observed_size = None
            observed_sha = ""
            observation_source = RuntimeModelAvailability.ObservationSource.NAME_ONLY
            if folder not in folder_values or any(
                error.startswith(f"comfyui_models_{folder}_unavailable")
                for error in deployment_errors
            ):
                state = RuntimeModelAvailability.Status.UNKNOWN
                reason = "ComfyUI 未返回该模型文件夹库存"
            elif artifact.filename in folder_values[folder] and not local_filesystem:
                state = RuntimeModelAvailability.Status.UNKNOWN
                reason = "远程 ComfyUI 仅报告文件名，缺少受认证 size/hash 证明"
            elif artifact.filename in folder_values[folder]:
                candidate = _controlled_local_model_path(folder, artifact.filename)
                if candidate is None:
                    state = RuntimeModelAvailability.Status.INCOMPATIBLE
                    reason = "本地模型路径不在受控 Comfy 根或文件不可读"
                else:
                    try:
                        observed_size, observed_sha = _observed_file_facts(
                            candidate,
                            lease_renew=lease_renew,
                        )
                    except OSError:
                        state = RuntimeModelAvailability.Status.UNKNOWN
                        reason = "本地模型文件无法读取"
                    else:
                        observation_source = (
                            RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM
                        )
                        if not artifact.sha256 or artifact.size_bytes is None:
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
) -> HostCapabilitySnapshot:
    host = RuntimeHost.objects.get(pk=host.pk)
    if probe_attempt is not None and probe_attempt.host_id != host.pk:
        raise ValidationError("runtime host probe attempt 与目标主机不一致")
    if host.status == RuntimeHost.Status.DISABLED:
        raise ValidationError("已禁用主机不能执行探测；请先通过受控状态入口启用")
    if lease_renew is not None:
        lease_renew()
    configuration = resolve_configuration(required_keys=("runtime.capability_ttl_seconds",))
    errors: list[str] = []
    local_probe = _host_probe_is_local(host)
    if local_probe:
        gpus, gpu_error = _gpu_probe()
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
    comfy, availability, comfy_errors = _comfy_probe(host, lease_renew=lease_renew)
    errors.extend(comfy_errors)
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
    }
    details_contract = json_contract(details)
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
        lease_renew()
    with transaction.atomic():
        host = RuntimeHost.objects.select_for_update().get(pk=host.pk)
        if host.status == RuntimeHost.Status.DISABLED:
            raise ValidationError("主机在探测期间已被禁用；探测结果未写入")
        if commit_guard is not None:
            # The durable attempt claim is fenced inside the same transaction
            # that persists the immutable snapshot. A cancelled or recovered
            # worker therefore cannot commit a late result.
            commit_guard()
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
            expires_at=captured_at + timedelta(
                seconds=int(configuration.values["runtime.capability_ttl_seconds"]),
            ),
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
        if local_probe:
            host.name = platform.node() or host.name
            host.fingerprint_sha256 = _host_fingerprint()
        host.status = RuntimeHost.Status.ACTIVE if core_ok else RuntimeHost.Status.UNAVAILABLE
        host.last_seen_at = captured_at
        host_fields = ["status", "last_seen_at", "updated_at"]
        if local_probe:
            host_fields.extend(("name", "fingerprint_sha256"))
        host.save(update_fields=host_fields)
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
    )


def capability_readiness(snapshot: HostCapabilitySnapshot) -> dict[str, Any]:
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
            if any(fresh_smoke_snapshot(deployment, operation) for operation in relevant):
                return "ready", "云端 async deployment 有新鲜真实烟测证据"
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

    details = snapshot.details if isinstance(snapshot.details, dict) else {}
    gpus = details.get("gpus") if isinstance(details.get("gpus"), list) else []
    max_vram = max(
        (int(item.get("vram_total_bytes") or 0) for item in gpus if isinstance(item, dict)),
        default=0,
    )
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        upper = tier.get("max_vram_bytes")
        if upper is None or max_vram <= int(upper):
            return int(tier.get("short_edge") or 512), int(tier.get("concurrency") or 1)
    raise ValidationError("recommendation.vram_tiers 没有可用规则")


def _declared_model_capabilities(deployment: ModelDeployment) -> set[str]:
    specification = deployment.profile_version.specification
    declared: set[str] = set()
    if isinstance(specification, dict):
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
        if fresh_smoke_snapshot(deployment, operation) is None:
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "async_smoke_test_required"
        return RecommendationCandidate.Status.COMPATIBLE, "remote_provider_compatible"
    if modality == ModelProfileVersion.Modality.TEXT:
        reference = deployment.credential or deployment.profile_version.provider.credential
        try:
            if not reference:
                raise ProviderDriverError("credential missing", code="credential_missing")
            resolve_credential(reference)
        except ProviderDriverError:
            return RecommendationCandidate.Status.UNAVAILABLE, "text_credential_unavailable"
        binding = credential_binding_sha256(deployment)
        evidence = deployment.capability_snapshots.filter(
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            expires_at__gt=timezone.now(),
            provider_config_sha256=deployment.profile_version.provider.driver_config_sha256,
            deployment_config_sha256=deployment.runtime_config_sha256,
            credential_binding_sha256=binding,
        ).order_by("-revision").first()
        if evidence is None or not set(evidence.operations or []).intersection({
            "chat_completion", "script.generate", "series_concept",
        }):
            return RecommendationCandidate.Status.SMOKE_TEST_REQUIRED, "text_health_or_smoke_required"
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
        minimum_vram = _minimum_vram_bytes(deployment.profile_version.specification or {})
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
        specification = deployment.profile_version.specification
        verify_json_contract(
            specification,
            schema_version=deployment.profile_version.specification_schema_version,
            size_bytes=deployment.profile_version.specification_size_bytes,
            sha256=deployment.profile_version.specification_sha256,
        )
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
    recommendation_contract = json_contract(recommendation)
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
            "route": {
                "capability_key": item["capability_key"],
                "model_deployment_id": str(item["deployment"].id),
                "workflow_version_id": (
                    str(item["workflow_version"].id) if item["workflow_version"] else None
                ),
            },
        }
        contract = json_contract(suggested_values)
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


def _draft_recommended_recipe(
    candidate: RecommendationCandidate,
    *,
    decided_by,
) -> ProductionRecipeVersion:
    stage_key = (
        "script_generate"
        if candidate.capability_key.startswith("script.")
        else "media_generation"
    )
    bases = ProductionRecipeVersion.objects.filter(
        status=ProductionRecipeVersion.Status.PUBLISHED,
    ).order_by("-published_at", "-version")
    base = next((
        item for item in bases
        if any(
            isinstance(stage, dict) and stage.get("key") == stage_key
            for stage in (item.definition or {}).get("stages", [])
        )
    ), None)
    if base is None:
        raise ValidationError(f"没有包含 {stage_key} 的已发布配方可供派生")
    definition = deepcopy(base.definition)
    route = {"model_deployment_id": str(candidate.deployment_id)}
    if candidate.workflow_version_id:
        route["workflow_version_id"] = str(candidate.workflow_version_id)
    for stage in definition.get("stages", []):
        if isinstance(stage, dict) and stage.get("key") == stage_key:
            stage["routing"] = {
                "mode": "fixed",
                "routes": [route],
                "variant_count": 1,
            }
            break
    next_version = (
        ProductionRecipeVersion.objects.filter(recipe_key=base.recipe_key)
        .aggregate(value=Max("version"))["value"]
        or 0
    ) + 1
    draft = ProductionRecipeVersion(
        recipe_key=base.recipe_key,
        version=next_version,
        name=f"{base.name} · 推荐草稿",
        status=ProductionRecipeVersion.Status.DRAFT,
    )
    assign_json_contract(draft, "definition", definition)
    draft.save()
    return draft


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
    contract = json_contract(override_values)
    config_revision = None
    recipe_version = None
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
        recipe_version = _draft_recommended_recipe(candidate, decided_by=decided_by)
    return RecommendationDecision.objects.create(
        recommendation_snapshot=recommendation,
        candidate=candidate,
        decision=decision,
        scope_type=scope_type,
        project=project,
        config_revision=config_revision,
        recipe_version=recipe_version,
        override_values=override_values,
        override_values_schema_version=contract.schema_version,
        override_values_size_bytes=contract.size_bytes,
        override_values_sha256=contract.sha256,
        reason=reason[:500],
        decided_by=decided_by,
    )
