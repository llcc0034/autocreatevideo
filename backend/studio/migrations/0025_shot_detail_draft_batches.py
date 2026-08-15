import uuid

from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0024_shot_frame_continuity_requirements"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ShotDetailDraftBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("request_id", models.UUIDField(editable=False, unique=True)),
                ("provider", models.CharField(default="local_qwen", max_length=80)),
                ("variation_note", models.TextField(blank=True)),
                ("source_manifest_hash", models.CharField(db_index=True, max_length=64)),
                ("policy_snapshot", models.JSONField(default=dict)),
                ("config_hash", models.CharField(max_length=64)),
                ("generation_status", models.CharField(choices=[("queued", "排队中"), ("running", "生成中"), ("completed", "已完成"), ("completed_with_errors", "部分完成"), ("failed", "已失败"), ("cancelled", "已取消")], db_index=True, default="queued", max_length=32)),
                ("apply_status", models.CharField(choices=[("not_started", "未应用"), ("complete", "已全部应用")], default="not_started", max_length=20)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("total_count", models.PositiveIntegerField(default=0)),
                ("pending_count", models.PositiveIntegerField(default=0)),
                ("succeeded_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("stale_count", models.PositiveIntegerField(default=0)),
                ("skipped_count", models.PositiveIntegerField(default=0)),
                ("applied_count", models.PositiveIntegerField(default=0)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shot_detail_batches", to=settings.AUTH_USER_MODEL)),
                ("episode", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shot_detail_batches", to="studio.episode")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shot_detail_batches", to="studio.seriesproject")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="ShotDetailDraftBatchItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("ordinal", models.PositiveIntegerField()),
                ("source_context_hash", models.CharField(db_index=True, max_length=64)),
                ("before_snapshot", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("pending", "待处理"), ("running", "处理中"), ("succeeded", "已生成"), ("failed", "失败"), ("stale", "源数据已变化"), ("skipped", "已跳过")], db_index=True, default="pending", max_length=20)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3, validators=[django.core.validators.MinValueValidator(1)])),
                ("next_attempt_at", models.DateTimeField(db_index=True, default=timezone.now)),
                ("worker_id", models.CharField(blank=True, max_length=160)),
                ("claim_token", models.UUIDField(blank=True, db_index=True, editable=False, null=True)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("error_message", models.TextField(blank=True)),
                ("apply_run_id", models.UUIDField(blank=True, db_index=True, editable=False, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="studio.shotdetaildraftbatch")),
                ("draft", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="batch_item", to="studio.shotdetaildraft")),
                ("shot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="detail_batch_items", to="studio.shot")),
            ],
            options={"ordering": ("batch", "ordinal")},
        ),
        migrations.AddConstraint(
            model_name="shotdetaildraftbatch",
            constraint=models.UniqueConstraint(condition=Q(("generation_status__in", ("queued", "running"))), fields=("episode",), name="unique_active_shot_detail_batch"),
        ),
        migrations.AddIndex(
            model_name="shotdetaildraftbatch",
            index=models.Index(fields=["project", "episode", "generation_status", "created_at"], name="studio_sdbatch_scope_idx"),
        ),
        migrations.AddConstraint(
            model_name="shotdetaildraftbatchitem",
            constraint=models.UniqueConstraint(fields=("batch", "shot"), name="unique_shot_detail_batch_shot"),
        ),
        migrations.AddConstraint(
            model_name="shotdetaildraftbatchitem",
            constraint=models.UniqueConstraint(fields=("batch", "ordinal"), name="unique_shot_detail_batch_ordinal"),
        ),
        migrations.AddIndex(
            model_name="shotdetaildraftbatchitem",
            index=models.Index(fields=["status", "next_attempt_at", "created_at"], name="studio_sdbitem_claim_idx"),
        ),
    ]
