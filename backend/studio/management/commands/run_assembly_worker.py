from __future__ import annotations

import math
import time

from django.core.management.base import BaseCommand, CommandError

from studio.services.assembly_queue import (
    ASSEMBLY_WORKER_HEARTBEAT_KEY,
    claim_next_assembly,
    default_worker_id,
    process_assembly_job,
    recover_stale_assemblies,
)
from studio.services.configuration import resolve_configuration
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.worker_database import retry_worker_database_step


class Command(BaseCommand):
    help = "Run the persistent database-backed FFmpeg assembly worker."

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_idle_timeout_seconds",
            "runtime.task_stale_recovery_interval_seconds",
        )).values
        parser.add_argument("--once", action="store_true", help="Process at most one job then exit")
        parser.add_argument(
            "--poll",
            type=float,
            default=float(runtime["runtime.task_worker_poll_seconds"]),
        )
        parser.add_argument(
            "--recover-minutes",
            type=float,
            default=float(runtime["runtime.task_idle_timeout_seconds"]) / 60,
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
            default=float(runtime["runtime.task_stale_recovery_interval_seconds"]),
        )
        parser.add_argument("--worker-id", default="", help="Auditable worker identity override")

    def handle(self, *args, **options):
        poll_seconds = float(options["poll"])
        recover_minutes = float(options["recover_minutes"])
        recover_interval = float(options["recover_interval"])
        if any(
            not math.isfinite(value) or value <= 0
            for value in (poll_seconds, recover_minutes, recover_interval)
        ):
            raise CommandError(
                "--poll、--recover-minutes 与 --recover-interval 必须是正数"
            )
        worker_id = str(options["worker_id"] or default_worker_id())[:160]
        startup_minutes = options["startup_recover_minutes"]
        if startup_minutes is None:
            startup_minutes = recover_minutes
        else:
            startup_minutes = float(startup_minutes)
            if not math.isfinite(startup_minutes) or startup_minutes <= 0:
                raise CommandError("--startup-recover-minutes 必须是正数")

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
                current = time.monotonic()
                if current - last_recovery >= recover_interval:
                    recovered = recover_stale_assemblies(recover_minutes)
                    recovered_total = sum(recovered.values())
                    if recovered_total:
                        self.stdout.write(
                            "Recovered assembly jobs: "
                            f"succeeded={recovered['succeeded']} "
                            f"requeued={recovered['requeued']} failed={recovered['failed']}"
                        )
                    last_recovery = current

                job = retry_worker_database_step(
                    claim_next_assembly, worker_id=worker_id, wait_seconds=poll_seconds,
                )
                if job is not None:
                    self.stdout.write(
                        f"Processing assembly {job.id} attempt {job.attempts}/{job.max_attempts}"
                    )
                    result = process_assembly_job(job)
                    self.stdout.write(f"Assembly {result.id}: {result.status}")
                elif options["once"]:
                    return
                else:
                    time.sleep(poll_seconds)
                if options["once"]:
                    return
