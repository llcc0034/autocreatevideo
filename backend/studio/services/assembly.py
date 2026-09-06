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
from django.db import transaction
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    AssemblySourceItem,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationTask,
    PromptArtifact,
    SeriesProject,
    Shot,
    WorkerLease,
)
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import verify_json_contract
from studio.services.media_processing_profiles import (
    MediaProcessingProfileError,
    ResolvedMediaProcessingProfile,
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


def _delivery_disclosure() -> tuple[bool, str]:
    try:
        disclosure = resolve_delivery_disclosure()
    except DeliveryDisclosureError as exc:
        raise AssemblyError(str(exc)) from exc
    return disclosure.visible_label_enabled, disclosure.base_label


@dataclass(frozen=True)
class TaskSelection:
    shot: Shot
    task: GenerationTask | None = None
    artifact: GeneratedArtifact | None = None


@dataclass(frozen=True)
class Clip:
    task: GenerationTask | None
    path: Path
    has_audio: bool
    duration_seconds: float
    start_seconds: float = 0
    trim_to_shot: bool = False
    artifact: GeneratedArtifact | None = None
    media_kind: str = Shot.MediaType.VIDEO


_ASSEMBLY_SIGNATURE_VERSION = 4
_ASSEMBLY_SIGNATURE_RE = re.compile(r"\.source-([0-9a-f]{64})\.ffconcat$")


def _eligible_tasks(episode: Episode):
    """Read one ordered task snapshot to avoid racing completion between queries."""

    return list(
        GenerationTask.objects.filter(
            episode=episode,
            kind__in=(GenerationTask.Kind.IMAGE, GenerationTask.Kind.VIDEO),
            status__in=(
                GenerationTask.Status.QUEUED,
                GenerationTask.Status.RUNNING,
                GenerationTask.Status.CANCEL_REQUESTED,
                GenerationTask.Status.SUCCEEDED,
            ),
        ).select_related(
            "generated_workflow__prompt_artifact__shot", "generated_workflow__template"
        ).order_by("-created_at")
    )


def _task_shot_ids(task: GenerationTask) -> tuple[str, ...]:
    prompt = task.generated_workflow.prompt_artifact
    if not prompt.is_multi_shot and prompt.shot_id:
        return (str(prompt.shot_id),)
    values = (prompt.request_options or {}).get("shot_ids", [])
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values)


def _ordered_tasks_for_episode(episode: Episode) -> list[TaskSelection]:
    """Select the newest successful source independently for every shot.

    A multi-shot task is intentionally represented once per covered shot.  The
    resolver below cuts the persisted multi-shot video at its prompt-time shot
    boundaries, allowing a newer single-shot result to replace only its own
    segment without silently retaining or duplicating the old segment.
    """

    shots = list(episode.shots.order_by("number", "id"))
    if not shots:
        raise AssemblyError(f"第 {episode.number} 集没有可组装的分镜")
    tasks = _eligible_tasks(episode)
    coverage = {task.id: set(_task_shot_ids(task)) for task in tasks}
    selected: list[TaskSelection] = []
    for shot in shots:
        task = next((
            item for item in tasks
            if item.kind == shot.media_type and str(shot.id) in coverage[item.id]
        ), None)
        if task and task.status != GenerationTask.Status.SUCCEEDED:
            raise AssemblyError(
                f"第 {episode.number} 集分镜 {shot.number} 的最新{shot.get_media_type_display()}仍生成中，完成后再组装"
            )
        if not task:
            raise AssemblyError(
                f"第 {episode.number} 集分镜 {shot.number} 缺少成功的{shot.get_media_type_display()}任务"
            )
        selected.append(TaskSelection(shot=shot, task=task))
    return selected


def _artifact_prompt(artifact: GeneratedArtifact) -> PromptArtifact:
    attempt = artifact.generation_attempt
    if attempt is None or attempt.status != GenerationAttempt.Status.SUCCEEDED:
        raise AssemblyError("受管产物没有成功的 GenerationAttempt")
    if attempt.request_sha256 != attempt.request_snapshot_sha256:
        raise AssemblyError("受管产物的请求 identity 已漂移")
    verify_json_contract(
        attempt.request_snapshot,
        schema_version=attempt.request_snapshot_schema_version,
        size_bytes=attempt.request_snapshot_size_bytes,
        sha256=attempt.request_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    )
    prompt_id = (
        attempt.request_snapshot.get("prompt_artifact_id")
        if isinstance(attempt.request_snapshot, dict) else None
    )
    prompt = PromptArtifact.objects.select_related("shot__episode", "project").filter(
        pk=prompt_id,
    ).first()
    if prompt is None or prompt.shot_id is None:
        raise AssemblyError("受管产物缺少可验证的逐镜 PromptArtifact")
    return prompt


