import hashlib
import json
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.core.validators


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def backfill_activation_and_stage_config(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    RuntimeHost = apps.get_model("studio", "RuntimeHost")
    StageRun = apps.get_model("studio", "StageRun")
    ConfigRevision.objects.filter(status="published").update(
        activation_state="active",
        activated_at=models.F("published_at"),
    )
    size, digest = _contract({})
    StageRun.objects.filter(config_snapshot_sha256="").update(
        config_snapshot={},
        config_snapshot_schema_version=1,
        config_snapshot_size_bytes=size,
        config_snapshot_sha256=digest,
    )
    metadata = {
        "workflow.max_graph_bytes": ("bytes", ["workflow"]),
        "workflow.smoke_timeout_seconds": ("seconds", ["workflow", "runtime"]),
        "artifact.max_bytes": ("bytes", ["artifact", "production"]),
        "script.max_upload_bytes": ("bytes", ["script"]),
        "production.default_shot_duration_ms": ("milliseconds", ["production", "storyboard"]),
        "production.default_multi_shot": ("", ["production", "storyboard"]),
        "production.default_include_audio": ("", ["production", "storyboard"]),
        "production.default_first_frame_continuity": ("", ["production", "storyboard"]),
        "production.default_last_frame_continuity": ("", ["production", "storyboard"]),
        "production.task_max_attempts": ("attempts", ["production", "task"]),
        "production.max_generation_tasks": ("tasks", ["production", "task"]),
        "production.allow_external_calls": ("", ["production", "provider"]),
        "runtime.capability_ttl_seconds": ("seconds", ["runtime", "recommendation"]),
        "recommendation.vram_tiers": ("bytes", ["runtime", "recommendation"]),
    }
    for key, (unit, modules) in metadata.items():
        ConfigDefinition.objects.filter(key=key).update(
            unit=unit,
            impact_modules=modules,
        )
    ConfigDefinition.objects.filter(key__in=(
        "production.default_shot_duration_ms",
        "production.default_multi_shot",
        "production.default_include_audio",
        "production.default_first_frame_continuity",
        "production.default_last_frame_continuity",
        "production.task_max_attempts",
    )).update(allowed_default_sources="model,workflow")
    RuntimeHost.objects.get_or_create(key="local", defaults={"name": "Local runtime host"})


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0039_demote_legacy_workflow_versions"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="configrevision",
            name="one_published_system_config",
        ),
        migrations.RemoveConstraint(
            model_name="configrevision",
            name="one_published_project_config",
        ),
        migrations.RemoveConstraint(
            model_name="configrevision",
            name="one_published_run_config",
        ),
        migrations.AddField(
            model_name="configdefinition",
            name="allowed_default_sources",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="configdefinition",
            name="impact_modules",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="configdefinition",
            name="restart_required",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="configdefinition",
            name="unit",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="configrevision",
            name="activated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="configrevision",
            name="activation_error",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="configrevision",
            name="activation_state",
            field=models.CharField(
                choices=[
                    ("inactive", "未激活"),
                    ("pending_activation", "等待重启激活"),
                    ("active", "已激活"),
                    ("failed", "激活失败"),
                ],
                default="inactive",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="configrevision",
            name="based_on_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="derived_revisions",
                to="studio.configrevision",
            ),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="config_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="config_snapshot_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="config_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="config_snapshot_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.CreateModel(
            name="RecommendationCandidate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("capability_key", models.CharField(max_length=100)),
                ("status", models.CharField(choices=[("recommended", "推荐"), ("compatible", "兼容"), ("smoke_test_required", "需要烟测"), ("unavailable", "不可用")], max_length=24)),
                ("rank", models.PositiveIntegerField(blank=True, null=True)),
                ("reason_code", models.CharField(max_length=100)),
                ("suggested_values", models.JSONField(blank=True, default=dict)),
                ("suggested_values_schema_version", models.PositiveIntegerField(default=1)),
                ("suggested_values_size_bytes", models.PositiveIntegerField(default=2)),
                ("suggested_values_sha256", models.CharField(max_length=64)),
                ("deployment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_candidates", to="studio.modeldeployment")),
                ("recommendation_snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="candidates", to="studio.recommendationsnapshot")),
                ("workflow_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_candidates", to="studio.workflowtemplateversion")),
            ],
            options={"ordering": ("recommendation_snapshot", "rank", "deployment__key")},
        ),
        migrations.CreateModel(
            name="RecommendationDecision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("decision", models.CharField(choices=[("accept", "接受"), ("override", "人工覆盖"), ("reject", "拒绝")], max_length=16)),
                ("scope_type", models.CharField(choices=[("system", "系统"), ("project", "项目"), ("run", "运行")], max_length=16)),
                ("override_values", models.JSONField(blank=True, default=dict)),
                ("override_values_schema_version", models.PositiveIntegerField(default=1)),
                ("override_values_size_bytes", models.PositiveIntegerField(default=2)),
                ("override_values_sha256", models.CharField(max_length=64)),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("candidate", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="decisions", to="studio.recommendationcandidate")),
                ("config_revision", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_decisions", to="studio.configrevision")),
                ("decided_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_decisions", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_decisions", to="studio.seriesproject")),
                ("recipe_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_decisions", to="studio.productionrecipeversion")),
                ("recommendation_snapshot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="decisions", to="studio.recommendationsnapshot")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.RunPython(backfill_activation_and_stage_config, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="configrevision",
            constraint=models.UniqueConstraint(condition=models.Q(("activation_state", "active"), ("scope_type", "system")), fields=("scope_type",), name="one_active_system_config"),
        ),
        migrations.AddConstraint(
            model_name="configrevision",
            constraint=models.UniqueConstraint(condition=models.Q(("activation_state", "active"), ("scope_type", "project")), fields=("project",), name="one_active_project_config"),
        ),
        migrations.AddConstraint(
            model_name="configrevision",
            constraint=models.UniqueConstraint(condition=models.Q(("activation_state", "active"), ("scope_type", "run")), fields=("run",), name="one_active_run_config"),
        ),
        migrations.AddConstraint(
            model_name="recommendationcandidate",
            constraint=models.UniqueConstraint(fields=("recommendation_snapshot", "capability_key", "deployment", "workflow_version"), name="unique_recommendation_candidate"),
        ),
        migrations.AddConstraint(
            model_name="recommendationdecision",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("project__isnull", True), ("scope_type", "system")), models.Q(("project__isnull", False), ("scope_type", "project")), _connector="OR"), name="recommendation_decision_scope_consistent"),
        ),
    ]
