"""Process-level database heartbeats for long-running background workers."""
from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from studio.models import WorkerLease


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
        configured = interval if interval is not None else getattr(
            settings, "WORKER_HEARTBEAT_INTERVAL_SECONDS", 5,
        )
        self.interval = max(1.0, float(configured))
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
        self._touch()
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
