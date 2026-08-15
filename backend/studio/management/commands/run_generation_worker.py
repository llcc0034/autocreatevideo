from __future__ import annotations

import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from studio.services.comfyui import ComfyUIClient
from studio.services.local_qwen_lock import (
    LocalQwenResourceBusy,
    local_qwen_runtime_lock,
)
from studio.services.task_queue import claim_next_task, process_task, recover_stale_tasks
from studio.services.shot_detail_batches import (
    claim_next_shot_detail_item,
    process_shot_detail_item,
    recover_stale_shot_detail_items,
)
from studio.services.worker_heartbeat import WorkerHeartbeat


class Command(BaseCommand):
    help = "Run the database-backed ComfyUI generation worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one task then exit")
        parser.add_argument("--poll", type=float, default=settings.TASK_WORKER_POLL_SECONDS)
        parser.add_argument("--wait-timeout", type=float, default=settings.TASK_GENERATION_TIMEOUT_SECONDS)
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
            default=settings.TASK_STALE_RECOVERY_INTERVAL_SECONDS,
        )

    def handle(self, *args, **options):
        with WorkerHeartbeat("generation-worker-heartbeat"):
            return self._run_worker(options)

    def _run_worker(self, options):
        client = ComfyUIClient()
        startup_recover_minutes = options["startup_recover_minutes"]
        if startup_recover_minutes is None:
            startup_recover_minutes = options["recover_minutes"]
        recovered = recover_stale_tasks(startup_recover_minutes, client=client)
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale tasks")
        recovered_details = recover_stale_shot_detail_items()
        if recovered_details:
            self.stdout.write(f"Recovered {recovered_details} stale shot-detail items")
        worker_id = f"generation-worker-{os.getpid()}"
        last_recovery = time.monotonic()
        while True:
            current = time.monotonic()
            if current - last_recovery >= max(1.0, options["recover_interval"]):
                recovered = recover_stale_tasks(options["recover_minutes"], client=client)
                if recovered:
                    self.stdout.write(f"Recovered {recovered} stale tasks")
                recovered_details = recover_stale_shot_detail_items()
                if recovered_details:
                    self.stdout.write(
                        f"Recovered {recovered_details} stale shot-detail items",
                    )
                last_recovery = current
            task = None
            try:
                # Claim only after owning the same cross-process resource lease
                # used by local Qwen.  Keeping it through the terminal ComfyUI
                # result closes the race where a queued GPU task could otherwise
                # start after Qwen had already passed its database busy check.
                with local_qwen_runtime_lock():
                    task = claim_next_task()
                    if task:
                        self.stdout.write(
                            f"Processing {task.id} attempt {task.attempts}/{task.max_attempts}",
                        )
                        result = process_task(
                            task, client=client, wait_timeout=options["wait_timeout"],
                        )
                        self.stdout.write(f"Task {result.id}: {result.status}")
            except LocalQwenResourceBusy:
                # A local translation/creative request owns the machine memory
                # budget.  Leave queued work untouched and retry on the normal
                # short worker poll instead of claiming a task we cannot run.
                task = None

            detail_item = None
            if task is None:
                detail_item = claim_next_shot_detail_item(worker_id)
                if detail_item:
                    self.stdout.write(
                        "Processing shot-detail item "
                        f"{detail_item.id} attempt {detail_item.attempts}/{detail_item.max_attempts}",
                    )
                    result = process_shot_detail_item(detail_item)
                    self.stdout.write(
                        f"Shot-detail item {result.id}: {result.status}",
                    )

            if task is None and detail_item is None and not options["once"]:
                time.sleep(max(0.1, options["poll"]))
            if options["once"]:
                return
