from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from studio.services.assembly_queue import (
    ASSEMBLY_WORKER_HEARTBEAT_KEY,
    claim_next_assembly,
    default_worker_id,
    process_assembly_job,
    recover_stale_assemblies,
)
from studio.services.worker_heartbeat import WorkerHeartbeat, touch_worker_heartbeat


class Command(BaseCommand):
    help = "Run the persistent database-backed FFmpeg assembly worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job then exit")
        parser.add_argument(
            "--poll",
            type=float,
            default=float(getattr(settings, "ASSEMBLY_WORKER_POLL_SECONDS", 1)),
        )
        parser.add_argument(
            "--recover-minutes",
            type=float,
            default=float(getattr(settings, "ASSEMBLY_STALE_MINUTES", 30)),
        )
        parser.add_argument(
            "--startup-recover-minutes",
            type=float,
            default=None,
            help="Optional one-time stale threshold used when the worker starts.",
        )
        parser.add_argument(
            "--recover-interval",
            type=float,
            default=float(getattr(settings, "ASSEMBLY_RECOVERY_INTERVAL_SECONDS", 60)),
        )
        parser.add_argument("--worker-id", default="", help="Auditable worker identity override")

    def handle(self, *args, **options):
        worker_id = str(options["worker_id"] or default_worker_id())[:160]
        startup_minutes = options["startup_recover_minutes"]
        if startup_minutes is None:
            startup_minutes = options["recover_minutes"]

        with WorkerHeartbeat(ASSEMBLY_WORKER_HEARTBEAT_KEY):
            recovered = recover_stale_assemblies(startup_minutes)
            recovered_total = sum(recovered.values())
            if recovered_total:
                self.stdout.write(
                    "Recovered assembly jobs: "
                    f"succeeded={recovered['succeeded']} "
                    f"requeued={recovered['requeued']} failed={recovered['failed']}"
                )
            last_recovery = time.monotonic()
            while True:
                touch_worker_heartbeat(ASSEMBLY_WORKER_HEARTBEAT_KEY)
                current = time.monotonic()
                if current - last_recovery >= max(1.0, options["recover_interval"]):
                    recovered = recover_stale_assemblies(options["recover_minutes"])
                    recovered_total = sum(recovered.values())
                    if recovered_total:
                        self.stdout.write(
                            "Recovered assembly jobs: "
                            f"succeeded={recovered['succeeded']} "
                            f"requeued={recovered['requeued']} failed={recovered['failed']}"
                        )
                    last_recovery = current

                job = claim_next_assembly(worker_id=worker_id)
                if job is not None:
                    self.stdout.write(
                        f"Processing assembly {job.id} attempt {job.attempts}/{job.max_attempts}"
                    )
                    result = process_assembly_job(job)
                    self.stdout.write(f"Assembly {result.id}: {result.status}")
                elif options["once"]:
                    return
                else:
                    time.sleep(max(0.1, options["poll"]))
                if options["once"]:
                    return
