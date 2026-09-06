import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0061_deactivate_invalid_exact_prompt_profile_bindings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflowtemplate",
            name="output_kind",
            field=models.CharField(
                choices=[("video", "视频"), ("image", "图片"), ("text", "文本")],
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="modelartifact",
            name="inventory_folder",
            field=models.CharField(
                blank=True,
                help_text="ComfyUI models 根下的受控目录；留空时按 role 使用平台默认目录。",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="modelartifact",
            name="inventory_input_name",
            field=models.CharField(
                blank=True,
                help_text="与 inventory_node_type 配套的模型文件输入名。",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="modelartifact",
            name="inventory_node_type",
            field=models.CharField(
                blank=True,
                help_text="自定义模型目录通过 object_info 枚举核对时使用的节点类型。",
                max_length=191,
            ),
        ),
        migrations.CreateModel(
            name="ComfyWorkflowSource",
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
                ("source_path", models.CharField(max_length=500)),
                ("source_path_key", models.CharField(max_length=500)),
                ("source_sha256", models.CharField(max_length=64)),
                ("source_size_bytes", models.PositiveIntegerField()),
                ("source_workflow_id", models.CharField(blank=True, max_length=191)),
                (
                    "import_kind",
                    models.CharField(
                        choices=[
                            ("template_version", "模板版本"),
                            ("execution_snapshot", "执行快照"),
                            ("ignored", "明确忽略"),
                        ],
                        max_length=24,
                    ),
                ),
                ("converted_graph_sha256", models.CharField(blank=True, max_length=64)),
                ("conversion_report", models.JSONField(blank=True, default=dict)),
                ("conversion_report_schema_version", models.PositiveIntegerField(default=1)),
                ("conversion_report_size_bytes", models.PositiveIntegerField(default=2)),
                ("conversion_report_sha256", models.CharField(max_length=64)),
                (
                    "generated_workflow",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comfy_sources",
                        to="studio.generatedworkflow",
                    ),
                ),
                (
                    "generation_task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comfy_sources",
                        to="studio.generationtask",
                    ),
                ),
                (
                    "imported_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="imported_comfy_workflow_sources",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "source_deployment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comfy_workflow_sources",
                        to="studio.modeldeployment",
                    ),
                ),
                (
                    "workflow_version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comfy_sources",
                        to="studio.workflowtemplateversion",
                    ),
                ),
            ],
            options={
                "ordering": ("source_deployment", "source_path_key", "-created_at"),
            },
        ),
        migrations.AddConstraint(
            model_name="comfyworkflowsource",
            constraint=models.UniqueConstraint(
                fields=("source_deployment", "source_path_key", "source_sha256"),
                name="unique_comfy_workflow_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="comfyworkflowsource",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        import_kind="template_version",
                        workflow_version__isnull=False,
                        generated_workflow__isnull=True,
                        generation_task__isnull=True,
                    )
                    | Q(import_kind="execution_snapshot", workflow_version__isnull=True)
                    | Q(
                        import_kind="ignored",
                        workflow_version__isnull=True,
                        generated_workflow__isnull=True,
                        generation_task__isnull=True,
                    )
                ),
                name="valid_comfy_workflow_source_target",
            ),
        ),
        migrations.AddIndex(
            model_name="comfyworkflowsource",
            index=models.Index(
                fields=["source_deployment", "source_path_key"],
                name="studio_comfy_source_path_idx",
            ),
        ),
    ]