def _managed_artifact_path(artifact: GeneratedArtifact) -> tuple[Path, dict]:
    if artifact.kind not in {GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO}:
        raise AssemblyError("组装只接受图片或视频媒体产物")
    if artifact.storage_backend not in {"managed_local", "local_media"}:
        raise AssemblyError("组装只接受已经归档到受管存储的媒体产物")
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / artifact.object_key).resolve()
    if not path.is_relative_to(root):
        raise AssemblyError("受管产物路径越过 MEDIA_ROOT")
    try:
        facts = verified_local_media_facts(
            path,
            expected_kind=artifact.kind,
            max_bytes=int(artifact.size_bytes or 0),
        )
    except (OSError, ProvenanceError, ValueError) as exc:
        raise AssemblyError(f"受管{artifact.get_kind_display()}产物校验失败: {exc}") from exc
    if (
        artifact.status != GeneratedArtifact.Status.SELECTED
        or facts["sha256"] != artifact.sha256
        or facts["size_bytes"] != artifact.size_bytes
        or facts["mime_type"] != artifact.mime_type
    ):
        raise AssemblyError("受管媒体产物 manifest 与真实字节不一致")
    return path, facts


def verify_assembly_sources(job: AssemblyJob) -> None:
    """Revalidate the immutable input manifest immediately before FFmpeg."""

    items = list(job.source_items.select_related(
        "shot__episode", "generation_task", "generated_artifact__generation_attempt",
    ).order_by("ordinal"))
    if not items:
        raise AssemblyError("组装任务缺少冻结的逐镜媒体来源", job=job)
    expected_shots = list(Shot.objects.filter(
        episode=job.episode,
    ).order_by("number", "id")) if job.episode_id else list(Shot.objects.filter(
        episode__project=job.project,
    ).order_by("episode__number", "episode_id", "number", "id"))
    if job.source_contract_version < 3:
        expected_shots = [shot for shot in expected_shots if shot.media_type == Shot.MediaType.VIDEO]
    if [item.shot_id for item in items] != [shot.id for shot in expected_shots]:
        raise AssemblyError("冻结媒体来源未完整覆盖组装合同中的分镜顺序", job=job)
    expected_episode_ids = {job.episode_id} if job.episode_id else set(
        job.project.episodes.values_list("id", flat=True)
    )
    seen_shots: set[str] = set()
    for item in items:
        if item.shot.episode.project_id != job.project_id or item.shot.episode_id not in expected_episode_ids:
            raise AssemblyError("冻结媒体来源的分镜归属已漂移", job=job)
        if str(item.shot_id) in seen_shots:
            raise AssemblyError("冻结媒体来源重复覆盖同一分镜", job=job)
        seen_shots.add(str(item.shot_id))
        if item.media_kind != item.shot.media_type:
            raise AssemblyError("冻结媒体来源类型与分镜类型不一致", job=job)
        if item.generated_artifact_id:
            artifact = item.generated_artifact
            if artifact is None or artifact.generation_attempt is None:
                raise AssemblyError("冻结受管产物来源已经失效", job=job)
            root = Path(settings.MEDIA_ROOT).resolve()
            path = (root / item.object_key).resolve()
            if (
                artifact.generation_attempt.status != GenerationAttempt.Status.SUCCEEDED
                or artifact.kind != item.media_kind
                or artifact.object_key != item.object_key
                or artifact.sha256 != item.sha256
                or artifact.size_bytes != item.size_bytes
                or artifact.mime_type != item.mime_type
            ):
                raise AssemblyError("冻结受管产物事实已漂移", job=job)
        else:
            task = item.generation_task
            if (
                task is None
                or task.status != GenerationTask.Status.SUCCEEDED
                or task.kind != item.media_kind
            ):
                raise AssemblyError("冻结 GenerationTask 来源已经失效", job=job)
            root = Path(settings.COMFYUI_OUTPUT_DIR).resolve()
            path = (root / item.object_key).resolve()
        if not path.is_relative_to(root):
            raise AssemblyError("冻结媒体来源路径越界", job=job)
        try:
            facts = verified_local_media_facts(
                path,
                expected_kind=item.media_kind,
                max_bytes=item.size_bytes,
            )
        except (OSError, ProvenanceError, ValueError) as exc:
            raise AssemblyError(f"冻结媒体来源无法重新校验: {exc}", job=job) from exc
        if (
            facts["sha256"] != item.sha256
            or facts["size_bytes"] != item.size_bytes
            or facts["mime_type"] != item.mime_type
        ):
            raise AssemblyError("冻结媒体来源字节事实已漂移", job=job)


