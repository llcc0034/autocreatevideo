from __future__ import annotations

import os
import socket
import time

from django.core.management.base import BaseCommand

from studio.services.async_media_queue import (
    claim_next_async_media_attempt,
    fail_unhandled_async_media_attempt,
    process_async_media_attempt,
    recover_stale_async_media_attempts,
)
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
    recover_stale_deployment_smokes,
)


class Command(BaseCommand):
    help = "Run restricted async media jobs and durable deployment smoke attempts"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=1.0)
        parser.add_argument("--recover-minutes", type=int, default=10)

    def handle(self, *args, **options):
        with WorkerHeartbeat("async-media-worker-heartbeat"):
            worker_id = f"{socket.gethostname()}:{os.getpid()}"
            recovery = recover_stale_async_media_attempts(options["recover_minutes"])
            self.stdout.write(f"async media recovery: {recovery}")
            smoke_recovery = recover_stale_deployment_smokes(options["recover_minutes"])
            self.stdout.write(f"deployment smoke recovery: {smoke_recovery}")
            while True:
                attempt = claim_next_async_media_attempt(worker_id)
                if attempt is None:
                    smoke = claim_next_deployment_smoke(worker_id)
                    if smoke is not None:
                        result = process_deployment_smoke(smoke)
                        self.stdout.write(f"deployment smoke {result.id}: {result.status}")
                        if options["once"]:
                            return
                        continue
                    if options["once"]:
                        return
                    time.sleep(max(0.1, float(options["poll_seconds"])))
                    continue
                try:
                    result = process_async_media_attempt(attempt)
                except Exception:
                    result = fail_unhandled_async_media_attempt(attempt)
                    self.stderr.write(
                        f"attempt {attempt.id}: async_worker_internal_error",
                    )
                self.stdout.write(f"attempt {result.id}: {result.status}/{result.error_code or '-'}")
                if options["once"]:
                    return
