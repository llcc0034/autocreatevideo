from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0217_remove_bible_visual_identity")]

    # Only new instances get the current default. Existing rows/hashes/reviews
    # are not converted; the retired v1 contract is rejected by runtime readers.
    operations = [
        migrations.AlterField(
            model_name="biblevariantrevision", name="payload_schema_version",
            field=models.PositiveSmallIntegerField(default=2),
        ),
    ]
