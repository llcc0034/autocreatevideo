"""Short, owned pause of automatic checks while an administrator reviews cleanup.

The existing WorkerLease row is the only pause fact. This is not a cancellation
or a second job queue: already claimed work must finish through its own writer.
"""
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection, models
from django.utils import timezone
from rest_framework.exceptions import APIException

from studio.models import WorkerLease
from studio.services.registry_reference_lock import registry_reference_write_guard


PAUSE_KEY = "history-cleanup-background-checks"
PAUSE_MINUTES = 5


class HistoryCleanupPauseConflict(APIException):
    status_code = 409
    default_code = "history_cleanup_pause_conflict"


def _active(lease):
    return bool(lease.holder_id and lease.expires_at and lease.expires_at > timezone.now())


def _holder(actor, token, include_maintenance=False):
    return f"{actor.pk}:{token}" + (":maintenance" if include_maintenance else "")


def background_pause_status():
    """Read the existing lease only; never create, extend or reveal its token."""
    now = timezone.now()
    lease = WorkerLease.objects.filter(pk=PAUSE_KEY).values("holder_id", "expires_at").first()
    active = bool(lease and lease["holder_id"] and lease["expires_at"] and lease["expires_at"] > now)
    return {"active": active, "expires_at": lease["expires_at"] if active else None, "server_time": now,
        "include_maintenance": bool(active and lease["holder_id"].endswith(":maintenance"))}


@contextmanager
def locked_background_check_pause():
    # Consistent root lock order for pause, scheduled enqueue and prep claim.
    # SQLite needs a real write before reading across worker processes.
    with registry_reference_write_guard():
        WorkerLease.objects.get_or_create(key=PAUSE_KEY)
        if connection.vendor == "sqlite":
            WorkerLease.objects.filter(pk=PAUSE_KEY).update(holder_id=models.F("holder_id"))
        lease = WorkerLease.objects.select_for_update().get(pk=PAUSE_KEY)
        yield lease


@contextmanager
def background_check_gate(*, maintenance=False):
    with locked_background_check_pause() as lease:
        yield _active(lease) and (not maintenance or lease.holder_id.endswith(":maintenance"))


def pause_background_checks(*, actor, token, include_maintenance=False):
    with locked_background_check_pause() as lease:
        holder = _holder(actor, token, include_maintenance)
        if _active(lease) and lease.holder_id != holder:
            raise HistoryCleanupPauseConflict("另一清理窗口正在暂停后台检查，请关闭原窗口或等待自动恢复。")
        # Replaying an unknown POST with the same token does not extend it.
        if not _active(lease):
            lease.holder_id = holder
            lease.expires_at = timezone.now() + timedelta(minutes=PAUSE_MINUTES)
            lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
        return {"token": str(token), "expires_at": lease.expires_at, "include_maintenance": include_maintenance}


def require_background_pause(*, actor, token, include_maintenance=False):
    lease = WorkerLease.objects.filter(pk=PAUSE_KEY).first()
    if lease is None or not _active(lease) or lease.holder_id != _holder(actor, token, include_maintenance):
        raise HistoryCleanupPauseConflict("后台检查暂停已到期或不属于当前窗口，请重新暂停并预览；未删除任何记录。")


def resume_background_checks(*, actor, token):
    with locked_background_check_pause() as lease:
        if lease.holder_id in {_holder(actor, token), _holder(actor, token, True)}:
            lease.holder_id = ""
            lease.expires_at = None
            lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
        elif _active(lease):
            raise HistoryCleanupPauseConflict("不能恢复其他清理窗口持有的后台检查暂停。")
