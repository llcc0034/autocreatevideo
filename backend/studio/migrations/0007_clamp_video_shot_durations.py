import django.core.validators
from django.db import migrations, models


MIN_VIDEO_DURATION_MS = 5000
MAX_VIDEO_DURATION_MS = 15000


def clamp_video_shot_durations(apps, schema_editor):
    SeriesProject = apps.get_model("studio", "SeriesProject")
    Shot = apps.get_model("studio", "Shot")

    SeriesProject.objects.filter(
        default_shot_duration_ms__lt=MIN_VIDEO_DURATION_MS,
    ).update(default_shot_duration_ms=MIN_VIDEO_DURATION_MS)
    SeriesProject.objects.filter(
        default_shot_duration_ms__gt=MAX_VIDEO_DURATION_MS,
    ).update(default_shot_duration_ms=MAX_VIDEO_DURATION_MS)
    Shot.objects.filter(
        media_type="video", duration_ms__lt=MIN_VIDEO_DURATION_MS,
    ).update(duration_ms=MIN_VIDEO_DURATION_MS)
    Shot.objects.filter(
        media_type="video", duration_ms__gt=MAX_VIDEO_DURATION_MS,
    ).update(duration_ms=MAX_VIDEO_DURATION_MS)


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0006_alter_seriesproject_default_shot_duration_ms"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shot",
            name="duration_ms",
            field=models.PositiveIntegerField(
                default=5000,
                validators=[django.core.validators.MinValueValidator(100)],
            ),
        ),
        migrations.RunPython(clamp_video_shot_durations, migrations.RunPython.noop),
    ]
