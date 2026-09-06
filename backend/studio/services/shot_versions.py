"""Authoritative storyboard-version identity and shot ordering helpers.

Storyboard versions predate a dedicated relational model, so their stable
identity is frozen in ``Shot.metadata.creative_version_key``.  This module is
the only read-side normalizer used by version-scoped services.  Shots created
before that metadata existed belong to one deterministic initial version for
their episode; they are never treated as belonging to whichever version is
currently active.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from studio.models import Episode, Shot


MAX_SHOT_VERSION_KEY_LENGTH = 191
MIXED_SHOT_VERSION_PREFIX = "legacy-mixed:"
ORPHANED_SHOT_VERSION_PREFIX = "legacy-orphaned:"
RESERVED_SHOT_VERSION_METADATA_KEYS = (
    "creative_version_key",
    "creative_version_label",
    "creative_version_mode",
    "creative_version_generation_no",
    "creative_version_applied_at",
    "creative_version_active",
    "creative_version_shot_number",
    "creative_version_shot_count",
)


class ShotVersionResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedShotVersion:
    key: str
    label: str
    shots: tuple[Shot, ...]


def legacy_shot_version_key(episode_id) -> str:
    return f"legacy:{episode_id}:initial"


def raw_shot_version_key(shot: Shot) -> str:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    return str(metadata.get("creative_version_key") or "").strip()


def shot_version_key(shot: Shot) -> str:
    return raw_shot_version_key(shot) or legacy_shot_version_key(shot.episode_id)


def shot_version_label(shot: Shot) -> str:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    value = str(metadata.get("creative_version_label") or "").strip()
    if value:
        return value
    return "初始分镜" if not raw_shot_version_key(shot) else "分镜版本"


def shot_version_number(shot: Shot) -> int:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    try:
        value = int(metadata.get("creative_version_shot_number"))
    except (TypeError, ValueError):
        value = int(shot.number)
    return value if value > 0 else int(shot.number)


def normalize_requested_shot_version_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ShotVersionResolutionError("必须选择一个分镜版本")
    if len(key) > MAX_SHOT_VERSION_KEY_LENGTH:
        raise ShotVersionResolutionError("分镜版本标识过长")
    if key.startswith((MIXED_SHOT_VERSION_PREFIX, ORPHANED_SHOT_VERSION_PREFIX)):
        raise ShotVersionResolutionError("该历史扩写批次无法归属到一个可编辑分镜版本")
    return key


def resolve_shot_version_from_rows(
    episode: Episode,
    requested_key: str,
    rows: Iterable[Shot],
) -> ResolvedShotVersion:
    key = normalize_requested_shot_version_key(requested_key)
    selected = [shot for shot in rows if shot_version_key(shot) == key]
    if not selected:
        raise ShotVersionResolutionError("所选分镜版本不存在或已被删除")
    positions: dict[int, Shot] = {}
    for shot in selected:
        local_number = shot_version_number(shot)
        if local_number in positions:
            raise ShotVersionResolutionError("所选分镜版本包含重复的镜头序号")
        positions[local_number] = shot
    ordered = tuple(positions[number] for number in sorted(positions))
    expected = list(range(1, len(ordered) + 1))
    if sorted(positions) != expected:
        raise ShotVersionResolutionError("所选分镜版本的镜头序号不连续")
    return ResolvedShotVersion(
        key=key,
        label=shot_version_label(ordered[0]),
        shots=ordered,
    )


def resolve_episode_shot_version(
    episode: Episode,
    requested_key: str,
    *,
    for_update: bool = False,
) -> ResolvedShotVersion:
    queryset = Shot.objects.filter(episode=episode)
    if for_update:
        # Lock only Shot rows.  ``scene`` is nullable and PostgreSQL rejects a
        # blanket FOR UPDATE across the nullable side of that outer join.
        queryset = queryset.select_for_update(of=("self",))
        # Fetch the nullable scene in a separate query as well.  This keeps the
        # locking SELECT free of an outer join on PostgreSQL.
        queryset = queryset.prefetch_related("scene", "characters", "props")
    else:
        queryset = queryset.select_related("scene").prefetch_related(
            "characters", "props",
        )
    rows = list(queryset.order_by("number", "id"))
    return resolve_shot_version_from_rows(episode, requested_key, rows)


def adjacent_version_shots(shot: Shot) -> tuple[Shot | None, Shot | None]:
    resolved = resolve_episode_shot_version(shot.episode, shot_version_key(shot))
    shot_ids = [item.pk for item in resolved.shots]
    try:
        index = shot_ids.index(shot.pk)
    except ValueError as exc:  # defensive: resolver and target must agree
        raise ShotVersionResolutionError("当前镜头不属于其声明的分镜版本") from exc
    previous = resolved.shots[index - 1] if index > 0 else None
    following = resolved.shots[index + 1] if index + 1 < len(resolved.shots) else None
    return previous, following
