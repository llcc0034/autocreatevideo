from __future__ import annotations

from django.core.management.base import BaseCommand
from studio.services.assembly_queue import recover_stale_assemblies


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
            default=0,
            help="Only recover RUNNING jobs whose heartbeat is at least this old.",
        )

    def handle(self, *args, **options):
        reason = str(options["reason"]).strip() or "Assembly process was interrupted"
        recovered = recover_stale_assemblies(
            options["stale_minutes"],
            reason=reason,
        )
        total = sum(recovered.values())
        self.stdout.write(self.style.SUCCESS(
            f"Recovered {total} interrupted assembly job(s): "
            f"succeeded={recovered['succeeded']}, "
            f"requeued={recovered['requeued']}, failed={recovered['failed']}."
        ))
