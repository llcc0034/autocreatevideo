"""Production readiness checks kept separate from the process liveness probe."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import shutil

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from studio.management.commands.seed_data import (
    H3_TURBO_LORA,
    REF_VIDEO_WORKFLOW,
    SDXL_IMAGE_CHECKPOINT,
    SDXL_IMAGE_WORKFLOW,
    T8_DUAL_CLOCK_REF_VIDEO_WORKFLOW,
    TURBO_LORA_REF_VIDEO_WORKFLOW,
    VIDEO_WORKFLOW,
)
from studio.models import WorkerLease
from studio.services.comfyui import ComfyUIClient, ComfyUIError
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    ComfyUIContractUnavailable,
    validate_comfyui_api_contract,
)
from studio.services.distribution import DistributionError, distribution_configuration


_PROBE_UPLOAD_PREFIX = "autoprompt/readiness-contract"
_PROBE_VALUES = {
    "PROMPT": "A restrained cinematic production-readiness probe.",
    "NEGATIVE_PROMPT": "blurry, watermark",
    "WIDTH": 608,
    "HEIGHT": 352,
    "FRAMES": 124,
    "SEED": 1,
    "MODEL": SDXL_IMAGE_CHECKPOINT,
    "OUTPUT_PREFIX": f"{_PROBE_UPLOAD_PREFIX}/output",
    "STEPS": 20,
    "LORA_STRENGTH": 1.0,
}


def _materialize_probe_value(value):
    if isinstance(value, dict):
        return {key: _materialize_probe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize_probe_value(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return _PROBE_VALUES[value[2:-1]]
    return value


def _reviewed_contract_probes() -> tuple[dict[str, dict], frozenset[str]]:
    """Build non-executing probes for every reviewed seed workflow capability.

    The first/last and reference probes deliberately populate every supported
    dynamic slot.  ``validate_comfyui_api_contract`` can therefore catch a
    live node whose class still exists but whose ports, output slots, types,
    autogrow limits, widgets, or fixed-model enums have drifted.
    """

    text_video = _materialize_probe_value(deepcopy(VIDEO_WORKFLOW))

    first_last = _materialize_probe_value(deepcopy(VIDEO_WORKFLOW))
    first_path = f"{_PROBE_UPLOAD_PREFIX}/first.png"
    last_path = f"{_PROBE_UPLOAD_PREFIX}/last.png"
    first_last["15"] = {"class_type": "LoadImage", "inputs": {"image": first_path}}
    first_last["16"] = {"class_type": "LoadImage", "inputs": {"image": last_path}}
    first_last["5"]["inputs"].update({
        "first_frame": ["15", 0],
        "last_frame": ["16", 0],
    })

    reference = _materialize_probe_value(deepcopy(REF_VIDEO_WORKFLOW))
    uploaded_paths = {first_path, last_path}
    next_id = 15
    for index in range(9):
        path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.png"
        node_id = str(next_id)
        next_id += 1
        uploaded_paths.add(path)
        reference[node_id] = {"class_type": "LoadImage", "inputs": {"image": path}}
        reference["5"]["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
    for index in range(3):
        path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.mp4"
        load_id = str(next_id)
        components_id = str(next_id + 1)
        next_id += 2
        uploaded_paths.add(path)
        reference[load_id] = {"class_type": "LoadVideo", "inputs": {"file": path}}
        reference[components_id] = {
            "class_type": "GetVideoComponents",
            "inputs": {"video": [load_id, 0]},
        }
        reference["5"]["inputs"][f"ref_videos.ref_video_{index}"] = [components_id, 0]
        reference["5"]["inputs"][f"ref_video_audios.ref_video_audio_{index}"] = [
            components_id, 1,
        ]
    for index in range(3):
        path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.wav"
        node_id = str(next_id)
        next_id += 1
        uploaded_paths.add(path)
        reference[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": path}}
        reference["5"]["inputs"][f"ref_audios.ref_audio_{index}"] = [node_id, 0]

    def populate_reference_slots(graph: dict, conditioning_id: str) -> None:
        nonlocal next_id
        conditioning = graph[conditioning_id]["inputs"]
        for index in range(9):
            path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.png"
            node_id = str(next_id)
            next_id += 1
            uploaded_paths.add(path)
            graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": path}}
            conditioning[f"ref_images.ref_image_{index}"] = [node_id, 0]
        for index in range(3):
            path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.mp4"
            load_id = str(next_id)
            components_id = str(next_id + 1)
            next_id += 2
            uploaded_paths.add(path)
            graph[load_id] = {"class_type": "LoadVideo", "inputs": {"file": path}}
            graph[components_id] = {
                "class_type": "GetVideoComponents", "inputs": {"video": [load_id, 0]},
            }
            conditioning[f"ref_videos.ref_video_{index}"] = [components_id, 0]
            conditioning[f"ref_video_audios.ref_video_audio_{index}"] = [components_id, 1]
        for index in range(3):
            path = f"{_PROBE_UPLOAD_PREFIX}/reference-{index}.wav"
            node_id = str(next_id)
            next_id += 1
            uploaded_paths.add(path)
            graph[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": path}}
            conditioning[f"ref_audios.ref_audio_{index}"] = [node_id, 0]

    turbo_lora = _materialize_probe_value(deepcopy(TURBO_LORA_REF_VIDEO_WORKFLOW))
    populate_reference_slots(turbo_lora, "5")
    t8_dual_clock = _materialize_probe_value(deepcopy(T8_DUAL_CLOCK_REF_VIDEO_WORKFLOW))
    populate_reference_slots(t8_dual_clock, "6")

    image = _materialize_probe_value(deepcopy(SDXL_IMAGE_WORKFLOW))
    return ({
        "h3_text_to_video": text_video,
        "h3_first_last_to_video": first_last,
        "h3_reference_to_video": reference,
        "h3_reference_to_video_t8_dual_clock_4step": t8_dual_clock,
        "h3_reference_to_video_turbo_lora": turbo_lora,
        "sdxl_text_to_image": image,
    }, frozenset(uploaded_paths))


REVIEWED_COMFYUI_CONTRACT_PROBES, REVIEWED_COMFYUI_PROBE_UPLOADS = _reviewed_contract_probes()
CORE_COMFYUI_PROBE_NAMES = frozenset({
    "h3_text_to_video",
    "h3_first_last_to_video",
    "h3_reference_to_video",
    "sdxl_text_to_image",
})
OPTIONAL_COMFYUI_PROBE_NAMES = frozenset(REVIEWED_COMFYUI_CONTRACT_PROBES) - CORE_COMFYUI_PROBE_NAMES
REQUIRED_COMFYUI_NODES = frozenset(
    node["class_type"]
    for graph in REVIEWED_COMFYUI_CONTRACT_PROBES.values()
    for node in graph.values()
)
CORE_COMFYUI_NODES = frozenset(
    node["class_type"]
    for name in CORE_COMFYUI_PROBE_NAMES
    for node in REVIEWED_COMFYUI_CONTRACT_PROBES[name].values()
)
REQUIRED_COMFYUI_MODELS = {
    "checkpoints": frozenset({"sd_xl_base_1.0.safetensors"}),
    "diffusion_models": frozenset({
        "MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors",
        "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors",
    }),
    "text_encoders": frozenset({"qwen3vl_32b_minimax_h3_int8_convrot.safetensors"}),
    "vae": frozenset({
        "minimax_h3_video_vae_fp16.safetensors",
        "minimax_h3_audio_vae_fp32.safetensors",
    }),
    "loras": frozenset({H3_TURBO_LORA}),
}
CORE_COMFYUI_MODELS = {
    folder: models
    for folder, models in REQUIRED_COMFYUI_MODELS.items()
    if folder != "loras"
}
OPTIONAL_COMFYUI_MODELS = {
    folder: models
    for folder, models in REQUIRED_COMFYUI_MODELS.items()
    if folder == "loras"
}


def _result(ok: bool, message: str, **details) -> dict:
    return {"ok": bool(ok), "message": message, **details}


def _database_check() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            value = cursor.fetchone()
        return _result(value == (1,), "数据库连接正常" if value == (1,) else "数据库响应异常")
    except Exception:
        return _result(False, "数据库不可用")


def _migration_check() -> dict:
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return _result(
            not pending,
            "数据库迁移已同步" if not pending else "存在未应用的数据库迁移",
            pending_count=len(pending),
        )
    except Exception:
        return _result(False, "无法核对数据库迁移", pending_count=None)


def _storage_check() -> dict:
    media_root = Path(settings.MEDIA_ROOT)
    minimum = max(0, int(getattr(settings, "READINESS_MIN_DISK_FREE_BYTES", 512 * 1024 * 1024)))
    try:
        if not media_root.is_dir() or not os.access(media_root, os.W_OK):
            return _result(False, "媒体存储目录不可写", free_bytes=None, minimum_free_bytes=minimum)
        free = shutil.disk_usage(media_root).free
        return _result(
            free >= minimum,
            "媒体存储空间正常" if free >= minimum else "媒体存储剩余空间不足",
            free_bytes=free,
            minimum_free_bytes=minimum,
        )
    except (OSError, ValueError):
        return _result(False, "无法核对媒体存储", free_bytes=None, minimum_free_bytes=minimum)


def _binary_available(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.is_file()
    return shutil.which(raw) is not None


def _toolchain_check() -> dict:
    ffmpeg_ok = _binary_available(getattr(settings, "FFMPEG_BINARY", "ffmpeg"))
    ffprobe_ok = _binary_available(getattr(settings, "FFPROBE_BINARY", "ffprobe"))
    try:
        distribution = distribution_configuration()
        disclosure_ok = True
    except (DistributionError, OSError, ValueError):
        distribution = {}
        disclosure_ok = False
    return _result(
        ffmpeg_ok and ffprobe_ok and disclosure_ok,
        "FFmpeg/FFprobe 与单镜发行标识工具链可用"
        if ffmpeg_ok and ffprobe_ok and disclosure_ok
        else "FFmpeg/FFprobe 或单镜发行标识工具链不完整",
        ffmpeg=ffmpeg_ok,
        ffprobe=ffprobe_ok,
        task_distribution_disclosure=disclosure_ok,
        distribution_version=distribution.get("version"),
        distribution_font_sha256=distribution.get("font_sha256"),
    )


def _comfyui_output_check() -> dict:
    configured = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    minimum = max(0, int(getattr(settings, "READINESS_MIN_DISK_FREE_BYTES", 512 * 1024 * 1024)))
    try:
        output_root = Path(configured)
        available = bool(configured) and output_root.is_dir() and os.access(output_root, os.R_OK)
        free = shutil.disk_usage(output_root).free if available else None
    except (OSError, ValueError, TypeError):
        available = False
        free = None
    enough_space = bool(available and free is not None and free >= minimum)
    return _result(
        enough_space,
        "ComfyUI 输出目录可读且空间充足"
        if enough_space else "ComfyUI 输出目录不可读或剩余空间不足",
        free_bytes=free,
        minimum_free_bytes=minimum,
    )


def _worker_check(key: str, label: str, *, required: bool) -> dict:
    if not required:
        return _result(True, f"{label}未设为必需", required=False, last_seen_at=None)
    max_age = max(5, int(getattr(settings, "READINESS_WORKER_MAX_AGE_SECONDS", 30)))
    try:
        last_seen = WorkerLease.objects.filter(pk=key).values_list("updated_at", flat=True).first()
    except Exception:
        return _result(False, f"无法读取{label}心跳", required=True, last_seen_at=None)
    fresh = bool(last_seen and last_seen >= timezone.now() - timedelta(seconds=max_age))
    return _result(
        fresh,
        f"{label}心跳正常" if fresh else f"{label}没有新鲜心跳",
        required=True,
        last_seen_at=last_seen.isoformat() if last_seen else None,
        max_age_seconds=max_age,
    )


def _cuda_device_count(devices: list[dict]) -> int:
    count = 0
    for device in devices:
        name = str(device.get("name") or "").strip().casefold()
        device_type = str(device.get("type") or "").strip().casefold()
        if (
            "cuda" in device_type
            or "nvidia" in device_type
            or ("nvidia" in name and "cpu" not in device_type)
        ):
            count += 1
    return count


def _comfyui_check() -> dict:
    required = bool(getattr(settings, "READINESS_COMFYUI_REQUIRED", True))
    if not required:
        return _result(True, "ComfyUI 未设为 readiness 必需", required=False)
    timeout = max(0.5, min(5.0, float(getattr(settings, "READINESS_COMFYUI_TIMEOUT_SECONDS", 2))))
    try:
        client = ComfyUIClient(timeout=timeout)
        payload = client.health()
        system = payload.get("system") if isinstance(payload, dict) else None
        devices = payload.get("devices") if isinstance(payload, dict) else None
        version = system.get("comfyui_version") if isinstance(system, dict) else None
        healthy = (
            isinstance(version, str)
            and bool(version.strip())
            and isinstance(devices, list)
            and bool(devices)
            and all(
                isinstance(device, dict)
                and isinstance(device.get("name"), str)
                and bool(device["name"].strip())
                and isinstance(device.get("type"), str)
                and bool(device["type"].strip())
                for device in devices
            )
        )
        if not healthy:
            return _result(False, "ComfyUI 响应无效", required=True)

        cuda_devices = _cuda_device_count(devices)
        if not cuda_devices:
            return _result(
                False,
                "ComfyUI 没有可用的 NVIDIA CUDA 设备",
                required=True,
                device_count=len(devices),
                cuda_device_count=0,
            )

        object_info = client.object_info()
        available_nodes = set(object_info) if isinstance(object_info, dict) else set()
        missing_nodes = sorted(CORE_COMFYUI_NODES - available_nodes)
        optional_missing_nodes = sorted(
            (REQUIRED_COMFYUI_NODES - CORE_COMFYUI_NODES) - available_nodes
        )

        missing_models = {}
        optional_missing_models = {}
        optional_model_errors = {}
        for folder, required_models in REQUIRED_COMFYUI_MODELS.items():
            try:
                raw_models = client.models(folder)
            except ComfyUIError:
                if folder in OPTIONAL_COMFYUI_MODELS:
                    optional_model_errors[folder] = "unavailable"
                    continue
                raise
            available_models = (
                set(raw_models)
                if isinstance(raw_models, (list, tuple, set, frozenset))
                and all(isinstance(item, str) for item in raw_models)
                else set()
            )
            missing = sorted(required_models - available_models)
            if missing and folder in CORE_COMFYUI_MODELS:
                missing_models[folder] = missing
            elif missing:
                optional_missing_models[folder] = missing

        invalid_contracts = {}
        optional_invalid_contracts = {}
        contract_schema_sha256 = {}
        for name, graph in REVIEWED_COMFYUI_CONTRACT_PROBES.items():
            try:
                report = validate_comfyui_api_contract(
                    graph,
                    object_info,
                    uploaded_paths=REVIEWED_COMFYUI_PROBE_UPLOADS,
                )
                contract_schema_sha256[name] = report.schema_sha256
            except ComfyUIContractError as exc:
                target = (
                    invalid_contracts
                    if name in CORE_COMFYUI_PROBE_NAMES
                    else optional_invalid_contracts
                )
                target[name] = len(exc.issues) or 1
            except ComfyUIContractUnavailable:
                target = (
                    invalid_contracts
                    if name in CORE_COMFYUI_PROBE_NAMES
                    else optional_invalid_contracts
                )
                target[name] = 1

        contract_ok = not missing_nodes and not missing_models and not invalid_contracts
        return _result(
            contract_ok,
            "ComfyUI 已满足审核工作流能力契约"
            if contract_ok else "ComfyUI 未满足审核工作流能力契约",
            required=True,
            device_count=len(devices),
            cuda_device_count=cuda_devices,
            missing_nodes=missing_nodes,
            missing_models=missing_models,
            invalid_contracts=invalid_contracts,
            optional_workflows_ready=(
                not optional_missing_nodes
                and not optional_missing_models
                and not optional_model_errors
                and not optional_invalid_contracts
            ),
            optional_missing_nodes=optional_missing_nodes,
            optional_missing_models=optional_missing_models,
            optional_model_errors=optional_model_errors,
            optional_invalid_contracts=optional_invalid_contracts,
            contract_schema_sha256=contract_schema_sha256,
        )
    except (ComfyUIError, ValueError, TypeError):
        return _result(False, "ComfyUI 不可用", required=True)


def collect_readiness() -> dict:
    """Return dependency readiness without exposing filesystem paths or raw secrets."""
    checks = {
        "database": _database_check(),
        "migrations": _migration_check(),
        "storage": _storage_check(),
        "toolchain": _toolchain_check(),
        "comfyui_output": _comfyui_output_check(),
        "generation_worker": _worker_check(
            "generation-worker-heartbeat",
            "生成 Worker",
            required=bool(getattr(settings, "READINESS_GENERATION_WORKER_REQUIRED", True)),
        ),
        "assembly_worker": _worker_check(
            "assembly-worker-heartbeat",
            "组装 Worker",
            required=bool(getattr(settings, "READINESS_ASSEMBLY_WORKER_REQUIRED", True)),
        ),
        "comfyui": _comfyui_check(),
    }
    ready = all(item["ok"] for item in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "service": "autoprompt-studio",
        "version": "1.0.0",
        "checks": checks,
    }
