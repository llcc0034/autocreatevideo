from django.db import migrations


class Migration(migrations.Migration):
    # Retired duplicate values are discarded, never converted into authored
    # settings. Reversing the schema cannot restore the discarded text.
    dependencies = [("studio", "0216_script_bible_setting_uses")]

    operations = [
        migrations.RemoveField(model_name="characterbible", name="visual_identity"),
        migrations.RemoveField(model_name="scenebible", name="visual_identity"),
        migrations.RemoveField(model_name="propbible", name="visual_identity"),
    ]
