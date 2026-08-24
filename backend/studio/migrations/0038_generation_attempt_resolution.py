from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("studio", "0037_workflow_semantic_compiler"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationattempt",
            name="ignore_late_result",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="GenerationAttemptResolution",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("revision", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("outcome", models.CharField(choices=[("confirmed_not_submitted", "确认未提交"), ("confirmed_remote_failed", "确认远端失败"), ("cancel_requested_ignore_late", "取消并忽略晚到结果"), ("late_result_ignored", "已忽略晚到结果"), ("known_response_rejected", "已知响应未通过校验")], max_length=40)),
                ("provider_request_id", models.CharField(blank=True, max_length=191)),
                ("evidence", models.JSONField(default=dict)),
                ("evidence_schema_version", models.PositiveIntegerField(default=1)),
                ("evidence_size_bytes", models.PositiveIntegerField(default=2)),
                ("evidence_sha256", models.CharField(max_length=64)),
                ("note", models.TextField(blank=True)),
                ("attempt", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="resolutions", to="studio.generationattempt")),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="generation_attempt_resolutions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("attempt", "revision")},
        ),
        migrations.AddConstraint(
            model_name="generationattemptresolution",
            constraint=models.UniqueConstraint(fields=("attempt", "revision"), name="unique_generation_attempt_resolution"),
        ),
    ]
