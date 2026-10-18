from __future__ import annotations

import math
import os
import time

from django.core.management.base import BaseCommand, CommandError

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
from studio.services.worker_database import retry_worker_database_step
from studio.services.configuration import resolve_configuration
from studio.services.execution_preparation_queue import advance_next_execution_preparation
from studio.services.asset_generation import (
    claim_next_text_asset_job,
    fail_unhandled_asset_generation_job,
    process_text_asset_job,
    recover_stale_asset_generation_jobs,
)


class Command(BaseCommand):
    help = "Run the independent database-backed text generation worker."

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_stale_recovery_interval_seconds",
            "runtime.worker_stale_recovery_minutes",
            "runtime.asset_text_submission_grace_seconds",
        )).values
        parser.add_argument("--once", action="store_true", help="Process at most one claim then exit")
        parser.add_argument("--poll", type=float, default=runtime["runtime.task_worker_poll_seconds"])
        parser.add_argument(
            "--recover-minutes", type=float, default=None,
            help="Explicit override for the versioned stale-recovery threshold.",
        )
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
        configured_runtime = resolve_configuration(required_keys=(
            "runtime.worker_stale_recovery_minutes",
            "runtime.asset_text_submission_grace_seconds",
        )).values
        recovery_minutes = (
            options["recover_minutes"]
            if options["recover_minutes"] is not None
            else configured_runtime["runtime.worker_stale_recovery_minutes"]
        )
        poll_seconds = float(options["poll"])
        recovery_interval = float(options["recover_interval"])
        text_submission_grace_seconds = configured_runtime[
            "runtime.asset_text_submission_grace_seconds"
        ]
        if any(
            not math.isfinite(float(value)) or float(value) <= 0
            for value in (recovery_minutes, poll_seconds, recovery_interval)
        ):
            raise CommandError("--poll、--recover-minutes 与 --recover-interval 必须是正有限数")
        startup_minutes = options["startup_recover_minutes"]
        if startup_minutes is not None and startup_minutes <= 0:
            raise CommandError("--startup-recover-minutes 必须是正整数")
        if startup_minutes is None:
            startup_minutes = recovery_minutes
        recovered = recover_stale_text_attempts(startup_minutes)
        if recovered["requeued"] or recovered["uncertain"]:
            self.stdout.write(f"Recovered text attempts: {recovered}")
        asset_recovery = recover_stale_asset_generation_jobs(
            startup_minutes,
            text=True,
            text_submission_grace_seconds=text_submission_grace_seconds,
        )
        if asset_recovery["released"] or asset_recovery["uncertain"]:
            self.stdout.write(f"Recovered asset generation jobs: {asset_recovery}")
        worker_id = f"text-generation-worker-{os.getpid()}"
        last_recovery = time.monotonic()
        while True:
            current = time.monotonic()
            if current - last_recovery >= recovery_interval:
                recovered = recover_stale_text_attempts(recovery_minutes)
                if recovered["requeued"] or recovered["uncertain"]:
                    self.stdout.write(f"Recovered text attempts: {recovered}")
                asset_recovery = recover_stale_asset_generation_jobs(
                    recovery_minutes,
                    text=True,
                    text_submission_grace_seconds=text_submission_grace_seconds,
                )
                if asset_recovery["released"] or asset_recovery["uncertain"]:
                    self.stdout.write(f"Recovered asset generation jobs: {asset_recovery}")
                last_recovery = current

            retry_worker_database_step(advance_next_execution_preparation, wait_seconds=poll_seconds)
            attempt = retry_worker_database_step(
                claim_next_text_attempt, worker_id, wait_seconds=poll_seconds,
            )
            asset_job = None if attempt is not None else retry_worker_database_step(
                claim_next_text_asset_job, worker_id, wait_seconds=poll_seconds,
            )
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
            elif asset_job is not None:
                driver_type = asset_job.model_deployment.provider.driver_type
                try:
                    if driver_type == ProviderProfile.DriverType.LOCAL_QWEN:
                        with local_qwen_runtime_lock():
                            result = process_text_asset_job(asset_job)
                    else:
                        result = process_text_asset_job(asset_job)
                except LocalQwenResourceBusy:
                    asset_job.claim_token = None
                    asset_job.worker_id = ""
                    asset_job.claimed_at = None
                    asset_job.save(update_fields=(
                        "claim_token", "worker_id", "claimed_at", "updated_at",
                    ))
                    result = None
                except Exception as exc:
                    result = fail_unhandled_asset_generation_job(asset_job)
                    self.stderr.write(
                        f"Asset text job {asset_job.id} failed in worker isolation: {exc}",
                    )
                if result is not None:
                    self.stdout.write(f"Asset text job {result.id}: {result.status}")

            if options["once"]:
                return
            if (attempt is None and asset_job is None) or result is None:
                time.sleep(poll_seconds)
