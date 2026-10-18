from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0218_bible_setting_schema_default")]

    operations = [
        migrations.AddField(
            model_name="stagerun",
            name="selected_variant_route_index",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
