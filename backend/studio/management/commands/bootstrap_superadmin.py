from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from studio.models import AccessControlAuditEvent
from studio.services.access_control import ROLE_SUPER_ADMIN, create_managed_user


User = get_user_model()


class Command(BaseCommand):
    help = "Create the one-time initial super administrator and print its temporary password once."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="superadmin")
        parser.add_argument("--email", default="")
        parser.add_argument("--if-missing", action="store_true")

    def handle(self, *args, **options):
        existing = User.objects.filter(is_superuser=True)
        if existing.exists():
            healthy = any(user.is_active and user.has_usable_password() for user in existing)
            if options["if_missing"] and healthy:
                self.stdout.write("superadmin=existing")
                return
            if options["if_missing"]:
                raise CommandError(
                    "检测到超级管理员记录，但没有可登录的活跃账号；为避免静默接管，"
                    "请通过受控恢复流程处理"
                )
            raise CommandError("已有超级管理员；该命令不会创建或重置第二个初始账号")
        username = str(options["username"] or "").strip()
        if not username:
            raise CommandError("username 不能为空")
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError("指定 username 已存在；不会覆盖现有账号")
        user, password = create_managed_user(
            actor=None,
            username=username,
            email=str(options["email"] or "").strip(),
            role=ROLE_SUPER_ADMIN,
            reason="initial_superadmin_bootstrap",
            audit_action=AccessControlAuditEvent.Action.BOOTSTRAP_SUPERUSER,
        )
        self.stdout.write(f"username={user.username}")
        self.stdout.write(f"temporary_password={password}")
        self.stdout.write("must_change_password=true")
