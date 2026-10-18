from __future__ import annotations

import math
import os
import time

from django.core.management.base import BaseCommand, CommandError

from studio.models import AssetGenerationJob
from studio.services.asset_generation import (
    claim_next_comfy_asset_job,
    fail_unhandled_asset_generation_job,
    process_comfy_asset_job,
    reconcile_uncertain_comfy_asset_jobs,
    recover_stale_asset_generation_jobs,
)
from studio.services.local_qwen_lock import (
    LocalQwenResourceBusy,
    local_qwen_runtime_lock,
)
from studio.services.task_queue import (
    claim_next_task,
    fail_claimed_task_preflight,
    process_task,
    recover_stale_tasks,
)
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.task_distribution_recovery import (
    claim_task_distribution, process_task_distribution, recover_stale_task_distributions,
)
from studio.services.worker_database import retry_worker_database_step
from studio.services.configuration import resolve_configuration
from studio.services.execution_preparation_queue import advance_next_execution_preparation
from studio.services.workflow_smoke_queue import (
    claim_next_workflow_smoke,
    process_workflow_smoke,
    recover_stale_workflow_smokes,
)


def _claim_generation_work(worker_id):
    # Release the local runtime lock before waiting on a database conflict.
    # Once a claim succeeds, return immediately without further DB steps.
    with local_qwen_runtime_lock():
        smoke = claim_next_workflow_smoke(worker_id)
        if smoke is not None:
            return smoke, None, None
        asset = claim_next_comfy_asset_job(worker_id)
        if asset is not None:
            return None, asset, None
        return None, None, claim_next_task()


