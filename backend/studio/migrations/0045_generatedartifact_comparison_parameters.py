from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0044_protect_versioned_workflow_template"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatedartifact",
            name="comparison_parameters",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
