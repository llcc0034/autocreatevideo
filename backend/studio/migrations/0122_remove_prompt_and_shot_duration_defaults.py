from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0121_remove_retired_draft_graph"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shot",
            name="duration_ms",
            field=models.PositiveIntegerField(
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="promptartifact",
            name="total_duration_ms",
            field=models.PositiveIntegerField(),
        ),
    ]
