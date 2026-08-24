from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0036_text_generation_attempt_runtime"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="compiler_id",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="compiler_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="semantic_bindings_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="model_requirements_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="execution_contract_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="workflow_template_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="generated_workflows",
                to="studio.workflowtemplateversion",
            ),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="compiler_id",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="compiler_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="route_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="workflow_contract_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="resolved_semantics_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="asset_bindings_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generatedworkflow",
            name="model_requirements_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RemoveConstraint(
            model_name="workflowsemanticbinding",
            name="unique_workflow_semantic_binding",
        ),
        migrations.AddConstraint(
            model_name="workflowsemanticbinding",
            constraint=models.UniqueConstraint(
                fields=("workflow_version", "node_id", "input_name"),
                name="unique_workflow_semantic_target",
            ),
        ),
    ]
