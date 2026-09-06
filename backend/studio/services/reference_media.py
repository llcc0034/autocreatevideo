"""Safe task-local media preprocessing for configured reference contracts.

Uploaded assets are immutable source material.  The reviewed ``cfr_video_v1``
adapter creates a task-local constant-frame-rate derivative at the target FPS
declared by the frozen workflow contract.  The reviewed ``aligned_audio_v1``
adapter pads/trims paired audio to that derivative's exact frame duration.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess

from django.conf import settings

from studio.services.media_processing_profiles import (
    REFERENCE_PROFILE_KEY,
    MediaProcessingProfileError,
    media_profile_sha256,
    resolve_reference_media_profile,
    validate_reference_profile,
)


class ReferenceMediaNormalizationError(RuntimeError):
    """The source cannot be proven safe for the frozen media-input contract."""


@dataclass(frozen=True)
class ReferenceVideoPlan:
    target_fps: int
    source_fps: float
    source_fps_fraction: str
    source_avg_frame_rate: str
    source_r_frame_rate: str
    source_duration_seconds: float
    source_has_audio: bool
    effective_frames: int
    effective_duration_seconds: float


@dataclass(frozen=True)
class PassthroughVideoPlan:
    source_fps: float | None
    source_fps_fraction: str
    source_avg_frame_rate: str
    source_r_frame_rate: str
    source_duration_seconds: float
    source_has_audio: bool
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


def _reference_profile(media_profile: dict | None) -> dict:
    try:
        return (
            validate_reference_profile(media_profile)
            if media_profile is not None
            else resolve_reference_media_profile().value
        )
    except MediaProcessingProfileError as exc:
        raise ReferenceMediaNormalizationError(str(exc)) from exc


def probe_media(path: str | Path, *, media_profile: dict | None = None) -> dict:
    profile = _reference_profile(media_profile)
    source = Path(path)
    if not source.is_file():
        raise ReferenceMediaNormalizationError(f"参考媒体不存在: {source.name}")
    completed = _run(
        [
            str(settings.FFPROBE_BINARY), "-v", "error", "-count_frames",
            "-show_streams", "-show_format", "-of", "json", str(source),
        ],
        timeout=profile["probe_timeout_seconds"],
        operation="ffprobe 参考媒体检测",
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ReferenceMediaNormalizationError("ffprobe 返回了无效 JSON") from exc
    if not isinstance(payload.get("streams"), list):
        raise ReferenceMediaNormalizationError("ffprobe 结果缺少 streams")
    return payload


def inspect_reference_video(
    path: str | Path,
    *,
    target_fps: int,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
    security_max_fps: int,
    security_max_duration_ms: int,
    media_profile: dict | None = None,
) -> ReferenceVideoPlan:
    if (
        isinstance(target_fps, bool)
        or not isinstance(target_fps, int)
        or type(security_max_fps) is not int
        or not 1 <= target_fps <= security_max_fps
    ):
        raise ReferenceMediaNormalizationError(
            f"目标 FPS 必须是 1–{security_max_fps} 的整数"
        )
    if (
        isinstance(minimum_duration_ms, bool)
        or isinstance(maximum_duration_ms, bool)
        or not isinstance(minimum_duration_ms, int)
        or not isinstance(maximum_duration_ms, int)
        or type(security_max_duration_ms) is not int
        or not 1 <= minimum_duration_ms <= maximum_duration_ms <= security_max_duration_ms
    ):
        raise ReferenceMediaNormalizationError("参考视频时长合同无效")
    probe = probe_media(path, media_profile=media_profile)
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
    minimum_seconds = minimum_duration_ms / 1000
    maximum_seconds = maximum_duration_ms / 1000
    if duration is None or not minimum_seconds <= duration <= maximum_seconds:
        raise ReferenceMediaNormalizationError(
            "参考视频时长无法可靠确定或不在冻结合同范围内："
            f"{minimum_duration_ms}–{maximum_duration_ms}ms"
        )

    effective_frames = max(1, int(math.floor(duration * target_fps + 0.5)))
    return ReferenceVideoPlan(
        target_fps=target_fps,
        source_fps=float(source_rate),
        source_fps_fraction=str(source_rate),
        source_avg_frame_rate=str(video.get("avg_frame_rate") or ""),
        source_r_frame_rate=str(video.get("r_frame_rate") or ""),
        source_duration_seconds=duration,
        source_has_audio=any(item.get("codec_type") == "audio" for item in streams),
        effective_frames=effective_frames,
        effective_duration_seconds=effective_frames / target_fps,
    )


def inspect_passthrough_reference_video(
    path: str | Path,
    *,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
    requires_source_fps: bool,
    security_max_duration_ms: int,
    media_profile: dict | None = None,
) -> PassthroughVideoPlan:
    """Validate a frozen passthrough-video contract without rewriting bytes."""

    if (
        isinstance(minimum_duration_ms, bool)
        or isinstance(maximum_duration_ms, bool)
        or not isinstance(minimum_duration_ms, int)
        or not isinstance(maximum_duration_ms, int)
        or type(security_max_duration_ms) is not int
        or not 1 <= minimum_duration_ms <= maximum_duration_ms <= security_max_duration_ms
    ):
        raise ReferenceMediaNormalizationError("参考视频时长合同无效")
    probe = probe_media(path, media_profile=media_profile)
    streams = probe["streams"]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ReferenceMediaNormalizationError("参考视频不含可解码视频流")
    average = _positive_fraction(video.get("avg_frame_rate"))
    nominal = _positive_fraction(video.get("r_frame_rate"))
    source_rate = average or nominal
    if requires_source_fps and source_rate is None:
        raise ReferenceMediaNormalizationError("冻结合同要求可验证源 FPS")
    duration = _stream_duration(video)
    if duration is None:
        duration = _positive_float((probe.get("format") or {}).get("duration"))
    if duration is None and source_rate is not None:
        frame_count = _positive_float(video.get("nb_read_frames") or video.get("nb_frames"))
        if frame_count is not None:
            duration = frame_count / float(source_rate)
    if (
        duration is None
        or not minimum_duration_ms / 1000 <= duration <= maximum_duration_ms / 1000
    ):
        raise ReferenceMediaNormalizationError(
            "参考视频时长无法可靠确定或不在冻结合同范围内："
            f"{minimum_duration_ms}–{maximum_duration_ms}ms"
        )
    return PassthroughVideoPlan(
        source_fps=float(source_rate) if source_rate is not None else None,
        source_fps_fraction=str(source_rate or ""),
        source_avg_frame_rate=str(video.get("avg_frame_rate") or ""),
        source_r_frame_rate=str(video.get("r_frame_rate") or ""),
        source_duration_seconds=duration,
        source_has_audio=any(item.get("codec_type") == "audio" for item in streams),
        effective_duration_seconds=duration,
    )


def passthrough_reference_video_metadata(
    *,
    plan: PassthroughVideoPlan,
    include_embedded_audio: bool,
    media_profile: dict | None = None,
) -> dict:
    """Return proven metadata for the reviewed byte-preserving video adapter."""

    profile = _reference_profile(media_profile)
    if include_embedded_audio != plan.source_has_audio:
        raise ReferenceMediaNormalizationError(
            "passthrough_video_v1 无法改变内嵌音轨；绑定选择必须与源文件一致"
        )
    return {
        "staging_version": 2,
        "media_role": "reference_video",
        "preprocessing_adapter_key": "passthrough_video_v1",
        "source_fps": round(plan.source_fps, 6) if plan.source_fps is not None else None,
        "source_fps_fraction": plan.source_fps_fraction,
        "source_avg_frame_rate": plan.source_avg_frame_rate,
        "source_r_frame_rate": plan.source_r_frame_rate,
        "source_duration_ms": round(plan.source_duration_seconds * 1000),
        "effective_fps": round(plan.source_fps, 6) if plan.source_fps is not None else None,
        "effective_duration_ms": round(plan.effective_duration_seconds * 1000),
        "cfr_guaranteed": False,
        "original_asset_preserved": True,
        "source_bytes_submitted": True,
        "embedded_audio_policy": "preserved" if include_embedded_audio else "absent",
        "media_processing_profile_key": REFERENCE_PROFILE_KEY,
        "media_processing_profile_sha256": media_profile_sha256(profile),
    }


def passthrough_reference_audio_metadata(
    path: str | Path,
    *,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
    security_max_duration_ms: int,
    media_profile: dict | None = None,
) -> dict:
    """Validate and describe a standalone audio file submitted unchanged."""

    if (
        isinstance(minimum_duration_ms, bool)
        or isinstance(maximum_duration_ms, bool)
        or not isinstance(minimum_duration_ms, int)
        or not isinstance(maximum_duration_ms, int)
        or type(security_max_duration_ms) is not int
        or not 1 <= minimum_duration_ms <= maximum_duration_ms <= security_max_duration_ms
    ):
        raise ReferenceMediaNormalizationError("参考音频时长合同无效")
    profile = _reference_profile(media_profile)
    probe = probe_media(path, media_profile=profile)
    audio = next((item for item in probe["streams"] if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ReferenceMediaNormalizationError("参考音频不含可解码音频流")
    duration = _stream_duration(audio)
    if duration is None:
        duration = _positive_float((probe.get("format") or {}).get("duration"))
    if (
        duration is None
        or not minimum_duration_ms / 1000 <= duration <= maximum_duration_ms / 1000
    ):
        raise ReferenceMediaNormalizationError(
            "参考音频时长无法可靠确定或不在冻结合同范围内："
            f"{minimum_duration_ms}–{maximum_duration_ms}ms"
        )
    return {
        "staging_version": 2,
        "media_role": "reference_audio",
        "preprocessing_adapter_key": "passthrough_audio_v1",
        "source_duration_ms": round(duration * 1000),
        "effective_duration_ms": round(duration * 1000),
        "original_asset_preserved": True,
        "source_bytes_submitted": True,
        "media_processing_profile_key": REFERENCE_PROFILE_KEY,
        "media_processing_profile_sha256": media_profile_sha256(profile),
    }


def _verify_normalized_video(
    path: Path, *, plan: ReferenceVideoPlan, expect_audio: bool,
    media_profile: dict,
) -> dict:
    profile = _reference_profile(media_profile)
    probe = probe_media(path, media_profile=profile)
    streams = probe["streams"]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ReferenceMediaNormalizationError("规范化产物缺少视频流")
    average = _positive_fraction(video.get("avg_frame_rate"))
    nominal = _positive_fraction(video.get("r_frame_rate"))
    if (
        average != Fraction(plan.target_fps, 1)
        or nominal != Fraction(plan.target_fps, 1)
    ):
        raise ReferenceMediaNormalizationError(
            f"规范化产物未通过 CFR {plan.target_fps} 验证；"
            "任务已阻断，未提交到 ComfyUI"
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
    if (
        video_duration is None
        or abs(video_duration - plan.effective_duration_seconds)
        > profile["video_duration_tolerance_ms"] / 1000
    ):
        raise ReferenceMediaNormalizationError("规范化产物的视频时长未通过验证")

    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if expect_audio and audio is None:
        raise ReferenceMediaNormalizationError("已请求内嵌音轨，但规范化产物缺少音频流")
    audio_duration = _stream_duration(audio) if audio else None
    if expect_audio and (
        audio_duration is None
        or abs(audio_duration - plan.effective_duration_seconds)
        > profile["embedded_audio_duration_tolerance_ms"] / 1000
    ):
        raise ReferenceMediaNormalizationError("内嵌音轨与目标 CFR 视频时长不同步")
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
    plan: ReferenceVideoPlan,
    media_profile: dict | None = None,
) -> dict:
    """Create and prove a task-local CFR derivative without modifying source."""
    source_path = Path(source)
    output_path = Path(output)
    profile = _reference_profile(media_profile)
    if include_embedded_audio and not plan.source_has_audio:
        raise ReferenceMediaNormalizationError(
            "参考视频没有可用内嵌音轨，不能启用 use_embedded_audio"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target = f"{plan.effective_duration_seconds:.9f}"
    video_filter = (
        "setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop_duration=1,"
        f"fps={plan.target_fps},setpts=N/({plan.target_fps}*TB)"
    )
    command = [
        str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source_path), "-map", "0:v:0", "-vf", video_filter,
        "-frames:v", str(plan.effective_frames),
        "-c:v", profile["video_codec"], "-preset", profile["video_preset"],
        "-crf", str(profile["video_crf"]), "-pix_fmt", profile["pixel_format"],
    ]
    if include_embedded_audio:
        command.extend([
            "-map", "0:a:0", "-af",
            f"asetpts=PTS-STARTPTS,aresample={profile['audio_sample_rate_hz']}:"
            f"async=1:first_pts=0,apad,atrim=duration={target}",
            "-c:a", profile["embedded_audio_codec"],
            "-b:a", f"{profile['embedded_audio_bitrate_kbps']}k",
        ])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(output_path)])
    _run(
        command,
        timeout=profile["video_normalize_timeout_seconds"],
        operation=f"参考视频 CFR{plan.target_fps} 规范化",
    )
    verification = _verify_normalized_video(
        output_path,
        plan=plan,
        expect_audio=include_embedded_audio,
        media_profile=profile,
    )
    return {
        "staging_version": 2,
        "media_role": "reference_video",
        "source_fps": round(plan.source_fps, 6),
        "source_fps_fraction": plan.source_fps_fraction,
        "source_avg_frame_rate": plan.source_avg_frame_rate,
        "source_r_frame_rate": plan.source_r_frame_rate,
        "source_duration_ms": round(plan.source_duration_seconds * 1000),
        "preprocessing_adapter_key": "cfr_video_v1",
        "effective_fps": plan.target_fps,
        "effective_fps_fraction": f"{plan.target_fps}/1",
        "effective_frames": plan.effective_frames,
        "effective_duration_ms": round(plan.effective_duration_seconds * 1000),
        "cfr_guaranteed": True,
        "original_asset_preserved": True,
        "embedded_audio_policy": "aligned" if include_embedded_audio else "not_staged",
        "media_processing_profile_key": REFERENCE_PROFILE_KEY,
        "media_processing_profile_sha256": media_profile_sha256(profile),
        **verification,
    }


def normalize_paired_audio(
    source: str | Path,
    output: str | Path,
    *,
    paired_video_asset_id: str,
    effective_duration_seconds: float,
    effective_fps: int | float | None,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
    security_max_duration_ms: int,
    media_profile: dict | None = None,
) -> dict:
    """Pad/trim an external paired track to the normalized video's duration."""
    source_path = Path(source)
    output_path = Path(output)
    profile = _reference_profile(media_profile)
    probe = probe_media(source_path, media_profile=profile)
    audio = next((item for item in probe["streams"] if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ReferenceMediaNormalizationError("配对音频不含可解码音频流")
    source_duration = _stream_duration(audio)
    if source_duration is None:
        source_duration = _positive_float((probe.get("format") or {}).get("duration"))
    if source_duration is None:
        raise ReferenceMediaNormalizationError("无法可靠确定配对音频时长")
    if (
        isinstance(minimum_duration_ms, bool)
        or isinstance(maximum_duration_ms, bool)
        or not isinstance(minimum_duration_ms, int)
        or not isinstance(maximum_duration_ms, int)
        or type(security_max_duration_ms) is not int
        or not 1 <= minimum_duration_ms <= maximum_duration_ms <= security_max_duration_ms
        or not minimum_duration_ms / 1000
        <= source_duration
        <= maximum_duration_ms / 1000
    ):
        raise ReferenceMediaNormalizationError("配对音频时长不在冻结合同范围内")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target = f"{effective_duration_seconds:.9f}"
    _run(
        [
            str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source_path), "-map", "0:a:0", "-vn", "-af",
            f"asetpts=PTS-STARTPTS,aresample={profile['audio_sample_rate_hz']}:"
            f"async=1:first_pts=0,apad,atrim=duration={target}",
            "-c:a", profile["paired_audio_codec"], str(output_path),
        ],
        timeout=profile["audio_normalize_timeout_seconds"],
        operation="配对音频时长规范化",
    )
    output_probe = probe_media(output_path, media_profile=profile)
    output_audio = next(
        (item for item in output_probe["streams"] if item.get("codec_type") == "audio"), None,
    )
    output_duration = _stream_duration(output_audio or {})
    if (
        output_duration is None
        or abs(output_duration - effective_duration_seconds)
        > profile["paired_audio_duration_tolerance_ms"] / 1000
    ):
        raise ReferenceMediaNormalizationError("配对音频与目标视频时长不同步")
    return {
        "staging_version": 2,
        "media_role": "paired_reference_audio",
        "preprocessing_adapter_key": "aligned_audio_v1",
        "paired_video_asset_id": str(paired_video_asset_id),
        "source_duration_ms": round(source_duration * 1000),
        "effective_fps": effective_fps,
        "effective_duration_ms": round(effective_duration_seconds * 1000),
        "duration_aligned": True,
        "original_asset_preserved": True,
        "verified_audio_duration_ms": round(output_duration * 1000),
        "media_processing_profile_key": REFERENCE_PROFILE_KEY,
        "media_processing_profile_sha256": media_profile_sha256(profile),
    }
