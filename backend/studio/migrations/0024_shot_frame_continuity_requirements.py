from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0023_seriesproject_cover_asset"),
    ]

    operations = [
        migrations.AddField(
            model_name="shot",
            name="requires_first_frame_continuity",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="shot",
            name="requires_last_frame_continuity",
            field=models.BooleanField(default=False),
        ),
    ]
