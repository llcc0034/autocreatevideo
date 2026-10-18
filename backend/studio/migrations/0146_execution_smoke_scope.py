"""Scope is assigned to new attempts only, never inferred for historical rows."""
from django.db import migrations, models
import django.db.models.deletion


def require_reviewed_smoke_cleanup(apps, schema_editor):
    occupied = [name for name in ("WorkflowSmokeAttempt", "ModelDeploymentSmokeAttempt")
        if apps.get_model("studio", name).objects.using(schema_editor.connection.alias).exists()]
    if occupied:
        raise RuntimeError("Execution smoke cutover requires reviewed cleanup before assigning scope: " + ", ".join(occupied))


class Migration(migrations.Migration):
    dependencies = [("studio", "0145_execution_task_links")]

    operations = [
        migrations.RunPython(require_reviewed_smoke_cleanup, migrations.RunPython.noop),
        *[operation for name, related, constraint in (
            ("workflowsmokeattempt", "workflow_smoke_attempts", "ck_workflow_smoke_scope"),
            ("modeldeploymentsmokeattempt", "deployment_smoke_attempts", "ck_deployment_smoke_scope"),
        ) for operation in (
            migrations.AddField(model_name=name, name="scope", field=models.CharField(
                choices=[("resource", "基础资源验收"), ("instance", "执行实例组合验收")], default="resource", max_length=16)),
            migrations.AddField(model_name=name, name="execution_instance_version", field=models.ForeignKey(
                blank=True, null=True, db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name=related, to="studio.modelexecutioninstanceversion")),
            migrations.AddConstraint(model_name=name, constraint=models.CheckConstraint(condition=(
                models.Q(scope="resource", execution_instance_version__isnull=True)
                | models.Q(scope="instance", execution_instance_version__isnull=False)), name=constraint)),
        )],
        migrations.RemoveConstraint(model_name="workflowsmokeattempt", name="one_active_workflow_smoke"),
        migrations.AddConstraint(model_name="workflowsmokeattempt", constraint=models.UniqueConstraint(
            fields=("workflow_version", "deployment"), condition=models.Q(status__in=("preparing", "queued", "submitting", "running", "cancel_requested")),
            name="one_active_workflow_smoke")),
        migrations.RemoveConstraint(model_name="modeldeploymentsmokeattempt", name="one_active_deployment_smoke"),
        migrations.AddConstraint(model_name="modeldeploymentsmokeattempt", constraint=models.UniqueConstraint(
            fields=("deployment", "operation"), condition=models.Q(status__in=("preparing", "queued", "submitting", "submitted", "cancel_requested")),
            name="one_active_deployment_smoke")),
        migrations.AlterField(model_name="workflowsmokeattempt", name="status", field=models.CharField(choices=[
            ("preparing", "准备请求"), ("queued", "排队中"), ("submitting", "正在提交"), ("running", "执行中"), ("cancel_requested", "取消中"),
            ("succeeded", "成功"), ("failed", "失败"), ("cancelled", "已取消"), ("uncertain", "远端状态不确定")], default="queued", max_length=24)),
        migrations.AlterField(model_name="modeldeploymentsmokeattempt", name="status", field=models.CharField(choices=[
            ("preparing", "准备请求"), ("queued", "排队中"), ("submitting", "正在提交"), ("submitted", "已提交"), ("cancel_requested", "取消中"),
            ("succeeded", "成功"), ("failed", "失败"), ("cancelled", "已取消"), ("uncertain", "远端状态不确定")], max_length=24)),
        migrations.AlterField(model_name="modeldeploymentsmokeattempt", name="submission_started_at", field=models.DateTimeField(blank=True, null=True)),
        *[migrations.AlterField(model_name="workflowsmokeattempt", name=name,
            field=models.DateTimeField(blank=True, null=True, **({"db_index": True} if name in {"hard_deadline_at", "idle_deadline_at"} else {})))
            for name in ("deadline_started_at", "hard_deadline_at", "idle_deadline_at", "last_meaningful_progress_at")],
        migrations.AddField(model_name="modeldeploymentsmokeattempt", name="result", field=models.JSONField(default=dict, blank=True)),
        *[operation for name, index_name in (("workflowsmokeattempt", "idx_workflow_instance_smoke"), ("modeldeploymentsmokeattempt", "idx_deploy_instance_smoke"))
            for operation in (
                migrations.AddField(model_name=name, name="result_schema_version", field=models.PositiveIntegerField(default=1)),
                migrations.AddField(model_name=name, name="result_size_bytes", field=models.PositiveIntegerField(default=2)),
                migrations.AddField(model_name=name, name="result_sha256", field=models.CharField(max_length=64, blank=True)),
                migrations.AddField(model_name=name, name="evidence_expires_at", field=models.DateTimeField(null=True, blank=True)),
                migrations.AddIndex(model_name=name, index=models.Index(fields=("execution_instance_version", "-finished_at", "-id"),
                    condition=models.Q(execution_instance_version__isnull=False), name=index_name)),
            )],
    ]
