from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    AssemblySourceItem,
    Episode,
    GeneratedArtifact,
    SeriesProject,
    Shot,
    StageRun,
    WorkerLease,
)
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.assembly_contracts import validate_assembly_configuration, validate_assembly_output, validate_assembly_sources
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.media_processing_profiles import (
    MediaProcessingProfileError,
    ASSEMBLY_PROFILE_KEY,
    media_profile_sha256,
    verify_media_processing_profile,
    resolve_assembly_media_profile,
    validate_assembly_profile,
)
from studio.services.provenance import ProvenanceError, sha256_file, verified_assembly_manifest
from studio.services.security_boundaries import boundary_int
from studio.services.delivery_disclosure import (
    DeliveryDisclosureError,
    resolve_delivery_disclosure,
)
from studio.services.configuration import ConfigurationResolutionError, resolve_configuration


class AssemblyError(ValueError):
    def __init__(self, message: str, job: AssemblyJob | None = None):
        super().__init__(message)
        self.job = job


def _delivery_disclosure(values=None) -> tuple[bool, str]:
    try:
        disclosure = resolve_delivery_disclosure(values=values)
    except DeliveryDisclosureError as exc:
        raise AssemblyError(str(exc)) from exc
    return disclosure.visible_label_enabled, disclosure.base_label


