from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0009_classify_legacy_terminal_failures"),
    ]

    operations = [
        migrations.AddField(
            model_name="shot",
            name="reference_bindings",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
