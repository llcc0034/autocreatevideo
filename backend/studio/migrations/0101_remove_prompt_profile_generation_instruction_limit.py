from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0100_prompt_profile_generation_instruction_budget"),
    ]

    operations = [
        migrations.AlterField(
            model_name="promptprofilefieldrule",
            name="generation_instruction",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "版本化、不可执行的字段生成说明；"
                    "禁止模板、脚本和动态表达式。"
                ),
            ),
        ),
    ]
