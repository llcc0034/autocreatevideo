"""Retry only rolled-back, database-only worker steps, never provider execution."""
from __future__ import annotations

import logging
import math
import sqlite3
import time

from django.db import OperationalError, close_old_connections, connection


logger = logging.getLogger(__name__)


def is_sqlite_lock_conflict(exc: OperationalError) -> bool:
    if connection.vendor != "sqlite":
        return False
    cause = exc.__cause__
    code = getattr(cause, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    # Older SQLite bindings may not expose error codes. Do not treat general
    # connection errors, I/O failures or malformed queries as lock contention.
    message = str(exc).lower()
    return message in {"database is locked", "database table is locked"} or (
        message.startswith("database table is locked: ")
    )


def retry_worker_database_step(action, *args, wait_seconds: float, **kwargs):
    """Caller must supply a DB-only claim/recovery operation safe to repeat.

    Uses the worker's configured poll interval. Unknown database errors and
    failures inside an outer transaction propagate; provider calls must stay
    outside this wrapper so a persistence failure never resubmits generation.
    """
    if not math.isfinite(wait_seconds) or wait_seconds <= 0:
        raise ValueError("database retry wait must be positive and finite")
    while True:
        try:
            return action(*args, **kwargs)
        except OperationalError as exc:
            if connection.in_atomic_block or not is_sqlite_lock_conflict(exc):
                raise
            logger.warning("Worker database lock conflict; retrying %s after %.3fs",
                           getattr(action, "__name__", "database step"), wait_seconds)
            close_old_connections()
            time.sleep(wait_seconds)
