"""Process-level database heartbeats for long-running background workers."""
from __future__ import annotations

import logging
import math
import threading

from django.db import OperationalError, close_old_connections
from django.utils import timezone

from studio.models import WorkerLease
from studio.services.configuration import resolve_configuration
from studio.services.worker_database import is_sqlite_lock_conflict


logger = logging.getLogger(__name__)


def touch_worker_heartbeat(key: str) -> None:
    if not key or len(key) > 80:
        raise ValueError("worker heartbeat key must contain 1-80 characters")
    WorkerLease.objects.get_or_create(key=key)
    WorkerLease.objects.filter(pk=key).update(updated_at=timezone.now())


class WorkerHeartbeat:
    """Keep a worker observable even while its main thread blocks on GPU/FFmpeg."""

    def __init__(self, key: str, interval: float | None = None):
        self.key = key
        configured = interval
        if configured is None:
            configured = resolve_configuration(
                required_keys=("runtime.task_heartbeat_seconds",),
            ).values["runtime.task_heartbeat_seconds"]
        if (
            type(configured) not in {int, float}
            or not math.isfinite(float(configured))
            or float(configured) <= 0
        ):
            raise ValueError("worker heartbeat interval must be a positive finite number")
        self.interval = float(configured)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _touch(self) -> None:
        close_old_connections()
        try:
            touch_worker_heartbeat(self.key)
        finally:
            close_old_connections()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._touch()
            except Exception:
                logger.exception("Failed to persist worker heartbeat %s", self.key)

    def __enter__(self):
        try:
            self._touch()
        except OperationalError as exc:
            if not is_sqlite_lock_conflict(exc):
                raise
            # The background loop will retry at the configured interval. Keep
            # the heartbeat missing/stale until an actual write succeeds.
            logger.warning("Worker initial heartbeat delayed by database lock: %s", self.key)
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self.key}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)
        close_old_connections()
        return False
