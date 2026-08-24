from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0051_settings_registry_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="assemblyjob",
            name="source_contract_version",
            field=models.PositiveSmallIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="assemblysourceitem",
            name="media_kind",
            field=models.CharField(
                choices=[("video", "视频"), ("image", "图片")],
                default="video",
                max_length=16,
            ),
        ),
    ]
