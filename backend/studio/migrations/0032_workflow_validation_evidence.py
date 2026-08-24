from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0031_bootstrap_platform_catalog"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="validated_deployment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="validated_workflow_versions",
                to="studio.modeldeployment",
            ),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="validation_evidence",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="validation_evidence_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="smoke_test_evidence",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="workflowtemplateversion",
            name="smoke_test_evidence_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
