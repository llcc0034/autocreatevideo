"""One-way business cutover: clean incompatible roots before adding required FKs.

This migration does not fabricate versions, infer preparations, delete business
records, or call a provider. The reviewed maintenance closure must run first.
"""
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def require_empty_execution_roots(apps, schema_editor):
    alias = schema_editor.connection.alias
    occupied = [name for name in ("AssetGenerationJob", "GenerationAttempt")
        if apps.get_model("studio", name).objects.using(alias).exists()]
    if occupied:
        raise RuntimeError(
            "Execution-instance cutover requires reviewed cleanup of incompatible roots before schema changes: "
            + ", ".join(occupied)
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0144_execution_policy_configuration")]

    operations = [
        migrations.RunPython(require_empty_execution_roots, migrations.RunPython.noop),
        migrations.AddField(model_name="assetgenerationjob", name="execution_instance_version",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="asset_generation_jobs", to="studio.modelexecutioninstanceversion")),
        migrations.AddField(model_name="assetgenerationjob", name="parent_preparation",
            field=models.ForeignKey(blank=True, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="auxiliary_asset_jobs", to="studio.executionpreparation")),
        migrations.AddField(model_name="assetgenerationjob", name="retry_of",
            field=models.ForeignKey(blank=True, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="retries", to="studio.assetgenerationjob")),
        migrations.AddField(model_name="assetgenerationjob", name="auxiliary_purpose",
            field=models.CharField(blank=True, max_length=24)),
        migrations.AddField(model_name="assetgenerationjob", name="field_batch_key",
            field=models.CharField(blank=True, max_length=191)),
        migrations.AddField(model_name="assetgenerationjob", name="attempt_number",
            field=models.PositiveIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(1)])),
        migrations.AlterField(model_name="assetgenerationjob", name="job_type",
            field=models.CharField(choices=[("prompt", "提示词候选"), ("asset", "素材生成"), ("field", "准备字段生成")], max_length=16)),
        migrations.AlterField(model_name="assetgenerationjob", name="media_type",
            field=models.CharField(choices=[("image", "图片"), ("video", "视频"), ("audio", "音频"), ("script", "剧本"), ("text", "准备字段文本")], max_length=16)),
        migrations.AlterField(model_name="assetgenerationjob", name="status",
            field=models.CharField(choices=[("preparing", "准备请求"), ("queued", "排队中"), ("submitting", "正在提交"),
                ("submitted", "已提交"), ("running", "运行中"), ("cancel_requested", "取消中"), ("succeeded", "已成功"),
                ("failed", "已失败"), ("cancelled", "已取消"), ("uncertain", "远端状态不确定")],
                db_index=True, default="preparing", max_length=24)),
        migrations.AddConstraint(model_name="assetgenerationjob", constraint=models.CheckConstraint(condition=(
            models.Q(job_type="field", media_type="text", parent_preparation__isnull=False,
                auxiliary_purpose__in=("generate_fields", "translate_fields"), attempt_number__isnull=False,
                attempt_number__gte=1) & ~models.Q(field_batch_key="")
            | models.Q(job_type__in=("asset", "prompt"), parent_preparation__isnull=True,
                auxiliary_purpose="", field_batch_key="", attempt_number__isnull=True) & ~models.Q(media_type="text")
        ), name="ck_asset_job_auxiliary")),
        migrations.AddConstraint(model_name="assetgenerationjob", constraint=models.UniqueConstraint(
            fields=("parent_preparation", "auxiliary_purpose", "field_batch_key", "attempt_number"),
            condition=models.Q(parent_preparation__isnull=False), name="uk_asset_job_field_attempt")),
        migrations.AddConstraint(model_name="assetgenerationjob", constraint=models.CheckConstraint(
            condition=models.Q(retry_of__isnull=True) | (models.Q(parent_preparation__isnull=True, job_type__in=("asset", "prompt"))
                & ~models.Q(id=models.F("retry_of"))), name="ck_asset_job_retry_source")),
        migrations.AddConstraint(model_name="assetgenerationjob", constraint=models.UniqueConstraint(fields=("retry_of",),
            condition=models.Q(retry_of__isnull=False, status__in=("preparing", "queued", "submitting", "submitted", "running", "cancel_requested", "uncertain")),
            name="uk_asset_job_active_retry")),
        migrations.AddIndex(model_name="assetgenerationjob", index=models.Index(fields=("retry_of",),
            condition=models.Q(retry_of__isnull=False), name="idx_asset_retry_source")),
        migrations.AddField(model_name="generationattempt", name="execution_instance_version",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="generation_attempts", to="studio.modelexecutioninstanceversion")),
        migrations.AddField(model_name="generationattempt", name="preparation",
            field=models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.PROTECT,
                related_name="generation_attempts", to="studio.executionpreparation")),
        migrations.AddField(model_name="generationattempt", name="role",
            field=models.CharField(choices=[("primary", "主调用"), ("auxiliary", "准备字段调用")], max_length=16)),
        migrations.AddField(model_name="generationattempt", name="auxiliary_purpose",
            field=models.CharField(blank=True, max_length=24)),
        migrations.AddField(model_name="generationattempt", name="field_batch_key",
            field=models.CharField(blank=True, max_length=191)),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.CheckConstraint(condition=(
            models.Q(role="primary", auxiliary_purpose="", field_batch_key="")
            | models.Q(role="auxiliary", auxiliary_purpose__in=("generate_fields", "translate_fields")) & ~models.Q(field_batch_key="")
        ), name="ck_attempt_execution_role")),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.UniqueConstraint(
            fields=("preparation",), condition=models.Q(status__in=("queued", "running", "submitting", "submitted", "cancel_requested")),
            name="uk_attempt_preparation_active")),
        migrations.AddIndex(model_name="generationattempt", index=models.Index(
            fields=("preparation", "-attempt_number"), name="idx_attempt_preparation")),
    ]
