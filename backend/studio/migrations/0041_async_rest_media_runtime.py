import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.core.validators import MinValueValidator


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0040_configuration_activation_and_recommendations"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationattempt",
            name="submission_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="remote_status",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="last_polled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="next_poll_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="poll_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="cancel_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="cancel_submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="generationattempt",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "排队中"),
                    ("running", "执行中"),
                    ("submitting", "正在提交"),
                    ("submitted", "已提交"),
                    ("cancel_requested", "取消中"),
                    ("succeeded", "成功"),
                    ("failed", "失败"),
                    ("cancelled", "已取消"),
                ],
                default="queued",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="generationattemptresolution",
            name="outcome",
            field=models.CharField(
                choices=[
                    ("confirmed_not_submitted", "确认未提交"),
                    ("confirmed_remote_submitted", "确认远端已提交"),
                    ("confirmed_remote_failed", "确认远端失败"),
                    ("actual_cost_confirmed", "确认实际费用"),
                    ("cancel_requested_ignore_late", "取消并忽略晚到结果"),
                    ("late_result_ignored", "已忽略晚到结果"),
                    ("known_response_rejected", "已知响应未通过校验"),
                ],
                max_length=40,
            ),
        ),
        migrations.AddIndex(
            model_name="generationattempt",
            index=models.Index(fields=["status", "next_poll_at"], name="studio_gene_status_93317d_idx"),
        ),
        migrations.CreateModel(
            name="ModelDeploymentCapabilitySnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("revision", models.PositiveIntegerField(validators=[MinValueValidator(1)])),
                ("probe_kind", models.CharField(choices=[("health", "健康探测"), ("smoke", "真实烟测")], max_length=16)),
                ("status", models.CharField(choices=[("passed", "通过"), ("failed", "失败")], max_length=16)),
                ("target_sha256", models.CharField(max_length=64)),
                ("provider_config_sha256", models.CharField(max_length=64)),
                ("deployment_config_sha256", models.CharField(max_length=64)),
                ("credential_binding_sha256", models.CharField(blank=True, max_length=64)),
                ("operations", models.JSONField(default=list)),
                ("operations_schema_version", models.PositiveIntegerField(default=1)),
                ("operations_size_bytes", models.PositiveIntegerField(default=2)),
                ("operations_sha256", models.CharField(max_length=64)),
                ("evidence", models.JSONField(default=dict)),
                ("evidence_schema_version", models.PositiveIntegerField(default=1)),
                ("evidence_size_bytes", models.PositiveIntegerField(default=2)),
                ("evidence_sha256", models.CharField(max_length=64)),
                ("captured_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("deployment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="capability_snapshots", to="studio.modeldeployment")),
            ],
            options={"ordering": ("deployment", "-revision")},
        ),
        migrations.AddConstraint(
            model_name="modeldeploymentcapabilitysnapshot",
            constraint=models.UniqueConstraint(fields=("deployment", "revision"), name="unique_deployment_capability_revision"),
        ),
        migrations.AddIndex(
            model_name="modeldeploymentcapabilitysnapshot",
            index=models.Index(fields=["deployment", "probe_kind", "status", "expires_at"], name="studio_mode_deploym_cbcad2_idx"),
        ),
    ]
