from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand

from studio.models import RuntimeHost
from studio.services.runtime_host_probe_queue import (
    claim_runtime_host_lease as _claim_host,
    release_runtime_host_lease as _release_host_lease,
    renew_runtime_host_lease as _renew_host_lease,
)
from studio.services.runtime_platform import probe_runtime_host


class Command(BaseCommand):
    help = "Probe registered runtime hosts and persist immutable capability snapshots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one complete probe sweep, then exit (the command is non-daemon).",
        )
        parser.add_argument(
            "--due",
            action="store_true",
            help="Only probe hosts whose latest snapshot is absent or expired.",
        )

    def handle(self, *args, **options):
        hosts = RuntimeHost.objects.exclude(status=RuntimeHost.Status.DISABLED).order_by("key")
        holder_id = uuid.uuid4().hex
        probed = 0
        skipped = 0
        for host in hosts:
            claimed, _reason = _claim_host(
                host,
                holder_id=holder_id,
                due_only=bool(options["due"]),
            )
            if not claimed:
                skipped += 1
                continue
            lease_key = f"runtime-host-probe:{host.id}"
            try:
                snapshot = probe_runtime_host(
                    host,
                    lease_renew=lambda key=lease_key: _renew_host_lease(
                        key,
                        holder_id=holder_id,
                    ),
                )
            finally:
                _release_host_lease(lease_key, holder_id=holder_id)
            probed += 1
            self.stdout.write(
                f"{host.key}: snapshot={snapshot.id} status={snapshot.probe_status}"
            )
        self.stdout.write(self.style.SUCCESS(f"probed={probed} skipped={skipped}"))
