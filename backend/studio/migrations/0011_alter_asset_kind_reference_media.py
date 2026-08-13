from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0010_shot_reference_bindings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="asset",
            name="kind",
            field=models.CharField(
                choices=[
                    ("character", "人物"),
                    ("scene", "场景"),
                    ("prop", "道具"),
                    ("reference", "参考"),
                    ("reference_video", "参考视频"),
                    ("reference_audio", "参考音频"),
                    ("audio", "音频"),
                    ("output_image", "生成图片"),
                    ("output_video", "生成视频"),
                ],
                max_length=32,
            ),
        ),
    ]
