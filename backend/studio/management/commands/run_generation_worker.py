from __future__ import annotations

import os
import time

from django.core.management.base import BaseCommand

from studio.models import GenerationTask
from studio.services.comfyui import ComfyUIClient
from studio.services.local_qwen_lock import (
    LocalQwenResourceBusy,
    local_qwen_runtime_lock,
)
from studio.services.task_queue import (
    claim_next_task,
    comfy_client_for_task,
    fail_claimed_task_preflight,
    process_task,
    recover_stale_tasks,
)
from studio.services.deferred_generation import (
    claim_next_deferred_request,
    ensure_next_first_frame_deferred_for_task,
    process_deferred_request,
    recover_stale_deferred_requests,
)
from studio.services.worker_heartbeat import WorkerHeartbeat
from studio.services.configuration import resolve_configuration
from studio.services.workflow_smoke_queue import (
    claim_next_workflow_smoke,
    process_workflow_smoke,
    recover_stale_workflow_smokes,
)


class Command(BaseCommand):
    help = "Run the database-backed ComfyUI generation worker."

    def add_arguments(self, parser):
        runtime = resolve_configuration(required_keys=(
            "runtime.task_worker_poll_seconds",
            "runtime.task_stale_recovery_interval_seconds",
        )).values
        parser.add_argument("--once", action="store_true", help="Process at most one task then exit")
        parser.add_argument("--poll", type=float, default=runtime["runtime.task_worker_poll_seconds"])
        parser.add_argument("--recover-minutes", type=int, default=30)
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
        client = ComfyUIClient()
        worker_id = f"generation-worker-{os.getpid()}"
        startup_recover_minutes = options["startup_recover_minutes"]
        if startup_recover_minutes is None:
            startup_recover_minutes = options["recover_minutes"]
        recovered = recover_stale_tasks(startup_recover_minutes, client=client)
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale tasks")
        recovered_deferred = recover_stale_deferred_requests(startup_recover_minutes)
        if recovered_deferred:
            self.stdout.write(f"Recovered {recovered_deferred} stale deferred requests")
        recovered_smokes = recover_stale_workflow_smokes(startup_recover_minutes)
        if recovered_smokes:
            self.stdout.write(f"Recovered {recovered_smokes} stale workflow smoke attempts")
        last_recovery = time.monotonic()
        while True:
            current = time.monotonic()
            if current - last_recovery >= max(1.0, options["recover_interval"]):
                recovered = recover_stale_tasks(options["recover_minutes"], client=client)
                if recovered:
                    self.stdout.write(f"Recovered {recovered} stale tasks")
                recovered_deferred = recover_stale_deferred_requests(options["recover_minutes"])
                if recovered_deferred:
                    self.stdout.write(
                        f"Recovered {recovered_deferred} stale deferred requests",
                    )
                recovered_smokes = recover_stale_workflow_smokes(options["recover_minutes"])
                if recovered_smokes:
                    self.stdout.write(
                        f"Recovered {recovered_smokes} stale workflow smoke attempts",
                    )
                last_recovery = current
            task = None
            smoke_attempt = None
            try:
                # Claim while owning the same short cross-process gate used by
                # local Qwen. The claim changes the database state to RUNNING;
                # Qwen re-checks that state after acquiring the gate. Releasing
                # the file lock before the remote wait allows the configured
                # number of independent media workers to run concurrently.
                with local_qwen_runtime_lock():
                    smoke_attempt = claim_next_workflow_smoke(worker_id)
                    if smoke_attempt is None:
                        task = claim_next_task()
                if smoke_attempt is not None:
                    self.stdout.write(f"Processing workflow smoke {smoke_attempt.id}")
                    result = process_workflow_smoke(smoke_attempt)
                    self.stdout.write(f"Workflow smoke {result.id}: {result.status}")
                if task:
                    try:
                        self.stdout.write(
                            f"Processing {task.id} attempt {task.attempts}/{task.max_attempts}",
                        )
                        result = process_task(
                            task,
                            client=comfy_client_for_task(task, fallback=client),
                        )
                        self.stdout.write(f"Task {result.id}: {result.status}")
                        if result.status == GenerationTask.Status.SUCCEEDED:
                            try:
                                created = ensure_next_first_frame_deferred_for_task(result)
                            except Exception as exc:
                                self.stderr.write(
                                    "Deferred continuity registration failed for "
                                    f"task {result.id}: {exc}",
                                )
                            else:
                                if created:
                                    deferred_request, is_new = created
                                    verb = "Created" if is_new else "Reused"
                                    self.stdout.write(
                                        f"{verb} deferred request {deferred_request.id} "
                                        f"for task {result.id}",
                                    )
                    except Exception as exc:
                        # Client/config/capacity validation happens after the
                        # durable claim. Persist a terminal fact before moving
                        # on so a malformed frozen task never remains RUNNING.
                        result = fail_claimed_task_preflight(task, exc)
                        self.stderr.write(
                            f"Task {result.id} failed before execution: {result.error_message}",
                        )
            except LocalQwenResourceBusy:
                # A local translation/creative request owns the machine memory
                # budget.  Leave queued work untouched and retry on the normal
                # short worker poll instead of claiming a task we cannot run.
                task = None

            deferred_request = None
            if task is None and smoke_attempt is None:
                deferred_request = claim_next_deferred_request(worker_id)
                if deferred_request:
                    self.stdout.write(
                        f"Processing deferred request {deferred_request.id} "
                        f"attempt {deferred_request.attempts}/{deferred_request.max_attempts}",
                    )
                    result = process_deferred_request(deferred_request)
                    self.stdout.write(
                        f"Deferred request {result.id}: {result.status}",
                    )

            if task is None and smoke_attempt is None and deferred_request is None and not options["once"]:
                time.sleep(max(0.1, options["poll"]))
            if options["once"]:
                return
