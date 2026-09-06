from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from fractions import Fraction
from typing import Any

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    ArtifactSelectionDecision,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationTask,
    ProductionRun,
    StageRun,
)
from studio.services.json_contracts import json_contract
from studio.services.json_contracts import canonical_json_bytes
from studio.services.configuration import (
    ConfigurationResolutionError,
    resolve_configuration,
)
from studio.services.provenance import (
    ProvenanceError,
    classify_task_output,
    sha256_file,
    resolve_task_output_path,
    verified_task_manifest,
)
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    parse_bible_candidate_unit_key,
    validate_bible_prompt_target,
)
from studio.services.security_boundaries import boundary_int


_MISSING = object()
_DIGEST_RE = re.compile(r"[0-9a-fA-F]{40,128}")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _artifact_media_probe_timeout_seconds() -> float:
    try:
        raw = resolve_configuration(
            required_keys=("artifact.media_probe_timeout_seconds",),
        ).values["artifact.media_probe_timeout_seconds"]
    except ConfigurationResolutionError as exc:
        raise ProvenanceError("受管媒体探测超时配置不可用") from exc
    if isinstance(raw, bool) or type(raw) not in {int, float} or float(raw) <= 0:
        raise ProvenanceError("受管媒体探测超时配置无效")
    return float(raw)
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
_WORKFLOW_PARAMETER_ALIASES = {
    "seed": {"seed", "sampling.seed"},
    "steps": {"steps", "sampling.steps"},
    "cfg_scale": {"cfg", "cfg_scale", "sampling.cfg_scale"},
    "sampler": {"sampler", "sampler_name", "sampling.sampler"},
    "scheduler": {"scheduler", "sampling.scheduler"},
    "denoise": {"denoise", "sampling.denoise"},
}
_GRAPH_PARAMETER_ALIASES = {
    **_WORKFLOW_PARAMETER_ALIASES,
    "width": {"width"},
    "height": {"height"},
    "fps": {"fps"},
    "frames": {"frames", "length"},
}
_CONFIG_PARAMETER_KEYS = {
    "production.default_include_audio": "include_audio",
    "production.default_multi_shot": "multi_shot",
    "production.default_first_frame_continuity": "first_frame_continuity",
    "production.default_last_frame_continuity": "last_frame_continuity",
    "production.default_shot_duration_ms": "default_shot_duration_ms",
}


def generated_artifact_storage_owner_id(artifact: GeneratedArtifact) -> str:
    """Return the frozen tenant owner encoded by a managed artifact path.

    ``ProductionRun.owner`` is the actor who started a run and can therefore be
    a staff user acting for somebody else's project.  Managed media belongs to
    the project tenant instead.  Older async artifacts were archived with the
    actor in their path; those rows remain readable as immutable history, but
    new writers must use the project owner.  This helper makes that legacy
    distinction explicit and never guesses a directory from the current actor.
    """

    if artifact.production_run_id is None:
        raise ProvenanceError("GeneratedArtifact 缺少 ProductionRun 归属")
    run = artifact.production_run
    project_id = str(run.project_id)
    try:
        parts = PurePosixPath(str(artifact.object_key or "")).parts
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("GeneratedArtifact object_key 无效") from exc
    expected_root = (
        "assemblies" if artifact.storage_backend == "local_media"
        else "generated-artifacts" if artifact.storage_backend == "managed_local"
        else ""
    )
    if (
        not expected_root
        or len(parts) < 3
        or parts[0] != expected_root
        or parts[2] != project_id
        or not parts[1]
    ):
        raise ProvenanceError("GeneratedArtifact object_key 未冻结到项目租户目录")
    path_owner_id = str(parts[1])

    if artifact.generation_task_id:
        task_owner_id = str(artifact.generation_task.owner_id)
        if task_owner_id != path_owner_id or artifact.generation_task.project_id != run.project_id:
            raise ProvenanceError("Comfy GeneratedArtifact 的任务与租户目录不一致")
        return task_owner_id

    metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
    if artifact.storage_backend == "local_media":
        assembly_job_id = metadata.get("assembly_job_id")
        if not assembly_job_id:
            raise ProvenanceError("assembly GeneratedArtifact 缺少冻结 job identity")
        job = AssemblyJob.objects.filter(pk=assembly_job_id).only(
            "owner_id", "project_id",
        ).first()
        if (
            job is None
            or str(job.owner_id) != path_owner_id
            or job.project_id != run.project_id
        ):
            raise ProvenanceError("assembly GeneratedArtifact 的 job 与租户目录不一致")
        return path_owner_id

    tenant_owner_id = metadata.get("tenant_owner_id")
    if tenant_owner_id is not None:
        if str(tenant_owner_id) != path_owner_id:
            raise ProvenanceError("async GeneratedArtifact 的冻结租户与目录不一致")
        return path_owner_id

    # Historical async rows written before tenant ownership was frozen used
    # either the run actor or the project owner.  Accept only those two factual
    # roots and keep this branch read-only; all current writers add tenant_owner_id.
    project_owner_id = str(run.project.owner_id)
    if (
        metadata.get("source") == "restricted_async_rest"
        and path_owner_id in {str(run.owner_id), project_owner_id}
    ):
        return path_owner_id
    if path_owner_id != project_owner_id:
        raise ProvenanceError("GeneratedArtifact 租户目录与项目所有者不一致")
    return path_owner_id


