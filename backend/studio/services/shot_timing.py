"""Authored episode timing, never a source-media trim instruction."""
from collections.abc import Mapping


def episode_interval_duration(start_ms, end_ms, *, duration_ms=None):
    """A null pair means duration-only planning; never invent authored timestamps."""
    if start_ms is None and end_ms is None:
        return duration_ms
    if (type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms):
        raise ValueError("剧集内起止时间须成对填写非负整数毫秒，且结束晚于开始")
    elapsed = end_ms - start_ms
    if duration_ms is not None and (type(duration_ms) is not int or duration_ms != elapsed):
        raise ValueError("镜头时长必须等于剧集内结束时间减开始时间；请调整区间，或清空起止时间后只设置时长")
    return elapsed


def validate_sequential_episode_timing(shots):
    """Sequential assembly cannot silently close authored gaps or overlaps."""
    cursor = 0
    for shot in shots:
        row = shot if isinstance(shot, Mapping) else {key: getattr(shot, key) for key in
            ("number", "duration_ms", "episode_start_ms", "episode_end_ms")}
        duration = episode_interval_duration(row["episode_start_ms"], row["episode_end_ms"],
            duration_ms=row["duration_ms"])
        if type(duration) is not int or duration < 1:
            raise ValueError(f"镜头 {row['number']} 缺少有效时长")
        if row["episode_start_ms"] is not None and row["episode_start_ms"] != cursor:
            raise ValueError(f"镜头 {row['number']} 的剧集起点为 {row['episode_start_ms']}ms，"
                f"但前序镜头结束于 {cursor}ms；请明确调整分镜空隙或重叠，不会自动补片或挪动时间")
        cursor += duration
