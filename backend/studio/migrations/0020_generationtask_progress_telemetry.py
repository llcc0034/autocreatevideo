from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0019_shot_high_detail_drafts"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationtask",
            name="progress_source",
            field=models.CharField(
                choices=[
                    ("none", "No quantitative progress"),
                    ("submission", "Submission milestone"),
                    ("comfyui", "ComfyUI node telemetry"),
                    ("terminal", "Terminal task state"),
                ],
                default="none",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="comfy_progress",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[django.core.validators.MaxValueValidator(100)],
            ),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="progress_node_id",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="progress_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="progress_telemetry_state",
            field=models.CharField(
                choices=[
                    ("unknown", "Not observed"),
                    ("connected", "WebSocket connected"),
                    ("receiving", "Progress telemetry received"),
                    ("unavailable", "Progress telemetry unavailable"),
                ],
                default="unknown",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="progress_telemetry_error",
            field=models.TextField(blank=True),
        ),
    ]
