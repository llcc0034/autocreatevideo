from __future__ import annotations

import math
import time
from datetime import timedelta
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from studio.models import WorkerLease
from studio.services.configuration import resolve_configuration
from studio.services.runtime_host_probe_queue import (
    ACTIVE as ACTIVE_HOST_PROBE_STATUSES,
    claim_next_runtime_host_probe,
    enqueue_due_runtime_host_probes,
    process_runtime_host_probe,
    recover_stale_runtime_host_probes,
)


LEASE_KEY = "platform-maintenance-single-flight"


def _maintenance_policy() -> dict[str, float]:
    values = resolve_configuration(required_keys=(
        "runtime.platform_maintenance_poll_seconds",
        "runtime.platform_maintenance_lease_seconds",
    )).values
    return {
        "poll_seconds": float(values["runtime.platform_maintenance_poll_seconds"]),
        "lease_seconds": float(values["runtime.platform_maintenance_lease_seconds"]),
    }


def _acquire_single_flight(holder_id: str) -> bool:
    now = timezone.now()
    lease_seconds = _maintenance_policy()["lease_seconds"]
    with transaction.atomic():
        lease, _created = WorkerLease.objects.get_or_create(key=LEASE_KEY)
        lease = WorkerLease.objects.select_for_update().get(pk=lease.pk)
        if (
            lease.holder_id
            and lease.holder_id != holder_id
            and lease.expires_at
            and lease.expires_at > now
        ):
            return False
        lease.holder_id = holder_id
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
    return True


def _renew_single_flight(holder_id: str) -> None:
    now = timezone.now()
    lease_seconds = _maintenance_policy()["lease_seconds"]
    updated = WorkerLease.objects.filter(pk=LEASE_KEY, holder_id=holder_id).update(
        expires_at=now + timedelta(seconds=lease_seconds),
        updated_at=now,
    )
    if updated != 1:
        raise RuntimeError("platform maintenance lease was lost")


def _release_single_flight(holder_id: str) -> None:
    WorkerLease.objects.filter(pk=LEASE_KEY, holder_id=holder_id).update(
        holder_id="",
        expires_at=None,
        updated_at=timezone.now(),
    )


class Command(BaseCommand):
    help = "Run recoverable platform maintenance, including durable runtime-host probes."

    def add_arguments(self, parser):
        policy = _maintenance_policy()
        parser.add_argument("--once", action="store_true", help="Run one maintenance sweep.")
        parser.add_argument("--poll", type=float, default=policy["poll_seconds"])

    def handle(self, *args, **options):
        poll_seconds = float(options["poll"])
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise CommandError("--poll 必须是正数")
        holder_id = uuid.uuid4().hex
        while True:
            if _acquire_single_flight(holder_id):
                try:
                    _renew_single_flight(holder_id)
                    recovered = recover_stale_runtime_host_probes()
                    queued = enqueue_due_runtime_host_probes()
                    processed = 0
                    while True:
                        _renew_single_flight(holder_id)
                        attempt = claim_next_runtime_host_probe(holder_id)
                        if attempt is None:
                            break
                        result = process_runtime_host_probe(
                            attempt,
                            worker_heartbeat=lambda: _renew_single_flight(holder_id),
                        )
                        processed += 1
                        self.stdout.write(
                            f"runtime-host-probe attempt={result.id} "
                            f"host={result.host_id} status={result.status}"
                        )
                        if result.status in ACTIVE_HOST_PROBE_STATUSES:
                            # A synchronous CLI probe still owns the host lease.
                            # Leave the durable attempt recoverable for the next sweep.
                            break
                    self.stdout.write(
                        f"runtime-host-probes recovered={recovered} "
                        f"queued={queued} processed={processed}"
                    )
                finally:
                    _release_single_flight(holder_id)
            else:
                self.stdout.write("maintenance sweep skipped: lease held")
            if options["once"]:
                return
            time.sleep(poll_seconds)