def _ordered_frozen_sources_for_episode(
    episode: Episode,
    source_refs_by_shot: dict[str, dict[str, str]],
) -> list[TaskSelection]:
    """Resolve a complete per-shot source map through one validation path."""

    shots = list(episode.shots.order_by("number", "id"))
    if not shots:
        raise AssemblyError(f"第 {episode.number} 集没有可组装的分镜")
    expected = {str(shot.id) for shot in shots}
    if set(source_refs_by_shot) != expected:
        raise AssemblyError(f"第 {episode.number} 集冻结的逐镜媒体来源不完整")
    selections: list[TaskSelection] = []
    for shot in shots:
        reference = source_refs_by_shot[str(shot.id)]
        if not isinstance(reference, dict) or set(reference) not in (
            {"generation_task_id"}, {"generated_artifact_id"},
        ):
            raise AssemblyError("冻结媒体来源必须且只能引用 task 或 artifact")
        if "generation_task_id" in reference:
            task = GenerationTask.objects.select_related(
                "generated_workflow__prompt_artifact",
            ).filter(
                pk=reference["generation_task_id"], owner=episode.project.owner,
                project=episode.project, episode=episode, kind=shot.media_type,
                status=GenerationTask.Status.SUCCEEDED,
            ).first()
            if task is None or str(shot.id) not in set(_task_shot_ids(task)):
                raise AssemblyError(f"分镜 {shot.number} 的冻结 GenerationTask 无效")
            selections.append(TaskSelection(shot=shot, task=task))
            continue
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
        prompt = _artifact_prompt(artifact)
        if prompt.project_id != episode.project_id or prompt.episode_id != episode.id or prompt.shot_id != shot.id:
            raise AssemblyError(f"分镜 {shot.number} 的冻结 GeneratedArtifact 归属不一致")
        _managed_artifact_path(artifact)
        selections.append(TaskSelection(shot=shot, artifact=artifact))
    return selections


