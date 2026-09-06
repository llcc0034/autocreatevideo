from __future__ import annotations

from django.core.management.base import BaseCommand
from studio.services.assembly_queue import recover_stale_assemblies
from studio.services.configuration import resolve_configuration


class Command(BaseCommand):
    help = "Recover interrupted persistent assembly jobs after an application or worker restart."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reason",
            default="Application process restarted while FFmpeg assembly was active",
            help="Auditable reason stored on each recovered AssemblyJob.",
        )
        parser.add_argument(
            "--stale-minutes",
            type=float,
            default=None,
            help=(
                "Only recover jobs whose heartbeat is at least this old. "
                "Omit to use runtime.task_idle_timeout_seconds; pass 0 only "
                "when the caller knows every active claim was interrupted."
            ),
        )

    def handle(self, *args, **options):
        reason = str(options["reason"]).strip() or "Assembly process was interrupted"
        stale_minutes = options["stale_minutes"]
        if stale_minutes is None:
            idle_timeout_seconds = float(resolve_configuration(required_keys=(
                "runtime.task_idle_timeout_seconds",
            )).values["runtime.task_idle_timeout_seconds"])
            stale_minutes = idle_timeout_seconds / 60
        recovered = recover_stale_assemblies(
            stale_minutes,
            reason=reason,
        )
        total = sum(recovered.values())
        self.stdout.write(self.style.SUCCESS(
            f"Recovered {total} interrupted assembly job(s): "
            f"succeeded={recovered['succeeded']}, "
            f"requeued={recovered['requeued']}, failed={recovered['failed']}."
        ))
