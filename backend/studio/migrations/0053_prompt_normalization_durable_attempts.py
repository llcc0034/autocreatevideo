import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0052_assemblysourceitem_media_kind"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="promptfieldtranslation",
            name="origin_generation_attempt",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="prompt_field_translations",
                to="studio.generationattempt",
            ),
        ),
        migrations.CreateModel(
            name="ModelDeploymentSmokeAttempt",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("operation", models.CharField(max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("submitting", "正在提交"),
                            ("succeeded", "成功"),
                            ("failed", "失败"),
                            ("uncertain", "远端状态不确定"),
                        ],
                        max_length=16,
                    ),
                ),
                ("request_sha256", models.CharField(max_length=64)),
                ("target_sha256", models.CharField(max_length=64)),
                (
                    "estimated_cost",
                    models.DecimalField(blank=True, decimal_places=6, max_digits=14, null=True),
                ),
                ("cost_currency", models.CharField(blank=True, max_length=12)),
                ("provider_request_id_sha256", models.CharField(blank=True, max_length=64)),
                (
                    "submission_started_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "deployment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="smoke_attempts",
                        to="studio.modeldeployment",
                    ),
                ),
                (
                    "final_snapshot",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="smoke_attempt",
                        to="studio.modeldeploymentcapabilitysnapshot",
                    ),
                ),
            ],
            options={"ordering": ("deployment", "-created_at")},
        ),
        migrations.CreateModel(
            name="ModelDeploymentSmokeAttemptResolution",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "revision",
                    models.PositiveIntegerField(
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("confirmed_not_submitted", "确认未提交"),
                            ("confirmed_remote_submitted", "确认远端已提交"),
                            ("confirmed_remote_failed", "确认远端失败"),
                        ],
                        max_length=40,
                    ),
                ),
                ("provider_request_id_sha256", models.CharField(blank=True, max_length=64)),
                ("evidence", models.JSONField(default=dict)),
                ("evidence_schema_version", models.PositiveIntegerField(default=1)),
                ("evidence_size_bytes", models.PositiveIntegerField(default=2)),
                ("evidence_sha256", models.CharField(max_length=64)),
                ("note", models.TextField(blank=True)),
                (
                    "attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="resolutions",
                        to="studio.modeldeploymentsmokeattempt",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deployment_smoke_attempt_resolutions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("attempt", "revision")},
        ),
        migrations.AddIndex(
            model_name="modeldeploymentsmokeattempt",
            index=models.Index(
                fields=["deployment", "operation", "status", "created_at"],
                name="studio_smoke_unresolved_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="modeldeploymentsmokeattemptresolution",
            constraint=models.UniqueConstraint(
                fields=("attempt", "revision"),
                name="unique_deployment_smoke_resolution",
            ),
        ),
    ]
