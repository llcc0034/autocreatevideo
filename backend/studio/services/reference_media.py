"""Safe task-local media normalization for MiniMax H3 Ref2VA inputs.

MiniMax H3's reference-video conditioner interprets frame sequences at 24 fps.
Uploaded assets are immutable source material, so execution creates a temporary
constant-frame-rate derivative and uploads that derivative to ComfyUI.  Paired
audio is similarly padded/trimmed to the derivative's exact frame duration.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess

from django.conf import settings


H3_REFERENCE_FPS = 24
MIN_REFERENCE_SECONDS = 2.0
MAX_REFERENCE_SECONDS = 15.0


class ReferenceMediaNormalizationError(RuntimeError):
    """The source cannot be proven safe for a Ref2VA execution input."""


@dataclass(frozen=True)
class ReferenceVideoPlan:
    source_fps: float
    source_fps_fraction: str
    source_avg_frame_rate: str
    source_r_frame_rate: str
    source_duration_seconds: float
    source_has_audio: bool
    effective_frames: int
    effective_duration_seconds: float


def _positive_fraction(value) -> Fraction | None:
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _stream_duration(stream: dict) -> float | None:
    direct = _positive_float(stream.get("duration"))
    if direct is not None:
        return direct
    duration_ts = _positive_float(stream.get("duration_ts"))
    time_base = _positive_fraction(stream.get("time_base"))
    if duration_ts is not None and time_base is not None:
        return duration_ts * float(time_base)
    return None


def _run(command: list[str], *, timeout: int, operation: str) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReferenceMediaNormalizationError(f"{operation}无法启动: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown ffmpeg error").strip()
        if len(detail) > 1200:
            detail = f"{detail[:1200]}..."
        raise ReferenceMediaNormalizationError(f"{operation}失败: {detail}")
    return completed


def probe_media(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file():
        raise ReferenceMediaNormalizationError(f"参考媒体不存在: {source.name}")
    completed = _run(
        [
            str(settings.FFPROBE_BINARY), "-v", "error", "-count_frames",
            "-show_streams", "-show_format", "-of", "json", str(source),
        ],
        timeout=45,
        operation="ffprobe 参考媒体检测",
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ReferenceMediaNormalizationError("ffprobe 返回了无效 JSON") from exc
    if not isinstance(payload.get("streams"), list):
        raise ReferenceMediaNormalizationError("ffprobe 结果缺少 streams")
    return payload


def inspect_reference_video(path: str | Path) -> ReferenceVideoPlan:
    probe = probe_media(path)
    streams = probe["streams"]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ReferenceMediaNormalizationError("参考视频不含可解码视频流")

    average = _positive_fraction(video.get("avg_frame_rate"))
    nominal = _positive_fraction(video.get("r_frame_rate"))
    source_rate = average or nominal
    if source_rate is None:
        raise ReferenceMediaNormalizationError(
            "无法可靠确定参考视频源 FPS；为避免错误解释帧时序，任务已阻断"
        )

    duration = _stream_duration(video)
    if duration is None:
        duration = _positive_float((probe.get("format") or {}).get("duration"))
    if duration is None:
        frame_count = _positive_float(video.get("nb_read_frames") or video.get("nb_frames"))
        if frame_count is not None:
            duration = frame_count / float(source_rate)
    if duration is None or not MIN_REFERENCE_SECONDS <= duration <= MAX_REFERENCE_SECONDS:
        raise ReferenceMediaNormalizationError(
            f"参考视频时长无法可靠确定或不在 {MIN_REFERENCE_SECONDS:g}–{MAX_REFERENCE_SECONDS:g} 秒范围内"
        )

    effective_frames = max(1, int(math.floor(duration * H3_REFERENCE_FPS + 0.5)))
    return ReferenceVideoPlan(
        source_fps=float(source_rate),
        source_fps_fraction=str(source_rate),
        source_avg_frame_rate=str(video.get("avg_frame_rate") or ""),
        source_r_frame_rate=str(video.get("r_frame_rate") or ""),
        source_duration_seconds=duration,
        source_has_audio=any(item.get("codec_type") == "audio" for item in streams),
        effective_frames=effective_frames,
        effective_duration_seconds=effective_frames / H3_REFERENCE_FPS,
    )


def _verify_normalized_video(
    path: Path, *, plan: ReferenceVideoPlan, expect_audio: bool,
) -> dict:
    probe = probe_media(path)
    streams = probe["streams"]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ReferenceMediaNormalizationError("规范化产物缺少视频流")
    average = _positive_fraction(video.get("avg_frame_rate"))
    nominal = _positive_fraction(video.get("r_frame_rate"))
    if average != Fraction(H3_REFERENCE_FPS, 1) or nominal != Fraction(H3_REFERENCE_FPS, 1):
        raise ReferenceMediaNormalizationError(
            "规范化产物未通过 CFR 24 验证；任务已阻断，未提交到 ComfyUI"
        )
    frame_count_value = video.get("nb_read_frames") or video.get("nb_frames")
    try:
        frame_count = int(frame_count_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceMediaNormalizationError("规范化产物无法验证实际帧数") from exc
    if frame_count != plan.effective_frames:
        raise ReferenceMediaNormalizationError(
            f"规范化产物帧数不符: expected={plan.effective_frames}, actual={frame_count}"
        )

    video_duration = _stream_duration(video)
    if video_duration is None:
        video_duration = _positive_float((probe.get("format") or {}).get("duration"))
    if video_duration is None or abs(video_duration - plan.effective_duration_seconds) > 0.06:
        raise ReferenceMediaNormalizationError("规范化产物的视频时长未通过验证")

    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if expect_audio and audio is None:
        raise ReferenceMediaNormalizationError("已请求内嵌音轨，但规范化产物缺少音频流")
    audio_duration = _stream_duration(audio) if audio else None
    if expect_audio and (
        audio_duration is None
        or abs(audio_duration - plan.effective_duration_seconds) > 0.08
    ):
        raise ReferenceMediaNormalizationError("内嵌音轨与 CFR24 视频时长不同步")
    return {
        "verified_video_duration_ms": round(video_duration * 1000),
        "verified_audio_duration_ms": round(audio_duration * 1000) if audio_duration else None,
        "verified_frame_count": frame_count,
    }


def normalize_reference_video(
    source: str | Path,
    output: str | Path,
    *,
    include_embedded_audio: bool,
    plan: ReferenceVideoPlan | None = None,
) -> dict:
    """Create and prove a task-local CFR24 MP4 without modifying ``source``."""
    source_path = Path(source)
    output_path = Path(output)
    plan = plan or inspect_reference_video(source_path)
    if include_embedded_audio and not plan.source_has_audio:
        raise ReferenceMediaNormalizationError(
            "参考视频没有可用内嵌音轨，不能启用 use_embedded_audio"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target = f"{plan.effective_duration_seconds:.9f}"
    video_filter = (
        "setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop_duration=1,"
        f"fps={H3_REFERENCE_FPS},setpts=N/({H3_REFERENCE_FPS}*TB)"
    )
    command = [
        str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source_path), "-map", "0:v:0", "-vf", video_filter,
        "-frames:v", str(plan.effective_frames), "-c:v", "libx264", "-preset", "medium",
        "-crf", "12", "-pix_fmt", "yuv420p",
    ]
    if include_embedded_audio:
        command.extend([
            "-map", "0:a:0", "-af",
            f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad,atrim=duration={target}",
            "-c:a", "aac", "-b:a", "192k",
        ])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(output_path)])
    _run(command, timeout=180, operation="参考视频 CFR24 规范化")
    verification = _verify_normalized_video(
        output_path, plan=plan, expect_audio=include_embedded_audio,
    )
    return {
        "staging_version": 2,
        "media_role": "reference_video",
        "source_fps": round(plan.source_fps, 6),
        "source_fps_fraction": plan.source_fps_fraction,
        "source_avg_frame_rate": plan.source_avg_frame_rate,
        "source_r_frame_rate": plan.source_r_frame_rate,
        "source_duration_ms": round(plan.source_duration_seconds * 1000),
        "effective_fps": H3_REFERENCE_FPS,
        "effective_fps_fraction": f"{H3_REFERENCE_FPS}/1",
        "effective_frames": plan.effective_frames,
        "effective_duration_ms": round(plan.effective_duration_seconds * 1000),
        "cfr_guaranteed": True,
        "original_asset_preserved": True,
        "embedded_audio_policy": "aligned" if include_embedded_audio else "not_staged",
        **verification,
    }


def normalize_paired_audio(
    source: str | Path,
    output: str | Path,
    *,
    paired_video_asset_id: str,
    effective_duration_seconds: float,
) -> dict:
    """Pad/trim an external paired track to the normalized video's duration."""
    source_path = Path(source)
    output_path = Path(output)
    probe = probe_media(source_path)
    audio = next((item for item in probe["streams"] if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ReferenceMediaNormalizationError("配对音频不含可解码音频流")
    source_duration = _stream_duration(audio)
    if source_duration is None:
        source_duration = _positive_float((probe.get("format") or {}).get("duration"))
    if source_duration is None:
        raise ReferenceMediaNormalizationError("无法可靠确定配对音频时长")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target = f"{effective_duration_seconds:.9f}"
    _run(
        [
            str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source_path), "-map", "0:a:0", "-vn", "-af",
            f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad,atrim=duration={target}",
            "-c:a", "pcm_s16le", str(output_path),
        ],
        timeout=120,
        operation="Ref2VA 配对音频时长规范化",
    )
    output_probe = probe_media(output_path)
    output_audio = next(
        (item for item in output_probe["streams"] if item.get("codec_type") == "audio"), None,
    )
    output_duration = _stream_duration(output_audio or {})
    if output_duration is None or abs(output_duration - effective_duration_seconds) > 0.05:
        raise ReferenceMediaNormalizationError("配对音频与 CFR24 视频时长不同步")
    return {
        "staging_version": 2,
        "media_role": "paired_reference_audio",
        "paired_video_asset_id": str(paired_video_asset_id),
        "source_duration_ms": round(source_duration * 1000),
        "effective_fps": H3_REFERENCE_FPS,
        "effective_duration_ms": round(effective_duration_seconds * 1000),
        "duration_aligned": True,
        "original_asset_preserved": True,
        "verified_audio_duration_ms": round(output_duration * 1000),
    }
