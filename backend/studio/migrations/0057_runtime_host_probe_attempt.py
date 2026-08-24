import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0056_platform_lifecycle_audit_event"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RuntimeHostProbeAttempt",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "trigger",
                    models.CharField(
                        choices=[
                            ("manual", "管理员手动"),
                            ("scheduled", "平台维护"),
                        ],
                        default="manual",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "排队中"),
                            ("running", "执行中"),
                            ("cancel_requested", "取消中"),
                            ("succeeded", "成功"),
                            ("failed", "失败"),
                            ("cancelled", "已取消"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=24,
                    ),
                ),
                (
                    "request_token",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "claim_token",
                    models.UUIDField(
                        blank=True,
                        db_index=True,
                        editable=False,
                        null=True,
                    ),
                ),
                ("worker_id", models.CharField(blank=True, max_length=160)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                (
                    "progress_marker",
                    models.CharField(default="phase:queued", max_length=191),
                ),
                ("cancel_requested_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                ("error_message", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="runtime_host_probe_attempts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "host",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="probe_attempts",
                        to="studio.runtimehost",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="runtimehostprobeattempt",
            constraint=models.UniqueConstraint(
                condition=Q(status__in=("queued", "running", "cancel_requested")),
                fields=("host",),
                name="one_active_runtime_host_probe",
            ),
        ),
        migrations.AddIndex(
            model_name="runtimehostprobeattempt",
            index=models.Index(
                fields=["host", "status", "-created_at"],
                name="studio_hp_host_st_39d0_idx",
            ),
        ),
        migrations.AddField(
            model_name="hostcapabilitysnapshot",
            name="probe_attempt",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="final_snapshot",
                to="studio.runtimehostprobeattempt",
            ),
        ),
    ]
