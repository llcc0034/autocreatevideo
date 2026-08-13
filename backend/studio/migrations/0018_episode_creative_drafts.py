import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0017_prompt_field_translations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EpisodeCreativeDraft",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("draft", "待审核"), ("confirmed", "已确认待应用"), ("applied", "已应用"), ("superseded", "已被新版本替代"), ("failed", "生成失败")], default="draft", max_length=24)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("generation_no", models.PositiveIntegerField(default=1)),
                ("context_hash", models.CharField(db_index=True, max_length=64)),
                ("context_snapshot", models.JSONField(default=dict)),
                ("input_settings", models.JSONField(default=dict)),
                ("creativity", models.CharField(choices=[("conservative", "保守"), ("balanced", "均衡"), ("bold", "大胆")], default="balanced", max_length=24)),
                ("variation_note", models.TextField(blank=True)),
                ("draft_data", models.JSONField(blank=True, default=dict)),
                ("validation", models.JSONField(blank=True, default=dict)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("error_message", models.TextField(blank=True)),
                ("provider", models.CharField(default="local_qwen", max_length=80)),
                ("model", models.CharField(blank=True, max_length=200)),
                ("runtime_metadata", models.JSONField(blank=True, default=dict)),
                ("revision_history", models.JSONField(blank=True, default=list)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("applied_shot_ids", models.JSONField(blank=True, default=list)),
                ("applied_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="applied_episode_creative_drafts", to=settings.AUTH_USER_MODEL)),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_episode_creative_drafts", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="episode_creative_drafts", to=settings.AUTH_USER_MODEL)),
                ("episode", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="creative_drafts", to="studio.episode")),
                ("parent_draft", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="regenerations", to="studio.episodecreativedraft")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="creative_drafts", to="studio.seriesproject")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="episodecreativedraft",
            constraint=models.UniqueConstraint(fields=("episode", "generation_no"), name="unique_creative_generation_per_episode"),
        ),
        migrations.AddIndex(
            model_name="episodecreativedraft",
            index=models.Index(fields=["project", "episode", "status"], name="studio_ecdraft_scope_idx"),
        ),
    ]
