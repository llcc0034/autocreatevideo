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
    Episode,
    GenerationTask,
    SeriesProject,
    Shot,
    WorkerLease,
)
from studio.services.provenance import ProvenanceError, sha256_file, verified_assembly_manifest


class AssemblyError(ValueError):
    def __init__(self, message: str, job: AssemblyJob | None = None):
        super().__init__(message)
        self.job = job


@dataclass(frozen=True)
class TaskSelection:
    task: GenerationTask
    shot: Shot


@dataclass(frozen=True)
class Clip:
    task: GenerationTask
    path: Path
    has_audio: bool
    duration_seconds: float
    start_seconds: float = 0
    trim_to_shot: bool = False


_ASSEMBLY_SIGNATURE_VERSION = 2
_ASSEMBLY_SIGNATURE_RE = re.compile(r"\.source-([0-9a-f]{64})\.ffconcat$")


def _eligible_tasks(episode: Episode):
    """Read one ordered task snapshot to avoid racing completion between queries."""

    return list(
        GenerationTask.objects.filter(
            episode=episode,
            kind=GenerationTask.Kind.VIDEO,
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
    """Select the newest successful source independently for every video shot.

    A multi-shot task is intentionally represented once per covered shot.  The
    resolver below cuts the persisted multi-shot video at its prompt-time shot
    boundaries, allowing a newer single-shot result to replace only its own
    segment without silently retaining or duplicating the old segment.
    """

    shots = list(episode.shots.filter(media_type=Shot.MediaType.VIDEO).order_by("number"))
    if not shots:
        raise AssemblyError(f"第 {episode.number} 集没有可组装的视频分镜")
    tasks = _eligible_tasks(episode)
    coverage = {task.id: set(_task_shot_ids(task)) for task in tasks}
    selected: list[TaskSelection] = []
    for shot in shots:
        task = next((item for item in tasks if str(shot.id) in coverage[item.id]), None)
        if task and task.status != GenerationTask.Status.SUCCEEDED:
            raise AssemblyError(
                f"第 {episode.number} 集分镜 {shot.number} 的最新视频仍生成中，完成后再组装"
            )
        if not task:
            raise AssemblyError(f"第 {episode.number} 集分镜 {shot.number} 缺少成功的视频任务")
        selected.append(TaskSelection(task=task, shot=shot))
    return selected


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


def _probe_media_streams(path: Path) -> frozenset[str]:
    """Return streams actually present in a persisted input media file.

    Prompt intent is not evidence that ComfyUI emitted an audio stream.  A
    failed probe is therefore a hard assembly error instead of silently
    constructing an FFmpeg graph that references a missing ``a:0`` stream.
    """

    try:
        result = subprocess.run(
            [
                _ffprobe_binary(), "-v", "error",
                "-show_entries", "stream=codec_type", "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=float(getattr(settings, "ASSEMBLY_PROBE_TIMEOUT_SECONDS", 30)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
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
) -> Clip:
    task = selection.task
    if not settings.COMFYUI_OUTPUT_DIR:
        raise AssemblyError("COMFYUI_OUTPUT_DIR 未配置")
    output_root = Path(settings.COMFYUI_OUTPUT_DIR).resolve()
    video = next((item for item in task.outputs if (
        item.get("kind") in {"videos", "gifs"}
        or str(item.get("filename", "")).lower().endswith((".mp4", ".mov", ".mkv", ".webm"))
    )), None)
    if not video or not video.get("filename"):
        raise AssemblyError(f"任务 {task.id} 没有可组装的视频输出")
    candidate = (output_root / str(video.get("subfolder", "")) / str(video["filename"])).resolve()
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise AssemblyError(f"任务 {task.id} 输出路径越过 ComfyUI 输出目录") from exc
    if not candidate.is_file():
        raise AssemblyError(f"任务 {task.id} 输出文件不存在: {candidate}")
    streams = stream_cache.get(candidate) if stream_cache is not None else None
    if streams is None:
        streams = _probe_media_streams(candidate)
        if stream_cache is not None:
            stream_cache[candidate] = streams
    if "video" not in streams:
        raise AssemblyError(f"任务 {task.id} 输出文件没有可组装的视频流: {candidate.name}")
    has_audio = "audio" in streams
    prompt = task.generated_workflow.prompt_artifact
    if prompt.is_multi_shot:
        start_seconds, duration_seconds = _multi_shot_window(selection)
        return Clip(
            task, candidate, has_audio, max(0.1, duration_seconds),
            max(0, start_seconds), True,
        )
    duration_ms = task.effective_duration_ms or prompt.total_duration_ms
    return Clip(task, candidate, has_audio, max(0.1, duration_ms / 1000))


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


def _ffmpeg_command(clips: list[Clip], output: Path, width: int, height: int) -> list[str]:
    command = [settings.FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", "-y"]
    for clip in clips:
        command.extend(("-i", str(clip.path)))
    filters = []
    concat_inputs = []
    for index, clip in enumerate(clips):
        video_trim = (
            f"trim=start={clip.start_seconds:.3f}:duration={clip.duration_seconds:.3f},"
            if clip.trim_to_shot else ""
        )
        filters.append(
            f"[{index}:v:0]{video_trim}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=24,format=yuv420p,"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")
        if clip.has_audio:
            audio_trim = (
                f"atrim=start={clip.start_seconds:.3f}:duration={clip.duration_seconds:.3f},"
                if clip.trim_to_shot else ""
            )
            filters.append(
                f"[{index}:a:0]{audio_trim}aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={clip.duration_seconds:.3f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        concat_inputs.append(f"[a{index}]")
    label_text = str(settings.ASSEMBLY_AI_LABEL_TEXT or "").strip()
    concat_video_label = "vconcat" if settings.ASSEMBLY_AI_LABEL_ENABLED else "vout"
    filters.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[{concat_video_label}][aout]")
    if settings.ASSEMBLY_AI_LABEL_ENABLED:
        fontfile = _resolve_ai_label_fontfile()
        drawtext = (
            f"drawtext=text='{_drawtext_value(label_text)}':"
            "fontcolor=white:fontsize=h/40:box=1:boxcolor=black@0.62:boxborderw=10:"
            "x=w-tw-24:y=h-th-24"
        )
        if fontfile:
            drawtext = (
                f"drawtext=fontfile='{_drawtext_value(fontfile)}':"
                + drawtext.removeprefix("drawtext=")
            )
        filters.append(f"[vconcat]{drawtext}[vout]")
    command.extend((
        "-filter_complex", ";".join(filters), "-map", "[vout]",
        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
    ))
    command.extend(("-c:v", "libx264", "-preset", "medium", "-crf", "18"))
    if label_text:
        command.extend((
            "-metadata", f"comment={label_text}",
            "-metadata", "description=AI-generated content produced with MiniMax H3",
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
) -> str:
    output_root = Path(settings.COMFYUI_OUTPUT_DIR).resolve()
    sources = []
    for selection, clip in zip(selections, clips, strict=True):
        sources.append({
            "task_id": str(clip.task.id),
            "shot_id": str(selection.shot.id),
            "source": clip.path.relative_to(output_root).as_posix(),
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
            "width": project.width,
            "height": project.height,
            "fps": 24,
            "video_codec": "libx264",
            "crf": 18,
            "audio_codec": "aac",
            "ai_label_enabled": bool(settings.ASSEMBLY_AI_LABEL_ENABLED),
            "ai_label_text": str(settings.ASSEMBLY_AI_LABEL_TEXT),
            "ai_label_fontfile": str(settings.ASSEMBLY_AI_LABEL_FONTFILE or ""),
            "mp4_metadata": bool(str(settings.ASSEMBLY_AI_LABEL_TEXT or "").strip()),
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
    *, owner, project: SeriesProject, episode: Episode | None, dry_run: bool, output_name: str = "",
) -> AssemblyJob:
    scope = AssemblyJob.Scope.EPISODE if episode else AssemblyJob.Scope.SERIES
    episodes = [episode] if episode else list(project.episodes.order_by("number"))
    if not episodes:
        raise AssemblyError("项目没有可组装的剧集")
    selections = []
    for item in episodes:
        selections.extend(_ordered_tasks_for_episode(item))
    stream_cache: dict[Path, frozenset[str]] = {}
    clips = [_resolve_output(selection, stream_cache) for selection in selections]
    normalized_output_name = _normalized_requested_output_name(output_name)
    source_signature = _assembly_source_signature(
        owner=owner,
        project=project,
        episode=episode,
        selections=selections,
        clips=clips,
        output_name=normalized_output_name,
    )
    # Source tasks are an audit set; clips intentionally keep repeated multi-shot
    # tasks because each occurrence is trimmed to a different shot boundary.
    tasks = list(dict.fromkeys(selection.task for selection in selections))

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

        job = AssemblyJob.objects.create(
            owner=owner, project=project, episode=episode, scope=scope,
            status=AssemblyJob.Status.PLANNED, dry_run=dry_run,
            source_signature=source_signature,
            # Every assembled MP4 has an AAC track.  Source clips without an audio
            # stream receive duration-matched silence in ``_ffmpeg_command``.
            has_audio=True,
        )
        job.source_tasks.set(tasks)
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
            command = _ffmpeg_command(clips, output, project.width, project.height)
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
