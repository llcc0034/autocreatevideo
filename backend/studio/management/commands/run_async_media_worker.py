from __future__ import annotations

import os
import math
import socket
import time

from django.core.management.base import BaseCommand, CommandError

from studio.services.async_media_queue import (
    claim_next_async_media_attempt,
    fail_unhandled_async_media_attempt,
    process_async_media_attempt,
    recover_stale_async_media_attempts,
)
from studio.services.configuration import resolve_configuration
from studio.services.execution_preparation_queue import advance_next_execution_preparation
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.worker_database import retry_worker_database_step
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
    recover_stale_deployment_smokes,
)
from studio.services.asset_generation import (
    claim_next_media_asset_job,
    fail_unhandled_asset_generation_job,
    process_media_asset_job,
    recover_stale_asset_generation_jobs,
)
from studio.models import AssetGenerationJob


class Command(BaseCommand):
    help = "Run restricted async media jobs and durable deployment smoke attempts"

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_idle_timeout_seconds",
            "runtime.asset_text_submission_grace_seconds",
        )).values
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=float(runtime["runtime.task_worker_poll_seconds"]),
        )
        parser.add_argument(
            "--recover-minutes",
            type=float,
            default=float(runtime["runtime.task_idle_timeout_seconds"]) / 60,
        )

    def handle(self, *args, **options):
        text_submission_grace_seconds = resolve_configuration(required_keys=(
            "runtime.asset_text_submission_grace_seconds",
        )).values["runtime.asset_text_submission_grace_seconds"]
        poll_seconds = float(options["poll_seconds"])
        recover_minutes = float(options["recover_minutes"])
        if any(
            not math.isfinite(value) or value <= 0
            for value in (poll_seconds, recover_minutes)
        ):
            raise CommandError("--poll-seconds 与 --recover-minutes 必须是正数")
        with WorkerHeartbeat("async-media-worker-heartbeat"):
            worker_id = f"{socket.gethostname()}:{os.getpid()}"
            recovery = recover_stale_async_media_attempts(recover_minutes)
            self.stdout.write(f"async media recovery: {recovery}")
            smoke_recovery = recover_stale_deployment_smokes(recover_minutes)
            self.stdout.write(f"deployment smoke recovery: {smoke_recovery}")
            asset_recovery = recover_stale_asset_generation_jobs(
                recover_minutes,
                text=False,
                execution_kind=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
                text_submission_grace_seconds=text_submission_grace_seconds,
            )
            self.stdout.write(f"asset generation recovery: {asset_recovery}")
            while True:
                retry_worker_database_step(advance_next_execution_preparation, wait_seconds=poll_seconds)
                attempt = retry_worker_database_step(
                    claim_next_async_media_attempt, worker_id, wait_seconds=poll_seconds,
                )
                if attempt is None:
                    asset_job = retry_worker_database_step(
                        claim_next_media_asset_job, worker_id, wait_seconds=poll_seconds,
                    )
                    if asset_job is not None:
                        try:
                            result = process_media_asset_job(asset_job)
                        except Exception:
                            result = fail_unhandled_asset_generation_job(asset_job)
                            self.stderr.write(
                                f"asset job {asset_job.id}: asset_generation_worker_internal_error",
                            )
                        self.stdout.write(f"asset job {result.id}: {result.status}/{result.error_code or '-'}")
                        if options["once"]:
                            return
                        continue
                    smoke = retry_worker_database_step(
                        claim_next_deployment_smoke, worker_id, wait_seconds=poll_seconds,
                    )
                    if smoke is not None:
                        result = process_deployment_smoke(smoke)
                        self.stdout.write(f"deployment smoke {result.id}: {result.status}")
                        if options["once"]:
                            return
                        continue
                    if options["once"]:
                        return
                    time.sleep(poll_seconds)
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
