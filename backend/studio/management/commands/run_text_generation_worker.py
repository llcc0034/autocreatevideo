from __future__ import annotations

import os
import time

from django.core.management.base import BaseCommand

from studio.models import ProviderProfile
from studio.services.local_qwen_lock import LocalQwenResourceBusy, local_qwen_runtime_lock
from studio.services.text_generation_queue import (
    claim_next_text_attempt,
    fail_unhandled_text_attempt,
    process_text_attempt,
    recover_stale_text_attempts,
    release_text_attempt_claim,
)
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.configuration import resolve_configuration


class Command(BaseCommand):
    help = "Run the independent database-backed text generation worker."

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_stale_recovery_interval_seconds",
        )).values
        parser.add_argument("--once", action="store_true", help="Process at most one claim then exit")
        parser.add_argument("--poll", type=float, default=runtime["runtime.task_worker_poll_seconds"])
        parser.add_argument("--recover-minutes", type=int, default=30)
        parser.add_argument("--startup-recover-minutes", type=int, default=None)
        parser.add_argument(
            "--recover-interval",
            type=float,
            default=runtime["runtime.task_stale_recovery_interval_seconds"],
        )

    def handle(self, *args, **options):
        with WorkerHeartbeat("text-generation-worker-heartbeat"):
            return self._run_worker(options)

    def _run_worker(self, options):
        startup_minutes = options["startup_recover_minutes"]
        if startup_minutes is None:
            startup_minutes = options["recover_minutes"]
        recovered = recover_stale_text_attempts(startup_minutes)
        if recovered["requeued"] or recovered["uncertain"]:
            self.stdout.write(f"Recovered text attempts: {recovered}")
        worker_id = f"text-generation-worker-{os.getpid()}"
        last_recovery = time.monotonic()
        while True:
            current = time.monotonic()
            if current - last_recovery >= max(1.0, options["recover_interval"]):
                recovered = recover_stale_text_attempts(options["recover_minutes"])
                if recovered["requeued"] or recovered["uncertain"]:
                    self.stdout.write(f"Recovered text attempts: {recovered}")
                last_recovery = current

            attempt = claim_next_text_attempt(worker_id)
            result = None
            if attempt is not None:
                driver_type = (
                    attempt.model_deployment.profile_version.provider.driver_type
                    if attempt.model_deployment_id else ""
                )
                try:
                    if driver_type == ProviderProfile.DriverType.LOCAL_QWEN:
                        with local_qwen_runtime_lock():
                            result = process_text_attempt(attempt)
                    else:
                        result = process_text_attempt(attempt)
                except LocalQwenResourceBusy:
                    release_text_attempt_claim(attempt)
                    result = None
                except Exception as exc:
                    fail_unhandled_text_attempt(attempt, exc)
                    self.stderr.write(f"Text attempt {attempt.id} failed in worker isolation: {exc}")
                    attempt.refresh_from_db()
                    result = attempt
                if result is not None:
                    self.stdout.write(f"Text attempt {result.id}: {result.status}")

            if options["once"]:
                return
            if attempt is None or result is None:
                time.sleep(max(0.1, options["poll"]))
