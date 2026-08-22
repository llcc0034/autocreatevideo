import uuid

from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0025_shot_detail_draft_batches"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DeferredGenerationRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source_output_role", models.CharField(default="primary_video", editable=False, max_length=32)),
                ("frame_slot", models.CharField(choices=[("first_frame", "首帧"), ("last_frame", "尾帧")], max_length=20)),
                ("target_template_hash", models.CharField(max_length=64)),
                ("idempotency_key", models.CharField(max_length=120)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("target_input_fingerprint", models.CharField(max_length=64)),
                ("status", models.CharField(choices=[("preparing_prompt", "正在准备提示词"), ("waiting_upstream", "等待上游视频"), ("extracting", "正在提取末帧"), ("materializing", "正在物化正式任务"), ("materialized", "已物化"), ("failed", "已失败"), ("stale", "目标输入已过期"), ("cancelled", "已取消")], default="preparing_prompt", max_length=24)),
                ("provisional_prompt_text", models.TextField()),
                ("provisional_negative_prompt", models.TextField(blank=True)),
                ("provisional_prompt_sha256", models.CharField(max_length=64)),
                ("resolved_source_output_index", models.PositiveIntegerField(blank=True, null=True)),
                ("source_artifact_sha256", models.CharField(blank=True, max_length=64)),
                ("source_manifest_sha256", models.CharField(blank=True, max_length=64)),
                ("priority", models.SmallIntegerField(default=0)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3, validators=[django.core.validators.MinValueValidator(1)])),
                ("next_attempt_at", models.DateTimeField(default=timezone.now)),
                ("extended_generation_confirmed", models.BooleanField(default=False)),
                ("worker_id", models.CharField(blank=True, max_length=120)),
                ("claim_token", models.CharField(blank=True, max_length=64)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("derived_asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="derived_deferred_generation_requests", to="studio.asset")),
                ("materialized_task", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="deferred_generation_request", to="studio.generationtask")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deferred_generation_requests", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deferred_generation_requests", to="studio.seriesproject")),
                ("source_task", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="downstream_deferred_requests", to="studio.generationtask")),
                ("target_shot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="deferred_generation_requests", to="studio.shot")),
                ("target_template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="deferred_generation_requests", to="studio.workflowtemplate")),
            ],
            options={"ordering": ("-priority", "created_at")},
        ),
        migrations.AddConstraint(
            model_name="deferredgenerationrequest",
            constraint=models.UniqueConstraint(fields=("project", "idempotency_key"), name="uk_deferred_project_idem"),
        ),
        migrations.AddConstraint(
            model_name="deferredgenerationrequest",
            constraint=models.UniqueConstraint(condition=Q(("status__in", ("preparing_prompt", "waiting_upstream", "extracting", "materializing"))), fields=("target_shot", "frame_slot"), name="uk_deferred_active_slot"),
        ),
        migrations.AddConstraint(
            model_name="deferredgenerationrequest",
            constraint=models.CheckConstraint(condition=Q(("attempts__lte", models.F("max_attempts"))), name="ck_deferred_attempts_lte_max"),
        ),
        migrations.AddConstraint(
            model_name="deferredgenerationrequest",
            constraint=models.CheckConstraint(condition=Q(Q(("materialized_task__isnull", False), ("status", "materialized")), Q(Q(("status", "materialized"), _negated=True), ("materialized_task__isnull", True)), _connector="OR"), name="ck_deferred_materialized_task"),
        ),
        migrations.AddIndex(
            model_name="deferredgenerationrequest",
            index=models.Index(fields=["status", "next_attempt_at", "created_at"], name="idx_deferred_due"),
        ),
        migrations.AddIndex(
            model_name="deferredgenerationrequest",
            index=models.Index(fields=["source_task", "status"], name="idx_deferred_source_status"),
        ),
    ]
