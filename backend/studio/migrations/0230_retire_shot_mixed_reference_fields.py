from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("studio", "0229_optional_shot_use_description")]

    # No interpretation or backfill: old pools cannot establish visibility,
    # speakers or per-call selections. Restoring their data requires a backup.
    operations = [
        migrations.RemoveField(model_name="shot", name="characters"),
        migrations.RemoveField(model_name="shot", name="reference_assets"),
        migrations.RemoveField(model_name="shot", name="reference_bindings"),
    ]
