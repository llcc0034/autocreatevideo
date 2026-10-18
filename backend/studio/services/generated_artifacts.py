from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import warnings
from fractions import Fraction
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

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
    the project tenant instead. Async artifacts freeze that tenant explicitly;
    incompatible old paths are not resolved through a second owner fallback.
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
    project_owner_id = str(run.project.owner_id)
    if metadata.get("source") == "restricted_async_rest" and str(tenant_owner_id) != project_owner_id:
        raise ProvenanceError("async GeneratedArtifact 缺少一致的项目租户事实")
    if tenant_owner_id is not None:
        if str(tenant_owner_id) != path_owner_id:
            raise ProvenanceError("async GeneratedArtifact 的冻结租户与目录不一致")
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


def _attempt_comparison_parameters(attempt: GenerationAttempt) -> dict[str, Any]:
    from studio.services.execution_preparations import read_frozen_execution_request
    from studio.services.execution_task_links import validate_execution_task_links
    validate_execution_task_links(attempt)
    _, definition, transport = read_frozen_execution_request(attempt.preparation)
    if attempt.request_sha256 != attempt.preparation.request_sha256:
        raise ValidationError("Artifact comparison request does not match its Preparation")
    result = {}
    _set_safe(result, "operation", transport.value["operation"])
    _set_safe(result, "model", definition.value["target"]["model"]["identifier"])
    parameters = {}
    for key, value in transport.value["parameters"]["values"].items():
        _set_safe(parameters, key, value)
    if parameters:
        result["generation"] = parameters
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
        from studio.services.execution_production_workflow_queue import read_prepared_generation_task
        linked = read_prepared_generation_task(task).attempt
        if attempt is not None and attempt.pk != linked.pk:
            raise ValidationError("候选比较参数必须来自任务自身的执行准备")
        attempt = linked
    if attempt is not None:
        attempt_values = _attempt_comparison_parameters(attempt)
        for key, value in attempt_values.items():
            if key in {"generation", "workflow"} and isinstance(value, dict):
                existing = result.get(key) if isinstance(result.get(key), dict) else {}
                result[key] = {**existing, **value}
            else:
                result[key] = value
        return json_contract(result, max_bytes=boundary_int("security.artifact.comparison_parameters_max_bytes")).value
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
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(candidate) as image:
                    width, height = image.size
                    image.verify()
                    image_format = str(image.format or "").upper()
                if include_observed_media:
                    with Image.open(candidate) as image:
                        frame_count = image.n_frames if hasattr(image, "n_frames") else 1
                        image.load()
                        width, height = ImageOps.exif_transpose(image).size
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ProvenanceError("图片产物无法按真实内容解码") from exc
        mime_type = Image.MIME.get(image_format)
        if not mime_type or not mime_type.startswith("image/"):
            raise ProvenanceError("图片产物缺少受支持的真实 MIME")
        if width <= 0 or height <= 0:
            raise ProvenanceError("图片产物无法可靠观测宽高")
        observed_media = {"schema_version": 1, "observation_source": "image_decoder", "width": width, "height": height}
        if include_observed_media:
            observed_media["frame_count"] = frame_count
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
    if include_observed_media:
        facts["observed_media"] = observed_media
    return facts


def verified_generated_artifact_media(artifact, *, max_bytes, include_observed_media=False):
    """Verify managed media bytes against their manifest, without selecting it.

    Ownership, production scope and the current selection belong to the caller.
    Assembly and execution-input preparation share this file-integrity path.
    No Asset is created and no remote URL is fetched here.
    """
    kind = "video" if artifact.kind == GeneratedArtifact.Kind.ASSEMBLY else artifact.kind
    if kind not in {"image", "video", "audio"}:
        raise ProvenanceError("产物不是可解析的图片、视频或音频")
    if artifact.storage_backend not in {"managed_local", "local_media"}:
        raise ProvenanceError("产物尚未归档到正式受管存储")
    if (not isinstance(artifact.object_key, str) or not artifact.object_key
            or type(artifact.size_bytes) is not int or artifact.size_bytes <= 0
            or not isinstance(artifact.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
            or not artifact.mime_type):
        raise ProvenanceError("产物缺少完整的文件 manifest")
    if type(max_bytes) is not int or max_bytes <= 0 or artifact.size_bytes > max_bytes:
        raise ProvenanceError("产物超过当前媒体大小边界")
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / artifact.object_key).resolve()
    if not path.is_relative_to(root):
        raise ProvenanceError("受管产物路径越过 MEDIA_ROOT")
    facts = verified_local_media_facts(path, expected_kind=kind, max_bytes=max_bytes,
        include_observed_media=include_observed_media)
    if any(facts[key] != getattr(artifact, key) for key in ("sha256", "size_bytes", "mime_type")):
        raise ProvenanceError("受管媒体产物 manifest 与真实字节不一致")
    return path, facts