class Command(BaseCommand):
    help = "Run the database-backed ComfyUI generation worker."

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_stale_recovery_interval_seconds",
            "runtime.worker_stale_recovery_minutes",
            "runtime.asset_text_submission_grace_seconds",
            "runtime.asset_uncertain_reconcile_batch_size",
        )).values
        parser.add_argument("--once", action="store_true", help="Process at most one task then exit")
        parser.add_argument("--poll", type=float, default=runtime["runtime.task_worker_poll_seconds"])
        parser.add_argument(
            "--recover-minutes", type=float, default=None,
            help="Explicit override for the versioned stale-recovery threshold.",
        )
        parser.add_argument(
            "--startup-recover-minutes",
            type=int,
            default=None,
            help="Optional one-time stale threshold used only when the worker starts.",
        )
        parser.add_argument(
            "--recover-interval",
            type=float,
            default=runtime["runtime.task_stale_recovery_interval_seconds"],
        )

    def handle(self, *args, **options):
        with WorkerHeartbeat("generation-worker-heartbeat"):
            return self._run_worker(options)

    def _run_worker(self, options):
        worker_id = f"generation-worker-{os.getpid()}"
        configured_runtime = resolve_configuration(required_keys=(
            "runtime.worker_stale_recovery_minutes",
            "runtime.asset_text_submission_grace_seconds",
            "runtime.asset_uncertain_reconcile_batch_size",
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
        uncertain_reconcile_batch_size = configured_runtime[
            "runtime.asset_uncertain_reconcile_batch_size"
        ]
        if any(
            not math.isfinite(float(value)) or float(value) <= 0
            for value in (recovery_minutes, poll_seconds, recovery_interval)
        ):
            raise CommandError("--poll、--recover-minutes 与 --recover-interval 必须是正有限数")
        startup_recover_minutes = options["startup_recover_minutes"]
        if startup_recover_minutes is not None and startup_recover_minutes <= 0:
            raise CommandError("--startup-recover-minutes 必须是正整数")
        if startup_recover_minutes is None:
            startup_recover_minutes = recovery_minutes
        recovered = recover_stale_tasks(startup_recover_minutes)
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale tasks")
        recovered_smokes = recover_stale_workflow_smokes(startup_recover_minutes)
        if recovered_smokes:
            self.stdout.write(f"Recovered {recovered_smokes} stale workflow smoke attempts")
        recovered_assets = recover_stale_asset_generation_jobs(
            startup_recover_minutes,
            text=False,
            execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
            text_submission_grace_seconds=text_submission_grace_seconds,
        )
        if recovered_assets["released"] or recovered_assets["uncertain"]:
            self.stdout.write(f"Recovered ComfyUI asset jobs: {recovered_assets}")
        reconciled_assets = reconcile_uncertain_comfy_asset_jobs(
            limit=uncertain_reconcile_batch_size,
        )
        if reconciled_assets["resumed"] or reconciled_assets["terminal"]:
            self.stdout.write(f"Reconciled ComfyUI asset jobs: {reconciled_assets}")
        last_recovery = time.monotonic()
        recover_stale_task_distributions()
        while True:
            current = time.monotonic()
            if current - last_recovery >= recovery_interval:
                recovered = recover_stale_tasks(recovery_minutes)
                if recovered:
                    self.stdout.write(f"Recovered {recovered} stale tasks")
                recovered_smokes = recover_stale_workflow_smokes(recovery_minutes)
                if recovered_smokes:
                    self.stdout.write(
                        f"Recovered {recovered_smokes} stale workflow smoke attempts",
                    )
                recovered_assets = recover_stale_asset_generation_jobs(
                    recovery_minutes,
                    text=False,
                    execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
                    text_submission_grace_seconds=text_submission_grace_seconds,
                )
                if recovered_assets["released"] or recovered_assets["uncertain"]:
                    self.stdout.write(f"Recovered ComfyUI asset jobs: {recovered_assets}")
                reconciled_assets = reconcile_uncertain_comfy_asset_jobs(
                    limit=uncertain_reconcile_batch_size,
                )
                if reconciled_assets["resumed"] or reconciled_assets["terminal"]:
                    self.stdout.write(
                        f"Reconciled ComfyUI asset jobs: {reconciled_assets}",
                    )
                last_recovery = current
                recover_stale_task_distributions()
            # Resume original local post-processing before acquiring any GPU
            # work. No Comfy client, generation claim or remote submission.
            publication = retry_worker_database_step(claim_task_distribution, wait_seconds=poll_seconds)
            if publication is not None:
                result = process_task_distribution(publication)
                self.stdout.write(f"Distribution {result.id}: {result.distribution_state}")
                if options["once"]:
                    return
                continue
            task = None
            smoke_attempt = None
            asset_job = None
            try:
                retry_worker_database_step(advance_next_execution_preparation, wait_seconds=poll_seconds)
                # Claim while owning the same short cross-process gate used by
                # local Qwen. The claim changes the database state to RUNNING;
                # Qwen re-checks that state after acquiring the gate. Releasing
                # the file lock before the remote wait allows the configured
                # number of independent media workers to run concurrently.
                smoke_attempt, asset_job, task = retry_worker_database_step(
                    _claim_generation_work, worker_id, wait_seconds=poll_seconds,
                )
                if smoke_attempt is not None:
                    self.stdout.write(f"Processing workflow smoke {smoke_attempt.id}")
                    result = process_workflow_smoke(smoke_attempt)
                    self.stdout.write(f"Workflow smoke {result.id}: {result.status}")
                if task:
                    try:
                        self.stdout.write(
                            f"Processing {task.id} worker claim {task.attempts}",
                        )
                        result = process_task(
                            task,
                        )
                        self.stdout.write(f"Task {result.id}: {result.status}")
                    except Exception as exc:
                        # Client/config/capacity validation happens after the
                        # durable claim. Persist a terminal fact before moving
                        # on so a malformed frozen task never remains RUNNING.
                        result = fail_claimed_task_preflight(task, exc)
                        self.stderr.write(
                            f"Task {result.id} failed before execution: {result.error_message}",
                        )
                if asset_job is not None:
                    try:
                        self.stdout.write(f"Processing ComfyUI asset job {asset_job.id}")
                        result = process_comfy_asset_job(asset_job)
                    except Exception:
                        result = fail_unhandled_asset_generation_job(asset_job)
                        self.stderr.write(
                            f"ComfyUI asset job {asset_job.id}: asset_generation_worker_internal_error",
                        )
                    self.stdout.write(
                        f"ComfyUI asset job {result.id}: {result.status}/{result.error_code or '-'}",
                    )
            except LocalQwenResourceBusy:
                # A local translation/creative request owns the machine memory
                # budget.  Leave queued work untouched and retry on the normal
                # short worker poll instead of claiming a task we cannot run.
                task = None

            if (
                task is None and smoke_attempt is None and asset_job is None
                and not options["once"]
            ):
                time.sleep(poll_seconds)
            if options["once"]:
                return
