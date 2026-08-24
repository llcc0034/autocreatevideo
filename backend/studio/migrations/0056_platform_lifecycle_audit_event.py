import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0055_execution_deadlines_and_jwt_boundary"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformLifecycleAuditEvent",
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
                    "action",
                    models.CharField(
                        max_length=32,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-z][a-z0-9_.-]{1,31}$",
                            ),
                        ],
                    ),
                ),
                (
                    "object_type",
                    models.CharField(
                        max_length=80,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-z][a-z0-9_.-]{1,79}$",
                            ),
                        ],
                    ),
                ),
                ("object_id", models.UUIDField()),
                (
                    "reason_code",
                    models.CharField(
                        max_length=80,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-z][a-z0-9_.-]{1,79}$",
                            ),
                        ],
                    ),
                ),
                ("before_sha256", models.CharField(blank=True, max_length=64)),
                ("after_sha256", models.CharField(blank=True, max_length=64)),
                ("before_summary", models.JSONField(blank=True, default=dict)),
                ("after_summary", models.JSONField(blank=True, default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="platform_lifecycle_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="platformlifecycleauditevent",
            index=models.Index(
                fields=["object_type", "object_id", "-created_at"],
                name="studio_plat_object_43e831_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="platformlifecycleauditevent",
            index=models.Index(
                fields=["actor", "-created_at"],
                name="studio_plat_actor_i_fa3a5f_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="platformlifecycleauditevent",
            index=models.Index(
                fields=["action", "-created_at"],
                name="studio_plat_action_4306dd_idx",
            ),
        ),
    ]