def _artifact_limit(production_run: ProductionRun | None) -> int:
    if production_run is not None:
        from studio.services.json_contracts import verify_json_contract
        snapshot = verify_json_contract(production_run.config_snapshot, schema_version=production_run.config_snapshot_schema_version,
            size_bytes=production_run.config_snapshot_size_bytes, sha256=production_run.config_snapshot_sha256,
            max_bytes=_stage_snapshot_max_bytes()).value
        values = snapshot.get("values") if isinstance(snapshot.get("values"), dict) else {}
        value = values.get("artifact.max_bytes")
        if type(value) is not int or value <= 0:
            raise ValidationError("ProductionRun 配置快照缺少 artifact.max_bytes")
        return value
    return int(resolve_configuration(required_keys=("artifact.max_bytes",)).values["artifact.max_bytes"])


def _prepared_task_primary(task, outputs):
    """One actual frozen primary output; never an old task snapshot fallback."""
    from studio.services.execution_production_workflow_queue import read_prepared_generation_task
    from studio.services.execution_workflow_outputs import verify_comfy_primary_output
    from studio.services.json_contracts import verify_json_contract

    context = read_prepared_generation_task(task)
    media, run = context.media, context.attempt.stage_run.production_run
    configuration = verify_json_contract(run.config_snapshot, schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes, sha256=run.config_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes())
    if configuration.sha256 != media["execution_deadlines"]["configuration_sha256"]:
        raise ValidationError("归档大小限制必须来自本次冻结Run")
    limit = _artifact_limit(run)
    if type(limit) is not int or limit <= 0:
        raise ValidationError("本次Run缺少有效归档大小限制")
    source, facts = verify_comfy_primary_output({"outputs": outputs}, output_contract=media["output_contract"],
        expected_kind=media["output_kind"], expected_prefix=media["output_prefix"],
        resolved_semantics=media["payload"]["compiled_workflow"]["resolved_semantics"], max_bytes=limit)
    primary = media["output_contract"]["primary"]
    index, item = next((index, row) for index, row in enumerate(outputs)
        if isinstance(row, dict) and str(row.get("node_id", "")) == str(primary["node_id"])
        and classify_task_output(row) == media["output_kind"])
    return context, index, item, source, facts, limit


def inspect_task_output_media(task: GenerationTask, outputs: list[dict]) -> list[dict]:
    """Only the declared primary becomes a formal task output, from actual bytes."""
    _context, _index, item, _path, facts, _limit = _prepared_task_primary(task, outputs)
    return [{**item, "observed_media": facts["observed_media"]}]


def _archive_task_output(
    task: GenerationTask,
    output_index: int,
    item: dict,
    *,
    expected_kind: str,
    expected_sha256: str,
    max_bytes: int,
    output_contract: dict,
) -> dict:
    source = resolve_task_output_path(task, output_index)
    safe_name = PurePosixPath(str(item.get("filename") or "")).name
    if not safe_name:
        raise ProvenanceError("生成产物文件名无效")
    from studio.services.workflow_resolution import validate_resolved_image_output
    try:
        validate_resolved_image_output(source, output_contract)
    except ValidationError as exc:
        raise ProvenanceError(str(exc)) from exc
    relative = Path("generated-artifacts", str(task.owner_id), str(task.project_id), str(task.id), str(output_index), safe_name)
    return archive_local_media(source, relative=relative, expected_kind=expected_kind,
        expected_sha256=expected_sha256, max_bytes=max_bytes)


def archive_local_media(source: Path, *, relative: Path, expected_kind: str, expected_sha256: str, max_bytes: int) -> dict:
    """Archive verified local media for original Artifact writers, not an HTTP path.

    The caller authorizes the source and supplies the tenant-relative destination.
    An existing content-addressed file is verified, never overwritten as repair.
    File creation is independent of the DB transaction; a retried writer reuses
    these exact bytes but must still create and verify its original DB facts.
    """
    source_facts = verified_local_media_facts(
        source,
        expected_kind=expected_kind,
        max_bytes=max_bytes,
        include_observed_media=True,
    )
    if source_facts["sha256"] != expected_sha256:
        raise ProvenanceError("实际媒体 checksum 与来源清单不一致")
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / relative).resolve()
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "generated-artifacts" or not target.is_relative_to(root):
        raise ProvenanceError("受管产物路径越过 MEDIA_ROOT")
    result = {"storage_backend": "managed_local", "object_key": relative.as_posix(), **source_facts}
    if target.exists():
        archived_facts = verified_local_media_facts(target, expected_kind=expected_kind, max_bytes=max_bytes, include_observed_media=True)
        if archived_facts != source_facts:
            raise ProvenanceError("已有受管归档与本次来源不一致，不能覆盖修补")
        return result
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
    return result


