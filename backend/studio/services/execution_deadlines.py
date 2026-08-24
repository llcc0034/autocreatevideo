from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math

from django.core.exceptions import ValidationError
from django.utils import timezone


@dataclass(frozen=True)
class DeadlineState:
    started_at: object
    hard_deadline_at: object
    idle_deadline_at: object
    last_meaningful_progress_at: object
    progress_marker: str


class ExecutionDeadlineExceeded(TimeoutError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"execution {kind} deadline exceeded")


def validate_deadline_seconds(*, idle_seconds: float, hard_seconds: float) -> tuple[float, float]:
    try:
        idle = float(idle_seconds)
        hard = float(hard_seconds)
    except (TypeError, ValueError) as exc:
        raise ValidationError("执行时限必须是数字") from exc
    if not math.isfinite(idle) or not math.isfinite(hard) or idle <= 0 or hard <= 0:
        raise ValidationError("执行时限必须是有限正数")
    if idle > hard:
        raise ValidationError("idle timeout 不能大于 hard timeout")
    return idle, hard


def initial_deadlines(
    *, idle_seconds: float, hard_seconds: float, now=None, marker: str = "started",
) -> DeadlineState:
    idle, hard = validate_deadline_seconds(
        idle_seconds=idle_seconds,
        hard_seconds=hard_seconds,
    )
    observed = now or timezone.now()
    return DeadlineState(
        started_at=observed,
        hard_deadline_at=observed + timedelta(seconds=hard),
        idle_deadline_at=observed + timedelta(seconds=idle),
        last_meaningful_progress_at=observed,
        progress_marker=str(marker)[:191],
    )


def expired_kind(subject, *, now=None) -> str | None:
    """Hard always wins; persisted absolute timestamps are never recomputed."""
    observed = now or timezone.now()
    hard = getattr(subject, "hard_deadline_at", None)
    idle = getattr(subject, "idle_deadline_at", None)
    if hard is not None and observed >= hard:
        return "hard"
    if idle is not None and observed >= idle:
        return "idle"
    return None


def require_not_expired(subject, *, now=None) -> None:
    kind = expired_kind(subject, now=now)
    if kind:
        raise ExecutionDeadlineExceeded(kind)


def meaningful_deadline_updates(
    subject,
    *,
    marker: str,
    idle_seconds: float,
    now=None,
) -> dict:
    """Renew idle only for a strictly new phase/node/progress marker.

    Callers must not feed heartbeats, HTTP success, connection events or a
    repeated remote ``running`` state into this function.
    """
    normalized = str(marker or "")[:191]
    if not normalized or normalized == str(getattr(subject, "progress_marker", "") or ""):
        return {}
    observed = now or timezone.now()
    hard = getattr(subject, "hard_deadline_at", None)
    idle = observed + timedelta(seconds=float(idle_seconds))
    if hard is not None and idle > hard:
        idle = hard
    return {
        "progress_marker": normalized,
        "last_meaningful_progress_at": observed,
        "idle_deadline_at": idle,
    }
