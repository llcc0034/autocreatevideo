from django.core.management.base import BaseCommand, CommandError
from studio import models
from studio.services.history_cleanup import HISTORY_MODELS


class Command(BaseCommand):
    help = "Read-only check that a fresh installation contains no business/execution history. Never clears data."

    def handle(self, *args, **options):
        occupied = {name: getattr(models, name)._base_manager.count() for name in sorted(HISTORY_MODELS)
            if name not in {"AccessControlAuditEvent", "SecretAuditEvent", "PlatformLifecycleAuditEvent"}}
        occupied = {name: count for name, count in occupied.items() if count}
        if occupied:
            raise CommandError("目标数据库已有业务历史，拒绝作为全新安装；未删除数据：" + str(occupied))
        self.stdout.write("Clean installation: no projects, executions, Smoke, probes or delivery history. Assets and configuration are independent.")