def resolve_assembly_configuration(project, *, frozen_values=None):
    """One configuration resolution for preview, enqueue, and explicit retry."""
    try:
        if frozen_values is None:
            values = resolve_configuration(project=project, required_keys=(
                ASSEMBLY_PROFILE_KEY, "delivery.assembly_max_attempts",
                "delivery.ai_label_enabled", "delivery.ai_label_text",
            )).values
        else:
            values = frozen_values
        if not isinstance(values, dict) or any(key not in values for key in (
                ASSEMBLY_PROFILE_KEY, "delivery.assembly_max_attempts")):
            raise AssemblyError("冻结配置缺少装配编码或重试参数，不补当前配置")
        profile = verify_media_processing_profile(ASSEMBLY_PROFILE_KEY, values[ASSEMBLY_PROFILE_KEY])
        maximum = values["delivery.assembly_max_attempts"]
    except (MediaProcessingProfileError, ConfigurationResolutionError) as exc:
        raise AssemblyError(str(exc)) from exc
    enabled, label = _delivery_disclosure(values)
    value = {"schema_version": 1, "width": project.width, "height": project.height, "fps": project.fps,
        "media_profile": profile, "media_profile_key": ASSEMBLY_PROFILE_KEY, "media_profile_sha256": media_profile_sha256(profile),
        "max_attempts": maximum, "label_enabled": enabled, "label_text": label,
        "fontfile": str(settings.ASSEMBLY_AI_LABEL_FONTFILE or "")}
    try:
        return validate_assembly_configuration(value, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    except ValidationError as exc:
        raise AssemblyError("装配配置无效：" + "; ".join(exc.messages)) from exc


@dataclass(frozen=True)
class MediaSelection:
    shot: Shot
    artifact: GeneratedArtifact
    duration_ms: int
    episode_start_ms: int | None
    episode_end_ms: int | None


@dataclass(frozen=True)
class Clip:
    artifact: GeneratedArtifact
    path: Path
    has_audio: bool
    duration_seconds: float
    start_seconds: float = 0
    trim_to_shot: bool = False
    media_kind: str = Shot.MediaType.VIDEO


_ASSEMBLY_SIGNATURE_VERSION = 5


def _assembly_source_label(shot=None, artifact=None):
    parts = []
    if shot is not None:
        parts.append(f"第 {shot.episode.number} 集 / 分镜 {shot.number}（{shot.pk}）")
    if artifact is not None:
        parts.append(f"产物 {artifact.pk}")
    return " / ".join(parts)


def _assembly_artifact_evidence(artifact, shot=None, *, frozen_source=None):
    """Read an explicitly adopted single-shot source through its real origin.

    A project/episode input scope does not prove output shot boundaries. Such
    output needs its own reviewed mapping; never infer equal cuts or read an
    obsolete PromptArtifact to manufacture that mapping.
    """
    from django.core.exceptions import ValidationError
    from studio.services.execution_production_candidates import read_production_media_candidate
    from studio.services.generated_artifacts import _artifact_limit

    def failure(message):
        return AssemblyError(f"{_assembly_source_label(shot, artifact)}：{message}")

    if (artifact.status != "selected" or artifact.kind not in {"image", "video"}
            or artifact.production_run is None or artifact.stage_run is None
            or artifact.stage_run.stage_key != "media_generation"):
        raise failure("装配只接受原媒体步骤中明确采用的图片或视频产物")
    decision = artifact.stage_run.artifact_selection_decisions.filter(
        variant_group=artifact.variant_group).order_by("-revision").first()
    if decision is None or decision.artifact_id != artifact.pk:
        raise failure("媒体产物缺少一致的正式选片决定，不能取最新任务代替")
    if frozen_source is not None and (
            artifact.sha256 != frozen_source["sha256"] or artifact.manifest_metadata_sha256 != frozen_source["manifest_sha256"]
            or str(decision.pk) != frozen_source["decision_id"] or decision.revision != frozen_source["decision_revision"]):
        raise failure("冻结素材摘要或采用修订已变化")
    try:
        detail = read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
    except ValidationError as exc:
        raise failure("装配来源校验失败：" + "; ".join(exc.messages)) from exc
    unit = detail["production_unit"]
    if unit["scope"]["kind"] != "shot":
        raise failure("该产物未明确归属单镜头；跨镜头成片须先具备已审核的输出时间边界，不自动拆分")
    actual = Shot.objects.select_related("episode").filter(pk=unit["scope"]["id"],
        episode_id=unit["episode_id"], episode__project_id=unit["project_id"]).first()
    if (actual is None or actual.media_type != artifact.kind
            or (shot is not None and (actual.pk != shot.pk or actual.episode_id != shot.episode_id))
            or type(actual.duration_ms) is not int or actual.duration_ms <= 0):
        raise failure("已采用产物的镜头归属、类型或当前明确时长不一致")
    return detail, actual


def verify_assembly_sources(job: AssemblyJob) -> None:
    """Revalidate the immutable input manifest immediately before FFmpeg."""

    items = list(job.source_items.select_related(
        "shot__episode", "generated_artifact__generation_attempt",
    ).order_by("ordinal"))
    if not items:
        raise AssemblyError("组装任务缺少冻结的逐镜媒体来源", job=job)
    expected_shots = list(Shot.objects.filter(
        episode=job.episode,
    ).order_by("number", "id")) if job.episode_id else list(Shot.objects.filter(
        episode__project=job.project,
    ).order_by("episode__number", "episode_id", "number", "id"))
    if job.source_contract_version != _ASSEMBLY_SIGNATURE_VERSION:
        raise AssemblyError("装配来源合同不兼容当前逻辑，请在新制作轮次中明确采用媒体", job=job)
    if [item.shot_id for item in items] != [shot.id for shot in expected_shots]:
        raise AssemblyError("冻结媒体来源未完整覆盖组装合同中的分镜顺序", job=job)
    from itertools import groupby
    from studio.services.shot_timing import validate_sequential_episode_timing
    try:
        for _, episode_shots in groupby(expected_shots, key=lambda shot: shot.episode_id):
            validate_sequential_episode_timing(episode_shots)
    except ValueError as exc:
        raise AssemblyError(str(exc), job=job) from exc
    expected_episode_ids = {job.episode_id} if job.episode_id else set(
        job.project.episodes.values_list("id", flat=True)
    )
    seen_shots: set[str] = set()
    for item in items:
        def failure(message):
            return AssemblyError(f"{_assembly_source_label(item.shot, item.generated_artifact)}：{message}", job=job)

        if item.shot.episode.project_id != job.project_id or item.shot.episode_id not in expected_episode_ids:
            raise failure("冻结媒体来源的分镜归属已漂移")
        if str(item.shot_id) in seen_shots:
            raise failure("冻结媒体来源重复覆盖同一分镜")
        seen_shots.add(str(item.shot_id))
        if item.media_kind != item.shot.media_type:
            raise failure("冻结媒体来源类型与分镜类型不一致")
        artifact = item.generated_artifact
        if artifact is None:
            raise failure("冻结来源必须是明确采用的受管产物")
        _assembly_artifact_evidence(artifact, item.shot)
        root = Path(settings.MEDIA_ROOT).resolve()
        path = (root / item.object_key).resolve()
        if (
            artifact.kind != item.media_kind
            or artifact.storage_backend != item.storage_backend
            or artifact.object_key != item.object_key
            or artifact.sha256 != item.sha256
            or artifact.size_bytes != item.size_bytes
            or artifact.mime_type != item.mime_type
            or item.start_ms != 0
            or item.trim_to_shot
            or item.duration_ms != item.shot.duration_ms
        ):
            raise failure("冻结受管产物或镜头时长已漂移")
        if not path.is_relative_to(root):
            raise failure("冻结媒体来源路径越界")
        try:
            facts = verified_local_media_facts(
                path,
                expected_kind=item.media_kind,
                max_bytes=item.size_bytes,
            )
        except (OSError, ProvenanceError, ValueError) as exc:
            raise failure(f"冻结媒体来源无法重新校验: {exc}") from exc
        if (
            facts["sha256"] != item.sha256
            or facts["size_bytes"] != item.size_bytes
            or facts["mime_type"] != item.mime_type
        ):
            raise failure("冻结媒体来源字节事实已漂移")


def _ordered_frozen_sources_for_episode(
    episode: Episode,
    frozen_episode: dict,
) -> list[MediaSelection]:
    """Validate current facts, but consume timing from the original contract."""

    shots = list(episode.shots.order_by("number", "id"))
    from studio.services.shot_timing import validate_sequential_episode_timing
    try:
        validate_sequential_episode_timing(shots)
    except ValueError as exc:
        raise AssemblyError(str(exc)) from exc
    if not shots:
        raise AssemblyError(f"第 {episode.number} 集没有可组装的分镜")
    rows = frozen_episode["shots"]
    if (frozen_episode["episode_id"] != str(episode.pk) or frozen_episode["number"] != episode.number
            or [row["shot_id"] for row in rows] != [str(shot.pk) for shot in shots]):
        raise AssemblyError(f"第 {episode.number} 集冻结的逐镜已采用媒体不完整或身份/顺序已变化")
    selections: list[MediaSelection] = []
    for shot, row in zip(shots, rows, strict=True):
        if (row["number"], row["duration_ms"], row["media_kind"], row["episode_start_ms"], row["episode_end_ms"]) != (
                shot.number, shot.duration_ms, shot.media_type, shot.episode_start_ms, shot.episode_end_ms):
            raise AssemblyError(f"分镜 {row['number']} 的冻结顺序、时长或类型已变化，不能换值入队")
        reference = row["source"]
        artifact = GeneratedArtifact.objects.select_related(
            "production_run__project", "generation_attempt",
        ).filter(pk=reference["generated_artifact_id"]).first()
        if (
            artifact is None
            or artifact.kind != shot.media_type
            or artifact.production_run is None
            or artifact.production_run.project_id != episode.project_id
            or artifact.production_run.project.owner_id != episode.project.owner_id
        ):
            raise AssemblyError(f"分镜 {shot.number} 的冻结 GeneratedArtifact 不属于当前项目")
        _assembly_artifact_evidence(artifact, shot, frozen_source=reference)
        selections.append(MediaSelection(shot=shot, artifact=artifact, duration_ms=row["duration_ms"],
            episode_start_ms=row["episode_start_ms"], episode_end_ms=row["episode_end_ms"]))
    return selections


def _ffprobe_binary() -> str:
    configured = str(getattr(settings, "FFPROBE_BINARY", "") or "").strip()
    if configured:
        return configured
    ffmpeg = Path(str(settings.FFMPEG_BINARY))
    suffix = ffmpeg.suffix if ffmpeg.suffix.casefold() == ".exe" else ""
    return str(ffmpeg.with_name(f"ffprobe{suffix}"))


def _probe_media_streams(
    path: Path,
    *,
    media_profile: dict | None = None,
) -> frozenset[str]:
    """Return streams actually present in a persisted input media file.

    Prompt intent is not evidence that ComfyUI emitted an audio stream.  A
    failed probe is therefore a hard assembly error instead of silently
    constructing an FFmpeg graph that references a missing ``a:0`` stream.
    """

    try:
        profile = validate_assembly_profile(media_profile) if media_profile is not None else (
            resolve_assembly_media_profile().value
        )
        result = subprocess.run(
            [
                _ffprobe_binary(), "-v", "error",
                "-show_entries", "stream=codec_type", "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=profile["probe_timeout_seconds"],
        )
    except (OSError, subprocess.SubprocessError, MediaProcessingProfileError) as exc:
        raise AssemblyError(f"无法探测输入媒体 {path.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffprobe exited with an error").strip()
        raise AssemblyError(f"无法探测输入媒体 {path.name}: {detail[-1000:]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AssemblyError(f"输入媒体 {path.name} 的 ffprobe 结果不是有效 JSON") from exc
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    if not isinstance(streams, list):
        raise AssemblyError(f"输入媒体 {path.name} 的 ffprobe 结果缺少 streams")
    return frozenset(
        str(item.get("codec_type", "")).casefold()
        for item in streams
        if isinstance(item, dict) and item.get("codec_type")
    )


def _resolve_output(
    selection: MediaSelection,
    stream_cache: dict[Path, frozenset[str]] | None = None,
    *,
    media_profile: dict | None = None,
) -> Clip:
    artifact = selection.artifact
    evidence, _shot = _assembly_artifact_evidence(artifact, selection.shot)
    candidate = evidence["path"]
    duration_seconds = selection.duration_ms / 1000
    if artifact.kind == GeneratedArtifact.Kind.IMAGE:
        return Clip(artifact, candidate, False, duration_seconds, media_kind=Shot.MediaType.IMAGE)
    streams = stream_cache.get(candidate) if stream_cache is not None else None
    if streams is None:
        try:
            streams = _probe_media_streams(candidate, media_profile=media_profile)
        except AssemblyError as exc:
            raise AssemblyError(f"{_assembly_source_label(selection.shot, artifact)}：{exc}") from exc
        if stream_cache is not None:
            stream_cache[candidate] = streams
    if "video" not in streams:
        raise AssemblyError(f"{_assembly_source_label(selection.shot, artifact)}：没有视频流")
    return Clip(artifact, candidate, "audio" in streams, duration_seconds, media_kind=Shot.MediaType.VIDEO)


def _safe_output_name(raw: str, default: str) -> str:
    value = raw.strip() or default
    if Path(value).name != value or ".." in value or not re.fullmatch(r"[\w.-]+\.mp4", value, flags=re.UNICODE):
        raise AssemblyError("output_name 必须是安全的 .mp4 文件名，不能包含路径")
    return value


def _manifest_line(path: Path) -> str:
    escaped = path.as_posix().replace("'", "'\\''")
    return f"file '{escaped}'"


def _manifest_shot_line(shot: Shot) -> str:
    # FFconcat ignores comments. Persisting the ordered shot identity beside
    # each ordered input lets the signed output provenance distinguish two
    # clips that intentionally reuse the same multi-shot task/output.
    return f"# autoprompt-shot-id {shot.id}"


def _drawtext_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def _resolve_ai_label_fontfile(configured=None) -> str:
    configured = str(settings.ASSEMBLY_AI_LABEL_FONTFILE or "").strip() if configured is None else configured.strip()
    if configured:
        candidate = Path(os.path.expandvars(configured)).expanduser()
        if not candidate.is_file():
            raise AssemblyError(f"AI 标识字体文件不存在: {candidate}")
        return candidate.resolve().as_posix()
    if sys.platform == "win32":
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for filename in ("msyh.ttc", "arial.ttf", "segoeui.ttf", "simhei.ttf"):
            candidate = fonts_dir / filename
            if candidate.is_file():
                return candidate.resolve().as_posix()
        raise AssemblyError(
            "Windows 未找到可用于 AI 标识的字体；请配置 ASSEMBLY_AI_LABEL_FONTFILE"
        )
    return ""


def _ffmpeg_command(
    clips: list[Clip], output: Path, width: int, height: int, fps: int,
    *,
    media_profile: dict | None = None,
    configuration: dict | None = None,
) -> list[str]:
    try:
        profile = validate_assembly_profile(media_profile) if media_profile is not None else (
            resolve_assembly_media_profile().value
        )
    except MediaProcessingProfileError as exc:
        raise AssemblyError(str(exc)) from exc
    try:
        validate_assembly_output({"width": width, "height": height, "fps": fps})
    except ValidationError as exc:
        raise AssemblyError("装配输出参数无效：" + "; ".join(exc.messages)) from exc
    command = [settings.FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", "-y"]
    for clip in clips:
        if clip.media_kind == Shot.MediaType.IMAGE:
            command.extend(("-loop", "1", "-framerate", str(fps)))
        command.extend(("-i", str(clip.path)))
    filters = []
    concat_inputs = []
    for index, clip in enumerate(clips):
        video_trim = (
            f"trim=start={clip.start_seconds:.3f}:duration={clip.duration_seconds:.3f},setpts=PTS-STARTPTS,"
            if clip.trim_to_shot else "setpts=PTS-STARTPTS,"
        )
        filters.append(
            f"[{index}:v:0]{video_trim}tpad=stop_mode=clone:stop_duration={clip.duration_seconds:.3f},"
            f"trim=duration={clip.duration_seconds:.3f},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},"
            f"format={profile['pixel_format']},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")
        if clip.has_audio:
            audio_trim = (
                f"atrim=start={clip.start_seconds:.3f}:duration={clip.duration_seconds:.3f},"
                if clip.trim_to_shot else ""
            )
            filters.append(
                f"[{index}:a:0]{audio_trim}aresample={profile['audio_sample_rate_hz']},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"apad=pad_dur={clip.duration_seconds:.3f},atrim=duration={clip.duration_seconds:.3f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=r={profile['audio_sample_rate_hz']}:cl=stereo,"
                f"atrim=duration={clip.duration_seconds:.3f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        concat_inputs.append(f"[a{index}]")
    label_enabled, label_text = _delivery_disclosure() if configuration is None else (configuration["label_enabled"], configuration["label_text"])
    label_text = label_text.strip()
    concat_video_label = "vconcat" if label_enabled else "vout"
    filters.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[{concat_video_label}][aout]")
    if label_enabled:
        fontfile = _resolve_ai_label_fontfile(None if configuration is None else configuration["fontfile"])
        overlay = profile["overlay"]
        drawtext = (
            f"drawtext=text='{_drawtext_value(label_text)}':"
            f"fontcolor={overlay['font_color']}:"
            f"fontsize=h/{overlay['font_size_divisor']}:box=1:"
            f"boxcolor={overlay['box_color']}@{overlay['box_opacity']}:"
            f"boxborderw={overlay['box_border_width_px']}:"
            f"x=w-tw-{overlay['margin_x_px']}:y=h-th-{overlay['margin_y_px']}"
        )
        if fontfile:
            drawtext = (
                f"drawtext=fontfile='{_drawtext_value(fontfile)}':"
                + drawtext.removeprefix("drawtext=")
            )
        filters.append(f"[vconcat]{drawtext}[vout]")
    command.extend((
        "-filter_complex", ";".join(filters), "-map", "[vout]",
        "-map", "[aout]", "-c:a", profile["audio_codec"],
        "-b:a", f"{profile['audio_bitrate_kbps']}k",
    ))
    command.extend((
        "-c:v", profile["video_codec"],
        "-preset", profile["video_preset"],
        "-crf", str(profile["video_crf"]),
        "-pix_fmt", profile["pixel_format"],
    ))
    if label_text:
        command.extend((
            "-metadata", f"comment={label_text}",
            "-metadata", f"description={label_text}",
        ))
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def _mark_failed(job: AssemblyJob, message: str) -> AssemblyError:
    job.status = AssemblyJob.Status.FAILED
    job.error_message = message
    job.finished_at = timezone.now()
    job.save(update_fields=("status", "error_message", "finished_at", "updated_at"))
    return AssemblyError(message, job=job)


def _normalized_requested_output_name(output_name: str) -> str:
    value = output_name.strip()
    return _safe_output_name(value, "assembly-output.mp4") if value else ""


def _assembly_source_signature(
    *, owner, project: SeriesProject, episode: Episode | None,
    selections: list[MediaSelection], clips: list[Clip], output_name: str,
    configuration: dict,
) -> str:
    sources = []
    for selection, clip in zip(selections, clips, strict=True):
        root = Path(settings.MEDIA_ROOT).resolve()
        sources.append({
            "source_kind": "artifact",
            "media_kind": clip.media_kind,
            "source_id": str(clip.artifact.id),
            "shot_id": str(selection.shot.id),
            "episode_start_ms": selection.episode_start_ms,
            "episode_end_ms": selection.episode_end_ms,
            "source": clip.path.relative_to(root).as_posix(),
            "start_ms": round(clip.start_seconds * 1000),
            "duration_ms": round(clip.duration_seconds * 1000),
            "trim_to_shot": clip.trim_to_shot,
            "has_audio_stream": clip.has_audio,
            "sha256": sha256_file(clip.path),
        })
    payload = {
        "version": _ASSEMBLY_SIGNATURE_VERSION,
        "owner_id": str(owner.id),
        "project_id": str(project.id),
        "episode_id": str(episode.id) if episode else None,
        "scope": AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES,
        "sources": sources,
        "output": {
            "requested_name": output_name,
            "configuration": configuration,
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reusable_output_exists(job: AssemblyJob, *, owner, project: SeriesProject) -> bool:
    if not job.output_path:
        return False
    candidate = Path(job.output_path).resolve()
    allowed_root = (
        Path(settings.MEDIA_ROOT).resolve() / "assemblies" / str(owner.id) / str(project.id)
    ).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        return False
    try:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return False
        verified_assembly_manifest(job)
        return True
    except (OSError, ProvenanceError):
        return False


def _find_reusable_assembly(
    *, owner, project: SeriesProject, episode: Episode | None, source_signature: str,
    consumer_stage=None, consumer_input_sha256="",
) -> AssemblyJob | None:
    scope = AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES
    queryset = AssemblyJob.objects.filter(
        owner=owner,
        project=project,
        episode=episode,
        scope=scope,
        source_contract_version=_ASSEMBLY_SIGNATURE_VERSION,
        consumer_stage=consumer_stage,
        consumer_input_sha256=consumer_input_sha256,
        dry_run=False,
        status__in=(
            AssemblyJob.Status.QUEUED,
            AssemblyJob.Status.RUNNING,
            AssemblyJob.Status.SUCCEEDED,
        ),
    ).order_by("-created_at")
    for job in queryset:
        if job.source_signature != source_signature:
            continue
        if job.status != AssemblyJob.Status.SUCCEEDED:
            return job
        if _reusable_output_exists(job, owner=owner, project=project):
            return job
    return None


def _mark_scope_complete(project: SeriesProject, episode: Episode | None) -> None:
    if episode:
        if episode.status != Episode.Status.COMPLETED:
            episode.status = Episode.Status.COMPLETED
            episode.save(update_fields=("status", "updated_at"))
        return
    project.episodes.exclude(status=Episode.Status.COMPLETED).update(
        status=Episode.Status.COMPLETED, updated_at=timezone.now(),
    )
    if project.status != SeriesProject.Status.COMPLETED or project.completed_at is None:
        project.status = SeriesProject.Status.COMPLETED
        project.completed_at = timezone.now()
        project.save(update_fields=("status", "completed_at", "updated_at"))


def create_assembly(
    *,
    owner,
    project: SeriesProject,
    episode: Episode | None,
    dry_run: bool,
    output_name: str = "",
    frozen_sources: dict,
    frozen_configuration: dict | None = None,
    file_guard=None,
    consumer_stage=None,
) -> AssemblyJob:
    if project.owner_id != owner.pk or (episode is not None and episode.project_id != project.pk):
        raise AssemblyError("装配所有者、项目与剧集不一致")
    try:
        configuration = resolve_assembly_configuration(project) if frozen_configuration is None else validate_assembly_configuration(
            frozen_configuration, max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
        )
        sources = validate_assembly_sources(frozen_sources, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    except ValidationError as exc:
        raise AssemblyError("冻结装配输入无效：" + "; ".join(exc.messages)) from exc
    media_profile = configuration["media_profile"]
    scope = AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES
    episodes = [episode] if episode else list(project.episodes.order_by("number"))
    if not episodes:
        raise AssemblyError("项目没有可组装的剧集")
    if [row["episode_id"] for row in sources["episodes"]] != [str(item.pk) for item in episodes]:
        raise AssemblyError("装配必须完整指定本次交付范围的逐集逐镜已采用媒体")
    selections = []
    for item, source in zip(episodes, sources["episodes"], strict=True):
        selections.extend(_ordered_frozen_sources_for_episode(item, source))
    stream_cache: dict[Path, frozenset[str]] = {}
    clips = [
        _resolve_output(
            selection,
            stream_cache,
            media_profile=media_profile,
        )
        for selection in selections
    ]
    normalized_output_name = _normalized_requested_output_name(output_name)
    source_signature = _assembly_source_signature(
        owner=owner,
        project=project,
        episode=episode,
        selections=selections,
        clips=clips,
        output_name=normalized_output_name,
        configuration=configuration,
    )
    # Serialize idempotency lookup + durable enqueue.  The expensive media probe
    # happens before this short transaction, while the mutex prevents two API
    # requests with the same source snapshot from creating duplicate FFmpeg work.
    preparation_failure: AssemblyError | None = None
    preparation_cause: BaseException | None = None
    with transaction.atomic():
        # Formal callers hold the Run lock. Capture the persisted input version,
        # never a caller-supplied digest or a source artifact's generation stage.
        consumer_input_sha256 = ""
        if consumer_stage is not None:
            consumer_stage = StageRun.objects.select_for_update().select_related("production_run").get(pk=consumer_stage.pk)
            if consumer_stage.stage_key != "assembly" or consumer_stage.production_run.project_id != project.pk:
                raise AssemblyError("装配消费步骤不属于当前项目的正式装配阶段")
            try:
                contract = verify_json_contract(consumer_stage.input_snapshot,
                    schema_version=consumer_stage.input_snapshot_schema_version,
                    size_bytes=consumer_stage.input_snapshot_size_bytes, sha256=consumer_stage.input_snapshot_sha256,
                    max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
                if not contract.value:
                    raise ValidationError("装配输入未冻结")
            except ValidationError as exc:
                raise AssemblyError("装配消费输入无效：" + "; ".join(exc.messages)) from exc
            consumer_input_sha256 = contract.sha256
        try:
            project = SeriesProject.objects.select_for_update().get(pk=project.pk)
        except SeriesProject.DoesNotExist as exc:
            raise AssemblyError("连续剧项目不存在") from exc
        WorkerLease.objects.get_or_create(key="assembly-enqueue")
        WorkerLease.objects.select_for_update().get(key="assembly-enqueue")
        if not dry_run:
            reusable = _find_reusable_assembly(
                owner=owner,
                project=project,
                episode=episode,
                source_signature=source_signature,
                consumer_stage=consumer_stage,
                consumer_input_sha256=consumer_input_sha256,
            )
            if reusable is not None:
                if reusable.status == AssemblyJob.Status.SUCCEEDED:
                    _mark_scope_complete(project, episode)
                return reusable

        max_attempts = configuration["max_attempts"]
        job = AssemblyJob.objects.create(
            owner=owner, project=project, episode=episode, scope=scope,
            status=AssemblyJob.Status.PLANNED, dry_run=dry_run,
            source_signature=source_signature,
            source_contract_version=_ASSEMBLY_SIGNATURE_VERSION,
            consumer_stage=consumer_stage,
            consumer_input_sha256=consumer_input_sha256,
            max_attempts=max_attempts,
            # Every assembled MP4 has an AAC track.  Source clips without an audio
            # stream receive duration-matched silence in ``_ffmpeg_command``.
            has_audio=True,
        )
        source_items = []
        for index, (selection, clip) in enumerate(zip(selections, clips, strict=True), start=1):
            facts = verified_local_media_facts(
                clip.path,
                expected_kind=clip.media_kind,
                max_bytes=clip.path.stat().st_size,
            )
            source_items.append(AssemblySourceItem(
                assembly_job=job,
                ordinal=index,
                shot=selection.shot,
                generated_artifact=selection.artifact,
                storage_backend=selection.artifact.storage_backend,
                object_key=selection.artifact.object_key,
                mime_type=facts["mime_type"],
                size_bytes=facts["size_bytes"],
                sha256=facts["sha256"],
                media_kind=clip.media_kind,
                start_ms=round(clip.start_seconds * 1000),
                duration_ms=max(1, round(clip.duration_seconds * 1000)),
                trim_to_shot=clip.trim_to_shot,
                has_audio_stream=clip.has_audio,
            ))
        AssemblySourceItem.objects.bulk_create(source_items)
        verify_assembly_sources(job)
        try:
            output_dir = Path(settings.MEDIA_ROOT).resolve() / "assemblies" / str(owner.id) / str(project.id)
            output_dir.mkdir(parents=True, exist_ok=True)
            default_name = f"episode-{episode.number:04d}-{job.id}.mp4" if episode else f"series-{job.id}.mp4"
            output = output_dir / (normalized_output_name or default_name)
            active_output = AssemblyJob.objects.filter(
                output_path=str(output),
                dry_run=False,
                status__in=(AssemblyJob.Status.QUEUED, AssemblyJob.Status.RUNNING),
            ).exclude(pk=job.pk).exists()
            if active_output:
                raise AssemblyError("目标输出正在被另一个组装任务使用；请换一个 output_name")
            if output.exists():
                raise AssemblyError("目标输出已存在；为避免覆盖，请换一个 output_name")
            command = _ffmpeg_command(
                clips,
                output,
                configuration["width"],
                configuration["height"],
                configuration["fps"],
                media_profile=media_profile,
                configuration=configuration,
            )
            manifest = output_dir / f"{job.id}.source-{source_signature}.ffconcat"
            manifest_lines = ["ffconcat version 1.0"]
            for selection, clip in zip(selections, clips, strict=True):
                manifest_lines.extend((
                    _manifest_shot_line(selection.shot),
                    _manifest_line(clip.path),
                ))
            if file_guard is not None:
                file_guard.track_path(manifest)
            manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        except (OSError, AssemblyError) as exc:
            # Do not raise inside this atomic block: doing so would roll back the
            # FAILED audit row that the API includes in its 422 response.
            preparation_cause = exc
            preparation_failure = _mark_failed(job, str(exc))
        if preparation_failure is None:
            job.output_path = str(output)
            job.concat_manifest_path = str(manifest)
            job.ffmpeg_command = command
            if not dry_run:
                job.status = AssemblyJob.Status.QUEUED
            job.save(update_fields=(
                "output_path", "concat_manifest_path", "ffmpeg_command", "status", "updated_at",
            ))
    if preparation_failure is not None:
        raise preparation_failure from preparation_cause
    return job