def record_generation_task_artifacts(
    task: GenerationTask,
    *,
    production_run: ProductionRun | None = None,
    stage_run: StageRun | None = None,
    generation_attempt: GenerationAttempt | None = None,
) -> list[GeneratedArtifact]:
    """Archive this Preparation's primary into the original candidate registry.

    Run/Stage -> Preparation -> Attempt -> Task locks also cover worker success.
    No implicit selection, old PromptArtifact identity, or bill inference.
    """
    from studio.services.execution_production_workflow_queue import (
        locked_prepared_generation_task, read_prepared_generation_task,
    )
    from studio.services.json_contracts import assign_json_contract, verify_json_contract

    context = read_prepared_generation_task(task)
    original = context.attempt
    if ((generation_attempt is not None and generation_attempt.pk != original.pk)
            or (stage_run is not None and stage_run.pk != original.stage_run_id)
            or (production_run is not None and production_run.pk != original.stage_run.production_run_id)):
        raise ValidationError("产物归档不能替换原任务的调用或阶段归属")
    with locked_prepared_generation_task(task, context) as (stage, preparation, attempt, current):
        run = stage.production_run
        if (current.status != "succeeded" or attempt.generation_task_id != current.pk
                or current.owner_id != task.owner_id or current.project_id != task.project_id
                or current.attempts != task.attempts or current.started_at != task.started_at
                or preparation.status != "dispatched"
                or attempt.status not in {"submitted", "succeeded"} or attempt.ignore_late_result
                or attempt.cancel_requested_at is not None
                or run.status not in {"running", "blocked", "awaiting_input", "awaiting_approval"}
                or stage.status not in {"running", "blocked", "awaiting_approval"}):
            raise ValidationError("归档必须来自仍有效的本次主调用，停止后的回执不能生成候选")
        context, index, item, _source, facts, limit = _prepared_task_primary(current, current.outputs)
        media = context.media
        if len(current.outputs) != 1 or index != 0:
            raise ValidationError("正式任务输出只能包含本次声明的唯一主产物")
        if (attempt.provider_request_id != str(attempt.pk)
                or current.submission_token != str(attempt.pk)
                or current.comfy_prompt_id != str(attempt.pk)
                or attempt.submission_started_at is None or attempt.submitted_at is None):
            raise ValidationError("产物缺少本次准备的明确远端提交事实")
        manifest = verified_task_manifest(current, index)
        identity = {
            "task_id": str(current.pk), "generation_attempt_id": str(attempt.pk),
            "preparation_id": str(preparation.pk), "execution_instance_version_id": str(attempt.execution_instance_version_id),
            "production_run_id": str(run.pk), "stage_run_id": str(stage.pk),
            "request_sha256": attempt.request_sha256, "definition_sha256": media["definition_sha256"],
            "resolved_inputs_sha256": media["resolved_inputs_sha256"],
        }
        if (any(manifest["generation"].get(key) != value for key, value in identity.items())
                or manifest["artifact"]["sha256"] != facts["sha256"]
                or manifest["artifact"].get("observed_media") != facts["observed_media"]):
            raise ProvenanceError("产物签名、冻结准备或真实媒体事实不一致")
        is_bible = stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
        if is_bible and media["output_kind"] != "image":
            raise ValidationError("Bible参考图阶段只能归档图片")
        metadata = {"schema_version": 1, "source": "comfyui", "tenant_owner_id": str(current.owner_id),
            **identity, "provider_request_id": attempt.provider_request_id, "unit_key": attempt.unit_key,
            "production_unit": media["production_unit"],
            "output_index": index, "node_id": str(item["node_id"]),
            "provenance_manifest_sha256": manifest["integrity"]["payload_sha256"],
            "observed_media": facts["observed_media"]}
        contract = json_contract(metadata, max_bytes=_stage_snapshot_max_bytes())
        comparison = build_comparison_parameters(attempt=attempt)
        group = f"stage:{stage.pk}:unit:{hashlib.sha256(attempt.unit_key.encode()).hexdigest()}"
        key = f"task:{current.pk}:{index}"
        artifact = GeneratedArtifact.objects.filter(manifest_key=key).first()
        if artifact is not None:
            stored = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
                size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
                max_bytes=_stage_snapshot_max_bytes())
            if (stored.sha256 != contract.sha256 or artifact.generation_attempt_id != attempt.pk
                    or artifact.generation_task_id != current.pk or artifact.stage_run_id != stage.pk
                    or artifact.production_run_id != run.pk or artifact.variant_group != group
                    or artifact.storage_backend != "managed_local" or artifact.kind != media["output_kind"]
                    or artifact.comparison_parameters != comparison
                    or any(getattr(artifact, name) != facts[name] for name in ("sha256", "size_bytes", "mime_type"))):
                raise ProvenanceError("已有产物与本次执行准备不一致，不能覆盖归档")
            verified_generated_artifact_media(artifact, max_bytes=limit,
                include_observed_media=True)
        else:
            if attempt.status == "succeeded":
                raise ProvenanceError("调用已成功但产物记录缺失，不能在读取时重新制造候选")
            archived = _archive_task_output(current, index, item, expected_kind=media["output_kind"],
                expected_sha256=facts["sha256"], max_bytes=limit, output_contract=media["output_contract"])
            if archived.pop("observed_media") != facts["observed_media"]:
                raise ProvenanceError("归档前后的媒体事实不一致")
            last = GeneratedArtifact.objects.filter(production_run=run, stage_run=stage, variant_group=group).aggregate(
                value=Max("variant_index"))["value"] or 0
            artifact = GeneratedArtifact(production_run=run, stage_run=stage, generation_attempt=attempt,
                generation_task=current, manifest_key=key, variant_group=group, variant_index=last + 1,
                kind=media["output_kind"], role="bible_reference_candidate" if is_bible else "primary_generation_output",
                status=GeneratedArtifact.Status.AVAILABLE, comparison_parameters=comparison, **archived)
            assign_json_contract(artifact, "manifest_metadata", metadata, max_bytes=_stage_snapshot_max_bytes())
            artifact.save()
        digest = hashlib.sha256(canonical_json_bytes([{"id": str(artifact.pk), "sha256": artifact.sha256}])).hexdigest()
        if attempt.status == "succeeded":
            if attempt.result_sha256 != digest:
                raise ProvenanceError("调用结果摘要与已归档产物不一致")
        else:
            attempt.status, attempt.result_sha256 = "succeeded", digest
            attempt.finished_at = attempt.heartbeat_at = timezone.now()
            attempt.error_code = attempt.error_message = ""
            attempt.save(update_fields=("status", "result_sha256", "finished_at", "heartbeat_at",
                "error_code", "error_message", "updated_at"))
        return [artifact]


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
def select_artifact(
    artifact: GeneratedArtifact, *, user,
    selection_mode=ArtifactSelectionDecision.SelectionMode.MANUAL,
) -> GeneratedArtifact:
    # The caller authorizes the policy; user remains the permission principal,
    # not a human selector when a frozen recipe policy makes the choice.
    if selection_mode not in ArtifactSelectionDecision.SelectionMode.values:
        raise ValidationError("未知的候选选择模式")
    if artifact.role == "structured_text_candidate":
        if selection_mode != ArtifactSelectionDecision.SelectionMode.MANUAL:
            raise ValidationError("通用文本自动选择必须通过本步骤的冻结策略")
        from studio.services.production_structured_text_selection import choose_structured_text_candidate
        decision = choose_structured_text_candidate(artifact.stage_run, actor=user, artifact_id=artifact.pk)
        return decision.artifact
    # All selection writers serialize on the same Run root, including callers
    # outside manual-actions. Never acquire the artifact before its Run.
    run_id = artifact.production_run_id
    if run_id is None:
        raise ValidationError("未归属运行的候选不能采用")
    ProductionRun.objects.select_for_update().get(pk=run_id)
    artifact = GeneratedArtifact.objects.select_for_update(of=("self",)).select_related(
        "production_run__project", "stage_run",
    ).filter(pk=artifact.pk, production_run_id=run_id).first()
    if artifact is None:
        raise ValidationError("候选所属运行已变化，请重新读取")
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
    _record_artifact_selection(
        artifact,
        selected_by=user if selection_mode == ArtifactSelectionDecision.SelectionMode.MANUAL else None,
        selection_mode=selection_mode,
    )
    return artifact


def _record_artifact_selection(artifact, *, selected_by, selection_mode):
    """Shared write under the caller's Run lock and candidate validation."""
    GeneratedArtifact.objects.filter(
        production_run=artifact.production_run,
        stage_run_id=artifact.stage_run_id,
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
        return ArtifactSelectionDecision.objects.create(
            stage_run_id=artifact.stage_run_id,
            variant_group=artifact.variant_group,
            revision=revision,
            artifact=artifact,
            selected_by=selected_by,
            selection_mode=selection_mode,
        )
