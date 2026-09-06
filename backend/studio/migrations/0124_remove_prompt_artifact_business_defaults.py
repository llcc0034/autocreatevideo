from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0123_configurable_project_and_shot_creation_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="promptartifact",
            name="is_multi_shot",
            field=models.BooleanField(),
        ),
        migrations.AlterField(
            model_name="promptartifact",
            name="include_audio",
            field=models.BooleanField(),
        ),
    ]
