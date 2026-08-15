from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0021_remove_content_compliance_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="seriesproject",
            name="is_favorite",
            field=models.BooleanField(default=False),
        ),
    ]
