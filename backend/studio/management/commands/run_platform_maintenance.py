from __future__ import annotations

import time
from datetime import timedelta
import uuid

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from studio.models import WorkerLease
from studio.services.runtime_host_probe_queue import (
    ACTIVE as ACTIVE_HOST_PROBE_STATUSES,
    claim_next_runtime_host_probe,
    enqueue_due_runtime_host_probes,
    process_runtime_host_probe,
    recover_stale_runtime_host_probes,
)


LEASE_KEY = "platform-maintenance-single-flight"
LEASE_SECONDS = 180


def _acquire_single_flight(holder_id: str) -> bool:
    now = timezone.now()
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
        lease.expires_at = now + timedelta(seconds=LEASE_SECONDS)
        lease.save(update_fields=("holder_id", "expires_at", "updated_at"))
    return True


def _renew_single_flight(holder_id: str) -> None:
    now = timezone.now()
    updated = WorkerLease.objects.filter(pk=LEASE_KEY, holder_id=holder_id).update(
        expires_at=now + timedelta(seconds=LEASE_SECONDS),
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
        parser.add_argument("--once", action="store_true", help="Run one maintenance sweep.")
        parser.add_argument("--poll", type=float, default=60.0)

    def handle(self, *args, **options):
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
            time.sleep(max(1.0, min(float(options["poll"]), 60.0)))
