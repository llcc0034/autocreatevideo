from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0053_prompt_normalization_durable_attempts"),
    ]

    operations = [
        migrations.AddField(
            model_name="runtimemodelavailability",
            name="observation_source",
            field=models.CharField(
                choices=[
                    ("name_only", "仅文件名"),
                    ("local_filesystem", "本机受控文件系统"),
                    ("remote_attestation", "远程受认证证明"),
                ],
                default="name_only",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="runtimemodelavailability",
            name="observed_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="runtimemodelavailability",
            name="observed_size_bytes",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
