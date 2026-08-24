from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0035_stage_snapshots_and_recipe_guard"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationattempt",
            name="claim_token",
            field=models.UUIDField(blank=True, db_index=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="request_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="request_snapshot_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="request_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="request_snapshot_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="worker_id",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="scriptrevision",
            name="origin_generation_attempt",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="generated_script_revision",
                to="studio.generationattempt",
            ),
        ),
    ]