def _comparison_scalar(value: Any):
    """Return a bounded public scalar, never a path, URL, digest, or object id."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _MISSING
    if not isinstance(value, str):
        return _MISSING
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        return _MISSING
    if any(character in normalized for character in ("/", "\\", "\x00", "\r", "\n")):
        return _MISSING
    if "://" in normalized or _DIGEST_RE.fullmatch(normalized) or _UUID_RE.fullmatch(normalized):
        return _MISSING
    return normalized


def _set_safe(target: dict[str, Any], key: str, value: Any, *, positive: bool = False) -> None:
    normalized = _comparison_scalar(value)
    if normalized is _MISSING:
        return
    if positive and (
        isinstance(normalized, bool)
        or not isinstance(normalized, (int, float))
        or normalized <= 0
    ):
        return
    target[key] = normalized


def _collect_graph_scalars(value: Any, result: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
            for public_key, aliases in _GRAPH_PARAMETER_ALIASES.items():
                if key in aliases:
                    normalized = _comparison_scalar(item)
                    if normalized is not _MISSING and normalized not in result.setdefault(public_key, []):
                        result[public_key].append(normalized)
            _collect_graph_scalars(item, result)
    elif isinstance(value, list):
        for item in value:
            _collect_graph_scalars(item, result)


def _workflow_comparison_parameters(task: GenerationTask) -> dict[str, Any]:
    snapshot = task.capability_snapshot if isinstance(task.capability_snapshot, dict) else {}
    workflow = task.generated_workflow
    injected = workflow.injected_values if isinstance(workflow.injected_values, dict) else {}
    execution = injected.get("execution_metadata") if isinstance(injected.get("execution_metadata"), dict) else {}
    result: dict[str, Any] = {}
    model = snapshot.get("model") or execution.get("model_identifier") or injected.get("MODEL")
    _set_safe(result, "model", model)

    workflow_info: dict[str, Any] = {}
    _set_safe(workflow_info, "template", snapshot.get("template_slug"))
    _set_safe(workflow_info, "version", task.workflow_version, positive=True)
    _set_safe(workflow_info, "mode", snapshot.get("workflow_mode"))
    if workflow_info:
        result["workflow"] = workflow_info

    generation: dict[str, Any] = {}
    _set_safe(generation, "seed", task.seed)
    _set_safe(generation, "width", snapshot.get("width"), positive=True)
    _set_safe(generation, "height", snapshot.get("height"), positive=True)
    _set_safe(generation, "fps", snapshot.get("fps"), positive=True)
    _set_safe(generation, "requested_duration_ms", snapshot.get("requested_duration_ms"), positive=True)
    _set_safe(generation, "duration_ms", task.effective_duration_ms, positive=True)
    _set_safe(generation, "frames", task.effective_frames, positive=True)
    if isinstance(snapshot.get("include_audio"), bool):
        generation["include_audio"] = snapshot["include_audio"]

    sampling: dict[str, Any] = {}
    parameters = snapshot.get("workflow_parameters")
    if isinstance(parameters, dict):
        for raw_key, value in parameters.items():
            normalized_key = str(raw_key).casefold()
            for public_key, aliases in _WORKFLOW_PARAMETER_ALIASES.items():
                if normalized_key in aliases:
                    _set_safe(sampling, public_key, value)

    graph_values: dict[str, list[Any]] = {}
    _collect_graph_scalars(workflow.workflow_json, graph_values)
    for key, values in graph_values.items():
        if len(values) != 1:
            continue
        if key in {"width", "height", "fps", "frames"}:
            if key not in generation:
                _set_safe(generation, key, values[0], positive=True)
        elif key not in sampling:
            _set_safe(sampling, key, values[0])
    if "seed" not in sampling and "seed" in generation:
        sampling["seed"] = generation["seed"]
    if generation:
        result["generation"] = generation
    if sampling:
        result["sampling"] = sampling
    return result


def _attempt_comparison_parameters(attempt: GenerationAttempt) -> dict[str, Any]:
    snapshot = attempt.request_snapshot if isinstance(attempt.request_snapshot, dict) else {}
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    result: dict[str, Any] = {}
    _set_safe(result, "model", payload.get("model"))
    _set_safe(result, "operation", snapshot.get("operation") or attempt.operation)
    generation: dict[str, Any] = {}
    for key in ("seed", "width", "height", "fps", "duration_seconds", "audio"):
        _set_safe(
            generation,
            "include_audio" if key == "audio" else key,
            payload.get(key),
            positive=key in {"width", "height", "fps", "duration_seconds"},
        )
    if generation:
        result["generation"] = generation

    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    workflow_snapshot = route.get("workflow_snapshot") if isinstance(route.get("workflow_snapshot"), dict) else {}
    template = workflow_snapshot.get("template") if isinstance(workflow_snapshot.get("template"), dict) else {}
    workflow_info: dict[str, Any] = {}
    _set_safe(workflow_info, "template", template.get("slug"))
    _set_safe(workflow_info, "version", workflow_snapshot.get("workflow_version"), positive=True)
    _set_safe(
        workflow_info,
        "mode",
        (workflow_snapshot.get("input_contract") or {}).get("workflow_mode")
        if isinstance(workflow_snapshot.get("input_contract"), dict) else None,
    )
    if workflow_info:
        result["workflow"] = workflow_info
    return result


def build_comparison_parameters(
    *,
    task: GenerationTask | None = None,
    attempt: GenerationAttempt | None = None,
    stage_run: StageRun | None = None,
) -> dict[str, Any]:
    """Freeze only public generation settings used by this artifact."""

    result: dict[str, Any] = {"schema_version": 1}
    if task is not None:
        result.update(_workflow_comparison_parameters(task))
    if attempt is not None:
        attempt_values = _attempt_comparison_parameters(attempt)
        for key, value in attempt_values.items():
            if key in {"generation", "workflow"} and isinstance(value, dict):
                existing = result.get(key) if isinstance(result.get(key), dict) else {}
                result[key] = {**existing, **value}
            else:
                result[key] = value
    stage = stage_run or (attempt.stage_run if attempt is not None else None)
    snapshot = stage.config_snapshot if stage is not None and isinstance(stage.config_snapshot, dict) else {}
    route_index = attempt.route_index if attempt is not None and attempt.route_index is not None else (
        stage.selected_route_index if stage is not None else None
    )
    routes = snapshot.get("routes") if isinstance(snapshot.get("routes"), dict) else {}
    selected_snapshot = routes.get(str(route_index)) if route_index is not None else None
    if not isinstance(selected_snapshot, dict):
        selected_snapshot = snapshot.get("base") if isinstance(snapshot.get("base"), dict) else {}
    values = selected_snapshot.get("values") if isinstance(selected_snapshot.get("values"), dict) else {}
    configuration: dict[str, Any] = {}
    for source_key, public_key in _CONFIG_PARAMETER_KEYS.items():
        if source_key in values:
            _set_safe(configuration, public_key, values[source_key])
    if configuration:
        result["configuration"] = configuration
    return json_contract(
        result,
        max_bytes=boundary_int(
            "security.artifact.comparison_parameters_max_bytes",
        ),
    ).value


def _safe_object_key(item: dict) -> str:
    filename = str(item.get("filename") or "").replace("\\", "/")
    subfolder = str(item.get("subfolder") or "").replace("\\", "/")
    candidate = PurePosixPath(subfolder) / PurePosixPath(filename).name
    if not filename or candidate.is_absolute() or ".." in candidate.parts:
        raise ProvenanceError("生成产物 object key 无效")
    return candidate.as_posix()


def verified_local_media_facts(
    path: Path,
    *,
    expected_kind: str,
    max_bytes: int,
    include_observed_media: bool = False,
) -> dict:
    """Inspect actual bytes and return stable facts; extensions are never evidence."""

    candidate = path.resolve()
    if not candidate.is_file():
        raise ProvenanceError("媒体文件不存在")
    size = candidate.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ProvenanceError("媒体文件为空或超过大小上限")
    if expected_kind == "image":
        try:
            with Image.open(candidate) as image:
                image.verify()
                image_format = str(image.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ProvenanceError("图片产物无法按真实内容解码") from exc
        mime_type = Image.MIME.get(image_format)
        if not mime_type or not mime_type.startswith("image/"):
            raise ProvenanceError("图片产物缺少受支持的真实 MIME")
    elif expected_kind == "video":
        probe_arguments = [
            str(getattr(settings, "FFPROBE_BINARY", "ffprobe")), "-v", "error",
        ]
        if include_observed_media:
            probe_arguments.extend([
                "-count_frames", "-show_entries",
                (
                    "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,"
                    "nb_frames,nb_read_frames,duration:format=format_name,duration"
                ),
            ])
        else:
            probe_arguments.extend([
                "-show_entries", "stream=codec_type:format=format_name",
            ])
        probe_arguments.extend(["-of", "json", str(candidate)])
        try:
            result = subprocess.run(
                probe_arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=_artifact_media_probe_timeout_seconds(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvenanceError("视频产物无法执行 ffprobe") from exc
        if result.returncode != 0:
            raise ProvenanceError("视频产物未通过 ffprobe")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ProvenanceError("ffprobe 返回无效 JSON") from exc
        streams = payload.get("streams") if isinstance(payload, dict) else None
        video = next((
            item for item in streams or []
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ), None)
        if not isinstance(streams, list) or video is None:
            raise ProvenanceError("视频产物不包含视频流")
        format_name = str((payload.get("format") or {}).get("format_name") or "").casefold()
        if "mp4" in format_name or "mov" in format_name:
            mime_type = "video/mp4"
        elif "webm" in format_name:
            mime_type = "video/webm"
        elif "matroska" in format_name:
            mime_type = "video/x-matroska"
        else:
            mime_type = "video/" + (format_name.split(",", 1)[0] or "unknown")
        if include_observed_media:
            try:
                width = int(video.get("width"))
                height = int(video.get("height"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProvenanceError("视频产物无法可靠观测宽高") from exc
            if width <= 0 or height <= 0:
                raise ProvenanceError("视频产物无法可靠观测宽高")
            frame_rate = None
            for raw_rate in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
                try:
                    candidate_rate = Fraction(str(raw_rate))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if candidate_rate > 0:
                    frame_rate = candidate_rate
                    break
            if frame_rate is None:
                raise ProvenanceError("视频产物无法可靠观测帧率")
            frames = None
            for raw_frames in (video.get("nb_read_frames"), video.get("nb_frames")):
                try:
                    candidate_frames = int(raw_frames)
                except (TypeError, ValueError, OverflowError):
                    continue
                if candidate_frames > 0:
                    frames = candidate_frames
                    break
            if frames is None:
                raise ProvenanceError("视频产物无法可靠观测实际帧数")
            duration_seconds = None
            for raw_duration in (
                video.get("duration"), (payload.get("format") or {}).get("duration"),
            ):
                try:
                    candidate_duration = float(raw_duration)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(candidate_duration) and candidate_duration > 0:
                    duration_seconds = candidate_duration
                    break
            if duration_seconds is None:
                duration_seconds = frames / float(frame_rate)
            if not math.isfinite(duration_seconds) or duration_seconds <= 0:
                raise ProvenanceError("视频产物无法可靠观测实际时长")
            observed_media = {
                "schema_version": 1,
                "observation_source": "ffprobe",
                "width": width,
                "height": height,
                "fps": round(float(frame_rate), 6),
                "frame_rate": f"{frame_rate.numerator}/{frame_rate.denominator}",
                "frames": frames,
                "duration_ms": round(duration_seconds * 1000),
                "has_audio": any(
                    isinstance(item, dict) and item.get("codec_type") == "audio"
                    for item in streams
                ),
            }
    elif expected_kind == "audio":
        probe_arguments = [
            str(getattr(settings, "FFPROBE_BINARY", "ffprobe")),
            "-v", "error",
            "-show_entries", "stream=codec_type,duration:format=format_name,duration",
            "-of", "json", str(candidate),
        ]
        try:
            result = subprocess.run(
                probe_arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=_artifact_media_probe_timeout_seconds(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvenanceError("音频产物无法执行 ffprobe") from exc
        if result.returncode != 0:
            raise ProvenanceError("音频产物未通过 ffprobe")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ProvenanceError("ffprobe 返回无效 JSON") from exc
        streams = payload.get("streams") if isinstance(payload, dict) else None
        audio = next((
            item for item in streams or []
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ), None)
        if not isinstance(streams, list) or audio is None:
            raise ProvenanceError("音频产物不包含音频流")
        format_name = str((payload.get("format") or {}).get("format_name") or "").casefold()
        if "wav" in format_name:
            mime_type = "audio/wav"
        elif "mp3" in format_name:
            mime_type = "audio/mpeg"
        elif "flac" in format_name:
            mime_type = "audio/flac"
        elif "ogg" in format_name:
            mime_type = "audio/ogg"
        elif "mp4" in format_name or "mov" in format_name:
            mime_type = "audio/mp4"
        else:
            mime_type = "audio/" + (format_name.split(",", 1)[0] or "unknown")
        duration_seconds = None
        for raw_duration in (
            audio.get("duration"), (payload.get("format") or {}).get("duration"),
        ):
            try:
                candidate_duration = float(raw_duration)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(candidate_duration) and candidate_duration > 0:
                duration_seconds = candidate_duration
                break
        if duration_seconds is None:
            raise ProvenanceError("音频产物无法可靠观测实际时长")
        observed_media = {
            "schema_version": 1,
            "observation_source": "ffprobe",
            "duration_ms": round(duration_seconds * 1000),
            "has_audio": True,
        }
    else:
        raise ProvenanceError("媒体类型不受支持")
    facts = {
        "mime_type": mime_type,
        "size_bytes": size,
        "sha256": sha256_file(candidate),
    }
    if expected_kind in {"video", "audio"} and include_observed_media:
        facts["observed_media"] = observed_media
    return facts


def _artifact_limit(production_run: ProductionRun | None) -> int:
    if production_run is not None:
        snapshot = production_run.config_snapshot if isinstance(production_run.config_snapshot, dict) else {}
        values = snapshot.get("values") if isinstance(snapshot.get("values"), dict) else {}
        value = values.get("artifact.max_bytes")
        if type(value) is not int:
            raise ValidationError("ProductionRun 配置快照缺少 artifact.max_bytes")
        return value
    return int(resolve_configuration(required_keys=("artifact.max_bytes",)).values["artifact.max_bytes"])


def inspect_task_output_media(task: GenerationTask, outputs: list[dict]) -> list[dict]:
    """Attach byte-derived output facts before provenance is sealed.

    These facts describe the returned container.  They deliberately do not
    replace ``GenerationTask.effective_*``, which is the immutable workflow
    request plan.
    """

    attempt = task.platform_attempts.select_related(
        "stage_run__production_run",
    ).order_by("-attempt_number").first()
    production_run = attempt.stage_run.production_run if attempt is not None else None
    max_bytes = _artifact_limit(production_run)
    original_outputs = task.outputs
    inspected: list[dict] = []
    task.outputs = outputs
    try:
        for index, raw in enumerate(outputs):
            item = dict(raw) if isinstance(raw, dict) else raw
            media_kind = classify_task_output(item)
            if not isinstance(item, dict) or media_kind not in {"image", "video"}:
                inspected.append(item)
                continue
            facts = verified_local_media_facts(
                resolve_task_output_path(task, index),
                expected_kind=media_kind,
                max_bytes=max_bytes,
                include_observed_media=media_kind == "video",
            )
            if isinstance(facts.get("observed_media"), dict):
                item["observed_media"] = facts["observed_media"]
            inspected.append(item)
    finally:
        task.outputs = original_outputs
    return inspected


def _archive_task_output(
    task: GenerationTask,
    output_index: int,
    item: dict,
    *,
    expected_kind: str,
    expected_sha256: str,
    max_bytes: int,
) -> dict:
    source = resolve_task_output_path(task, output_index)
    safe_name = PurePosixPath(str(item.get("filename") or "")).name
    if not safe_name:
        raise ProvenanceError("生成产物文件名无效")
    source_facts = verified_local_media_facts(
        source,
        expected_kind=expected_kind,
        max_bytes=max_bytes,
        include_observed_media=expected_kind == "video",
    )
    from studio.services.workflow_resolution import validate_resolved_image_output
    try:
        validate_resolved_image_output(source, (task.capability_snapshot or {}).get("output_contract") or {})
    except ValidationError as exc:
        raise ProvenanceError(str(exc)) from exc
    mime_type = source_facts["mime_type"]
    if source_facts["sha256"] != expected_sha256:
        raise ProvenanceError("实际媒体 checksum 与来源清单不一致")
    root = Path(settings.MEDIA_ROOT).resolve()
    relative = Path(
        "generated-artifacts",
        str(task.owner_id),
        str(task.project_id),
        str(task.id),
        str(output_index),
        safe_name,
    )
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ProvenanceError("受管产物路径越过 MEDIA_ROOT")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".archive-",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    digest = hashlib.sha256()
    size = 0
    try:
        with handle, source.open("rb") as source_handle:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ProvenanceError("生成产物超过正式归档大小上限")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        archived_sha = digest.hexdigest()
        if archived_sha != expected_sha256:
            raise ProvenanceError("受管归档 checksum 与来源清单不一致")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if not target.is_file() or target.stat().st_size != size or sha256_file(target) != expected_sha256:
        target.unlink(missing_ok=True)
        raise ProvenanceError("受管归档落盘后二次校验失败")
    return {
        "storage_backend": "managed_local",
        "object_key": relative.as_posix(),
        "mime_type": mime_type,
        "size_bytes": size,
        "sha256": expected_sha256,
        **(
            {"observed_media": source_facts["observed_media"]}
            if isinstance(source_facts.get("observed_media"), dict) else {}
        ),
    }


@transaction.atomic
def record_generation_task_artifacts(
    task: GenerationTask,
    *,
    production_run: ProductionRun | None = None,
    stage_run: StageRun | None = None,
    generation_attempt: GenerationAttempt | None = None,
) -> list[GeneratedArtifact]:
    task = GenerationTask.objects.select_for_update().select_related(
        "project", "generated_workflow__prompt_artifact",
    ).get(pk=task.pk)
    if task.status != GenerationTask.Status.SUCCEEDED:
        raise ValidationError("只有 succeeded task 可以写正式 artifact manifest")
    if generation_attempt is None:
        generation_attempt = task.platform_attempts.order_by("-attempt_number").first()
    if stage_run is None and generation_attempt is not None:
        stage_run = generation_attempt.stage_run
    if production_run is None and stage_run is not None:
        production_run = stage_run.production_run
    if generation_attempt is not None:
        generation_attempt = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run__project",
        ).get(pk=generation_attempt.pk)
        if generation_attempt.generation_task_id != task.id:
            raise ValidationError(
                "GenerationAttempt 与 GenerationTask 不是同一冻结执行链",
            )
        if stage_run is None or generation_attempt.stage_run_id != stage_run.id:
            raise ValidationError("GenerationAttempt 与 StageRun 归属不一致")
        if (
            production_run is None
            or stage_run.production_run_id != production_run.id
            or generation_attempt.stage_run.production_run_id != production_run.id
            or task.project_id != production_run.project_id
            or task.owner_id != production_run.project.owner_id
        ):
            raise ValidationError("GenerationTask/Attempt 与 ProductionRun 项目租户不一致")
        capability = (
            task.capability_snapshot
            if isinstance(task.capability_snapshot, dict) else {}
        )
        prompt = task.generated_workflow.prompt_artifact
        expected_capability = {
            "production_run_id": str(production_run.id),
            "stage_run_id": str(stage_run.id),
            "generation_attempt_id": str(generation_attempt.id),
            "prompt_artifact_id": str(prompt.id),
        }
        if any(capability.get(key) != value for key, value in expected_capability.items()):
            raise ValidationError("GenerationTask capability execution identity 已漂移")
        if str(generation_attempt.request_snapshot.get("prompt_artifact_id") or "") != str(prompt.id):
            raise ValidationError("GenerationAttempt 请求与 GenerationTask Prompt 不一致")
    created: list[GeneratedArtifact] = []
    max_bytes = _artifact_limit(production_run)
    media_index = 0
    variant_number = 1
    grouped_unit_key = generation_attempt.unit_key if generation_attempt is not None else ""
    bible_identity = None
    if (
        stage_run is not None
        and stage_run.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
        and generation_attempt is not None
    ):
        bible_identity = parse_bible_candidate_unit_key(grouped_unit_key)
        variant_number = bible_identity["variant_number"]
    elif ":variant:" in grouped_unit_key:
        grouped_unit_key, raw_variant = grouped_unit_key.rsplit(":variant:", 1)
        try:
            variant_number = int(raw_variant)
        except ValueError as exc:
            raise ValidationError("GenerationAttempt variant identity 无效") from exc
        if variant_number < 1:
            raise ValidationError("GenerationAttempt variant identity 必须是正整数")
    variant_group = (
        bible_identity["variant_group"]
        if bible_identity is not None
        else f"stage:{stage_run.id}:unit:{grouped_unit_key}"
        if stage_run is not None and generation_attempt is not None
        else f"task:{task.id}"
    )
    existing_selection = GeneratedArtifact.objects.filter(
        production_run=production_run,
        variant_group=variant_group,
        status=GeneratedArtifact.Status.SELECTED,
    ).exists() if production_run is not None else False
    outputs = task.outputs if isinstance(task.outputs, list) else []
    outputs_observed_metadata_updated = False
    indexed_outputs = list(enumerate(outputs))
    workflow = task.generated_workflow
    semantic_compiled = bool(
        workflow.workflow_template_version_id
        and workflow.compiler_id
        and (task.capability_snapshot or {}).get("workflow_contract_sha256")
    )
    if semantic_compiled:
        injected = workflow.injected_values if isinstance(workflow.injected_values, dict) else {}
        frozen_contract = injected.get("output_contract")
        task_contract = (task.capability_snapshot or {}).get("output_contract")
        if frozen_contract != task_contract or not isinstance(frozen_contract, dict):
            raise ProvenanceError("semantic task 的冻结 output contract 副本不一致")
        primary = frozen_contract.get("primary")
        if (
            (frozen_contract.get("schema_version"), set(frozen_contract)) not in (
                (1, {"schema_version", "primary"}),
                (2, {"schema_version", "primary", "resolution"}),
            )
            or not isinstance(primary, dict)
            or set(primary) != {"node_id", "kind", "required"}
            or not isinstance(primary.get("node_id"), str)
            or primary.get("kind") not in {"image", "video"}
            or primary.get("required") is not True
        ):
            raise ProvenanceError("semantic task 的冻结 primary output contract 无效")
        indexed_outputs = [
            (index, item)
            for index, item in indexed_outputs
            if isinstance(item, dict)
            and str(item.get("node_id") or "") == primary["node_id"]
            and classify_task_output(item) == primary["kind"]
        ]
        if len(indexed_outputs) != 1:
            raise ProvenanceError("semantic task 的声明 primary node 必须且只能归档一个输出")

    for index, item in indexed_outputs:
        if not isinstance(item, dict):
            continue
        media_kind = classify_task_output(item)
        if media_kind not in {"image", "video"}:
            continue
        manifest = verified_task_manifest(task, index)
        artifact_facts = manifest.get("artifact") if isinstance(manifest, dict) else {}
        integrity = manifest.get("integrity") if isinstance(manifest, dict) else {}
        if not isinstance(artifact_facts, dict) or not isinstance(integrity, dict):
            raise ProvenanceError("生成产物 provenance manifest 结构无效")
        expected_sha = str(artifact_facts.get("sha256") or "")
        archived = _archive_task_output(
            task,
            index,
            item,
            expected_kind=media_kind,
            expected_sha256=expected_sha,
            max_bytes=max_bytes,
        )
        observed_media = archived.pop("observed_media", None)
        signed_observed_media = artifact_facts.get("observed_media")
        if (
            isinstance(signed_observed_media, dict)
            and signed_observed_media != observed_media
        ):
            raise ProvenanceError("视频产物观测元数据与签名来源清单不一致")
        media_index += 1
        metadata = {
            "schema_version": 1,
            "task_id": str(task.id),
            "generation_attempt_id": (
                str(generation_attempt.id) if generation_attempt is not None else None
            ),
            "production_run_id": (
                str(production_run.id) if production_run is not None else None
            ),
            "stage_run_id": str(stage_run.id) if stage_run is not None else None,
            "request_sha256": (
                generation_attempt.request_sha256
                if generation_attempt is not None else ""
            ),
            "output_index": index,
            "node_id": str(item.get("node_id") or ""),
            "provenance_manifest_sha256": str(integrity.get("payload_sha256") or ""),
            **(
                {"observed_media": observed_media}
                if isinstance(observed_media, dict) else {}
            ),
        }
        if bible_identity is not None:
            prompt = getattr(workflow, "prompt_artifact", None)
            options = prompt.request_options if prompt and isinstance(prompt.request_options, dict) else {}
            target = validate_bible_prompt_target(options.get("target"))
            if (
                target["entity_type"] != bible_identity["entity_type"]
                or target["entity_id"] != bible_identity["entity_id"]
            ):
                raise ProvenanceError("Bible 参考图 task unit 与 Prompt target 不一致")
            metadata.update({
                "generation_purpose": "bible_reference_image",
                "target": target,
                "prompt_artifact_id": str(prompt.id),
                "prompt_content_hash": prompt.content_hash,
                "prompt_profile_version_id": (
                    str(prompt.prompt_profile_version_id)
                    if prompt.prompt_profile_version_id else None
                ),
                "prompt_profile_binding_key": prompt.prompt_profile_binding_key,
                "prompt_profile_binding_id": str(
                    options.get("prompt_profile_binding_id") or ""
                ),
                "prompt_profile_contract_sha256": prompt.prompt_profile_contract_sha256,
                "source_snapshot_sha256": target["source_snapshot_sha256"],
            })
        if isinstance(observed_media, dict):
            if item.get("observed_media") != observed_media:
                item["observed_media"] = observed_media
                outputs[index] = item
                outputs_observed_metadata_updated = True
        metadata_contract = json_contract(
            metadata,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        comparison_parameters = build_comparison_parameters(
            task=task,
            attempt=generation_attempt,
            stage_run=stage_run,
        )
        artifact, was_created = GeneratedArtifact.objects.get_or_create(
            manifest_key=f"task:{task.id}:{index}",
            defaults={
                "production_run": production_run,
                "stage_run": stage_run,
                "generation_attempt": generation_attempt,
                "generation_task": task,
                "kind": media_kind,
                "role": (
                    "bible_reference_candidate"
                    if bible_identity is not None
                    else "primary_generation_output" if media_index == 1 else "generation_output"
                ),
                "variant_group": variant_group,
                "variant_index": (
                    (variant_number * 10_000)
                    + ((generation_attempt.attempt_number - 1) * 100)
                    + media_index
                    if generation_attempt is not None else media_index
                ),
                "status": (
                    GeneratedArtifact.Status.AVAILABLE
                    if bible_identity is not None
                    else GeneratedArtifact.Status.SELECTED
                    if media_index == 1 and production_run is not None and not existing_selection
                    else GeneratedArtifact.Status.AVAILABLE
                ),
                **archived,
                "manifest_metadata": metadata,
                "comparison_parameters": comparison_parameters,
                "manifest_schema_version": metadata_contract.schema_version,
                "manifest_metadata_size_bytes": metadata_contract.size_bytes,
                "manifest_metadata_sha256": metadata_contract.sha256,
            },
        )
        if not was_created:
            if (
                artifact.sha256 != archived["sha256"]
                or artifact.storage_backend != "managed_local"
                or artifact.object_key != archived["object_key"]
            ):
                raise ProvenanceError("已有 GeneratedArtifact 与任务 provenance 不一致")
        created.append(artifact)
    if not created:
        raise ProvenanceError("成功任务没有可验证的图片或视频输出")
    if bible_identity is not None and (
        len(created) != 1 or created[0].kind != GeneratedArtifact.Kind.IMAGE
    ):
        raise ProvenanceError("Bible 参考图 attempt 必须且只能归档一个图片 primary output")
    if outputs_observed_metadata_updated:
        task.outputs = outputs
        task.save(update_fields=("outputs", "updated_at"))
    if generation_attempt is not None:
        generation_attempt.status = GenerationAttempt.Status.SUCCEEDED
        generation_attempt.result_sha256 = hashlib.sha256(canonical_json_bytes([
            {"id": str(item.id), "sha256": item.sha256} for item in created
        ])).hexdigest()
        generation_attempt.finished_at = timezone.now()
        generation_attempt.error_code = ""
        generation_attempt.error_message = ""
        generation_attempt.save(update_fields=(
            "status", "result_sha256", "finished_at", "error_code",
            "error_message", "updated_at",
        ))
    return created


@transaction.atomic
def record_assembly_artifact(
    job: AssemblyJob,
    *,
    production_run: ProductionRun,
    stage_run: StageRun,
) -> GeneratedArtifact:
    job = AssemblyJob.objects.select_for_update().get(pk=job.pk)
    if job.status != AssemblyJob.Status.SUCCEEDED or not job.output_path:
        raise ValidationError("只有真实 succeeded assembly 可以写正式 artifact manifest")
    output = Path(job.output_path).resolve()
    allowed_root = (
        Path(settings.MEDIA_ROOT).resolve()
        / "assemblies"
        / str(job.owner_id)
        / str(job.project_id)
    ).resolve()
    if not output.is_relative_to(allowed_root) or not output.is_file():
        raise ValidationError("assembly 输出不存在或越过租户目录")
    digest = sha256_file(output)
    metadata = {
        "schema_version": 1,
        "assembly_job_id": str(job.id),
        "scope": job.scope,
        "episode_id": str(job.episode_id) if job.episode_id else None,
        "source_signature": job.source_signature,
    }
    metadata_contract = json_contract(
        metadata,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    artifact, created = GeneratedArtifact.objects.get_or_create(
        manifest_key=f"assembly:{job.id}",
        defaults={
            "production_run": production_run,
            "stage_run": stage_run,
            "kind": GeneratedArtifact.Kind.ASSEMBLY,
            "role": "episode_master" if job.episode_id else "series_master",
            "variant_group": (
                f"episode_master:{job.episode_id}" if job.episode_id else "series_master"
            ),
            "variant_index": 1,
            "status": GeneratedArtifact.Status.SELECTED,
            "storage_backend": "local_media",
            "object_key": output.relative_to(Path(settings.MEDIA_ROOT).resolve()).as_posix(),
            "mime_type": "video/mp4",
            "size_bytes": output.stat().st_size,
            "sha256": digest,
            "manifest_metadata": metadata,
            "manifest_schema_version": metadata_contract.schema_version,
            "manifest_metadata_size_bytes": metadata_contract.size_bytes,
            "manifest_metadata_sha256": metadata_contract.sha256,
        },
    )
    if not created and artifact.sha256 != digest:
        raise ValidationError("已有 assembly artifact 与当前文件校验和不一致")
    return artifact


@transaction.atomic
def select_artifact(artifact: GeneratedArtifact, *, user) -> GeneratedArtifact:
    artifact = GeneratedArtifact.objects.select_for_update().select_related(
        "production_run__project", "stage_run",
    ).get(pk=artifact.pk)
    if artifact.production_run_id is None:
        raise ValidationError("历史未归属 artifact 不能通过 ProductionRun 选择")
    if artifact.production_run.project.owner_id != user.id and not user.is_staff:
        raise ValidationError("artifact 不属于当前用户")
    if (
        artifact.stage_run is not None
        and artifact.stage_run.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
    ):
        raise ValidationError("Bible 参考图候选只能在 bible_image_review 批量选择并绑定")
    if artifact.status not in {
        GeneratedArtifact.Status.AVAILABLE,
        GeneratedArtifact.Status.SELECTED,
    }:
        raise ValidationError("只有 available artifact 可以选择")
    if artifact.generation_attempt_id:
        attempt_status = GenerationAttempt.objects.filter(
            pk=artifact.generation_attempt_id,
        ).values_list("status", flat=True).first()
        if attempt_status != GenerationAttempt.Status.SUCCEEDED:
            raise ValidationError("只有成功 GenerationAttempt 的产物可以选择")
    GeneratedArtifact.objects.filter(
        production_run=artifact.production_run,
        variant_group=artifact.variant_group,
        status=GeneratedArtifact.Status.SELECTED,
    ).exclude(pk=artifact.pk).update(status=GeneratedArtifact.Status.AVAILABLE)
    artifact.status = GeneratedArtifact.Status.SELECTED
    artifact.save(update_fields=("status", "updated_at"))
    if artifact.stage_run_id:
        revision = (
            ArtifactSelectionDecision.objects.filter(
                stage_run_id=artifact.stage_run_id,
                variant_group=artifact.variant_group,
            ).aggregate(value=Max("revision"))["value"]
            or 0
        ) + 1
        ArtifactSelectionDecision.objects.create(
            stage_run_id=artifact.stage_run_id,
            variant_group=artifact.variant_group,
            revision=revision,
            artifact=artifact,
            selected_by=user,
        )
    return artifact