def _multi_shot_window(selection: TaskSelection) -> tuple[float, float]:
    prompt = selection.task.generated_workflow.prompt_artifact
    shot_ids = _task_shot_ids(selection.task)
    durations = (prompt.structured_payload or {}).get("shot_durations_ms", [])
    if (
        not shot_ids
        or not isinstance(durations, list)
        or len(durations) != len(shot_ids)
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in durations)
    ):
        raise AssemblyError(
            f"多分镜任务 {selection.task.id} 缺少有效的逐镜时长，无法安全替换单个分镜"
        )
    try:
        index = shot_ids.index(str(selection.shot.id))
    except ValueError as exc:
        raise AssemblyError(
            f"多分镜任务 {selection.task.id} 未记录分镜 {selection.shot.number} 的时间边界"
        ) from exc
    return sum(float(value) for value in durations[:index]) / 1000, float(durations[index]) / 1000


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
    selection: TaskSelection,
    stream_cache: dict[Path, frozenset[str]] | None = None,
    *,
    media_profile: dict | None = None,
) -> Clip:
    task = selection.task
    if selection.artifact is not None:
        artifact = selection.artifact
        candidate, _facts = _managed_artifact_path(artifact)
        prompt = _artifact_prompt(artifact)
        duration_seconds = selection.shot.duration_ms / 1000
        if artifact.kind == GeneratedArtifact.Kind.IMAGE:
            return Clip(
                None, candidate, False, duration_seconds,
                artifact=artifact, media_kind=Shot.MediaType.IMAGE,
            )
        streams = stream_cache.get(candidate) if stream_cache is not None else None
        if streams is None:
            streams = _probe_media_streams(candidate, media_profile=media_profile)
            if stream_cache is not None:
                stream_cache[candidate] = streams
        if "video" not in streams:
            raise AssemblyError(f"受管产物 {artifact.id} 没有视频流")
        return Clip(
            None, candidate, "audio" in streams,
            duration_seconds, artifact=artifact, media_kind=Shot.MediaType.VIDEO,
        )
    if task is None:
        raise AssemblyError("组装来源没有 task 或 artifact")
    if not settings.COMFYUI_OUTPUT_DIR:
        raise AssemblyError("COMFYUI_OUTPUT_DIR 未配置")
    output_root = Path(settings.COMFYUI_OUTPUT_DIR).resolve()
    is_image = selection.shot.media_type == Shot.MediaType.IMAGE
    output = next((item for item in task.outputs if (
        item.get("kind") in ({"images"} if is_image else {"videos", "gifs"})
        or str(item.get("filename", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp") if is_image
            else (".mp4", ".mov", ".mkv", ".webm")
        )
    )), None)
    if not output or not output.get("filename"):
        raise AssemblyError(f"任务 {task.id} 没有可组装的{selection.shot.get_media_type_display()}输出")
    candidate = (output_root / str(output.get("subfolder", "")) / str(output["filename"])).resolve()
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise AssemblyError(f"任务 {task.id} 输出路径越过 ComfyUI 输出目录") from exc
    if not candidate.is_file():
        raise AssemblyError(f"任务 {task.id} 输出文件不存在: {candidate}")
    if is_image:
        return Clip(
            task, candidate, False,
            selection.shot.duration_ms / 1000,
            media_kind=Shot.MediaType.IMAGE,
        )
    streams = stream_cache.get(candidate) if stream_cache is not None else None
    if streams is None:
        streams = _probe_media_streams(candidate, media_profile=media_profile)
        if stream_cache is not None:
            stream_cache[candidate] = streams
    if "video" not in streams:
        raise AssemblyError(f"任务 {task.id} 输出文件没有可组装的视频流: {candidate.name}")
    has_audio = "audio" in streams
    prompt = task.generated_workflow.prompt_artifact
    if prompt.is_multi_shot:
        start_seconds, _source_window_seconds = _multi_shot_window(selection)
        return Clip(
            task, candidate, has_audio,
            selection.shot.duration_ms / 1000,
            start_seconds, True, media_kind=Shot.MediaType.VIDEO,
        )
    return Clip(
        task, candidate, has_audio,
        selection.shot.duration_ms / 1000,
        media_kind=Shot.MediaType.VIDEO,
    )


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


def _resolve_ai_label_fontfile() -> str:
    configured = str(settings.ASSEMBLY_AI_LABEL_FONTFILE or "").strip()
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
) -> list[str]:
    try:
        profile = validate_assembly_profile(media_profile) if media_profile is not None else (
            resolve_assembly_media_profile().value
        )
    except MediaProcessingProfileError as exc:
        raise AssemblyError(str(exc)) from exc
    maximum_fps = boundary_int("security.media.max_fps")
    if (
        isinstance(fps, bool)
        or not isinstance(fps, int)
        or not 1 <= fps <= maximum_fps
    ):
        raise AssemblyError(f"交付帧率必须是 1–{maximum_fps} 的整数")
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
    label_enabled, label_text = _delivery_disclosure()
    label_text = label_text.strip()
    concat_video_label = "vconcat" if label_enabled else "vout"
    filters.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[{concat_video_label}][aout]")
    if label_enabled:
        fontfile = _resolve_ai_label_fontfile()
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
    selections: list[TaskSelection], clips: list[Clip], output_name: str,
    media_profile: ResolvedMediaProcessingProfile,
) -> str:
    sources = []
    for selection, clip in zip(selections, clips, strict=True):
        root = (
            Path(settings.MEDIA_ROOT).resolve()
            if clip.artifact is not None else Path(settings.COMFYUI_OUTPUT_DIR).resolve()
        )
        sources.append({
            "source_kind": "artifact" if clip.artifact is not None else "task",
            "media_kind": clip.media_kind,
            "source_id": str(clip.artifact.id if clip.artifact is not None else clip.task.id),
            "shot_id": str(selection.shot.id),
            "source": clip.path.relative_to(root).as_posix(),
            "start_ms": round(clip.start_seconds * 1000),
            "duration_ms": round(clip.duration_seconds * 1000),
            "trim_to_shot": clip.trim_to_shot,
            "has_audio_stream": clip.has_audio,
            "sha256": sha256_file(clip.path),
        })
    label_enabled, label_text = _delivery_disclosure()
    payload = {
        "version": _ASSEMBLY_SIGNATURE_VERSION,
        "owner_id": str(owner.id),
        "project_id": str(project.id),
        "episode_id": str(episode.id) if episode else None,
        "scope": AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES,
        "sources": sources,
        "output": {
            "requested_name": output_name,
            "width": project.width,
            "height": project.height,
            "fps": project.fps,
            "media_profile_key": media_profile.key,
            "media_profile_sha256": media_profile.sha256,
            "media_profile": media_profile.value,
            "ai_label_enabled": label_enabled,
            "ai_label_text": label_text,
            "ai_label_fontfile": str(settings.ASSEMBLY_AI_LABEL_FONTFILE or ""),
            "mp4_metadata": bool(label_text.strip()),
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored_source_signature(job: AssemblyJob) -> str:
    if job.source_signature:
        return job.source_signature
    match = _ASSEMBLY_SIGNATURE_RE.search(str(job.concat_manifest_path or ""))
    return match.group(1) if match else ""


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
) -> AssemblyJob | None:
    scope = AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES
    queryset = AssemblyJob.objects.filter(
        owner=owner,
        project=project,
        episode=episode,
        scope=scope,
        source_contract_version=_ASSEMBLY_SIGNATURE_VERSION,
        dry_run=False,
        status__in=(
            AssemblyJob.Status.QUEUED,
            AssemblyJob.Status.RUNNING,
            AssemblyJob.Status.SUCCEEDED,
        ),
    ).order_by("-created_at")
    for job in queryset:
        if _stored_source_signature(job) != source_signature:
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
    task_ids_by_shot: dict[str, str] | None = None,
    source_refs_by_shot: dict[str, dict[str, str]] | None = None,
) -> AssemblyJob:
    try:
        media_profile = resolve_assembly_media_profile(project=project)
    except MediaProcessingProfileError as exc:
        raise AssemblyError(str(exc)) from exc
    scope = AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES
    episodes = [episode] if episode else list(project.episodes.order_by("number"))
    if not episodes:
        raise AssemblyError("项目没有可组装的剧集")
    selections = []
    for item in episodes:
        if source_refs_by_shot is not None:
            episode_mapping = {
                str(shot.id): source_refs_by_shot[str(shot.id)]
                for shot in item.shots.all()
                if str(shot.id) in source_refs_by_shot
            }
            selections.extend(_ordered_frozen_sources_for_episode(item, episode_mapping))
        elif task_ids_by_shot is None:
            selections.extend(_ordered_tasks_for_episode(item))
        else:
            episode_mapping = {
                str(shot.id): {"generation_task_id": str(task_ids_by_shot[str(shot.id)])}
                for shot in item.shots.all()
                if str(shot.id) in task_ids_by_shot
            }
            selections.extend(_ordered_frozen_sources_for_episode(item, episode_mapping))
    stream_cache: dict[Path, frozenset[str]] = {}
    clips = [
        _resolve_output(
            selection,
            stream_cache,
            media_profile=media_profile.value,
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
        media_profile=media_profile,
    )
    # Source tasks are an audit set; clips intentionally keep repeated multi-shot
    # tasks because each occurrence is trimmed to a different shot boundary.
    tasks = list(dict.fromkeys(
        selection.task for selection in selections if selection.task is not None
    ))

    # Serialize idempotency lookup + durable enqueue.  The expensive media probe
    # happens before this short transaction, while the mutex prevents two API
    # requests with the same source snapshot from creating duplicate FFmpeg work.
    preparation_failure: AssemblyError | None = None
    preparation_cause: BaseException | None = None
    with transaction.atomic():
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
            )
            if reusable is not None:
                if reusable.status == AssemblyJob.Status.SUCCEEDED:
                    _mark_scope_complete(project, episode)
                return reusable

        try:
            max_attempts = resolve_configuration(project=project, required_keys=(
                "delivery.assembly_max_attempts",
            )).values["delivery.assembly_max_attempts"]
        except ConfigurationResolutionError as exc:
            raise AssemblyError(str(exc)) from exc
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise AssemblyError("冻结的组装重试上限无效")
        job = AssemblyJob.objects.create(
            owner=owner, project=project, episode=episode, scope=scope,
            status=AssemblyJob.Status.PLANNED, dry_run=dry_run,
            source_signature=source_signature,
            source_contract_version=_ASSEMBLY_SIGNATURE_VERSION,
            max_attempts=max_attempts,
            # Every assembled MP4 has an AAC track.  Source clips without an audio
            # stream receive duration-matched silence in ``_ffmpeg_command``.
            has_audio=True,
        )
        job.source_tasks.set(tasks)
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
                generation_task=selection.task,
                generated_artifact=selection.artifact,
                storage_backend=(
                    selection.artifact.storage_backend if selection.artifact is not None
                    else "comfyui_output"
                ),
                object_key=(
                    selection.artifact.object_key if selection.artifact is not None
                    else clip.path.relative_to(Path(settings.COMFYUI_OUTPUT_DIR).resolve()).as_posix()
                ),
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
                project.width,
                project.height,
                project.fps,
                media_profile=media_profile.value,
            )
            manifest = output_dir / f"{job.id}.source-{source_signature}.ffconcat"
            manifest_lines = ["ffconcat version 1.0"]
            for selection, clip in zip(selections, clips, strict=True):
                manifest_lines.extend((
                    _manifest_shot_line(selection.shot),
                    _manifest_line(clip.path),
                ))
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
