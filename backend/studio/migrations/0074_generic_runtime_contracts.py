from django.db import migrations, models
import django.db.models.deletion


def clear_retired_deferred_requests(apps, schema_editor):
    del schema_editor
    apps.get_model("studio", "DeferredGenerationRequest").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0073_conditioning_mode_generic_fields")]

    operations = [
        migrations.RunPython(
            clear_retired_deferred_requests,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="deferredgenerationrequest",
            name="target_template",
        ),
        migrations.RemoveField(
            model_name="deferredgenerationrequest",
            name="target_template_hash",
        ),
        migrations.AddField(
            model_name="deferredgenerationrequest",
            name="target_workflow_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="deferredgenerationrequest",
            name="target_workflow_snapshot_sha256",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="deferredgenerationrequest",
            name="target_workflow_version",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deferred_generation_requests",
                to="studio.workflowtemplateversion",
            ),
        ),
        migrations.AlterField(
            model_name="deferredgenerationrequest",
            name="target_workflow_version",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deferred_generation_requests",
                to="studio.workflowtemplateversion",
            ),
        ),
        migrations.AlterField(
            model_name="seriesproject",
            name="target_model",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AlterField(
            model_name="promptartifact",
            name="model",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AlterField(
            model_name="workflowtemplate",
            name="model",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AlterField(
            model_name="generationtask",
            name="backend",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AlterField(
            model_name="generationtask",
            name="model_profile",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AlterField(
            model_name="workflowsmokeattempt",
            name="fixture_values_schema_version",
            field=models.PositiveIntegerField(default=2),
        ),
    ]
