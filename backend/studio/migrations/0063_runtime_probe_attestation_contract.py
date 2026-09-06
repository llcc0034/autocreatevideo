from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0062_comfy_workflow_imports"),
    ]

    operations = [
        migrations.AddField(
            model_name="runtimehostprobeattempt",
            name="probe_kind",
            field=models.CharField(
                choices=[
                    ("capability", "主机能力"),
                    ("draft_model_artifact_attestation", "草稿模型文件证明"),
                ],
                db_index=True,
                default="capability",
                max_length=48,
            ),
        ),
        migrations.AddField(
            model_name="runtimehostprobeattempt",
            name="request_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="runtimehostprobeattempt",
            name="request_snapshot_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="runtimehostprobeattempt",
            name="request_snapshot_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="runtimehostprobeattempt",
            name="request_snapshot_sha256",
            field=models.CharField(
                default="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
                max_length=64,
            ),
        ),
    ]
