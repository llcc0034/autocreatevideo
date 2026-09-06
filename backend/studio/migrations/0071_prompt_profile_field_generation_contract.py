import django.core.validators
from django.db import migrations, models
from django.db.models import F


def populate_generation_sources(apps, schema_editor):
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Rule.objects.filter(generation_source_selector="").update(
        generation_source_selector=F("source_selector"),
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0070_prompt_field_validation_retry_policy")]

    operations = [
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_mode",
            field=models.CharField(
                choices=[
                    ("server_locked", "服务端锁定 / Server locked"),
                    ("model_generated", "模型生成 / Model generated"),
                    ("translate_only", "仅翻译 / Translate only"),
                    ("passthrough", "原值透传 / Passthrough"),
                ],
                default="passthrough",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_source_selector",
            field=models.CharField(
                blank=True,
                default="",
                help_text="受控点路径；历史 schema v1 可为空，新建 schema v2 规则必须显式填写。",
                max_length=191,
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_required",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="language_policy",
            field=models.CharField(
                default="inherit",
                help_text=(
                    "inherit、preserve_source、any、BCP 47 语言标签，或 "
                    "bilingual:<tag>+<tag>。"
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="length_unit",
            field=models.CharField(
                choices=[
                    ("none", "不限制 / None"),
                    ("characters", "字符数 / Characters"),
                    ("english_words", "英文词数 / English words"),
                    ("items", "列表项数 / Items"),
                ],
                default="none",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_min",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(30000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_max",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(30000),
                ],
            ),
        ),
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="generation_validator_keys",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(
            populate_generation_sources,
            migrations.RunPython.noop,
        ),
    ]
