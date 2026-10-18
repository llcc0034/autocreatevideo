from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("studio", "0231_shot_field_revision")]

    # Only the superseded columns are dropped. Never fabricate independent
    # field identities/values or rewrite historical requests and approvals.
    # Reversing the schema cannot recover the discarded text; keep a backup
    # before applying this migration to a persistent database.
    operations = [
        migrations.RemoveField(model_name="shot", name="narrative"),
        migrations.RemoveField(model_name="shot", name="detailed_description"),
        migrations.RemoveField(model_name="shot", name="camera"),
        migrations.RemoveField(model_name="shot", name="action"),
        migrations.RemoveField(model_name="shot", name="sound_effects"),
        migrations.RemoveField(model_name="shot", name="music"),
        migrations.RemoveField(model_name="shot", name="continuity_notes"),
    ]
